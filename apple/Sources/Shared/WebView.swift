import SwiftUI
import UniformTypeIdentifiers
import WebKit

#if os(macOS)
import AppKit

struct WebView: NSViewRepresentable {
    let url: URL
    let reloadID: UUID
    let nativeCommand: NativeWebCommand?
    let onOpenConnectionSettings: () -> Void
    let onThemeChange: ((ColorScheme?) -> Void)?
    let onChromeStateChange: ((NativeChromeState) -> Void)?
    let onOpenFilePreview: () -> Void
    let onOpenBrowserLink: (URL) -> Void
    let onAgentResume: () -> Void
    let onConversationOverlayChange: (Bool) -> Void
    let browserWorkspaceVisible: Bool
    let browserWorkspaceWidth: CGFloat?

    func makeCoordinator() -> Coordinator {
        Coordinator(
            onThemeChange: onThemeChange,
            onChromeStateChange: onChromeStateChange,
            onOpenFilePreview: onOpenFilePreview,
            onOpenBrowserLink: onOpenBrowserLink,
            onAgentResume: onAgentResume,
            onConversationOverlayChange: onConversationOverlayChange
        )
    }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.userContentController.add(context.coordinator, name: "autoverseThemeChange")
        configuration.userContentController.add(context.coordinator, name: "autoverseNativeChrome")
        configuration.userContentController.add(context.coordinator, name: "autoverseFilePreview")
        configuration.userContentController.add(context.coordinator, name: "autoverseBrowserLink")
        configuration.userContentController.add(
            context.coordinator,
            name: "autoverseConversationOverlay"
        )
        configuration.userContentController.addScriptMessageHandler(
            context.coordinator,
            contentWorld: .page,
            name: "autoverseAgentResume"
        )
        configuration.userContentController.addUserScript(
            WKUserScript(
                source: """
                window.__autoverseNativeBrowserWorkspace = \(browserWorkspaceVisible);
                document.documentElement.classList.add('native-macos');
                document.documentElement.classList.toggle(
                  'native-browser-workspace', \(browserWorkspaceVisible)
                );
                window.__autoverseNativeBrowserWorkspaceWidth = \(
                    Self.browserWorkspaceWidthValue(browserWorkspaceWidth)
                );
                """,
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true
            )
        )
        configuration.userContentController.addUserScript(
            WKUserScript(
                source: Self.conversationOverlayObserverScript,
                injectionTime: .atDocumentEnd,
                forMainFrameOnly: true
            )
        )
        configuration.userContentController.addUserScript(
            WKUserScript(
                source: """
                (() => {
                  const send = () => {
                    const t = document.documentElement.dataset.theme;
                    window.webkit.messageHandlers.autoverseThemeChange.postMessage(t || "");
                  };
                  send();
                  new MutationObserver(() => send()).observe(
                    document.documentElement, {attributes: true, attributeFilter: ["data-theme"]}
                  );
                })();
                """,
                injectionTime: .atDocumentEnd,
                forMainFrameOnly: true
            )
        )
        let view = WKWebView(frame: .zero, configuration: configuration)
        // The WebUI makes only the native Browser reservation transparent while
        // an app-owned overlay is visible.  A clear under-page surface lets the
        // still-mounted Browser remain visible beneath the shared dimming mask.
        view.underPageBackgroundColor = .clear
        view.setValue(false, forKey: "drawsBackground")
        context.coordinator.loadedURL = url
        context.coordinator.reloadID = reloadID
        context.coordinator.browserWorkspaceVisible = browserWorkspaceVisible
        context.coordinator.browserWorkspaceWidth = browserWorkspaceWidth
        view.navigationDelegate = context.coordinator
        view.uiDelegate = context.coordinator
        view.allowsMagnification = true
        view.load(URLRequest(url: url))
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        if context.coordinator.loadedURL != url {
            context.coordinator.loadedURL = url
            view.load(URLRequest(url: url))
        } else if context.coordinator.reloadID != reloadID {
            context.coordinator.reloadID = reloadID
            view.reload()
        }
        let visibilityChanged =
            context.coordinator.browserWorkspaceVisible != browserWorkspaceVisible
        let widthChanged =
            context.coordinator.browserWorkspaceWidth != browserWorkspaceWidth
        if visibilityChanged {
            context.coordinator.browserWorkspaceVisible = browserWorkspaceVisible
            context.coordinator.browserWorkspaceWidth = browserWorkspaceWidth
            view.evaluateJavaScript(
                Self.browserWorkspaceScript(
                    browserWorkspaceVisible,
                    width: browserWorkspaceWidth
                )
            )
        } else if widthChanged {
            context.coordinator.browserWorkspaceWidth = browserWorkspaceWidth
            view.evaluateJavaScript(
                Self.browserWorkspaceWidthScript(browserWorkspaceWidth)
            )
        }
        if let nativeCommand,
           context.coordinator.nativeCommandID != nativeCommand.id {
            context.coordinator.nativeCommandID = nativeCommand.id
            if let data = try? JSONEncoder().encode(nativeCommand),
               let argument = String(data: data, encoding: .utf8) {
                InteractionSignposts.webDispatch(nativeCommand.id.uuidString)
                view.evaluateJavaScript("window.autoverseNativeAction?.(\(argument))")
            }
        }
    }

    static func dismantleNSView(_ nsView: WKWebView, coordinator: Coordinator) {
        nsView.navigationDelegate = nil
        nsView.uiDelegate = nil
        nsView.configuration.userContentController.removeScriptMessageHandler(
            forName: "autoverseThemeChange"
        )
        nsView.configuration.userContentController.removeScriptMessageHandler(
            forName: "autoverseNativeChrome"
        )
        nsView.configuration.userContentController.removeScriptMessageHandler(
            forName: "autoverseFilePreview"
        )
        nsView.configuration.userContentController.removeScriptMessageHandler(
            forName: "autoverseBrowserLink"
        )
        nsView.configuration.userContentController.removeScriptMessageHandler(
            forName: "autoverseConversationOverlay"
        )
        nsView.configuration.userContentController.removeScriptMessageHandler(
            forName: "autoverseAgentResume",
            contentWorld: .page
        )
    }

    private static func browserWorkspaceScript(
        _ visible: Bool,
        width: CGFloat?
    ) -> String {
        """
        window.__autoverseNativeBrowserWorkspace = \(visible);
        window.__autoverseNativeBrowserWorkspaceWidth = \(
            browserWorkspaceWidthValue(width)
        );
        document.documentElement.classList.toggle('native-browser-workspace', \(visible));
        window.autoverseSetNativeBrowserWorkspace?.(\(visible));
        """
    }

    private static func browserWorkspaceWidthScript(_ width: CGFloat?) -> String {
        let value = browserWorkspaceWidthValue(width)
        return """
        window.__autoverseNativeBrowserWorkspaceWidth = \(value);
        window.autoversePreviewNativeBrowserWorkspaceWidth?.(\(value));
        """
    }

    private static func browserWorkspaceWidthValue(_ width: CGFloat?) -> String {
        width.map { String(Double($0)) } ?? "null"
    }

    private static let conversationOverlayObserverScript = """
    (() => {
      const previous = { value: null };
      const send = () => {
        const visible = Array.from(
          document.querySelectorAll('.conversation-overlay')
        ).some((element) => !element.hidden);
        if (visible === previous.value) return;
        previous.value = visible;
        document.documentElement.classList.toggle(
          'native-conversation-overlay-active', visible
        );
        window.webkit.messageHandlers.autoverseConversationOverlay.postMessage(visible);
      };
      send();
      new MutationObserver(send).observe(document.documentElement, {
        attributes: true,
        attributeFilter: ['hidden'],
        childList: true,
        subtree: true
      });
    })();
    """

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate,
                             WKScriptMessageHandler, WKScriptMessageHandlerWithReply {
        var loadedURL: URL?
        var reloadID: UUID?
        var nativeCommandID: UUID?
        var browserWorkspaceVisible = false
        var browserWorkspaceWidth: CGFloat?
        let onThemeChange: ((ColorScheme?) -> Void)?
        let onChromeStateChange: ((NativeChromeState) -> Void)?
        let onOpenFilePreview: () -> Void
        let onOpenBrowserLink: (URL) -> Void
        let onAgentResume: () -> Void
        let onConversationOverlayChange: (Bool) -> Void
        private var lastTheme: String?
        private var lastChromeState: NativeChromeState?
        private var latestChromeActionID = ""

        init(
            onThemeChange: ((ColorScheme?) -> Void)?,
            onChromeStateChange: ((NativeChromeState) -> Void)?,
            onOpenFilePreview: @escaping () -> Void = {},
            onOpenBrowserLink: @escaping (URL) -> Void = { _ in },
            onAgentResume: @escaping () -> Void,
            onConversationOverlayChange: @escaping (Bool) -> Void = { _ in }
        ) {
            self.onThemeChange = onThemeChange
            self.onChromeStateChange = onChromeStateChange
            self.onOpenFilePreview = onOpenFilePreview
            self.onOpenBrowserLink = onOpenBrowserLink
            self.onAgentResume = onAgentResume
            self.onConversationOverlayChange = onConversationOverlayChange
        }

        func userContentController(
            _ userContentController: WKUserContentController,
            didReceive message: WKScriptMessage
        ) {
            switch message.name {
            case "autoverseThemeChange":
                guard let theme = message.body as? String,
                      theme != lastTheme else { return }
                lastTheme = theme
                DispatchQueue.main.async {
                    switch theme {
                    case "light": self.onThemeChange?(.light)
                    case "dark":  self.onThemeChange?(.dark)
                    default:      self.onThemeChange?(nil)
                    }
                }
            case "autoverseNativeChrome":
                guard let value = message.body as? [String: Any] else { return }
                let actionID = value["actionId"] as? String ?? ""
                let phase = value["phase"] as? String ?? "settled"
                if phase == "start" && !actionID.isEmpty {
                    latestChromeActionID = actionID
                } else if phase == "settled" && !actionID.isEmpty
                            && !latestChromeActionID.isEmpty
                            && actionID != latestChromeActionID {
                    return
                }
                let state = NativeChromeState(
                    sessionTitle: value["sessionTitle"] as? String ?? "AutoVerse",
                    leftWidth: CGFloat((value["leftWidth"] as? NSNumber)?.doubleValue ?? 260),
                    rightWidth: CGFloat((value["rightWidth"] as? NSNumber)?.doubleValue ?? 336),
                    leftVisible: (value["leftVisible"] as? NSNumber)?.boolValue ?? true,
                    rightVisible: (value["rightVisible"] as? NSNumber)?.boolValue ?? true,
                    actionID: actionID,
                    phase: phase,
                    motionToken: value["motionToken"] as? String ?? "panel"
                )
                guard state != lastChromeState else { return }
                lastChromeState = state
                InteractionSignposts.transition(phase, actionID: actionID)
                DispatchQueue.main.async {
                    self.onChromeStateChange?(state)
                }
            case "autoverseFilePreview":
                guard let value = message.body as? [String: Any],
                      value["action"] as? String == "open" else { return }
                DispatchQueue.main.async {
                    self.onOpenFilePreview()
                }
            case "autoverseBrowserLink":
                guard let value = message.body as? [String: Any],
                      value["action"] as? String == "open",
                      let rawURL = value["url"] as? String,
                      let url = URL(string: rawURL),
                      ["http", "https"].contains(url.scheme?.lowercased() ?? "") else { return }
                DispatchQueue.main.async {
                    self.onOpenBrowserLink(url)
                }
            case "autoverseConversationOverlay":
                guard let value = message.body as? NSNumber else { return }
                DispatchQueue.main.async {
                    self.onConversationOverlayChange(value.boolValue)
                }
            default:
                return
            }
        }

        @MainActor
        func userContentController(
            _ userContentController: WKUserContentController,
            didReceive message: WKScriptMessage
        ) async -> (Any?, String?) {
            onAgentResume()
            return (["resumed": true], nil)
        }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void
        ) {
            guard navigationAction.navigationType == .linkActivated,
                  let target = navigationAction.request.url,
                  let loadedURL,
                  target.host != loadedURL.host else {
                decisionHandler(.allow)
                return
            }
            onOpenBrowserLink(target)
            decisionHandler(.cancel)
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation?) {
            // The HTML navigation finishes before asynchronous session restore
            // necessarily does. A few bounded, idempotent snapshots cover both
            // orderings without leaving a permanent timer in either layer.
            for delay in [0.0, 0.25, 1.0] {
                DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak webView] in
                    guard let webView else { return }
                    webView.evaluateJavaScript(
                        WebView.browserWorkspaceScript(
                            self.browserWorkspaceVisible,
                            width: self.browserWorkspaceWidth
                        )
                    )
                    webView.evaluateJavaScript("window.autoverseNativeSync?.()")
                }
            }
        }

        func webView(
            _ webView: WKWebView,
            runOpenPanelWith parameters: WKOpenPanelParameters,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping @MainActor @Sendable ([URL]?) -> Void
        ) {
            let panel = NSOpenPanel()
            panel.allowedContentTypes = [.image]
            panel.allowsMultipleSelection = parameters.allowsMultipleSelection
            panel.canChooseFiles = true
            panel.canChooseDirectories = false
            panel.resolvesAliases = true
            panel.begin { response in
                completionHandler(response == .OK ? panel.urls : nil)
            }
        }
    }
}
#else
import UIKit
import ObjectiveC
import PhotosUI

