import Foundation
import SwiftUI
import WebKit

#if os(macOS)
import AppKit
#else
import UIKit
#endif

enum BrowserControlOwner: String {
    case agent
    case user

    var label: String { self == .agent ? "Agent control" : "Your control" }
}

struct BrowserAgentActivity: Equatable {
    let action: String

    var label: String {
        switch action {
        case "launch": "Opening"
        case "new_page": "Opening page"
        case "switch_page": "Switching page"
        case "close_page", "close": "Closing page"
        case "navigate": "Navigating"
        case "observe": "Reading page"
        case "screenshot": "Capturing page"
        case "evaluate": "Running script"
        case "click": "Clicking"
        case "type": "Typing"
        case "content": "Reading content"
        case "scroll": "Scrolling"
        case "wait": "Waiting"
        case "select": "Selecting"
        case "keyboard": "Pressing key"
        default: action.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    var emphasizesAddress: Bool {
        ["launch", "new_page", "switch_page", "navigate"].contains(action)
    }
}

enum BrowserUserAgent {
    @MainActor
    static var applicationName: String {
        #if os(macOS)
        let installedSafariVersion = Bundle(path: "/Applications/Safari.app")?
            .object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        #else
        let installedSafariVersion: String? = nil
        #endif
        let operatingSystem = ProcessInfo.processInfo.operatingSystemVersion
        let fallbackSafariVersion =
            "\(operatingSystem.majorVersion).\(operatingSystem.minorVersion)"
        let webKitBundleVersion = Bundle(for: WKWebView.self)
            .object(forInfoDictionaryKey: "CFBundleVersion") as? String
        let appVersion = Bundle.main
            .object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        return makeApplicationName(
            safariVersion: installedSafariVersion ?? fallbackSafariVersion,
            webKitBundleVersion: webKitBundleVersion,
            appVersion: appVersion
        )
    }

    static func makeApplicationName(
        safariVersion: String?,
        webKitBundleVersion: String?,
        appVersion: String?
    ) -> String {
        let safari = normalizedVersion(safariVersion, fallback: "17.0")
        let webKit = normalizedWebKitVersion(webKitBundleVersion)
        let app = normalizedVersion(appVersion, fallback: "0.1.0")
        return "Version/\(safari) Safari/\(webKit) AutoVerse/\(app)"
    }

    private static func normalizedWebKitVersion(_ raw: String?) -> String {
        let version = normalizedVersion(raw, fallback: "605.1.15")
        var components = version.split(separator: ".").map(String.init)
        // Framework builds prefix the public WebKit major with build-train
        // digits (for example, 22625.1.24 corresponds to WebKit 625.1.24).
        if let major = components.first.flatMap(Int.init), major >= 1_000 {
            components[0] = String(major % 1_000)
        }
        return components.joined(separator: ".")
    }

    private static func normalizedVersion(_ raw: String?, fallback: String) -> String {
        guard let raw, !raw.isEmpty else { return fallback }
        let components = raw.split(separator: ".", omittingEmptySubsequences: false)
        guard !components.isEmpty,
              components.allSatisfy({ !$0.isEmpty && $0.allSatisfy(\.isNumber) }) else {
            return fallback
        }
        return components.joined(separator: ".")
    }
}

enum BrowserAddressResolver {
    static func resolve(_ input: String) -> URL? {
        let value = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return nil }
        if value.contains("://") {
            return URL(string: value)
        }
        if looksLikeHost(value) {
            return URL(string: "https://\(value)")
        }
        var search = URLComponents(string: "https://www.google.com/search")
        search?.queryItems = [URLQueryItem(name: "q", value: value)]
        return search?.url
    }

    private static func looksLikeHost(_ value: String) -> Bool {
        guard !value.contains(where: \.isWhitespace) else { return false }
        return value.contains(".")
            || value == "localhost"
            || value.hasPrefix("localhost:")
            || value.hasPrefix("localhost/")
            || value.hasPrefix("[")
    }
}

@MainActor
struct BrowserTab: Identifiable, Equatable {
    let id: String
    var title: String
    var url: String
    var isLoading: Bool
}

@MainActor
private final class BrowserPageRecord {
    let id: String
    let webView: WKWebView
    var navigationFailure: BrowserHostError?
    var navigationSequence = 0
    var committedNavigationSequence = 0
    var finishedNavigationSequence = 0
    var documentTitle = ""
    private var navigationGenerations: [ObjectIdentifier: Int] = [:]

    init(id: String = "page-\(UUID().uuidString.lowercased())", webView: WKWebView) {
        self.id = id
        self.webView = webView
    }

    var currentDocumentIsCommitted: Bool {
        navigationSequence == 0 || committedNavigationSequence >= navigationSequence
    }

    func startNavigation(_ navigation: WKNavigation?) {
        navigationSequence += 1
        if let navigation {
            navigationGenerations[ObjectIdentifier(navigation)] = navigationSequence
        }
    }

    func generation(for navigation: WKNavigation?) -> Int {
        guard let navigation else { return navigationSequence }
        return navigationGenerations[ObjectIdentifier(navigation)] ?? navigationSequence
    }

    func commitNavigation(_ navigation: WKNavigation?) -> Int {
        let generation = generation(for: navigation)
        committedNavigationSequence = max(committedNavigationSequence, generation)
        return generation
    }

    func finishNavigation(_ navigation: WKNavigation?) -> Int {
        let generation = commitNavigation(navigation)
        finishedNavigationSequence = max(finishedNavigationSequence, generation)
        forgetNavigation(navigation)
        return generation
    }

    func forgetNavigation(_ navigation: WKNavigation?) {
        guard let navigation else { return }
        navigationGenerations.removeValue(forKey: ObjectIdentifier(navigation))
    }
}

@MainActor
final class BrowserHostController: NSObject, ObservableObject, WKNavigationDelegate, WKUIDelegate {
    @Published var isVisible = false
    @Published var address = ""
    @Published private(set) var title = "Browser"
    @Published private(set) var connectionLabel = "Browser host offline"
    @Published private(set) var lastAction = "Ready"
    @Published private(set) var controlOwner: BrowserControlOwner = .agent
    @Published private(set) var agentActivity: BrowserAgentActivity?
    @Published private(set) var tabs: [BrowserTab] = []
    @Published private(set) var activePageID: String?

    var webView: WKWebView { activePage?.webView ?? fallbackWebView }

    private let fallbackWebView: WKWebView
    private var pages: [BrowserPageRecord] = []
    private var socket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?
    private var activeCommandTask: Task<BrowserCommandResult, Error>?
    private var activeCommandID = ""
    private var agentActivityToken: UUID?
    private var agentActivityStartedAt: Date?
    private var agentActivityClearTask: Task<Void, Never>?
    private var baseURL: URL?
    private var activeSessionID = ""
    private var reconnectAttempt = 0
    // Internal for a short timeout in command tests; production uses 30 seconds.
    var evaluationTimeoutMilliseconds: Double = 30_000
    // Keep fast commands perceptible without extending the command itself.
    var agentActivityMinimumDisplayMilliseconds: Double = 800
    private var activePage: BrowserPageRecord? {
        guard let activePageID else { return nil }
        return pages.first { $0.id == activePageID }
    }

    override init() {
        let configuration = Self.makeConfiguration()
        fallbackWebView = WKWebView(frame: .zero, configuration: configuration)
        super.init()
        register(fallbackWebView, activate: true)
    }

    private static func makeConfiguration() -> WKWebViewConfiguration {
        let configuration = WKWebViewConfiguration()
        // WKWebView omits Safari's product tokens from its default user agent.
        // Some sites respond with a legacy compatibility page unless the host
        // identifies the system WebKit as Safari-compatible.
        configuration.applicationNameForUserAgent = BrowserUserAgent.applicationName
        // Stable named data store: browser credentials/cookies persist across
        // launches but remain isolated from the chat WebUI's default data store.
        let profileID = UUID(uuidString: "7EC89123-1F64-47A6-B4DE-9FA7A7710285")!
        configuration.websiteDataStore = WKWebsiteDataStore(forIdentifier: profileID)
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = true
        return configuration
    }

    @discardableResult
    private func makePage(
        configuration: WKWebViewConfiguration? = nil,
        activate: Bool = true
    ) -> BrowserPageRecord {
        let webView = WKWebView(
            frame: .zero,
            configuration: configuration ?? Self.makeConfiguration()
        )
        return register(webView, activate: activate)
    }

    @discardableResult
    private func register(
        _ webView: WKWebView,
        activate: Bool
    ) -> BrowserPageRecord {
        let record = BrowserPageRecord(webView: webView)
        pages.append(record)
        configure(webView)
        refreshTab(record)
        if activate { activatePage(record.id) }
        return record
    }

