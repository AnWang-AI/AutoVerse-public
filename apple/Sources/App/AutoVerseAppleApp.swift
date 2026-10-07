import SwiftUI

@main
struct AutoVerseAppleApp: App {
    @StateObject private var model = AppModel()

    #if os(macOS)
    var body: some Scene {
        WindowGroup {
            rootView
        }
        .defaultSize(width: 1280, height: 820)
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(after: .appInfo) {
                Button("Reconnect") {
                    Task { await model.connect() }
                }
                .keyboardShortcut("r", modifiers: [.command, .shift])
            }
        }

        Settings {
            ConnectionSettingsView()
                .environmentObject(model)
                .frame(width: 540)
                .padding()
        }
    }

    private var rootView: some View {
        ContentView()
            .environmentObject(model)
            .preferredColorScheme(model.currentColorScheme)
            .modifier(WindowConfigModifier(scheme: model.currentColorScheme))
            .task {
                await model.startIfNeeded()
            }
    }
    #else
    var body: some Scene {
        WindowGroup {
            rootView
        }
    }

    private var rootView: some View {
        ContentView()
            .environmentObject(model)
            .preferredColorScheme(model.currentColorScheme)
            .task {
                await model.startIfNeeded()
            }
    }
    #endif
}

#if os(macOS)
// MARK: - Window Titlebar Transparency & Background Sync

/// AutoVerse dark theme background: #0e0e10
private let autoverseDarkBg = NSColor(red: 0x0E / 255, green: 0x0E / 255, blue: 0x10 / 255, alpha: 1)
/// AutoVerse light theme background: #f7f4ed
private let autoverseLightBg = NSColor(red: 0xF7 / 255, green: 0xF4 / 255, blue: 0xED / 255, alpha: 1)
/// Shared geometry for the native controls and the SwiftUI titlebar content.
let autoverseTitlebarHeight: CGFloat = 40

@MainActor
final class TrafficLightButton: NSButton {
    enum Kind {
        case close
        case minimize
        case fullScreen
    }

    let kind: Kind
    private var hovering = false

    init(kind: Kind) {
        self.kind = kind
        super.init(frame: .zero)
        isBordered = false
        title = ""
        focusRingType = .none
        setButtonType(.momentaryChange)
        switch kind {
        case .close:
            toolTip = "Close"
            setAccessibilityLabel("Close")
        case .minimize:
            toolTip = "Minimize"
            setAccessibilityLabel("Minimize")
        case .fullScreen:
            toolTip = "Enter Full Screen"
            setAccessibilityLabel("Full Screen")
        }
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(
            NSTrackingArea(
                rect: bounds,
                options: [.activeInKeyWindow, .mouseEnteredAndExited],
                owner: self
            )
        )
    }

    override func mouseEntered(with event: NSEvent) {
        hovering = true
        needsDisplay = true
    }

    override func mouseExited(with event: NSEvent) {
        hovering = false
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        let isActive = window?.isKeyWindow ?? false
        let fillColor: NSColor
        let strokeColor: NSColor
        if isActive {
            switch kind {
            case .close:
                fillColor = NSColor(red: 1.00, green: 0.37, blue: 0.34, alpha: 1)
                strokeColor = NSColor(red: 0.88, green: 0.24, blue: 0.22, alpha: 1)
            case .minimize:
                fillColor = NSColor(red: 1.00, green: 0.74, blue: 0.18, alpha: 1)
                strokeColor = NSColor(red: 0.87, green: 0.58, blue: 0.08, alpha: 1)
            case .fullScreen:
                fillColor = NSColor(red: 0.16, green: 0.78, blue: 0.25, alpha: 1)
                strokeColor = NSColor(red: 0.08, green: 0.64, blue: 0.17, alpha: 1)
            }
        } else {
            fillColor = NSColor(white: 0.78, alpha: 1)
            strokeColor = NSColor(white: 0.69, alpha: 1)
        }

        let circle = NSBezierPath(ovalIn: bounds.insetBy(dx: 0.5, dy: 0.5))
        fillColor.setFill()
        circle.fill()
        strokeColor.setStroke()
        circle.lineWidth = 0.7
        circle.stroke()

        guard hovering, isActive else { return }
        strokeColor.setStroke()
        let glyph = NSBezierPath()
        glyph.lineWidth = 1.1
        glyph.lineCapStyle = .round
        switch kind {
        case .close:
            glyph.move(to: NSPoint(x: 4.5, y: 4.5))
            glyph.line(to: NSPoint(x: 9.5, y: 9.5))
            glyph.move(to: NSPoint(x: 9.5, y: 4.5))
            glyph.line(to: NSPoint(x: 4.5, y: 9.5))
        case .minimize:
            glyph.move(to: NSPoint(x: 4, y: 7))
            glyph.line(to: NSPoint(x: 10, y: 7))
        case .fullScreen:
            glyph.move(to: NSPoint(x: 4, y: 8))
            glyph.line(to: NSPoint(x: 4, y: 4))
            glyph.line(to: NSPoint(x: 8, y: 4))
            glyph.move(to: NSPoint(x: 10, y: 6))
            glyph.line(to: NSPoint(x: 10, y: 10))
            glyph.line(to: NSPoint(x: 6, y: 10))
        }
        glyph.stroke()
    }
}

@MainActor
final class TrafficLightHostNSView: NSView {
    private var buttons: [TrafficLightButton] = []
    private var originalButtons: [NSButton] = []
    private var originalHiddenStates: [Bool] = []
    private var positioningConstraints: [NSLayoutConstraint] = []
    private weak var managedWindow: NSWindow?

