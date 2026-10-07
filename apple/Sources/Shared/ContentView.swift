import SwiftUI

#if os(macOS)
import AppKit
#endif

enum BrowserWorkspaceLayout {
    static let minimumBrowserWidth: CGFloat = 320
    static let defaultBrowserWidthRatio: CGFloat = 0.5
    static let minimumDefaultBrowserWidth: CGFloat = 600
    static let maximumDefaultBrowserWidth: CGFloat = 900
    static let comfortableConversationWidth: CGFloat = 520
    static let minimumConversationWidth: CGFloat = 520
    static let sessionsWidth: CGFloat = 260
    static let dividerWidth: CGFloat = 40

    static func defaultBrowserWidth(available: CGFloat) -> CGFloat {
        min(
            max(available * defaultBrowserWidthRatio, minimumDefaultBrowserWidth),
            maximumDefaultBrowserWidth
        )
    }

    static func browserWidth(
        preferred: CGFloat,
        available: CGFloat,
        sessionsWidth: CGFloat
    ) -> CGFloat {
        let chatMinimum = minimumConversationWidth + max(0, sessionsWidth)
        let maximum = max(
            minimumBrowserWidth,
            available - chatMinimum
        )
        return min(max(preferred, minimumBrowserWidth), maximum)
    }

    static func maximumBrowserWidth(
        available: CGFloat,
        sessionsWidth: CGFloat
    ) -> CGFloat {
        browserWidth(
            preferred: available,
            available: available,
            sessionsWidth: sessionsWidth
        )
    }

    static func shouldCollapseSessions(
        preferredBrowserWidth: CGFloat,
        available: CGFloat,
        sessionsWidth: CGFloat
    ) -> Bool {
        sessionsWidth > 0
            && available - sessionsWidth - preferredBrowserWidth
                < comfortableConversationWidth
    }
}

struct ContentView: View {
    @EnvironmentObject private var model: AppModel
    #if os(macOS)
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var browserWorkspaceAvailableWidth: CGFloat = 880
    @State private var browserWorkspaceDragStart: CGFloat?
    @State private var browserWorkspaceWidth: CGFloat?
    @State private var browserWorkspaceRestoreWidth: CGFloat?
    @State private var browserWorkspaceIsMaximized = false
    @State private var browserWorkspaceCollapseRequested = false
    #endif
    @State private var conversationOverlayVisible = false