    private func configure(_ webView: WKWebView) {
        webView.navigationDelegate = self
        webView.uiDelegate = self
        #if os(macOS)
        webView.allowsMagnification = true
        let click = NSClickGestureRecognizer(
            target: self, action: #selector(userInteractedWithPage)
        )
        click.delaysPrimaryMouseButtonEvents = false
        click.delegate = self
        webView.addGestureRecognizer(click)
        let pan = NSPanGestureRecognizer(
            target: self, action: #selector(userInteractedWithPage)
        )
        pan.delaysPrimaryMouseButtonEvents = false
        pan.delegate = self
        webView.addGestureRecognizer(pan)
        #else
        let tap = UITapGestureRecognizer(target: self, action: #selector(userInteractedWithPage))
        tap.cancelsTouchesInView = false
        webView.addGestureRecognizer(tap)
        webView.scrollView.panGestureRecognizer.addTarget(
            self, action: #selector(userInteractedWithPage)
        )
        #endif
    }

    private func record(for webView: WKWebView) -> BrowserPageRecord? {
        pages.first { $0.webView === webView }
    }

    private func record(pageID: String?) throws -> BrowserPageRecord {
        let targetID = pageID ?? activePageID
        guard let targetID,
              let record = pages.first(where: { $0.id == targetID }) else {
            throw BrowserHostError(
                kind: "page_not_found",
                message: "The requested browser page does not exist."
            )
        }
        return record
    }

    private func ensurePage() -> BrowserPageRecord {
        activePage ?? makePage()
    }

    private func activatePage(_ pageID: String) {
        guard let record = pages.first(where: { $0.id == pageID }) else { return }
        activePageID = record.id
        address = record.webView.url?.absoluteString ?? ""
        title = resolvedTitle(for: record)
        refreshTabs()
    }

    private func refreshTab(_ record: BrowserPageRecord) {
        let tab = BrowserTab(
            id: record.id,
            title: resolvedTitle(for: record),
            url: record.webView.url?.absoluteString ?? "",
            isLoading: record.webView.isLoading
        )
        if let index = tabs.firstIndex(where: { $0.id == record.id }) {
            tabs[index] = tab
        } else {
            tabs.append(tab)
        }
    }

    private func refreshTabs() {
        tabs = pages.map {
            BrowserTab(
                id: $0.id,
                title: resolvedTitle(for: $0),
                url: $0.webView.url?.absoluteString ?? "",
                isLoading: $0.webView.isLoading
            )
        }
    }

    private func resolvedTitle(for record: BrowserPageRecord) -> String {
        if !record.documentTitle.isEmpty { return record.documentTitle }
        if let title = record.webView.title, !title.isEmpty { return title }
        if let host = record.webView.url?.host, !host.isEmpty { return host }
        return "New Page"
    }

    @discardableResult
    private func removePage(_ pageID: String) -> String? {
        guard let index = pages.firstIndex(where: { $0.id == pageID }) else { return nil }
        let record = pages.remove(at: index)
        record.webView.stopLoading()
        record.webView.navigationDelegate = nil
        record.webView.uiDelegate = nil
        tabs.removeAll { $0.id == pageID }
        if activePageID == pageID {
            if pages.isEmpty {
                activePageID = nil
                address = ""
                title = "Browser"
                setVisible(false)
            } else {
                activatePage(pages[min(index, pages.count - 1)].id)
            }
        }
        refreshTabs()
        return activePageID
    }

    func newPageFromToolbar() {
        takeControl()
        _ = makePage()
        setVisible(true)
        lastAction = "New page"
    }

    @discardableResult
    func openPageFromChat(_ url: URL) -> Bool {
        guard BrowserURLPolicy.isAllowed(url) else {
            lastAction = "Blocked unsafe URL"
            return false
        }
        takeControl()
        let page = makePage()
        address = url.absoluteString
        page.webView.load(URLRequest(url: url))
        setVisible(true)
        lastAction = "Opened link from chat"
        return true
    }

    func switchPageFromToolbar(_ pageID: String) {
        takeControl()
        activatePage(pageID)
        setVisible(true)
        lastAction = "Switched page"
    }

    func closePageFromToolbar(_ pageID: String) {
        takeControl()
        _ = removePage(pageID)
        lastAction = pages.isEmpty ? "Browser hidden" : "Page closed"
    }

    func connect(to baseURL: URL) {
        stopSocket(clearBaseURL: false)
        self.baseURL = baseURL
        reconnectAttempt = 0
        startSocket()
    }

    private func startSocket() {
        guard let baseURL,
              var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            connectionLabel = "Invalid browser host URL"
            return
        }
        components.scheme = components.scheme?.lowercased() == "https" ? "wss" : "ws"
        components.path = "/api/browser/host"
        components.query = nil
        components.fragment = nil
        guard let url = components.url else {
            connectionLabel = "Invalid browser host URL"
            return
        }
        let task = URLSession.shared.webSocketTask(with: url)
        socket = task
        task.resume()
        connectionLabel = "Connecting browser host…"
        send([
            "type": "hello",
            "protocol": 1,
            "engine": "webkit",
            "profile": "autoverse-default",
            "capabilities": [
                "visible", "persistent_profile", "user_takeover", "multi_page",
            ],
        ])
        receiveTask = Task { [weak self, weak task] in
            guard let self, let task else { return }
            while !Task.isCancelled {
                do {
                    let message = try await task.receive()
                    await self.receive(message)
                } catch {
                    if self.socket === task {
                        self.socket = nil
                        if task.closeCode.rawValue == 1012 {
                            self.connectionLabel = "Browser host active in another app"
                        } else {
                            self.connectionLabel = "Browser host offline"
                            self.scheduleReconnect()
                        }
                    }
                    return
                }
            }
        }
    }

    func disconnect() {
        stopSocket(clearBaseURL: true)
    }

    private func stopSocket(clearBaseURL: Bool) {
        reconnectTask?.cancel()
        reconnectTask = nil
        receiveTask?.cancel()
        receiveTask = nil
        activeCommandTask?.cancel()
        activeCommandTask = nil
        activeCommandID = ""
        clearAgentActivity()
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        if clearBaseURL { baseURL = nil }
        connectionLabel = "Browser host offline"
    }

    private func scheduleReconnect() {
        guard reconnectTask == nil, baseURL != nil else { return }
        let delays = [0.5, 1.0, 2.0, 5.0, 10.0]
        let delay = delays[min(reconnectAttempt, delays.count - 1)]
        reconnectAttempt += 1
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard let self, !Task.isCancelled, self.socket == nil, self.baseURL != nil else {
                return
            }
            self.reconnectTask = nil
            self.startSocket()
        }
    }

    func toggleVisible() {
        if !isVisible { _ = ensurePage() }
        setVisible(!isVisible)
    }

    private func setVisible(_ visible: Bool) {
        guard isVisible != visible else { return }
        isVisible = visible
    }

    func takeControl() {
        activeCommandTask?.cancel()
        webView.stopLoading()
        clearAgentActivity()
        controlOwner = .user
        setVisible(true)
        lastAction = "User took control"
    }

    func resumeAgentControl() {
        guard controlOwner == .user else { return }
        controlOwner = .agent
        lastAction = "Agent resumed"
    }

    @objc private func userInteractedWithPage() {
        if controlOwner == .agent { takeControl() }
    }

    func navigateFromToolbar() {
        guard let url = BrowserAddressResolver.resolve(address) else { return }
        guard BrowserURLPolicy.isAllowed(url) else {
            lastAction = "Blocked unsafe URL"
            return
        }
        takeControl()
        webView.load(URLRequest(url: url))
    }

    func goBack() {
        takeControl()
        webView.goBack()
    }

    func goForward() {
        takeControl()
        webView.goForward()
    }

    func reload() {
        takeControl()
        webView.reload()
    }

    private func receive(_ message: URLSessionWebSocketTask.Message) async {
        let data: Data
        switch message {
        case .string(let text): data = Data(text.utf8)
        case .data(let value): data = value
        @unknown default: return
        }
        guard let object = try? JSONSerialization.jsonObject(with: data),
              let command = object as? [String: Any] else { return }
        if command["type"] as? String == "hello_ack" {
            guard (command["protocol"] as? NSNumber)?.intValue == 1,
                  command["engine"] as? String == "webkit" else {
                socket?.cancel(with: .protocolError, reason: nil)
                socket = nil
                connectionLabel = "Incompatible browser host"
                return
            }
            reconnectAttempt = 0
            connectionLabel = "Browser host connected"
            return
        }
        if command["type"] as? String == "hello_reject" {
            socket?.cancel(with: .protocolError, reason: nil)
            socket = nil
            connectionLabel = "Incompatible browser host"
            return
        }
        if command["type"] as? String == "host_replaced" {
            socket?.cancel(with: .goingAway, reason: nil)
            socket = nil
            connectionLabel = "Browser host active in another app"
            return
        }
        guard command["type"] as? String == "command",
              let commandID = command["id"] as? String,
              let action = command["action"] as? String else { return }
        activeSessionID = command["session_id"] as? String ?? ""
        let params = command["params"] as? [String: Any] ?? [:]
        let commandTask = Task { [weak self] () throws -> BrowserCommandResult in
            guard let self else { throw CancellationError() }
            return BrowserCommandResult(
                value: try await self.execute(action: action, params: params)
            )
        }
        activeCommandTask = commandTask
        activeCommandID = commandID
        defer {
            if activeCommandID == commandID {
                activeCommandTask = nil
                activeCommandID = ""
            }
        }
        do {
            let result = try await commandTask.value
            guard JSONSerialization.isValidJSONObject(["result": result.value]) else {
                throw BrowserHostError(
                    kind: "unsupported_result",
                    message: Self.unsupportedEvaluateResultMessage
                )
            }
            send(["type": "result", "id": commandID, "ok": true, "result": result.value])
        } catch is CancellationError {
            send([
                "type": "result",
                "id": commandID,
                "ok": false,
                "error": [
                    "kind": "user_control",
                    "message": "The user took control before the browser action completed.",
                ],
            ])
        } catch let error as BrowserHostError {
            send([
                "type": "result",
                "id": commandID,
                "ok": false,
                "error": ["kind": error.kind, "message": error.message],
            ])
        } catch {
            send([
                "type": "result",
                "id": commandID,
                "ok": false,
                "error": ["kind": "browser_error", "message": error.localizedDescription],
            ])
        }
    }