/// Donor class whose `inputAccessoryView` getter (returning nil) is grafted
/// onto a runtime subclass of the web view's internal content view.
private final class _NoAccessoryDonor: NSObject {
    @objc var inputAccessoryView: UIView? { nil }
}

/// Strip the default iOS form-assistant bar (‹ › prev/next + Done) that
/// WebKit shows above the keyboard for web inputs. The bar is attached to the
/// internal `WKContentView` (the actual first responder), not to `WKWebView`,
/// so overriding `WKWebView.inputAccessoryView` has no effect. Instead we
/// make a per-instance runtime subclass whose `inputAccessoryView` returns nil
/// and reassign just this content view to it — global state is untouched.
///
/// Returns true once the content view was found and patched. Callers retry
/// until then because, with `suppressesIncrementalRendering`, the content view
/// can be inserted after `didFinish`.
@MainActor
@discardableResult
private func disableInputAccessory(on webView: WKWebView) -> Bool {
    func findContentView(_ view: UIView) -> UIView? {
        for sub in view.subviews {
            if String(describing: type(of: sub)).hasPrefix("WKContent") { return sub }
            if let found = findContentView(sub) { return found }
        }
        return nil
    }
    guard let contentView = findContentView(webView),
          let baseClass: AnyClass = object_getClass(contentView) else { return false }

    let subclassName = "\(NSStringFromClass(baseClass))_NoInputAccessory"
    if let cached = NSClassFromString(subclassName) {
        if object_getClass(contentView) != cached { object_setClass(contentView, cached) }
    } else if let subclass = objc_allocateClassPair(baseClass, subclassName, 0) {
        if let donor = class_getInstanceMethod(
            _NoAccessoryDonor.self,
            #selector(getter: _NoAccessoryDonor.inputAccessoryView)
        ) {
            class_addMethod(
                subclass,
                #selector(getter: UIResponder.inputAccessoryView),
                method_getImplementation(donor),
                method_getTypeEncoding(donor)
            )
        }
        objc_registerClassPair(subclass)
        object_setClass(contentView, subclass)
    } else {
        return false
    }

    // If the field is already focused the bar is on screen — re-query the
    // (now nil) accessory so it disappears immediately instead of next focus.
    if contentView.isFirstResponder { contentView.reloadInputViews() }
    return true
}

