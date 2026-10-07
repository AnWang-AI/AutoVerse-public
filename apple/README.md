# AutoVerse Apple

SwiftUI macOS + iOS shell for the AutoVerse WebUI and its visible browser host.

Architecture, shell boundaries, and responsive frontend behavior are documented
in [`docs/design/frontend.md`](../docs/design/frontend.md) and
[`docs/guides/using-webui.md`](../docs/guides/using-webui.md).

## macOS development

The app looks for `autoverse-web` on `PATH`, then falls back to the repository
virtual environment's Python module entry point. From this repository:

```bash
cd /Users/alden/CCWorkSpace/AutoVerse
source .venv/bin/activate
pip install -e '.[web]'

cd apple
swift run AutoVerseApple
```

Alternatively set an explicit executable:

```bash
AUTOVERSE_WEB_EXECUTABLE=/Users/alden/CCWorkSpace/AutoVerse/.venv/bin/autoverse-web \
  swift run AutoVerseApple
```

The default working directory is the parent of the `apple` directory. It can be
changed from the app's Connection settings.

The WebUI defaults to the light appearance when no preference has been saved.
The toolbar appearance button persists an explicit light or dark choice, and
the macOS shell mirrors that choice into the native titlebar so its controls
retain the correct contrast. The titlebar is split into the same left, content,
and right regions as the WebUI: the left button toggles the session rail, the
center shows the active session title and opens WebUI settings, and the right
region contains connection status plus reload. The WebView sends only derived
presentation state (title and rail geometry) to this shell; session and agent
state remain owned by the WebUI/runtime.

The Browser button opens a separate WKWebView driven by the same browser tools
used by headless sessions. It uses the operating system WebKit engine and a
stable named `WKWebsiteDataStore`, isolated from the chat WebView, so website
cookies and sign-ins persist without exposing the host command channel to page
scripts. The address field opens domains and explicit HTTP(S) URLs directly;
other text uses Google Search with a properly encoded `q` query. Its user agent
adds version-matched Safari product tokens plus an
`AutoVerse` token because some sites otherwise send WKWebView a legacy
compatibility page; the rendering engine remains the installed system WebKit.
The native browser toolbar follows the selected AutoVerse light or dark palette,
including its hairline separators, address-field focus ring, and press motion.
Its taller rounded address field, roomier text inset, and readable type use
desktop-browser proportions without falling back to a platform-default field.
A width action immediately to the right of the address field
maximizes the Browser workspace or restores its previous Browser width. At
narrow panel widths the takeover action collapses to an icon without reducing
the address field to an unusable size.
The tab strip creates, switches, and closes independent WKWebViews with stable
page ids. Tabs fill the strip edge to edge as larger, gapless segments instead
of inset pills. Hairline separators divide adjacent tabs; the selected tab uses
a subtle accent surface and bottom indicator, while the full-height new-page
action remains fixed. These controls share the surrounding native chrome's
panel, accent, and motion tokens. The active tab scrolls into view when the
strip overflows. Switching swaps
preserved WKWebViews inside one stable native container instead of rebuilding
them, so history, form values, script state, and page identity do not drift
during rapid changes. Script-driven title changes are synchronized back to the
tab model. Links that request a new window become active tabs. Explicit Markdown
HTTP(S) links, bare URLs, and complete inline-code URLs in chat are rendered as
standard links; a dedicated
chat-WebView message routes their clicks through `BrowserURLPolicy`, creates a
new active Browser tab, and leaves the previous tab intact. The bridge is local
to the Apple shell; a standalone WebUI keeps normal `_blank` behavior, and
non-Web or blocked addresses are not opened through it. Hiding the pane
preserves every tab and its navigation state; a read-only page listing does not
reopen it, while the next real page action does. Closing the final tab hides the
pane, and reopening creates a fresh blank page. User tab changes take control
before changing the active page so an in-flight agent command cannot drift to a
different target.
Each native page result reports its monotonic navigation revision, live URL,
title, active state, and loading state. At the next chat turn the Python proxy
compares a compact snapshot of all pages, not only the active id, so same-URL
reloads, same-tab navigation, redirects, late load/title changes, new tabs, and
closed tabs are visible to the agent. WebKit's cancelled
navigation callback is ignored when a newer navigation supersedes it; a page
that is already loading successfully is not misreported as a failed page.
Clicks, Enter submissions, selections, and keyboard actions also allow a short
WebKit commit window. If they start a navigation, the command returns the new
page snapshot after DOM readiness; a page still loading after the bounded wait
returns `is_loading=true` instead of turning the successful interaction into a
false failure.
After a page action, the host's internal `observe` command captures bounded
semantic roles, controls, and visible text plus final page state within one
revision. It retries once if navigation changes during capture and never combines
DOM, URL, or screenshot data from different revisions. Every model receives this
semantic observation. Text-only models do not take automatic screenshots;
vision models take them automatically only for canvas/video or image-heavy pages,
while an explicit screenshot request remains unconditional. Input field values
are excluded from the semantic snapshot.
Both adaptive and explicit captures use bounded visual readiness: DOM readiness,
fonts, up to twelve incomplete images in the viewport, and two animation frames.
Asset waiting is capped at 1100 ms and frame waiting has a 150 ms fallback; the
path never waits for global network idle, so long-lived requests cannot stall
the browser. Adaptive observations refresh the
semantic snapshot after this wait so text and image reflect the same painted
state as closely as possible. Capture retries once when the page revision changes
and otherwise rejects a cross-revision screenshot.
The app connects that view to `/api/browser/host`; agent actions are visible,
and **Take Over** provides single-owner control for manual login or verification.
A direct page click or scroll also takes control without swallowing that first
interaction; takeover cancels an in-flight agent command. Submitting the next
chat message automatically resumes agent control, so the browser toolbar does
not need a separate hand-back action. The status bar shows handshake state, last
action, and owner. Agent ownership adds a restrained amber pane outline; while a
page command is running, the relevant address/page surface, active-tab dot, and
status label pulse with the shared activity token. Fast commands remain visible
for at least 800 ms, and the fixed-width **Take Over** area becomes an action
capsule such as **Clicking** without shifting the address field. User takeover switches the
outline and owner label to green. Reduced Motion keeps the color, outline, and
action text but removes the pulse. On macOS it replaces the WebUI right rail in place while the
chat viewport, titlebar, sessions rail, and composer keep their desktop geometry.
Opening Browser captures the dashboard visibility and width, then chooses a
transient Browser width equal to 50% of the window clamped to 600–900 points and
the available conversation space. Closing restores that dashboard snapshot;
Browser drag and maximize previews never overwrite the persisted dashboard
width. The Browser drag range has no fixed maximum width. Widening it
automatically collapses an open Sessions rail before the conversation would
drop below 520 points; after that, the right rail can keep growing only while
preserving the same 520-point functional minimum.
Shrinking the right rail never collapses Sessions, and the automatic collapse
is idempotent so delayed native/Web acknowledgements cannot toggle it open again.
Opening Browser in an already narrow window applies the same rule immediately;
an automatically collapsed Sessions rail is restored when Browser closes unless
the user changed it while Browser was open. Manually reopening Sessions clamps
the Browser to keep the 520-point floor. The Browser itself has a 320-point
minimum so its address and width controls remain usable.
Its visible separator is centered in a 40-point native AppKit drag target that
straddles the pane edge across both the titlebar and content. A persistent grip
and accent-colored hover state make the target discoverable. The native target
explicitly owns its complete hit rectangle and keeps pointer capture over the
embedded `WKWebView`, while drag previews update the browser and titlebar together.
The covered WebUI rail receives the same width through a non-persistent one-way
preview, so shrinking the browser cannot reveal or select the dashboard below;
no Browser width is persisted into dashboard state. The maximize action uses the
current window minus the 520-point conversation floor, restores the pre-maximize
Browser width, follows later window resizes while active, and a manual drag exits
maximize state. Preview measurements do not feed back into the active native drag.
Every app-owned fixed dialog uses the shared `conversation-overlay` marker.
The chat WebView reports only whether such an overlay is visible; while it is,
the shell raises the full chat WebView above the Browser pane and disables the
Browser and separator hit targets. The chat WebView has a clear under-page
surface; while an overlay is active, its reserved Browser rail becomes
transparent so the live Browser remains visible beneath the dimming mask rather
than being replaced by an opaque chat background. Settings, new-session, image-preview,
worktree-exit, rename, delete, and error dialogs therefore keep the same
full-content geometry with or without Browser. Dismissing the dialog restores
the prior view order without rebuilding tabs, navigation, width, or control owner.
When the chat requests a local file preview, a dedicated presentation-only
WebKit message hides the Browser pane so the underlying dashboard track can show
the file immediately. The message carries no file path; tabs, navigation state,
and browser ownership remain preserved for the next Browser open.
The separator forms one continuous
hairline with the titlebar instead of an inset seam. The
WebSocket host reconnects with bounded backoff after a server restart,
and only reports connected after the server acknowledges its hello. Closing the
pane hides it without deleting the browser profile. The `web` extra installs the
Uvicorn WebSocket transport required by this channel.