    var body: some View {
        Group {
            #if os(macOS)
            VStack(spacing: 0) {
                nativeTitlebar
                appContent
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .layoutPriority(1)
            }
            .overlay(alignment: .trailing) {
                if model.browserHost.isVisible && !conversationOverlayVisible {
                    ZStack {
                        BrowserWorkspaceResizeHandle(
                            onDragBegan: beginBrowserWorkspaceDrag,
                            onDragChanged: updateBrowserWorkspaceDrag,
                            onDragEnded: endBrowserWorkspaceDrag,
                            onDoubleClick: resetBrowserWorkspaceWidth
                        )
                        .frame(maxWidth: .infinity, maxHeight: .infinity)

                        Rectangle()
                            .fill(separatorColor)
                            .frame(width: 1)
                            .allowsHitTesting(false)
                    }
                    .frame(width: BrowserWorkspaceLayout.dividerWidth)
                    .frame(maxHeight: .infinity)
                    .offset(
                        x: -rightPanelWidth
                            + BrowserWorkspaceLayout.dividerWidth / 2
                    )
                    .zIndex(3)
                }
            }
            .ignoresSafeArea(.container, edges: .top)
            #else
            appContent
            #endif
        }
        .frame(minWidth: minimumWidth, minHeight: minimumHeight)
        #if os(iOS)
        .background(Color.black)
        .ignoresSafeArea()
        #endif
        #if os(macOS)
        // Paint the theme color behind everything (including the titlebar strip)
        // so the window looks seamless, but keep the foreground content within
        // the safe area so it starts *below* the titlebar/toolbar instead of
        // sliding under the native controls and overlapping them.
        .background(appBackground.ignoresSafeArea())
        #endif
        .sheet(isPresented: $model.showingSettings) {
            NavigationStack {
                ConnectionSettingsView()
                    .environmentObject(model)
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) {
                            Button("Done") {
                                model.showingSettings = false
                            }
                        }
                    }
            }
            #if os(macOS)
            .frame(minWidth: 560, minHeight: 480)
            #endif
        }
    }

    @ViewBuilder
    private var appContent: some View {
        if let url = model.webURL {
            browserLayout(chatURL: url)
        } else {
            connectionPlaceholder
        }
    }

    @ViewBuilder
    private func browserLayout(chatURL url: URL) -> some View {
        let chat = WebView(
                url: url,
                reloadID: model.reloadID,
                nativeCommand: model.nativeWebCommand,
                onOpenConnectionSettings: {
                    model.showingSettings = true
                },
                onThemeChange: { scheme in
                    if model.currentColorScheme != scheme {
                        model.currentColorScheme = scheme
                    }
                },
                onChromeStateChange: { state in
                    model.applyNativeChromeState(state)
                },
                onOpenFilePreview: {
                    #if os(macOS)
                    if model.browserHost.isVisible {
                        model.browserHost.toggleVisible()
                    }
                    #endif
                },
                onOpenBrowserLink: { url in
                    model.browserHost.openPageFromChat(url)
                },
                onAgentResume: {
                    model.browserHost.resumeAgentControl()
                },
                onConversationOverlayChange: { visible in
                    if conversationOverlayVisible != visible {
                        conversationOverlayVisible = visible
                    }
                },
                browserWorkspaceVisible: model.browserHost.isVisible,
                browserWorkspaceWidth: nativeBrowserWorkspaceWidth
            )
                .ignoresSafeArea(.container, edges: webViewIgnoredEdges)
                // Keyboard avoidance is handled natively in WebView.swift by
                // animating the web view's bottom inset with the keyboard's
                // own curve. SwiftUI must NOT also push the view up, or the
                // two stack and the whole UI jumps/shakes on focus.
                .ignoresSafeArea(.keyboard)

        #if os(macOS)
        GeometryReader { geometry in
            let browserWidth = resolvedBrowserWorkspaceWidth(
                availableWidth: geometry.size.width
            )
            ZStack(alignment: .trailing) {
                chat
                    .allowsHitTesting(browserWorkspaceDragStart == nil)
                    .zIndex(conversationOverlayVisible ? 2 : 0)
                BrowserPane(
                    controller: model.browserHost,
                    workspaceIsMaximized: browserWorkspaceIsMaximized,
                    onToggleWorkspaceSize: {
                        toggleBrowserWorkspaceSize(
                            currentWidth: browserWidth,
                            availableWidth: geometry.size.width
                        )
                    }
                )
                    .frame(width: browserWidth)
                    .offset(
                        x: model.browserHost.isVisible
                            ? 0 : browserWidth * 1.02
                    )
                    .opacity(model.browserHost.isVisible ? 1 : 0.35)
                    .allowsHitTesting(
                        model.browserHost.isVisible && !conversationOverlayVisible
                    )
                    .accessibilityHidden(
                        !model.browserHost.isVisible || conversationOverlayVisible
                    )
                    .zIndex(1)
            }
            .clipped()
            .animation(
                panelAnimation,
                value: model.browserHost.isVisible
            )
            .onChange(of: model.browserHost.isVisible) { _, visible in
                browserWorkspaceDragStart = nil
                browserWorkspaceRestoreWidth = nil
                browserWorkspaceIsMaximized = false
                if visible {
                    let preferred = BrowserWorkspaceLayout.defaultBrowserWidth(
                        available: geometry.size.width
                    )
                    if BrowserWorkspaceLayout.shouldCollapseSessions(
                        preferredBrowserWidth: preferred,
                        available: geometry.size.width,
                        sessionsWidth: effectiveSessionsWidth
                    ) {
                        requestSessionsCollapse()
                    }
                    browserWorkspaceWidth = BrowserWorkspaceLayout.browserWidth(
                        preferred: preferred,
                        available: geometry.size.width,
                        sessionsWidth: effectiveSessionsWidth
                    )
                } else {
                    browserWorkspaceWidth = nil
                    browserWorkspaceCollapseRequested = false
                }
            }
            .onChange(of: model.nativeChrome.leftVisible) { _, visible in
                if browserWorkspaceCollapseRequested, !visible {
                    browserWorkspaceCollapseRequested = false
                } else if model.browserHost.isVisible, visible {
                    if let preferred = browserWorkspaceWidth {
                        browserWorkspaceWidth = BrowserWorkspaceLayout.browserWidth(
                            preferred: preferred,
                            available: geometry.size.width,
                            sessionsWidth: leftPanelWidth
                        )
                    }
                }
            }
            .onChange(of: geometry.size.width) { _, availableWidth in
                browserWorkspaceAvailableWidth = availableWidth
                guard model.browserHost.isVisible else { return }
                if browserWorkspaceIsMaximized {
                    browserWorkspaceWidth = BrowserWorkspaceLayout.maximumBrowserWidth(
                        available: availableWidth,
                        sessionsWidth: effectiveSessionsWidth
                    )
                } else if let preferred = browserWorkspaceWidth {
                    browserWorkspaceWidth = BrowserWorkspaceLayout.browserWidth(
                        preferred: preferred,
                        available: availableWidth,
                        sessionsWidth: effectiveSessionsWidth
                    )
                }
            }
            .onAppear {
                browserWorkspaceAvailableWidth = geometry.size.width
            }
        }
        #else
        if model.browserHost.isVisible {
            BrowserPane(controller: model.browserHost)
                .ignoresSafeArea(.keyboard)
        } else {
            chat.overlay(alignment: .topTrailing) {
                Button {
                    model.browserHost.toggleVisible()
                } label: {
                    Image(systemName: "safari")
                        .padding(10)
                        .background(.regularMaterial, in: Circle())
                }
                .buttonStyle(.plain)
                .padding(.top, 12)
                .padding(.trailing, 12)
                .accessibilityLabel("Show Browser")
            }
        }
        #endif
    }

    #if os(macOS)
    private func beginBrowserWorkspaceDrag() {
        browserWorkspaceDragStart = rightPanelWidth
        browserWorkspaceRestoreWidth = nil
        browserWorkspaceIsMaximized = false
    }

    private func updateBrowserWorkspaceDrag(translation: CGFloat) {
        let start = browserWorkspaceDragStart ?? rightPanelWidth
        let preferred = start - translation
        if preferred > start,
           BrowserWorkspaceLayout.shouldCollapseSessions(
            preferredBrowserWidth: preferred,
            available: browserWorkspaceAvailableWidth,
            sessionsWidth: effectiveSessionsWidth
           ) {
            requestSessionsCollapse()
        }
        browserWorkspaceWidth = BrowserWorkspaceLayout.browserWidth(
            preferred: preferred,
            available: browserWorkspaceAvailableWidth,
            sessionsWidth: effectiveSessionsWidth
        )
    }

    private func endBrowserWorkspaceDrag() {
        browserWorkspaceDragStart = nil
    }

    private func resetBrowserWorkspaceWidth() {
        browserWorkspaceWidth = BrowserWorkspaceLayout.browserWidth(
            preferred: BrowserWorkspaceLayout.defaultBrowserWidth(
                available: browserWorkspaceAvailableWidth
            ),
            available: browserWorkspaceAvailableWidth,
            sessionsWidth: effectiveSessionsWidth
        )
        browserWorkspaceRestoreWidth = nil
        browserWorkspaceIsMaximized = false
        browserWorkspaceDragStart = nil
    }
    #endif

    private var connectionPlaceholder: some View {
        VStack(spacing: 18) {
            Image(systemName: placeholderSymbol)
                .font(.system(size: 44, weight: .light))
                .foregroundStyle(.secondary)

            Text("AutoVerse")
                .font(.title.bold())

            Text(model.state.label)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 520)

            if model.state.isBusy {
                ProgressView()
                    .controlSize(.large)
            } else {
                HStack {
                    Button("Connection Settings") {
                        model.showingSettings = true
                    }
                    .buttonStyle(.bordered)

                    Button("Connect") {
                        Task { await model.connect() }
                    }
                    .buttonStyle(.borderedProminent)
                }
            }
        }
        .padding(32)
    }

    private var connectionIndicator: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(model.webURL == nil ? Color.secondary : Color.green)
                .frame(width: 6, height: 6)
            Text(model.mode.title)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary.opacity(0.86))
        }
    }

    private var placeholderSymbol: String {
        if case .failed = model.state { return "exclamationmark.triangle" }
        return "sparkles.rectangle.stack"
    }

    #if os(macOS)
    private var webViewIgnoredEdges: Edge.Set { .bottom }
    private var minimumWidth: CGFloat { 880 }
    private var minimumHeight: CGFloat { 600 }

    private var nativeTitlebar: some View {
        ZStack {
            appBackground

            titlebarPanelSurface(
                width: leftPanelWidth,
                visible: model.nativeChrome.leftVisible,
                hiddenOffset: -leftPanelWidth * 1.02,
                separatorAlignment: .trailing
            )
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)

            titlebarPanelSurface(
                width: rightPanelWidth,
                visible: rightWorkspaceVisible,
                hiddenOffset: rightPanelWidth * 1.02,
                separatorAlignment: .leading
            )
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .trailing)

            HStack(spacing: 0) {
                HStack(spacing: 0) {
                    NativeTrafficLights()
                        .frame(width: 82)

                    sidebarToggleButton(
                        help: model.nativeChrome.leftVisible
                            ? "Collapse Sidebar" : "Show Sidebar"
                    ) {
                        model.sendWebCommand("toggleSessions")
                    }

                    Spacer(minLength: 0)
                }
                .frame(width: leftTitlebarWidth)
                .frame(maxHeight: .infinity)

                HStack(spacing: 8) {
                    Image(systemName: "folder")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(.secondary)

                    Text(model.nativeChrome.sessionTitle)
                        .font(.system(size: 12.5, weight: .semibold))
                        .lineLimit(1)
                        .truncationMode(.tail)

                    chromeButton(systemName: "ellipsis", help: "WebUI Settings") {
                        model.sendWebCommand("openSettings")
                    }

                    Spacer(minLength: 8)
                }
                .offset(y: -1)
                .padding(.leading, 16)
                .padding(.trailing, 8)
                .frame(maxWidth: .infinity, maxHeight: .infinity)

                HStack(spacing: 4) {
                    Button {
                        model.showingSettings = true
                    } label: {
                        connectionIndicator
                            .padding(.horizontal, 5)
                            .frame(height: 26)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Connection Settings")
                    .accessibilityLabel("Connection Settings")

                    Divider()
                        .frame(height: 14)

                    chromeButton(systemName: "arrow.clockwise", help: "Reload WebUI") {
                        model.reload()
                    }
                    .disabled(model.webURL == nil)

                    chromeButton(
                        systemName: model.browserHost.isVisible ? "safari.fill" : "safari",
                        help: model.browserHost.isVisible ? "Hide Browser" : "Show Browser"
                    ) {
                        model.browserHost.toggleVisible()
                    }
                    .disabled(model.webURL == nil)
                }
                .offset(y: -1)
                .padding(.leading, 9)
                .padding(.trailing, 8)
                .frame(width: rightTitlebarWidth, alignment: .trailing)
                .frame(maxHeight: .infinity)
            }
        }
        .frame(height: autoverseTitlebarHeight)
        .clipped()
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(separatorColor)
                .frame(height: 1)
        }
    }

    private func chromeButton(
        systemName: String,
        help: String,
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
        .help(help)
        .accessibilityLabel(help)
    }

    private func titlebarPanelSurface(
        width: CGFloat,
        visible: Bool,
        hiddenOffset: CGFloat,
        separatorAlignment: Alignment
    ) -> some View {
        Rectangle()
            .fill(panelBackground)
            .overlay(alignment: separatorAlignment) {
                Rectangle()
                    .fill(separatorColor)
                    .frame(width: 1)
            }
            .frame(width: width)
            .frame(maxHeight: .infinity)
            .offset(x: visible ? 0 : hiddenOffset)
            .opacity(visible ? 1 : 0.35)
            .animation(panelAnimation, value: visible)
    }

    private func sidebarToggleButton(
        help: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 2.5, style: .continuous)
                    .strokeBorder(Color.secondary.opacity(0.82), lineWidth: 1.2)

                Rectangle()
                    .fill(Color.secondary.opacity(0.82))
                    .frame(width: 1, height: 10)
                    .offset(x: 4)
            }
            .frame(width: 13, height: 12)
            .frame(width: 26, height: 26)
            .contentShape(Rectangle())
        }
        .buttonStyle(ChromeButtonMotionStyle())
        .help(help)
        .accessibilityLabel(help)
    }

    private var leftTitlebarWidth: CGFloat {
        model.nativeChrome.leftVisible ? leftPanelWidth : 118
    }

    private var rightTitlebarWidth: CGFloat {
        rightWorkspaceVisible ? rightPanelWidth : 176
    }

    private var leftPanelWidth: CGFloat {
        model.nativeChrome.leftWidth > 0
            ? max(118, model.nativeChrome.leftWidth)
            : BrowserWorkspaceLayout.sessionsWidth
    }

    private var effectiveSessionsWidth: CGFloat {
        model.nativeChrome.leftVisible && !browserWorkspaceCollapseRequested
            ? leftPanelWidth : 0
    }

    private func requestSessionsCollapse() {
        guard model.nativeChrome.leftVisible,
              !browserWorkspaceCollapseRequested else { return }
        browserWorkspaceCollapseRequested = true
        model.sendWebCommand("collapseSessions")
    }

    private func toggleBrowserWorkspaceSize(
        currentWidth: CGFloat,
        availableWidth: CGFloat
    ) {
        browserWorkspaceDragStart = nil
        if browserWorkspaceIsMaximized {
            let preferred = browserWorkspaceRestoreWidth
                ?? BrowserWorkspaceLayout.defaultBrowserWidth(
                    available: availableWidth
                )
            browserWorkspaceWidth = BrowserWorkspaceLayout.browserWidth(
                preferred: preferred,
                available: availableWidth,
                sessionsWidth: effectiveSessionsWidth
            )
            browserWorkspaceRestoreWidth = nil
            browserWorkspaceIsMaximized = false
            return
        }

        browserWorkspaceRestoreWidth = currentWidth
        if BrowserWorkspaceLayout.shouldCollapseSessions(
            preferredBrowserWidth: availableWidth,
            available: availableWidth,
            sessionsWidth: effectiveSessionsWidth
        ) {
            requestSessionsCollapse()
        }
        browserWorkspaceWidth = BrowserWorkspaceLayout.maximumBrowserWidth(
            available: availableWidth,
            sessionsWidth: effectiveSessionsWidth
        )
        browserWorkspaceIsMaximized = true
    }

    private var rightPanelWidth: CGFloat {
        if model.browserHost.isVisible {
            return max(
                176,
                resolvedBrowserWorkspaceWidth(
                    availableWidth: browserWorkspaceAvailableWidth
                )
            )
        }
        return authoritativeRightPanelWidth
    }

    private func resolvedBrowserWorkspaceWidth(
        availableWidth: CGFloat
    ) -> CGFloat {
        let preferred = browserWorkspaceWidth
            ?? (model.browserHost.isVisible
                ? BrowserWorkspaceLayout.defaultBrowserWidth(
                    available: availableWidth
                )
                : authoritativeRightPanelWidth)
        let sessionsWidth = model.browserHost.isVisible
            && browserWorkspaceWidth == nil
            && BrowserWorkspaceLayout.shouldCollapseSessions(
                preferredBrowserWidth: preferred,
                available: availableWidth,
                sessionsWidth: effectiveSessionsWidth
            )
            ? 0 : effectiveSessionsWidth
        return BrowserWorkspaceLayout.browserWidth(
            preferred: preferred,
            available: availableWidth,
            sessionsWidth: sessionsWidth
        )
    }

    private var authoritativeRightPanelWidth: CGFloat {
        model.nativeChrome.rightWidth > 0
            ? max(176, model.nativeChrome.rightWidth)
            : 336
    }

    private var rightWorkspaceVisible: Bool {
        model.nativeChrome.rightVisible || model.browserHost.isVisible
    }

    private var panelAnimation: Animation? {
        reduceMotion ? nil : InteractionMotion.panel
    }

    /// Matches the AutoVerse Web UI theme backgrounds: dark #0e0e10 / light #f7f4ed
    private var appBackground: Color {
        if model.currentColorScheme == .dark {
            Color(red: 0x0E / 255, green: 0x0E / 255, blue: 0x10 / 255)
        } else {
            Color(red: 0xF7 / 255, green: 0xF4 / 255, blue: 0xED / 255)
        }
    }

    private var panelBackground: Color {
        if model.currentColorScheme == .dark {
            Color(red: 0x16 / 255, green: 0x16 / 255, blue: 0x18 / 255)
        } else {
            Color(red: 0xEF / 255, green: 0xEA / 255, blue: 0xE0 / 255)
        }
    }

    /// Visible hairline under the titlebar, tuned per theme.
    private var separatorColor: Color {
        if model.currentColorScheme == .dark {
            Color(red: 0x26 / 255, green: 0x26 / 255, blue: 0x2A / 255)
        } else {
            Color(red: 0xD8 / 255, green: 0xD0 / 255, blue: 0xC2 / 255)
        }
    }
    #else
    private var webViewIgnoredEdges: Edge.Set { .bottom }
    private var minimumWidth: CGFloat { 0 }
    private var minimumHeight: CGFloat { 0 }
    #endif

    private var nativeBrowserWorkspaceWidth: CGFloat? {
        #if os(macOS)
        model.browserHost.isVisible
            ? resolvedBrowserWorkspaceWidth(
                availableWidth: browserWorkspaceAvailableWidth
            )
            : nil
        #else
        nil
        #endif
    }
}

