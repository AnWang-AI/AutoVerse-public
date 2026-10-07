from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_webui_defaults_to_light_appearance_before_and_after_bootstrap() -> None:
    index_html = (
        ROOT / "src/autoverse/frontends/web/static/index.html"
    ).read_text(encoding="utf-8")
    app_js = (
        ROOT / "src/autoverse/frontends/web/static/app.js"
    ).read_text(encoding="utf-8")

    assert 'localStorage.getItem("autoverse-appearance") || "light"' in index_html
    assert 'const DEFAULT_APPEARANCE = "light";' in app_js
    assert "localStorage.getItem(APPEARANCE_KEY) || DEFAULT_APPEARANCE" in app_js


def test_webui_scrollbars_are_transient_without_changing_layout_width() -> None:
    index_html = (
        ROOT / "src/autoverse/frontends/web/static/index.html"
    ).read_text(encoding="utf-8")
    app_js = (
        ROOT / "src/autoverse/frontends/web/static/app.js"
    ).read_text(encoding="utf-8")

    assert "::-webkit-scrollbar { width: 8px; height: 8px; }" in index_html
    assert ".scrollbar-active::-webkit-scrollbar-thumb" in index_html
    assert "background-color: transparent;" in index_html
    assert "@media (forced-colors: active)" in index_html
    assert "const scrollbarHideTimers = new WeakMap();" in app_js
    assert 'target.classList.add("scrollbar-active")' in app_js
    assert 'target.classList.remove("scrollbar-active")' in app_js
    assert "}, 900));" in app_js


def test_webui_sidebar_tabs_are_centered_flat_buttons() -> None:
    index_html = (
        ROOT / "src/autoverse/frontends/web/static/index.html"
    ).read_text(encoding="utf-8")

    header_rule = index_html.split(".sessions .hd {", 1)[1].split("}", 1)[0]
    tabs_rule = index_html.split(".session-tabs {", 1)[1].split("}", 1)[0]
    tab_button_rule = index_html.split(".session-tabs button {", 1)[1].split("}", 1)[0]
    active_rule = index_html.split(".session-tabs button.active {", 1)[1].split("}", 1)[0]

    assert "border-bottom" not in header_rule
    assert "padding: 0 1.2ch 0 1.6ch" in header_rule
    assert "gap: .5ch" in tabs_rule
    assert "margin: 0 1.2ch" in tabs_rule
    assert "padding: 0 0 .35rem" in tabs_rule
    assert "border:" not in tabs_rule
    assert "border-radius" not in tabs_rule
    assert "align-items: center" in tab_button_rule
    assert "justify-content: center" in tab_button_rule
    assert "border: 1px solid" in tab_button_rule
    assert "border-radius" not in tab_button_rule
    assert 'font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text"' in tab_button_rule
    assert "background: color-mix(in srgb, var(--accent) 8%, var(--bg-element))" in active_rule
    assert "border-color: color-mix(in srgb, var(--accent) 34%, var(--border-subtle))" in active_rule
    assert "font-weight: 600" in active_rule
    assert "box-shadow: inset 0 -2px 0 var(--accent)" in active_rule
    assert ".session-tabs button:active { background:" in index_html


def test_macos_shell_defaults_to_light_and_syncs_native_appearance() -> None:
    app_model = (ROOT / "apple/Sources/Shared/AppModel.swift").read_text(encoding="utf-8")
    apple_app = (ROOT / "apple/Sources/App/AutoVerseAppleApp.swift").read_text(
        encoding="utf-8"
    )
    content_view = (ROOT / "apple/Sources/Shared/ContentView.swift").read_text(
        encoding="utf-8"
    )

    assert "currentColorScheme: ColorScheme? = .light" in app_model
    assert "isDark ? .darkAqua : .aqua" in apple_app
    assert "window.appearance = NSAppearance(named: appearanceName)" in apple_app
    assert "ToolbarItemGroup" not in content_view
    assert 'systemImage: "network"' not in content_view
    assert '.help("Connection Settings")' in content_view
    assert 'help: "Reload WebUI"' in content_view


