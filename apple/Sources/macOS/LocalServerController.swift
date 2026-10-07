#if os(macOS)
import AppKit
import Darwin
import Foundation

@MainActor
final class LocalServerController {
    private var process: Process?

    init() {
        NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                self?.stop()
            }
        }
    }

    deinit {
        process?.terminate()
    }

    func start(
        executableOverride: String?,
        workingDirectory: URL
    ) async throws -> URL {
        stop()

        let executable = try resolveExecutable(override: executableOverride)
        let port = try reservePort()
        let baseURL = URL(string: "http://127.0.0.1:\(port)")!

        let child = Process()
        child.executableURL = executable.executable
        child.arguments = executable.prefixArguments + [
            "--host", "127.0.0.1",
            "--port", String(port),
            "--cwd", workingDirectory.path,
            "--parent-pid", String(ProcessInfo.processInfo.processIdentifier),
        ]
        child.currentDirectoryURL = workingDirectory

        let logURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("autoverse-apple-server.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        let logHandle = try FileHandle(forWritingTo: logURL)
        try logHandle.seekToEnd()
        child.standardOutput = logHandle
        child.standardError = logHandle

        do {
            try child.run()
        } catch {
            try? logHandle.close()
            throw LocalServerError.message("Could not launch AutoVerse: \(error.localizedDescription)")
        }

        process = child

        do {
            try await waitUntilReady(baseURL: baseURL, process: child)
            try? logHandle.close()
            return baseURL
        } catch {
            stop()
            try? logHandle.close()
            throw error
        }
    }

    func stop() {
        guard let process else { return }
        if process.isRunning {
            process.terminate()
            let deadline = Date().addingTimeInterval(1.5)
            while process.isRunning && Date() < deadline {
                RunLoop.current.run(until: Date().addingTimeInterval(0.05))
            }
            if process.isRunning {
                kill(process.processIdentifier, SIGKILL)
            }
        }
        self.process = nil
    }

    private func waitUntilReady(baseURL: URL, process: Process) async throws {
        let healthURL = baseURL.appendingPathComponent("theme.json")
        for _ in 0..<80 {
            guard process.isRunning else {
                throw LocalServerError.message(
                    "AutoVerse stopped during startup. See the log in \(FileManager.default.temporaryDirectory.path)."
                )
            }

            var request = URLRequest(url: healthURL)
            request.timeoutInterval = 0.5
            if let (_, response) = try? await URLSession.shared.data(for: request),
               let http = response as? HTTPURLResponse,
               (200..<300).contains(http.statusCode) {
                return
            }
            try await Task.sleep(for: .milliseconds(100))
        }
        throw LocalServerError.message("Timed out waiting for the local AutoVerse server.")
    }

    private func resolveExecutable(override: String?) throws -> ResolvedExecutable {
        if let override, !override.isEmpty {
            let url = URL(fileURLWithPath: override)
            guard FileManager.default.isExecutableFile(atPath: url.path) else {
                throw LocalServerError.message("The configured autoverse-web executable is not executable.")
            }
            return ResolvedExecutable(executable: url, prefixArguments: [])
        }

        if let environmentPath = ProcessInfo.processInfo.environment["AUTOVERSE_WEB_EXECUTABLE"],
           FileManager.default.isExecutableFile(atPath: environmentPath) {
            return ResolvedExecutable(
                executable: URL(fileURLWithPath: environmentPath),
                prefixArguments: []
            )
        }

        let candidates = developmentCandidates()
        if let candidate = candidates.first(where: {
            FileManager.default.isExecutableFile(atPath: $0.path)
        }) {
            return ResolvedExecutable(executable: candidate, prefixArguments: [])
        }

        if let env = executableInPath(named: "autoverse-web") {
            return ResolvedExecutable(executable: env, prefixArguments: [])
        }

        if let python = developmentPythonCandidates().first(where: {
            FileManager.default.isExecutableFile(atPath: $0.path)
        }) {
            return ResolvedExecutable(
                executable: python,
                prefixArguments: ["-m", "autoverse.frontends.web.server"]
            )
        }

        throw LocalServerError.message(
            "autoverse-web was not found. Activate the AutoVerse virtual environment or set AUTOVERSE_WEB_EXECUTABLE."
        )
    }

    private func developmentCandidates() -> [URL] {
        let cwd = URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true)
        return [
            cwd.appendingPathComponent(".venv/bin/autoverse-web"),
            cwd.deletingLastPathComponent().appendingPathComponent(".venv/bin/autoverse-web"),
        ]
    }

    private func developmentPythonCandidates() -> [URL] {
        let cwd = URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true)
        return [
            cwd.appendingPathComponent(".venv/bin/python"),
            cwd.deletingLastPathComponent().appendingPathComponent(".venv/bin/python"),
        ]
    }

    private func executableInPath(named name: String) -> URL? {
        let path = ProcessInfo.processInfo.environment["PATH"] ?? ""
        for directory in path.split(separator: ":") {
            let candidate = URL(fileURLWithPath: String(directory))
                .appendingPathComponent(name)
            if FileManager.default.isExecutableFile(atPath: candidate.path) {
                return candidate
            }
        }
        return nil
    }

    private func reservePort() throws -> UInt16 {
        let descriptor = socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else {
            throw LocalServerError.message("Could not create a local socket.")
        }
        defer { close(descriptor) }

        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = 0
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))

        let bindResult = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindResult == 0 else {
            throw LocalServerError.message("Could not reserve a local port.")
        }

        var selected = sockaddr_in()
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let nameResult = withUnsafeMutablePointer(to: &selected) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(descriptor, $0, &length)
            }
        }
        guard nameResult == 0 else {
            throw LocalServerError.message("Could not read the reserved local port.")
        }
        return UInt16(bigEndian: selected.sin_port)
    }
}

private struct ResolvedExecutable {
    let executable: URL
    let prefixArguments: [String]
}

private enum LocalServerError: LocalizedError {
    case message(String)

    var errorDescription: String? {
        switch self {
        case .message(let message): message
        }
    }
}
#endif
