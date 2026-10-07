import SwiftUI

struct ConnectionSettingsView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        Form {
            #if os(macOS)
            Section("Connection mode") {
                Picker("Mode", selection: Binding(
                    get: { model.mode },
                    set: { model.mode = $0 }
                )) {
                    ForEach(ConnectionMode.allCases) { mode in
                        Text(mode.title).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
            }
            #endif

            if model.mode == .remote {
                remoteSection
            } else {
                #if os(macOS)
                localSection
                #endif
            }

            Section {
                HStack {
                    Button("Disconnect") {
                        model.disconnect()
                    }
                    .disabled(model.webURL == nil)

                    Spacer()

                    Button("Connect") {
                        Task {
                            await model.connect()
                            if model.webURL != nil {
                                dismiss()
                            }
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.state.isBusy)
                }
            } footer: {
                Text(statusText)
                    .foregroundStyle(statusColor)
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Connection")
        .padding()
    }

    private var remoteSection: some View {
        Section("Remote AutoVerse") {
            TextField("https://autoverse.example.com", text: $model.remoteBaseURL)
                .textContentType(.URL)
                #if os(iOS)
                .textInputAutocapitalization(.never)
                .keyboardType(.URL)
                .autocorrectionDisabled()
                #endif

            Toggle("Allow insecure HTTP for development", isOn: $model.allowInsecureRemoteHTTP)

            Text("Use HTTPS or a trusted VPN. The current AutoVerse Web server must not be exposed directly to the public internet without authentication.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    #if os(macOS)
    private var localSection: some View {
        Section("Local execution") {
            TextField("Working directory (defaults to repository)", text: $model.localWorkingDirectory)
            TextField("autoverse-web executable (optional)", text: $model.localExecutablePath)

            Text("The app starts the existing Python/FastAPI WebUI on 127.0.0.1 and terminates it when the app exits.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
    #endif

    private var statusText: String {
        switch model.state {
        case .connected(let url): "Connected to \(url.absoluteString)"
        default: model.state.label
        }
    }

    private var statusColor: Color {
        if case .failed = model.state { return .red }
        return .secondary
    }
}
