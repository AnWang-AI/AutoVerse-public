#if os(macOS)
import AppKit
import Foundation
import Network
import SwiftUI
import Testing
import WebKit
@testable import AutoVerseApple

@Suite("Native browser command surface")
@MainActor
struct BrowserHostCommandTests {
    @Test("Clamps the transient browser workspace and its default size")
    func clampsBrowserWorkspaceWidth() {
        #expect(BrowserWorkspaceLayout.dividerWidth == 40)
        #expect(BrowserWorkspaceLayout.minimumBrowserWidth == 320)
        #expect(BrowserWorkspaceLayout.comfortableConversationWidth == 520)
        #expect(BrowserWorkspaceLayout.minimumConversationWidth == 520)
        #expect(BrowserWorkspaceLayout.defaultBrowserWidth(available: 1_000) == 600)
        #expect(BrowserWorkspaceLayout.defaultBrowserWidth(available: 1_280) == 640)
        #expect(BrowserWorkspaceLayout.defaultBrowserWidth(available: 2_000) == 900)
        #expect(BrowserChromeMetrics.toolbarHeight == 46)
        #expect(BrowserChromeMetrics.addressFieldHeight == 34)
        #expect(BrowserChromeMetrics.addressFieldMinimumWidth == 96)
        #expect(BrowserChromeMetrics.addressFieldHorizontalPadding == 12)
        #expect(BrowserChromeMetrics.addressFieldCornerRadius == 10)
        #expect(
            BrowserChromeMetrics.addressFieldHeight
                <= BrowserChromeMetrics.toolbarHeight - 8
        )
        #expect(BrowserChromeMetrics.tabStripHeight == 40)
        #expect(BrowserChromeMetrics.tabHeight == 40)
        #expect(BrowserChromeMetrics.tabHeight == BrowserChromeMetrics.tabStripHeight)
        #expect(BrowserChromeMetrics.tabSpacing == 0)
        #expect(BrowserChromeMetrics.tabMinimumWidth == 120)
        #expect(BrowserChromeMetrics.tabMaximumWidth == 196)
        #expect(BrowserChromeMetrics.tabSeparatorWidth == 1)
        #expect(BrowserChromeMetrics.tabSelectionIndicatorHeight == 2)
        #expect(BrowserChromeMetrics.newPageButtonWidth == 40)
        #expect(BrowserChromeMetrics.activityButtonWidth == 108)
        #expect(
            BrowserWorkspaceLayout.browserWidth(
                preferred: 620, available: 1_280, sessionsWidth: 0
            )
                == 620
        )
        #expect(
            BrowserWorkspaceLayout.browserWidth(
                preferred: 100, available: 1_280, sessionsWidth: 0
            )
                == 320
        )
        #expect(
            BrowserWorkspaceLayout.browserWidth(
                preferred: 900, available: 1_280, sessionsWidth: 0
            )
                == 760
        )
        #expect(
            BrowserWorkspaceLayout.browserWidth(
                preferred: 1_200, available: 1_280, sessionsWidth: 0
            )
                == 760
        )
        #expect(
            BrowserWorkspaceLayout.browserWidth(
                preferred: 620, available: 880, sessionsWidth: 0
            ) == 360
        )
        #expect(
            BrowserWorkspaceLayout.browserWidth(
                preferred: 620, available: 1_200, sessionsWidth: 260
            ) == 420
        )
        #expect(
            BrowserWorkspaceLayout.maximumBrowserWidth(
                available: 1_280, sessionsWidth: 0
            ) == 760
        )
        #expect(
            BrowserWorkspaceLayout.maximumBrowserWidth(
                available: 1_200, sessionsWidth: 260
            ) == 420
        )
        #expect(
            !BrowserWorkspaceLayout.shouldCollapseSessions(
                preferredBrowserWidth: 500,
                available: 1_280,
                sessionsWidth: 260
            )
        )
        #expect(
            BrowserWorkspaceLayout.shouldCollapseSessions(
                preferredBrowserWidth: 501,
                available: 1_280,
                sessionsWidth: 260
            )
        )
        #expect(
            !BrowserWorkspaceLayout.shouldCollapseSessions(
                preferredBrowserWidth: 1_000,
                available: 1_280,
                sessionsWidth: 0
            )
        )
    }

    @Test("Resize handle wins its full hit area over WebKit and tracks dragging")
    func resizeHandleCapturesFullHitArea() throws {
        var began = 0
        var translations: [CGFloat] = []
        var ended = 0
        var resets = 0
        let handle = BrowserWorkspaceResizeView(
            onDragBegan: { began += 1 },
            onDragChanged: { translations.append($0) },
            onDragEnded: { ended += 1 },
            onDoubleClick: { resets += 1 }
        )
        let container = NSView(
            frame: NSRect(x: 0, y: 0, width: 800, height: 600)
        )
        let webView = WKWebView(frame: container.bounds)
        container.addSubview(webView)
        handle.frame = NSRect(
            x: 380,
            y: 0,
            width: BrowserWorkspaceLayout.dividerWidth,
            height: 600
        )
        container.addSubview(handle, positioned: .above, relativeTo: webView)

        #expect(container.hitTest(NSPoint(x: 380.5, y: 300)) === handle)
        #expect(
            container.hitTest(
                NSPoint(
                    x: 380 + BrowserWorkspaceLayout.dividerWidth - 0.5,
                    y: 300
                )
            ) === handle
        )
        #expect(handle.hitTest(NSPoint(x: 379.5, y: 300)) == nil)
        #expect(
            handle.hitTest(
                NSPoint(
                    x: 380 + BrowserWorkspaceLayout.dividerWidth + 0.5,
                    y: 300
                )
            ) == nil
        )
        #expect(handle.acceptsFirstMouse(for: nil))

        handle.mouseDown(with: try mouseEvent(type: .leftMouseDown, x: 12))
        handle.mouseDragged(with: try mouseEvent(type: .leftMouseDragged, x: 31))
        handle.mouseUp(with: try mouseEvent(type: .leftMouseUp, x: 31))

        #expect(began == 1)
        #expect(translations == [19])
        #expect(ended == 1)

        handle.mouseDown(
            with: try mouseEvent(type: .leftMouseDown, x: 20, clickCount: 2)
        )
        #expect(resets == 1)
        #expect(began == 1)
    }

    @Test("SwiftUI wrapper gives the native resize handle the full target")
    func resizeHandleFillsSwiftUITarget() throws {
        let target = ZStack {
            BrowserWorkspaceResizeHandle(
                onDragBegan: {},
                onDragChanged: { _ in },
                onDragEnded: {},
                onDoubleClick: {}
            )
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(
            width: BrowserWorkspaceLayout.dividerWidth,
            height: 600
        )
        let hostingView = NSHostingView(rootView: target)
        hostingView.frame = NSRect(
            x: 0,
            y: 0,
            width: BrowserWorkspaceLayout.dividerWidth,
            height: 600
        )
        hostingView.layoutSubtreeIfNeeded()

        let nativeHandle = try #require(
            descendant(
                of: hostingView,
                type: BrowserWorkspaceResizeView.self
            )
        )
        #expect(nativeHandle.frame.width == BrowserWorkspaceLayout.dividerWidth)
        #expect(nativeHandle.frame.height == 600)
    }

    private func descendant<ViewType: NSView>(
        of view: NSView,
        type: ViewType.Type
    ) -> ViewType? {
        if let match = view as? ViewType {
            return match
        }
        for subview in view.subviews {
            if let match = descendant(of: subview, type: type) {
                return match
            }
        }
        return nil
    }

    private func mouseEvent(
        type: NSEvent.EventType,
        x: CGFloat,
        clickCount: Int = 1
    ) throws -> NSEvent {
        try #require(
            NSEvent.mouseEvent(
                with: type,
                location: NSPoint(x: x, y: 300),
                modifierFlags: [],
                timestamp: 0,
                windowNumber: 0,
                context: nil,
                eventNumber: 0,
                clickCount: clickCount,
                pressure: 0
            )
        )
    }

    @Test("Builds a version-matched Safari-compatible identity")
    func buildsSafariCompatibleIdentity() {
        let applicationName = BrowserUserAgent.makeApplicationName(
            safariVersion: "27.0",
            webKitBundleVersion: "22625.1.24.11.2",
            appVersion: "0.1.0"
        )

        #expect(
            applicationName
                == "Version/27.0 Safari/625.1.24.11.2 AutoVerse/0.1.0"
        )
        #expect(
            BrowserUserAgent.makeApplicationName(
                safariVersion: "invalid version",
                webKitBundleVersion: nil,
                appVersion: nil
            ) == "Version/17.0 Safari/605.1.15 AutoVerse/0.1.0"
        )
    }

    @Test("Ignores WebKit cancellation when a navigation is superseded")
    func recognizesSupersededNavigation() {
        let cancelled = NSError(
            domain: NSURLErrorDomain,
            code: NSURLErrorCancelled
        )
        let failed = NSError(
            domain: NSURLErrorDomain,
            code: NSURLErrorCannotConnectToHost
        )

        #expect(BrowserHostController.isCancelledNavigationError(cancelled))
        #expect(!BrowserHostController.isCancelledNavigationError(failed))
    }

    @Test("Resumes agent control through the acknowledged chat bridge")
    func resumesAgentControlFromChatBridge() async throws {
        let browser = BrowserHostController()
        browser.takeControl()
        let coordinator = WebView.Coordinator(
            onThemeChange: nil,
            onChromeStateChange: nil,
            onAgentResume: browser.resumeAgentControl
        )
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.addScriptMessageHandler(
            coordinator,
            contentWorld: .page,
            name: "autoverseAgentResume"
        )
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.loadHTMLString(
            """
            <script>
            window.webkit.messageHandlers.autoverseAgentResume
              .postMessage({source: "message_submit"})
              .then(result => document.title = result.resumed ? "resumed" : "failed");
            </script>
            """,
            baseURL: nil
        )

        var title = ""
        for _ in 0..<50 {
            title = (try? await webView.evaluateJavaScript("document.title") as? String) ?? ""
            if title == "resumed" { break }
            try await Task.sleep(for: .milliseconds(20))
        }

        #expect(title == "resumed")
        #expect(browser.controlOwner == .agent)
        #expect(browser.lastAction == "Agent resumed")
        configuration.userContentController.removeScriptMessageHandler(
            forName: "autoverseAgentResume",
            contentWorld: .page
        )
    }

    @Test("Publishes bounded visual activity only while an agent command runs")
    func publishesAgentCommandActivity() async throws {
        let browser = BrowserHostController()
        browser.evaluationTimeoutMilliseconds = 1_000
        browser.agentActivityMinimumDisplayMilliseconds = 180

        _ = try await browser.execute(action: "launch", params: [:])
        #expect(browser.agentActivity == BrowserAgentActivity(action: "launch"))
        for _ in 0..<100 {
            if browser.agentActivity == nil { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(browser.agentActivity == nil)

        browser.agentActivityMinimumDisplayMilliseconds = 0
        let task = Task<Void, Error> { @MainActor in
            _ = try await browser.execute(
                action: "evaluate",
                params: [
                    "script": "return await new Promise(resolve => setTimeout(() => resolve(true), 180));"
                ]
            )
        }

        for _ in 0..<100 {
            if browser.agentActivity?.action == "evaluate" { break }
            try await Task.sleep(for: .milliseconds(10))
        }

        #expect(browser.agentActivity == BrowserAgentActivity(action: "evaluate"))
        #expect(browser.agentActivity?.label == "Running script")
        #expect(BrowserAgentActivity(action: "navigate").emphasizesAddress)
        #expect(!BrowserAgentActivity(action: "click").emphasizesAddress)
        _ = try await task.value
        #expect(browser.agentActivity == nil)

        _ = try await browser.execute(action: "list_pages", params: [:])
        #expect(browser.agentActivity == nil)

        await #expect(throws: BrowserHostError.self) {
            _ = try await browser.execute(action: "unsupported", params: [:])
        }
        #expect(browser.agentActivity == nil)

        let cancellationTask = Task<Void, Error> { @MainActor in
            _ = try await browser.execute(
                action: "wait",
                params: [
                    "selector": "#never-present", "condition": "visible",
                    "timeout": 1_000,
                ]
            )
        }
        for _ in 0..<20 {
            if browser.agentActivity?.action == "wait" { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(browser.agentActivity?.action == "wait")
        cancellationTask.cancel()
        await #expect(throws: CancellationError.self) {
            try await cancellationTask.value
        }
        #expect(browser.agentActivity == nil)

        browser.agentActivityMinimumDisplayMilliseconds = 500
        _ = try await browser.execute(action: "launch", params: [:])
        #expect(browser.agentActivity != nil)
        browser.takeControl()
        #expect(browser.agentActivity == nil)
        await #expect(throws: BrowserHostError.self) {
            _ = try await browser.execute(action: "click", params: ["selector": "body"])
        }
        #expect(browser.agentActivity == nil)
    }

    @Test("Yields the browser workspace to a chat file preview")
    func yieldsBrowserWorkspaceToFilePreview() async throws {
        var opened = false
        let coordinator = WebView.Coordinator(
            onThemeChange: nil,
            onChromeStateChange: nil,
            onOpenFilePreview: { opened = true },
            onAgentResume: {}
        )
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(
            coordinator,
            name: "autoverseFilePreview"
        )
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.loadHTMLString(
            """
            <script>
            window.webkit.messageHandlers.autoverseFilePreview
              .postMessage({action: "open"});
            </script>
            """,
            baseURL: nil
        )

        for _ in 0..<50 {
            if opened { break }
            try await Task.sleep(for: .milliseconds(20))
        }

        #expect(opened)
        configuration.userContentController.removeScriptMessageHandler(
            forName: "autoverseFilePreview"
        )
    }

    @Test("Reports app overlay visibility through the native layering bridge")
    func reportsConversationOverlayVisibility() async throws {
        var states: [Bool] = []
        let coordinator = WebView.Coordinator(
            onThemeChange: nil,
            onChromeStateChange: nil,
            onAgentResume: {},
            onConversationOverlayChange: { states.append($0) }
        )
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(
            coordinator,
            name: "autoverseConversationOverlay"
        )
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.loadHTMLString(
            """
            <script>
            window.webkit.messageHandlers.autoverseConversationOverlay.postMessage(true);
            window.setTimeout(() => {
              window.webkit.messageHandlers.autoverseConversationOverlay.postMessage(false);
            }, 25);
            </script>
            """,
            baseURL: nil
        )

        for _ in 0..<50 {
            if states == [true, false] { break }
            try await Task.sleep(for: .milliseconds(20))
        }

        #expect(states == [true, false])
        configuration.userContentController.removeScriptMessageHandler(
            forName: "autoverseConversationOverlay"
        )
    }

    @Test("Routes chat web links through the native Browser bridge")
    func routesChatLinksToBrowserBridge() async throws {
        var openedURL: URL?
        let coordinator = WebView.Coordinator(
            onThemeChange: nil,
            onChromeStateChange: nil,
            onOpenBrowserLink: { openedURL = $0 },
            onAgentResume: {}
        )
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(
            coordinator,
            name: "autoverseBrowserLink"
        )
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.loadHTMLString(
            """
            <script>
            window.webkit.messageHandlers.autoverseBrowserLink.postMessage({
              action: "open",
              url: "https://example.com/from-chat"
            });
            </script>
            """,
            baseURL: nil
        )

        for _ in 0..<50 {
            if openedURL != nil { break }
            try await Task.sleep(for: .milliseconds(20))
        }

        #expect(openedURL?.absoluteString == "https://example.com/from-chat")
        configuration.userContentController.removeScriptMessageHandler(
            forName: "autoverseBrowserLink"
        )
    }

    @Test("Opens an allowed chat link in a distinct visible page")
    func opensChatLinkInNewPage() async throws {
        let fixture = try await LocalHTTPFixture.start(body: Self.fixture)
        defer { fixture.stop() }
        let controller = BrowserHostController()
        let originalPageID = try #require(controller.activePageID)

        #expect(!controller.openPageFromChat(URL(string: "http://192.168.1.1")!))
        #expect(controller.tabs.count == 1)
        #expect(controller.lastAction == "Blocked unsafe URL")

        #expect(controller.openPageFromChat(fixture.url))
        #expect(controller.isVisible)
        #expect(controller.controlOwner == .user)
        #expect(controller.tabs.count == 2)
        #expect(controller.activePageID != originalPageID)
        #expect(controller.address == fixture.url.absoluteString)
        #expect(controller.lastAction == "Opened link from chat")

        for _ in 0..<50 {
            if controller.webView.url == fixture.url { break }
            try await Task.sleep(for: .milliseconds(20))
        }
        #expect(controller.webView.url == fixture.url)
    }

    @Test("Executes deterministic page actions and screenshots")
    func executesPageActions() async throws {
        let fixture = try await LocalHTTPFixture.start(body: Self.fixture)
        defer { fixture.stop() }
        let controller = BrowserHostController()
        controller.webView.frame = CGRect(x: 0, y: 0, width: 800, height: 600)

        _ = try await controller.execute(action: "launch", params: [:])
        #expect(controller.isVisible)
        let navigation = try resultDictionary(
            await controller.execute(
                action: "navigate",
                params: ["url": fixture.url.absoluteString, "wait_until": "load"],
            )
        )
        #expect(navigation["title"] as? String == "Browser fixture")
        #expect(navigation["is_loading"] as? Bool == false)
        #expect((navigation["revision"] as? Int) != nil)

        let semantic = try resultDictionary(
            await controller.execute(
                action: "observe", params: ["screenshot": "when_visual"]
            )
        )
        #expect((semantic["semantic_snapshot"] as? String)?.contains(
            "[heading level=1] \"Browser command test\""
        ) == true)
        #expect((semantic["semantic_snapshot"] as? String)?.contains(
            "selector=#same-page"
        ) == true)
        #expect((semantic["semantic_snapshot"] as? String)?.contains(
            "Password"
        ) == true)
        #expect((semantic["semantic_snapshot"] as? String)?.contains(
            "top-secret"
        ) == false)
        #expect(semantic["visual_hint"] as? Bool == false)
        #expect(semantic["screenshot_data"] == nil)

        _ = try await controller.execute(
            action: "evaluate",
            params: [
                "script": "window.visualFrameCount=0; window.originalRequestAnimationFrame=window.requestAnimationFrame; window.requestAnimationFrame=(callback)=>{window.visualFrameCount+=1; return window.originalRequestAnimationFrame(callback)}; true",
            ]
        )
        _ = try await controller.execute(action: "screenshot", params: [:])
        let frameCount = try resultDictionary(
            await controller.execute(
                action: "evaluate", params: ["script": "window.visualFrameCount"]
            )
        )
        #expect((frameCount["value"] as? NSNumber)?.intValue ?? 0 >= 1)
        _ = try await controller.execute(
            action: "evaluate",
            params: ["script": "window.requestAnimationFrame=()=>1; true"]
        )
        let stalledFrameStarted = ContinuousClock.now
        _ = try await controller.execute(action: "screenshot", params: [:])
        #expect(ContinuousClock.now - stalledFrameStarted < .seconds(1))
        _ = try await controller.execute(
            action: "evaluate",
            params: ["script": "window.requestAnimationFrame=window.originalRequestAnimationFrame; true"]
        )

        _ = try await controller.execute(
            action: "evaluate",
            params: [
                "script": "const canvas=document.createElement('canvas'); canvas.id='chart'; document.body.appendChild(canvas); true",
            ]
        )
        let adaptiveVisual = try resultDictionary(
            await controller.execute(
                action: "observe", params: ["screenshot": "when_visual"]
            )
        )
        #expect(adaptiveVisual["visual_hint"] as? Bool == true)
        #expect((adaptiveVisual["screenshot_data"] as? String)?.isEmpty == false)

        let visual = try resultDictionary(
            await controller.execute(
                action: "observe", params: ["screenshot": "always"]
            )
        )
        #expect((visual["screenshot_data"] as? String)?.isEmpty == false)
        #expect(visual["screenshot_media_type"] as? String == "image/png")

        let linked = try resultDictionary(
            await controller.execute(
                action: "click",
                params: ["selector": "#same-page", "button": "left"]
            )
        )
        #expect((linked["url"] as? String)?.hasSuffix("/next") == true)
        #expect(linked["is_loading"] is Bool)
        let linkedAfterLoad = try resultDictionary(
            await controller.execute(
                action: "wait", params: ["condition": "load", "timeout": 3_000]
            )
        )
        #expect((linkedAfterLoad["url"] as? String)?.hasSuffix("/next") == true)
        #expect(linkedAfterLoad["is_loading"] as? Bool == false)

        let userAgent = try resultDictionary(
            await controller.execute(
                action: "evaluate", params: ["script": "navigator.userAgent"]
            )
        )["value"] as? String
        #expect(userAgent?.contains("Version/") == true)
        #expect(userAgent?.contains(" Safari/") == true)
        #expect(userAgent?.contains(" AutoVerse/") == true)

        let text = try resultDictionary(
            await controller.execute(action: "content", params: ["mode": "text"])
        )
        #expect((text["content"] as? String)?.contains("Browser command test") == true)

        let html = try resultDictionary(
            await controller.execute(
                action: "content", params: ["selector": "#markup", "mode": "html"]
            )
        )
        #expect(html["content"] as? String == "<strong>Inner</strong><span> markup</span>")

        let markdown = try resultDictionary(
            await controller.execute(
                action: "content", params: ["selector": "#main", "mode": "markdown"]
            )
        )
        #expect((markdown["content"] as? String)?.contains("# Browser command test") == true)

        let asyncValue = try resultDictionary(
            await controller.execute(
                action: "evaluate",
                params: ["script": "return await Promise.resolve('async-ok')"],
            )
        )
        #expect(asyncValue["value"] as? String == "async-ok")

        do {
            _ = try await controller.execute(
                action: "evaluate",
                params: ["script": "Promise.resolve('unsupported')"],
            )
            Issue.record("A top-level Promise result should be rejected.")
        } catch let error as BrowserHostError {
            #expect(error.kind == "unsupported_result")
            #expect(error.message.contains("return await"))
        } catch {
            Issue.record("Unexpected Promise-result error: \(error)")
        }

        controller.evaluationTimeoutMilliseconds = 20
        let evaluationStarted = ContinuousClock.now
        do {
            _ = try await controller.execute(
                action: "evaluate",
                params: ["script": "return await new Promise(() => {})"],
            )
            Issue.record("An unresolved Promise should time out.")
        } catch let error as BrowserHostError {
            #expect(error.kind == "browser_timeout")
            #expect(ContinuousClock.now - evaluationStarted < .seconds(1))
        } catch {
            Issue.record("Unexpected evaluate-timeout error: \(error)")
        }
        controller.evaluationTimeoutMilliseconds = 30_000
        let afterTimeout = try resultDictionary(
            await controller.execute(
                action: "evaluate", params: ["script": "1 + 1"]
            )
        )
        #expect((afterTimeout["value"] as? NSNumber)?.intValue == 2)

        _ = try await controller.execute(
            action: "type",
            params: [
                "selector": "#name", "text": "Ada", "clear": true,
                "press_key": "Enter",
            ],
        )
        #expect(try await pageText(controller, selector: "#status") == "entered-Ada")

        _ = try await controller.execute(
            action: "click", params: ["selector": "#run", "button": "left"]
        )
        _ = try await controller.execute(
            action: "wait",
            params: ["selector": "#result", "condition": "visible", "timeout": 1_000],
        )
        #expect(try await pageText(controller, selector: "#status") == "clicked-1")

        let selected = try resultDictionary(
            await controller.execute(
                action: "select", params: ["selector": "#choice", "value": "b"]
            )
        )
        #expect(selected["values"] as? [String] == ["b"])
        #expect(try await pageText(controller, selector: "#status") == "selected-b")

        _ = try await controller.execute(
            action: "keyboard", params: ["key": "Enter", "selector": "#run"]
        )
        #expect(try await pageText(controller, selector: "#status") == "clicked-2")

        _ = try await controller.execute(
            action: "scroll", params: ["direction": "down", "amount": 900]
        )
        let scroll = try resultDictionary(
            await controller.execute(action: "evaluate", params: ["script": "window.scrollY"])
        )
        #expect((scroll["value"] as? NSNumber)?.doubleValue ?? 0 > 0)

        for params: [String: Any] in [
            [:],
            ["selector": "#bottom"],
            ["full_page": true],
        ] {
            let screenshot = try resultDictionary(
                await controller.execute(action: "screenshot", params: params)
            )
            let encoded = try #require(screenshot["data"] as? String)
            let data = try #require(Data(base64Encoded: encoded))
            #expect(data.starts(with: [0x89, 0x50, 0x4e, 0x47]))
        }

        controller.takeControl()
        await #expect(throws: BrowserHostError.self) {
            _ = try await controller.execute(
                action: "evaluate", params: ["script": "document.title"]
            )
        }
        controller.resumeAgentControl()
        #expect(controller.controlOwner == .agent)
        _ = try await controller.execute(action: "close", params: [:])
        #expect(!controller.isVisible)
    }

    @Test("Manages stable pages without reopening a hidden browser")
    func managesMultiplePagesAndHiddenState() async throws {
        let fixture = try await LocalHTTPFixture.start(body: Self.fixture)
        defer { fixture.stop() }
        let controller = BrowserHostController()
        controller.webView.frame = CGRect(x: 0, y: 0, width: 800, height: 600)

        let first = try resultDictionary(
            await controller.execute(
                action: "navigate",
                params: ["url": fixture.url.absoluteString, "wait_until": "load"],
            )
        )
        let firstID = try #require(first["page_id"] as? String)
        let second = try resultDictionary(
            await controller.execute(
                action: "new_page",
                params: ["url": fixture.url.absoluteString, "wait_until": "load"],
            )
        )
        let secondID = try #require(second["page_id"] as? String)
        #expect(firstID != secondID)

        _ = try await controller.execute(
            action: "evaluate",
            params: [
                "page_id": secondID,
                "script": "document.title='Second page'; document.title",
            ],
        )
        let pages = try resultDictionary(
            await controller.execute(action: "list_pages", params: [:])
        )
        #expect((pages["pages"] as? [[String: Any]])?.count == 2)
        #expect(pages["active_page_id"] as? String == secondID)

        let targeted = try resultDictionary(
            await controller.execute(
                action: "evaluate",
                params: ["page_id": firstID, "script": "document.title"],
            )
        )
        #expect(targeted["value"] as? String == "Browser fixture")
        #expect(targeted["page_id"] as? String == firstID)

        _ = try await controller.execute(
            action: "click",
            params: ["page_id": secondID, "selector": "#popup", "button": "left"],
        )
        var afterPopup: [String: Any] = [:]
        var popupPages: [[String: Any]] = []
        for _ in 0..<30 {
            afterPopup = try resultDictionary(
                await controller.execute(action: "list_pages", params: [:])
            )
            popupPages = afterPopup["pages"] as? [[String: Any]] ?? []
            if popupPages.count == 3 { break }
            try await Task.sleep(for: .milliseconds(20))
        }
        #expect(popupPages.count == 3)
        let popupID = try #require(afterPopup["active_page_id"] as? String)
        #expect(popupID != firstID && popupID != secondID)
        _ = try await controller.execute(
            action: "close_page", params: ["page_id": popupID]
        )

        _ = try await controller.execute(action: "close", params: [:])
        #expect(!controller.isVisible)
        let hiddenPages = try resultDictionary(
            await controller.execute(action: "list_pages", params: [:])
        )
        #expect(hiddenPages["is_visible"] as? Bool == false)
        #expect((hiddenPages["pages"] as? [[String: Any]])?.count == 2)
        #expect(!controller.isVisible)

        _ = try await controller.execute(
            action: "switch_page", params: ["page_id": secondID]
        )
        #expect(controller.isVisible)
        _ = try await controller.execute(
            action: "close_page", params: ["page_id": firstID]
        )
        let lastClose = try resultDictionary(
            await controller.execute(
                action: "close_page", params: ["page_id": secondID]
            )
        )
        #expect(lastClose["page_count"] as? Int == 0)
        #expect(!controller.isVisible)

        let reopened = try resultDictionary(
            await controller.execute(action: "launch", params: [:])
        )
        #expect(reopened["page_count"] as? Int == 1)
        #expect(controller.isVisible)
    }

    @Test("Switches isolated WebKit pages without losing view or tab state")
    func switchesPagesWithoutStateLoss() async throws {
        let fixture = try await LocalHTTPFixture.start(body: Self.fixture)
        defer { fixture.stop() }
        let controller = BrowserHostController()
        controller.webView.frame = CGRect(x: 0, y: 0, width: 800, height: 600)
        let firstWebView = controller.webView

        let first = try resultDictionary(
            await controller.execute(
                action: "navigate",
                params: ["url": fixture.url.absoluteString, "wait_until": "load"],
            )
        )
        let firstID = try #require(first["page_id"] as? String)
        _ = try await controller.execute(
            action: "evaluate",
            params: [
                "page_id": firstID,
                "script": "window.fixtureState='alpha'; document.title='Alpha tab'",
            ],
        )
        #expect(controller.tabs.first(where: { $0.id == firstID })?.title == "Alpha tab")

        let second = try resultDictionary(
            await controller.execute(
                action: "new_page",
                params: ["url": fixture.url.absoluteString, "wait_until": "load"],
            )
        )
        let secondID = try #require(second["page_id"] as? String)
        let secondWebView = controller.webView
        #expect(secondWebView !== firstWebView)
        _ = try await controller.execute(
            action: "evaluate",
            params: [
                "page_id": secondID,
                "script": "window.fixtureState='beta'; document.title='Beta tab'",
            ],
        )
        #expect(controller.tabs.first(where: { $0.id == secondID })?.title == "Beta tab")

        for _ in 0..<12 {
            _ = try await controller.execute(
                action: "switch_page", params: ["page_id": firstID]
            )
            #expect(controller.webView === firstWebView)
            _ = try await controller.execute(
                action: "switch_page", params: ["page_id": secondID]
            )
            #expect(controller.webView === secondWebView)
        }

        _ = try await controller.execute(
            action: "switch_page", params: ["page_id": firstID]
        )
        #expect(controller.webView === firstWebView)
        let firstState = try resultDictionary(
            await controller.execute(
                action: "evaluate",
                params: ["page_id": firstID, "script": "window.fixtureState"],
            )
        )
        #expect(firstState["value"] as? String == "alpha")

        _ = try await controller.execute(
            action: "close_page", params: ["page_id": secondID]
        )
        #expect(controller.activePageID == firstID)
        #expect(controller.webView === firstWebView)
        #expect(controller.tabs.map(\.id) == [firstID])
    }

    private func pageText(
        _ controller: BrowserHostController, selector: String
    ) async throws -> String {
        let result = try resultDictionary(
            await controller.execute(
                action: "evaluate",
                params: ["script": "document.querySelector('\(selector)').textContent"],
            )
        )
        return result["value"] as? String ?? ""
    }

    private func resultDictionary(_ value: Any) throws -> [String: Any] {
        try #require(value as? [String: Any])
    }

    private static let fixture = """
    <!doctype html><html><head><title>Browser fixture</title>
    <style>.spacer{height:1400px}</style></head><body>
    <main id="main" data-ready="yes"><h1>Browser command test</h1>
    <div id="markup"><strong>Inner</strong><span> markup</span></div>
    <a id="same-page" href="/next">Navigate same page</a>
    <a id="popup" href="/popup" target="_blank">Open popup</a>
    <input id="name" value="old">
    <input id="password" type="password" placeholder="Password" value="top-secret">
    <button id="run" onclick="window.clicks=(window.clicks||0)+1;document.querySelector('#status').textContent='clicked-'+window.clicks;document.querySelector('#result').hidden=false">Run</button>
    <select id="choice" onchange="document.querySelector('#status').textContent='selected-'+this.value">
      <option value="a">Alpha</option><option value="b">Beta</option>
    </select>
    <p id="status">ready</p><p id="result" hidden>Result</p>
    <div class="spacer"></div><div id="bottom">Bottom</div></main>
    <script>document.querySelector('#name').addEventListener('keydown',e=>{if(e.key==='Enter')document.querySelector('#status').textContent='entered-'+e.target.value})</script>
    </body></html>
    """
}

