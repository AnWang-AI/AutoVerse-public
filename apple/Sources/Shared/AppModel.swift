import Combine
import Foundation
import SwiftUI

enum ConnectionMode: String, CaseIterable, Identifiable {
    case local
    case remote

    var id: String { rawValue }

    var title: String {
        switch self {
        case .local: "Local Mac"
        case .remote: "Remote"
        }
    }
}

enum ConnectionState: Equatable {
    case idle
    case connecting(String)
    case connected(URL)
    case failed(String)

    var label: String {
        switch self {
        case .idle: "Not connected"
        case .connecting(let message): message
        case .connected: "Connected"
        case .failed(let message): message
        }
    }

    var isBusy: Bool {
        if case .connecting = self { return true }
        return false
    }
}

struct NativeChromeState: Equatable {
    var sessionTitle = "AutoVerse"
    var leftWidth: CGFloat = 260
    var rightWidth: CGFloat = 336
    var leftVisible = true
    var rightVisible = true
    var actionID = ""
    var phase = "settled"
    var motionToken = "panel"
}

struct NativeWebCommand: Equatable, Encodable {
    let id: UUID
    let action: String
    let motionToken: String

    init(
        action: String,
        motionToken: String = "panel"
    ) {
        id = UUID()
        self.action = action
        self.motionToken = motionToken
    }
}

@MainActor
final class AppModel: ObservableObject {
    let browserHost = BrowserHostController()
    @Published private(set) var state: ConnectionState = .idle
    @Published private(set) var webURL: URL?
    @Published var showingSettings = false
    @Published var reloadID = UUID()
    @Published var currentColorScheme: ColorScheme? = .light
    @Published var nativeChrome = NativeChromeState()
    @Published var nativeWebCommand: NativeWebCommand?

    @AppStorage("connectionMode") private var storedMode = ConnectionMode.local.rawValue
    @AppStorage("remoteBaseURL") var remoteBaseURL = ""
    @AppStorage("allowInsecureRemoteHTTP") var allowInsecureRemoteHTTP = false
    @AppStorage("localWorkingDirectory") var localWorkingDirectory = ""
    @AppStorage("localExecutablePath") var localExecutablePath = ""

    #if os(macOS)
    private let localServer = LocalServerController()
    #endif
    private var didStart = false
    private var healthCheckTask: Task<Void, Never>?
    private var browserObservation: AnyCancellable?

    init() {
        browserObservation = browserHost.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }
    }

    var mode: ConnectionMode {
        get {
            #if os(iOS)
            return .remote
            #else
            return ConnectionMode(rawValue: storedMode) ?? .local
            #endif
        }
        set {
            storedMode = newValue.rawValue
            objectWillChange.send()
        }
    }

    func startIfNeeded() async {
        guard !didStart else { return }
        didStart = true
        await connect()
    }

    func connect() async {
        stopHealthCheck()
        browserHost.disconnect()
        webURL = nil
        switch mode {
        case .local:
            #if os(macOS)
            await connectLocal()
            #else
            state = .failed("Local execution is only available on macOS.")
            #endif
        case .remote:
            await connectRemote()
        }
    }

    func reload() {
        reloadID = UUID()
    }

    func sendWebCommand(_ action: String) {
        let command = NativeWebCommand(action: action)
        InteractionSignposts.nativeInput(command.id.uuidString)
        nativeWebCommand = command
    }

    func applyNativeChromeState(_ state: NativeChromeState) {
        guard state != nativeChrome else { return }
        nativeChrome = state
    }

    func disconnect() {
        #if os(macOS)
        localServer.stop()
        #endif
        stopHealthCheck()
        browserHost.disconnect()
        webURL = nil
        state = .idle
    }

    // ── health check ───────────────────────────────────────────────────────────
    private func stopHealthCheck() {
        healthCheckTask?.cancel()
        healthCheckTask = nil
    }

    #if os(macOS)
    private func startHealthCheck(url: URL) {
        stopHealthCheck()
        healthCheckTask = Task {
            var failures = 0
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(5))
                guard !Task.isCancelled,
                      case .connected = state,
                      webURL == url else { break }
                do {
                    try await probe(url)
                    failures = 0
                } catch {
                    failures += 1
                    guard failures >= 2 else { continue }
                    await connect()
                    break
                }
            }
        }
    }
    #endif

    #if os(macOS)
    private func connectLocal() async {
        state = .connecting("Starting local AutoVerse…")
        do {
            let cwd = resolvedWorkingDirectory()
            let executable = localExecutablePath.trimmingCharacters(in: .whitespacesAndNewlines)
            let url = try await localServer.start(
                executableOverride: executable.isEmpty ? nil : executable,
                workingDirectory: cwd
            )
            webURL = url
            state = .connected(url)
            browserHost.connect(to: url)
            startHealthCheck(url: url)
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    private func resolvedWorkingDirectory() -> URL {
        let configured = localWorkingDirectory.trimmingCharacters(in: .whitespacesAndNewlines)
        if !configured.isEmpty {
            return URL(fileURLWithPath: configured, isDirectory: true)
        }

        let cwd = URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true)
        if cwd.lastPathComponent == "apple" {
            return cwd.deletingLastPathComponent()
        }
        return cwd
    }
    #endif

    private func connectRemote() async {
        state = .connecting("Checking remote AutoVerse…")
        do {
            let url = try normalizedRemoteURL()
            try await probe(url)
            webURL = url
            state = .connected(url)
            browserHost.connect(to: url)
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    private func normalizedRemoteURL() throws -> URL {
        var raw = remoteBaseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !raw.isEmpty else {
            throw AppConnectionError.message("Enter the AutoVerse server URL.")
        }
        if !raw.contains("://") {
            raw = "https://" + raw
        }
        guard let components = URLComponents(string: raw),
              let scheme = components.scheme?.lowercased(),
              ["http", "https"].contains(scheme),
              components.host != nil,
              let url = components.url else {
            throw AppConnectionError.message("The server URL is invalid.")
        }
        if scheme == "http" && !allowInsecureRemoteHTTP && !isLoopback(url) {
            throw AppConnectionError.message(
                "Remote HTTP is disabled. Use HTTPS or explicitly allow insecure HTTP for development."
            )
        }
        return url
    }

    private func probe(_ baseURL: URL) async throws {
        let healthURL = baseURL.appendingPathComponent("theme.json")
        var request = URLRequest(url: healthURL)
        request.timeoutInterval = 8
        let (_, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw AppConnectionError.message("The server did not return a valid AutoVerse response.")
        }
    }

    private func isLoopback(_ url: URL) -> Bool {
        guard let host = url.host?.lowercased() else { return false }
        return host == "localhost" || host == "127.0.0.1" || host == "::1"
    }
}

enum AppConnectionError: LocalizedError {
    case message(String)

    var errorDescription: String? {
        switch self {
        case .message(let message): message
        }
    }
}