struct WebView: UIViewRepresentable {
    let url: URL
    let reloadID: UUID
    let nativeCommand: NativeWebCommand?
    let onOpenConnectionSettings: () -> Void
    let onThemeChange: ((ColorScheme?) -> Void)?
    let onChromeStateChange: ((NativeChromeState) -> Void)?
    let onOpenFilePreview: () -> Void
    let onOpenBrowserLink: (URL) -> Void
    let onAgentResume: () -> Void
    let onConversationOverlayChange: (Bool) -> Void
    let browserWorkspaceVisible: Bool
    let browserWorkspaceWidth: CGFloat?

    func makeCoordinator() -> Coordinator {
        Coordinator(
            onOpenConnectionSettings: onOpenConnectionSettings,
            onOpenBrowserLink: onOpenBrowserLink,
            onAgentResume: onAgentResume
        )
    }

    func makeUIView(context: Context) -> UIView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        // Skip partial paint frames — the chat UI mutates the DOM at high
        // frequency during SSE streaming; deferring until each frame is ready
        // avoids checkerboarding and reduces GPU pressure on mobile.
        configuration.suppressesIncrementalRendering = true
        configuration.userContentController.add(
            context.coordinator,
            name: "autoverseNative"
        )
        configuration.userContentController.add(
            context.coordinator,
            name: "autoverseBrowserLink"
        )
        configuration.userContentController.addScriptMessageHandler(
            context.coordinator,
            contentWorld: .page,
            name: "autoverseAgentResume"
        )
        configuration.userContentController.addUserScript(
            WKUserScript(
                source: "document.documentElement.classList.add('native-ios')",
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true
            )
        )
        configuration.userContentController.addUserScript(
            WKUserScript(
                source: """
                (() => {
                  const pick = (event) => {
                    event.preventDefault();
                    event.stopImmediatePropagation();
                    window.webkit.messageHandlers.autoverseNative.postMessage({ action: "pickImages" });
                  };
                  const attach = () => {
                    const btn = document.getElementById("btnImage");
                    if (!btn || btn.dataset.nativeImagePicker === "1") return;
                    btn.dataset.nativeImagePicker = "1";
                    btn.addEventListener("click", pick, true);
                    btn.addEventListener("keydown", (event) => {
                      if (event.key === "Enter" || event.key === " ") pick(event);
                    }, true);
                  };
                  if (document.readyState === "loading") {
                    document.addEventListener("DOMContentLoaded", attach, { once: true });
                  } else {
                    attach();
                  }
                  new MutationObserver(attach).observe(document.documentElement, {
                    childList: true,
                    subtree: true
                  });
                })();
                """,
                injectionTime: .atDocumentEnd,
                forMainFrameOnly: true
            )
        )
        let webView = WKWebView(frame: .zero, configuration: configuration)
        context.coordinator.loadedURL = url
        context.coordinator.reloadID = reloadID
        webView.navigationDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.isOpaque = false
        webView.backgroundColor = .black
        webView.underPageBackgroundColor = .black
        webView.scrollView.backgroundColor = .black
        // The page body is `position: fixed` and never scrolls as a whole — only
        // the inner `.log` does, and that uses WebKit's own internal overflow
        // scroller (independent of this outer scroll view). Disabling the main
        // document scroll view stops WebKit from auto-scrolling the page to
        // "reveal" the focused composer when the keyboard appears. That auto
        // scroll is what dragged the whole page (top bar included) up and back
        // down on focus — disabling it keeps the top bar rock-steady.
        webView.scrollView.isScrollEnabled = false
        webView.scrollView.bounces = false