The `evaluate` browser command accepts synchronous expressions and asynchronous
function bodies that use a top-level `return await ...`. It waits up to 30
seconds for the latter and only sends JSON-compatible final values. WebKit
unsupported-result failures are normalized to `unsupported_result` with an
actionable hint, unresolved promises return `browser_timeout`, and a timed-out
evaluation does not prevent the host from receiving the next command.

The handshake is versioned (`protocol: 1`); an unacknowledged, idle, or
incompatible socket never becomes an active host. Only one Apple app owns a
server's visible browser at a time. Before closing a replaced socket, the server
sends an explicit `host_replaced` message so the old app reports that another app
is active and does not enter a reconnect loop.

Panel transitions use values generated from `../design-tokens/motion.json`;
`python ../scripts/generate_motion_tokens.py --check` verifies the Web CSS and
Swift constants. Native commands and Web chrome updates carry the same action
id and motion token. The WebUI commits target geometry once and posts the start
state synchronously; the shell snaps its titlebar tracks to that geometry and
animates only the matching panel surfaces with the shared compositor motion.
This avoids a second inherited width animation or a native optimistic toggle
being retargeted by WebKit. The shell ignores a stale `settled` message after a
newer action and records native input, Web dispatch, transition start, and
settled with macOS signposts. Resize observation only verifies coalesced stable
geometry; it does not drive SwiftUI layout on every Web animation frame.

## Xcode / iOS

Open `AutoVerseApple.xcodeproj` with a full Xcode installation. The project has
separate macOS and iOS app targets sharing the files under `Sources/Shared`.
The checked-in macOS development scheme uses this workspace as its working
directory, so Xcode Run can discover `.venv/bin/autoverse-web`.

The iOS app is a remote client. Enter an HTTPS URL for an AutoVerse Web server
reachable over a trusted VPN or authenticated reverse proxy. Do not expose the
current unauthenticated server directly to the public internet.

For local simulator development, `http://127.0.0.1:8170` reaches a Web server
running on the host Mac and is accepted as a loopback development URL.

## Distribution note

`swift run` is a development path. A release build should bundle a frozen,
signed Python runtime as a macOS sidecar. The iOS app never bundles or launches
Python.
