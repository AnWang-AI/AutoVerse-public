"""Static interaction-performance contracts for the WebUI adapter."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
APP_JS = ROOT / "src/autoverse/frontends/web/static/app.js"
INDEX_HTML = ROOT / "src/autoverse/frontends/web/static/index.html"
MOTION_CSS = ROOT / "src/autoverse/frontends/web/static/motion-tokens.css"
MOTION_SWIFT = ROOT / "apple/Sources/Shared/MotionTokenValues.swift"
CONTENT_VIEW = ROOT / "apple/Sources/Shared/ContentView.swift"
WEB_VIEW = ROOT / "apple/Sources/Shared/WebView.swift"


def _function(source: str, name: str, next_marker: str) -> str:
    return source.split(f"function {name}", 1)[1].split(next_marker, 1)[0]


def test_session_switch_acknowledges_before_waiting_and_keeps_old_stream_alive() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    switch = _function(app_js, "switchSession", "async function newSession")

    pending = switch.index('_sessionSwitchPendingId = sid;')
    request = switch.index('await api("POST", "/api/sessions"')
    disconnect = switch.index("disconnectSessionStream();")

    assert pending < request < disconnect
    assert '$("sessionList").setAttribute("aria-busy", "true")' in switch
    assert "if (!res.__ok || !res.session_id)" in switch
    assert 'performance.mark("autoverse:session-switch-input",' in switch
    assert 'performance.mark("autoverse:session-switch-request-start",' in switch
    assert "monitorInteractionFrameGaps(actionId);" in switch
    assert '"autoverse:session-switch-input-to-ack"' in switch


def test_session_inventory_renders_locally_and_poll_reuses_its_response() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    rail_poll = _function(app_js, "startRailPoll", "function stopRailPoll")
    click_handler = app_js.split('$("sessionList").addEventListener("click"', 1)[1]
    click_handler = click_handler.split('$("sessionList").addEventListener("keydown"', 1)[0]

    assert "function renderSessions(data, activeId" in app_js
    assert "function _syncSessionChildren(ul, desired)" in app_js
    assert "renderSessions(data, STATE.sid);" in rail_poll
    assert "loadSessions(STATE.sid)" not in rail_poll
    assert "renderSessions(_sessionInventory, STATE.sid)" in click_handler
    assert "loadSessions(" not in click_handler
    assert "const SESSION_WINDOW_LIMIT = 120;" in app_js
    assert "_syncSessionWindow(ul, desired" in app_js
    assert 'type="button" class="session-hit"' in app_js


def test_settings_shell_opens_before_parallel_data_loading() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    settings = _function(app_js, "openSettings", "const MODELS_CACHE")

    shown = settings.index("settingsMask.hidden = false;")
    loaded = settings.index("await Promise.allSettled")

    assert shown < loaded
    assert "loadModelsOnce()," in settings
    assert "loadTiers(settingsSid, seq)," in settings
    assert "loadSecrets(settingsSid, seq)," in settings
    assert 'settingsMask.setAttribute("aria-busy", "true")' in settings


def test_native_browser_workspace_promotes_full_window_conversation_dialogs() -> None:
    index_html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")
    content_view = CONTENT_VIEW.read_text(encoding="utf-8")
    web_view = WEB_VIEW.read_text(encoding="utf-8")

    assert ".native-macos.native-browser-workspace .conversation-overlay" not in index_html
    assert index_html.count('class="modal-mask') == index_html.count(
        'class="modal-mask conversation-overlay"'
    )
    assert 'class="image-lightbox conversation-overlay"' in index_html
    dynamic_dialog_roots = re.findall(
        r'<div class="([^"]+)" role="dialog" aria-modal="true"', app_js
    )
    assert dynamic_dialog_roots
    assert all("conversation-overlay" in classes for classes in dynamic_dialog_roots)
    assert "app.appendChild(overlay);" in app_js
    assert "prompt(" not in app_js
    assert "confirm(" not in app_js
    assert "alert(" not in app_js
    assert 'name: "autoverseConversationOverlay"' in web_view
    assert "document.querySelectorAll('.conversation-overlay')" in web_view
    assert "attributeFilter: ['hidden']" in web_view
    assert "'native-conversation-overlay-active', visible" in web_view
    assert "view.underPageBackgroundColor = .clear" in web_view
    assert 'view.setValue(false, forKey: "drawsBackground")' in web_view
    assert "onConversationOverlayChange(value.boolValue)" in web_view
    assert ".zIndex(conversationOverlayVisible ? 2 : 0)" in content_view
    assert "model.browserHost.isVisible && !conversationOverlayVisible" in content_view
    assert "!model.browserHost.isVisible || conversationOverlayVisible" in content_view
    assert (
        ".native-macos.native-browser-workspace.native-conversation-overlay-active .sidebar"
        in index_html
    )
    assert "visibility: hidden;" in index_html
    assert "background: transparent !important;" in index_html
    assert "width: min(680px, 100%); min-width: 0; max-width: 100%;" in index_html
    assert ".interaction-dialog { width: min(520px, 100%); }" in index_html
    assert (
        ".image-lightbox-panel { position: relative; display: flex; "
        "flex-direction: column; align-items: center;\n"
        "  gap: .75rem; width: 100%; min-width: 0; max-width: 100%;"
        in index_html
    )
    assert ".image-lightbox-panel img { max-width: 100%;" in index_html
    assert (
        ".image-lightbox-caption { width: 100%; min-width: 0; max-width: 100%;"
        in index_html
    )


def test_panel_motion_is_compositor_only_and_native_bridge_is_settle_coalesced() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    index_html = INDEX_HTML.read_text(encoding="utf-8")
    motion_css = MOTION_CSS.read_text(encoding="utf-8")
    motion_swift = MOTION_SWIFT.read_text(encoding="utf-8")
    animate = _function(app_js, "animatePanelClass", '$("btnRail")')
    bridge = _function(app_js, "setupNativeChromeBridge", "window.autoverseNativeAction")
    post = _function(app_js, "postNativeChromeState", "function scheduleNativeChromeSettled")

    assert "transition: grid-template-columns" not in index_html
    assert "app.classList.toggle(className, enabled);" in animate
    assert animate.index("app.classList.toggle(className, enabled);") < animate.index(
        "setRightRailWidth(currentRightRailWidth(), false);"
    )
    assert "panel.animate(frames, options);" in animate
    assert "ghost.animate([...frames].reverse(), options)" in animate
    assert "new ResizeObserver(scheduleNativeChromeSettled)" in bridge
    assert "new ResizeObserver(postNativeChromeState)" not in bridge
    assert 'if (phase === "start")' in post
    assert post.index('if (phase === "start")') < post.index("requestAnimationFrame")
    assert "emitNativeChromeState(handler, phase, action);" in post
    assert "nativeBrowserWorkspaceState.priorRailVisible === true" in app_js
    assert "nativeBrowserWorkspaceState.priorRailWidth" in app_js
    assert "--motion-panel: 200ms" in motion_css
    assert "--motion-activity: 1100ms" in motion_css
    assert "static let activityDuration = 1.100" in motion_swift
    assert "static let activityCurve = (0.37, 0.0, 0.63, 1.0)" in motion_swift
    assert 'href="/static/motion-tokens.css"' in index_html
    assert '@media (prefers-reduced-motion: reduce)' in index_html


def test_native_browser_workspace_reuses_the_desktop_right_rail() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    index_html = INDEX_HTML.read_text(encoding="utf-8")
    content_view = (
        ROOT / "apple/Sources/Shared/ContentView.swift"
    ).read_text(encoding="utf-8")
    web_view = (ROOT / "apple/Sources/Shared/WebView.swift").read_text(
        encoding="utf-8"
    )
    browser_host = (
        ROOT / "apple/Sources/Shared/BrowserHost.swift"
    ).read_text(encoding="utf-8")
    app_model = (
        ROOT / "apple/Sources/Shared/AppModel.swift"
    ).read_text(encoding="utf-8")
    drag_changed = content_view.split(
        "private func updateBrowserWorkspaceDrag", 1
    )[1].split("private func endBrowserWorkspaceDrag", 1)[0]
    resize_view = content_view.split(
        "final class BrowserWorkspaceResizeView", 1
    )[1].split("#endif", 1)[0]
    rail_resize = _function(app_js, "setupRailResize", "function closeMobilePanels")
    visibility_change = content_view.split(
        ".onChange(of: model.browserHost.isVisible)", 1
    )[1].split(".onChange(of: model.nativeChrome.leftVisible)", 1)[0]

    assert "window.autoverseSetNativeBrowserWorkspace" in app_js
    assert 'animatePanelClass("show-rail", true)' in app_js
    assert 'setRightRailWidth(restoreWidth, false)' in app_js
    assert 'animatePanelClass("show-rail", restoreVisible)' in app_js
    assert "priorRailVisible" in app_js
    assert "priorRailWidth" in app_js
    assert "const underlyingRightWidth = nativeBrowserWorkspaceState.active" in app_js
    assert "if (!nativeBrowserWorkspaceState.active)" in app_js
    assert "function toggleRightRail()" in app_js
    assert "if (nativeBrowserWorkspaceState.active) return" in app_js
    assert 'action === "collapseSessions"' in app_js
    assert "nativeBrowserWorkspaceState.autoCollapsedSessions" in app_js
    assert "const restoreSessions = nativeBrowserWorkspaceState.autoCollapsedSessions" in app_js
    assert 'if (restoreSessions && app.classList.contains("hide-sessions"))' in app_js
    assert "window.autoversePreviewNativeBrowserWorkspaceWidth" in app_js
    assert "setRightRailWidth(width, false)" in app_js
    assert "const RAIL_COMFORTABLE_CENTER_WIDTH = 520;" in app_js
    assert "const RAIL_MINIMUM_CENTER_WIDTH = 520;" in app_js
    assert "maxFixed = 760" not in app_js
    assert rail_resize.index("setRightRailWidth(preferred") < rail_resize.index(
        'animatePanelClass("hide-sessions", true)'
    )
    assert "native-macos.native-browser-workspace .mobile-bar" not in index_html
    assert "native-macos.native-browser-workspace .composer" not in index_html
    assert ".native-macos .edge-tool { display: none !important; }" in index_html
    assert "ZStack(alignment: .trailing)" in content_view
    assert "workspaceIsMaximized: browserWorkspaceIsMaximized" in content_view
    assert "onToggleWorkspaceSize:" in content_view
    assert "resolvedBrowserWorkspaceWidth(" in content_view
    assert "browserWorkspaceAvailableWidth" in content_view
    assert '@AppStorage("browserWorkspaceWidth")' not in content_view
    assert "browserWidth * 1.02" in content_view
    assert ".opacity(model.browserHost.isVisible ? 1 : 0.35)" in content_view
    assert "model.browserHost.isVisible && !conversationOverlayVisible" in content_view
    assert "InteractionMotion.panel" in content_view
    assert "titlebarPanelSurface(" in content_view
    assert "hiddenOffset: -leftPanelWidth * 1.02" in content_view
    assert "hiddenOffset: rightPanelWidth * 1.02" in content_view
    assert "@Environment(\\.accessibilityReduceMotion)" in content_view
    assert "static let dividerWidth: CGFloat = 40" in content_view
    assert "BrowserWorkspaceResizeHandle(" in content_view
    assert ".frame(maxWidth: .infinity, maxHeight: .infinity)" in content_view
    assert "NSViewRepresentable" in content_view
    assert "override func acceptsFirstMouse" in resize_view
    assert "override func hitTest" in resize_view
    assert "frame.contains(point)" in resize_view
    assert "override func updateTrackingAreas" in resize_view
    assert "override func mouseEntered" in resize_view
    assert "override func mouseDragged" in resize_view
    assert "addCursorRect(bounds, cursor: .resizeLeftRight)" in resize_view
    assert "x: -rightPanelWidth" in content_view
    assert "+ BrowserWorkspaceLayout.dividerWidth / 2" in content_view
    assert ".zIndex(3)" in content_view
    assert ".allowsHitTesting(browserWorkspaceDragStart == nil)" in content_view
    assert "browserWorkspaceWidth = BrowserWorkspaceLayout.browserWidth" in drag_changed
    assert "preferred > start" in drag_changed
    assert "requestSessionsCollapse()" in drag_changed
    assert "model.setRightRailWidth" not in drag_changed
    assert "private func endBrowserWorkspaceDrag" in content_view
    assert "private func resetBrowserWorkspaceWidth" in content_view
    assert "private func browserWorkspaceDivider" not in content_view
    assert "if model.browserHost.isVisible" in content_view
    assert "browserWorkspaceWidth: nativeBrowserWorkspaceWidth" in content_view
    assert "BrowserWorkspaceLayout.defaultBrowserWidth" in visibility_change
    assert "browserWorkspaceWidth = nil" in visibility_change
    assert "BrowserWorkspaceLayout.shouldCollapseSessions" in visibility_change
    assert "requestSessionsCollapse()" in visibility_change
    assert "browserWorkspaceWidthScript(browserWorkspaceWidth)" in web_view
    assert "autoversePreviewNativeBrowserWorkspaceWidth" in web_view
    assert "withAnimation(InteractionMotion.panel)" not in browser_host
    assert "nativeChrome.leftVisible.toggle()" not in app_model
    assert "let rightRailWidth: Double?" not in app_model
    assert "let collapseSessions: Bool?" not in app_model
    assert "withAnimation(InteractionMotion" not in app_model
    assert ".background(browserSurface)" in browser_host
    browser_control_overlay = browser_host.split(
        ".background(browserSurface)", 1
    )[1].split(".overlay(alignment: .top)", 1)[0]
    assert ".shadow(" not in browser_control_overlay
    assert ".textFieldStyle(.roundedBorder)" not in browser_host
    assert ".textFieldStyle(.plain)" in browser_host
    assert "@FocusState private var addressIsFocused" in browser_host
    assert "addressIsFocused || agentEmphasizesAddress" in browser_host
    assert "? browserAccent" in browser_host
    assert ": browserControlBorder" in browser_host
    assert ".fill(browserHairlineColor)" in browser_host
    assert ".background(browserPanelSurface)" in browser_host
    assert "geometry.size.width < 390" in browser_host
    assert 'help: workspaceIsMaximized' in browser_host
    assert '"Maximize Browser Width"' in browser_host
    assert '"Restore Browser Width"' in browser_host
    assert ".buttonStyle(ChromeButtonMotionStyle())" in browser_host
    assert "ScrollViewReader" in browser_host
    assert "proxy.scrollTo(activePageID, anchor: .trailing)" in browser_host
    assert "NativeBrowserContainerView" in browser_host
    assert "container.display(webView)" in browser_host


def test_native_browser_takeover_resumes_with_the_next_user_message() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    content_view = (
        ROOT / "apple/Sources/Shared/ContentView.swift"
    ).read_text(encoding="utf-8")
    web_view = (ROOT / "apple/Sources/Shared/WebView.swift").read_text(
        encoding="utf-8"
    )
    browser_host = (
        ROOT / "apple/Sources/Shared/BrowserHost.swift"
    ).read_text(encoding="utf-8")
    submit = _function(app_js, "submitText", "function stopTurn")

    assert 'name: "autoverseAgentResume"' in web_view
    assert "onAgentResume: onAgentResume" in web_view
    assert "model.browserHost.resumeAgentControl()" in content_view
    assert "await notifyNativeAgentResume();" in submit
    assert submit.index("await notifyNativeAgentResume();") < submit.index(
        'await api("POST", `/api/sessions/'
    )
    assert "Button(action: controller.takeControl)" in browser_host
    assert 'Label("Take Over", systemImage: "hand.raised")' in browser_host
    assert "Give to Agent" not in browser_host
    assert (
        'message: "Browser control is with the user. Wait for their next message."'
        in browser_host
    )


def test_snapshot_history_is_bounded_and_images_are_lazy_resources() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")

    assert "STATE.historyCursor = snap.has_more" in app_js
    assert "async function loadOlderHistory()" in app_js
    assert "/history?before=${enc(cursor)}" in app_js
    assert "if (img.resource_url) return String(img.resource_url);" in app_js
    assert "const virtualTurnChildren = new WeakMap();" in app_js
    assert "performance.now() - sliceStarted > 8" in app_js


def test_turn_history_counts_block_tool_cards() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    counter = _function(app_js, "toolCallCountForTurn", "function collapseBetween")

    assert "details.card.tool-block" in counter


def test_session_rows_separate_tree_guide_from_inset_state_surface() -> None:
    index_html = INDEX_HTML.read_text(encoding="utf-8")

    assert ".sessions li.sess-row .session-hit::before" in index_html
    assert "left: 1.2ch; right: 0; top: 2px; bottom: 2px" in index_html
    assert (
        "background: color-mix(in srgb, var(--bg-element) 58%, transparent)"
        in index_html
    )
    assert ".sessions li.sess-row.in-group { padding-left: 3ch; }" in index_html
    assert "left: 1.4ch; top: 0; bottom: 0" in index_html
    assert "border-left: 2px solid var(--accent)" in index_html


def test_large_turn_defers_only_execution_detail_with_user_facing_copy() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    builder = _function(app_js, "buildBlock", "async function loadOversizeBlock")
    loader = _function(app_js, "loadDeferredExecution", "// Lock a decision card")

    assert 'k === "deferred_execution"' in builder
    assert '<span class="card-title">执行过程</span>' in builder
    assert 'loadDeferredExecution(e, String(b.detail_url || ""))' in builder
    assert 'data.blocks.forEach(({ id, block }) =>' in loader
    assert 'blocksRoot.querySelector(`[data-bid="${cssEsc(id)}"]`)' in loader
    assert "展开完整内容" not in app_js
    assert "Turn contains" not in app_js