        // Host the web view in a container; the web view simply fills it (and the
        // screen) at all times. The keyboard is handled inside the page via
        // --kb-inset (app.js + index.html), not by resizing this frame.
        let container = UIView()
        container.backgroundColor = .black
        webView.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(webView)
        // The web view is pinned to fill the container (and thus the screen) at
        // all times. We deliberately do NOT animate its frame with the keyboard:
        // animating a WKWebView's bounds makes WebKit relayout in discrete steps,
        // so the page juddered on every keyboard show. Instead the keyboard is
        // handled inside the page — app.js measures it into the --kb-inset CSS var
        // and lifts only the composer — and the scroll view is disabled (above)
        // so the page can never shift, which keeps the top bar perfectly still.
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: container.topAnchor),
            webView.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])

        context.coordinator.container = container
        context.coordinator.webView = webView

        webView.load(URLRequest(url: url))
        context.coordinator.scheduleStripInputAccessory()
        return container
    }

    func updateUIView(_ view: UIView, context: Context) {
        guard let webView = context.coordinator.webView else { return }
        if context.coordinator.loadedURL != url {
            context.coordinator.loadedURL = url
            webView.load(URLRequest(url: url))
        } else if context.coordinator.reloadID != reloadID {
            context.coordinator.reloadID = reloadID
            webView.reload()
        }
    }

    static func dismantleUIView(_ uiView: UIView, coordinator: Coordinator) {
        coordinator.stopObservingKeyboard()
        coordinator.webView?.navigationDelegate = nil
        coordinator.webView?.configuration.userContentController.removeScriptMessageHandler(
            forName: "autoverseNative"
        )
        coordinator.webView?.configuration.userContentController.removeScriptMessageHandler(
            forName: "autoverseBrowserLink"
        )
        coordinator.webView?.configuration.userContentController.removeScriptMessageHandler(
            forName: "autoverseAgentResume",
            contentWorld: .page
        )
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKScriptMessageHandler,
                             WKScriptMessageHandlerWithReply, PHPickerViewControllerDelegate {
        var loadedURL: URL?
        var reloadID: UUID?
        weak var container: UIView?
        weak var webView: WKWebView?
        private var accessoryStripped = false
        private let onOpenConnectionSettings: () -> Void
        private let onOpenBrowserLink: (URL) -> Void
        private let onAgentResume: () -> Void

        init(
            onOpenConnectionSettings: @escaping () -> Void,
            onOpenBrowserLink: @escaping (URL) -> Void,
            onAgentResume: @escaping () -> Void
        ) {
            self.onOpenConnectionSettings = onOpenConnectionSettings
            self.onOpenBrowserLink = onOpenBrowserLink
            self.onAgentResume = onAgentResume
        }

        // ── input accessory bar removal ──────────────────────────────────────
        /// Try once to patch the content view; cache success so we stop retrying.
        func stripInputAccessory() {
            guard !accessoryStripped, let webView else { return }
            if disableInputAccessory(on: webView) { accessoryStripped = true }
        }

        /// The internal WKContentView can be inserted any time around the first
        /// render (later still with `suppressesIncrementalRendering`), so poke
        /// it at a few moments until it sticks.
        func scheduleStripInputAccessory() {
            for delayMs in [0, 150, 400, 800, 1500] {
                Task { @MainActor [weak self] in
                    if delayMs > 0 {
                        try? await Task.sleep(nanoseconds: UInt64(delayMs) * 1_000_000)
                    }
                    self?.stripInputAccessory()
                }
            }
        }

        // ── keyboard handling ────────────────────────────────────────────────
        // The keyboard is handled entirely inside the page now: app.js measures
        // it via visualViewport into the --kb-inset CSS var and lifts only the
        // composer above it, while the top bar and log stay put. We no longer
        // animate the web view's frame (that made WebKit relayout in discrete
        // steps and judder on every keyboard show), so there is nothing to
        // observe here. `stopObservingKeyboard` is kept as a harmless no-op for
        // the dismantle path.
        func stopObservingKeyboard() {
            NotificationCenter.default.removeObserver(self)
        }

        // ── image attachments ──────────────────────────────────────────────
        func pickImages() {
            guard let webView,
                  let presenter = webView.window?.rootViewController?.topPresentedViewController else {
                return
            }
            var config = PHPickerConfiguration(photoLibrary: .shared())
            config.filter = .images
            config.selectionLimit = 4
            config.preferredAssetRepresentationMode = .current
            let picker = PHPickerViewController(configuration: config)
            picker.delegate = self
            presenter.present(picker, animated: true)
        }

        func picker(_ picker: PHPickerViewController, didFinishPicking results: [PHPickerResult]) {
            picker.dismiss(animated: true)
            guard !results.isEmpty else { return }
            Task { [weak self] in
                let payloads = await Self.imagePayloads(from: Array(results.prefix(4)))
                await MainActor.run {
                    self?.deliverImagePayloads(payloads)
                }
            }
        }

        private func deliverImagePayloads(_ payloads: [[String: String]]) {
            guard !payloads.isEmpty,
                  let data = try? JSONSerialization.data(withJSONObject: payloads),
                  let json = String(data: data, encoding: .utf8) else { return }
            webView?.evaluateJavaScript("window.autoverseAddNativeImages && window.autoverseAddNativeImages(\(json));")
        }

        private nonisolated static func imagePayloads(
            from results: [PHPickerResult]
        ) async -> [[String: String]] {
            var payloads: [[String: String]] = []
            for (index, result) in results.enumerated() {
                if let image = await nativeImage(from: result.itemProvider, index: index) {
                    payloads.append(image)
                }
            }
            return payloads
        }

        private nonisolated static func nativeImage(
            from provider: NSItemProvider,
            index: Int
        ) async -> [String: String]? {
            if let direct = await supportedImageData(from: provider, index: index) {
                return direct
            }
            guard provider.canLoadObject(ofClass: UIImage.self),
                  let image = await loadUIImage(from: provider),
                  let data = image.jpegData(compressionQuality: 0.9) else {
                return nil
            }
            return [
                "media_type": "image/jpeg",
                "data": data.base64EncodedString(),
                "name": "photo-\(index + 1).jpg",
            ]
        }

        private nonisolated static func supportedImageData(
            from provider: NSItemProvider,
            index: Int
        ) async -> [String: String]? {
            let supported: [(id: String, media: String, ext: String)] = [
                (UTType.png.identifier, "image/png", "png"),
                (UTType.jpeg.identifier, "image/jpeg", "jpg"),
                (UTType.gif.identifier, "image/gif", "gif"),
                ("org.webmproject.webp", "image/webp", "webp"),
            ]
            guard let type = supported.first(where: {
                provider.registeredTypeIdentifiers.contains($0.id)
            }) else {
                return nil
            }
            guard let data = await loadData(from: provider, typeIdentifier: type.id) else {
                return nil
            }
            return [
                "media_type": type.media,
                "data": data.base64EncodedString(),
                "name": "photo-\(index + 1).\(type.ext)",
            ]
        }

        private nonisolated static func loadData(
            from provider: NSItemProvider,
            typeIdentifier: String
        ) async -> Data? {
            await withCheckedContinuation { continuation in
                provider.loadDataRepresentation(forTypeIdentifier: typeIdentifier) { data, _ in
                    continuation.resume(returning: data)
                }
            }
        }

        private nonisolated static func loadUIImage(from provider: NSItemProvider) async -> UIImage? {
            await withCheckedContinuation { continuation in
                provider.loadObject(ofClass: UIImage.self) { object, _ in
                    continuation.resume(returning: object as? UIImage)
                }
            }
        }

        func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
            // A fresh navigation may rebuild the content view — re-arm so it
            // gets patched again rather than relying on the stale success flag.
            accessoryStripped = false
            stripInputAccessory()
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            // The internal WKContentView only exists once content has rendered;
            // strip its form-assistant accessory bar now (idempotent), plus the
            // scheduled retries and the keyboard hook cover late insertion.
            stripInputAccessory()
            scheduleStripInputAccessory()
        }

        func userContentController(
            _ userContentController: WKUserContentController,
            didReceive message: WKScriptMessage
        ) {
            guard let body = message.body as? [String: Any],
                  let action = body["action"] as? String else {
                return
            }
            if message.name == "autoverseBrowserLink" {
                guard action == "open",
                      let rawURL = body["url"] as? String,
                      let url = URL(string: rawURL),
                      ["http", "https"].contains(url.scheme?.lowercased() ?? "") else { return }
                onOpenBrowserLink(url)
                return
            }
            guard message.name == "autoverseNative" else { return }
            switch action {
            case "connectionSettings":
                onOpenConnectionSettings()
            case "pickImages":
                pickImages()
            default:
                break
            }
        }

        func userContentController(
            _ userContentController: WKUserContentController,
            didReceive message: WKScriptMessage
        ) async -> (Any?, String?) {
            onAgentResume()
            return (["resumed": true], nil)
        }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void
        ) {
            guard navigationAction.navigationType == .linkActivated,
                  let target = navigationAction.request.url,
                  let loadedURL,
                  target.host != loadedURL.host else {
                decisionHandler(.allow)
                return
            }
            onOpenBrowserLink(target)
            decisionHandler(.cancel)
        }
    }
}

private extension UIViewController {
    var topPresentedViewController: UIViewController {
        var top = self
        while let presented = top.presentedViewController {
            top = presented
        }
        return top
    }
}
#endif
