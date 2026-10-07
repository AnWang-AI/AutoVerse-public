"""Static regression checks for WebUI composer button semantics."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
APP_JS = ROOT / "src/autoverse/frontends/web/static/app.js"
INDEX_HTML = ROOT / "src/autoverse/frontends/web/static/index.html"


def test_desktop_composer_matches_log_horizontal_gutters() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert ".log { flex: 1; min-height: 0; overflow-y: auto; padding: .8rem 2ch 0 var(--rail);" in html
    assert ".composer { position: relative; margin: 0 2ch 0 3ch;" in html
    assert "margin: 0;\n    width: calc(100% - 40px);" in html


def test_composer_meta_omits_turn_interaction_counter_and_model() -> None:
    js = APP_JS.read_text(encoding="utf-8")

    assert "本轮第" not in js
    assert "次交互" not in js
    assert '<span>${esc(m.model || "")}</span>' not in js
    assert '<span>${esc(m.cwd_short || m.cwd || "")}</span>' in js
    assert '<span class="pm-hint"> · Tab 切换模式</span>' in js


def test_queue_button_titles_match_idle_and_running_behaviour() -> None:
    js = APP_JS.read_text(encoding="utf-8")

    assert 'const hasPayload = !!$("prompt").value.trim() || IMAGE_ATTACHMENTS.length > 0;' in js
    assert 'queue.disabled = !hasPayload;' in js
    assert 'queue.title = hasPayload ? "排队发送（本轮结束后自动发送）" : "输入内容后排队发送";' in js
    assert 'queue.title = hasPayload ? "聚焦输入框" : "打开指令菜单";' in js


def test_idle_queue_button_opens_commands_only_when_no_payload() -> None:
    js = APP_JS.read_text(encoding="utf-8")

    assert '$("btnQueue").addEventListener("click", () => {' in js
    assert "if (!hasPayload) {" in js
    assert 'ta.value = "/";' in js
    assert "ta.focus();" in js


def test_sent_turn_actions_rewind_context_and_workspace() -> None:
    js = APP_JS.read_text(encoding="utf-8")

    assert 'data-turn-action="edit"' in js
    assert 'data-turn-action="undo"' in js
    assert "/turn/rewind`" in js
    assert "rewind_id: rewindId" in js
    assert 'ta.value = out.text || "";' in js
    assert "resetFileTree();" in js


def test_edit_sent_turn_never_overwrites_a_composer_draft() -> None:
    js = APP_JS.read_text(encoding="utf-8")

    assert 'action === "edit" && (ta.value.trim() || IMAGE_ATTACHMENTS.length)' in js
    assert "请先发送或清空当前草稿" in js


def test_sent_turn_actions_are_visible_without_hover() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")

    assert ".user-actions { position: absolute;" in html
    assert ".user-action-menu { position: absolute;" in html
    assert "right: calc(100% + 6px);" in html
    assert "visibility: hidden; opacity: 0;" in html
    assert ".user-actions.open .user-action-menu" in html
    assert 'data-turn-menu aria-label="指令操作"' in js
    assert "closeTurnActionMenus()" in js


def test_file_tree_keyboard_navigation_covers_files_and_directories() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")

    assert 'id="fileTree" role="tree" aria-label="工作区文件" tabindex="0"' in html
    assert 'role="treeitem"' in js
    assert 'data-depth="${depth}"' in js
    assert 'aria-level="${depth + 1}"' in js
    assert 'aria-selected="${selected ? "true" : "false"}"' in js
    assert '["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Enter", "Home", "End", "Escape"]' in js
    assert "moveFileTreeSelection(root, e.key === \"ArrowDown\" ? 1 : -1);" in js
    assert "handleFileTreeHorizontalKey(root, row, e.key);" in js
    assert "moveFileTreeToParent(root, row);" in js
    assert 'selectFileTreeRow(root, e.key === "Home" ? rows[0] : rows[rows.length - 1]);' in js


def test_file_tree_selection_is_distinct_from_file_preview() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")

    assert 'selectedPath: ""' in js
    assert 'previewPath: ""' in js
    assert 'selected ? " selected"' in js
    assert 'previewing ? " previewing"' in js
    assert ".file-row.selected" in html
    assert ".file-row.previewing" in html
    assert "if (path === FILE_STATE.previewPath) return;" in js


def test_file_preview_ignores_stale_keyboard_navigation_responses() -> None:
    js = APP_JS.read_text(encoding="utf-8")

    assert "const requestId = ++filePreviewRequest;" in js
    assert "requestId !== filePreviewRequest || path !== FILE_STATE.previewPath" in js


def test_conversation_and_sidebar_files_share_accessible_preview_dispatch() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")

    assert "function looksLikePreviewPath(value)" in js
    assert "Makefile|Dockerfile|Justfile|Procfile|Rakefile|Gemfile" in js
    assert "function inlineCodeHtml(value, basePath)" in js
    assert 'class="inline-file-link" data-open-file=' in js
    assert 'target.startsWith("/") || target.startsWith("~/")' in js
    assert 'document.addEventListener("click", (e) => {' in js
    assert 'const file = e.target.closest("[data-open-file]");' in js
    assert 'void openFilePreview(file.dataset.openFile);' in js
    assert 'type="button" class="file-link" data-open-file=' in js
    assert ".file-link:focus-visible, .md a[data-open-file]:focus-visible" in html


def test_native_browser_yields_to_requested_file_preview() -> None:
    js = APP_JS.read_text(encoding="utf-8")
    web_view = (ROOT / "apple/Sources/Shared/WebView.swift").read_text(
        encoding="utf-8"
    )
    content_view = (ROOT / "apple/Sources/Shared/ContentView.swift").read_text(
        encoding="utf-8"
    )

    assert "function requestNativeFilePreviewSurface()" in js
    assert "messageHandlers?.autoverseFilePreview" in js
    assert 'name: "autoverseFilePreview"' in web_view
    assert 'case "autoverseFilePreview":' in web_view
    assert "self.onOpenFilePreview()" in web_view
    assert "if model.browserHost.isVisible" in content_view
    assert "model.browserHost.toggleVisible()" in content_view


def test_conversation_web_links_open_native_browser_pages() -> None:
    js = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    web_view = (ROOT / "apple/Sources/Shared/WebView.swift").read_text(
        encoding="utf-8"
    )
    content_view = (ROOT / "apple/Sources/Shared/ContentView.swift").read_text(
        encoding="utf-8"
    )
    browser_host = (ROOT / "apple/Sources/Shared/BrowserHost.swift").read_text(
        encoding="utf-8"
    )

    assert "/\\bhttps?:\\/\\/[^\\s<>\"']+/gi" in js
    assert "function webLinkHtml(url, label)" in js
    assert 'if (/^https?:\\/\\/[^\\s]+$/i.test(String(value || "").trim()))' in js
    assert 'class="conversation-web-link"' in js
    assert ".md a.conversation-web-link" in html
    assert ".md a.conversation-web-link code { background: transparent; padding: 0;" in html
    assert "white-space: normal; overflow-wrap: anywhere; word-break: normal;" in html
    assert ".md ul li, .md ol li { position: relative; }" in html
    assert '.md ul li::before { content: "•";' in html
    assert ".md ol li { counter-increment: li; padding-left: 3ch; }" in html
    assert "function requestNativeBrowserPage(rawURL)" in js
    assert "messageHandlers?.autoverseBrowserLink" in js
    assert 'e.target.closest(".log .md a.conversation-web-link[href]")' in js
    assert 'name: "autoverseBrowserLink"' in web_view
    assert 'case "autoverseBrowserLink":' in web_view
    assert "self.onOpenBrowserLink(url)" in web_view
    assert "model.browserHost.openPageFromChat(url)" in content_view
    assert "func openPageFromChat(_ url: URL) -> Bool" in browser_host
    assert "guard BrowserURLPolicy.isAllowed(url)" in browser_host
    assert "let page = makePage()" in browser_host