def test_macos_shell_uses_session_aware_three_region_titlebar() -> None:
    app_model = (ROOT / "apple/Sources/Shared/AppModel.swift").read_text(encoding="utf-8")
    apple_app = (ROOT / "apple/Sources/App/AutoVerseAppleApp.swift").read_text(
        encoding="utf-8"
    )
    content_view = (ROOT / "apple/Sources/Shared/ContentView.swift").read_text(
        encoding="utf-8"
    )
    web_view = (ROOT / "apple/Sources/Shared/WebView.swift").read_text(encoding="utf-8")
    index_html = (
        ROOT / "src/autoverse/frontends/web/static/index.html"
    ).read_text(encoding="utf-8")
    app_js = (
        ROOT / "src/autoverse/frontends/web/static/app.js"
    ).read_text(encoding="utf-8")

    assert "struct NativeChromeState: Equatable" in app_model
    assert "private var nativeTitlebar: some View" in content_view
    assert "private func sidebarToggleButton(" in content_view
    assert 'systemName: "sidebar.left"' not in content_view
    assert "RoundedRectangle(cornerRadius: 2.5, style: .continuous)" in content_view
    assert ".frame(width: 13, height: 12)" in content_view
    assert ".frame(width: 82)" in content_view
    assert ".offset(y: 4.5)" not in content_view
    assert ".frame(height: autoverseTitlebarHeight)" in content_view
    assert 'Image(systemName: "folder")' in content_view
    assert ".windowStyle(.hiddenTitleBar)" in apple_app
    assert "window.styleMask.insert(.fullSizeContentView)" in apple_app
    assert "let autoverseTitlebarHeight: CGFloat = 40" in apple_app
    assert "trafficLightVerticalOffset" not in apple_app
    assert "NativeTrafficLights()" in content_view
    assert "button.centerYAnchor.constraint(equalTo: centerYAnchor)" in apple_app
    assert "let button = TrafficLightButton(kind: kind)" in apple_app
    assert "button.widthAnchor.constraint(equalToConstant: 14)" in apple_app
    assert "window.toggleFullScreen(sender)" in apple_app
    assert "scheduleTrafficLightPositioning" not in apple_app
    assert "positionTrafficLights" not in apple_app
    assert 'name: "autoverseNativeChrome"' in web_view
    assert 'evaluateJavaScript("window.autoverseNativeSync?.()")' in web_view
    assert "titlebarPanelSurface(" in content_view
    assert "visible: model.nativeChrome.leftVisible" in content_view
    assert "visible: rightWorkspaceVisible" in content_view
    assert ".frame(width: leftTitlebarWidth)" in content_view
    assert ".frame(maxWidth: .infinity, maxHeight: .infinity)" in content_view
    assert ".frame(width: rightTitlebarWidth, alignment: .trailing)" in content_view
    assert ".animation(panelAnimation, value: visible)" in content_view
    assert "window.autoverseNativeAction" in app_js
    assert "window.autoverseNativeSync = postNativeChromeState" in app_js
    assert "function updateSessionTitle(title)" in app_js
    assert "async function loadSessions(activeId, listedActiveId = activeId)" in app_js
    assert "await loadSessions(realId, sid);" in app_js
    assert 'updateSessionTitle(initial.title_display || initial.title || "AutoVerse")' in app_js
    assert 'sessionTitle: activeTitle || STATE.sessionTitle || "AutoVerse"' in app_js
    assert '<span class="brand">AutoVerse</span>' in index_html
    assert 'class="banner"' not in index_html
    assert ".native-macos .sessions .hd #btnSessions { display: none; }" in index_html
    assert "#blocks > .user-echo:first-child { margin-top: .55rem; }" in index_html
    assert "background-color: #efeae0" in index_html