#if os(macOS)
@MainActor
struct BrowserWorkspaceResizeHandle: NSViewRepresentable {
    let onDragBegan: () -> Void
    let onDragChanged: (CGFloat) -> Void
    let onDragEnded: () -> Void
    let onDoubleClick: () -> Void

    func makeNSView(context: Context) -> BrowserWorkspaceResizeView {
        BrowserWorkspaceResizeView(
            onDragBegan: onDragBegan,
            onDragChanged: onDragChanged,
            onDragEnded: onDragEnded,
            onDoubleClick: onDoubleClick
        )
    }

    func updateNSView(
        _ nsView: BrowserWorkspaceResizeView,
        context: Context
    ) {
        nsView.updateActions(
            onDragBegan: onDragBegan,
            onDragChanged: onDragChanged,
            onDragEnded: onDragEnded,
            onDoubleClick: onDoubleClick
        )
    }
}

@MainActor
final class BrowserWorkspaceResizeView: NSView {
    private var onDragBegan: () -> Void
    private var onDragChanged: (CGFloat) -> Void
    private var onDragEnded: () -> Void
    private var onDoubleClick: () -> Void
    private var dragStartX: CGFloat?
    private var pointerIsInside = false
    private var pointerTrackingArea: NSTrackingArea?

    init(
        onDragBegan: @escaping () -> Void,
        onDragChanged: @escaping (CGFloat) -> Void,
        onDragEnded: @escaping () -> Void,
        onDoubleClick: @escaping () -> Void
    ) {
        self.onDragBegan = onDragBegan
        self.onDragChanged = onDragChanged
        self.onDragEnded = onDragEnded
        self.onDoubleClick = onDoubleClick
        super.init(frame: .zero)
        setAccessibilityElement(true)
        setAccessibilityRole(.splitter)
        setAccessibilityLabel("Resize Browser Workspace")
        setAccessibilityHelp("Drag to resize Browser; double-click to reset")
        toolTip = "Drag to resize Browser; double-click to reset"
        wantsLayer = true
        layer?.zPosition = 1_000
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func updateActions(
        onDragBegan: @escaping () -> Void,
        onDragChanged: @escaping (CGFloat) -> Void,
        onDragEnded: @escaping () -> Void,
        onDoubleClick: @escaping () -> Void
    ) {
        self.onDragBegan = onDragBegan
        self.onDragChanged = onDragChanged
        self.onDragEnded = onDragEnded
        self.onDoubleClick = onDoubleClick
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        true
    }

    override func hitTest(_ point: NSPoint) -> NSView? {
        // AppKit supplies `point` in the superview's coordinate space.
        // Checking `bounds` here silently rejects the real window coordinate
        // and lets the WKWebView underneath win the event.
        guard !isHidden, alphaValue > 0, frame.contains(point) else {
            return nil
        }
        return self
    }

    override func updateTrackingAreas() {
        if let pointerTrackingArea {
            removeTrackingArea(pointerTrackingArea)
        }
        let trackingArea = NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeInKeyWindow, .inVisibleRect],
            owner: self,
            userInfo: nil
        )
        addTrackingArea(trackingArea)
        pointerTrackingArea = trackingArea
        super.updateTrackingAreas()
    }

    override func resetCursorRects() {
        super.resetCursorRects()
        addCursorRect(bounds, cursor: .resizeLeftRight)
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let gripHeight = min(36, max(0, bounds.height - 16))
        guard gripHeight > 0 else { return }
        let gripRect = NSRect(
            x: bounds.midX - 1.5,
            y: bounds.midY - gripHeight / 2,
            width: 3,
            height: gripHeight
        )
        let color = pointerIsInside || dragStartX != nil
            ? NSColor.controlAccentColor.withAlphaComponent(0.9)
            : NSColor.separatorColor.withAlphaComponent(0.72)
        color.setFill()
        NSBezierPath(
            roundedRect: gripRect,
            xRadius: 1.5,
            yRadius: 1.5
        ).fill()
    }

    override func mouseEntered(with event: NSEvent) {
        pointerIsInside = true
        needsDisplay = true
    }

    override func mouseExited(with event: NSEvent) {
        pointerIsInside = false
        needsDisplay = true
    }

    override func mouseDown(with event: NSEvent) {
        guard event.clickCount != 2 else {
            onDoubleClick()
            return
        }
        dragStartX = event.locationInWindow.x
        needsDisplay = true
        onDragBegan()
    }

    override func mouseDragged(with event: NSEvent) {
        guard let dragStartX else { return }
        onDragChanged(event.locationInWindow.x - dragStartX)
    }

    override func mouseUp(with event: NSEvent) {
        guard dragStartX != nil else { return }
        dragStartX = nil
        needsDisplay = true
        onDragEnded()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        needsDisplay = true
    }
}
#endif