    override var mouseDownCanMoveWindow: Bool { true }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        guard let window else { return }
        attachButtons(from: window)
    }

    override func viewWillMove(toWindow newWindow: NSWindow?) {
        if newWindow == nil {
            restoreButtons()
        }
        super.viewWillMove(toWindow: newWindow)
    }

    func attachButtons(from window: NSWindow) {
        guard managedWindow !== window else { return }
        restoreButtons()
        managedWindow = window

        let definitions: [(NSWindow.ButtonType, TrafficLightButton.Kind, CGFloat)] = [
            (.closeButton, .close, 15),
            (.miniaturizeButton, .minimize, 36),
            (.zoomButton, .fullScreen, 58),
        ]

        for (buttonType, kind, centerX) in definitions {
            guard let originalButton = window.standardWindowButton(buttonType) else { continue }

            originalButtons.append(originalButton)
            originalHiddenStates.append(originalButton.isHidden)
            originalButton.isHidden = true

            let button = TrafficLightButton(kind: kind)
            button.target = self
            button.action = #selector(handleTrafficLight(_:))
            button.translatesAutoresizingMaskIntoConstraints = false
            addSubview(button)
            buttons.append(button)
            positioningConstraints.append(contentsOf: [
                button.widthAnchor.constraint(equalToConstant: 14),
                button.heightAnchor.constraint(equalToConstant: 14),
                button.centerXAnchor.constraint(equalTo: leadingAnchor, constant: centerX),
                button.centerYAnchor.constraint(equalTo: centerYAnchor),
            ])
        }
        NSLayoutConstraint.activate(positioningConstraints)
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(windowKeyStateChanged),
            name: NSWindow.didBecomeKeyNotification,
            object: window
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(windowKeyStateChanged),
            name: NSWindow.didResignKeyNotification,
            object: window
        )
    }

    @objc private func handleTrafficLight(_ sender: TrafficLightButton) {
        guard let window = managedWindow else { return }
        switch sender.kind {
        case .close:
            window.performClose(sender)
        case .minimize:
            window.performMiniaturize(sender)
        case .fullScreen:
            if NSEvent.modifierFlags.contains(.option) {
                window.performZoom(sender)
            } else {
                window.toggleFullScreen(sender)
            }
        }
    }

    @objc private func windowKeyStateChanged() {
        buttons.forEach { $0.needsDisplay = true }
    }

    func restoreButtons() {
        NotificationCenter.default.removeObserver(self)
        NSLayoutConstraint.deactivate(positioningConstraints)
        positioningConstraints.removeAll()

        for button in buttons {
            button.removeFromSuperview()
        }
        for index in originalButtons.indices {
            originalButtons[index].isHidden = originalHiddenStates[index]
        }
        buttons.removeAll()
        originalButtons.removeAll()
        originalHiddenStates.removeAll()
        managedWindow = nil
    }
}

struct NativeTrafficLights: NSViewRepresentable {
    func makeNSView(context: Context) -> TrafficLightHostNSView {
        TrafficLightHostNSView()
    }

    func updateNSView(_ nsView: TrafficLightHostNSView, context: Context) {
        guard let window = nsView.window else { return }
        nsView.attachButtons(from: window)
    }

    static func dismantleNSView(_ nsView: TrafficLightHostNSView, coordinator: ()) {
        nsView.restoreButtons()
    }
}

/// Sync the window titlebar/background with the AutoVerse web theme so the
/// transparent titlebar never shows a stale dark frame over light content.
@MainActor
private func applyAutoVerseAppearance(_ scheme: ColorScheme?, to window: NSWindow) {
    window.styleMask.insert(.fullSizeContentView)
    if !window.titlebarAppearsTransparent {
        window.titlebarAppearsTransparent = true
    }
    if window.titleVisibility != .hidden {
        window.titleVisibility = .hidden
    }
    if !window.isMovableByWindowBackground {
        window.isMovableByWindowBackground = true
    }
    // Divider is drawn manually in ContentView (the native one is too faint).
    if window.titlebarSeparatorStyle != .none {
        window.titlebarSeparatorStyle = .none
    }
    let isDark = scheme == .dark
    let appearanceName: NSAppearance.Name = isDark ? .darkAqua : .aqua
    if window.appearance?.name != appearanceName {
        window.appearance = NSAppearance(named: appearanceName)
    }
    let backgroundColor = isDark ? autoverseDarkBg : autoverseLightBg
    if window.backgroundColor != backgroundColor {
        window.backgroundColor = backgroundColor
    }
}

private struct WindowConfigModifier: ViewModifier {
    /// Passed in from the App (where `model` is directly available) rather than
    /// read via `@EnvironmentObject` — this modifier is applied *outside* the
    /// `.environmentObject(model)` call, so it has no AppModel in its environment.
    var scheme: ColorScheme?

    func body(content: Content) -> some View {
        content
            .background(WindowConfigView(scheme: scheme))
    }
}

private struct WindowConfigView: NSViewRepresentable {
    var scheme: ColorScheme?

    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        let scheme = scheme
        DispatchQueue.main.async {
            guard let w = view.window else { return }
            applyAutoVerseAppearance(scheme, to: w)
        }
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        // Runs whenever `scheme` changes *and* once the view is attached to a
        // window — covers the case where the web theme is reported before the
        // window reference is available, avoiding the dark-titlebar race.
        guard let w = nsView.window else { return }
        applyAutoVerseAppearance(scheme, to: w)
    }
}
#endif