    func execute(action: String, params: [String: Any]) async throws -> Any {
        try Task.checkCancellation()
        let activityToken = beginAgentActivity(action)
        defer { endAgentActivity(activityToken) }
        if action == "launch" {
            _ = ensurePage()
            setVisible(true)
            lastAction = "Browser opened"
            return pageState()
        }
        guard controlOwner == .agent else {
            throw BrowserHostError(
                kind: "user_control",
                message: "Browser control is with the user. Wait for their next message."
            )
        }
        if action == "close" {
            setVisible(false)
            lastAction = "Browser hidden"
            return pageState()
        }
        if action == "list_pages" {
            return pagesState()
        }

        switch action {
        case "new_page":
            let record = makePage()
            setVisible(true)
            lastAction = "New page"
            if let raw = params["url"] as? String, !raw.isEmpty {
                try await navigate(record: record, rawURL: raw, params: params)
            }
            return pageState(record)
        case "switch_page":
            guard let pageID = params["page_id"] as? String else {
                throw BrowserHostError(
                    kind: "invalid_args", message: "page_id is required."
                )
            }
            let record = try record(pageID: pageID)
            activatePage(record.id)
            setVisible(true)
            lastAction = "Switched page"
            return pageState(record)
        case "close_page":
            let record = try record(pageID: params["page_id"] as? String)
            let closedPageID = record.id
            let nextPageID = removePage(closedPageID)
            lastAction = pages.isEmpty ? "Browser hidden" : "Page closed"
            return [
                "closed_page_id": closedPageID,
                "active_page_id": nextPageID ?? NSNull(),
                "page_count": pages.count,
                "is_visible": isVisible,
            ]
        default:
            break
        }

        let target = try record(pageID: params["page_id"] as? String)
        activatePage(target.id)
        setVisible(true)
        lastAction = action.replacingOccurrences(of: "_", with: " ").capitalized
        defer {
            if pages.contains(where: { $0.id == target.id }) {
                refreshTab(target)
                if target.id == activePageID {
                    address = target.webView.url?.absoluteString ?? address
                    title = resolvedTitle(for: target)
                }
            }
        }
        if action != "navigate" {
            try await waitForCurrentDocumentCommit(
                record: target,
                timeoutMilliseconds: 10_000
            )
        }
        let previousNavigationSequence = target.navigationSequence

        switch action {
        case "navigate":
            guard let raw = params["url"] as? String else {
                throw BrowserHostError(kind: "invalid_args", message: "url is required.")
            }
            try await navigate(record: target, rawURL: raw, params: params)
            return pageState(target)
        case "observe":
            return try await observe(target, params: params)
        case "screenshot":
            return try await screenshot(params, record: target)
        case "evaluate":
            let value = try await evaluate(params["script"] as? String ?? "")
            var state = pageState(target)
            state["value"] = value ?? NSNull()
            return state
        case "click":
            var result = try await click(params)
            try await settleAfterPageAction(
                record: target,
                previousNavigationSequence: previousNavigationSequence
            )
            result.merge(pageState(target)) { _, current in current }
            return result
        case "type":
            var result = try await typeText(params)
            try await settleAfterPageAction(
                record: target,
                previousNavigationSequence: previousNavigationSequence
            )
            result.merge(pageState(target)) { _, current in current }
            return result
        case "content":
            return try await content(params)
        case "scroll":
            return try await scroll(params)
        case "wait":
            return try await waitForCondition(params)
        case "select":
            var result = try await select(params)
            try await settleAfterPageAction(
                record: target,
                previousNavigationSequence: previousNavigationSequence
            )
            result.merge(pageState(target)) { _, current in current }
            return result
        case "keyboard":
            var result = try await keyboard(params)
            try await settleAfterPageAction(
                record: target,
                previousNavigationSequence: previousNavigationSequence
            )
            result.merge(pageState(target)) { _, current in current }
            return result
        default:
            throw BrowserHostError(kind: "unsupported_action", message: "Unsupported action: \(action)")
        }
    }

    private func beginAgentActivity(_ action: String) -> UUID? {
        guard controlOwner == .agent, action != "list_pages" else { return nil }
        agentActivityClearTask?.cancel()
        agentActivityClearTask = nil
        let token = UUID()
        agentActivityToken = token
        agentActivityStartedAt = Date()
        agentActivity = BrowserAgentActivity(action: action)
        return token
    }