private final class LocalHTTPFixture: @unchecked Sendable {
    let listener: NWListener
    let queue = DispatchQueue(label: "autoverse.browser-command-test")

    private init(listener: NWListener) {
        self.listener = listener
    }

    var url: URL {
        URL(string: "http://127.0.0.1:\(listener.port!.rawValue)/fixture")!
    }

    static func start(body: String) async throws -> LocalHTTPFixture {
        let listener = try NWListener(using: .tcp, on: .any)
        let fixture = LocalHTTPFixture(listener: listener)
        listener.newConnectionHandler = { connection in
            connection.start(queue: fixture.queue)
            connection.receive(
                minimumIncompleteLength: 1,
                maximumLength: 65_536
            ) { _, _, _, _ in
                let bodyData = Data(body.utf8)
                let header = "HTTP/1.1 200 OK\r\n"
                    + "Content-Type: text/html; charset=utf-8\r\n"
                    + "Content-Length: \(bodyData.count)\r\n"
                    + "Connection: close\r\n\r\n"
                var response = Data(header.utf8)
                response.append(bodyData)
                connection.send(content: response, completion: .contentProcessed { _ in
                    connection.cancel()
                })
            }
        }
        return try await withCheckedThrowingContinuation { continuation in
            listener.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    listener.stateUpdateHandler = nil
                    continuation.resume(returning: fixture)
                case .failed(let error):
                    listener.stateUpdateHandler = nil
                    continuation.resume(throwing: error)
                default:
                    break
                }
            }
            listener.start(queue: fixture.queue)
        }
    }

    func stop() {
        listener.newConnectionHandler = nil
        listener.stateUpdateHandler = nil
        listener.cancel()
    }
}
#endif