    private func endAgentActivity(_ token: UUID?) {
        guard let token, agentActivityToken == token else { return }
        let elapsed = Date().timeIntervalSince(agentActivityStartedAt ?? Date())
        let minimumDuration = max(0, agentActivityMinimumDisplayMilliseconds / 1_000)
        let remaining = max(0, minimumDuration - elapsed)
        guard remaining > 0 else {
            clearAgentActivity(token: token)
            return
        }
        agentActivityClearTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(remaining))
            guard !Task.isCancelled else { return }
            self?.clearAgentActivity(token: token)
        }
    }

    private func clearAgentActivity(token: UUID? = nil) {
        if let token, agentActivityToken != token { return }
        agentActivityClearTask?.cancel()
        agentActivityClearTask = nil
        agentActivityToken = nil
        agentActivityStartedAt = nil
        agentActivity = nil
    }

    private func navigate(
        record: BrowserPageRecord,
        rawURL: String,
        params: [String: Any]
    ) async throws {
        guard let url = URL(string: rawURL), BrowserURLPolicy.isAllowed(url) else {
            throw BrowserHostError(kind: "blocked_url", message: "URL is not allowed.")
        }
        let waitUntil = params["wait_until"] as? String ?? "domcontentloaded"
        guard ["domcontentloaded", "load", "networkidle"].contains(waitUntil) else {
            throw BrowserHostError(
                kind: "invalid_args",
                message: "wait_until must be domcontentloaded, load, or networkidle."
            )
        }
        record.navigationFailure = nil
        let previousNavigationSequence = record.navigationSequence
        guard record.webView.load(URLRequest(url: url)) != nil else {
            throw BrowserHostError(
                kind: "navigation_error", message: "WebKit did not start navigation."
            )
        }
        try await waitForReady(
            record: record,
            waitUntil: waitUntil,
            previousNavigationSequence: previousNavigationSequence,
            timeoutMilliseconds: 30_000
        )
        refreshTab(record)
        if record.id == activePageID {
            address = record.webView.url?.absoluteString ?? address
            title = resolvedTitle(for: record)
        }
    }

    private func settleAfterPageAction(
        record: BrowserPageRecord,
        previousNavigationSequence: Int
    ) async throws {
        // JS click/input handlers resolve before WebKit necessarily announces a
        // resulting navigation. Give it a short commit window; if navigation
        // starts, wait briefly for usable DOM feedback. A slow page remains a
        // successful action with is_loading=true instead of becoming a false
        // click/type failure.
        let commitDeadline = Date().addingTimeInterval(0.25)
        while Date() < commitDeadline {
            try Task.checkCancellation()
            if record.navigationSequence > previousNavigationSequence
                || record.webView.isLoading {
                do {
                    try await waitForReady(
                        record: record,
                        waitUntil: "domcontentloaded",
                        previousNavigationSequence: previousNavigationSequence,
                        timeoutMilliseconds: 3_000
                    )
                } catch let error as BrowserHostError
                    where error.kind == "browser_timeout" {
                    return
                }
                return
            }
            try await Task.sleep(for: .milliseconds(25))
        }
    }

    private func observe(
        _ record: BrowserPageRecord,
        params: [String: Any]
    ) async throws -> [String: Any] {
        let screenshotPolicy = params["screenshot"] as? String ?? "never"
        guard ["never", "always", "when_visual"].contains(screenshotPolicy) else {
            throw BrowserHostError(
                kind: "invalid_args",
                message: "screenshot must be never, always, or when_visual."
            )
        }

        for _ in 0..<2 {
            try await waitForCurrentDocumentCommit(
                record: record,
                timeoutMilliseconds: 10_000
            )
            let revision = record.navigationSequence
            let rawSemantic = try await evaluate(
                Self.semanticObservationScript,
                in: record.webView
            )
            var semantic = rawSemantic as? [String: Any] ?? [:]
            var visualHint = semantic["visual_hint"] as? Bool ?? false
            var state = pageState(record)
            state["semantic_snapshot"] = semantic["snapshot"] as? String ?? ""
            state["semantic_truncated"] = semantic["truncated"] as? Bool ?? false
            state["visual_hint"] = visualHint

            let shouldCapture = screenshotPolicy == "always"
                || (screenshotPolicy == "when_visual" && visualHint)
            if shouldCapture {
                do {
                    try await waitForVisualReadiness(record)
                    if let refreshed = try await evaluate(
                        Self.semanticObservationScript,
                        in: record.webView
                    ) as? [String: Any] {
                        semantic = refreshed
                        visualHint = semantic["visual_hint"] as? Bool ?? false
                        state["semantic_snapshot"] = semantic["snapshot"] as? String ?? ""
                        state["semantic_truncated"] = semantic["truncated"] as? Bool ?? false
                        state["visual_hint"] = visualHint
                    }
                    let stillShouldCapture = screenshotPolicy == "always" || visualHint
                    if stillShouldCapture {
                        let captured = try await captureScreenshot(
                            ["full_page": false], record: record
                        )
                        state["screenshot_data"] = captured["data"]
                        state["screenshot_media_type"] = captured["media_type"] ?? "image/png"
                    }
                } catch let error as CancellationError {
                    throw error
                } catch {
                    state["screenshot_error"] = error.localizedDescription
                }
            }
            for (key, value) in pageState(record) {
                state[key] = value
            }
            if record.navigationSequence == revision && record.currentDocumentIsCommitted {
                state["url"] = semantic["url"] as? String ?? state["url"]
                state["title"] = semantic["title"] as? String ?? state["title"]
                return state
            }
        }
        throw BrowserHostError(
            kind: "browser_state_unstable",
            message: "Browser page changed repeatedly during observation."
        )
    }

    private func screenshot(
        _ params: [String: Any],
        record: BrowserPageRecord
    ) async throws -> [String: Any] {
        for _ in 0..<2 {
            let revision = record.navigationSequence
            try await waitForVisualReadiness(record)
            let captured = try await captureScreenshot(params, record: record)
            if record.navigationSequence == revision {
                return captured
            }
        }
        throw BrowserHostError(
            kind: "browser_state_unstable",
            message: "Browser page changed repeatedly during screenshot capture."
        )
    }

    private func waitForVisualReadiness(_ record: BrowserPageRecord) async throws {
        do {
            _ = try await evaluate(
                Self.visualReadinessScript,
                in: record.webView
            )
        } catch let error as CancellationError {
            throw error
        } catch {
            // Readiness is best-effort; a renderable page can still be captured.
        }
    }

    private func captureScreenshot(
        _ params: [String: Any],
        record: BrowserPageRecord
    ) async throws -> [String: Any] {
        let targetWebView = record.webView
        let configuration = WKSnapshotConfiguration()
        if (params["full_page"] as? Bool) == true {
            let sizeScript = """
            ({width: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0),
              height: Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0)})
            """
            if let size = try await evaluate(
                sizeScript, in: targetWebView
            ) as? [String: Any],
               let width = number(size["width"]), let height = number(size["height"]) {
                guard width > 0, height > 0, width * height <= 50_000_000 else {
                    throw BrowserHostError(
                        kind: "screenshot_too_large",
                        message: "Full-page screenshot exceeds the 50 megapixel safety limit."
                    )
                }
                configuration.rect = CGRect(x: 0, y: 0, width: width, height: height)
            }
        }
        if let selector = params["selector"] as? String, !selector.isEmpty {
            let revealScript = "document.querySelector(\(json(selector)))?.scrollIntoView({block:'center', inline:'center'})"
            _ = try await evaluate(revealScript, in: targetWebView)
            try await Task.sleep(for: .milliseconds(100))
            let script = """
            (() => { const e = document.querySelector(\(json(selector)));
              if (!e) return null; const r = e.getBoundingClientRect();
              return {x:r.x, y:r.y, width:r.width, height:r.height}; })()
            """
            guard let rect = try await evaluate(
                script, in: targetWebView
            ) as? [String: Any],
                  let x = number(rect["x"]), let y = number(rect["y"]),
                  let width = number(rect["width"]), let height = number(rect["height"]) else {
                throw BrowserHostError(kind: "selector_not_found", message: "Screenshot selector was not found.")
            }
            let requested = CGRect(x: x, y: y, width: width, height: height)
            let visible = requested.intersection(targetWebView.bounds)
            guard !visible.isNull, visible.width > 0, visible.height > 0 else {
                throw BrowserHostError(
                    kind: "screenshot_error", message: "Screenshot target is outside the visible viewport."
                )
            }
            configuration.rect = visible
        }
        try Task.checkCancellation()
        let image = try await targetWebView.takeSnapshot(configuration: configuration)
        try Task.checkCancellation()
        #if os(macOS)
        guard let tiff = image.tiffRepresentation,
              let bitmap = NSBitmapImageRep(data: tiff),
              let data = bitmap.representation(using: .png, properties: [:]) else {
            throw BrowserHostError(kind: "screenshot_error", message: "Could not encode screenshot.")
        }
        #else
        guard let data = image.pngData() else {
            throw BrowserHostError(kind: "screenshot_error", message: "Could not encode screenshot.")
        }
        #endif
        var state = pageState(record)
        state["data"] = data.base64EncodedString()
        state["media_type"] = "image/png"
        return state
    }

    private func click(_ params: [String: Any]) async throws -> [String: Any] {
        let double = (params["double"] as? Bool) == true
        let button = params["button"] as? String ?? "left"
        guard ["left", "right", "middle"].contains(button) else {
            throw BrowserHostError(
                kind: "invalid_args", message: "button must be left, right, or middle."
            )
        }
        let target: String
        if let selector = params["selector"] as? String, !selector.isEmpty {
            target = "document.querySelector(\(json(selector)))"
        } else if let x = number(params["x"]), let y = number(params["y"]) {
            target = "document.elementFromPoint(\(x), \(y))"
        } else {
            throw BrowserHostError(kind: "invalid_arguments", message: "A selector or x/y coordinates are required.")
        }
        let script = """
        (() => { const e = \(target); if (!e) return false;
          e.focus?.();
          if (\(json(button)) === 'left') {
            e.click();
            if (\(double ? "true" : "false")) {
              e.click();
              e.dispatchEvent(new MouseEvent('dblclick', {bubbles:true, cancelable:true, view:window, detail:2}));
            }
          } else {
            const code = \(button == "right" ? 2 : 1);
            const kind = code === 2 ? 'contextmenu' : 'auxclick';
            e.dispatchEvent(new MouseEvent(kind, {bubbles:true, cancelable:true, view:window, button:code}));
          }
          return true; })()
        """
        guard (try await evaluate(script) as? Bool) == true else {
            throw BrowserHostError(kind: "selector_not_found", message: "Click target was not found.")
        }
        return pageState()
    }

    private func typeText(_ params: [String: Any]) async throws -> [String: Any] {
        guard let selector = params["selector"] as? String,
              let text = params["text"] as? String else {
            throw BrowserHostError(kind: "invalid_arguments", message: "Selector and text are required.")
        }
        let clear = params["clear"] as? Bool ?? true
        let valueExpression = clear ? json(text) : "String(e.value || '') + \(json(text))"
        let script = """
        (() => { const e = document.querySelector(\(json(selector))); if (!e) return false;
          e.focus(); const next = \(valueExpression);
          const proto = Object.getPrototypeOf(e);
          const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
          if (setter) setter.call(e, next); else e.value = next;
          e.dispatchEvent(new Event('input', {bubbles:true}));
          e.dispatchEvent(new Event('change', {bubbles:true}));
          return true; })()
        """
        guard (try await evaluate(script) as? Bool) == true else {
            throw BrowserHostError(kind: "selector_not_found", message: "Input selector was not found.")
        }
        if let key = params["press_key"] as? String, !key.isEmpty {
            _ = try await keyboard(["key": key, "selector": selector])
        }
        return pageState()
    }

    private func content(_ params: [String: Any]) async throws -> [String: Any] {
        let selector = params["selector"] as? String
        let mode = (params["mode"] as? String ?? "text").lowercased()
        guard ["text", "html", "markdown"].contains(mode) else {
            throw BrowserHostError(
                kind: "invalid_args", message: "mode must be text, html, or markdown."
            )
        }
        let contentTarget = selector.map { "document.querySelector(\(json($0)))" } ?? "document.body"
        let script: String
        if mode == "markdown" {
            script = """
            (() => { const root=\(contentTarget); if (!root) return null;
              const walk=(node)=>{ if(node.nodeType===Node.TEXT_NODE) return node.textContent;
                if(node.nodeType!==Node.ELEMENT_NODE) return ''; const tag=node.tagName.toLowerCase();
                if(['script','style','nav','footer','aside'].includes(tag)) return '';
                const children=Array.from(node.childNodes).map(walk).join('');
                if(tag==='a'){const href=node.getAttribute('href'); return href?`[${children}](${href})`:children;}
                if(/^h[1-6]$/.test(tag)) return `\n${'#'.repeat(Number(tag[1]))} ${children}\n`;
                if(tag==='p') return `\n${children}\n`; if(tag==='br') return String.fromCharCode(10);
                if(tag==='li') return `\n- ${children}`; return children;}; return walk(root); })()
            """
        } else if mode == "html" {
            let htmlTarget = selector.map {
                "(() => { const e=document.querySelector(\(json($0))); return e ? e.innerHTML : null; })()"
            } ?? "document.documentElement.outerHTML"
            script = htmlTarget
        } else {
            script = "(() => { const e = \(contentTarget); return e ? (e.innerText || '') : null; })()"
        }
        guard let value = try await evaluate(script) as? String else {
            throw BrowserHostError(kind: "selector_not_found", message: "Content selector was not found.")
        }
        var state = pageState()
        state["content"] = value
        return state
    }

    private func scroll(_ params: [String: Any]) async throws -> [String: Any] {
        let amount = number(params["amount"]) ?? 500
        let direction = params["direction"] as? String ?? "down"
        guard ["up", "down", "left", "right"].contains(direction) else {
            throw BrowserHostError(
                kind: "invalid_args", message: "direction must be up, down, left, or right."
            )
        }
        let delta = ["up": -amount, "left": -amount, "down": amount, "right": amount][direction] ?? amount
        let selector = params["selector"] as? String
        let target = selector.map { "document.querySelector(\(json($0)))" } ?? "window"
        let axis = ["left", "right"].contains(direction) ? "left" : "top"
        let script = "(() => { const e=\(target); if (!e) return false; e.scrollBy({\(axis):\(delta), behavior:'instant'}); return true; })()"
        guard (try await evaluate(script) as? Bool) == true else {
            throw BrowserHostError(kind: "selector_not_found", message: "Scroll selector was not found.")
        }
        return pageState()
    }

    private func waitForCondition(_ params: [String: Any]) async throws -> [String: Any] {
        let timeout = number(params["timeout"]) ?? 10_000
        let condition = (params["condition"] as? String ?? "visible").lowercased()
        let selector = params["selector"] as? String
        let elementConditions = ["visible", "hidden", "attached", "detached"]
        let pageConditions = ["load", "domcontentloaded", "networkidle"]
        guard elementConditions.contains(condition) || pageConditions.contains(condition) else {
            throw BrowserHostError(kind: "invalid_args", message: "Unsupported browser wait condition.")
        }
        if elementConditions.contains(condition) && selector == nil {
            throw BrowserHostError(
                kind: "invalid_args", message: "This browser wait condition requires a selector."
            )
        }
        if pageConditions.contains(condition) {
            let currentPage = try record(pageID: nil)
            try await waitForReady(
                record: currentPage,
                waitUntil: condition,
                previousNavigationSequence: currentPage.navigationSequence,
                requireNavigation: false,
                timeoutMilliseconds: timeout
            )
            return pageState()
        }
        let deadline = Date().addingTimeInterval(timeout / 1000)
        while Date() < deadline {
            try Task.checkCancellation()
            let matched: Bool
            let expression: String
            switch condition {
            case "hidden": expression = "!e || !visible"
            case "detached": expression = "!e"
            case "attached": expression = "!!e"
            default: expression = "visible"
            }
            let script = """
            (() => { const e=document.querySelector(\(json(selector!)));
              const visible=!!e && !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
              return \(expression); })()
            """
            matched = (try await evaluate(script) as? Bool) == true
            if matched { return pageState() }
            try await Task.sleep(for: .milliseconds(100))
        }
        throw BrowserHostError(kind: "browser_timeout", message: "Browser wait timed out.")
    }

    private func select(_ params: [String: Any]) async throws -> [String: Any] {
        guard let selector = params["selector"] as? String else {
            throw BrowserHostError(kind: "invalid_arguments", message: "Selector is required.")
        }
        let value = params["value"] as? String
        let label = params["label"] as? String
        let index = (params["index"] as? NSNumber)?.intValue
        let choices = [value != nil, label != nil, index != nil].filter { $0 }.count
        guard choices == 1 else {
            throw BrowserHostError(
                kind: "invalid_args", message: "Select requires exactly one of label, value, or index."
            )
        }
        let script = """
        (() => { const e=document.querySelector(\(json(selector))); if (!e || !e.options) return null;
          const options=Array.from(e.options); let option=null;
          \(value.map { "option=options.find(o => o.value === \(json($0)));" } ?? "")
          \(label.map { "option=options.find(o => o.label === \(json($0)) || o.text === \(json($0)));" } ?? "")
          \(index.map { "option=options[\($0)];" } ?? "")
          if (!option) return null; options.forEach(o => { o.selected=false; });
          e.value=option.value; option.selected=true;
          e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true}));
          return Array.from(e.selectedOptions).map(o => o.value); })()
        """
        guard let values = try await evaluate(script) as? [String] else {
            throw BrowserHostError(kind: "selector_not_found", message: "Select target or option was not found.")
        }
        var state = pageState()
        state["values"] = values
        return state
    }

    private func keyboard(_ params: [String: Any]) async throws -> [String: Any] {
        guard let key = params["key"] as? String else {
            throw BrowserHostError(kind: "invalid_arguments", message: "Key is required.")
        }
        let selector = params["selector"] as? String
        let target = selector.map { "document.querySelector(\(json($0)))" } ?? "document.activeElement"
        let pieces = key.split(separator: "+").map(String.init)
        guard let primary = pieces.last, !primary.isEmpty else {
            throw BrowserHostError(kind: "invalid_args", message: "Key is empty.")
        }
        let modifiers = Set(pieces.dropLast().map { $0.lowercased() })
        let control = modifiers.contains("control") || modifiers.contains("ctrl")
        let command = modifiers.contains("meta") || modifiers.contains("command") || modifiers.contains("cmd")
        let alt = modifiers.contains("alt") || modifiers.contains("option")
        let shift = modifiers.contains("shift")
        let script = """
        (() => { const e=\(target); if (!e) return false; e.focus?.();
          const init={key:\(json(primary)),bubbles:true,cancelable:true,
            ctrlKey:\(control),metaKey:\(command),altKey:\(alt),shiftKey:\(shift)};
          e.dispatchEvent(new KeyboardEvent('keydown',init));
          if ((init.ctrlKey || init.metaKey) && init.key.toLowerCase()==='a' && e.select) e.select();
          if (init.key==='Enter') {
            if (e.tagName==='BUTTON' || e.tagName==='A') e.click();
            else if (e.form?.requestSubmit) e.form.requestSubmit();
          } else if (init.key==='Tab') {
            const nodes=Array.from(document.querySelectorAll('a,button,input,select,textarea,[tabindex]:not([tabindex="-1"])'));
            const i=nodes.indexOf(e); const step=init.shiftKey?-1:1;
            nodes[(i+step+nodes.length)%nodes.length]?.focus();
          }
          e.dispatchEvent(new KeyboardEvent('keyup',init)); return true; })()
        """
        guard (try await evaluate(script) as? Bool) == true else {
            throw BrowserHostError(kind: "selector_not_found", message: "Keyboard target was not found.")
        }
        return pageState()
    }

    private func waitForReady(
        record: BrowserPageRecord,
        waitUntil: String,
        previousNavigationSequence: Int,
        requireNavigation: Bool = true,
        timeoutMilliseconds: Double
    ) async throws {
        let deadline = Date().addingTimeInterval(timeoutMilliseconds / 1000)
        var stableResourceCount: Int?
        var stableSince = Date()
        while Date() < deadline {
            try Task.checkCancellation()
            if let navigationFailure = record.navigationFailure { throw navigationFailure }
            let revision = record.navigationSequence
            let moved = !requireNavigation
                || revision > previousNavigationSequence
            guard moved && record.currentDocumentIsCommitted else {
                try await Task.sleep(for: .milliseconds(100))
                continue
            }
            let readyState = (
                try? await evaluate("document.readyState", in: record.webView)
            ) as? String
            guard record.navigationSequence == revision
                    && record.currentDocumentIsCommitted else {
                stableResourceCount = nil
                stableSince = Date()
                continue
            }
            let ready: Bool
            switch waitUntil {
            case "domcontentloaded":
                ready = readyState == "interactive" || readyState == "complete"
            case "networkidle":
                if readyState == "complete" && !record.webView.isLoading {
                    let count = (
                        try? await evaluate(
                            "performance.getEntriesByType('resource').length",
                            in: record.webView
                        )
                    ) as? NSNumber
                    let current = count?.intValue ?? 0
                    if stableResourceCount != current {
                        stableResourceCount = current
                        stableSince = Date()
                    }
                    ready = Date().timeIntervalSince(stableSince) >= 0.5
                } else {
                    ready = false
                    stableSince = Date()
                }
            default:
                ready = readyState == "complete"
            }
            if ready && (waitUntil == "domcontentloaded" || !record.webView.isLoading) {
                return
            }
            try await Task.sleep(for: .milliseconds(100))
        }
        throw BrowserHostError(kind: "browser_timeout", message: "Navigation timed out.")
    }

    private func waitForCurrentDocumentCommit(
        record: BrowserPageRecord,
        timeoutMilliseconds: Double
    ) async throws {
        guard !record.currentDocumentIsCommitted else { return }
        let expectedSequence = record.navigationSequence
        try await waitForReady(
            record: record,
            waitUntil: "domcontentloaded",
            previousNavigationSequence: max(0, expectedSequence - 1),
            timeoutMilliseconds: timeoutMilliseconds
        )
    }

    private func evaluate(
        _ script: String,
        in targetWebView: WKWebView? = nil
    ) async throws -> Any? {
        try Task.checkCancellation()
        let targetWebView = targetWebView ?? webView
        let trimmed = script.trimmingCharacters(in: .whitespacesAndNewlines)
        let containsReturn = trimmed.range(of: #"\breturn\b"#, options: .regularExpression) != nil
        let value: Any?
        do {
            if containsReturn && !trimmed.hasPrefix("(") {
                let timeoutMarker = UUID().uuidString
                let timeout = max(1, Int(evaluationTimeoutMilliseconds.rounded(.up)))
                let timeoutKey = "__autoverse_evaluate_timeout__"
                let boundedScript = """
                let __autoverseTimeoutID;
                try {
                  return await Promise.race([
                    (async () => { \(script) })(),
                    new Promise((resolve) => {
                      __autoverseTimeoutID = setTimeout(
                        () => resolve({\(json(timeoutKey)): \(json(timeoutMarker))}),
                        \(timeout)
                      );
                    }),
                  ]);
                } finally {
                  clearTimeout(__autoverseTimeoutID);
                }
                """
                value = try await targetWebView.callAsyncJavaScript(
                    boundedScript,
                    arguments: [:],
                    in: nil,
                    contentWorld: .page
                )
                if let payload = value as? [String: Any],
                   payload[timeoutKey] as? String == timeoutMarker {
                    throw BrowserHostError(
                        kind: "browser_timeout",
                        message: "JavaScript evaluation timed out."
                    )
                }
            } else {
                value = try await targetWebView.evaluateJavaScript(script)
            }
        } catch let error as BrowserHostError {
            throw error
        } catch {
            let nativeError = error as NSError
            if nativeError.domain == WKErrorDomain,
               nativeError.code
                == WKError.Code.javaScriptResultTypeIsUnsupported.rawValue {
                throw BrowserHostError(
                    kind: "unsupported_result",
                    message: Self.unsupportedEvaluateResultMessage
                )
            }
            throw error
        }
        try Task.checkCancellation()
        if let record = record(for: targetWebView),
           let latestTitle = try? await targetWebView.evaluateJavaScript("document.title") as? String {
            record.documentTitle = latestTitle
        }
        return value
    }

    private static let unsupportedEvaluateResultMessage =
        "JavaScript returned an unsupported value. Return only JSON-serializable "
        + "null, booleans, numbers, strings, arrays, or objects; for asynchronous "
        + "work use a top-level `return await ...`."

    private func pageState(_ record: BrowserPageRecord? = nil) -> [String: Any] {
        guard let record = record ?? activePage else {
            return [
                "active_page_id": NSNull(),
                "page_count": pages.count,
                "is_visible": isVisible,
                "session_id": activeSessionID,
            ]
        }
        return [
            "page_id": record.id,
            "revision": record.navigationSequence,
            "url": record.webView.url?.absoluteString ?? "",
            "title": resolvedTitle(for: record),
            "is_loading": record.webView.isLoading,
            "active": record.id == activePageID,
            "active_page_id": activePageID ?? NSNull(),
            "page_count": pages.count,
            "is_visible": isVisible,
            "session_id": activeSessionID,
        ]
    }

    private static let visualReadinessScript = #"""
    (async () => {
      const VISUAL_READINESS_TIMEOUT_MS = 1100;
      const domReady = document.readyState === 'loading'
        ? new Promise((resolve) => document.addEventListener(
            'DOMContentLoaded', resolve, {once: true}))
        : Promise.resolve();
      const assetsReady = (async () => {
        await domReady;
        const fontsReady = document.fonts?.ready || Promise.resolve();
        const images = Array.from(document.images || [])
          .filter((image) => {
            if (image.complete) return false;
            const rect = image.getBoundingClientRect();
            return rect.bottom > 0 && rect.right > 0
              && rect.top < innerHeight && rect.left < innerWidth;
          })
          .slice(0, 12)
          .map((image) => new Promise((resolve) => {
            image.addEventListener('load', resolve, {once: true});
            image.addEventListener('error', resolve, {once: true});
          }));
        await Promise.allSettled([fontsReady, ...images]);
      })();
      const timedOut = await Promise.race([
        assetsReady.then(() => false),
        new Promise((resolve) => setTimeout(
          () => resolve(true), VISUAL_READINESS_TIMEOUT_MS)),
      ]);
      await Promise.race([
        new Promise((resolve) => requestAnimationFrame(
          () => requestAnimationFrame(resolve))),
        new Promise((resolve) => setTimeout(resolve, 150)),
      ]);
      return {timed_out: timedOut, ready_state: document.readyState};
    })()
    """#

    private static let semanticObservationScript = #"""
    (() => {
      const LIMIT = 6000;
      const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
      const visible = (element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden'
          && Number(style.opacity || 1) !== 0 && rect.width > 0 && rect.height > 0;
      };
      const selectorHint = (element) => {
        if (element.id) {
          const escaped = globalThis.CSS?.escape ? CSS.escape(element.id) : element.id;
          return ` selector=#${escaped}`;
        }
        const name = element.getAttribute('name');
        if (name) return ` selector=${element.tagName.toLowerCase()}[name=${JSON.stringify(name)}]`;
        return '';
      };
      const roleFor = (element) => {
        const explicit = element.getAttribute('role');
        if (explicit) return explicit;
        const tag = element.tagName.toLowerCase();
        if (/^h[1-6]$/.test(tag)) return `heading level=${tag.slice(1)}`;
        if (tag === 'a') return 'link';
        if (tag === 'button') return 'button';
        if (tag === 'textarea') return 'textbox';
        if (tag === 'select') return 'combobox';
        if (tag === 'summary') return 'summary';
        if (tag === 'input') {
          const type = (element.getAttribute('type') || 'text').toLowerCase();
          if (['checkbox', 'radio', 'button', 'submit', 'reset'].includes(type)) return type;
          return 'textbox';
        }
        return tag;
      };
      const accessibleName = (element) => normalize(
        element.getAttribute('aria-label')
          || element.labels?.[0]?.innerText
          || element.getAttribute('alt')
          || element.innerText
          || element.getAttribute('placeholder')
          || element.getAttribute('title')
      );
      const lines = [`document ${JSON.stringify(normalize(document.title))}`];
      let estimatedLength = lines[0].length + 1;
      let truncated = false;
      const append = (line) => {
        const remaining = LIMIT - estimatedLength;
        if (remaining <= 1) {
          truncated = true;
          return false;
        }
        if (line.length + 1 > remaining) {
          lines.push(line.slice(0, remaining - 1));
          estimatedLength = LIMIT;
          truncated = true;
          return false;
        }
        lines.push(line);
        estimatedLength += line.length + 1;
        return true;
      };
      const named = new Set();
      const semanticSelector = [
        'h1','h2','h3','h4','h5','h6','a','button','input','textarea','select',
        'summary','[role]','table','dialog'
      ].join(',');
      for (const element of document.querySelectorAll(semanticSelector)) {
        if (!visible(element)) continue;
        const name = accessibleName(element);
        if (!name && !['table', 'dialog'].includes(element.tagName.toLowerCase())) continue;
        if (name) named.add(name);
        const states = [];
        if (element.disabled) states.push('disabled');
        if (element.checked) states.push('checked');
        if (element.selected) states.push('selected');
        const expanded = element.getAttribute('aria-expanded');
        if (expanded !== null) states.push(`expanded=${expanded}`);
        if (element.tagName.toLowerCase() === 'select') {
          const selected = Array.from(element.selectedOptions || [])
            .map((item) => normalize(item.textContent));
          if (selected.length) states.push(`value=${JSON.stringify(selected.join(', '))}`);
        }
        if (!append(`[${roleFor(element)}] ${JSON.stringify(name)}${selectorHint(element)}`
          + `${states.length ? ` ${states.join(' ')}` : ''}`)) break;
      }
      const rawBodyText = String(document.body?.innerText || '');
      const bodyText = normalize(rawBodyText.slice(0, 2000));
      const bodySlice = rawBodyText.slice(0, LIMIT * 4);
      if (bodySlice.length < rawBodyText.length) truncated = true;
      for (const rawLine of bodySlice.split(/\n+/)) {
        const text = normalize(rawLine);
        if (text && !named.has(text) && !append(`[text] ${JSON.stringify(text)}`)) break;
      }
      const visualElements = Array.from(document.querySelectorAll('canvas,video,img,svg'))
        .filter(visible);
      const strongVisual = visualElements.some((element) =>
        ['CANVAS', 'VIDEO'].includes(element.tagName));
      const visualHint = strongVisual
        || (visualElements.length >= 3 && bodyText.length < 1000)
        || (visualElements.length > 0 && lines.length <= 1);
      const joined = lines.join('\n');
      return {
        url: String(location.href || ''),
        title: String(document.title || ''),
        snapshot: joined.slice(0, LIMIT),
        truncated,
        visual_hint: visualHint,
      };
    })()
    """#

    private func pagesState() -> [String: Any] {
        [
            "pages": pages.map { pageState($0) },
            "active_page_id": activePageID ?? NSNull(),
            "is_visible": isVisible,
        ]
    }

    private func send(_ object: [String: Any]) {
        guard let socket,
              JSONSerialization.isValidJSONObject(object),
              let data = try? JSONSerialization.data(withJSONObject: object),
              let text = String(data: data, encoding: .utf8) else { return }
        Task {
            do { try await socket.send(.string(text)) }
            catch {
                if self.socket === socket {
                    socket.cancel(with: .goingAway, reason: nil)
                    self.socket = nil
                    self.connectionLabel = "Browser host offline"
                    self.scheduleReconnect()
                }
            }
        }
    }

    static func isCancelledNavigationError(_ error: Error) -> Bool {
        let error = error as NSError
        return error.domain == NSURLErrorDomain && error.code == NSURLErrorCancelled
    }

    private func json(_ value: String) -> String {
        guard let data = try? JSONEncoder().encode(value),
              let result = String(data: data, encoding: .utf8) else { return "\"\"" }
        return result
    }

    private func number(_ value: Any?) -> Double? {
        (value as? NSNumber)?.doubleValue
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url, BrowserURLPolicy.isAllowed(url) else {
            if navigationAction.targetFrame?.isMainFrame == true {
                lastAction = "Blocked unsafe navigation"
                record(for: webView)?.navigationFailure = BrowserHostError(
                    kind: "blocked_url", message: "A navigation redirect was blocked by the URL policy."
                )
            }
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation?) {
        guard let record = record(for: webView) else { return }
        let generation = record.commitNavigation(navigation)
        guard generation == record.navigationSequence else { return }
        record.documentTitle = ""
        refreshTab(record)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation?) {
        guard let record = record(for: webView) else { return }
        let generation = record.finishNavigation(navigation)
        guard generation == record.navigationSequence else { return }
        record.documentTitle = webView.title ?? ""
        refreshTab(record)
        if record.id == activePageID {
            address = webView.url?.absoluteString ?? address
            title = resolvedTitle(for: record)
            lastAction = "Loaded"
        }
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation?) {
        guard let record = record(for: webView) else { return }
        record.startNavigation(navigation)
        record.navigationFailure = nil
        record.documentTitle = ""
        refreshTab(record)
        if record.id == activePageID { lastAction = "Loading…" }
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation?,
        withError error: Error
    ) {
        guard let record = record(for: webView) else { return }
        let generation = record.generation(for: navigation)
        defer { record.forgetNavigation(navigation) }
        // WebKit reports -999 when a navigation is replaced by a redirect,
        // script navigation, or a newer user/agent request. The replacement
        // can already be visible and healthy, so treating that callback as the
        // page failure makes the agent contradict the browser UI.
        if Self.isCancelledNavigationError(error) {
            refreshTab(record)
            return
        }
        guard generation == record.navigationSequence else { return }
        if record.navigationFailure == nil {
            record.navigationFailure = BrowserHostError(
                kind: "navigation_error", message: error.localizedDescription
            )
            refreshTab(record)
            if record.id == activePageID { lastAction = "Navigation failed" }
        }
    }

    func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation?,
        withError error: Error
    ) {
        guard let record = record(for: webView) else { return }
        let generation = record.generation(for: navigation)
        defer { record.forgetNavigation(navigation) }
        if Self.isCancelledNavigationError(error) {
            refreshTab(record)
            return
        }
        guard generation == record.navigationSequence else { return }
        if record.navigationFailure == nil {
            record.navigationFailure = BrowserHostError(
                kind: "navigation_error", message: error.localizedDescription
            )
            refreshTab(record)
            if record.id == activePageID { lastAction = "Navigation failed" }
        }
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        guard navigationAction.targetFrame == nil else { return nil }
        if let url = navigationAction.request.url, !BrowserURLPolicy.isAllowed(url) {
            lastAction = "Blocked unsafe popup"
            return nil
        }
        let record = makePage(configuration: configuration)
        setVisible(true)
        lastAction = "New page"
        return record.webView
    }

    func webViewDidClose(_ webView: WKWebView) {
        guard let record = record(for: webView) else { return }
        _ = removePage(record.id)
        lastAction = pages.isEmpty ? "Browser hidden" : "Page closed"
    }
}

#if os(macOS)
extension BrowserHostController: NSGestureRecognizerDelegate {
    func gestureRecognizer(
        _ gestureRecognizer: NSGestureRecognizer,
        shouldRecognizeSimultaneouslyWith otherGestureRecognizer: NSGestureRecognizer
    ) -> Bool {
        true
    }
}
#endif

struct BrowserHostError: Error {
    let kind: String
    let message: String
}

private struct BrowserCommandResult: @unchecked Sendable {
    let value: Any
}

enum BrowserChromeMetrics {
    static let toolbarHeight: CGFloat = 46
    static let addressFieldHeight: CGFloat = 34
    static let addressFieldMinimumWidth: CGFloat = 96
    static let addressFieldHorizontalPadding: CGFloat = 12
    static let addressFieldCornerRadius: CGFloat = 10
    static let tabStripHeight: CGFloat = 40
    static let tabHeight: CGFloat = 40
    static let tabSpacing: CGFloat = 0
    static let tabMinimumWidth: CGFloat = 120
    static let tabMaximumWidth: CGFloat = 196
    static let tabSeparatorWidth: CGFloat = 1
    static let tabSelectionIndicatorHeight: CGFloat = 2
    static let newPageButtonWidth: CGFloat = 40
    static let activityButtonWidth: CGFloat = 108
    static let statusHeight: CGFloat = 24
}

struct BrowserPane: View {
    @ObservedObject var controller: BrowserHostController
    let workspaceIsMaximized: Bool
    let onToggleWorkspaceSize: (() -> Void)?
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @FocusState private var addressIsFocused: Bool
    @State private var agentActivityPulse = false

    init(
        controller: BrowserHostController,
        workspaceIsMaximized: Bool = false,
        onToggleWorkspaceSize: (() -> Void)? = nil
    ) {
        self.controller = controller
        self.workspaceIsMaximized = workspaceIsMaximized
        self.onToggleWorkspaceSize = onToggleWorkspaceSize
    }

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            browserHairline
            tabStrip
            browserHairline
            NativeBrowserWebView(webView: controller.webView)
                .overlay {
                    Rectangle()
                        .strokeBorder(
                            browserAccent.opacity(
                                agentIsActing ? 0.24 + 0.34 * agentPulseStrength : 0
                            ),
                            lineWidth: 1
                        )
                        .animation(
                            reduceMotion ? nil : InteractionMotion.state,
                            value: agentIsActing
                        )
                        .allowsHitTesting(false)
                }
            browserHairline
            statusBar
        }
        .background(browserSurface)
        .overlay {
            Rectangle()
                .strokeBorder(
                    controlIndicatorColor.opacity(controlBorderOpacity),
                    lineWidth: agentIsActing ? 1.5 : 1
                )
                .animation(
                    reduceMotion ? nil : InteractionMotion.state,
                    value: controller.controlOwner
                )
                .animation(
                    reduceMotion ? nil : InteractionMotion.state,
                    value: agentIsActing
                )
                .allowsHitTesting(false)
        }
        .overlay(alignment: .top) {
            LinearGradient(
                colors: [.clear, browserAccent, .clear],
                startPoint: .leading,
                endPoint: .trailing
            )
            .frame(height: 2)
            .opacity(topActivityOpacity)
            .animation(
                reduceMotion ? nil : InteractionMotion.state,
                value: controller.controlOwner
            )
            .animation(
                reduceMotion ? nil : InteractionMotion.state,
                value: agentIsActing
            )
            .allowsHitTesting(false)
        }
        .task(id: agentPulseTaskID) {
            agentActivityPulse = agentIsActing
            guard agentIsActing, !reduceMotion else { return }
            try? await Task.sleep(for: .milliseconds(180))
            while !Task.isCancelled {
                withAnimation(InteractionMotion.activity) {
                    agentActivityPulse.toggle()
                }
                try? await Task.sleep(for: .seconds(InteractionMotion.activityDuration))
            }
        }
    }

    private var tabStrip: some View {
        ScrollViewReader { proxy in
            HStack(spacing: BrowserChromeMetrics.tabSpacing) {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: BrowserChromeMetrics.tabSpacing) {
                        ForEach(controller.tabs) { tab in
                            browserTab(tab)
                                .id(tab.id)
                        }
                    }
                }
                newPageButton
            }
            .onAppear { revealActiveTab(proxy) }
            .onChange(of: controller.activePageID) { _, _ in revealActiveTab(proxy) }
        }
        .frame(height: BrowserChromeMetrics.tabStripHeight)
        .background(browserPanelSurface)
    }

    private var newPageButton: some View {
        Button(action: controller.newPageFromToolbar) {
            Image(systemName: "plus")
                .font(.system(size: 13, weight: .medium))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(.secondary)
                .frame(
                    width: BrowserChromeMetrics.newPageButtonWidth,
                    height: BrowserChromeMetrics.tabStripHeight
                )
                .contentShape(Rectangle())
        }
        .buttonStyle(ChromeButtonMotionStyle())
        .overlay(alignment: .leading) {
            Rectangle()
                .fill(browserHairlineColor)
                .frame(width: BrowserChromeMetrics.tabSeparatorWidth)
        }
        .help("New Page")
        .accessibilityLabel("New Page")
    }

    private func revealActiveTab(_ proxy: ScrollViewProxy) {
        guard let activePageID = controller.activePageID else { return }
        withAnimation(reduceMotion ? nil : InteractionMotion.state) {
            proxy.scrollTo(activePageID, anchor: .trailing)
        }
    }

    private func browserTab(_ tab: BrowserTab) -> some View {
        let selected = tab.id == controller.activePageID
        return HStack(spacing: 5) {
            Button {
                controller.switchPageFromToolbar(tab.id)
            } label: {
                HStack(spacing: 5) {
                    if tab.isLoading {
                        ProgressView()
                            .controlSize(.mini)
                            .tint(browserAccent)
                            .frame(width: 12, height: 12)
                    } else {
                        ZStack {
                            Image(systemName: "globe")
                                .font(.system(size: 10, weight: .medium))
                                .foregroundStyle(selected ? browserAccent : Color.secondary)
                                .opacity(selected && agentIsActing ? 0 : 1)
                            Circle()
                                .fill(browserAccent)
                                .frame(width: 8, height: 8)
                                .opacity(
                                    selected && agentIsActing
                                        ? 0.54 + 0.46 * agentPulseStrength
                                        : 0
                                )
                                .scaleEffect(0.94 + 0.06 * agentPulseStrength)
                                .shadow(
                                    color: browserAccent.opacity(
                                        selected && agentIsActing
                                            ? 0.12 + 0.18 * agentPulseStrength
                                            : 0
                                    ),
                                    radius: 2
                                )
                        }
                        .frame(width: 12, height: 12)
                        .animation(
                            reduceMotion ? nil : InteractionMotion.state,
                            value: selected && agentIsActing
                        )
                    }
                    Text(tab.title)
                        .font(
                            .system(
                                size: 12.5,
                                weight: selected ? .medium : .regular
                            )
                        )
                        .lineLimit(1)
                        .frame(maxWidth: 126, alignment: .leading)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(tab.title) browser page")
            .accessibilityAddTraits(selected ? .isSelected : [])
            Button {
                controller.closePageFromToolbar(tab.id)
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 9, weight: .semibold))
                    .frame(width: 17, height: 17)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(Color.secondary.opacity(selected ? 0.9 : 0.62))
            .help("Close Page")
            .accessibilityLabel("Close \(tab.title)")
        }
        .padding(.leading, 8)
        .padding(.trailing, 4)
        .frame(
            minWidth: BrowserChromeMetrics.tabMinimumWidth,
            maxWidth: BrowserChromeMetrics.tabMaximumWidth
        )
        .frame(height: BrowserChromeMetrics.tabHeight)
        .background {
            Rectangle()
                .fill(
                    selected
                        ? browserAccent.opacity(colorScheme == .dark ? 0.14 : 0.08)
                        : Color.clear
                )
        }
        .overlay(alignment: .trailing) {
            Rectangle()
                .fill(browserHairlineColor)
                .frame(width: BrowserChromeMetrics.tabSeparatorWidth)
        }
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(selected ? browserAccent : Color.clear)
                .frame(height: BrowserChromeMetrics.tabSelectionIndicatorHeight)
        }
        .contentShape(Rectangle())
        .animation(reduceMotion ? nil : InteractionMotion.state, value: selected)
    }

    private var toolbar: some View {
        GeometryReader { geometry in
            HStack(spacing: 7) {
                browserButton(
                    systemName: "chevron.left",
                    help: "Back",
                    disabled: !controller.webView.canGoBack,
                    action: controller.goBack
                )
                browserButton(
                    systemName: "chevron.right",
                    help: "Forward",
                    disabled: !controller.webView.canGoForward,
                    action: controller.goForward
                )
                browserButton(
                    systemName: "arrow.clockwise",
                    help: "Reload",
                    action: controller.reload
                )
                addressField
                if let onToggleWorkspaceSize {
                    browserButton(
                        systemName: workspaceIsMaximized
                            ? "arrow.down.right.and.arrow.up.left"
                            : "arrow.up.left.and.arrow.down.right",
                        help: workspaceIsMaximized
                            ? "Restore Browser Width"
                            : "Maximize Browser Width",
                        action: onToggleWorkspaceSize
                    )
                }
                if controller.controlOwner == .agent {
                    takeoverButton(compact: geometry.size.width < 390)
                }
                browserButton(
                    systemName: "xmark",
                    help: "Close Browser",
                    action: controller.toggleVisible
                )
            }
            .padding(.horizontal, 10)
            .frame(maxHeight: .infinity)
        }
        .frame(height: BrowserChromeMetrics.toolbarHeight)
        .background(browserPanelSurface)
    }

    private var addressField: some View {
        TextField("Search or enter website", text: $controller.address)
            .textFieldStyle(.plain)
            .font(.system(size: 14, weight: .regular))
            .foregroundStyle(.primary)
            .focused($addressIsFocused)
            .padding(.horizontal, BrowserChromeMetrics.addressFieldHorizontalPadding)
            .frame(
                minWidth: BrowserChromeMetrics.addressFieldMinimumWidth,
                minHeight: BrowserChromeMetrics.addressFieldHeight,
                maxHeight: BrowserChromeMetrics.addressFieldHeight
            )
            .background {
                RoundedRectangle(
                    cornerRadius: BrowserChromeMetrics.addressFieldCornerRadius,
                    style: .continuous
                )
                    .fill(addressFieldSurface)
            }
            .overlay {
                RoundedRectangle(
                    cornerRadius: BrowserChromeMetrics.addressFieldCornerRadius,
                    style: .continuous
                )
                    .strokeBorder(
                        addressIsFocused || agentEmphasizesAddress
                            ? browserAccent
                            : browserControlBorder,
                        lineWidth: addressIsFocused || agentEmphasizesAddress ? 1.5 : 1
                    )
            }
            .shadow(
                color: agentEmphasizesAddress
                    ? browserAccent.opacity(0.08 + 0.14 * agentPulseStrength)
                    : .clear,
                radius: 3
            )
            .layoutPriority(1)
            .animation(InteractionMotion.state, value: addressIsFocused)
            .animation(
                reduceMotion ? nil : InteractionMotion.state,
                value: agentEmphasizesAddress
            )
            .onSubmit(controller.navigateFromToolbar)
            .accessibilityLabel("Browser address or search")
    }

    private func browserButton(
        systemName: String,
        help: String,
        disabled: Bool = false,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 12.5, weight: .medium))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(.secondary)
                .frame(width: 26, height: 26)
                .contentShape(Rectangle())
        }
        .buttonStyle(ChromeButtonMotionStyle())
        .disabled(disabled)
        .help(help)
        .accessibilityLabel(help)
    }

    private func takeoverButton(compact: Bool) -> some View {
        Button(action: controller.takeControl) {
            Group {
                if compact {
                    Image(systemName: agentIsActing ? "circle.fill" : "hand.raised")
                        .font(.system(size: agentIsActing ? 8 : 12, weight: .medium))
                        .scaleEffect(agentIsActing ? 0.94 + 0.06 * agentPulseStrength : 1)
                        .frame(width: 26, height: 26)
                } else {
                    ZStack {
                        Label("Take Over", systemImage: "hand.raised")
                            .opacity(agentIsActing ? 0 : 1)
                        HStack(spacing: 6) {
                            Circle()
                                .fill(browserAccent)
                                .frame(width: 7, height: 7)
                                .scaleEffect(0.94 + 0.06 * agentPulseStrength)
                            Text(controller.agentActivity?.label ?? "Working")
                                .lineLimit(1)
                                .contentTransition(.opacity)
                                .animation(
                                    reduceMotion ? nil : InteractionMotion.state,
                                    value: controller.agentActivity?.label
                                )
                        }
                        .opacity(agentIsActing ? 1 : 0)
                    }
                    .frame(width: BrowserChromeMetrics.activityButtonWidth, height: 26)
                }
            }
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(browserAccent)
            .background {
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .fill(
                        browserAccent.opacity(
                            agentIsActing
                                ? (colorScheme == .dark ? 0.18 : 0.12)
                                : (colorScheme == .dark ? 0.12 : 0.08)
                        )
                    )
            }
            .overlay {
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .strokeBorder(
                        browserAccent.opacity(
                            agentIsActing ? 0.42 + 0.16 * agentPulseStrength : 0.34
                        ),
                        lineWidth: 1
                    )
            }
            .contentShape(Rectangle())
            .animation(
                reduceMotion ? nil : InteractionMotion.state,
                value: agentIsActing
            )
        }
        .buttonStyle(ChromeButtonMotionStyle())
        .help("Take control of the browser")
        .accessibilityLabel("Take control of the browser")
        .accessibilityValue(controller.agentActivity?.label ?? "Agent control")
    }

    private var statusBar: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(connectionColor)
                .frame(width: 7, height: 7)
            Text(controller.connectionLabel)
            Text("·")
            Text(controller.lastAction)
                .lineLimit(1)
            Spacer(minLength: 8)
            Text(controlStatusLabel)
                .fontWeight(.medium)
                .foregroundStyle(controlIndicatorColor)
                .contentTransition(.opacity)
                .animation(
                    reduceMotion ? nil : InteractionMotion.state,
                    value: controlStatusLabel
                )
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 10)
        .frame(height: BrowserChromeMetrics.statusHeight)
        .background(browserPanelSurface)
    }

    private var connectionColor: Color {
        if controller.connectionLabel == "Browser host connected" { return .green }
        if controller.connectionLabel.hasPrefix("Connecting") { return .orange }
        return .red
    }

    private var agentIsActing: Bool {
        controller.controlOwner == .agent && controller.agentActivity != nil
    }

    private var agentEmphasizesAddress: Bool {
        agentIsActing && controller.agentActivity?.emphasizesAddress == true
    }

    private var agentPulseTaskID: String {
        "\(controller.agentActivity?.action ?? "idle")-\(reduceMotion)"
    }

    private var controlStatusLabel: String {
        guard controller.controlOwner == .agent,
              let activity = controller.agentActivity else {
            return controller.controlOwner.label
        }
        return "\(controller.controlOwner.label) · \(activity.label)"
    }

    private var controlBorderOpacity: Double {
        if controller.controlOwner == .user { return 0.58 }
        if agentIsActing { return 0.52 + 0.28 * agentPulseStrength }
        return 0.28
    }

    private var agentPulseStrength: Double {
        if reduceMotion { return 0.5 }
        return agentActivityPulse ? 1 : 0
    }

    private var topActivityOpacity: Double {
        guard controller.controlOwner == .agent else { return 0 }
        if agentIsActing { return 0.38 + 0.42 * agentPulseStrength }
        return 0.18
    }

    private var controlIndicatorColor: Color {
        controller.controlOwner == .agent ? browserAccent : browserUserControl
    }

    private var browserSurface: Color {
        colorScheme == .dark
            ? Color(red: 0x0E / 255, green: 0x0E / 255, blue: 0x10 / 255)
            : Color(red: 0xF7 / 255, green: 0xF4 / 255, blue: 0xED / 255)
    }

    private var browserPanelSurface: Color {
        colorScheme == .dark
            ? Color(red: 0x16 / 255, green: 0x16 / 255, blue: 0x18 / 255)
            : Color(red: 0xEF / 255, green: 0xEA / 255, blue: 0xE0 / 255)
    }

    private var addressFieldSurface: Color {
        colorScheme == .dark
            ? Color(red: 0x1D / 255, green: 0x1D / 255, blue: 0x20 / 255)
            : Color(red: 0xF7 / 255, green: 0xF4 / 255, blue: 0xED / 255)
    }

    private var browserControlBorder: Color {
        colorScheme == .dark
            ? Color(red: 0x3B / 255, green: 0x3B / 255, blue: 0x41 / 255)
            : Color(red: 0xD8 / 255, green: 0xD0 / 255, blue: 0xC2 / 255)
    }

    private var browserHairlineColor: Color {
        colorScheme == .dark
            ? Color(red: 0x26 / 255, green: 0x26 / 255, blue: 0x2A / 255)
            : Color(red: 0xD8 / 255, green: 0xD0 / 255, blue: 0xC2 / 255)
    }

    private var browserAccent: Color {
        colorScheme == .dark
            ? Color(red: 0xE8 / 255, green: 0xB3 / 255, blue: 0x39 / 255)
            : Color(red: 0xA5 / 255, green: 0x6C / 255, blue: 0x12 / 255)
    }

    private var browserUserControl: Color {
        colorScheme == .dark
            ? Color(red: 0x58 / 255, green: 0xB8 / 255, blue: 0x8A / 255)
            : Color(red: 0x39 / 255, green: 0x73 / 255, blue: 0x57 / 255)
    }

    private var browserHairline: some View {
        Rectangle()
            .fill(browserHairlineColor)
            .frame(height: 1)
    }
}

#if os(macOS)
private final class NativeBrowserContainerView: NSView {
    private weak var hostedWebView: WKWebView?
    private var hostedConstraints: [NSLayoutConstraint] = []

    func display(_ webView: WKWebView) {
        guard hostedWebView !== webView else { return }
        NSLayoutConstraint.deactivate(hostedConstraints)
        hostedWebView?.removeFromSuperview()
        webView.removeFromSuperview()
        hostedWebView = webView
        webView.translatesAutoresizingMaskIntoConstraints = false
        addSubview(webView)
        hostedConstraints = [
            webView.leadingAnchor.constraint(equalTo: leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: trailingAnchor),
            webView.topAnchor.constraint(equalTo: topAnchor),
            webView.bottomAnchor.constraint(equalTo: bottomAnchor),
        ]
        NSLayoutConstraint.activate(hostedConstraints)
    }
}

private struct NativeBrowserWebView: NSViewRepresentable {
    let webView: WKWebView

    func makeNSView(context: Context) -> NativeBrowserContainerView {
        let container = NativeBrowserContainerView()
        container.display(webView)
        return container
    }

    func updateNSView(_ container: NativeBrowserContainerView, context: Context) {
        container.display(webView)
    }
}
#else
private final class NativeBrowserContainerView: UIView {
    private weak var hostedWebView: WKWebView?
    private var hostedConstraints: [NSLayoutConstraint] = []

    func display(_ webView: WKWebView) {
        guard hostedWebView !== webView else { return }
        NSLayoutConstraint.deactivate(hostedConstraints)
        hostedWebView?.removeFromSuperview()
        webView.removeFromSuperview()
        hostedWebView = webView
        webView.translatesAutoresizingMaskIntoConstraints = false
        addSubview(webView)
        hostedConstraints = [
            webView.leadingAnchor.constraint(equalTo: leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: trailingAnchor),
            webView.topAnchor.constraint(equalTo: topAnchor),
            webView.bottomAnchor.constraint(equalTo: bottomAnchor),
        ]
        NSLayoutConstraint.activate(hostedConstraints)
    }
}

private struct NativeBrowserWebView: UIViewRepresentable {
    let webView: WKWebView

    func makeUIView(context: Context) -> NativeBrowserContainerView {
        let container = NativeBrowserContainerView()
        container.display(webView)
        return container
    }

    func updateUIView(_ container: NativeBrowserContainerView, context: Context) {
        container.display(webView)
    }
}
#endif
