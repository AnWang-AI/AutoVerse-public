"use strict";
/* AutoVerse WebUI client — SSE block stream → TUI-faithful DOM (docs/guides/using-webui.md §5).
   The server does all the rendering decisions; this file only paints the
   structured blocks it streams, mirrors derived state into the sidebar, and
   relays user intent (message / approval / plan / commands) back over REST. */

// ── tiny helpers ─────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const attr = (s) => esc(s).replace(/"/g, "&quot;");
const el = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
// Structured text blocks should already be plain text. Strip CSI sequences as
// a compatibility guard for cached/older snapshots so terminal styling can
// never become visible chat content.
const stripAnsiText = (s) => String(s == null ? "" : s)
  .replace(/(?:\x1B\[|\x9B)[0-?]*[ -/]*[@-~]/g, "");

// ── appearance ──────────────────────────────────────────────────────────────
const APPEARANCE_KEY = "autoverse-appearance";
const DEFAULT_APPEARANCE = "light";
const ACTIVE_SESSION_KEY = "autoverse-active-session";
const RIGHT_RAIL_WIDTH_KEY = "autoverse-right-rail-width";
const colorScheme = matchMedia("(prefers-color-scheme: light)");
const APPEARANCE_ICONS = {
  light: `<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M16.5 12.15A6.75 6.75 0 0 1 7.85 3.5 6.75 6.75 0 1 0 16.5 12.15Z"></path></svg>`,
  dark: `<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="3.25"></circle><path d="M10 2.2v1.6M10 16.2v1.6M2.2 10h1.6M16.2 10h1.6M4.5 4.5l1.15 1.15M14.35 14.35l1.15 1.15M15.5 4.5l-1.15 1.15M5.65 14.35L4.5 15.5"></path></svg>`,
};
const FILE_PREVIEW_ICONS = {
  copy: `<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="7" y="6" width="9" height="11" rx="1.5"></rect><path d="M4 13.5V4.75C4 3.8 4.8 3 5.75 3H13"></path></svg>`,
  open: `<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M8 5H5.75C4.8 5 4 5.8 4 6.75v7.5C4 15.2 4.8 16 5.75 16h7.5c.95 0 1.75-.8 1.75-1.75V12"></path><path d="M11 4h5v5M10 10l6-6"></path></svg>`,
  fullscreen: `<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 7V3h4M13 3h4v4M17 13v4h-4M7 17H3v-4"></path></svg>`,
  close: `<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 5l10 10M15 5L5 15"></path></svg>`,
};

function filePreviewActionButton(action, title, icon) {
  return `<button type="button" data-${action}-file title="${attr(title)}" aria-label="${attr(title)}">${icon}</button>`;
}

function applyAppearance(mode, persist = true) {
  const appearance = ["system", "light", "dark"].includes(mode) ? mode : DEFAULT_APPEARANCE;
  const theme = appearance === "system" ? (colorScheme.matches ? "light" : "dark") : appearance;
  const root = document.documentElement;
  const themeChanged = root.dataset.theme !== theme;
  if (root.dataset.appearance !== appearance) root.dataset.appearance = appearance;
  if (themeChanged) root.dataset.theme = theme;
  if (persist && localStorage.getItem(APPEARANCE_KEY) !== appearance) {
    localStorage.setItem(APPEARANCE_KEY, appearance);
  }

  const quick = $("btnTheme");
  if (quick) {
    quick.innerHTML = APPEARANCE_ICONS[theme];
    quick.title = theme === "light" ? "切换到深色外观" : "切换到浅色外观";
    quick.setAttribute("aria-label", quick.title);
  }
  if (themeChanged && document.body) rerenderMermaid();
}

colorScheme.addEventListener("change", () => {
  if ((localStorage.getItem(APPEARANCE_KEY) || DEFAULT_APPEARANCE) === "system") applyAppearance("system", false);
});

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch { data = {}; }
  if (!data || typeof data !== "object") data = {};
  data.__ok = r.ok;
  data.__status = r.status;
  return data;
}

function kfmt(n) {
  n = Number(n) || 0;
  if (n >= 1000) return (n / 1000).toFixed(n % 1000 === 0 ? 0 : 1) + "k";
  return String(n);
}
function commas(n) { return (Number(n) || 0).toLocaleString("en-US"); }
function fmtDur(ms) {
  ms = Number(ms) || 0;
  if (ms < 1000) return Math.round(ms) + "ms";
  const s = Math.round(ms / 1000);
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60), r = s % 60;
  return m + "m" + (r ? " " + r + "s" : "");
}
function fmtTurnDur(ms) {
  ms = Math.max(0, Number(ms) || 0);
  if (ms < 1000) return Math.floor(ms) + "ms";
  if (ms < 60000) return Math.floor(ms / 1000) + "s";
  const total = Math.floor(ms / 1000);
  if (ms < 3600000) return Math.floor(total / 60) + "m " + (total % 60) + "s";
  return Math.floor(total / 3600) + "h " + Math.floor((total % 3600) / 60) + "m";
}

function monitorInteractionFrameGaps(actionId) {
  const started = performance.now();
  let previous = started;
  const sample = (now) => {
    const gap = now - previous;
    if (gap > 50) {
      performance.measure(`autoverse:raf-gap:${actionId}`, {
        start: previous,
        duration: gap,
        detail: { actionId, approximateLongTask: true },
      });
    }
    previous = now;
    if (now - started < 500) requestAnimationFrame(sample);
  };
  requestAnimationFrame(sample);
}
function relTime(epoch) {
  if (!epoch) return "";
  const d = Date.now() / 1000 - epoch;
  if (d < 60) return "刚刚";
  if (d < 3600) return Math.floor(d / 60) + " 分钟前";
  if (d < 86400) return Math.floor(d / 3600) + " 小时前";
  if (d < 172800) return "昨天";
  return Math.floor(d / 86400) + " 天前";
}

// ── minimal markdown → HTML (headings/lists/quote/fence/inline) ──────────────
function normalizePreviewPath(path) {
  const source = String(path || "").replace(/\\/g, "/");
  const absolute = source.startsWith("/");
  const parts = [];
  source.split("/").forEach((part) => {
    if (!part || part === ".") return;
    if (part === ".." && parts.length && parts[parts.length - 1] !== "..") parts.pop();
    else parts.push(part);
  });
  const normalized = parts.join("/");
  return absolute ? `/${normalized}` : normalized || ".";
}

function previewLinkTarget(href, basePath) {
  const raw = String(href || "").trim();
  if (!raw || raw.startsWith("#") || raw.startsWith("//") || /^[a-zA-Z][\w+.-]*:/.test(raw)) return "";
  const target = raw.split(/[?#]/, 1)[0];
  if (!target) return "";
  if (target.startsWith("/") || target.startsWith("~/")) {
    return normalizePreviewPath(target);
  }
  const base = String(basePath || "");
  const dir = base && base !== "." && base.includes("/") ? base.slice(0, base.lastIndexOf("/")) : "";
  return normalizePreviewPath(dir ? `${dir}/${target}` : target);
}

function looksLikePreviewPath(value) {
  const text = String(value || "").trim();
  if (!text || /[\r\n]/.test(text)) return false;
  if (/^(?:\/|~\/|\.\.?\/)/.test(text)) return true;
  const path = text.replace(/:\d+(?::\d+)?$/, "");
  const basename = path.replace(/\\/g, "/").split("/").pop() || "";
  if (/^(?:Makefile|Dockerfile|Justfile|Procfile|Rakefile|Gemfile)$/i.test(basename)) return true;
  return /^[^/]+\.[a-zA-Z0-9]{1,16}$/.test(basename);
}

function inlineCodeHtml(value, basePath) {
  const code = `<code>${esc(value)}</code>`;
  if (/^https?:\/\/[^\s]+$/i.test(String(value || "").trim())) {
    return webLinkHtml(String(value).trim(), code);
  }
  if (!looksLikePreviewPath(value)) return code;
  const target = previewLinkTarget(value, basePath);
  if (!target) return code;
  return `<a href="#" class="inline-file-link" data-open-file="${attr(target)}" ` +
    `title="在文件预览中打开">${code}</a>`;
}

function webLinkHtml(url, label) {
  return `<a href="${attr(url)}" target="_blank" rel="noopener" ` +
    `class="conversation-web-link">${label}</a>`;
}

function splitBareUrlSuffix(value) {
  let url = String(value || "");
  let suffix = "";
  while (url && /[.,;:!?)\]}，。；：！？、）】》」』]/.test(url.slice(-1))) {
    suffix = url.slice(-1) + suffix;
    url = url.slice(0, -1);
  }
  return [url, suffix];
}

function inlineMd(s, basePath = "") {
  const codeSpans = [];
  const markdownLinks = [];
  const bareLinks = [];
  s = String(s || "").replace(/`([^`]+)`/g, (_, code) => {
    const index = codeSpans.push(code) - 1;
    return `AUTOVERSE_INLINE_CODE_${index}_END`;
  });
  // Protect explicit Markdown links before auto-linking bare URLs, otherwise
  // the URL inside ``[label](https://...)`` would become a nested anchor.
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, url) => {
    const index = markdownLinks.push({ label, url }) - 1;
    return `AUTOVERSE_MARKDOWN_LINK_${index}_END`;
  });
  s = s.replace(/\bhttps?:\/\/[^\s<>"']+/gi, (raw) => {
    const [url, suffix] = splitBareUrlSuffix(raw);
    if (!url) return raw;
    const index = bareLinks.push(url) - 1;
    return `AUTOVERSE_BARE_LINK_${index}_END${suffix}`;
  });
  s = esc(s);
  s = s.replace(/\*\*([^*]+)\*\*/g, (_, c) => `<strong>${c}</strong>`);
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, (_, p, c) => `${p}<em>${c}</em>`);
  s = s.replace(/AUTOVERSE_MARKDOWN_LINK_(\d+)_END/g, (_, index) => {
    const link = markdownLinks[Number(index)] || { label: "", url: "" };
    let label = esc(link.label);
    label = label.replace(/\*\*([^*]+)\*\*/g, (_, content) => `<strong>${content}</strong>`);
    label = label.replace(/(^|[^*])\*([^*\n]+)\*/g, (_, prefix, content) =>
      `${prefix}<em>${content}</em>`
    );
    label = label.replace(/AUTOVERSE_INLINE_CODE_(\d+)_END/g, (_, codeIndex) =>
      `<code>${esc(codeSpans[Number(codeIndex)] || "")}</code>`
    );
    const target = previewLinkTarget(link.url, basePath);
    if (target) return `<a href="#" data-open-file="${attr(target)}">${label}</a>`;
    return webLinkHtml(link.url, label);
  });
  s = s.replace(/AUTOVERSE_BARE_LINK_(\d+)_END/g, (_, index) => {
    const url = bareLinks[Number(index)] || "";
    return webLinkHtml(url, esc(url));
  });
  s = s.replace(/AUTOVERSE_INLINE_CODE_(\d+)_END/g, (_, index) =>
    inlineCodeHtml(codeSpans[Number(index)] || "", basePath)
  );
  return s;
}
function tableCells(line) {
  let s = String(line || "").trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
  const cells = [], buf = [];
  let code = false;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (ch === "\\" && s[i + 1] === "|") { buf.push("|"); i++; continue; }
    if (ch === "`") code = !code;
    if (ch === "|" && !code) { cells.push(buf.join("").trim()); buf.length = 0; }
    else buf.push(ch);
  }
  cells.push(buf.join("").trim());
  return cells;
}
function tableAligns(line) {
  if (!String(line || "").includes("|")) return null;
  const cells = tableCells(line);
  if (!cells.length || cells.some((c) => !/^:?-{3,}:?$/.test(c))) return null;
  return cells.map((c) => c.startsWith(":") && c.endsWith(":") ? "center"
    : c.endsWith(":") ? "right" : "left");
}
function tableRowHtml(cells, aligns, tag) {
  return "<tr>" + aligns.map((align, i) =>
    `<${tag} class="align-${align}">${inlineMd(cells[i] || "")}</${tag}>`
  ).join("") + "</tr>";
}
function normalizeCodeLanguage(lang) {
  const key = String(lang || "").toLowerCase().replace(/^\./, "");
  const aliases = {
    bash: "sh", zsh: "sh", shell: "sh", console: "sh",
    javascript: "js", mjs: "js", cjs: "js",
    typescript: "ts",
    py: "python",
    yml: "yaml",
    htm: "html", xhtml: "html", xml: "html", svg: "html",
    cxx: "cpp", cc: "cpp", hpp: "cpp", h: "c",
    rs: "rust",
    mdx: "md",
  };
  return aliases[key] || key;
}
const CODE_KEYWORDS = {
  python: new Set("and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield True False None self cls".split(" ")),
  js: new Set("async await break case catch class const continue debugger default delete do else export extends finally for from function get if import in instanceof let new of return set static super switch this throw try typeof var void while with yield".split(" ")),
  ts: new Set("abstract any as async await boolean break case catch class const constructor continue debugger declare default delete do else enum export extends false finally for from function get if implements import in infer instanceof interface is keyof let module namespace never new null number object of private protected public readonly return set static string super switch symbol this throw true try type typeof undefined unknown var void while with yield".split(" ")),
  css: new Set("important media supports container keyframes from to import charset namespace layer font-face page".split(" ")),
  sh: new Set("if then else elif fi for while do done case esac function in select until time coproc return local export readonly unset shift break continue eval exec source alias unalias test".split(" ")),
  yaml: new Set("true false null yes no on off".split(" ")),
  json: new Set("true false null".split(" ")),
  sql: new Set("select from where join left right inner outer on group by order having limit offset insert into update delete create alter drop table view index values set and or not null is in exists as distinct union all case when then else end".split(" ")),
  go: new Set("break default func interface select case defer go map struct chan else goto package switch const fallthrough if range type continue for import return var".split(" ")),
  rust: new Set("as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while".split(" ")),
  java: new Set("abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for if implements import instanceof int interface long native new package private protected public return short static strictfp super switch synchronized this throw throws transient try void volatile while true false null".split(" ")),
  c: new Set("auto break case char const continue default do double else enum extern float for goto if inline int long register restrict return short signed sizeof static struct switch typedef union unsigned void volatile while".split(" ")),
  cpp: new Set("alignas alignof and asm auto bool break case catch char class const constexpr continue decltype default delete do double else enum explicit export extern false float for friend goto if inline int long mutable namespace new noexcept nullptr operator private protected public register reinterpret_cast return short signed sizeof static_assert static_cast struct switch template this throw true try typedef typeid typename union unsigned using virtual void volatile while".split(" ")),
};
const CODE_CONSTANTS = new Set("true false null undefined none nil nan infinity yes no on off True False None".split(" "));
const CODE_BUILTINS = new Set("print len range dict list set tuple str int float bool open enumerate zip map filter reduce console log error warn JSON Math Date Promise Array Object String Number Boolean fetch require module process window document".split(" "));
const PLAIN_CODE_LANGS = new Set(["txt", "text", "log", "diff", "patch", "md", "markdown"]);

function highlightHtmlLine(line) {
  return esc(line).replace(/(&lt;\/?)([A-Za-z][\w:-]*)([^&]*?)(\/?&gt;)/g, (_, open, tag, attrs, close) => {
    const attrHtml = attrs.replace(/([\w:-]+)(=)(&quot;.*?&quot;|&#39;.*?&#39;|[^\s&]+)/g,
      '<span class="t">$1</span><span class="o">$2</span><span class="s">$3</span>');
    return `<span class="o">${open}</span><span class="k">${tag}</span>${attrHtml}<span class="o">${close}</span>`;
  });
}

function highlightCodeLine(line, lang) {
  const language = normalizeCodeLanguage(lang);
  if (PLAIN_CODE_LANGS.has(language)) return esc(line);
  if (language === "html") return highlightHtmlLine(line);
  const keywords = CODE_KEYWORDS[language] || CODE_KEYWORDS.js;
  const hashComments = new Set(["python", "sh", "yaml", "ruby", "toml", "ini", "conf"]);
  const slashComments = new Set(["js", "ts", "jsx", "tsx", "css", "go", "rust", "java", "c", "cpp"]);
  let out = "";
  let i = 0;
  while (i < line.length) {
    const ch = line[i];
    const next = line[i + 1] || "";
    if (hashComments.has(language) && ch === "#") {
      out += `<span class="c">${esc(line.slice(i))}</span>`;
      break;
    }
    if (slashComments.has(language) && ch === "/" && (next === "/" || next === "*")) {
      out += `<span class="c">${esc(line.slice(i))}</span>`;
      break;
    }
    if (ch === '"' || ch === "'" || (ch === "`" && ["js", "ts", "jsx", "tsx", "sh"].includes(language))) {
      const quote = ch;
      let j = i + 1;
      while (j < line.length) {
        if (line[j] === "\\") { j += 2; continue; }
        if (line[j] === quote) { j++; break; }
        j++;
      }
      out += `<span class="s">${esc(line.slice(i, j))}</span>`;
      i = j;
      continue;
    }
    if (/\d/.test(ch) && (i === 0 || !/[\w.]/.test(line[i - 1] || ""))) {
      const m = line.slice(i).match(/^(0x[\da-fA-F]+|\d+(?:\.\d+)?(?:e[+-]?\d+)?)/);
      if (m) {
        out += `<span class="n">${esc(m[0])}</span>`;
        i += m[0].length;
        continue;
      }
    }
    if (/[A-Za-z_$]/.test(ch)) {
      const m = line.slice(i).match(/^[A-Za-z_$][\w$-]*/);
      const word = m ? m[0] : ch;
      const lower = word.toLowerCase();
      let cls = "";
      if (keywords.has(word) || keywords.has(lower)) cls = "k";
      else if (CODE_CONSTANTS.has(word) || CODE_CONSTANTS.has(lower)) cls = "cn";
      else if (CODE_BUILTINS.has(word)) cls = "b";
      else {
        const rest = line.slice(i + word.length);
        if (/^\s*\(/.test(rest)) cls = "f";
      }
      out += cls ? `<span class="${cls}">${esc(word)}</span>` : esc(word);
      i += word.length;
      continue;
    }
    if (/[-+*/%=!<>|&^~?:.,;()[\]{}]/.test(ch)) out += `<span class="o">${esc(ch)}</span>`;
    else out += esc(ch);
    i++;
  }
  return out;
}

function highlightCodeBlock(code, lang) {
  const language = normalizeCodeLanguage(lang);
  return String(code || "").split("\n").map((line) => highlightCodeLine(line, language)).join("\n");
}

// ── ASCII architecture diagrams ──────────────────────────────────────────────
function looksLikeAsciiArchitecture(text) {
  const s = String(text || "");
  const lines = s.split("\n").filter((line) => line.trim());
  const boxChars = (s.match(/[┌┐└┘├┤│─]/g) || []).length;
  const pipeChars = (s.match(/[|+]/g) || []).length;
  const titleLines = lines.filter((line) => parseAsciiArchitectureTitle(line)).length;
  return lines.length >= 4 && titleLines >= 2 && (boxChars >= 6 || pipeChars >= 8);
}

function parseAsciiArchitectureTitle(line) {
  const trimmed = String(line || "").trim();
  if (!/^[│|┌├+]/.test(trimmed)) return "";
  const parts = trimmed.split(/[─-]{3,}/);
  if (parts.length < 2) return "";
  const title = parts[0]
    .replace(/^[│|┌├+][─\-\s]*/, "")
    .replace(/[┐┤│|+\s]+$/g, "")
    .trim();
  if (!title || title.length > 56 || !/[A-Za-z0-9]/.test(title)) return "";
  return title;
}

function isAsciiArchitectureRunLine(line) {
  const trimmed = String(line || "").trim();
  if (!trimmed) return false;
  if (/^[│|┌├└+]/.test(trimmed)) return true;
  return /[┌┐└┘├┤│─]/.test(trimmed) && /[│|─-]/.test(trimmed);
}

function parseAsciiArchitectureItems(line) {
  const trimmed = String(line || "").trim();
  if (!trimmed || /^[└+][─\-\s]*[┘+]$/.test(trimmed)) return [];
  const withoutOuterBars = trimmed
    .replace(/^[│|]\s*/, "")
    .replace(/\s*[│|]$/, "");
  return withoutOuterBars
    .split(/[│|]/)
    .flatMap((part) => part.split(/\s*,\s*/))
    .map((part) => part.trim())
    .filter((part) => part && !/^[─\-\s]+$/.test(part));
}

function parseAsciiArchitecture(text) {
  if (!looksLikeAsciiArchitecture(text)) return null;
  const layers = [];
  String(text || "").split("\n").forEach((line) => {
    const title = parseAsciiArchitectureTitle(line);
    if (title) {
      layers.push({ title, items: [] });
      return;
    }
    const current = layers[layers.length - 1];
    if (!current) return;
    current.items.push(...parseAsciiArchitectureItems(line));
  });
  const meaningful = layers
    .map((layer) => ({ title: layer.title, items: [...new Set(layer.items)] }))
    .filter((layer) => layer.items.length);
  return meaningful.length >= 2 ? { layers: meaningful, original: String(text || "") } : null;
}

function renderAsciiArchitectureHtml(diagram) {
  const layers = diagram.layers.map((layer, idx) => {
    const items = layer.items.map((item) => `<span>${esc(item)}</span>`).join(`<i>│</i>`);
    return `<div class="ascii-arch-row"><strong>${idx + 1}. ${esc(layer.title)}</strong><div>${items}</div></div>`;
  }).join("");
  return `<div class="ascii-arch">` +
    `<div class="ascii-arch-head"><strong>ASCII Architecture</strong><em>${diagram.layers.length} layers</em></div>` +
    `<div class="ascii-arch-body">${layers}</div>` +
    `<details><summary>查看原始 ASCII</summary><pre><code>${esc(diagram.original)}</code></pre></details>` +
    `</div>`;
}

function collectAsciiArchitecture(lines, start) {
  if (!parseAsciiArchitectureTitle(lines[start])) return null;
  const buf = [];
  let i = start;
  while (i < lines.length && isAsciiArchitectureRunLine(lines[i])) {
    buf.push(lines[i]);
    i++;
  }
  const diagram = parseAsciiArchitecture(buf.join("\n"));
  return diagram ? { diagram, next: i } : null;
}

function mdToHtml(src, basePath = "") {
  const lines = String(src || "").split("\n");
  const out = [];
  let i = 0, list = null, para = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${inlineMd(para.join(" "), basePath)}</p>`); para = []; } };
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^```(\w*)\s*$/);
    if (fence) {
      flushPara(); closeList();
      const lang = (fence[1] || "").toLowerCase();
      const buf = []; i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;
      const code = buf.join("\n");
      const asciiDiagram = parseAsciiArchitecture(code);
      out.push(asciiDiagram
        ? renderAsciiArchitectureHtml(asciiDiagram)
        : `<pre><code>${highlightCodeBlock(code, lang)}</code></pre>`);
      continue;
    }
    const asciiRun = collectAsciiArchitecture(lines, i);
    if (asciiRun) {
      flushPara(); closeList();
      out.push(renderAsciiArchitectureHtml(asciiRun.diagram));
      i = asciiRun.next;
      continue;
    }
    const aligns = i + 1 < lines.length ? tableAligns(lines[i + 1]) : null;
    if (aligns && line.includes("|")) {
      flushPara(); closeList();
      const header = tableCells(line);
      if (header.length === aligns.length) {
        const rows = [];
        i += 2;
        while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
          const cells = tableCells(lines[i]);
          rows.push(tableRowHtml(cells, aligns, "td"));
          i++;
        }
        out.push(`<div class="md-table-wrap"><table><thead>${tableRowHtml(header, aligns, "th")}</thead>` +
          `<tbody>${rows.join("")}</tbody></table></div>`);
        continue;
      }
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { flushPara(); closeList(); out.push(`<h${h[1].length}>${inlineMd(h[2], basePath)}</h${h[1].length}>`); i++; continue; }
    if (/^\s*>\s?/.test(line)) {
      flushPara(); closeList();
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^\s*>\s?/, "")); i++; }
      out.push(`<blockquote>${inlineMd(buf.join(" "), basePath)}</blockquote>`);
      continue;
    }
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ul || ol) {
      flushPara();
      const want = ul ? "ul" : "ol";
      if (list && list !== want) closeList();
      if (!list) { list = want; out.push(`<${want}>`); }
      out.push(`<li>${inlineMd((ul || ol)[1], basePath)}</li>`);
      i++; continue;
    }
    if (!line.trim()) { flushPara(); closeList(); i++; continue; }
    para.push(line.trim()); i++;
  }
  flushPara(); closeList();
  return out.join("");
}

// ── Mermaid diagrams ────────────────────────────────────────────────────────
// Mermaid is loaded lazily and progressively enhances fenced ```mermaid blocks.
// If the CDN/module fails, the source remains visible as a normal code block.
let _mermaidMod = null;
let _mermaidPromise = null;
let _mermaidSeq = 0;
let _mermaidRenderToken = 0;

function cssVar(name, fallback) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

function mermaidThemeVars() {
  const bg = cssVar("--bg-panel", "#161618");
  const page = cssVar("--bg", "#0e0e10");
  const element = cssVar("--bg-element", "#1d1d20");
  const text = cssVar("--text", "#ececee");
  const muted = cssVar("--text-muted", "#9a9aa2");
  const dim = cssVar("--text-dim", "#787880");
  const border = cssVar("--border", "#3b3b41");
  const subtle = cssVar("--border-subtle", "#26262a");
  const accent = cssVar("--accent", "#e8b339");
  const cool = cssVar("--accent-cool", "#8eccaa");
  const wash = cssVar("--accent-wash", "#282216");
  const success = cssVar("--success", "#74c09a");
  const warning = cssVar("--warning", "#e0a458");
  const error = cssVar("--error", "#d16b6b");
  return {
    background: page,
    mainBkg: bg,
    secondBkg: element,
    tertiaryColor: page,
    primaryColor: bg,
    primaryBorderColor: border,
    primaryTextColor: text,
    secondaryColor: element,
    secondaryBorderColor: subtle,
    secondaryTextColor: text,
    tertiaryBorderColor: subtle,
    tertiaryTextColor: muted,
    lineColor: dim,
    textColor: text,
    nodeBorder: border,
    clusterBkg: page,
    clusterBorder: subtle,
    titleColor: accent,
    edgeLabelBackground: bg,
    fontFamily: '"AutoVerse CJK", -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif',
    actorBkg: bg,
    actorBorder: subtle,
    actorTextColor: text,
    labelBoxBkgColor: bg,
    labelBoxBorderColor: subtle,
    signalColor: dim,
    signalTextColor: muted,
    noteBkgColor: wash,
    noteBorderColor: accent,
    noteTextColor: text,
    activationBkgColor: element,
    activationBorderColor: border,
    loopTextColor: muted,
    altBackground: page,
    taskTextColor: text,
    taskTextOutsideColor: muted,
    taskTextLightColor: text,
    sectionBkgColor: bg,
    sectionBkgColor2: element,
    sectionBkgColor3: wash,
    sectionBkgColor4: page,
    sectionBkgColor5: bg,
    sectionBkgColor6: element,
    sectionBkgColor7: wash,
    sectionBkgColor8: page,
    activeTaskBkgColor: wash,
    activeTaskBorderColor: accent,
    doneTaskBkgColor: element,
    doneTaskBorderColor: success,
    critBkgColor: error,
    critBorderColor: error,
    todayLineColor: warning,
    gridColor: subtle,
    classText: text,
    stateLabelColor: text,
    stateBkg: element,
    labelBackground: bg,
    compositeBackground: page,
    c0: accent,
    c1: success,
    c2: error,
    c3: cool,
    c4: warning,
  };
}

function mermaidNodeId(text) {
  const s = String(text || "").trim();
  const m = s.match(/^([A-Za-z_][\w-]*)/);
  return m ? m[1] : "";
}

function cleanMermaidLabel(text) {
  let s = String(text || "").trim();
  s = s
    .replace(/&lt;br\s*\/?&gt;/gi, "\n")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/\\n/g, "\n");
  let changed = true;
  while (changed && s.length > 1) {
    changed = false;
    const pairs = [
      ["[", "]"],
      ["(", ")"],
      ["{", "}"],
      [">", "]"],
      ['"', '"'],
      ["'", "'"],
    ];
    for (const [open, close] of pairs) {
      if (s.startsWith(open) && s.endsWith(close)) {
        s = s.slice(1, -1).trim();
        changed = true;
      }
    }
  }
  return s;
}

function mermaidNodeDefToken(token) {
  const s = String(token || "").trim();
  const id = mermaidNodeId(s);
  if (!id) return null;
  const def = s.match(/^([A-Za-z_][\w-]*)(\s*[\[\(\{>].*)$/);
  return { id, def: def ? s : "" };
}

function parseMermaidEdgeSegments(line) {
  const text = String(line || "").trim();
  const matches = [...text.matchAll(/\s*-->\s*(?:\|([^|]+)\|\s*)?/g)];
  if (!matches.length) return [];
  return matches.map((match, i) => {
    const fromToken = text.slice(i === 0 ? 0 : matches[i - 1].index + matches[i - 1][0].length, match.index).trim();
    const toEnd = i + 1 < matches.length ? matches[i + 1].index : text.length;
    const toToken = text.slice(match.index + match[0].length, toEnd).trim();
    const from = mermaidNodeDefToken(fromToken);
    const to = mermaidNodeDefToken(toToken);
    if (!from?.id || !to?.id) return null;
    return {
      from: from.id,
      to: to.id,
      fromDef: from.def,
      toDef: to.def,
      label: cleanMermaidLabel(match[1] || ""),
    };
  }).filter(Boolean);
}

function mermaidSubgraphTitle(line) {
  const m = String(line || "").trim().match(/^subgraph\s+([A-Za-z_][\w-]*)(.*)$/);
  if (!m) return "";
  const tail = (m[2] || "").trim();
  const bracket = tail.match(/^\[(.*)\]$/);
  const quoted = tail.match(/^"(.+)"$/);
  return cleanMermaidLabel((bracket && bracket[1]) || (quoted && quoted[1]) || tail || m[1] || "");
}

function mermaidProfile(source) {
  const text = String(source || "");
  const lines = text.split("\n").map((line) => line.trim()).filter(Boolean);
  const head = lines.find((line) => !line.startsWith("%%")) || "";
  const kind = head.match(/^(flowchart|graph)\b/i) ? "flowchart"
    : head.match(/^sequenceDiagram\b/i) ? "sequence"
    : head.match(/^stateDiagram/i) ? "state"
    : head.match(/^classDiagram\b/i) ? "class"
    : head.match(/^gantt\b/i) ? "gantt"
    : head.match(/^pie\b/i) ? "pie"
    : head.match(/^gitGraph\b/i) ? "git"
    : "other";
  const edges = (text.match(/-->|---|==>|-.->|->>|-->>/g) || []).length;
  const subgraphs = lines.filter((line) => /^subgraph\b/i.test(line)).length;
  const decisions = (text.match(/\{[^}]+\}/g) || []).length;
  const branches = new Map();
  lines.forEach((line) => {
    if (!line.includes("-->")) return;
    const parts = line.split(/\s*-->\s*/).map(mermaidNodeId).filter(Boolean);
    for (let i = 0; i < parts.length - 1; i++) {
      branches.set(parts[i], (branches.get(parts[i]) || 0) + 1);
    }
  });
  const fanout = Math.max(0, ...branches.values());
  const complex = kind === "flowchart" && (edges >= 10 || subgraphs >= 2 || decisions >= 2 || fanout >= 2);
  return { kind, edges, subgraphs, decisions, fanout, complex };
}

function applyMermaidProfileClasses(wrap, profile) {
  if (!wrap || !profile) return;
  [...wrap.classList].forEach((cls) => {
    if (
      cls.startsWith("mermaid-kind-") ||
      cls.startsWith("mermaid-profile-") ||
      cls === "mermaid-reflowed" ||
      cls === "mermaid-flow-custom" ||
      cls === "mermaid-flow-arch"
    ) {
      wrap.classList.remove(cls);
    }
  });
  wrap.classList.add(`mermaid-kind-${profile.kind}`);
  if (profile.complex) wrap.classList.add("mermaid-profile-complex");
}

function analyseLinearFlowchart(source) {
  const lines = String(source || "").split("\n");
  const head = lines.find((line) => line.trim());
  if (!/^(flowchart|graph)\s+(TD|TB)\b/i.test(String(head || "").trim())) return null;

  const defs = new Map();
  const groups = new Map();
  let currentGroup = "";
  const edges = [];
  const edgeLabels = new Map();

  for (const raw of lines.slice(1)) {
    const line = raw.trim();
    if (!line || line.startsWith("%%")) continue;
    if (/^subgraph\b/i.test(line)) {
      currentGroup = mermaidSubgraphTitle(line);
      continue;
    }
    if (/^end\b/i.test(line)) {
      currentGroup = "";
      continue;
    }
    if (/^direction\b|^style\b|^classDef\b|^class\b|^linkStyle\b|^click\b/i.test(line)) {
      continue;
    }
    if (line.includes("-->")) {
      const segments = parseMermaidEdgeSegments(line);
      segments.forEach((segment) => {
        if (segment.fromDef && !defs.has(segment.from)) defs.set(segment.from, segment.fromDef);
        if (segment.toDef && !defs.has(segment.to)) defs.set(segment.to, segment.toDef);
        if (currentGroup) {
          if (segment.fromDef) groups.set(segment.from, currentGroup);
          if (segment.toDef) groups.set(segment.to, currentGroup);
        }
        edges.push([segment.from, segment.to]);
        if (segment.label) edgeLabels.set(`${segment.from}->${segment.to}`, segment.label);
      });
      continue;
    }
    const def = line.match(/^([A-Za-z_][\w-]*)(\s*[\[\(\{>].*)$/);
    if (def) {
      defs.set(def[1], line);
      if (currentGroup) groups.set(def[1], currentGroup);
    }
  }

  if (edges.length < 3) return null;
  const nodes = new Set();
  const indeg = new Map();
  const out = new Map();
  for (const [a, b] of edges) {
    nodes.add(a); nodes.add(b);
    indeg.set(b, (indeg.get(b) || 0) + 1);
    indeg.set(a, indeg.get(a) || 0);
    out.set(a, (out.get(a) || 0) + 1);
    out.set(b, out.get(b) || 0);
  }
  if (nodes.size !== edges.length + 1) return null;
  const starts = [...nodes].filter((id) => (indeg.get(id) || 0) === 0);
  const ends = [...nodes].filter((id) => (out.get(id) || 0) === 0);
  if (starts.length !== 1 || ends.length !== 1) return null;
  for (const id of nodes) {
    const isStart = id === starts[0];
    const isEnd = id === ends[0];
    if (!isStart && (indeg.get(id) || 0) !== 1) return null;
    if (!isEnd && (out.get(id) || 0) !== 1) return null;
  }

  const next = new Map(edges);
  const chain = [];
  for (let id = starts[0]; id; id = next.get(id)) {
    chain.push(id);
    if (id === ends[0]) break;
  }
  if (chain.length !== nodes.size || chain.length < 4) return null;
  return { chain, defs, groups, edgeLabels };
}

function contiguousMermaidGroups(chain, groupMap) {
  const chunks = [];
  let current = null;
  for (const id of chain) {
    const title = groupMap.get(id) || "";
    if (!title) return null;
    if (!current || current.title !== title) {
      current = { title, nodes: [] };
      chunks.push(current);
    }
    current.nodes.push(id);
  }
  const seen = new Set();
  for (const chunk of chunks) {
    if (seen.has(chunk.title)) return null;
    seen.add(chunk.title);
  }
  return chunks.length >= 2 ? chunks : null;
}

function chunkLinearMermaidChain(chain) {
  const rows = chain.length <= 8 ? 4 : chain.length <= 12 ? 5 : 6;
  const chunks = [];
  for (let i = 0; i < chain.length; i += rows) {
    chunks.push({ title: "", nodes: chain.slice(i, i + rows) });
  }
  return chunks;
}

function analyseGroupedFlowchart(source) {
  const lines = String(source || "").split("\n");
  const head = lines.find((line) => line.trim());
  if (!/^(flowchart|graph)\s+(TD|TB)\b/i.test(String(head || "").trim())) return null;

  const defs = new Map();
  const nodeGroups = new Map();
  const groupIds = new Map();
  const groups = [];
  const edges = [];
  const edgeLabels = new Map();
  let current = null;

  const ensureGroup = (id, title) => {
    let group = groups.find((g) => g.id === id);
    if (!group) {
      group = { id, title: cleanMermaidLabel(title || id), nodes: [] };
      groups.push(group);
    }
    groupIds.set(id, group.title);
    return group;
  };
  const addNode = (id, def = "", group = current) => {
    if (!id || groupIds.has(id)) return;
    if (def && !defs.has(id)) defs.set(id, def);
    if (group && !nodeGroups.has(id)) {
      nodeGroups.set(id, group.id);
      if (!group.nodes.includes(id)) group.nodes.push(id);
    }
  };

  for (const raw of lines.slice(1)) {
    const line = raw.trim();
    if (!line || line.startsWith("%%")) continue;
    if (/^subgraph\b/i.test(line)) {
      const m = line.match(/^subgraph\s+([A-Za-z_][\w-]*)(.*)$/i);
      const id = m ? m[1] : `group_${groups.length}`;
      const title = mermaidSubgraphTitle(line);
      current = ensureGroup(id, title);
      continue;
    }
    if (/^end\b/i.test(line)) {
      current = null;
      continue;
    }
    if (/^direction\b|^style\b|^classDef\b|^class\b|^linkStyle\b|^click\b/i.test(line)) {
      continue;
    }
    if (line.includes("-->")) {
      const segments = parseMermaidEdgeSegments(line);
      segments.forEach((segment) => {
        addNode(segment.from, segment.fromDef);
        addNode(segment.to, segment.toDef);
        edges.push([segment.from, segment.to]);
        if (segment.label) edgeLabels.set(`${segment.from}->${segment.to}`, segment.label);
      });
      continue;
    }
    const def = line.match(/^([A-Za-z_][\w-]*)(\s*[\[\(\{>].*)$/);
    if (def) addNode(def[1], line);
  }

  const groupedNodes = [...nodeGroups.keys()].length;
  if (groups.length < 3 || groupedNodes < 6 || edges.length < 4) return null;
  const decisionLike = [...defs.values()].some((def) => /\{.+\}/.test(def));
  if (decisionLike) return null;

  const out = new Map();
  edges.forEach(([from, to]) => {
    out.set(from, [...(out.get(from) || []), to]);
  });
  const fanout = Math.max(0, ...[...out.values()].map((v) => v.length));
  const groupEdges = edges.filter(([from, to]) => nodeGroups.get(from) !== nodeGroups.get(to) || groupIds.has(to)).length;
  if (fanout < 3 && groupEdges < groups.length - 1) return null;
  return { groups, defs, nodeGroups, groupIds, edges, edgeLabels, fanout };
}

function reflowLinearFlowchart(source) {
  const analysis = analyseLinearFlowchart(source);
  if (!analysis) return "";
  const groups = contiguousMermaidGroups(analysis.chain, analysis.groups) || chunkLinearMermaidChain(analysis.chain);
  if (groups.length < 2) return "";
  const lines = ["flowchart LR"];
  const nodeDef = (id) => analysis.defs.get(id) || `${id}[${id}]`;
  groups.forEach((group, i) => {
    const title = group.title || " ";
    lines.push(`  subgraph av_col_${i}["${title.replace(/"/g, '\\"')}"]`);
    lines.push("    direction TB");
    group.nodes.forEach((id) => lines.push(`    ${nodeDef(id)}`));
    for (let j = 0; j < group.nodes.length - 1; j++) {
      lines.push(`    ${group.nodes[j]} --> ${group.nodes[j + 1]}`);
    }
    lines.push("  end");
  });
  for (let i = 0; i < groups.length - 1; i++) {
    const from = groups[i].nodes[groups[i].nodes.length - 1];
    const to = groups[i + 1].nodes[0];
    lines.push(`  ${from} --> ${to}`);
  }
  return { source: lines.join("\n"), groups, defs: analysis.defs, edgeLabels: analysis.edgeLabels };
}

function reflowGroupedFlowchart(source) {
  const analysis = analyseGroupedFlowchart(source);
  if (!analysis) return "";
  return analysis;
}

function analyseGenericFlowchart(source) {
  const lines = String(source || "").split("\n");
  const head = lines.find((line) => line.trim());
  const headText = String(head || "").trim();
  if (!/^(flowchart|graph)\s+/i.test(headText)) return null;
  const dir = (headText.match(/^(?:flowchart|graph)\s+([A-Z]{2})\b/i)?.[1] || "TD").toUpperCase();
  const defs = new Map();
  const edges = [];
  const edgeLabels = new Map();
  const nodes = new Set();
  let inSubgraph = false;

  const addNode = (id, def = "") => {
    if (!id) return;
    nodes.add(id);
    if (def && !defs.has(id)) defs.set(id, def);
  };

  for (const raw of lines.slice(1)) {
    const line = raw.trim();
    if (!line || line.startsWith("%%")) continue;
    if (/^subgraph\b/i.test(line)) {
      inSubgraph = true;
      continue;
    }
    if (/^end\b/i.test(line)) {
      inSubgraph = false;
      continue;
    }
    if (/^direction\b|^style\b|^classDef\b|^class\b|^linkStyle\b|^click\b/i.test(line)) continue;
    if (line.includes("-->")) {
      parseMermaidEdgeSegments(line).forEach((segment) => {
        addNode(segment.from, segment.fromDef);
        addNode(segment.to, segment.toDef);
        edges.push([segment.from, segment.to]);
        if (segment.label) edgeLabels.set(`${segment.from}->${segment.to}`, segment.label);
      });
      continue;
    }
    if (!inSubgraph) {
      const def = line.match(/^([A-Za-z_][\w-]*)(\s*[\[\(\{>].*)$/);
      if (def) addNode(def[1], line);
    } else {
      const def = line.match(/^([A-Za-z_][\w-]*)(\s*[\[\(\{>].*)$/);
      if (def) addNode(def[1], line);
    }
  }
  if (!nodes.size) return null;
  return { kind: "generic", dir, nodes: [...nodes], defs, edges, edgeLabels };
}

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

function mermaidNodeLabel(id, defs) {
  const def = String(defs && defs.get(id) ? defs.get(id) : id).trim();
  let body = def.startsWith(id) ? def.slice(id.length).trim() : def;
  if (!body) return id;
  body = body.replace(/^[-=]+/, "").trim();
  body = cleanMermaidLabel(body);
  return body || id;
}

function svgText(parent, text, attrs = {}) {
  const node = svgEl("text", attrs);
  node.textContent = text;
  parent.appendChild(node);
  return node;
}

function splitLinearNodeLabel(label) {
  const text = cleanMermaidLabel(label);
  const explicit = text.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  if (explicit.length > 1) {
    const m = explicit[0].match(/^([A-Za-z]*\d+[:：])\s*(.+)$/);
    return { prefix: m ? m[1] : "", lines: m ? [m[2], ...explicit.slice(1)] : explicit };
  }
  const m = text.match(/^([A-Za-z]*\d+[:：])\s*(.+)$/);
  const prefix = m ? m[1] : "";
  const body = m ? m[2] : text;
  const chars = Array.from(body);
  if (chars.length <= 12 || (/^[\x00-\x7F]+$/.test(body) && chars.length <= 15)) return { prefix, lines: [body] };
  const mid = Math.ceil(chars.length / 2);
  let cut = mid;
  for (let i = Math.max(3, mid - 6); i <= Math.min(chars.length - 3, mid + 6); i++) {
    if (/[、,，/／\s]/.test(chars[i])) {
      cut = i + 1;
      break;
    }
  }
  return {
    prefix,
    lines: [
      chars.slice(0, cut).join("").trim(),
      chars.slice(cut).join("").trim(),
    ].filter(Boolean),
  };
}

function linearNodeMetrics(label) {
  const parsed = splitLinearNodeLabel(label);
  const longest = Math.max(...parsed.lines.map((line) => Array.from(line).length), 0);
  const prefixLen = parsed.prefix ? Array.from(parsed.prefix).length + 1 : 0;
  const width = Math.min(174, Math.max(112, Math.max(longest + prefixLen, 8) * 9.5 + 34));
  const height = parsed.lines.length > 1 ? Math.max(54, 28 + parsed.lines.length * 15) : 42;
  return { ...parsed, width, height };
}

function archNodeMetrics(label) {
  const text = cleanMermaidLabel(label);
  const explicit = text.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  const m = text.match(/^([A-Za-z]*\d+[:：])\s*(.+)$/);
  const prefix = m ? m[1] : "";
  const body = m ? m[2] : text;
  let lines;
  if (explicit.length > 1) {
    lines = explicit;
  } else if (/^[\x00-\x7F]+$/.test(body) && Array.from(body).length <= 24) {
    lines = [body];
  } else if (Array.from(body).length <= 12) {
    lines = [body];
  } else {
    lines = splitLinearNodeLabel(text).lines;
  }
  const longest = Math.max(...lines.map((line) => Array.from(line).length), 0);
  const prefixLen = prefix ? Array.from(prefix).length + 1 : 0;
  const width = Math.min(196, Math.max(118, Math.max(longest + prefixLen, 9) * 8.4 + 34));
  const height = lines.length > 1 ? Math.max(54, 28 + lines.length * 15) : 42;
  return { prefix, lines, width, height };
}

function drawLinearNodeLabel(parent, metrics, cx, cy, colors, font) {
  const lineHeight = 15;
  const firstY = cy - ((metrics.lines.length - 1) * lineHeight) / 2 + 0.5;
  metrics.lines.forEach((line, i) => {
    const t = svgEl("text", {
      x: cx,
      y: firstY + i * lineHeight,
      "text-anchor": "middle",
      "dominant-baseline": "middle",
      fill: colors.text,
      "font-family": font,
      "font-size": "12.1",
      "font-weight": "620",
    });
    if (i === 0 && metrics.prefix) {
      const prefix = svgEl("tspan", { fill: colors.prefix, "font-weight": "760" });
      prefix.textContent = metrics.prefix;
      t.appendChild(prefix);
      const spacer = svgEl("tspan");
      spacer.textContent = " ";
      t.appendChild(spacer);
      const body = svgEl("tspan");
      body.textContent = line;
      t.appendChild(body);
    } else {
      t.textContent = line;
    }
    parent.appendChild(t);
  });
}

function drawFlowNode(parent, metrics, cx, cy, colors, font, opts = {}) {
  parent.appendChild(svgEl("rect", {
    x: cx - metrics.width / 2,
    y: cy - metrics.height / 2,
    width: metrics.width,
    height: metrics.height,
    rx: opts.primary ? 10 : 8,
    ry: opts.primary ? 10 : 8,
    fill: opts.primary ? colors.primaryBg : colors.nodeBg,
    stroke: opts.primary ? colors.primaryBorder : colors.border,
    "stroke-width": opts.primary ? "1.1" : "1",
    filter: "drop-shadow(0 5px 12px rgba(0,0,0,.08))",
  }));
  drawLinearNodeLabel(parent, metrics, cx, cy, colors, font);
}

function drawFlowEdgeLabel(parent, label, x, y, colors, font) {
  const text = cleanMermaidLabel(label);
  if (!text) return;
  const parts = Array.from(text);
  let lines = [text];
  if (parts.length > 15) {
    const chunks = text.split(/([/／、,，])/).reduce((acc, part) => {
      if (!part) return acc;
      if (/[/／、,，]/.test(part) && acc.length) acc[acc.length - 1] += part;
      else acc.push(part);
      return acc;
    }, []);
    const out = ["", ""];
    const targetLen = Math.ceil(parts.length / 2);
    chunks.forEach((chunk) => {
      const target = Array.from(out[0] + chunk).length <= targetLen || !out[0] ? 0 : 1;
      out[target] += chunk;
    });
    lines = out.map((line) => line.trim()).filter(Boolean);
  }
  const longest = Math.max(...lines.map((line) => Array.from(line).length));
  const w = Math.min(124, Math.max(54, longest * 7.4 + 18));
  const h = lines.length > 1 ? 32 : 22;
  parent.appendChild(svgEl("rect", {
    x: x - w / 2,
    y: y - h / 2,
    width: w,
    height: h,
    rx: 11,
    ry: 11,
    fill: colors.panel,
    stroke: colors.subtle,
    "stroke-width": "0.8",
    opacity: "0.86",
  }));
  const firstY = y - ((lines.length - 1) * 11) / 2 + 0.5;
  lines.forEach((line, i) => {
    svgText(parent, line, {
      x,
      y: firstY + i * 11,
      "text-anchor": "middle",
      "dominant-baseline": "middle",
      fill: colors.muted,
      "font-family": font,
      "font-size": "10.6",
      "font-weight": "560",
    });
  });
}

function flowConnectorPath(x1, y1, x2, y2) {
  if (Math.abs(y2 - y1) < 2) return `M ${x1} ${y1} H ${x2}`;
  const midX = Math.round((x1 + x2) / 2);
  const dir = y2 > y1 ? 1 : -1;
  const r = Math.min(10, Math.abs(y2 - y1) / 2, Math.abs(midX - x1) / 2, Math.abs(x2 - midX) / 2);
  return [
    `M ${x1} ${y1}`,
    `H ${midX - r}`,
    `Q ${midX} ${y1} ${midX} ${y1 + dir * r}`,
    `V ${y2 - dir * r}`,
    `Q ${midX} ${y2} ${midX + r} ${y2}`,
    `H ${x2}`,
  ].join(" ");
}

function renderLinearFlowchartSvg(reflow) {
  if (!reflow || !Array.isArray(reflow.groups)) return null;
  const panel = cssVar("--bg-panel", "#161618");
  const nodeBg = cssVar("--bg-element", "#1d1d20");
  const subtle = cssVar("--border-subtle", "#26262a");
  const border = cssVar("--border", "#3b3b41");
  const text = cssVar("--text", "#ececee");
  const muted = cssVar("--text-muted", "#9a9aa2");
  const dim = cssVar("--text-dim", "#787880");
  const accent = cssVar("--accent", "#e8b339");
  const font = '"AutoVerse CJK", -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif';
  const labels = new Map();
  reflow.groups.flatMap((g) => g.nodes).forEach((id) => labels.set(id, mermaidNodeLabel(id, reflow.defs)));
  const metrics = new Map([...labels.entries()].map(([id, label]) => [id, linearNodeMetrics(label)]));
  const nodeW = Math.max(...[...metrics.values()].map((m) => m.width));
  const rowGap = 12;
  const colGap = 34;
  const padX = 18;
  const padTop = 32;
  const padBottom = 18;
  const colW = nodeW + 34;
  const groupContentHeight = (group) =>
    group.nodes.reduce((sum, id) => sum + (metrics.get(id)?.height || 42), 0) +
    Math.max(0, group.nodes.length - 1) * rowGap;
  const minGroupContentH = 42 * 2 + rowGap;
  const maxContentH = Math.max(...reflow.groups.map((group) => Math.max(minGroupContentH, groupContentHeight(group))));
  const height = padTop + maxContentH + padBottom;
  const width = padX * 2 + reflow.groups.length * colW + Math.max(0, reflow.groups.length - 1) * colGap;
  const svg = svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`,
    xmlns: "http://www.w3.org/2000/svg",
    role: "img",
    class: "av-linear-flow-svg",
  });
  svg.style.overflow = "visible";
  const layer = svgEl("g", { class: "av-linear-flow" });
  svg.appendChild(layer);
  const edgeLabels = reflow.edgeLabels || new Map();
  const colors = { text, prefix: accent, panel, subtle, muted };

  const arrowHead = (x, y, angle, opts = {}) => {
    const size = 5;
    const a1 = angle + Math.PI * 0.82;
    const a2 = angle - Math.PI * 0.82;
    layer.appendChild(svgEl("path", {
      d: `M ${x} ${y} L ${x + Math.cos(a1) * size} ${y + Math.sin(a1) * size} M ${x} ${y} L ${x + Math.cos(a2) * size} ${y + Math.sin(a2) * size}`,
      fill: "none",
      stroke: dim,
      "stroke-width": "1.15",
      "stroke-linecap": "round",
      opacity: "0.66",
    }));
  };
  const line = (d, endX, endY, angle) => {
    layer.appendChild(svgEl("path", {
      d,
      fill: "none",
      stroke: dim,
      "stroke-width": "1.15",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      opacity: "0.66",
    }));
    arrowHead(endX, endY, angle);
  };
  const connectorPath = (x1, y1, x2, y2) => {
    if (Math.abs(y2 - y1) < 2) return `M ${x1} ${y1} H ${x2}`;
    const midX = Math.round((x1 + x2) / 2);
    const dir = y2 > y1 ? 1 : -1;
    const r = Math.min(10, Math.abs(y2 - y1) / 2, Math.abs(midX - x1) / 2, Math.abs(x2 - midX) / 2);
    return [
      `M ${x1} ${y1}`,
      `H ${midX - r}`,
      `Q ${midX} ${y1} ${midX} ${y1 + dir * r}`,
      `V ${y2 - dir * r}`,
      `Q ${midX} ${y2} ${midX + r} ${y2}`,
      `H ${x2}`,
    ].join(" ");
  };

  const centers = new Map();
  reflow.groups.forEach((group, col) => {
    const x = padX + col * (colW + colGap);
    const contentH = groupContentHeight(group);
    const groupH = padTop + Math.max(minGroupContentH, contentH) + 12;
    layer.appendChild(svgEl("rect", {
      x,
      y: 8,
      width: colW,
      height: groupH,
      rx: 13,
      ry: 13,
      fill: panel,
      stroke: subtle,
      "stroke-width": "1",
      "stroke-dasharray": "3 6",
      opacity: "0.42",
    }));
    if (group.title && group.title.trim()) {
      svgText(layer, group.title.trim(), {
        x: x + colW / 2,
        y: 25,
        "text-anchor": "middle",
        fill: muted,
        "font-family": font,
        "font-size": "12.5",
        "font-weight": "650",
      });
    }
    let cursorY = padTop;
    group.nodes.forEach((id, row) => {
      const label = labels.get(id) || id;
      const m = metrics.get(id) || linearNodeMetrics(label);
      const w = m.width;
      const nodeH = m.height;
      const cx = x + colW / 2;
      const cy = cursorY + nodeH / 2;
      centers.set(id, { x: cx, y: cy, w, h: nodeH });
      layer.appendChild(svgEl("rect", {
        x: cx - w / 2,
        y: cy - nodeH / 2,
        width: w,
        height: nodeH,
        rx: 8,
        ry: 8,
        fill: nodeBg,
        stroke: border,
        "stroke-width": "1",
        filter: "drop-shadow(0 5px 12px rgba(0,0,0,.08))",
      }));
      drawLinearNodeLabel(layer, m, cx, cy, colors, font);
      if (row > 0) {
        const prevId = group.nodes[row - 1];
        const prev = centers.get(prevId);
        line(`M ${prev.x} ${prev.y + prev.h / 2 + 5} V ${cy - nodeH / 2 - 5}`, cx, cy - nodeH / 2 - 5, Math.PI / 2);
        drawFlowEdgeLabel(layer, edgeLabels.get(`${prevId}->${id}`), cx + Math.min(w / 2 + 28, colW / 2 - 12), (prev.y + cy) / 2, colors, font);
      }
      cursorY += nodeH + rowGap;
    });
  });

  for (let col = 0; col < reflow.groups.length - 1; col++) {
    const fromId = reflow.groups[col].nodes[reflow.groups[col].nodes.length - 1];
    const toId = reflow.groups[col + 1].nodes[0];
    const from = centers.get(fromId);
    const to = centers.get(toId);
    if (!from || !to) continue;
    const x1 = from.x + from.w / 2 + 8;
    const y1 = from.y;
    const x2 = to.x - to.w / 2 - 8;
    const y2 = to.y;
    line(connectorPath(x1, y1, x2, y2), x2, y2, 0);
    drawFlowEdgeLabel(layer, edgeLabels.get(`${fromId}->${toId}`), (x1 + x2) / 2, Math.min(y1, y2) - 16, colors, font);
  }

  return svg;
}

function renderGroupedFlowchartSvg(reflow) {
  if (!reflow || !Array.isArray(reflow.groups)) return null;
  const panel = cssVar("--bg-panel", "#161618");
  const nodeBg = cssVar("--bg-element", "#1d1d20");
  const subtle = cssVar("--border-subtle", "#26262a");
  const border = cssVar("--border", "#3b3b41");
  const text = cssVar("--text", "#ececee");
  const muted = cssVar("--text-muted", "#9a9aa2");
  const dim = cssVar("--text-dim", "#787880");
  const accent = cssVar("--accent", "#e8b339");
  const accentWash = cssVar("--accent-wash", "#282216");
  const font = '"AutoVerse CJK", -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif';
  const colors = {
    text,
    muted,
    prefix: accent,
    panel,
    subtle,
    border,
    nodeBg,
    primaryBg: `color-mix(in srgb, ${accentWash} 54%, ${nodeBg})`,
    primaryBorder: `color-mix(in srgb, ${accent} 42%, ${border})`,
  };
  const metrics = new Map();
  reflow.groups.forEach((group) => {
    group.nodes.forEach((id) => metrics.set(id, archNodeMetrics(mermaidNodeLabel(id, reflow.defs))));
  });
  const out = new Map();
  reflow.edges.forEach(([from, to]) => {
    out.set(from, [...(out.get(from) || []), to]);
  });

  const titleH = 28;
  const nodeGapY = 12;
  const gridGapX = 18;
  const groupGap = 34;
  const padX = 18;
  const padY = 16;
  const groupLayouts = [];

  const layoutGroup = (group) => {
    const nodes = group.nodes.filter((id) => metrics.has(id));
    const internal = (id) => (out.get(id) || []).filter((to) => reflow.nodeGroups.get(to) === group.id);
    const hub = nodes
      .map((id) => ({ id, count: internal(id).length }))
      .filter((item) => item.count >= 3)
      .sort((a, b) => b.count - a.count)[0]?.id || "";
    const positions = new Map();
    const maxW = Math.max(118, ...nodes.map((id) => metrics.get(id)?.width || 118));
    if (hub) {
      const children = internal(hub);
      const rest = nodes.filter((id) => id !== hub && !children.includes(id));
      const cols = children.length >= 4 ? 2 : 1;
      const rows = Math.ceil(children.length / cols);
      const groupW = Math.max(190, cols * maxW + Math.max(0, cols - 1) * gridGapX + 34);
      let y = titleH + 24;
      positions.set(hub, { x: groupW / 2, y, primary: true });
      y += (metrics.get(hub)?.height || 42) / 2 + 36;
      children.forEach((id, i) => {
        const row = Math.floor(i / cols);
        const col = i % cols;
        const x = cols === 1 ? groupW / 2 : 17 + maxW / 2 + col * (maxW + gridGapX);
        positions.set(id, { x, y: y + row * (42 + nodeGapY) + (metrics.get(id)?.height || 42) / 2 });
      });
      const childrenBottom = y + rows * 42 + Math.max(0, rows - 1) * nodeGapY;
      let restY = childrenBottom + (rest.length ? 18 : 0);
      rest.forEach((id) => {
        positions.set(id, { x: groupW / 2, y: restY + (metrics.get(id)?.height || 42) / 2 });
        restY += (metrics.get(id)?.height || 42) + nodeGapY;
      });
      const groupH = Math.max(childrenBottom, restY) + 18;
      return { group, positions, width: groupW, height: groupH, hub };
    }
    const internalEdges = reflow.edges.filter(([from, to]) => reflow.nodeGroups.get(from) === group.id && reflow.nodeGroups.get(to) === group.id);
    const grid = nodes.length >= 5 && internalEdges.length === 0;
    if (grid) {
      const cols = 2;
      const rows = Math.ceil(nodes.length / cols);
      const groupW = Math.max(210, cols * maxW + gridGapX + 34);
      nodes.forEach((id, i) => {
        const row = Math.floor(i / cols);
        const col = i % cols;
        positions.set(id, {
          x: 17 + maxW / 2 + col * (maxW + gridGapX),
          y: titleH + 20 + row * (42 + nodeGapY) + (metrics.get(id)?.height || 42) / 2,
        });
      });
      return { group, positions, width: groupW, height: titleH + 22 + rows * 42 + Math.max(0, rows - 1) * nodeGapY + 16, hub: "" };
    }
    const groupW = Math.max(176, maxW + 34);
    let y = titleH + 14;
    nodes.forEach((id) => {
      const h = metrics.get(id)?.height || 42;
      positions.set(id, { x: groupW / 2, y: y + h / 2 });
      y += h + nodeGapY;
    });
    return { group, positions, width: groupW, height: y - nodeGapY + 18, hub: "" };
  };

  reflow.groups.forEach((group) => groupLayouts.push(layoutGroup(group)));
  let cursorX = padX;
  groupLayouts.forEach((layout) => {
    layout.x = cursorX;
    layout.y = padY;
    cursorX += layout.width + groupGap;
  });
  const width = Math.round(cursorX - groupGap + padX);
  const height = Math.round(Math.max(...groupLayouts.map((g) => g.height)) + padY * 2);
  const svg = svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`,
    xmlns: "http://www.w3.org/2000/svg",
    role: "img",
    class: "av-linear-flow-svg av-arch-flow-svg",
  });
  svg.style.overflow = "visible";
  const bgLayer = svgEl("g", { class: "av-arch-bg" });
  const edgeLayer = svgEl("g", { class: "av-arch-edges" });
  const nodeLayer = svgEl("g", { class: "av-arch-nodes" });
  const labelLayer = svgEl("g", { class: "av-arch-labels" });
  svg.appendChild(bgLayer);
  svg.appendChild(edgeLayer);
  svg.appendChild(nodeLayer);
  svg.appendChild(labelLayer);

  const layoutByGroup = new Map(groupLayouts.map((layout) => [layout.group.id, layout]));
  const nodeCenters = new Map();
  groupLayouts.forEach((layout) => {
    bgLayer.appendChild(svgEl("rect", {
      x: layout.x,
      y: layout.y,
      width: layout.width,
      height: layout.height,
      rx: 14,
      ry: 14,
      fill: panel,
      stroke: subtle,
      "stroke-width": "1",
      "stroke-dasharray": "3 6",
      opacity: "0.42",
    }));
    svgText(bgLayer, layout.group.title, {
      x: layout.x + layout.width / 2,
      y: layout.y + 20,
      "text-anchor": "middle",
      fill: muted,
      "font-family": font,
      "font-size": "12.4",
      "font-weight": "660",
    });
    layout.group.nodes.forEach((id) => {
      const pos = layout.positions.get(id);
      const m = metrics.get(id);
      if (!pos || !m) return;
      const cx = layout.x + pos.x;
      const cy = layout.y + pos.y;
      nodeCenters.set(id, { x: cx, y: cy, w: m.width, h: m.height, group: layout.group.id, primary: Boolean(pos.primary) });
    });
  });

  const arrowHead = (x, y, angle, opts = {}) => {
    const size = 5;
    const a1 = angle + Math.PI * 0.82;
    const a2 = angle - Math.PI * 0.82;
    edgeLayer.appendChild(svgEl("path", {
      d: `M ${x} ${y} L ${x + Math.cos(a1) * size} ${y + Math.sin(a1) * size} M ${x} ${y} L ${x + Math.cos(a2) * size} ${y + Math.sin(a2) * size}`,
      fill: "none",
      stroke: dim,
      "stroke-width": opts.subtle ? "0.95" : "1.1",
      "stroke-linecap": "round",
      opacity: opts.subtle ? "0.44" : "0.58",
    }));
  };
  const drawEdge = (d, endX, endY, angle, opts = {}) => {
    edgeLayer.appendChild(svgEl("path", {
      d,
      fill: "none",
      stroke: dim,
      "stroke-width": opts.subtle ? "0.95" : "1.1",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      opacity: opts.subtle ? "0.44" : "0.58",
    }));
    arrowHead(endX, endY, angle, opts);
  };
  const groupEntry = (id) => {
    const layout = layoutByGroup.get(id);
    if (!layout) return null;
    return { x: layout.x - 8, y: layout.y + Math.min(72, layout.height / 2), w: 0, h: 0, group: id };
  };

  reflow.edges.forEach(([fromId, toId]) => {
    const from = nodeCenters.get(fromId);
    const to = nodeCenters.get(toId) || groupEntry(toId);
    if (!from || !to) return;
    const label = reflow.edgeLabels.get(`${fromId}->${toId}`);
    if (from.group === to.group && nodeCenters.has(toId)) {
      const labelTooLong = Array.from(cleanMermaidLabel(label || "")).length > 14;
      if (from.primary && to.y > from.y && !label) {
        return;
      }
      if (Math.abs(to.y - from.y) < 3) {
        const x1 = from.x + from.w / 2 + 7;
        const y1 = from.y;
        const x2 = to.x - to.w / 2 - 7;
        const y2 = to.y;
        drawEdge(flowConnectorPath(x1, y1, x2, y2), x2, y2, 0);
        if (!labelTooLong) {
          drawFlowEdgeLabel(labelLayer, label, (x1 + x2) / 2, Math.min(from.y - from.h / 2, to.y - to.h / 2) - 18, colors, font);
        }
      } else if (to.y > from.y) {
        const x1 = from.x;
        const y1 = from.y + from.h / 2 + 5;
        const x2 = to.x;
        const y2 = to.y - to.h / 2 - 5;
        const midY = Math.round((y1 + y2) / 2);
        drawEdge(`M ${x1} ${y1} V ${midY} H ${x2} V ${y2}`, x2, y2, Math.PI / 2);
        if (!labelTooLong) {
          drawFlowEdgeLabel(labelLayer, label, (x1 + x2) / 2, midY - 12, colors, font);
        }
      }
      return;
    }
    const x1 = from.x + from.w / 2 + 8;
    const y1 = from.y;
    const x2 = to.x - (to.w ? to.w / 2 : 0) - 8;
    const y2 = to.y;
    if (x2 < x1) {
      const sx = from.x - from.w / 2 - 7;
      const sy = from.y;
      const ex = to.x + to.w / 2 + 7;
      const ey = to.y;
      const bendX = Math.round(ex + Math.min(28, Math.max(14, (sx - ex) * 0.22)));
      drawEdge(`M ${sx} ${sy} H ${bendX} V ${ey} H ${ex}`, ex, ey, Math.PI, { subtle: true });
    } else {
      if (Math.abs(y2 - y1) < 18) {
        drawEdge(`M ${x1} ${y1} H ${x2}`, x2, y1, 0);
      } else {
        drawEdge(flowConnectorPath(x1, y1, x2, y2), x2, y2, 0);
      }
    }
    const labelY = nodeCenters.has(toId)
      ? Math.min(from.y - from.h / 2, to.y - to.h / 2) - 12
      : Math.min(from.y - from.h / 2, to.y) - 12;
    drawFlowEdgeLabel(labelLayer, label, (x1 + x2) / 2, labelY, colors, font);
  });

  groupLayouts.forEach((layout) => {
    layout.group.nodes.forEach((id) => {
      const center = nodeCenters.get(id);
      const m = metrics.get(id);
      if (!center || !m) return;
      drawFlowNode(nodeLayer, m, center.x, center.y, colors, font, { primary: center.primary });
    });
  });
  return svg;
}

function renderGenericFlowchartSvg(reflow) {
  if (!reflow || !Array.isArray(reflow.nodes)) return null;
  const panel = cssVar("--bg-panel", "#161618");
  const nodeBg = cssVar("--bg-element", "#1d1d20");
  const subtle = cssVar("--border-subtle", "#26262a");
  const border = cssVar("--border", "#3b3b41");
  const text = cssVar("--text", "#ececee");
  const muted = cssVar("--text-muted", "#9a9aa2");
  const dim = cssVar("--text-dim", "#787880");
  const accent = cssVar("--accent", "#e8b339");
  const accentWash = cssVar("--accent-wash", "#282216");
  const font = '"AutoVerse CJK", -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif';
  const colors = {
    text,
    muted,
    prefix: accent,
    panel,
    subtle,
    border,
    nodeBg,
    primaryBg: `color-mix(in srgb, ${accentWash} 48%, ${nodeBg})`,
    primaryBorder: `color-mix(in srgb, ${accent} 36%, ${border})`,
  };
  const metrics = new Map(reflow.nodes.map((id) => [id, archNodeMetrics(mermaidNodeLabel(id, reflow.defs))]));
  const incoming = new Map();
  const outgoing = new Map();
  reflow.nodes.forEach((id) => {
    incoming.set(id, []);
    outgoing.set(id, []);
  });
  reflow.edges.forEach(([from, to]) => {
    if (!incoming.has(to) || !outgoing.has(from)) return;
    incoming.get(to).push(from);
    outgoing.get(from).push(to);
  });

  const rank = new Map();
  const queue = reflow.nodes.filter((id) => !(incoming.get(id) || []).length);
  (queue.length ? queue : [reflow.nodes[0]]).forEach((id) => rank.set(id, 0));
  for (let guard = 0; guard < reflow.nodes.length * 4; guard++) {
    let changed = false;
    reflow.edges.forEach(([from, to]) => {
      if (!rank.has(from)) return;
      const next = rank.get(from) + 1;
      if (!rank.has(to) || rank.get(to) < next) {
        rank.set(to, next);
        changed = true;
      }
    });
    if (!changed) break;
  }
  reflow.nodes.forEach((id) => {
    if (!rank.has(id)) rank.set(id, 0);
  });

  const layers = [];
  reflow.nodes.forEach((id) => {
    const r = Math.min(rank.get(id) || 0, reflow.nodes.length - 1);
    if (!layers[r]) layers[r] = [];
    layers[r].push(id);
  });
  const compactLayers = layers.filter(Boolean);
  const vertical = !/^(LR|RL)$/i.test(reflow.dir || "TD");
  const nodeGapX = 28;
  const nodeGapY = 28;
  const rankGap = 56;
  const pad = 24;

  const layerSize = (ids) => {
    const widths = ids.map((id) => metrics.get(id)?.width || 120);
    const heights = ids.map((id) => metrics.get(id)?.height || 42);
    return {
      w: widths.reduce((a, b) => a + b, 0) + Math.max(0, ids.length - 1) * nodeGapX,
      h: Math.max(42, ...heights),
      colW: Math.max(120, ...widths),
      colH: heights.reduce((a, b) => a + b, 0) + Math.max(0, ids.length - 1) * nodeGapY,
    };
  };
  const sizes = compactLayers.map(layerSize);
  const width = vertical
    ? Math.max(...sizes.map((s) => s.w), 180) + pad * 2
    : sizes.reduce((sum, s) => sum + s.colW, 0) + Math.max(0, sizes.length - 1) * rankGap + pad * 2;
  const height = vertical
    ? sizes.reduce((sum, s) => sum + s.h, 0) + Math.max(0, sizes.length - 1) * rankGap + pad * 2
    : Math.max(...sizes.map((s) => s.colH), 80) + pad * 2;
  const svg = svgEl("svg", {
    viewBox: `0 0 ${Math.round(width)} ${Math.round(height)}`,
    xmlns: "http://www.w3.org/2000/svg",
    role: "img",
    class: "av-linear-flow-svg av-generic-flow-svg",
  });
  svg.style.overflow = "visible";
  const edgeLayer = svgEl("g", { class: "av-generic-edges" });
  const nodeLayer = svgEl("g", { class: "av-generic-nodes" });
  const labelLayer = svgEl("g", { class: "av-generic-labels" });
  svg.appendChild(edgeLayer);
  svg.appendChild(nodeLayer);
  svg.appendChild(labelLayer);

  const centers = new Map();
  if (vertical) {
    let y = pad;
    compactLayers.forEach((ids, layerIdx) => {
      const s = sizes[layerIdx];
      let x = (width - s.w) / 2;
      ids.forEach((id) => {
        const m = metrics.get(id);
        centers.set(id, { x: x + m.width / 2, y: y + s.h / 2, w: m.width, h: m.height });
        x += m.width + nodeGapX;
      });
      y += s.h + rankGap;
    });
  } else {
    let x = pad;
    compactLayers.forEach((ids, layerIdx) => {
      const s = sizes[layerIdx];
      let y = (height - s.colH) / 2;
      ids.forEach((id) => {
        const m = metrics.get(id);
        centers.set(id, { x: x + s.colW / 2, y: y + m.height / 2, w: m.width, h: m.height });
        y += m.height + nodeGapY;
      });
      x += s.colW + rankGap;
    });
  }

  const arrowHead = (x, y, angle) => {
    const size = 5;
    const a1 = angle + Math.PI * 0.82;
    const a2 = angle - Math.PI * 0.82;
    edgeLayer.appendChild(svgEl("path", {
      d: `M ${x} ${y} L ${x + Math.cos(a1) * size} ${y + Math.sin(a1) * size} M ${x} ${y} L ${x + Math.cos(a2) * size} ${y + Math.sin(a2) * size}`,
      fill: "none",
      stroke: dim,
      "stroke-width": "1.12",
      "stroke-linecap": "round",
      opacity: "0.64",
    }));
  };
  const drawEdge = (d, x, y, angle) => {
    edgeLayer.appendChild(svgEl("path", {
      d,
      fill: "none",
      stroke: dim,
      "stroke-width": "1.12",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      opacity: "0.64",
    }));
    arrowHead(x, y, angle);
  };
  reflow.edges.forEach(([fromId, toId]) => {
    const from = centers.get(fromId);
    const to = centers.get(toId);
    if (!from || !to) return;
    const label = reflow.edgeLabels.get(`${fromId}->${toId}`);
    if (vertical) {
      const x1 = from.x;
      const y1 = from.y + from.h / 2 + 6;
      const x2 = to.x;
      const y2 = to.y - to.h / 2 - 6;
      const midY = (y1 + y2) / 2;
      drawEdge(`M ${x1} ${y1} V ${midY} H ${x2} V ${y2}`, x2, y2, Math.PI / 2);
      drawFlowEdgeLabel(labelLayer, label, (x1 + x2) / 2, midY - 12, colors, font);
    } else {
      const x1 = from.x + from.w / 2 + 7;
      const y1 = from.y;
      const x2 = to.x - to.w / 2 - 7;
      const y2 = to.y;
      drawEdge(flowConnectorPath(x1, y1, x2, y2), x2, y2, 0);
      drawFlowEdgeLabel(labelLayer, label, (x1 + x2) / 2, Math.min(y1, y2) - 16, colors, font);
    }
  });
  reflow.nodes.forEach((id) => {
    const c = centers.get(id);
    const m = metrics.get(id);
    if (!c || !m) return;
    const primary = (outgoing.get(id) || []).length > 1 || !(incoming.get(id) || []).length;
    drawFlowNode(nodeLayer, m, c.x, c.y, colors, font, { primary });
  });
  return svg;
}

function flowchartNodeForId(svg, id) {
  return [...svg.querySelectorAll(".node")].find((node) =>
    node.id === id || node.id.includes(`-${id}-`) || node.id.endsWith(`-${id}`)
  );
}

function applyLinearFlowchartColumnLayout(svg, groups) {
  if (!Array.isArray(groups) || groups.length < 2) return false;
  const nodeMap = new Map();
  groups.flatMap((group) => group.nodes).forEach((id) => {
    const node = flowchartNodeForId(svg, id);
    if (node) nodeMap.set(id, node);
  });
  const allIds = groups.flatMap((group) => group.nodes);
  if (nodeMap.size !== allIds.length) return false;

  const boxes = allIds.map((id) => {
    const node = nodeMap.get(id);
    try {
      const box = node.getBBox();
      return { id, width: box.width || 100, height: box.height || 42 };
    } catch {
      return { id, width: 100, height: 42 };
    }
  });
  const maxNodeW = Math.max(96, ...boxes.map((b) => b.width));
  const maxNodeH = Math.max(42, ...boxes.map((b) => b.height));
  const maxRows = Math.max(...groups.map((group) => group.nodes.length));
  const colW = Math.max(320, maxNodeW + 140);
  const rowStep = Math.max(96, maxNodeH + 42);
  const padX = 34;
  const padTop = 48;
  const padBottom = 38;
  const totalW = Math.round(groups.length * colW + padX * 2);
  const totalH = Math.round(padTop + maxRows * rowStep + padBottom);
  const lineColor = cssVar("--text-dim", "#787880");
  const panel = cssVar("--bg-panel", "#161618");
  const subtle = cssVar("--border-subtle", "#26262a");
  const text = cssVar("--text-muted", "#9a9aa2");

  svg.querySelectorAll(".edgePath, .flowchart-link, .clusters, .cluster, .cluster-label").forEach((node) => {
    node.style.display = "none";
  });

  const layer = svgEl("g", { class: "av-linear-reflow-layer" });
  const defs = svg.querySelector("defs");
  const firstDrawable = defs ? defs.nextSibling : svg.firstChild;
  svg.insertBefore(layer, firstDrawable || null);

  const arrowHead = (x, y, angle) => {
    const size = 6;
    const a1 = angle + Math.PI * 0.82;
    const a2 = angle - Math.PI * 0.82;
    const head = svgEl("path", {
      d: `M ${x} ${y} L ${x + Math.cos(a1) * size} ${y + Math.sin(a1) * size} M ${x} ${y} L ${x + Math.cos(a2) * size} ${y + Math.sin(a2) * size}`,
      fill: "none",
      stroke: lineColor,
      "stroke-width": "1.2",
      "stroke-linecap": "round",
      opacity: "0.72",
    });
    layer.appendChild(head);
  };

  const arrow = (x1, y1, x2, y2) => {
    const path = svgEl("path", {
      d: `M ${x1} ${y1} L ${x2} ${y2}`,
      fill: "none",
      stroke: lineColor,
      "stroke-width": "1.2",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      opacity: "0.72",
    });
    layer.appendChild(path);
    arrowHead(x2, y2, Math.atan2(y2 - y1, x2 - x1));
  };

  groups.forEach((group, col) => {
    const left = padX + col * colW;
    const centerX = left + colW / 2;
    const height = padTop + group.nodes.length * rowStep + 8;
    const rect = svgEl("rect", {
      x: left + 8,
      y: 8,
      width: colW - 16,
      height,
      rx: 14,
      ry: 14,
      fill: panel,
      stroke: subtle,
      "stroke-width": "1",
      "stroke-dasharray": "4 5",
      opacity: "0.76",
    });
    layer.appendChild(rect);
    if (group.title.trim()) {
      const title = svgEl("text", {
        x: centerX,
        y: 27,
        "text-anchor": "middle",
        fill: text,
        "font-size": "13",
        "font-weight": "650",
      });
      title.textContent = group.title;
      layer.appendChild(title);
    }
    group.nodes.forEach((id, row) => {
      const node = nodeMap.get(id);
      const y = padTop + row * rowStep + maxNodeH / 2;
      node.setAttribute("transform", `translate(${centerX}, ${y})`);
      if (row > 0) arrow(centerX, y - rowStep + maxNodeH / 2 + 10, centerX, y - maxNodeH / 2 - 10);
    });
  });

  for (let col = 0; col < groups.length - 1; col++) {
    const from = groups[col].nodes[groups[col].nodes.length - 1];
    const to = groups[col + 1].nodes[0];
    const fromBox = boxes.find((b) => b.id === from) || { width: maxNodeW, height: maxNodeH };
    const toBox = boxes.find((b) => b.id === to) || { width: maxNodeW, height: maxNodeH };
    const x1 = padX + col * colW + colW / 2 + fromBox.width / 2 + 12;
    const y1 = padTop + (groups[col].nodes.length - 1) * rowStep + maxNodeH / 2;
    const x2 = padX + (col + 1) * colW + colW / 2 - toBox.width / 2 - 12;
    const y2 = padTop + maxNodeH / 2;
    const midX = (x1 + x2) / 2;
    const path = svgEl("path", {
      d: `M ${x1} ${y1} H ${midX} V ${y2} H ${x2}`,
      fill: "none",
      stroke: lineColor,
      "stroke-width": "1.2",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      opacity: "0.72",
    });
    layer.appendChild(path);
    arrowHead(x2, y2, 0);
  }

  allIds.forEach((id) => {
    const node = nodeMap.get(id);
    if (node) layer.appendChild(node);
  });

  svg.setAttribute("viewBox", `0 0 ${totalW} ${totalH}`);
  svg.removeAttribute("height");
  svg.removeAttribute("width");
  return true;
}

async function loadMermaid() {
  if (_mermaidMod) return _mermaidMod;
  if (!_mermaidPromise) {
    _mermaidPromise = import("https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs")
      .catch(() => import("https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs"))
      .then((mod) => {
        _mermaidMod = mod.default || mod;
        return _mermaidMod;
      });
  }
  return _mermaidPromise;
}

function initMermaid(mermaid) {
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    theme: "base",
    layout: "dagre",
    look: "classic",
    themeVariables: mermaidThemeVars(),
    flowchart: {
      htmlLabels: false,
      useMaxWidth: false,
      curve: "linear",
      nodeSpacing: 52,
      rankSpacing: 70,
      padding: 28,
      wrappingWidth: 168,
    },
    sequence: {
      mirrorActors: false,
      useMaxWidth: false,
      boxMargin: 12,
      boxTextMargin: 7,
      messageMargin: 38,
      actorMargin: 58,
      bottomMarginAdj: 8,
      noteMargin: 12,
      rightAngles: false,
    },
    state: {
      useMaxWidth: false,
    },
  });
}

function mermaidNaturalSize(svg) {
  const vb = svg.viewBox && svg.viewBox.baseVal;
  if (vb && vb.width && vb.height) return { width: vb.width, height: vb.height };
  const w = parseFloat(svg.getAttribute("width") || "0");
  const h = parseFloat(svg.getAttribute("height") || "0");
  return { width: w || 800, height: h || 400 };
}

function applyMermaidZoom(wrap, mode = "fit") {
  const stage = wrap.querySelector(".mermaid-stage");
  const svg = wrap.querySelector(".mermaid-diagram svg");
  if (!stage || !svg) return;
  const size = mermaidNaturalSize(svg);
  const customFlow = wrap.classList.contains("mermaid-flow-custom");
  if (customFlow) {
    const parentAvailable = Math.max(320, (wrap.parentElement?.clientWidth || stage.clientWidth || size.width) - 28);
    let scale = 1;
    if (mode !== "actual") scale = Math.min(1, parentAvailable / Math.max(size.width, 1));
    svg.style.width = `${Math.round(size.width * scale)}px`;
    svg.style.height = `${Math.round(size.height * scale)}px`;
    wrap.dataset.zoom = mode;
    wrap.classList.remove("mermaid-fit-page", "mermaid-fit-scroll", "mermaid-large");
    wrap.querySelectorAll(".mermaid-toolbar button").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.zoom === mode);
    });
    return;
  }
  const available = Math.max(320, stage.clientWidth - 20);
  const tallNarrow = size.height / Math.max(size.width, 1) > 1.65 && size.width < available * 0.72;
  // Preview mode should make the whole shape legible as a diagram, not sprawl
  // across the chat. Small and medium diagrams can grow a little; 100% remains
  // the exact detail-reading mode with scrolling.
  const widthFit = available / Math.max(size.width, 1);
  const heightLimit = tallNarrow
    ? Math.max(420, Math.min(640, window.innerHeight * 0.68))
    : Math.max(360, Math.min(560, window.innerHeight * 0.58));
  const heightFit = heightLimit / Math.max(size.height, 1);
  let scale = 1;
  if (mode !== "actual") {
    if (tallNarrow) {
      scale = Math.min(1.08, widthFit, heightFit);
      scale = Math.max(0.34, scale);
    } else {
      scale = Math.min(1.14, widthFit);
      if (size.height > heightLimit * 1.15) scale = Math.min(scale, Math.max(0.62, heightFit));
      scale = Math.max(0.64, scale);
    }
  }
  svg.style.width = `${Math.round(size.width * scale)}px`;
  svg.style.height = `${Math.round(size.height * scale)}px`;
  wrap.dataset.zoom = mode;
  wrap.classList.toggle("mermaid-fit-page", mode !== "actual" && tallNarrow);
  wrap.classList.toggle("mermaid-fit-scroll", mode !== "actual" && widthFit < 0.64);
  wrap.classList.toggle("mermaid-large", !tallNarrow && (size.width > available * 1.6 || size.height > 620));
  wrap.querySelectorAll(".mermaid-toolbar button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.zoom === mode);
  });
}

function centerMermaidNodeLabels(svg) {
  svg.querySelectorAll(".node").forEach((node) => {
    const shape = node.querySelector("rect, polygon, circle, ellipse");
    const label = node.querySelector(".label, .nodeLabel");
    if (!shape || !label || typeof shape.getBBox !== "function" || typeof label.getBBox !== "function") return;
    try {
      const sb = shape.getBBox();
      const lb = label.getBBox();
      if (!sb.width || !sb.height || !lb.height) return;
      const dy = (sb.y + sb.height / 2) - (lb.y + lb.height / 2);
      if (Math.abs(dy) < 0.35 || Math.abs(dy) > 14) return;
      label.style.transformBox = "fill-box";
      label.style.transformOrigin = "center";
      label.style.transform = `translateY(${dy.toFixed(2)}px)`;
    } catch {
      // Some SVG nodes do not expose a useful bbox until after paint.
    }
  });
}

function tuneMermaidSvg(svg) {
  // Mermaid optimizes for generic diagrams, not this chat surface. The rendered
  // SVG needs a small typography pass so node labels sit optically centered and
  // edge furniture doesn't overpower the boxes.
  svg.querySelectorAll(".node").forEach((node) => {
    const label = node.querySelector(".label, .nodeLabel");
    if (label && !label.dataset.tuned) {
      label.dataset.tuned = "1";
      label.setAttribute("dominant-baseline", "middle");
    }
    node.querySelectorAll("text").forEach((text) => {
      text.setAttribute("dominant-baseline", "middle");
      text.setAttribute("text-anchor", "middle");
    });
  });

  svg.querySelectorAll(".edgePath .path, .flowchart-link").forEach((path) => {
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
  });

  svg.querySelectorAll(".edgeLabel rect, .labelBkg").forEach((rect) => {
    rect.setAttribute("rx", "8");
    rect.setAttribute("ry", "8");
  });

  svg.querySelectorAll(".actor, .note, .activation0, .activation1, .activation2").forEach((shape) => {
    shape.setAttribute("rx", "8");
    shape.setAttribute("ry", "8");
  });

  const pieColors = [
    cssVar("--accent", "#e8b339"),
    cssVar("--accent-cool", "#8eccaa"),
    cssVar("--success", "#74c09a"),
    cssVar("--warning", "#e0a458"),
    cssVar("--secondary", "#c08a6a"),
    cssVar("--error", "#d16b6b"),
  ];
  svg.querySelectorAll(".pieCircle").forEach((slice, i) => {
    const color = pieColors[i % pieColors.length];
    slice.style.fill = color;
    slice.style.stroke = cssVar("--bg-panel", "#161618");
    slice.style.strokeWidth = "2px";
  });
  svg.querySelectorAll(".legend rect").forEach((rect, i) => {
    const color = pieColors[i % pieColors.length];
    rect.style.fill = color;
    rect.style.stroke = color;
    rect.setAttribute("rx", "2");
    rect.setAttribute("ry", "2");
  });

  const gitColors = [
    cssVar("--text-muted", "#9a9aa2"),
    cssVar("--accent", "#e8b339"),
    cssVar("--accent-cool", "#8eccaa"),
    cssVar("--success", "#74c09a"),
  ];
  gitColors.forEach((color, i) => {
    svg.querySelectorAll(`.branch${i}, .arrow${i}`).forEach((shape) => {
      shape.style.stroke = color;
      shape.style.strokeWidth = "3px";
      shape.style.fill = "none";
      shape.setAttribute("stroke-linecap", "round");
      shape.setAttribute("stroke-linejoin", "round");
    });
    svg.querySelectorAll(`.commit${i}`).forEach((node) => {
      node.style.fill = color;
      node.style.stroke = cssVar("--bg-panel", "#161618");
      node.style.strokeWidth = "2px";
    });
    svg.querySelectorAll(`.label${i}`).forEach((label) => {
      label.style.fill = cssVar("--bg-element", "#1d1d20");
      label.style.stroke = cssVar("--border-subtle", "#26262a");
    });
  });
  svg.querySelectorAll(".commit-label-bkg").forEach((label) => {
    label.style.fill = cssVar("--bg-panel", "#161618");
    label.style.stroke = "transparent";
    label.setAttribute("rx", "5");
    label.setAttribute("ry", "5");
  });
}

function bindMermaidToolbar(wrap) {
  if (wrap.dataset.toolbarBound) return;
  wrap.dataset.toolbarBound = "1";
  wrap.querySelector(".mermaid-toolbar")?.addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-zoom]");
    if (!btn) return;
    ev.preventDefault();
    applyMermaidZoom(wrap, btn.dataset.zoom || "fit");
  });
}

function scheduleMermaidRender(root = document, token = _mermaidRenderToken) {
  return;
}

function rerenderMermaid() {
  return;
}

function normalizeDiffHtml(html) {
  const raw = String(html || "");
  if (raw.includes('class="diff"')) return raw;

  // Old persisted blocks contain Rich's dark terminal palette as inline
  // styles. Rebuild them from text so the active browser theme owns all colour.
  const tmp = document.createElement("div");
  tmp.innerHTML = raw;
  const rows = [];
  for (const source of (tmp.textContent || "").split("\n")) {
    const stripped = source.trim();
    if (!stripped) continue;
    if (stripped.startsWith("@@")) {
      rows.push(`<div class="row hunk"><span class="cd">${esc(stripped)}</span></div>`);
      continue;
    }
    if (/^\d+$/.test(stripped)) {
      rows.push(`<div class="row ctx"><span class="ln">${esc(stripped)}</span>` +
        `<span class="sg"> </span><span class="cd"></span></div>`);
      continue;
    }
    const m = stripped.match(/^(\d+)\s([+\- ])\s?(.*)$/);
    if (!m) {
      rows.push(`<div class="row ctx"><span class="cd">${esc(stripped)}</span></div>`);
      continue;
    }
    const cls = m[2] === "+" ? "add" : m[2] === "-" ? "del" : "ctx";
    rows.push(`<div class="row ${cls}"><span class="ln">${esc(m[1])}</span>` +
      `<span class="sg">${esc(m[2])}</span><span class="cd">${esc(m[3].trimEnd())}</span></div>`);
  }
  return `<div class="diff">${rows.join("")}</div>`;
}

function diffStats(diffRoot) {
  return {
    added: diffRoot ? diffRoot.querySelectorAll(".row.add").length : 0,
    removed: diffRoot ? diffRoot.querySelectorAll(".row.del").length : 0,
  };
}

// ── image lightbox ───────────────────────────────────────────────────────────
function openImageLightbox(src, label = "attached image") {
  const box = $("imageLightbox");
  const img = $("imageLightboxImg");
  const cap = $("imageLightboxCaption");
  if (!box || !img) return;
  img.src = src || "";
  img.alt = label || "attached image";
  if (cap) cap.textContent = label || "";
  box.hidden = false;
  box.setAttribute("aria-hidden", "false");
  box.querySelector("button")?.focus();
}

function closeImageLightbox() {
  const box = $("imageLightbox");
  const img = $("imageLightboxImg");
  if (!box || box.hidden) return false;
  box.hidden = true;
  box.setAttribute("aria-hidden", "true");
  if (img) img.src = "";
  return true;
}

function attachmentImageSrc(img) {
  if (!img || typeof img !== "object") return "";
  if (img.resource_url) return String(img.resource_url);
  if (img.source_type === "url") return String(img.data || "");
  const media = img.media_type || "image/png";
  return `data:${media};base64,${img.data || ""}`;
}

// ── block → element ──────────────────────────────────────────────────────────
function buildBlock(b) {
  const k = b.kind;
  if (k === "user") {
    const mode = ["normal", "plan", "worktree"].includes(b.mode) ? b.mode : "normal";
    const e = el(`<div class="user-echo mode-${mode}"></div>`);
    const body = el(`<div class="user-text"></div>`);
    body.textContent = b.text || "";
    e.appendChild(body);
    const attachments = Array.isArray(b.attachments) ? b.attachments : [];
    if (attachments.length) {
      const strip = el(`<div class="user-images"></div>`);
      attachments.forEach((img) => {
        const src = attachmentImageSrc(img);
        if (!src) return;
        const node = el(`<img alt="attached image" loading="lazy" />`);
        node.src = src;
        node.tabIndex = 0;
        node.title = "点击放大图片";
        node.addEventListener("click", () => openImageLightbox(src, "会话图片"));
        node.addEventListener("keydown", (ev) => {
          if (ev.key !== "Enter" && ev.key !== " ") return;
          ev.preventDefault();
          openImageLightbox(src, "会话图片");
        });
        strip.appendChild(node);
      });
      e.appendChild(strip);
    }
    if (b.rewind_id) {
      const actions = el(`<div class="user-actions"></div>`);
      actions.innerHTML =
        `<button type="button" class="user-more" data-turn-menu aria-label="指令操作" ` +
        `aria-haspopup="menu" aria-expanded="false">•••</button>` +
        `<div class="user-action-menu" role="menu">` +
        `<button type="button" role="menuitem" data-turn-action="edit" ` +
        `data-rewind-id="${esc(b.rewind_id)}">✎ 修改</button>` +
        `<button type="button" role="menuitem" class="undo" data-turn-action="undo" ` +
        `data-rewind-id="${esc(b.rewind_id)}">↶ 撤销</button></div>`;
      e.appendChild(actions);
    }
    return e;
  }
  if (k === "markdown") {
    const e = el(`<div class="blk md"></div>`);
    e.innerHTML = mdToHtml(b.md) + (b.streaming ? `<span class="cursor">▏</span>` : "");
    return e;
  }
  if (k === "line") {
    const e = el(`<div class="blk inline"></div>`);
    if (b.cls) {
      // cls may carry multiple classes (e.g. "inline tool" for single-line
      // tool cards) — apply each separately instead of a single token.
      const want = String(b.cls).split(/\s+/).filter(Boolean);
      if (want.includes("inline")) {
        for (const c of want) if (c !== "inline") e.classList.add(c);
      } else {
        e.classList.remove("inline");
        for (const c of want) e.classList.add(c);
      }
    }
    e.innerHTML = b.html || "";
    return e;
  }
  if (k === "toolblock") {
    const e = el(`<div class="blk"></div>`);
    const pre = el(`<pre class="toolout"></pre>`); pre.innerHTML = b.html || "";
    e.appendChild(pre); return e;
  }
  if (k === "tool_images") {
    const e = el(`<div class="blk tool-images"></div>`);
    const images = Array.isArray(b.images) ? b.images : [];
    images.forEach((img, i) => {
      const media = String(img?.media_type || "image/png");
      if (!media.startsWith("image/")) return;
      const resource = String(img?.resource_url || "");
      const data = String(img?.data || "");
      if (!resource && !data) return;
      const src = resource || `data:${media};base64,${data}`;
      const label = String(img?.label || `tool image ${i + 1}`);
      const node = el(`<img alt="${esc(label)}" loading="lazy" />`);
      node.src = src;
      node.tabIndex = 0;
      node.title = "点击放大截图";
      node.addEventListener("click", () => openImageLightbox(src, label));
      node.addEventListener("keydown", (ev) => {
        if (ev.key !== "Enter" && ev.key !== " ") return;
        ev.preventDefault();
        openImageLightbox(src, label);
      });
      e.appendChild(node);
    });
    return e.childElementCount ? e : el(`<div></div>`);
  }
  if (k === "cmdout") {
    const e = el(`<div class="cmdout"></div>`);
    e.innerHTML = b.html || "";
    return e;
  }
  if (k === "collapsible") {
    const e = el(`<details class="card ${b.card_cls || ""}" ${b.collapsed ? "" : "open"}></details>`);
    const sum = el(`<summary><span class="chev">+</span><span class="card-title">${b.title_html || ""}</span></summary>`);
    e.appendChild(sum);
    const body = document.createElement("div");
    body.className = "body";
    if (b.card_cls === "diff-card") body.classList.add("diff-body");
    if (b.body_kind === "md") { body.classList.add("md"); body.innerHTML = mdToHtml(b.body_html); }
    else if (b.body_kind === "text") { body.style.whiteSpace = "pre-wrap"; body.innerHTML = b.body_html || ""; }
    else if (b.card_cls === "thought" || b.card_cls === "todos") { body.innerHTML = b.body_html || ""; }
    else if (b.card_cls === "diff-card") { body.innerHTML = normalizeDiffHtml(b.body_html); }
    else { const pre = el(`<pre class="toolout"></pre>`); pre.innerHTML = b.body_html || ""; body.appendChild(pre); }
    if (b.card_cls === "diff-card") {
      const stats = diffStats(body.querySelector(".diff"));
      const meta = el(`<span class="diff-stats" aria-label="${stats.added} lines added, ${stats.removed} lines removed"></span>`);
      if (stats.added) meta.appendChild(el(`<span class="add">+${stats.added}</span>`));
      if (stats.removed) meta.appendChild(el(`<span class="del">−${stats.removed}</span>`));
      if (meta.childElementCount) sum.appendChild(meta);
    }
    e.appendChild(body);
    bindCard(e);
    return e;
  }
  if (k === "subagent") {
    const e = el(`<div class="blk subagent" data-job="${esc(b.job)}"></div>`);
    const role = String(b.role || "").toLowerCase();
    const hasSemanticRole = /^[a-z][a-z0-9-]*$/.test(role);
    if (hasSemanticRole) e.classList.add(role);
    if (b.failed) e.classList.add("failed");
    // Compatibility fallback for an older server snapshot. New blocks carry
    // semantic text + role so light/dark theme CSS owns their colours.
    if (!hasSemanticRole && b.accent) e.style.borderLeftColor = b.accent;
    const head = el(`<div class="head"></div>`);
    if (b.head_text != null) head.textContent = stripAnsiText(b.head_text);
    else head.innerHTML = b.head_html || "";
    e.appendChild(head);
    if (b.foot_text || b.foot_html) {
      const foot = el(`<div class="foot"></div>`);
      if (b.foot_text != null) foot.textContent = stripAnsiText(b.foot_text);
      else foot.innerHTML = b.foot_html;
      e.appendChild(foot);
    }
    e.addEventListener("click", () => openAgent(b.job));
    return e;
  }
  if (k === "compaction") {
    const e = el(`<div class="compaction"></div>`);
    e.textContent = "── " + (b.label || "Context summarised") + " ──";
    return e;
  }
  if (k === "turn_meta") {
    const e = el(`<div class="blk turn-meta"><span class="mk">▪</span> </div>`);
    const text = el(`<span class="turn-meta-text"></span>`);
    text.textContent = b.text || "";
    e.appendChild(text);
    if (b.live) e.dataset.liveFooter = "true";
    return e;
  }
  if (k === "oversize") {
    const isAnswer = b.original_kind === "markdown";
    const isMessage = b.original_kind === "user";
    const e = el(`<div class="blk oversize-block"><div class="oversize-title"></div>` +
      `<div class="oversize-summary"></div><button type="button"></button></div>`);
    e.querySelector(".oversize-title").textContent = isAnswer
      ? "回答内容较长"
      : isMessage ? "消息内容较长" : "执行详情";
    e.querySelector(".oversize-summary").textContent = b.summary || "内容较大，已延迟加载。";
    e.querySelector("button").textContent = isAnswer ? "继续阅读" : "查看详情";
    e.querySelector("button").addEventListener("click", (ev) =>
      loadOversizeBlock(ev.currentTarget, String(b.detail_url || "")));
    return e;
  }
  if (k === "deferred_execution") {
    const e = el(`<details class="card turn-history deferred-execution">` +
      `<summary><span class="chev">▶</span><span class="card-title">执行过程</span></summary>` +
      `</details>`);
    e.addEventListener("toggle", () => {
      if (e.open) loadDeferredExecution(e, String(b.detail_url || ""));
    });
    bindCard(e);
    return e;
  }
  if (k === "approval") return buildApproval(b);
  if (k === "plan") return buildPlan(b);
  if (k === "ask_question") return buildAskQuestion(b);
  // unknown → plain line
  const e = el(`<div class="blk inline"></div>`); e.textContent = JSON.stringify(b); return e;
}

async function loadOversizeBlock(button, detailUrl) {
  if (!detailUrl || button.disabled) return;
  button.disabled = true;
  button.textContent = "正在加载…";
  const host = button.closest("[data-bid]");
  const expectedSid = STATE.sid;
  let data;
  try {
    data = await api("GET", detailUrl);
  } catch {
    data = null;
  }
  if (STATE.sid !== expectedSid || !host?.isConnected) return;
  if (!data?.__ok || (!data.block && !Array.isArray(data.blocks))) {
    button.disabled = false;
    button.textContent = "重试加载";
    return;
  }
  if (Array.isArray(data.blocks)) {
    const fragment = document.createDocumentFragment();
    data.blocks.forEach(({ id, block }) => {
      if (blocksRoot.querySelector(`[data-bid="${cssEsc(id)}"]`)) return;
      const child = buildBlock(block);
      if (!child) return;
      child.dataset.bid = String(id || "");
      fragment.appendChild(child);
    });
    host.replaceWith(fragment);
    scheduleMermaidRender(blocksRoot);
    return;
  }
  const node = buildBlock(data.block);
  if (!node) return;
  node.dataset.bid = String(data.id || host.dataset.bid || "");
  host.replaceWith(node);
  scheduleMermaidRender(node);
}

async function loadDeferredExecution(details, detailUrl) {
  if (!detailUrl || details.dataset.loaded === "true" || details.dataset.loading === "true") return;
  details.dataset.loading = "true";
  const status = el(`<div class="deferred-execution-status">正在加载执行过程…</div>`);
  details.appendChild(status);
  const expectedSid = STATE.sid;
  let data;
  try {
    data = await api("GET", detailUrl);
  } catch {
    data = null;
  }
  if (STATE.sid !== expectedSid || !details.isConnected) return;
  details.dataset.loading = "false";
  if (!data?.__ok || !Array.isArray(data.blocks)) {
    status.textContent = "加载失败，收起后可重试";
    return;
  }
  const fragment = document.createDocumentFragment();
  data.blocks.forEach(({ id, block }) => {
    if (blocksRoot.querySelector(`[data-bid="${cssEsc(id)}"]`)) return;
    const child = buildBlock(block);
    if (!child) return;
    child.dataset.bid = String(id || "");
    fragment.appendChild(child);
  });
  status.remove();
  details.appendChild(fragment);
  details.dataset.loaded = "true";
  scheduleMermaidRender(details);
}

// Lock a decision card (approval / plan) after its first click: disable every
// action button so the choice cannot be re-submitted, highlight the chosen one
// and fade the rest. Without this the buttons stay live and a second click
// fires a duplicate decision (the plan card is not removed server-side).
function lockDecisionCard(card, chosenBtn) {
  card.querySelectorAll(".acts button").forEach(btn => {
    btn.disabled = true;
    btn.classList.add(btn === chosenBtn ? "chosen" : "faded");
  });
}

function buildApproval(b) {
  const e = el(`<div class="approval">
    <div class="ti">⤷ Permission required</div>
    <div class="who"><b>${esc(b.tool_name)}</b>　${esc(b.reason || "approval required")}</div>
    <div class="cmd"></div>
    <div class="acts">
      <button class="y">[y] Allow once</button>
      <button class="a">[a] Always allow</button>
      <button class="n">[n] Deny</button>
    </div>
  </div>`);
  e.querySelector(".cmd").textContent = b.summary || "";
  let decided = false;
  const send = (action, scope, btn) => {
    if (decided) return;
    decided = true;
    lockDecisionCard(e, btn);
    api("POST", `/api/sessions/${enc(STATE.sid)}/approval`,
      { call_id: b.call_id, action, scope });
  };
  e.querySelector(".y").onclick = (ev) => send("allow", "once", ev.currentTarget);
  e.querySelector(".a").onclick = (ev) => send("allow", "always", ev.currentTarget);
  e.querySelector(".n").onclick = (ev) => send("deny", "once", ev.currentTarget);
  return e;
}

function buildPlan(b) {
  const e = el(`<div class="plan">
    <div class="ti">◇ Plan proposed</div>
    <div class="md body"></div>
    <div class="acts">
      <button class="y">[y] Approve</button>
      <button class="r">[e] Revise</button>
      <button class="n">[n] Cancel</button>
    </div>
  </div>`);
  e.querySelector(".md").innerHTML = mdToHtml(b.md);
  const btnFor = (action) => e.querySelector(
    action === "approve" ? ".y" : action === "revise" ? ".r" : ".n");
  // Already-resolved card (replayed from a snapshot, or updated server-side
  // after the decision): render it locked and bind no handlers.
  if (b.decided) { lockDecisionCard(e, btnFor(b.decided)); return e; }
  let decided = false;
  const send = (action, btn) => {
    if (decided) return;
    decided = true;
    lockDecisionCard(e, btn);
    api("POST", `/api/sessions/${enc(STATE.sid)}/plan`, { action });
  };
  e.querySelector(".y").onclick = (ev) => send("approve", ev.currentTarget);
  e.querySelector(".r").onclick = (ev) => send("revise", ev.currentTarget);
  e.querySelector(".n").onclick = (ev) => send("cancel", ev.currentTarget);
  return e;
}

function buildAskQuestion(b) {
  const multi = !!b.allow_multiple;
  const heading = b.title ? `${esc(b.title)} — ${esc(b.prompt)}` : esc(b.prompt);
  const opts = (b.options || []);
  const mk = (id, label, extra) =>
    `<button class="opt ${extra || ""}" data-id="${esc(id)}">${multi ? '<span class="ck">☐</span> ' : ""}<span class="lbl">${esc(label)}</span></button>`;
  const optsHtml = opts.map((o) => mk(o.id, o.label)).join("");
  // "Other" escape hatch — user must never be boxed into the model's options.
  const otherHtml = mk("__other__", "✎ Other (type your own answer)", "other");
  const e = el(`<div class="ask-question">
    <div class="ti">? ${heading}</div>
    <div class="opts">${optsHtml}${otherHtml}</div>
    <div class="other-input" hidden><input type="text" placeholder="Type your answer…" /><button class="y other-send">Send</button></div>
    ${multi ? '<div class="acts"><button class="y confirm">Confirm</button></div>' : ''}
  </div>`);
  const otherInput = e.querySelector(".other-input");
  const otherText = otherInput.querySelector("input");
  const otherSend = otherInput.querySelector(".other-send");

  const send = (selected) => {
    api("POST", `/api/sessions/${enc(STATE.sid)}/ask_question`,
      { question_id: b.question_id, selected });
    e.querySelectorAll(".opt, .confirm, .other-send").forEach(btn => { btn.disabled = true; });
    otherText.disabled = true;
    const chosen = new Set(selected);
    e.querySelectorAll(".opt").forEach(btn => {
      if (chosen.has(btn.dataset.id)) btn.classList.add("chosen");
      else btn.classList.add("faded");
    });
    const confirmBtn = e.querySelector(".confirm");
    if (confirmBtn) confirmBtn.remove();
  };
  const submitOther = () => {
    const val = otherText.value.trim();
    if (val) send([val]);
  };
  otherSend.onclick = submitOther;
  otherText.addEventListener("keydown", (ev) => { if (ev.key === "Enter") submitOther(); });

  const toggleCk = (btn, on) => {
    const ck = btn.querySelector(".ck");
    if (ck) ck.textContent = on ? "☑" : "☐";
  };

  if (multi) {
    const checked = new Set();
    e.querySelectorAll(".opt").forEach(btn => {
      btn.onclick = () => {
        const id = btn.dataset.id;
        if (id === "__other__") {
          otherInput.hidden = false;
          otherText.focus();
          return;
        }
        if (checked.has(id)) { checked.delete(id); btn.classList.remove("chosen"); toggleCk(btn, false); }
        else { checked.add(id); btn.classList.add("chosen"); toggleCk(btn, true); }
      };
    });
    e.querySelector(".confirm").onclick = () => {
      const customVal = otherText.value.trim();
      if (customVal && !otherInput.hidden) send([...checked, customVal]);
      else send([...checked]);
    };
  } else {
    e.querySelectorAll(".opt").forEach(btn => {
      btn.onclick = () => {
        const id = btn.dataset.id;
        if (id === "__other__") {
          otherInput.hidden = false;
          otherText.focus();
          return;
        }
        send([id]);
      };
    });
  }
  return e;
}

function bindCard(d) {
  const chev = d.querySelector(".chev");
  const sync = () => {
    if (d.open) restoreVirtualTurn(d);
    if (!chev) return;
    chev.textContent = d.classList.contains("turn-history")
      ? (d.open ? "▼" : "▶")
      : (d.open ? "-" : "+");
  };
  d.addEventListener("toggle", sync); sync();
}

// ── auto-collapse turn history ──────────────────────────────────────────────
// When a turn finishes, wrap the intermediate blocks (tool outputs, thinking
// cards, sub-agent dispatch, etc.) in a collapsed <details> so only the user
// message, the final model response, and the turn footer remain visible.
// Each completed turn keeps its own wrapper; earlier wrappers stay as-is.

function toolCallCountForTurn(nodes) {
  return nodes.filter((node) => {
    if (node.classList.contains("subagent")) return true;
    if (node.classList.contains("cmdout")) return true;
    // Single-line inline tool cards (web_search / read / grep / …) carry the
    // semantic ``tool`` class; without it a turn using only inline tools
    // counts 0 tool calls and never folds.
    if (node.classList.contains("tool")) return true;
    if (node.matches("details.card.tool, details.card.tool-block, details.card.diff-card, details.card.todos, details.card.error")) return true;
    return !!node.querySelector(":scope > .toolout");
  }).length;
}

// Collapse everything between *userEl* and *mdEl* (exclusive).
function collapseBetween(userEl, mdEl) {
  const toCollapse = [];
  let el = userEl.nextElementSibling;
  while (el && el !== mdEl) {
    toCollapse.push(el);
    el = el.nextElementSibling;
  }
  if (toCollapse.length === 0) return;
  const toolCalls = toolCallCountForTurn(toCollapse);
  if (toolCalls === 0) return;

  const details = document.createElement("details");
  details.className = "card turn-history";
  const summary = document.createElement("summary");
  summary.innerHTML = `<span class="chev">▶</span><span class="card-title">执行过程 (${toolCalls} tool call${toolCalls === 1 ? "" : "s"})</span>`;
  details.appendChild(summary);
  for (const child of toCollapse) details.appendChild(child);
  mdEl.parentNode.insertBefore(details, mdEl);
  bindCard(details);
  observeVirtualTurn(details);
}

function autoCollapseLastTurn() {
  const root = $("blocks");
  if (!root) return;

  // Find the last user-echo block.
  const children = [...root.children];
  let userEl = null;
  for (let i = children.length - 1; i >= 0; i--) {
    if (children[i].classList.contains("user-echo")) { userEl = children[i]; break; }
  }
  if (!userEl) return;

  // Find the last markdown block strictly after the user block.
  let mdEl = null;
  for (let i = children.length - 1; i >= 0; i--) {
    if (children[i] === userEl) break;
    if (children[i].classList.contains("md")) { mdEl = children[i]; break; }
  }
  if (!mdEl || mdEl === userEl) return;

  collapseBetween(userEl, mdEl);
}

// Collapse all completed turns in history (called on snapshot load / reconnect).
function autoCollapseAllTurns({ skipUserEl = null } = {}) {
  const root = $("blocks");
  if (!root) return;

  const children = [...root.children];

  // Collect every (user-echo → final .md) pair first, so DOM mutations
  // in one range don't interfere with others.
  const pairs = [];
  for (let i = 0; i < children.length; i++) {
    if (!children[i].classList.contains("user-echo")) continue;
    if (skipUserEl && children[i] === skipUserEl) continue;
    let mdEl = null;
    for (let j = i + 1; j < children.length; j++) {
      if (children[j].classList.contains("user-echo")) break;
      if (children[j].classList.contains("md")) mdEl = children[j];
    }
    if (mdEl) pairs.push({ userEl: children[i], mdEl });
  }

  // Collapse from last to first so earlier pair indices remain stable.
  for (let p = pairs.length - 1; p >= 0; p--) {
    collapseBetween(pairs[p].userEl, pairs[p].mdEl);
  }
}

const enc = encodeURIComponent;

// ── global UI state ──────────────────────────────────────────────────────────
const STATE = {
  sid: null, es: null, meta: {}, agents: [],
  sessionTitle: "AutoVerse",
  mode: "normal", modeLabel: "normal",
  spinTimer: null, spinLabel: "", runStart: 0, running: false, queued: [], interaction: 0,
  liveFooter: null, liveFooterTimer: null,
  serverClockAnchorMs: 0, serverClockAnchorPerfMs: 0,
  spinAnchorElapsedMs: 0, spinAnchorPerfMs: 0, spinPaused: false,
  historyCursor: null, historyLoading: false,
  switchTimingSid: null, switchTimingActionId: "",
};
const FILE_STATE = {
  tab: localStorage.getItem("autoverse-left-tab") || "sessions",
  rootEntries: [],
  expanded: new Set(),
  children: new Map(),
  loading: new Set(),
  showHidden: true,
  error: "",
  previewPath: "",
  previewRoot: "",
  contextPath: "",
  selectedPath: "",
  previewRailRestoreWidth: null,
};
let filePreviewRequest = 0;
const blocksRoot = $("blocks");
const logEl = $("log");
const SESSION_VIEW_CACHE_LIMIT = 12;
const sessionViewCache = new Map();
let renderedSessionID = "";

function saveSessionView(sessionId) {
  if (!sessionId || !blocksRoot.children.length) return;
  const expandedTurns = [...blocksRoot.querySelectorAll("details.turn-history[open]")]
    .map((details) => details.previousElementSibling?.dataset?.bid || "")
    .filter(Boolean);
  sessionViewCache.delete(sessionId);
  sessionViewCache.set(sessionId, {
    scrollTop: logEl.scrollTop,
    stickBottom: nearBottom(),
    expandedTurns,
  });
  while (sessionViewCache.size > SESSION_VIEW_CACHE_LIMIT) {
    sessionViewCache.delete(sessionViewCache.keys().next().value);
  }
}

function restoreSessionView(sessionId) {
  const cached = sessionViewCache.get(sessionId);
  if (!cached) return false;
  const expanded = new Set(cached.expandedTurns || []);
  blocksRoot.querySelectorAll("details.turn-history").forEach((details) => {
    const userId = details.previousElementSibling?.dataset?.bid || "";
    if (expanded.has(userId)) details.open = true;
  });
  if (cached.stickBottom) scrollDown();
  else logEl.scrollTop = Math.min(cached.scrollTop || 0, logEl.scrollHeight);
  return true;
}

// Completed execution traces are collapsed by default. Once a collapsed trace
// is well outside the viewport, detach its hidden children so long sessions do
// not retain a full DOM tree. Re-entering the viewport or opening the card
// restores the exact nodes (and therefore their local expanded state).
const virtualTurnChildren = new WeakMap();
function restoreVirtualTurn(details) {
  const children = virtualTurnChildren.get(details);
  if (!children) return;
  const fragment = document.createDocumentFragment();
  children.forEach((child) => fragment.appendChild(child));
  details.appendChild(fragment);
  virtualTurnChildren.delete(details);
}

function detachVirtualTurn(details) {
  if (details.open || virtualTurnChildren.has(details)) return;
  const children = [...details.children].slice(1);
  if (!children.length) return;
  virtualTurnChildren.set(details, children);
  children.forEach((child) => child.remove());
}

const turnHistoryObserver = window.IntersectionObserver
  ? new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        const details = entry.target;
        if (entry.isIntersecting) restoreVirtualTurn(details);
        else requestAnimationFrame(() => detachVirtualTurn(details));
      });
    }, { root: logEl, rootMargin: "900px 0px" })
  : null;

function observeVirtualTurn(details) {
  turnHistoryObserver?.observe(details);
}

// ── transient overlay scrollbars ────────────────────────────────────────────
// Keep the scrollbar geometry stable so content never shifts, but reveal the
// thumb only while its own container is moving. Hover/drag visibility is
// handled by the scrollbar pseudo states in index.html.
const scrollbarHideTimers = new WeakMap();
function revealTransientScrollbar(target) {
  if (!(target instanceof Element)) return;
  target.classList.add("scrollbar-active");
  const previous = scrollbarHideTimers.get(target);
  if (previous) clearTimeout(previous);
  scrollbarHideTimers.set(target, setTimeout(() => {
    target.classList.remove("scrollbar-active");
    scrollbarHideTimers.delete(target);
  }, 900));
}

document.addEventListener("scroll", (event) => {
  revealTransientScrollbar(event.target);
}, true);

// ── native macOS titlebar bridge ───────────────────────────────────────────
// The native shell owns window chrome, while the browser remains the source of
// truth for presentation state such as the selected session and visible rails.
// This bridge reports only those derived values; no conversation state crosses
// the boundary and browsers without a WebKit handler simply skip it.
let nativeChromeFrame = 0;
let nativeChromeSettleTimer = null;
let nativeChromeActionSeq = 0;
let nativeChromeAction = { id: "", motionToken: "panel" };
function nextNativeChromeActionID() {
  nativeChromeActionSeq += 1;
  return `web-${Date.now().toString(36)}-${nativeChromeActionSeq}`;
}
function emitNativeChromeState(handler, phase, action) {
  const root = $("app");
  const sessions = document.querySelector(".sessions");
  const sidebar = document.querySelector(".sidebar");
  const activeTitle = document.querySelector(".sess-row.active .ttl-text")
    ?.textContent?.trim();
  const leftWidth = Math.round(sessions?.getBoundingClientRect().width || 0);
  const measuredRightWidth = Math.round(
    sidebar?.getBoundingClientRect().width || 0
  );
  const underlyingRightWidth = nativeBrowserWorkspaceState.active
    && nativeBrowserWorkspaceState.priorRailWidth != null
    ? nativeBrowserWorkspaceState.priorRailWidth
    : measuredRightWidth || currentRightRailWidth();
  const rightWidth = Math.round(underlyingRightWidth);
  const rightVisible = nativeBrowserWorkspaceState.active
    ? nativeBrowserWorkspaceState.priorRailVisible === true
    : root.classList.contains("show-rail");
  handler.postMessage({
    // Mirror the row the user can actually see as selected. STATE is the
    // fallback while the rail is still being constructed during bootstrap.
    sessionTitle: activeTitle || STATE.sessionTitle || "AutoVerse",
    leftWidth,
    rightWidth,
    leftVisible: !!leftWidth && !root.classList.contains("hide-sessions"),
    // While Browser covers the rail, report the underlying dashboard state.
    // Swift combines it with Browser visibility so closing both surfaces starts
    // in the same native update instead of waiting for a Web round trip.
    rightVisible: !!rightWidth && rightVisible,
    phase,
    actionId: String(action?.id || ""),
    motionToken: String(action?.motionToken || "panel"),
  });
}
function postNativeChromeState(phase = "settled", action = nativeChromeAction) {
  const handler = window.webkit?.messageHandlers?.autoverseNativeChrome;
  if (!handler) return;
  if (nativeChromeFrame) cancelAnimationFrame(nativeChromeFrame);
  if (phase === "start") {
    nativeChromeFrame = 0;
    emitNativeChromeState(handler, phase, action);
    return;
  }
  nativeChromeFrame = requestAnimationFrame(() => {
    nativeChromeFrame = 0;
    emitNativeChromeState(handler, phase, action);
  });
}

function scheduleNativeChromeSettled(action = nativeChromeAction) {
  clearTimeout(nativeChromeSettleTimer);
  const expected = { ...action };
  nativeChromeSettleTimer = setTimeout(
    () => {
      postNativeChromeState("settled", expected);
      if (expected.id) {
        performance.mark("autoverse:panel-settled", {
          detail: { actionId: expected.id },
        });
      }
    }, 220
  );
}

function updateSessionTitle(title) {
  STATE.sessionTitle = String(title || "AutoVerse");
  const mobileTitle = $("mobileTitle");
  if (mobileTitle) mobileTitle.textContent = STATE.sessionTitle;
  postNativeChromeState();
}

// WebKit asks for one authoritative snapshot when navigation finishes. This
// closes the small startup window where the page can finish restoring a
// session before the native message handler is ready to receive bridge data.
window.autoverseNativeSync = postNativeChromeState;

function setupNativeChromeBridge() {
  if (!window.webkit?.messageHandlers?.autoverseNativeChrome) return;
  const root = $("app");
  new MutationObserver(() => {
    // Panel actions post their start payload explicitly with an action id.
    // The observer only verifies the final geometry after unrelated class work.
    scheduleNativeChromeSettled();
  }).observe(root, {
    attributes: true,
    attributeFilter: ["class"],
  });
  if (window.ResizeObserver) {
    // ResizeObserver may fire every frame during WebKit layout. Coalesce those
    // callbacks and bridge only stable geometry to SwiftUI.
    const observer = new ResizeObserver(scheduleNativeChromeSettled);
    observer.observe(root);
    observer.observe(document.querySelector(".sessions"));
    observer.observe(document.querySelector(".sidebar"));
  }
  postNativeChromeState();
}

window.autoverseNativeAction = (command) => {
  const action = typeof command === "string" ? command : String(command?.action || "");
  const transition = {
    id: String(command?.id || nextNativeChromeActionID()),
    motionToken: String(command?.motionToken || "panel"),
  };
  const root = $("app");
  if (action === "toggleSessions") {
    if (nativeBrowserWorkspaceState.active) {
      nativeBrowserWorkspaceState.autoCollapsedSessions = false;
    }
    const visible = root.classList.contains("hide-sessions");
    animatePanelClass("hide-sessions", !visible, transition);
  }
  else if (action === "collapseSessions") {
    if (nativeBrowserWorkspaceState.active
        && !root.classList.contains("hide-sessions")) {
      nativeBrowserWorkspaceState.autoCollapsedSessions = true;
      animatePanelClass("hide-sessions", true, transition);
    } else {
      postNativeChromeState("settled", transition);
    }
  }
  else if (action === "openSettings") void openSettings();
};

// ══ Scroll-follow lock ───────────────────────────────────────────────────────
// Streaming grows the log while auto-scroll pins it to the bottom. If the user
// scrolls up mid-stream, the forced scrollDown() fights them (jitter / can't
// scroll up), because nearBottom() keeps seeing "within 120px of a bottom that
// is itself growing". Any scroll away from the bottom — wheel, touch, keyboard,
// scrollbar — funnels through the scroll event, so it locks auto-follow here;
// rolling back to the bottom clears the lock. Programmatic scrollDown() always
// lands at the bottom, so it never re-locks.
let stickLocked = false;
logEl.addEventListener("scroll", () => {
  stickLocked = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight >= 4;
  if (logEl.scrollTop < 160 && STATE.historyCursor) void loadOlderHistory();
});

// ══ Layout-read batching: nearBottom() forces a sync layout flush, which
// is fine once per frame but expensive at SSE-streaming rates (50+ Hz).
// The gate ensures at most one layout read per animation frame.
let _nbCache = true, _nbFrame = 0;
function nearBottom() {
  if (stickLocked) return false;
  const now = performance.now();
  if (now - _nbFrame < 15) return _nbCache;  // ~1 frame @ 60 Hz
  _nbFrame = now;
  _nbCache = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 120;
  return _nbCache;
}
function scrollDown() {
  if (stickLocked) return;
  logEl.scrollTop = logEl.scrollHeight;
}

function syncMobileBottomChrome() {
  const composerHeight = $("composer").getBoundingClientRect().height;
  const statusHeight = $("statusbar").getBoundingClientRect().height;
  document.documentElement.style.setProperty("--composer-height", `${Math.ceil(composerHeight)}px`);
  document.documentElement.style.setProperty("--status-height", `${Math.ceil(statusHeight)}px`);
}

// ── file tree + preview (read-only, session cwd scoped) ──────────────────────
function fmtSize(n) {
  n = Number(n);
  if (!Number.isFinite(n)) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KiB`;
  return `${(n / 1024 / 1024).toFixed(1)} MiB`;
}

function fileRawUrl(path, download = false) {
  if (!STATE.sid || !path) return "";
  return `/api/sessions/${enc(STATE.sid)}/file/raw?path=${enc(path)}${download ? "&download=true" : ""}`;
}

function filePreviewActions(data, opts = {}) {
  const path = data.path || FILE_STATE.previewPath || "";
  let html = filePreviewActionButton("copy", "复制路径", FILE_PREVIEW_ICONS.copy);
  if (opts.raw !== false && path) html += filePreviewActionButton("open-raw", "打开原始文件", FILE_PREVIEW_ICONS.open);
  if (opts.fullscreen && path) html += filePreviewActionButton("fullscreen", "全屏查看图片", FILE_PREVIEW_ICONS.fullscreen);
  html += filePreviewActionButton("close", "关闭预览", FILE_PREVIEW_ICONS.close);
  return html;
}

function filePreviewShell(data, meta, body, bodyClass = "", opts = {}) {
  const panel = $("filePreviewPanel");
  const sidebar = document.querySelector(".sidebar");
  if (!panel || !sidebar) return null;
  setFilePreviewPath(data.path || "");
  FILE_STATE.previewRoot = data.root || "";
  expandRailForFilePreview();
  sidebar.classList.add("preview-open");
  panel.hidden = false;
  panel.innerHTML = `<div class="file-preview-head"><div class="file-preview-title">${esc(data.name || data.path || "File")}</div>` +
    `<div class="file-preview-actions">${filePreviewActions(data, opts)}</div></div>` +
    `<div class="file-preview-path">${esc(data.path || "")}</div>` +
    `<div class="file-preview-meta">${esc(meta)}</div>` +
    `<div class="file-preview-body ${bodyClass}">${body}</div>`;
  app.classList.add("show-rail");
  return panel;
}

function tuneFilePreviewImage(panel) {
  const img = panel?.querySelector("[data-preview-image]");
  if (!img) return;
  const apply = () => {
    const tiny = img.naturalWidth > 0 && img.naturalHeight > 0
      && img.naturalWidth < 96 && img.naturalHeight < 96;
    img.classList.toggle("tiny", tiny);
    img.title = `${img.naturalWidth || "?"} × ${img.naturalHeight || "?"}`;
  };
  if (img.complete) apply();
  else img.addEventListener("load", apply, { once: true });
}

function parseDelimitedRows(text, delimiter) {
  const rows = [];
  let row = [], cell = "", quoted = false;
  const src = String(text || "");
  for (let i = 0; i < src.length; i++) {
    const ch = src[i];
    if (quoted) {
      if (ch === '"' && src[i + 1] === '"') { cell += '"'; i++; }
      else if (ch === '"') quoted = false;
      else cell += ch;
      continue;
    }
    if (ch === '"') quoted = true;
    else if (ch === delimiter) { row.push(cell); cell = ""; }
    else if (ch === "\n") {
      row.push(cell); rows.push(row); row = []; cell = "";
    } else if (ch !== "\r") cell += ch;
  }
  if (cell || row.length || src.endsWith(delimiter)) { row.push(cell); rows.push(row); }
  return rows;
}

function renderDelimitedTable(data) {
  const lang = String(data.language || "").toLowerCase();
  const rows = parseDelimitedRows(data.content || "", lang === "tsv" ? "\t" : ",");
  const maxRows = 200, maxCols = 40;
  const visible = rows.slice(0, maxRows);
  const cols = Math.min(maxCols, Math.max(1, ...visible.map((r) => r.length)));
  const body = visible.map((row, i) => {
    const tag = i === 0 ? "th" : "td";
    const cells = Array.from({ length: cols }, (_, c) => `<${tag}>${esc(row[c] || "")}</${tag}>`).join("");
    return `<tr>${cells}</tr>`;
  }).join("");
  const clipped = rows.length > maxRows || Math.max(0, ...rows.map((r) => r.length)) > maxCols
    ? `<div class="file-preview-note">Showing first ${Math.min(rows.length, maxRows)} rows and ${cols} columns.</div>`
    : "";
  return `<div class="file-preview-table-wrap"><table class="file-preview-table">${body}</table></div>${clipped}`;
}

function resetFileTree() {
  FILE_STATE.rootEntries = [];
  FILE_STATE.expanded.clear();
  FILE_STATE.children.clear();
  FILE_STATE.loading.clear();
  FILE_STATE.error = "";
  FILE_STATE.selectedPath = "";
  renderFileTree();
  updateFileRootLabel();
}

function updateFileRootLabel() {
  const label = $("fileRootName");
  if (!label) return;
  const m = STATE.meta || {};
  label.textContent = m.cwd_short || m.cwd || "未选择目录";
  label.title = m.cwd || label.textContent;
}

function setFilePreviewPath(path) {
  const next = String(path || "");
  if (FILE_STATE.previewPath === next) return;
  FILE_STATE.previewPath = next;
  renderFileTree();
}

function setLeftTab(tab) {
  FILE_STATE.tab = tab === "files" ? "files" : "sessions";
  localStorage.setItem("autoverse-left-tab", FILE_STATE.tab);
  document.querySelector(".sessions")?.classList.toggle("files-mode", FILE_STATE.tab === "files");
  [["tabSessions", "sessions"], ["tabFiles", "files"]].forEach(([id, value]) => {
    const b = $(id);
    if (!b) return;
    const active = FILE_STATE.tab === value;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
  });
  updateFileRootLabel();
  if (FILE_STATE.tab === "files" && STATE.sid && !FILE_STATE.rootEntries.length && !FILE_STATE.error) {
    loadFileDir(".");
  }
}

async function loadFileDir(path) {
  if (!STATE.sid) return;
  FILE_STATE.loading.add(path);
  FILE_STATE.error = "";
  renderFileTree();
  const d = await api(
    "GET",
    `/api/sessions/${enc(STATE.sid)}/files?path=${enc(path)}&show_hidden=${FILE_STATE.showHidden ? "true" : "false"}`
  );
  FILE_STATE.loading.delete(path);
  if (d.__ok === false) {
    FILE_STATE.error = d.detail || `读取目录失败：HTTP ${d.__status || ""}`.trim();
    renderFileTree();
    return;
  }
  if (path === ".") FILE_STATE.rootEntries = d.entries || [];
  else FILE_STATE.children.set(path, d.entries || []);
  renderFileTree();
}

function fileEntryHtml(entry, depth) {
  const isDir = !!entry.is_dir;
  const path = entry.path || entry.name || ".";
  const expanded = isDir && FILE_STATE.expanded.has(path);
  const loading = isDir && FILE_STATE.loading.has(path);
  const kids = isDir ? FILE_STATE.children.get(path) : null;
  const icon = isDir ? (expanded ? "▾" : "▸") : "·";
  const selected = path === FILE_STATE.selectedPath;
  const previewing = !isDir && path === FILE_STATE.previewPath;
  const contextActive = path === FILE_STATE.contextPath;
  const size = !isDir && entry.size != null ? `<span class="sz">${esc(fmtSize(entry.size))}</span>` : "";
  const row = `<button class="file-row ${isDir ? "dir" : "file"}${selected ? " selected" : ""}${previewing ? " previewing" : ""}${contextActive ? " context-active" : ""}" style="padding-left:${0.8 + depth * 1.8}ch"` +
    ` data-path="${attr(path)}" data-dir="${isDir ? "1" : "0"}" data-depth="${depth}" title="${attr(path)}" role="treeitem" aria-level="${depth + 1}" tabindex="-1"` +
    (isDir ? ` aria-expanded="${expanded ? "true" : "false"}"` : "") +
    ` aria-selected="${selected ? "true" : "false"}"` +
    (previewing ? ` aria-current="true"` : "") + `>` +
    `<span class="ic">${esc(icon)}</span><span class="nm">${esc(entry.name || path)}</span>${size}</button>`;
  if (!isDir || !expanded) return row;
  let childHtml = "";
  if (loading) childHtml = `<div class="file-empty" style="padding-left:${1.8 + (depth + 1) * 1.8}ch">加载中…</div>`;
  else if (!kids) childHtml = "";
  else if (!kids.length) childHtml = `<div class="file-empty" style="padding-left:${1.8 + (depth + 1) * 1.8}ch">空目录</div>`;
  else childHtml = kids.map((kid) => fileEntryHtml(kid, depth + 1)).join("");
  return row + childHtml;
}

function renderFileTree() {
  const root = $("fileTree");
  if (!root) return;
  const activeElement = document.activeElement;
  const focusedRow = activeElement?.closest?.(".file-row");
  const restoreFocus = activeElement === root || root.contains(activeElement);
  if (focusedRow?.dataset.path) FILE_STATE.selectedPath = focusedRow.dataset.path;
  let html = "";
  if (!STATE.sid) {
    html = `<div class="file-empty">先打开一个会话。</div>`;
  } else if (FILE_STATE.error) {
    html = `<div class="file-error">${esc(FILE_STATE.error)}</div>`;
  } else if (FILE_STATE.loading.has(".") && !FILE_STATE.rootEntries.length) {
    html = `<div class="file-empty">加载文件树…</div>`;
  } else if (!FILE_STATE.rootEntries.length) {
    html = `<div class="file-empty">空目录</div>`;
  } else {
    html = FILE_STATE.rootEntries.map((entry) => fileEntryHtml(entry, 0)).join("");
  }
  root.innerHTML = html;
  if (!restoreFocus) return;
  const target = Array.from(root.querySelectorAll(".file-row"))
    .find((row) => row.dataset.path === FILE_STATE.selectedPath);
  (target || root).focus({ preventScroll: true });
}

function fileTreeRows(root) {
  return Array.from(root.querySelectorAll(".file-row"));
}

function currentFileTreeRow(root) {
  const active = document.activeElement?.closest?.(".file-row");
  if (active && root.contains(active)) return active;
  return fileTreeRows(root).find((row) => row.dataset.path === FILE_STATE.selectedPath) || null;
}

function markFileTreeSelection(root, row) {
  const path = row?.dataset.path || "";
  if (!path) return;
  FILE_STATE.selectedPath = path;
  for (const candidate of fileTreeRows(root)) {
    const selected = candidate === row;
    candidate.classList.toggle("selected", selected);
    candidate.setAttribute("aria-selected", selected ? "true" : "false");
  }
}

function selectFileTreeRow(root, row, previewFile = true) {
  if (!row) return;
  markFileTreeSelection(root, row);
  row.focus({ preventScroll: true });
  row.scrollIntoView({ block: "nearest" });
  const path = row.dataset.path || "";
  if (previewFile && row.dataset.dir !== "1" && path && path !== FILE_STATE.previewPath) {
    void openFilePreview(path);
  }
}

function moveFileTreeSelection(root, direction) {
  const rows = fileTreeRows(root);
  if (!rows.length) return;
  const current = currentFileTreeRow(root);
  let index = current ? rows.indexOf(current) : -1;
  if (index < 0 && direction < 0) index = rows.length;
  const next = index + direction;
  if (next < 0 || next >= rows.length) return;
  selectFileTreeRow(root, rows[next]);
}

function moveFileTreeToParent(root, row) {
  const depth = Number(row.dataset.depth || 0);
  if (depth <= 0) return;
  const rows = fileTreeRows(root);
  const index = rows.indexOf(row);
  for (let previous = index - 1; previous >= 0; previous--) {
    if (Number(rows[previous].dataset.depth || 0) === depth - 1) {
      selectFileTreeRow(root, rows[previous]);
      return;
    }
  }
}

function handleFileTreeHorizontalKey(root, row, key) {
  if (!row) return;
  const isDir = row.dataset.dir === "1";
  const path = row.dataset.path || ".";
  const expanded = isDir && FILE_STATE.expanded.has(path);
  if (key === "ArrowRight") {
    if (!isDir) return;
    if (!expanded) {
      void toggleFileDir(path);
      return;
    }
    const rows = fileTreeRows(root);
    const child = rows[rows.indexOf(row) + 1];
    if (child && Number(child.dataset.depth || 0) === Number(row.dataset.depth || 0) + 1) {
      selectFileTreeRow(root, child);
    }
    return;
  }
  if (isDir && expanded) {
    void toggleFileDir(path);
    return;
  }
  moveFileTreeToParent(root, row);
}

async function toggleFileDir(path) {
  if (FILE_STATE.expanded.has(path)) {
    FILE_STATE.expanded.delete(path);
    renderFileTree();
    return;
  }
  FILE_STATE.expanded.add(path);
  renderFileTree();
  if (!FILE_STATE.children.has(path)) await loadFileDir(path);
}

function setFilePreviewLoading(path) {
  const panel = $("filePreviewPanel");
  const sidebar = document.querySelector(".sidebar");
  setFilePreviewPath(path);
  if (!panel || !sidebar) return;
  expandRailForFilePreview();
  sidebar.classList.add("preview-open");
  panel.hidden = false;
  panel.innerHTML = `<div class="file-preview-head"><div class="file-preview-title">File Preview</div>` +
    `<div class="file-preview-actions">${filePreviewActionButton("close", "关闭预览", FILE_PREVIEW_ICONS.close)}</div></div>` +
    `<div class="file-preview-path">${esc(path)}</div><div class="file-empty">加载文件…</div>`;
}

function renderFilePreview(data) {
  const kind = String(data.kind || "").toLowerCase();
  if (kind === "image") {
    const url = fileRawUrl(data.path || "");
    const panel = filePreviewShell(
      data,
      `${fmtSize(data.size)} · ${data.mime || "image"}`,
      `<div class="file-preview-media"><img data-preview-image src="${attr(url)}" alt="${attr(data.name || data.path || "image preview")}" /></div>`,
      "media",
      {fullscreen: true, raw: false}
    );
    tuneFilePreviewImage(panel);
    return;
  }
  if (kind === "pdf") {
    const url = fileRawUrl(data.path || "");
    filePreviewShell(
      data,
      `${fmtSize(data.size)} · ${data.mime || "application/pdf"}`,
      `<iframe class="file-preview-pdf" src="${attr(url)}" title="${attr(data.name || "PDF preview")}"></iframe>`,
      "media pdf"
    );
    return;
  }
  if (kind === "raw") {
    const url = fileRawUrl(data.path || "");
    filePreviewShell(
      data,
      `${fmtSize(data.size)} · ${data.mime || "binary"}`,
      `<div class="file-preview-raw"><div class="file-preview-raw-kind">${esc(data.mime || "Binary file")}</div>` +
      `<a href="${attr(url)}" target="_blank" rel="noopener">Open raw</a>` +
      `<a href="${attr(fileRawUrl(data.path || "", true))}">Download</a></div>`,
      "raw"
    );
    return;
  }
  const content = String(data.content || "");
  const lines = content.split("\n");
  const lang = String(data.language || "").toLowerCase();
  const isMarkdown = kind === "markdown" || lang === "md" || lang === "markdown";
  const isTable = kind === "table" || lang === "csv" || lang === "tsv";
  const body = lines.map((line, i) =>
    `<div class="file-preview-line"><span class="ln">${i + 1}</span><span class="tx">${highlightCodeLine(line, lang)}</span></div>`
  ).join("");
  filePreviewShell(
    data,
    `${data.lines || lines.length} lines · ${fmtSize(data.size)} · ${data.language || "text"}`,
    isMarkdown
      ? `<div class="file-preview-md md">${mdToHtml(content, data.path || "")}</div>`
      : isTable
        ? renderDelimitedTable(data)
        : `<div class="file-preview-code">${body}</div>`,
    isMarkdown || isTable ? "rendered" : ""
  );
  if (isMarkdown) scheduleMermaidRender($("filePreviewPanel"));
}

function renderFilePreviewError(path, message) {
  const panel = $("filePreviewPanel");
  const sidebar = document.querySelector(".sidebar");
  setFilePreviewPath(path);
  if (!panel || !sidebar) return;
  expandRailForFilePreview();
  sidebar.classList.add("preview-open");
  panel.hidden = false;
  panel.innerHTML = `<div class="file-preview-head"><div class="file-preview-title">File Preview</div>` +
    `<div class="file-preview-actions">${filePreviewActionButton("close", "关闭预览", FILE_PREVIEW_ICONS.close)}</div></div>` +
    `<div class="file-preview-path">${esc(path)}</div><div class="file-error">${esc(message)}</div>`;
  app.classList.add("show-rail");
}

function requestNativeFilePreviewSurface() {
  const handler = window.webkit?.messageHandlers?.autoverseFilePreview;
  if (handler) handler.postMessage({ action: "open" });
}

function requestNativeBrowserPage(rawURL) {
  const handler = window.webkit?.messageHandlers?.autoverseBrowserLink;
  if (!handler) return false;
  try {
    const url = new URL(String(rawURL || ""), window.location.href);
    if (url.protocol !== "http:" && url.protocol !== "https:") return false;
    handler.postMessage({ action: "open", url: url.href });
    return true;
  } catch {
    return false;
  }
}

async function openFilePreview(path) {
  if (!STATE.sid || !path) return;
  if (path === FILE_STATE.previewPath) return;
  const requestId = ++filePreviewRequest;
  requestNativeFilePreviewSurface();
  closeMobilePanels();
  setFilePreviewLoading(path);
  const d = await api("GET", `/api/sessions/${enc(STATE.sid)}/file?path=${enc(path)}`);
  if (requestId !== filePreviewRequest || path !== FILE_STATE.previewPath) return;
  if (d.__ok === false) {
    renderFilePreviewError(path, d.detail || `读取文件失败：HTTP ${d.__status || ""}`.trim());
    return;
  }
  renderFilePreview(d);
}

function closeFilePreview() {
  filePreviewRequest++;
  setFilePreviewPath("");
  FILE_STATE.previewRoot = "";
  const panel = $("filePreviewPanel");
  const sidebar = document.querySelector(".sidebar");
  if (panel) { panel.hidden = true; panel.innerHTML = ""; }
  if (sidebar) sidebar.classList.remove("preview-open");
  restoreRailAfterFilePreview();
}

// Cache streaming block state so applyBlockCmd can incrementally patch the
// DOM instead of destroying/recreating entire elements on every SSE delta.
const _streamingMd = new Map(); // block_id → last_raw_md

function applyBlockCmd(op, id, block) {
  if (op === "remove") {
    const cur = blocksRoot.querySelector(`[data-bid="${cssEsc(id)}"]`);
    if (cur) { cur.remove(); _streamingMd.delete(id); }
    if (STATE.liveFooter && STATE.liveFooter.id === id) stopLiveFooterTimer();
    return;
  }

  const stick = nearBottom();

  // ── Streaming markdown fast-path ──────────────────────────────────
  // Reuse the existing DOM element instead of building a new one every
  // SSE delta (which would destroy/recreate the node 50+ times/sec).
  // innerHTML with the full markdown is still fast — the browser only
  // patches what changed — and guarantees correct line-break/list/code-
  // block structure, which a suffix-based approach can't offer.
  if (block && block.kind === "markdown" && block.streaming) {
    const cur = blocksRoot.querySelector(`[data-bid="${cssEsc(id)}"]`);
    const newMd = block.md || "";
    _streamingMd.set(id, newMd);
    if (cur) {
      cur.innerHTML = mdToHtml(newMd) + `<span class="cursor">▏</span>`;
      if (stick) scrollDown();
      return;
    }
    // Fall through: first append, build a fresh element.
  } else if (block && !block.streaming) {
    _streamingMd.delete(id);
  }

  // ── Normal path: build a new DOM node ──────────────────────────────
  const node = buildBlock(block);
  if (!node) return;
  node.dataset.bid = id;
  const cur = blocksRoot.querySelector(`[data-bid="${cssEsc(id)}"]`);
  if (cur) cur.replaceWith(node);
  else {
    const liveFooter = blocksRoot.querySelector('[data-live-footer="true"]');
    if (liveFooter && block.kind !== "turn_meta") blocksRoot.insertBefore(node, liveFooter);
    else blocksRoot.appendChild(node);
  }
  scheduleMermaidRender(node);
  if (block.kind === "turn_meta") {
    if (block.live) startLiveFooterTimer(id, block, false);
    else if (STATE.liveFooter && STATE.liveFooter.id === id) stopLiveFooterTimer();
  }
  if (stick) scrollDown();
}
function cssEsc(s) { return String(s).replace(/["\\]/g, "\\$&"); }

function renderSnapshot(snap) {
  if (renderedSessionID) saveSessionView(renderedSessionID);
  stopLiveFooterTimer();
  if (Number.isFinite(Number(snap.server_now_ms))) {
    STATE.serverClockAnchorMs = Number(snap.server_now_ms);
    STATE.serverClockAnchorPerfMs = performance.now();
  }
  blocksRoot.innerHTML = "";
  const fragment = document.createDocumentFragment();
  (snap.blocks || []).forEach(({ id, block }) => {
    const node = buildBlock(block);
    if (!node) return;
    node.dataset.bid = id;
    fragment.appendChild(node);
  });
  blocksRoot.appendChild(fragment);
  renderedSessionID = STATE.sid || "";
  STATE.historyCursor = snap.has_more ? String(snap.before_cursor || "") : null;
  STATE.historyLoading = false;
  syncHistoryLoader();
  scheduleMermaidRender(blocksRoot);
  if (snap.meta) { STATE.meta = snap.meta; updateFileRootLabel(); }
  if (snap.state) renderState(snap.state);
  if (snap.status) renderStatus(snap.status);
  renderMeta();
  if (STATE.switchTimingSid && STATE.switchTimingSid === STATE.sid) {
    performance.mark("autoverse:session-switch-useful", {
      detail: { actionId: STATE.switchTimingActionId },
    });
    performance.measure(
      "autoverse:session-switch-input-to-useful",
      "autoverse:session-switch-input",
      "autoverse:session-switch-useful"
    );
    STATE.switchTimingSid = null;
    STATE.switchTimingActionId = "";
  }
  const live = (snap.blocks || []).find(({ block }) =>
    block && block.kind === "turn_meta" && block.live);
  if (live) startLiveFooterTimer(live.id, live.block, true);
  // Collapse completed history only. A snapshot can arrive while a turn is still
  // running (session switch / SSE reconnect), so leave that latest turn expanded
  // until renderStatus observes the final running -> idle transition.
  const runningInSnapshot = !!(snap.status && snap.status.running) || !!live;
  const activeUserEl = runningInSnapshot
    ? [...blocksRoot.children].reverse().find((node) => node.classList.contains("user-echo"))
    : null;
  autoCollapseAllTurns({ skipUserEl: activeUserEl });
  if (!restoreSessionView(STATE.sid)) scrollDown();
}

function syncHistoryLoader() {
  let loader = blocksRoot.querySelector("[data-history-loader]");
  if (!STATE.historyCursor) {
    loader?.remove();
    return;
  }
  if (!loader) {
    loader = el(`<div class="history-loader" data-history-loader>` +
      `<button type="button">加载更早记录</button></div>`);
    loader.querySelector("button").addEventListener("click", loadOlderHistory);
    blocksRoot.insertBefore(loader, blocksRoot.firstChild);
  }
  const button = loader.querySelector("button");
  button.disabled = STATE.historyLoading;
  button.textContent = STATE.historyLoading ? "正在加载…" : "加载更早记录";
}

async function loadOlderHistory() {
  if (!STATE.sid || !STATE.historyCursor || STATE.historyLoading) return;
  const sid = STATE.sid;
  const cursor = STATE.historyCursor;
  STATE.historyLoading = true;
  syncHistoryLoader();
  const oldHeight = logEl.scrollHeight;
  let data;
  try {
    data = await api(
      "GET",
      `/api/sessions/${enc(sid)}/history?before=${enc(cursor)}`
    );
  } catch {
    if (STATE.sid === sid && STATE.historyCursor === cursor) {
      STATE.historyLoading = false;
      syncHistoryLoader();
    }
    return;
  }
  if (STATE.sid !== sid || STATE.historyCursor !== cursor) return;
  if (!data.__ok) {
    STATE.historyLoading = false;
    syncHistoryLoader();
    return;
  }
  const loader = blocksRoot.querySelector("[data-history-loader]");
  const anchor = loader?.nextSibling || blocksRoot.firstChild;
  const fragment = document.createDocumentFragment();
  let sliceStarted = performance.now();
  for (const { id, block } of (data.blocks || [])) {
    if (blocksRoot.querySelector(`[data-bid="${cssEsc(id)}"]`)) continue;
    const node = buildBlock(block);
    if (!node) continue;
    node.dataset.bid = id;
    fragment.appendChild(node);
    if (performance.now() - sliceStarted > 8) {
      await new Promise((resolve) => requestAnimationFrame(resolve));
      if (STATE.sid !== sid || STATE.historyCursor !== cursor) return;
      sliceStarted = performance.now();
    }
  }
  blocksRoot.insertBefore(fragment, anchor);
  STATE.historyCursor = data.has_more ? String(data.before_cursor || "") : null;
  STATE.historyLoading = false;
  syncHistoryLoader();
  scheduleMermaidRender(blocksRoot);
  autoCollapseAllTurns();
  logEl.scrollTop += logEl.scrollHeight - oldHeight;
}

function estimatedServerNowMs() {
  if (STATE.serverClockAnchorMs > 0) {
    return STATE.serverClockAnchorMs + performance.now() - STATE.serverClockAnchorPerfMs;
  }
  return Date.now();
}

function turnFooterText(block, elapsedMs) {
  const parts = String(block.text || "").split(" · ");
  const model = parts[0] || "";
  const details = parts.slice((Number(block.elapsed_ms) || 0) >= 1 ? 2 : 1);
  return [model, fmtTurnDur(elapsedMs), ...details].filter(Boolean).join(" · ");
}

function stopLiveFooterTimer() {
  if (STATE.liveFooterTimer) clearInterval(STATE.liveFooterTimer);
  STATE.liveFooterTimer = null;
  STATE.liveFooter = null;
}

function startLiveFooterTimer(id, block, fromSnapshot) {
  stopLiveFooterTimer();
  let elapsed = Math.max(0, Number(block.elapsed_ms) || 0);
  const paused = !!block.timer_paused;
  if (!paused && fromSnapshot && Number(block.started_at_ms) > 0) {
    elapsed = Math.max(elapsed, estimatedServerNowMs() - Number(block.started_at_ms));
  }
  STATE.liveFooter = {
    id,
    block: { ...block },
    anchorElapsedMs: elapsed,
    anchorPerfMs: performance.now(),
    paused,
  };
  const tick = () => {
    const live = STATE.liveFooter;
    if (!live || live.id !== id) return;
    const node = blocksRoot.querySelector(
      `[data-bid="${cssEsc(id)}"] .turn-meta-text`);
    if (!node) return;
    const current = live.anchorElapsedMs + (live.paused ? 0 : performance.now() - live.anchorPerfMs);
    node.textContent = turnFooterText(live.block, current);
  };
  tick();
  STATE.liveFooterTimer = setInterval(tick, 250);
}

function setLiveFooterPaused(paused) {
  const live = STATE.liveFooter;
  if (!live || live.paused === paused) return;
  const now = performance.now();
  if (paused) {
    live.anchorElapsedMs += now - live.anchorPerfMs;
  }
  live.anchorPerfMs = now;
  live.paused = paused;
  live.block.timer_paused = paused;
}

// ── sidebar / status ─────────────────────────────────────────────────────────
// Show/hide a panel by id (TUI parity: panels with no data are skipped).
function showPanel(id, on) { const e = $(id); if (e) e.hidden = !on; }

function renderTierPanel(tiers) {
  const list = $("tierList");
  if (!list) return;
  showPanel("tiersPanel", true);
  const t = tiers || {};
  list.innerHTML = ["deep", "balanced", "fast"].map((k) => {
    const v = t[k];
    const tv = v ? `<span class="tv">${esc(v)}</span>` : `<span class="tv main">(main)</span>`;
    return `<div class="tier"><span class="tk">${k}</span>${tv}</div>`;
  }).join("");
}

let _rsPending = false, _rsLatest = null;
function renderState(s) {
  STATE.agents = s.agents || [];
  scheduleAgentRefresh();
  if (s.model) STATE.meta.model = s.model;
  if (s.cwd) STATE.meta.cwd = s.cwd;
  if (s.cwd_short) STATE.meta.cwd_short = s.cwd_short;
  if (s.mode) {
    STATE.mode = s.mode;
    STATE.modeLabel = s.mode_label || s.mode;
    applyComposerMode();
  }
  renderPromptMeta();
  _rsLatest = s;
  if (_rsPending) return;
  _rsPending = true;
  requestAnimationFrame(() => {
    _rsPending = false;
    _patchCtx(_rsLatest);
  });
}
// ── sidebar context: incremental textContent patch instead of innerHTML ─────
// SSE state frames deliver snapshot values for tokens/tps/cache/cost/model.
// Patching textContent avoids destroying & recreating DOM nodes on every frame,
// so the browser can keep them in the same paint layer across updates.
function _patchCtx(s) {
  const ctx = $("ctx");
  if (!ctx) return;
  // Lazy-init stable DOM skeleton (once).
  if (!ctx.dataset.inited) {
    ctx.dataset.inited = "1";
    ctx.innerHTML =
      `<div class="ctx"><span data-ctx="tokens">0</span><span data-ctx="tokenLabel"> tokens</span></div>` +
      `<div class="ctx" data-ctx="window" hidden><span data-ctx="pct">0</span>% of <span data-ctx="windowK">—</span> window</div>` +
      `<div class="ctx" data-ctx="tps" hidden><span data-ctx="tpsVal">0</span> tok/s</div>` +
      `<div class="ctx" data-ctx="cache" hidden>cache <span data-ctx="cacheRate">0</span>% hit · <span data-ctx="cacheRead">0</span> read / <span data-ctx="cacheWrite">0</span> write</div>` +
      `<div class="ctx"><span data-ctx="costPrefix">$</span><span data-ctx="cost">0.0000</span> spent</div>` +
      `<div class="ctx-model" data-ctx="model"></div>`;
  }
  const T = (sel) => ctx.querySelector(`[data-ctx="${sel}"]`);

  const usageAvailable = s.usage_available !== false;
  const usageKind = s.usage_kind || "empty";
  const tokens = Number(s.tokens) || 0;
  const elT = T("tokens"); if (elT) elT.textContent = usageAvailable ? commas(tokens) : "—";
  const elTL = T("tokenLabel");
  if (elTL) {
    elTL.textContent = !usageAvailable ? " historical usage unavailable"
      : usageKind === "turn_total" ? " tokens in last turn"
      : " tokens";
  }

  const elWin = ctx.querySelector('[data-ctx="window"]');
  if (s.window && usageAvailable && usageKind !== "turn_total") {
    const wk = Math.round(s.window / 1000) + "k";
    const elP = T("pct"); if (elP) elP.textContent = s.percent || 0;
    const elW = T("windowK"); if (elW) elW.textContent = wk;
    if (elWin) elWin.hidden = false;
  } else if (elWin) { elWin.hidden = true; }

  const elTps = ctx.querySelector('[data-ctx="tps"]');
  if (s.tps > 0) {
    const elV = T("tpsVal"); if (elV) elV.textContent = Math.round(s.tps);
    if (elTps) elTps.hidden = false;
  } else if (elTps) { elTps.hidden = true; }

  const elCache = ctx.querySelector('[data-ctx="cache"]');
  if (s.cache_read || s.cache_write) {
    const elR = T("cacheRate"); if (elR) elR.textContent = s.cache_rate || 0;
    const elRd = T("cacheRead"); if (elRd) elRd.textContent = kfmt(s.cache_read);
    const elWr = T("cacheWrite"); if (elWr) elWr.textContent = kfmt(s.cache_write);
    if (elCache) elCache.hidden = false;
  } else if (elCache) { elCache.hidden = true; }

  const elC = T("cost");
  const costAvailable = usageAvailable && s.cost_available !== false;
  if (elC) elC.textContent = costAvailable ? (Number(s.cost) || 0).toFixed(4) : "—";
  const elCP = T("costPrefix"); if (elCP) elCP.textContent = costAvailable ? "$" : "";

  const elM = T("model"); if (elM) elM.textContent = s.model || STATE.meta.model || "";

  // Session id
  const sid = s.session_id || STATE.meta.session_id || "";
  showPanel("sessionPanel", !!sid);
  if (sid) $("sessId").textContent = sid.length <= 18 ? sid : sid.slice(0, 18) + "…";

  // Sub-agent models — empty value falls back to "(main)".
  renderTierPanel(s.tiers || {});

  // Directory — home-relative cwd (~/...), matching the TUI Directory panel.
  const cwd = s.cwd_short || s.cwd || STATE.meta.cwd_short || STATE.meta.cwd || "";
  showPanel("dirPanel", !!cwd);
  if (cwd) $("dirPath").textContent = cwd;
  updateFileRootLabel();

  // Todo — hidden once every item is complete (or the list is empty), so
  // finished work doesn't linger (TUI: show only while an open item exists).
  const todos = s.todos || [];
  const hasOpen = todos.some((t) => t.status !== "completed" && t.status !== "done");
  showPanel("todoPanel", hasOpen);
  const todoDone = s.todo_done || 0, todoTotal = s.todo_total || 0;
  $("todoTitle").textContent = `Todo  ${todoDone}/${todoTotal}`;
  $("todoBar").firstElementChild.style.width =
    todoTotal ? `${Math.round((todoDone / todoTotal) * 100)}%` : "0%";
  $("todoList").innerHTML = todos.map((t) => {
    const st = t.status;
    const [cls, mk] = (st === "completed" || st === "done") ? ["done", "✓"]
      : (st === "in_progress" || st === "running" || st === "active") ? ["run", "●"]
      : ["wait", "○"];
    return `<div class="todo ${cls}"><span class="mk">${mk}</span>` +
           `<span class="tx">${esc(t.content)}</span></div>`;
  }).join("");

  // Changed files — deleted paths render struck-through with no counts (TUI).
  const changed = s.changed || [];
  showPanel("changedPanel", changed.length > 0);
  $("changedTitle").textContent = `Changed  ${changed.length}`;
  $("changedList").innerHTML = changed.map((c) => {
    if (c.status === "deleted") return `<div class="chg deleted"><span>${esc(c.path)}</span></div>`;
    const file = `<button type="button" class="file-link" data-open-file="${attr(c.path)}" ` +
      `title="在文件预览中打开">${esc(c.path)}</button>`;
    const adds = c.added ? `<span class="add">+${c.added}</span>` : "";
    const dels = c.removed ? `<span class="del">-${c.removed}</span>` : "";
    const suffix = (adds || dels) ? `  ${adds} ${dels}` : "";
    return `<div class="chg">${file}${suffix}</div>`;
  }).join("");

  // Memory files — loaded from cwd and injected into the system prompt (TUI parity).
  const mf = s.memory_files || [];
  showPanel("memoryPanel", mf.length > 0);
  if (mf.length) $("memoryList").innerHTML = mf.map((f) =>
    `<div><button type="button" class="file-link" data-open-file="${attr(f)}" ` +
    `title="在文件预览中打开">${esc(f)}</button></div>`
  ).join("");

  // Extensions — loaded skills/plugins from the shared admin service.
  const exts = s.extensions || [];
  showPanel("extensionsPanel", exts.length > 0);
  if (exts.length) {
    $("extensionsTitle").textContent = `Extensions  ${exts.length}`;
    $("extensionsList").innerHTML = exts.map((x) => {
      const kind = x.kind === "skill" ? "Skill" : "Plugin";
      const state = x.state || "enabled";
      const suffix = [];
      if (state !== "enabled") suffix.push(state);
      if (x.diagnostics_count) suffix.push(`!${x.diagnostics_count}`);
      const cls = x.diagnostics_count ? "warn" : state !== "enabled" ? "off" : "";
      const tail = suffix.length ? ` [${suffix.join(", ")}]` : "";
      return `<div class="ext ${cls}"><span class="ek">${kind}</span>` +
             `<span class="en">${esc(x.name || "")}${esc(tail)}</span></div>`;
    }).join("");
  }

  // Agents — header carries the live count ("Agents N live"); hidden when none.
  const agents = s.agents || [];
  const live = agents.filter((a) => (a.status || "running") === "running").length;
  showPanel("agentsPanel", agents.length > 0);
  $("agentsTitle").textContent = `Agents  ${live} live`;
  $("agentList").innerHTML = agents.map((a) => {
    const cls = a.status === "running" ? "running" : (a.status === "failed" || a.status === "cancelled") ? "fail" : "done";
    const glyph = a.status === "running" ? "●" : a.status === "pending" ? "○"
      : a.status === "failed" ? "✗" : a.status === "cancelled" ? "⊘" : "✓";
    const bits = [];
    if (a.turn) bits.push(a.turn + "t");
    if (a.elapsed_ms >= 1000) bits.push(fmtDur(a.elapsed_ms));
    const meta = bits.length ? `<span class="meta"> · ${bits.join(" · ")}</span>` : "";
    return `<div class="agent ${cls}" data-job="${esc(a.job)}">${glyph} [${esc(a.type)}] ${esc(a.activity || a.status)}${meta}</div>`;
  }).join("");
  $("agentList").querySelectorAll("[data-job]").forEach((n) =>
    n.addEventListener("click", () => openAgent(n.dataset.job)));
}

function renderMeta() {
  renderPromptMeta();
}

// The compact desktop composer footer line (mode · cwd).
function renderPromptMeta() {
  const m = STATE.meta || {};
  const pm = $("promptMeta");
  const mode = STATE.mode || "normal";
  const label = STATE.modeLabel || mode;
  pm.innerHTML =
    `<button id="modeBadge" class="mode-badge mode-${mode}" title="切换模式 (Tab / Shift+Tab)">${esc(label)}</button>` +
    `<span class="pm-sep"> · </span><span>${esc(m.cwd_short || m.cwd || "")}</span>` +
    `<span class="pm-hint"> · Tab 切换模式</span>`;
  const mobileSubtitle = $("mobileSubtitle");
  if (mobileSubtitle) mobileSubtitle.textContent =
    [label, m.model || "", m.cwd_short || m.cwd || ""].filter(Boolean).join(" · ") || "智能体工作台";
}

function applyComposerMode() {
  const c = $("composer");
  if (!c) return;
  c.classList.toggle("mode-normal", STATE.mode === "normal");
  c.classList.toggle("mode-plan", STATE.mode === "plan");
  c.classList.toggle("mode-worktree", STATE.mode === "worktree");
}

function askWorktreeExitAction() {
  return new Promise((resolve) => {
    document.querySelector(".wt-exit")?.remove();
    const overlay = el(`<div class="wt-exit conversation-overlay" role="dialog" aria-modal="true" aria-label="Exit worktree"></div>`);
    overlay.innerHTML = `
      <div class="wt-exit-panel">
        <div class="wt-exit-title">Exit ${esc(STATE.modeLabel || "worktree")}</div>
        <div class="wt-exit-actions">
          <button type="button" data-action="keep">Keep</button>
          <button type="button" data-action="merge">Merge</button>
          <button type="button" data-action="discard" class="danger">Discard</button>
          <button type="button" data-action="cancel" class="muted">Cancel</button>
        </div>
      </div>`;
    const done = (value) => {
      document.removeEventListener("keydown", onKey, true);
      overlay.remove();
      resolve(value);
    };
    const onKey = (ev) => {
      if (ev.key === "Escape") {
        ev.preventDefault();
        done(null);
      }
    };
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) done(null);
    });
    overlay.querySelectorAll("button").forEach((button) => {
      button.addEventListener("click", () => {
        const action = button.dataset.action || "";
        done(action === "cancel" ? null : action);
      });
    });
    document.addEventListener("keydown", onKey, true);
    app.appendChild(overlay);
    overlay.querySelector("button")?.focus();
  });
}

function worktreeExitMessage(result) {
  if (!result || typeof result !== "object") return "";
  if (result.message) return String(result.message);
  const action = String(result.action || "keep");
  const branch = String(result.branch || "worktree");
  const commit = String(result.commit || "");
  const note = String(result.note || "");
  const shortCommit = commit ? commit.slice(0, 8) : "";
  let msg = "";
  if (note) msg = `Worktree ${action}: ${note}`;
  else if (action === "merge") msg = `Merged ${branch} back to base`;
  else if (action === "discard") msg = `Discarded ${branch}`;
  else msg = `Kept ${branch} for later`;
  if (shortCommit && !msg.includes(shortCommit)) msg += ` (${shortCommit})`;
  if (result.removed && !/removed/i.test(msg)) msg += "; worktree removed";
  return msg;
}

async function switchMode(direction = "forward") {
  if (!STATE.sid || STATE.running) return;
  let exitAction = "keep";
  try {
    let out = await api("POST", `/api/sessions/${enc(STATE.sid)}/mode`, {
      direction,
      exit_action: STATE.mode === "worktree" ? "auto" : exitAction,
    });
    if (out.needs_worktree_exit_action) {
      exitAction = await askWorktreeExitAction();
      if (!exitAction) return;
      out = await api("POST", `/api/sessions/${enc(STATE.sid)}/mode`, {
        direction,
        exit_action: exitAction,
      });
    }
    if (out.mode) {
      STATE.mode = out.mode;
      STATE.modeLabel = out.label || out.mode;
      if (out.cwd) STATE.meta.cwd = out.cwd;
      if (out.cwd_short) STATE.meta.cwd_short = out.cwd_short;
      applyComposerMode();
      resetFileTree();
      if (FILE_STATE.tab === "files") loadFileDir(".");
      renderPromptMeta();
    }
    const exitMessage = worktreeExitMessage(out.worktree_exit);
    if (exitMessage) $("statusbar").textContent = exitMessage;
    if (out.ok === false && out.error) $("statusbar").textContent = `mode: ${out.error}`;
  } catch (err) {
    $("statusbar").textContent = `mode switch failed: ${err.message || err}`;
  }
}

function renderStatus(st) {
  const stick = nearBottom();
  const composer = $("composer");
  STATE.interaction = st.interaction || 0;
  renderPromptMeta();
  const wasRunning = STATE.running;
  STATE.running = !!st.running;
  STATE.queued = st.queued || [];
  if (st.running) {
    composer.classList.add("busy");
    $("btnQueue").title = "加入排队";
    // Prefer the per-phase start so the bar times the current step (thinking /
    // writing / a tool); fall back to turn start for older servers.
    startSpinner(
      st.label || "Responding…",
      st.activity_started_at_ms || st.started_at_ms,
      st.activity_elapsed_ms,
      !!st.timer_paused,
    );
    setLiveFooterPaused(!!st.timer_paused);
    // Reflect the live state in the session rail immediately; the periodic
    // poll below keeps activity/status fresh after this first update.
    if (STATE.sid) loadSessions(STATE.sid);
    startRailPoll();
  } else {
    composer.classList.remove("busy");
    $("btnQueue").title = "打开指令菜单";
    stopSpinner();
    // One final rail refresh to clear activity lines, then stop polling.
    if (STATE.sid) loadSessions(STATE.sid);
    stopRailPoll();
    // Auto-collapse intermediate blocks when a turn just finished.
    if (wasRunning) {
      requestAnimationFrame(() => autoCollapseLastTurn());
    }
  }
  renderQueued();
  syncSendBtn();
  // ResizeObserver on composer/statusbar handles --composer-height sync.
  if (stick) requestAnimationFrame(scrollDown);
}

// Pending follow-ups, rendered as cancelable chips above the composer (R15).
function renderQueued() {
  const box = $("queued");
  const items = STATE.queued || [];
  box.hidden = items.length === 0;
  box.innerHTML = items.map((q, i) => {
    const n = Number(q.attachment_count || 0);
    const label = n ? ` · ${n} 张图片` : "";
    return (
    `<div class="qitem" data-qid="${esc(q.id)}">` +
    `<span class="qn">${i + 1}.</span>` +
    `<span class="qt">${esc(q.text || "(仅图片)")}${esc(label)}</span>` +
    `<button class="qx" title="取消排队">✕</button></div>`
    );
  }).join("");
}

// The morph button: Stop while running, else Send (disabled when empty).
function syncSendBtn() {
  const btn = $("btnSend");
  const queue = $("btnQueue");
  const hasPayload = !!$("prompt").value.trim() || IMAGE_ATTACHMENTS.length > 0;
  if (STATE.running) {
    btn.disabled = false;
    btn.title = "停止 (Esc)";
    if (queue) {
      queue.disabled = !hasPayload;
      queue.title = hasPayload ? "排队发送（本轮结束后自动发送）" : "输入内容后排队发送";
    }
  } else {
    btn.disabled = !hasPayload;
    btn.title = "发送 (Enter)";
    if (queue) {
      queue.disabled = false;
      queue.title = hasPayload ? "聚焦输入框" : "打开指令菜单";
    }
  }
}

function startSpinner(label, startedAtMs, elapsedMs, paused) {
  // `startedAtMs` is the wall-clock start of the *current* phase. Re-anchoring
  // the elapsed to it makes the timer reflect how long this step (thinking /
  // writing / a tool) has run, and survives reconnects/refreshes.
  const phaseStart = Number(startedAtMs);
  const explicitElapsed = Number(elapsedMs);
  if (Number.isFinite(explicitElapsed) && explicitElapsed >= 0) {
    STATE.spinAnchorElapsedMs = explicitElapsed;
  } else if (Number.isFinite(phaseStart) && phaseStart > 0) {
    STATE.runStart = phaseStart;
    STATE.spinAnchorElapsedMs = Math.max(0, estimatedServerNowMs() - phaseStart);
  } else if (STATE.spinTimer) {
    STATE.spinAnchorElapsedMs += performance.now() - STATE.spinAnchorPerfMs;
  } else if (!STATE.spinTimer) {
    STATE.runStart = 0;
    STATE.spinAnchorElapsedMs = 0;
  }
  STATE.spinAnchorPerfMs = performance.now();
  STATE.spinPaused = !!paused;
  const bar = $("statusbar");
  // Only rebuild the DOM when the label changes — otherwise unrelated status
  // frames (e.g. a queue edit) would restart the pulse animation and recreate
  // the elapsed node every time.
  if (STATE.spinLabel !== label || !bar.querySelector("#spElapsed")) {
    STATE.spinLabel = label;
    bar.innerHTML = `<span class="sp-dot"></span> <span class="sp-label">${esc(label)}</span> · <span class="sp-elapsed" id="spElapsed">0s</span>`;
  }
  const tick = () => {
    const el = $("spElapsed");
    const elapsed = STATE.spinAnchorElapsedMs +
      (STATE.spinPaused ? 0 : performance.now() - STATE.spinAnchorPerfMs);
    if (el) el.textContent = Math.max(0, Math.round(elapsed / 1000)) + "s";
  };
  tick();
  if (!STATE.spinTimer) STATE.spinTimer = setInterval(tick, 1000);
}
function stopSpinner() {
  if (STATE.spinTimer) { clearInterval(STATE.spinTimer); STATE.spinTimer = null; }
  STATE.runStart = 0;
  STATE.spinLabel = "";
  STATE.spinAnchorElapsedMs = 0;
  STATE.spinAnchorPerfMs = 0;
  STATE.spinPaused = false;
  $("statusbar").textContent = "";
}

function resetActiveSessionStatus() {
  STATE.running = false;
  STATE.queued = [];
  $("composer").classList.remove("busy");
  $("btnQueue").title = "打开指令菜单";
  stopSpinner();
  renderQueued();
  syncSendBtn();
}

// ── SSE wiring ───────────────────────────────────────────────────────────────
function connect(sid) {
  stopLiveFooterTimer();
  if (STATE.es) { STATE.es.close(); STATE.es = null; }
  STATE.sid = sid;
  localStorage.setItem(ACTIVE_SESSION_KEY, sid);
  const es = new EventSource(`/api/sessions/${enc(sid)}/stream`);
  STATE.es = es;
  es.onmessage = (ev) => {
    // Closing EventSource does not discard an already queued message. Ignore
    // any late frame from the session we just switched away from.
    if (STATE.es !== es || STATE.sid !== sid) return;
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    switch (msg.type) {
      case "snapshot": renderSnapshot(msg); break;
      case "block": applyBlockCmd(msg.op, msg.id, msg.block); break;
      case "state": renderState(msg.state); break;
      case "status": renderStatus(msg.status); break;
      case "reset":
        stopLiveFooterTimer();
        blocksRoot.innerHTML = "";
        _streamingMd.clear();
        STATE.historyCursor = null;
        STATE.historyLoading = false;
        break;
    }
  };
  es.onerror = () => { /* EventSource auto-reconnects */ };
}

function disconnectSessionStream() {
  if (STATE.es) STATE.es.close();
  STATE.es = null;
  STATE.sid = null;
  stopLiveFooterTimer();
}

// ── sessions rail (cwd grouping + four-channel status, TUI parity §6.3) ───────
// Explicit disclosure choices keyed by group_key (true = expanded). Groups the
// user has not touched default to open only when they contain the active session.
const GROUP_DISCLOSURE = new Map();
// Highest-urgency-first order for the group header rollup (matches ports.STATUS_PRECEDENCE).
const STATUS_PRECEDENCE = ["awaiting_input", "error_unseen", "running", "completed_unseen"];
const STATUS_GLYPH = { awaiting_input: "◆", error_unseen: "✗", running: "●", completed_unseen: "✓" };

function groupRollup(members) {
  for (const st of STATUS_PRECEDENCE) {
    const n = members.filter((m) => m.status === st).length;
    if (n) return `<span class="rollup ${st}">${STATUS_GLYPH[st]} ${n}</span>`;
  }
  return "";
}

function sessionRow(s, activeId) {
  const li = document.createElement("li");
  li.className = "sess-row";
  li.dataset.id = s.session_id;
  li.dataset.rawtitle = s.title || "";
  if (s.pinned) li.classList.add("pinned");
  const isCurrent = s.session_id === activeId;
  if (isCurrent) li.classList.add("active");
  // The current session is marked by the `.active` left border + background
  // (TUI §6.1①); no separate glyph is drawn for an idle current session.
  // Only a more urgent live state overlays a status glyph from the server.
  const glyph = s.glyph, tag = s.tag, cls = s.status || "idle";
  const title = s.title_display || s.title || "(未命名会话)";
  li.dataset.displaytitle = title;
  const noun = s.message_count === 1 ? "prompt" : "prompts";
  const rel = s.updated_label || relTime(s.updated_at);
  const meta = `${s.message_count || 0} ${noun}` + (rel ? ` · ${rel}` : "");
  const isRunning = cls === "running";
  const hasRightStatus = ["running", "awaiting_input", "completed_unseen", "error_unseen"].includes(cls);
  const g = glyph && !hasRightStatus ? `<span class="glyph ${cls}">${glyph}</span> ` : "";
  const t = tag && !isRunning ? ` <span class="tag ${cls}">${esc(tag)}</span>` : "";
  const rightStatus = cls === "running"
    ? `<span class="status-mark running-dot" aria-label="running" title="running"></span>`
    : cls === "awaiting_input"
      ? `<span class="status-mark awaiting_input" aria-label="needs input" title="needs input">◆</span>`
      : cls === "completed_unseen"
        ? `<span class="status-mark completed_unseen" aria-label="done" title="done">✓</span>`
        : cls === "error_unseen"
          ? `<span class="status-mark error_unseen" aria-label="failed" title="failed">✗</span>`
          : "";
  // Live activity (e.g. "Read src/foo.py", "Bash: npm test") — shown as a
  // dimmed line below the title for running sessions, replacing the static
  // meta (prompt count · time) so the rail matches the TUI StatusBar.
  const act = s.current_activity
    ? `<small class="activity">${esc(s.current_activity)}</small>`
    : `<small>${esc(meta)}</small>`;
  li.innerHTML = `<button type="button" class="session-hit" aria-current="${isCurrent ? "true" : "false"}">` +
    `<div class="ttl">${g}<span class="ttl-text">${esc(title)}</span>${t}${rightStatus}</div>` +
    `${act}</button>`;
  return li;
}

// Session-element cache — avoids destroying and recreating DOM on every poll.
const _sessEls = new Map(); // session_id → { li, hash }
const _groupEls = new Map(); // group_key → { li, hash }
let _sessionInventory = null;
let _sessionSwitchPendingId = "";
let _sessionDesired = [];
let _sessionVirtualFrame = 0;
const SESSION_ROW_ESTIMATE = 48;
const SESSION_WINDOW_LIMIT = 120;
const _sessionTopSpacer = document.createElement("li");
const _sessionBottomSpacer = document.createElement("li");
_sessionTopSpacer.className = "session-spacer";
_sessionBottomSpacer.className = "session-spacer";
_sessionTopSpacer.setAttribute("aria-hidden", "true");
_sessionBottomSpacer.setAttribute("aria-hidden", "true");

function _sessHash(s) {
  return [s.status || "", s.glyph || "", s.tag || "", s.title_display || s.title || "",
    s.pinned ? "1" : "0", s.message_count || 0, s.updated_at || 0,
    s.current_activity || ""].join("\x00");
}

function _syncSessionChildren(ul, desired) {
  const keep = new Set(desired);
  let cursor = ul.firstChild;
  for (const node of desired) {
    if (node === cursor) cursor = cursor.nextSibling;
    else ul.insertBefore(node, cursor);
  }
  while (cursor) {
    const next = cursor.nextSibling;
    if (!keep.has(cursor)) cursor.remove();
    cursor = next;
  }
}

function _syncSessionWindow(ul, desired, { centerActive = false } = {}) {
  _sessionDesired = desired;
  if (desired.length <= SESSION_WINDOW_LIMIT) {
    _syncSessionChildren(ul, desired);
    return;
  }
  const visibleRows = Math.max(1, Math.ceil(ul.clientHeight / SESSION_ROW_ESTIMATE));
  const activeIndex = desired.findIndex((node) => node.classList.contains("active"));
  let targetScroll = null;
  let start = Math.max(
    0,
    Math.floor(ul.scrollTop / SESSION_ROW_ESTIMATE) - 30
  );
  if (centerActive && activeIndex >= 0) {
    start = Math.max(0, activeIndex - Math.floor(visibleRows / 2) - 15);
    targetScroll = Math.max(
      0,
      (activeIndex - Math.floor(visibleRows / 2)) * SESSION_ROW_ESTIMATE
    );
  }
  const end = Math.min(desired.length, start + visibleRows + 60);
  _sessionTopSpacer.style.height = `${start * SESSION_ROW_ESTIMATE}px`;
  _sessionBottomSpacer.style.height = `${(desired.length - end) * SESSION_ROW_ESTIMATE}px`;
  _syncSessionChildren(ul, [
    _sessionTopSpacer,
    ...desired.slice(start, end),
    _sessionBottomSpacer,
  ]);
  if (targetScroll !== null) ul.scrollTop = targetScroll;
}

function _sessionElement(s, activeId, inGroup) {
  const hash = `${_sessHash(s)}\x00${inGroup ? "group" : "flat"}`;
  let cached = _sessEls.get(s.session_id);
  if (!cached || cached.hash !== hash) {
    const li = sessionRow(s, activeId);
    if (inGroup) li.classList.add("in-group");
    cached = { li, hash };
    _sessEls.set(s.session_id, cached);
  }
  cached.li.classList.toggle("active", s.session_id === activeId);
  cached.li.classList.toggle("switch-pending", s.session_id === _sessionSwitchPendingId);
  cached.li.querySelector(".session-hit")?.setAttribute(
    "aria-current", s.session_id === activeId ? "true" : "false"
  );
  return cached.li;
}

function renderSessions(data, activeId, listedActiveId = activeId) {
  const ul = $("sessionList");
  const sessions = data.sessions || [];
  const hasLiveStatus = sessions.some(
    (s) => s.status === "running" || s.status === "awaiting_input"
  );
  if (hasLiveStatus) startRailPoll();
  else if (!STATE.running) stopRailPoll();

  const groups = new Map();
  for (const s of sessions) {
    const k = s.group_key || "";
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(s);
  }
  const order = [...groups.keys()];
  // The runtime can normalize a persisted session alias to another id. Keep
  // stale-response protection keyed to the runtime id, while selecting and
  // naming the row by the id that actually appears in the session inventory.
  const active = sessions.find((s) => s.session_id === _sessionSwitchPendingId)
    || sessions.find((s) => s.session_id === listedActiveId)
    || sessions.find((s) => s.session_id === activeId);
  const selectedId = active?.session_id || listedActiveId;
  if (active) {
    updateSessionTitle(active.title_display || active.title || "AutoVerse");
  } else if (listedActiveId === activeId) {
    updateSessionTitle("AutoVerse");
  }
  const activeGroup = active ? (active.group_key || "") : "";
  const desired = [];
  const grouped = order.length > 1;
  const liveSessionIds = new Set(sessions.map((s) => s.session_id));
  const liveGroupIds = new Set(grouped ? order : []);

  if (!grouped) {
    sessions.forEach((s) => desired.push(_sessionElement(s, selectedId, false)));
  } else {
    for (const key of order) {
      const members = groups.get(key);
      const expanded = GROUP_DISCLOSURE.has(key)
        ? GROUP_DISCLOSURE.get(key)
        : key === activeGroup;
      const collapsed = !expanded;
      const base = members[0].group_base || "(no directory)";
      const word = members.length === 1 ? "session" : "sessions";
      const rollup = groupRollup(members);
      const hash = [collapsed, base, members[0].group_full || "", members.length, rollup].join("\x00");
      let cached = _groupEls.get(key);
      if (!cached || cached.hash !== hash) {
        const hdr = document.createElement("li");
        hdr.className = "sess-group" + (collapsed ? " collapsed" : "");
        hdr.dataset.group = key;
        hdr.title = members[0].group_full || base;
        hdr.innerHTML =
          `<button type="button" class="session-hit group-hit" aria-expanded="${String(!collapsed)}">` +
          `<div class="ghd"><span class="chev">${collapsed ? "▸" : "▾"}</span> ` +
          `<span class="gname">${esc(base)}</span> ${rollup}</div>` +
          `<small>${members.length} ${word}</small></button>`;
        cached = { li: hdr, hash };
        _groupEls.set(key, cached);
      }
      desired.push(cached.li);
      if (!collapsed) {
        members.forEach((s) => desired.push(_sessionElement(s, selectedId, true)));
      }
    }
  }

  for (const [id, cached] of _sessEls) {
    if (!liveSessionIds.has(id)) { cached.li.remove(); _sessEls.delete(id); }
  }
  for (const [key, cached] of _groupEls) {
    if (!liveGroupIds.has(key)) { cached.li.remove(); _groupEls.delete(key); }
  }
  _syncSessionWindow(ul, desired, { centerActive: !ul.children.length });
}

$("sessionList").addEventListener("scroll", () => {
  if (_sessionDesired.length <= SESSION_WINDOW_LIMIT || _sessionVirtualFrame) return;
  _sessionVirtualFrame = requestAnimationFrame(() => {
    _sessionVirtualFrame = 0;
    _syncSessionWindow($("sessionList"), _sessionDesired);
  });
}, { passive: true });

async function loadSessions(activeId, listedActiveId = activeId) {
  const data = await api("GET", "/api/sessions");
  // A response started for a session we have since left must not repaint the
  // rail with the stale row selected.
  if (activeId && activeId !== STATE.sid) return;
  _sessionInventory = data;
  renderSessions(data, activeId, listedActiveId);
}

// Poll the rail while sessions are running so background activity is visible.
let _railPollTimer = null, _railPollBusy = false;
function startRailPoll() {
  if (_railPollTimer) return;
  _railPollTimer = setInterval(async () => {
    if (_railPollBusy) return;
    _railPollBusy = true;
    try {
      const data = await api("GET", "/api/sessions");
      const anyLiveStatus = (data.sessions || []).some(
        (s) => s.status === "running" || s.status === "awaiting_input"
      );
      if (!anyLiveStatus && !STATE.running) { stopRailPoll(); }
      if (STATE.sid) {
        _sessionInventory = data;
        renderSessions(data, STATE.sid);
      }
    } catch {} finally { _railPollBusy = false; }
  }, 2500);
}
function stopRailPoll() {
  if (_railPollTimer) { clearInterval(_railPollTimer); _railPollTimer = null; }
}

let _sessionSwitchSeq = 0;
async function switchSession(sid) {
  if (!sid || sid === _sessionSwitchPendingId) return;
  if (sid === STATE.sid) {
    if (!_sessionSwitchPendingId) return;
    ++_sessionSwitchSeq;
    _sessionSwitchPendingId = "";
    $("sessionList").removeAttribute("aria-busy");
    if (_sessionInventory) renderSessions(_sessionInventory, STATE.sid);
    return;
  }
  const actionId = `session-${Date.now().toString(36)}-${_sessionSwitchSeq + 1}`;
  performance.mark("autoverse:session-switch-input", { detail: { actionId } });
  monitorInteractionFrameGaps(actionId);
  const seq = ++_sessionSwitchSeq;
  const previousId = STATE.sid;
  const row = [...document.querySelectorAll(".sess-row")]
    .find((item) => item.dataset.id === sid);
  _sessionSwitchPendingId = sid;
  $("sessionList").setAttribute("aria-busy", "true");
  document.querySelectorAll(".sess-row").forEach((item) => {
    item.classList.toggle("active", item.dataset.id === sid);
    item.classList.toggle("switch-pending", item.dataset.id === sid);
    item.querySelector(".session-hit")?.setAttribute(
      "aria-current", item.dataset.id === sid ? "true" : "false"
    );
  });
  if (row?.dataset.displaytitle) updateSessionTitle(row.dataset.displaytitle);
  performance.mark("autoverse:session-switch-ack", { detail: { actionId } });
  performance.measure(
    "autoverse:session-switch-input-to-ack",
    "autoverse:session-switch-input",
    "autoverse:session-switch-ack"
  );
  let res;
  try {
    performance.mark("autoverse:session-switch-request-start", {
      detail: { actionId },
    });
    res = await api("POST", "/api/sessions", { session_id: sid });
  } catch (err) {
    res = { __ok: false, error: err?.message || String(err) };
  }
  if (seq !== _sessionSwitchSeq) return;
  _sessionSwitchPendingId = "";
  $("sessionList").removeAttribute("aria-busy");
  if (!res.__ok || !res.session_id) {
    if (_sessionInventory) renderSessions(_sessionInventory, previousId);
    const previous = (_sessionInventory?.sessions || [])
      .find((item) => item.session_id === previousId);
    updateSessionTitle(previous?.title_display || previous?.title || "AutoVerse");
    $("statusbar").textContent = `session switch failed: ${res.error || `HTTP ${res.__status || "error"}`}`;
    return;
  }
  const realId = res.session_id || sid;
  saveSessionView(previousId);
  closeAgent();
  closeFilePreview();
  resetFileTree();
  _streamingMd.clear();
  disconnectSessionStream();
  resetActiveSessionStatus();
  if (res.meta) { STATE.meta = res.meta; renderMeta(); updateFileRootLabel(); }
  STATE.switchTimingSid = realId;
  STATE.switchTimingActionId = actionId;
  connect(realId);
  await loadSessions(realId, sid);
  if (seq !== _sessionSwitchSeq) return;
  if (FILE_STATE.tab === "files") await loadFileDir(".");
  performance.mark("autoverse:session-switch-settled", { detail: { actionId } });
  performance.measure(
    "autoverse:session-switch-input-to-settled",
    "autoverse:session-switch-input",
    "autoverse:session-switch-settled"
  );
  closeMobilePanels();
}

async function newSession(cwd) {
  const seq = ++_sessionSwitchSeq;
  closeAgent();
  disconnectSessionStream();
  resetActiveSessionStatus();
  const body = cwd ? { cwd } : {};
  const res = await api("POST", "/api/sessions", body);
  if (seq !== _sessionSwitchSeq) return res;
  if (!res.__ok || !res.session_id) return res;
  closeFilePreview();
  resetFileTree();
  if (res.meta) { STATE.meta = res.meta; renderMeta(); updateFileRootLabel(); }
  connect(res.session_id);
  await loadSessions(res.session_id);
  if (seq !== _sessionSwitchSeq) return res;
  if (FILE_STATE.tab === "files") await loadFileDir(".");
  closeMobilePanels();
  return res;
}

// ── new-session dialog (choose working directory) ───────────────────────────
const newSessionMask = $("newSessionMask");
let _defaultCwd = "";
async function openNewSessionDialog() {
  closeMobilePanels();
  const input = $("newSessionCwd");
  const err = $("newSessionError");
  if (err) { err.hidden = true; err.textContent = ""; }
  // Prefill with the server default (fetched once), else the active session cwd.
  if (!_defaultCwd) {
    const d = await api("GET", "/api/workspace");
    _defaultCwd = (d && d.default_cwd) || "";
  }
  if (input) input.value = (STATE.meta && STATE.meta.cwd) || _defaultCwd || "";
  newSessionMask.hidden = false;
  if (input) { input.focus(); input.select(); }
}
function closeNewSessionDialog() { if (newSessionMask) newSessionMask.hidden = true; }
async function submitNewSessionDialog() {
  const input = $("newSessionCwd");
  const err = $("newSessionError");
  const cwd = input ? input.value.trim() : "";
  const res = await newSession(cwd || undefined);
  if (res && res.__ok) { closeNewSessionDialog(); return; }
  if (err) {
    err.textContent = (res && res.detail) || "无法在该目录创建会话，请检查路径。";
    err.hidden = false;
  }
}
if (newSessionMask) {
  $("btnNewSessionClose").onclick = closeNewSessionDialog;
  $("newSessionCancel").onclick = closeNewSessionDialog;
  $("newSessionCreate").onclick = submitNewSessionDialog;
  newSessionMask.addEventListener("click", (e) => { if (e.target === newSessionMask) closeNewSessionDialog(); });
  const cwdInput = $("newSessionCwd");
  if (cwdInput) cwdInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submitNewSessionDialog(); }
  });
}

// Shared conversation dialog. Keep file operations out of WebKit's native
// prompt/confirm/alert path so every app-owned modal follows the same visible
// center-track geometry while the native Browser workspace is open.
const interactionDialogMask = $("interactionDialogMask");
let interactionDialogResolve = null;

function closeInteractionDialog(value = null) {
  if (!interactionDialogMask || interactionDialogMask.hidden) return false;
  interactionDialogMask.hidden = true;
  const resolve = interactionDialogResolve;
  interactionDialogResolve = null;
  if (resolve) resolve(value);
  return true;
}

function openInteractionDialog({
  title,
  message = "",
  inputLabel = "",
  inputValue = null,
  confirmLabel = "确定",
  showCancel = true,
  danger = false,
}) {
  closeInteractionDialog(null);
  $("interactionDialogTitle").textContent = title || "确认";
  const messageNode = $("interactionDialogMessage");
  messageNode.textContent = message;
  messageNode.hidden = !message;
  const inputRow = $("interactionDialogInputRow");
  const input = $("interactionDialogInput");
  const hasInput = inputValue !== null;
  inputRow.hidden = !hasInput;
  $("interactionDialogInputLabel").textContent = inputLabel || "名称";
  input.value = hasInput ? String(inputValue) : "";
  const confirmButton = $("interactionDialogConfirm");
  confirmButton.textContent = confirmLabel;
  confirmButton.classList.toggle("danger", danger);
  const cancelButton = $("interactionDialogCancel");
  cancelButton.hidden = !showCancel;
  interactionDialogMask.hidden = false;
  return new Promise((resolve) => {
    interactionDialogResolve = resolve;
    requestAnimationFrame(() => {
      const target = hasInput ? input : confirmButton;
      target.focus();
      if (hasInput) input.select();
    });
  });
}

function submitInteractionDialog() {
  const inputRow = $("interactionDialogInputRow");
  if (!inputRow.hidden) {
    const input = $("interactionDialogInput");
    const value = input.value.trim();
    if (!value) { input.focus(); return; }
    closeInteractionDialog(value);
    return;
  }
  closeInteractionDialog(true);
}

if (interactionDialogMask) {
  $("interactionDialogClose").onclick = () => closeInteractionDialog(null);
  $("interactionDialogCancel").onclick = () => closeInteractionDialog(null);
  $("interactionDialogConfirm").onclick = submitInteractionDialog;
  interactionDialogMask.addEventListener("click", (e) => {
    if (e.target === interactionDialogMask) closeInteractionDialog(null);
  });
  $("interactionDialogInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submitInteractionDialog(); }
  });
}

// session edit menu (pin / rename / delete)
const sessionMenu = $("sessionMenu");
let menuTarget = null;
function openSessionMenu(li, anchor) {
  if (!li) return;
  closeFileContextMenu();
  if (menuTarget && menuTarget !== li) menuTarget.classList.remove("context-active");
  menuTarget = li;
  menuTarget.classList.add("context-active");
  const pinned = li.classList.contains("pinned");
  sessionMenu.querySelector('[data-act="pin"]').innerHTML = pinned
    ? '<span class="gl">☆</span>取消置顶' : '<span class="gl">★</span>置顶';
  sessionMenu.hidden = false;
  const mw = sessionMenu.offsetWidth;
  if (anchor && Number.isFinite(anchor.clientX) && Number.isFinite(anchor.clientY)) {
    anchor.preventDefault();
    sessionMenu.style.top = Math.min(anchor.clientY, window.innerHeight - 140) + "px";
    sessionMenu.style.left = Math.max(8, Math.min(anchor.clientX, window.innerWidth - mw - 8)) + "px";
    return;
  }
  const r = anchor.getBoundingClientRect();
  sessionMenu.style.top = (r.bottom + 4) + "px";
  sessionMenu.style.left = Math.max(8, Math.min(r.right - mw, window.innerWidth - mw - 8)) + "px";
}
function closeSessionMenu() {
  if (menuTarget) menuTarget.classList.remove("context-active");
  sessionMenu.hidden = true;
  menuTarget = null;
}

// ── file context menu (right-click on file tree entries) ─────────────────────
const fileContextMenu = $("fileContextMenu");
let fileMenuTargetPath = "";
let fileMenuTargetIsDir = false;

function fileAbsolutePath(relPath) {
  const cwd = (STATE.meta && STATE.meta.cwd) || "";
  if (!cwd) return relPath;
  const base = cwd.replace(/\/+$/, "");
  const clean = String(relPath || "").replace(/^\.\//, "").replace(/^\.$/, "");
  return clean ? `${base}/${clean}` : base;
}

function openFileContextMenu(path, isDir, event) {
  fileMenuTargetPath = path;
  fileMenuTargetIsDir = isDir;
  if (FILE_STATE.contextPath !== path) {
    FILE_STATE.contextPath = path;
    renderFileTree();
  }
  closeSessionMenu();
  fileContextMenu.hidden = false;
  const mw = fileContextMenu.offsetWidth;
  fileContextMenu.style.top = Math.min(event.clientY, window.innerHeight - 140) + "px";
  fileContextMenu.style.left = Math.max(8, Math.min(event.clientX, window.innerWidth - mw - 8)) + "px";
  // Hide rename for the root directory "."
  const renameBtn = fileContextMenu.querySelector('[data-act="rename"]');
  if (renameBtn) renameBtn.hidden = path === ".";
  event.preventDefault();
}

function closeFileContextMenu() {
  fileContextMenu.hidden = true;
  fileMenuTargetPath = "";
  if (FILE_STATE.contextPath) {
    FILE_STATE.contextPath = "";
    renderFileTree();
  }
}

async function doRenameFile(path = fileMenuTargetPath) {
  if (!path || !STATE.sid) return;
  const oldName = path.split("/").pop() || path;
  const newName = await openInteractionDialog({
    title: "重命名",
    inputLabel: "新名称",
    inputValue: oldName,
    confirmLabel: "重命名",
  });
  if (!newName || newName === oldName) return;
  const d = await api("POST", `/api/sessions/${enc(STATE.sid)}/file/rename`, { path, new_name: newName });
  if (d.__ok === false) {
    await openInteractionDialog({
      title: "重命名失败",
      message: d.detail || `HTTP ${d.__status}`,
      confirmLabel: "知道了",
      showCancel: false,
    });
    return;
  }
  // Refresh the parent directory
  const parts = path.split("/");
  parts.pop();
  const parent = parts.join("/") || ".";
  FILE_STATE.children.delete(parent);
  FILE_STATE.children.delete(path);
  FILE_STATE.expanded.delete(path);
  if (path === ".") FILE_STATE.rootEntries = [];
  if (FILE_STATE.previewPath === path) setFilePreviewPath("");
  await loadFileDir(parent);
}

async function doDeleteFile(path = fileMenuTargetPath, isDir = fileMenuTargetIsDir) {
  if (!path || !STATE.sid) return;
  const confirmed = await openInteractionDialog({
    title: `删除${isDir ? "目录" : "文件"}`,
    message: `确定要删除「${path}」吗？此操作不可撤销。`,
    confirmLabel: "删除",
    danger: true,
  });
  if (confirmed !== true) return;
  const d = await api("DELETE", `/api/sessions/${enc(STATE.sid)}/file?path=${enc(path)}`);
  if (d.__ok === false) {
    await openInteractionDialog({
      title: "删除失败",
      message: d.detail || `HTTP ${d.__status}`,
      confirmLabel: "知道了",
      showCancel: false,
    });
    return;
  }
  // Refresh the parent directory
  const parts = path.split("/");
  parts.pop();
  const parent = parts.join("/") || ".";
  FILE_STATE.children.delete(parent);
  FILE_STATE.children.delete(path);
  FILE_STATE.expanded.delete(path);
  if (path === ".") FILE_STATE.rootEntries = [];
  if (FILE_STATE.previewPath === path) closeFilePreview();
  await loadFileDir(parent);
}

function doCopyFilePath(path = fileMenuTargetPath) {
  const abs = fileAbsolutePath(path);
  if (navigator.clipboard) {
    navigator.clipboard.writeText(abs).then(() => {
      composerNotice("路径已复制");
    }).catch(() => {});
  }
}

$("fileTree")?.addEventListener("contextmenu", (e) => {
  const row = e.target.closest(".file-row");
  if (!row) return;
  const path = row.dataset.path || ".";
  const isDir = row.dataset.dir === "1";
  openFileContextMenu(path, isDir, e);
});

fileContextMenu.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const act = b.dataset.act;
  const path = fileMenuTargetPath;
  const isDir = fileMenuTargetIsDir;
  closeFileContextMenu();
  if (act === "rename") doRenameFile(path);
  else if (act === "copy-path") doCopyFilePath(path);
  else if (act === "delete") doDeleteFile(path, isDir);
});

$("sessionList").addEventListener("click", (e) => {
  const grp = e.target.closest(".sess-group");
  if (grp) {
    const k = grp.dataset.group;
    GROUP_DISCLOSURE.set(k, grp.classList.contains("collapsed"));
    if (_sessionInventory) renderSessions(_sessionInventory, STATE.sid);
    return;
  }
  const li = e.target.closest("li.sess-row");
  if (li && !li.querySelector(".rename-input")) switchSession(li.dataset.id);
});

$("sessionList").addEventListener("keydown", (e) => {
  if (e.target.closest("button")) return;
  if (e.key !== "Enter" && e.key !== " ") return;
  const target = e.target.closest(".sess-group, .sess-row");
  if (!target || target.querySelector(".rename-input")) return;
  e.preventDefault();
  target.click();
});

$("sessionList").addEventListener("contextmenu", (e) => {
  const li = e.target.closest("li.sess-row");
  if (!li) return;
  openSessionMenu(li, e);
});

$("tabSessions")?.addEventListener("click", () => setLeftTab("sessions"));
$("tabFiles")?.addEventListener("click", () => setLeftTab("files"));
$("btnFileRefresh")?.addEventListener("click", () => {
  FILE_STATE.children.clear();
  FILE_STATE.rootEntries = [];
  loadFileDir(".");
});
$("fileTree")?.addEventListener("click", (e) => {
  const root = e.currentTarget;
  const row = e.target.closest(".file-row");
  if (!row) {
    root.focus();
    return;
  }
  const path = row.dataset.path || ".";
  selectFileTreeRow(root, row);
  if (row.dataset.dir === "1") void toggleFileDir(path);
});
$("fileTree")?.addEventListener("focusin", (e) => {
  const row = e.target.closest?.(".file-row");
  if (row?.dataset.path) markFileTreeSelection(e.currentTarget, row);
});
$("fileTree")?.addEventListener("focus", (e) => {
  const root = e.currentTarget;
  const rows = fileTreeRows(root);
  const target = rows.find((row) => row.dataset.path === FILE_STATE.selectedPath)
    || rows.find((row) => row.dataset.path === FILE_STATE.previewPath)
    || rows[0];
  if (target) selectFileTreeRow(root, target, false);
});
$("fileTree")?.addEventListener("keydown", (e) => {
  const handled = ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Enter", "Home", "End", "Escape"];
  if (!handled.includes(e.key)) return;
  e.preventDefault();
  e.stopPropagation();
  const root = e.currentTarget;
  const rows = fileTreeRows(root);
  const row = currentFileTreeRow(root);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    moveFileTreeSelection(root, e.key === "ArrowDown" ? 1 : -1);
  } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    handleFileTreeHorizontalKey(root, row, e.key);
  } else if (e.key === "Home" || e.key === "End") {
    selectFileTreeRow(root, e.key === "Home" ? rows[0] : rows[rows.length - 1]);
  } else if (e.key === "Enter" && row) {
    const path = row.dataset.path || ".";
    if (row.dataset.dir === "1") void toggleFileDir(path);
    else void openFilePreview(path);
  } else if (e.key === "Escape" && FILE_STATE.previewPath) {
    closeFilePreview();
  }
});
document.querySelector(".sidebar")?.addEventListener("click", (e) => {
  if (e.target.closest("[data-close-file]")) {
    e.preventDefault();
    closeFilePreview();
    return;
  }
  if (e.target.closest("[data-copy-file]")) {
    e.preventDefault();
    const root = FILE_STATE.previewRoot || "";
    const rel = FILE_STATE.previewPath || "";
    const absPath = root && rel ? `${root}/${rel}` : rel;
    if (absPath && navigator.clipboard) {
      navigator.clipboard.writeText(absPath).then(() => {
        composerNotice("路径已复制");
      }).catch(() => {});
    }
    return;
  }
  if (e.target.closest("[data-open-raw-file]")) {
    e.preventDefault();
    const url = fileRawUrl(FILE_STATE.previewPath);
    if (url) window.open(url, "_blank", "noopener");
    return;
  }
  if (e.target.closest("[data-fullscreen-file]")) {
    e.preventDefault();
    const img = document.querySelector("[data-preview-image]");
    if (img && img.src) {
      openImageLightbox(img.src, (FILE_STATE.previewPath || "").split("/").pop() || "图片预览");
    }
    return;
  }
});

document.addEventListener("click", (e) => {
  const file = e.target.closest("[data-open-file]");
  if (!file) return;
  e.preventDefault();
  void openFilePreview(file.dataset.openFile);
});

document.addEventListener("click", (e) => {
  const link = e.target.closest(".log .md a.conversation-web-link[href]");
  if (!link || !requestNativeBrowserPage(link.href)) return;
  e.preventDefault();
});

sessionMenu.addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b || !menuTarget) return;
  const li = menuTarget, sid = li.dataset.id, act = b.dataset.act;
  closeSessionMenu();
  if (act === "pin") { await api("POST", `/api/sessions/${enc(sid)}/pin`, { pinned: !li.classList.contains("pinned") }); await loadSessions(STATE.sid); }
  else if (act === "rename") startRename(li, sid);
  else if (act === "delete") {
    await api("DELETE", `/api/sessions/${enc(sid)}`);
    if (sid === STATE.sid) { const d = await api("GET", "/api/sessions"); const next = (d.sessions || [])[0]; if (next) switchSession(next.session_id); else newSession(); }
    else await loadSessions(STATE.sid);
  }
});

function startRename(li, sid) {
  const hit = li.querySelector(".session-hit");
  if (!hit) return;
  // Edit the raw title only — never the rendered glyph/tag decoration.
  const old = li.dataset.rawtitle || "";
  const inp = document.createElement("input");
  inp.className = "rename-input"; inp.value = old;
  hit.replaceWith(inp); inp.focus(); inp.select();
  let done = false;
  const commit = async (save) => {
    if (done) return; done = true;
    const v = (save && inp.value.trim()) ? inp.value.trim() : old;
    if (save && v !== old) { await api("POST", `/api/sessions/${enc(sid)}/rename`, { title: v }); }
    _sessEls.delete(sid);
    await loadSessions(STATE.sid);  // rebuild the row (restores glyph/tag/meta)
  };
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(true); }
    else if (e.key === "Escape") { e.preventDefault(); commit(false); }
  });
  inp.addEventListener("blur", () => commit(true));
}

document.addEventListener("click", (e) => {
  if (!sessionMenu.hidden && !sessionMenu.contains(e.target)) closeSessionMenu();
  if (!fileContextMenu.hidden && !fileContextMenu.contains(e.target)) closeFileContextMenu();
});

// ── layout toggles + responsive (vertical & horizontal adaptive) ─────────────
const app = $("app");

function _panelGhost(panel, rect) {
  const ghost = panel.cloneNode(true);
  ghost.querySelectorAll("[id]").forEach((node) => node.removeAttribute("id"));
  ghost.removeAttribute("id");
  ghost.setAttribute("aria-hidden", "true");
  Object.assign(ghost.style, {
    position: "fixed",
    zIndex: "75",
    inset: "auto",
    top: `${rect.top}px`,
    left: `${rect.left}px`,
    width: `${rect.width}px`,
    height: `${rect.height}px`,
    pointerEvents: "none",
    overflow: "hidden",
    contain: "layout paint",
  });
  document.body.appendChild(ghost);
  return ghost;
}

function animatePanelClass(className, enabled, transition = null) {
  if (app.classList.contains(className) === enabled) return;
  nativeChromeAction = transition || {
    id: nextNativeChromeActionID(),
    motionToken: "panel",
  };
  performance.mark("autoverse:panel-input", {
    detail: { actionId: nativeChromeAction.id },
  });
  monitorInteractionFrameGaps(nativeChromeAction.id);
  const panel = className === "hide-sessions"
    ? document.querySelector(".sessions")
    : document.querySelector(".sidebar");
  const side = className === "hide-sessions" ? -1 : 1;
  const before = panel?.getBoundingClientRect();
  const wasVisible = !!before && before.width > 1;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const ghost = !reduce && wasVisible ? _panelGhost(panel, before) : null;

  // Commit the final grid once. Only transform/opacity animate after this point,
  // so the browser does not recompute grid tracks on every animation frame.
  app.classList.toggle(className, enabled);
  if (className === "hide-sessions") {
    // Re-clamp against the target left-rail geometry in the same task. This
    // preserves the 520px conversation floor when Sessions is reopened and
    // prevents a wide persisted rail from extending under the native Browser.
    // Native drag previews remain non-persistent until their final command.
    setRightRailWidth(currentRightRailWidth(), false);
  }
  performance.mark("autoverse:panel-ack", {
    detail: { actionId: nativeChromeAction.id },
  });
  const after = panel?.getBoundingClientRect();
  const isVisible = !!after && after.width > 1;
  postNativeChromeState("start", nativeChromeAction);
  scheduleNativeChromeSettled(nativeChromeAction);
  if (reduce || wasVisible === isVisible) {
    ghost?.remove();
    return;
  }

  const frames = [
    { transform: `translateX(${side * 102}%)`, opacity: .35 },
    { transform: "translateX(0)", opacity: 1 },
  ];
  const options = {
    duration: 200,
    easing: "cubic-bezier(.2,.8,.2,1)",
    fill: "both",
  };
  if (!wasVisible && isVisible) {
    panel.animate(frames, options);
  } else if (ghost) {
    ghost.animate([...frames].reverse(), options).finished
      .catch(() => {})
      .finally(() => ghost.remove());
  }
}

function toggleRightRail() {
  if (nativeBrowserWorkspaceState.active) return;
  animatePanelClass("show-rail", !app.classList.contains("show-rail"));
}
$("btnRail").onclick = toggleRightRail;
$("btnSessions").onclick = () => {
  nativeBrowserWorkspaceState.autoCollapsedSessions = false;
  animatePanelClass("hide-sessions", true);
};
$("btnReopen").onclick = () => {
  nativeBrowserWorkspaceState.autoCollapsedSessions = false;
  animatePanelClass("hide-sessions", false);
};
$("btnNew").onclick = () => openNewSessionDialog();

const RAIL_COMFORTABLE_CENTER_WIDTH = 520;
const RAIL_MINIMUM_CENTER_WIDTH = 520;

function clampRailWidth(px, sessionsWidthOverride = null) {
  const min = 260;
  const sessionsWidth = Number.isFinite(sessionsWidthOverride)
    ? Math.max(0, Number(sessionsWidthOverride))
    : app.classList.contains("hide-sessions") || window.innerWidth < 820 ? 0 : 260;
  const maxByViewport = Math.max(
    min,
    window.innerWidth - sessionsWidth - RAIL_MINIMUM_CENTER_WIDTH,
  );
  return Math.round(Math.min(Math.max(Number(px) || 0, min), maxByViewport));
}

function setRightRailWidth(px, persist = true, sessionsWidthOverride = null) {
  const width = clampRailWidth(px, sessionsWidthOverride);
  document.documentElement.style.setProperty("--right-rail", `${width}px`);
  if (persist) localStorage.setItem(RIGHT_RAIL_WIDTH_KEY, String(width));
}

function restoreRightRailWidth() {
  const saved = Number(localStorage.getItem(RIGHT_RAIL_WIDTH_KEY) || 0);
  if (saved > 0) setRightRailWidth(saved, false);
}

function currentRightRailWidth() {
  const sidebar = document.querySelector(".sidebar");
  const measured = sidebar && sidebar.getBoundingClientRect().width;
  if (measured) return measured;
  const saved = Number(localStorage.getItem(RIGHT_RAIL_WIDTH_KEY) || 0);
  return saved || 42 * 8;
}

function filePreviewRailTargetWidth() {
  const sessionsWidth = app.classList.contains("hide-sessions") || window.innerWidth < 820 ? 0 : 260;
  return Math.floor((window.innerWidth - sessionsWidth) / 2);
}

function expandRailForFilePreview() {
  if (window.innerWidth <= 1100) {
    app.classList.add("mobile-dashboard-open");
    return;
  }
  app.classList.add("show-rail");
  if (FILE_STATE.previewRailRestoreWidth == null) {
    FILE_STATE.previewRailRestoreWidth = currentRightRailWidth();
  }
  setRightRailWidth(filePreviewRailTargetWidth(), false);
}

function restoreRailAfterFilePreview() {
  const restoreWidth = FILE_STATE.previewRailRestoreWidth;
  FILE_STATE.previewRailRestoreWidth = null;
  if (window.innerWidth <= 1100) {
    app.classList.remove("mobile-dashboard-open");
    return;
  }
  if (restoreWidth != null) {
    setRightRailWidth(restoreWidth, false);
  }
}

function setupRailResize() {
  const handle = $("railResizer");
  if (!handle) return;
  let startX = 0;
  let startWidth = 0;
  let dragging = false;
  const stop = (e) => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove("dragging");
    app.classList.remove("resizing-rail");
    try { handle.releasePointerCapture(e.pointerId); } catch {}
  };
  handle.addEventListener("pointerdown", (e) => {
    if (window.innerWidth <= 1100) return;
    e.preventDefault();
    dragging = true;
    startX = e.clientX;
    startWidth = currentRightRailWidth();
    app.classList.add("show-rail", "resizing-rail");
    handle.classList.add("dragging");
    handle.setPointerCapture(e.pointerId);
  });
  handle.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    e.preventDefault();
    // The handle is on the left edge of the right rail: moving left widens it.
    const preferred = startWidth + (startX - e.clientX);
    const sessionsVisible = !app.classList.contains("hide-sessions")
      && window.innerWidth >= 820;
    const shouldCollapse = preferred > startWidth
      && sessionsVisible
      && window.innerWidth - 260 - preferred < RAIL_COMFORTABLE_CENTER_WIDTH;
    setRightRailWidth(preferred, true, shouldCollapse ? 0 : null);
    if (shouldCollapse) {
      animatePanelClass("hide-sessions", true);
    }
  });
  handle.addEventListener("pointerup", stop);
  handle.addEventListener("pointercancel", stop);
  handle.addEventListener("dblclick", () => setRightRailWidth(42 * 8));
}

function closeMobilePanels() {
  app.classList.remove("mobile-sessions-open", "mobile-dashboard-open");
}
function toggleMobilePanel(panel) {
  const cls = panel === "sessions" ? "mobile-sessions-open" : "mobile-dashboard-open";
  const opening = !app.classList.contains(cls);
  closeMobilePanels();
  if (opening) app.classList.add(cls);
}
const btnMobileSessions = $("btnMobileSessions");
const btnMobileDashboard = $("btnMobileDashboard");
const mobileBackdrop = $("mobileBackdrop");
if (btnMobileSessions) btnMobileSessions.onclick = () => toggleMobilePanel("sessions");
if (btnMobileDashboard) btnMobileDashboard.onclick = () => toggleMobilePanel("dashboard");
if (mobileBackdrop) mobileBackdrop.onclick = closeMobilePanels;

// Width-driven panel collapse: dashboard first, then the sessions rail, so the
// center column never gets squeezed to a single-word-per-line column.
function applyResponsive() {
  const w = window.innerWidth;
  const prompt = $("prompt");
  if (prompt) {
    prompt.placeholder = w <= 720
      ? "输入消息"
      : "输入消息，/ 触发指令，Enter 发送，Shift+Enter 换行…";
  }
  if (!nativeBrowserWorkspaceState.active) {
    if (w < 820) { app.classList.add("hide-sessions"); app.classList.remove("show-rail"); }
    else if (w < 1180) { app.classList.remove("hide-sessions"); app.classList.remove("show-rail"); }
    else { app.classList.remove("hide-sessions"); app.classList.add("show-rail"); }
  }
  const savedRail = Number(localStorage.getItem(RIGHT_RAIL_WIDTH_KEY) || 0);
  if (nativeBrowserWorkspaceState.active) {
    app.classList.add("show-rail");
    closeMobilePanels();
    window.autoversePreviewNativeBrowserWorkspaceWidth(
      window.__autoverseNativeBrowserWorkspaceWidth
    );
  } else if (FILE_STATE.previewRailRestoreWidth != null || FILE_STATE.previewPath) {
    expandRailForFilePreview();
  } else {
    setRightRailWidth(savedRail || currentRightRailWidth(), false);
  }
  if (w > 1100) closeMobilePanels();
}

const nativeBrowserWorkspaceState = {
  active: false,
  priorRailVisible: null,
  priorRailWidth: null,
  autoCollapsedSessions: false,
};
window.autoversePreviewNativeBrowserWorkspaceWidth = (value) => {
  const width = Number(value);
  window.__autoverseNativeBrowserWorkspaceWidth =
    Number.isFinite(width) && width > 0 ? width : null;
  if (!nativeBrowserWorkspaceState.active || !window.__autoverseNativeBrowserWorkspaceWidth) {
    return;
  }
  setRightRailWidth(width, false);
};
window.autoverseSetNativeBrowserWorkspace = (visible) => {
  const active = Boolean(visible);
  window.__autoverseNativeBrowserWorkspace = active;
  document.documentElement.classList.toggle("native-browser-workspace", active);
  if (active === nativeBrowserWorkspaceState.active) {
    postNativeChromeState();
    return;
  }
  nativeBrowserWorkspaceState.active = active;
  if (active) {
    nativeBrowserWorkspaceState.priorRailVisible =
      app.classList.contains("show-rail");
    nativeBrowserWorkspaceState.priorRailWidth = currentRightRailWidth();
    nativeBrowserWorkspaceState.autoCollapsedSessions = false;
    closeMobilePanels();
    animatePanelClass("show-rail", true);
    window.autoversePreviewNativeBrowserWorkspaceWidth(
      window.__autoverseNativeBrowserWorkspaceWidth
    );
  } else {
    const restoreVisible = nativeBrowserWorkspaceState.priorRailVisible;
    const restoreWidth = nativeBrowserWorkspaceState.priorRailWidth;
    const restoreSessions = nativeBrowserWorkspaceState.autoCollapsedSessions;
    nativeBrowserWorkspaceState.priorRailVisible = null;
    nativeBrowserWorkspaceState.priorRailWidth = null;
    nativeBrowserWorkspaceState.autoCollapsedSessions = false;
    if (restoreWidth != null) setRightRailWidth(restoreWidth, false);
    if (restoreVisible != null) {
      animatePanelClass("show-rail", restoreVisible);
    }
    if (restoreSessions && app.classList.contains("hide-sessions")) {
      animatePanelClass("hide-sessions", false);
    }
  }
  postNativeChromeState();
};
let _rzTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(_rzTimer);
  _rzTimer = setTimeout(applyResponsive, 120);
  if (STATE.sid && nearBottom()) scrollDown();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && closeImageLightbox()) { e.preventDefault(); return; }
  if (e.key === "Escape" && closeInteractionDialog()) { e.preventDefault(); return; }
  if (e.ctrlKey && e.key === "g") {
    e.preventDefault();
    toggleRightRail();
  }
  if (e.key === "Escape") { closeSessionMenu(); closeFileContextMenu(); closeSettings(); closeNewSessionDialog(); closeMobilePanels(); }
});

// ── settings panel ───────────────────────────────────────────────────────────
const settingsMask = $("settingsMask");
const SECRET_STATE = { items: [], vault: {}, accounts: {} };
let _settingsLoadSeq = 0;
async function openSettings() {
  const seq = ++_settingsLoadSeq;
  const actionId = `settings-${Date.now().toString(36)}-${seq}`;
  performance.mark("autoverse:settings-input", { detail: { actionId } });
  monitorInteractionFrameGaps(actionId);
  closeMobilePanels();
  const m = STATE.meta || {};
  $("setCwd").textContent = m.cwd || m.cwd_short || "—";
  $("setWindow").textContent = m.context_window ? commas(m.context_window) + " tokens" : "—";
  $("setVersion").textContent = m.version ? "v" + m.version : "—";
  $("setSession").textContent = STATE.sid || m.session_id || "—";
  const settingsSid = STATE.sid;
  const cur = m.model || "";
  settingsMask.hidden = false;
  settingsMask.setAttribute("aria-busy", "true");
  performance.mark("autoverse:settings-ack", { detail: { actionId } });
  try {
    performance.mark("autoverse:settings-request-start", { detail: { actionId } });
    const [modelsResult, tiersResult, secretsResult] = await Promise.allSettled([
      loadModelsOnce(),
      loadTiers(settingsSid, seq),
      loadSecrets(settingsSid, seq),
    ]);
    if (seq !== _settingsLoadSeq) return;
    const models = modelsResult.status === "fulfilled" ? modelsResult.value : [];
    fillModelSelect($("setModel"), models, cur, false, TIER_AVAIL);
    if (tiersResult.status === "rejected") {
      $("tierWarn").textContent = "无法加载模型 tier，请稍后重试";
      $("tierWarn").hidden = false;
    }
    if (secretsResult.status === "rejected") {
      $("secretList").innerHTML =
        `<div class="secret-item missing"><div class="nm">无法加载密钥状态</div>` +
        `<div class="src">请稍后重试</div></div>`;
    }
  } finally {
    if (seq === _settingsLoadSeq) {
      settingsMask.removeAttribute("aria-busy");
      performance.mark("autoverse:settings-settled", { detail: { actionId } });
    }
  }
}

const MODELS_CACHE = { models: null, promise: null };
// model_id -> bool: does a credential resolve for this model's provider? Populated
// by loadTiers() from the session-scoped /tiers endpoint (env → session → vault).
let TIER_AVAIL = {};
async function loadModelsOnce() {
  if (MODELS_CACHE.models) return MODELS_CACHE.models;
  if (!MODELS_CACHE.promise) {
    MODELS_CACHE.promise = api("GET", "/api/models")
      .then((d) => {
        MODELS_CACHE.models = d.models || [];
        return MODELS_CACHE.models;
      })
      .finally(() => { MODELS_CACHE.promise = null; });
  }
  return MODELS_CACHE.promise;
}

function cleanModelLabel(o) {
  const label = o.label || o.model_id;
  const source = (o.source || "").trim();
  if (!source) return label;

  const sourceAliases = new Set([source]);
  if (source.toLowerCase().includes("official")) sourceAliases.add("Official");
  sourceAliases.add(source.replace(/\s*\([^)]*\)\s*$/, ""));

  for (const alias of sourceAliases) {
    if (!alias) continue;
    const suffix = ` (${alias})`;
    if (label.endsWith(suffix)) return label.slice(0, -suffix.length);
  }
  return label;
}

function formatModelPrice(o) {
  const inPrice = Number(o.input_price_per_mtok || 0);
  const outPrice = Number(o.output_price_per_mtok || 0);
  if (inPrice <= 0 && outPrice <= 0) return "";
  return `$${inPrice.toPrecision(4).replace(/\.?0+$/, "")}/$${outPrice.toPrecision(4).replace(/\.?0+$/, "")} /1M`;
}

function modelModalityLabel(o) {
  return o.vision ? "多模态" : "纯文本";
}

// Build an <option> label: "Model · Provider · Price · Modality" plus a "✗无key" marker when no
// credential resolves for that provider (so the user sees why a tier will fall back).
function optionLabel(o, avail) {
  let s = cleanModelLabel(o);
  if (o.source) s += `  ·  ${o.source}`;
  const price = formatModelPrice(o);
  if (price) s += `  ·  ${price}`;
  s += `  ·  ${modelModalityLabel(o)}`;
  if (avail && avail[o.model_id] === false) s += "  ✗无key";
  return s;
}

// Fill a <select> with model options; pre-select `current` (inserting it if the
// catalogue doesn't list it, e.g. an env-pinned id). `withInherit` adds a blank
// "(继承主模型)" row for the tier selects, whose empty value means "use main".
// `avail` (optional) annotates each option with provider + key status.
function fillModelSelect(sel, models, current, withInherit, avail) {
  if (!sel) return;
  sel.innerHTML = "";
  if (withInherit) {
    const op = document.createElement("option");
    op.value = ""; op.textContent = "（继承主模型）";
    sel.appendChild(op);
  }
  let has = false;
  (models || []).forEach((o) => {
    const op = document.createElement("option");
    op.value = o.model_id; op.textContent = optionLabel(o, avail);
    if (avail && avail[o.model_id] === false) op.classList.add("opt-nokey");
    if (o.model_id === current) { op.selected = true; has = true; }
    sel.appendChild(op);
  });
  if (current && !has) {
    const op = document.createElement("option");
    op.value = current; op.textContent = current; op.selected = true;
    sel.insertBefore(op, sel.firstChild);
  }
  if (!current && withInherit) sel.value = "";
}

async function loadTiers(sessionId = STATE.sid, requestSeq = null) {
  if (!sessionId) return;
  const models = await loadModelsOnce();
  let d = {};
  try { d = await api("GET", `/api/sessions/${enc(sessionId)}/tiers`); } catch (e) { d = {}; }
  if (STATE.sid !== sessionId || (requestSeq != null && requestSeq !== _settingsLoadSeq)) return;
  const t = d.tiers || {};
  TIER_AVAIL = d.available || {};
  fillModelSelect($("setTierDeep"), models, t.deep || "", true, TIER_AVAIL);
  fillModelSelect($("setTierBalanced"), models, t.balanced || "", true, TIER_AVAIL);
  fillModelSelect($("setTierFast"), models, t.fast || "", true, TIER_AVAIL);
  renderTierMeta(t, d.main);
}

function renderTierMeta(t, main) {
  // Warn about any tier pinned to a provider with no key — the runner will
  // silently fall back to the main model at spawn time, so flag it up front.
  const warn = [];
  [["deep", t.deep], ["balanced", t.balanced], ["fast", t.fast]].forEach(([k, v]) => {
    if (v && TIER_AVAIL[v] === false) warn.push(`${k}（${v}）`);
  });
  const w = $("tierWarn");
  if (w) {
    if (warn.length) {
      w.textContent = `⚠ ${warn.join("、")} 缺少对应 provider 的 key，运行子智能体时会自动回退到主模型`;
      w.hidden = false;
    } else {
      w.hidden = true;
    }
  }
}
function closeSettings() {
  _settingsLoadSeq++;
  if (settingsMask) {
    settingsMask.hidden = true;
    settingsMask.removeAttribute("aria-busy");
  }
}
$("btnSettings").onclick = openSettings;
$("btnSettingsClose").onclick = closeSettings;
settingsMask.addEventListener("click", (e) => { if (e.target === settingsMask) closeSettings(); });
$("setModelApply").onclick = async () => {
  const model = $("setModel").value;
  if (!model || !STATE.sid) return;
  const r = await api("POST", `/api/sessions/${enc(STATE.sid)}/model`, { model });
  if (r && r.model) { STATE.meta.model = r.model; renderMeta(); }
};
$("setTiersApply").onclick = async () => {
  if (!STATE.sid) return;
  const body = {
    deep: $("setTierDeep").value,
    balanced: $("setTierBalanced").value,
    fast: $("setTierFast").value,
  };
  const r = await api("POST", `/api/sessions/${enc(STATE.sid)}/tiers`, body);
  if (r && r.tiers) {
    if (r.available) TIER_AVAIL = r.available;
    renderTierMeta(r.tiers, r.main);
    renderTierPanel(r.tiers);
  }
};
const btnSettingsFloat = $("btnSettingsFloat");
if (btnSettingsFloat) btnSettingsFloat.onclick = openSettings;
const btnMobileSettings = $("btnMobileSettings");
if (btnMobileSettings) btnMobileSettings.onclick = openSettings;
const btnNativeConnection = $("btnNativeConnection");
if (btnNativeConnection) btnNativeConnection.onclick = () => {
  const handler = window.webkit && window.webkit.messageHandlers &&
    window.webkit.messageHandlers.autoverseNative;
  if (handler) handler.postMessage({ action: "connectionSettings" });
};
$("btnTheme").onclick = () => {
  const current = document.documentElement.dataset.theme || "dark";
  applyAppearance(current === "light" ? "dark" : "light");
};

async function loadSecrets(sessionId = STATE.sid, requestSeq = null) {
  if (!sessionId) return;
  const d = await api("GET", `/api/secrets?session_id=${enc(sessionId)}`);
  if (STATE.sid !== sessionId || (requestSeq != null && requestSeq !== _settingsLoadSeq)) return;
  SECRET_STATE.items = d.secrets || [];
  SECRET_STATE.vault = d.vault || {};
  renderSecrets(d);
}

function renderSecrets(d) {
  const list = $("secretList");
  const sel = $("setSecretKey");
  const old = sel.value;
  const items = d.secrets || [];
  list.innerHTML = items.length ? items.map(secretRow).join("") :
    `<div class="secret-item missing"><div class="nm">未发现模型 key 需求</div><div class="src">—</div></div>`;
  sel.innerHTML = "";
  items.forEach((s) => {
    const op = document.createElement("option");
    op.value = s.name; op.textContent = s.name;
    sel.appendChild(op);
  });
  if (old && items.some((s) => s.name === old)) sel.value = old;
  else {
    const curMissing = items.find((s) => s.current && !s.configured);
    const cur = curMissing || items.find((s) => s.current) || items[0];
    if (cur) sel.value = cur.name;
  }
  const vault = d.vault || {};
  $("secretMeta").textContent = vault.available
    ? `文件：${vault.path}`
    : `文件不可用：${vault.error || "未启用 encrypted vault"}`;
}

function secretRow(s) {
  const src = sourceLabel(s.effective_source);
  const flags = Object.entries(s.sources || {})
    .filter(([, on]) => on)
    .map(([k]) => sourceLabel(k))
    .join(" / ") || "未配置";
  const used = (s.used_by || []).join(", ");
  const account = SECRET_STATE.accounts[s.name];
  const accountBtn = s.account_supported
    ? `<button class="acct-btn" type="button" data-secret-account="${attr(s.name)}" ${s.configured ? "" : "disabled"}>` +
      `${account && account.loading ? "查询中…" : "查看余额"}</button>`
    : "";
  return `<div class="secret-item ${s.configured ? "ok" : "missing"} ${s.current ? "current" : ""}">` +
    `<div class="nm">${esc(s.name)}${s.current ? " ＊" : ""}</div>` +
    `<div class="src">${accountBtn}<span>${esc(src)} · ${esc(flags)}</span></div>` +
    `<div class="uses">${esc(used || "未被当前 registry 使用")}</div>` +
    `${account ? accountPanel(account) : ""}` +
    `</div>`;
}

function accountPanel(state) {
  if (state.loading) {
    return `<div class="acct-panel loading">正在查询账户级余额/用量…</div>`;
  }
  const d = state.data || {};
  if (!d.supported) {
    return `<div class="acct-panel muted">${esc(d.reason || "此 Provider 暂无账户余额接口")}</div>`;
  }
  if (!d.configured) {
    return `<div class="acct-panel err">${esc(d.reason || "未配置 API key")}</div>`;
  }
  if (d.error) {
    const prefix = d.status ? `HTTP ${d.status}: ` : "";
    return `<div class="acct-panel err">${esc(prefix + d.error)}</div>`;
  }
  const provider = d.provider || "Provider";
  const checked = d.checked_at ? new Date(Number(d.checked_at) * 1000).toLocaleTimeString() : "";
  let rows = "";
  if (d.provider === "OpenRouter") {
    rows = [
      acctMetric("总额度", money(d.total, d.currency)),
      acctMetric("已使用", money(d.used, d.currency)),
      acctMetric("剩余", money(d.remaining, d.currency), "strong"),
    ].join("");
  } else if (d.provider === "DeepSeek") {
    const primary = (d.balances || [])[0] || {};
    rows = [
      acctMetric("可用余额", money(d.remaining, d.currency), "strong"),
      acctMetric("充值余额", money(primary.topped_up, primary.currency || d.currency)),
      acctMetric("赠送余额", money(primary.granted, primary.currency || d.currency)),
    ].join("");
  } else {
    rows = acctMetric("余额", money(d.remaining, d.currency), "strong");
  }
  return `<div class="acct-panel">` +
    `<div class="acct-head"><span>${esc(provider)} 账户状态</span><small>${esc(sourceLabel(d.source))}${checked ? " · " + esc(checked) : ""}</small></div>` +
    `<div class="acct-grid">${rows}</div>` +
    `</div>`;
}

function acctMetric(label, value, cls = "") {
  return `<div class="acct-metric ${cls}"><span>${esc(label)}</span><b>${esc(value)}</b></div>`;
}

function money(value, currency) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (currency === "USD") return `$${n.toFixed(2)}`;
  if (currency === "CNY") return `¥${n.toFixed(2)}`;
  return `${n.toFixed(2)}${currency ? " " + currency : ""}`;
}

function sourceLabel(source) {
  return {
    env: "环境变量",
    session: "session",
    file: "文件",
    missing: "缺失",
  }[source] || source || "—";
}

async function updateSecret(where) {
  const key = $("setSecretKey").value;
  const value = $("setSecretValue").value;
  if (!key || !value) return;
  const path = where === "session" ? "/api/secrets/session" : "/api/secrets/vault";
  const body = where === "session" ? { session_id: STATE.sid, key, value } : { session_id: STATE.sid, key, value };
  await api("POST", path, body);
  $("setSecretValue").value = "";
  delete SECRET_STATE.accounts[key];
  await loadSecrets();
}

$("setSecretSession").onclick = () => updateSecret("session");
$("setSecretVault").onclick = () => updateSecret("vault");
$("clearSecretSession").onclick = async () => {
  const key = $("setSecretKey").value;
  if (STATE.sid && key) {
    await api("DELETE", `/api/secrets/session/${enc(key)}?session_id=${enc(STATE.sid)}`);
    delete SECRET_STATE.accounts[key];
    await loadSecrets();
  }
};
$("deleteSecretVault").onclick = async () => {
  const key = $("setSecretKey").value;
  if (key) {
    await api("DELETE", `/api/secrets/vault/${enc(key)}?session_id=${enc(STATE.sid || "")}`);
    delete SECRET_STATE.accounts[key];
    await loadSecrets();
  }
};

$("secretList").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-secret-account]");
  if (!btn || btn.disabled || !STATE.sid) return;
  const key = btn.dataset.secretAccount;
  if (!key) return;
  if (SECRET_STATE.accounts[key] && !SECRET_STATE.accounts[key].loading) {
    delete SECRET_STATE.accounts[key];
    renderSecrets({ secrets: SECRET_STATE.items, vault: SECRET_STATE.vault });
    return;
  }
  SECRET_STATE.accounts[key] = { loading: true };
  renderSecrets({ secrets: SECRET_STATE.items, vault: SECRET_STATE.vault });
  const data = await api("GET", `/api/providers/account?key=${enc(key)}&session_id=${enc(STATE.sid)}`);
  SECRET_STATE.accounts[key] = { loading: false, data };
  renderSecrets({ secrets: SECRET_STATE.items, vault: SECRET_STATE.vault });
});

// ── restart server ─────────────────────────────────────────────────────────
$("restartServer").onclick = async () => {
  const btn = $("restartServer");
  const status = $("restartStatus");
  btn.disabled = true;
  status.textContent = "正在重启…";
  try {
    await api("POST", "/api/restart");
  } catch {
    // Server may already be gone — that's fine; poll anyway.
  }
  let attempts = 0;
  const check = async () => {
    attempts++;
    status.textContent = `等待服务器恢复… (${attempts})`;
    try {
      const resp = await fetch("/theme.json", { cache: "no-store" });
      if (resp.ok) {
        status.textContent = "已恢复，重载页面…";
        location.reload();
        return;
      }
    } catch {}
    if (attempts < 60) setTimeout(check, 1000);
    else status.textContent = "服务器无响应，请手动重载页面。";
  };
  setTimeout(check, 1200);
};

// ── composer ─────────────────────────────────────────────────────────────────
const ta = $("prompt");
const LH = 14 * 1.55;
let followLatestDuringKeyboard = false;
const IMAGE_ATTACHMENTS = [];
let REWINDING_TURN = false;
const MAX_IMAGE_ATTACHMENTS = 4;
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
const ALLOWED_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);

function composerNotice(text) {
  const bar = $("statusbar");
  if (!bar || !text) return;
  bar.textContent = text;
  clearTimeout(composerNotice._t);
  composerNotice._t = setTimeout(() => {
    if (bar.textContent === text) bar.textContent = "";
  }, 3200);
}

function renderImageAttachments() {
  const tray = $("imageTray");
  if (!tray) return;
  tray.hidden = IMAGE_ATTACHMENTS.length === 0;
  tray.innerHTML = IMAGE_ATTACHMENTS.map((img, i) =>
    `<div class="img-chip" data-i="${i}">` +
    `<img src="data:${esc(img.media_type)};base64,${esc(img.data)}" alt="attached image" tabindex="0" title="点击放大图片" />` +
    `<span>${esc(img.name || "image")}</span>` +
    `<button type="button" title="移除图片">✕</button></div>`
  ).join("");
  syncSendBtn();
}

function clearImageAttachments() {
  IMAGE_ATTACHMENTS.length = 0;
  const input = $("imageInput");
  if (input) input.value = "";
  renderImageAttachments();
}

async function rewindSentTurn(rewindId, action) {
  if (!rewindId || !STATE.sid || REWINDING_TURN) return;
  if (action === "edit" && (ta.value.trim() || IMAGE_ATTACHMENTS.length)) {
    composerNotice("请先发送或清空当前草稿，再修改已发送的指令");
    ta.focus();
    return;
  }
  REWINDING_TURN = true;
  document.querySelectorAll("[data-turn-action]").forEach((button) => { button.disabled = true; });
  composerNotice(action === "edit" ? "正在停止并恢复到指令发送前…" : "正在撤销指令及其文件修改…");
  const out = await api("POST", `/api/sessions/${enc(STATE.sid)}/turn/rewind`, {
    rewind_id: rewindId,
  });
  REWINDING_TURN = false;
  document.querySelectorAll("[data-turn-action]").forEach((button) => { button.disabled = false; });
  if (!out || out.__ok === false || out.ok === false) {
    composerNotice(out?.detail || "无法恢复该指令");
    return;
  }
  clearImageAttachments();
  if (action === "edit") {
    ta.value = out.text || "";
    (out.attachments || []).forEach((image, index) => IMAGE_ATTACHMENTS.push({
      media_type: image.media_type,
      data: image.data,
      source_type: image.source_type || "base64",
      name: `image-${index + 1}`,
    }));
    grow(); renderImageAttachments(); ta.focus();
  } else {
    grow(); syncSendBtn();
  }
  resetFileTree();
  if (FILE_STATE.tab === "files") loadFileDir(".");
  const changed = Array.isArray(out.changed) ? out.changed.length : 0;
  composerNotice(changed ? `已恢复上下文和 ${changed} 个文件` : "已恢复到指令发送前");
}

function readImageFile(file) {
  return new Promise((resolve, reject) => {
    if (!ALLOWED_IMAGE_TYPES.has(file.type || "")) {
      reject(new Error(`不支持的图片格式：${file.type || file.name || "unknown"}`));
      return;
    }
    if (file.size > MAX_IMAGE_BYTES) {
      reject(new Error(`图片过大：${file.name || "image"} 超过 8 MB`));
      return;
    }
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("读取图片失败"));
    reader.onload = () => {
      const raw = String(reader.result || "");
      const marker = ";base64,";
      const cut = raw.indexOf(marker);
      if (cut < 0) { reject(new Error("不支持的图片数据")); return; }
      resolve({
        media_type: file.type || raw.slice(5, cut),
        data: raw.slice(cut + marker.length),
        name: file.name || "image",
      });
    };
    reader.readAsDataURL(file);
  });
}

async function addImageFiles(files) {
  const all = Array.from(files || []);
  const picked = all.filter((f) => f && f.type && f.type.startsWith("image/"));
  if (!picked.length) {
    if (all.length) composerNotice("未找到可添加的图片文件。");
    return;
  }
  const room = Math.max(0, MAX_IMAGE_ATTACHMENTS - IMAGE_ATTACHMENTS.length);
  if (!room) {
    composerNotice(`最多可附加 ${MAX_IMAGE_ATTACHMENTS} 张图片。`);
    return;
  }
  const rejected = [];
  for (const file of picked.slice(0, room)) {
    try {
      IMAGE_ATTACHMENTS.push(await readImageFile(file));
    } catch (err) {
      rejected.push(err && err.message ? err.message : String(err));
    }
  }
  if (picked.length > room) rejected.push(`已达到 ${MAX_IMAGE_ATTACHMENTS} 张上限，其余图片未添加。`);
  if (rejected.length) composerNotice(rejected[0]);
  renderImageAttachments();
}

window.autoverseAddNativeImages = (images) => {
  const picked = Array.isArray(images) ? images : [];
  if (!picked.length) return;
  const room = Math.max(0, MAX_IMAGE_ATTACHMENTS - IMAGE_ATTACHMENTS.length);
  if (!room) {
    composerNotice(`最多可附加 ${MAX_IMAGE_ATTACHMENTS} 张图片。`);
    return;
  }
  const rejected = [];
  for (const img of picked.slice(0, room)) {
    const media = String(img && img.media_type || "");
    const data = String(img && img.data || "");
    if (!ALLOWED_IMAGE_TYPES.has(media)) {
      rejected.push(`不支持的图片格式：${media || "unknown"}`);
      continue;
    }
    if (Math.floor(data.length * 3 / 4) > MAX_IMAGE_BYTES) {
      rejected.push(`图片过大：${img.name || "image"} 超过 8 MB`);
      continue;
    }
    IMAGE_ATTACHMENTS.push({
      media_type: media,
      data,
      name: String(img.name || "image"),
    });
  }
  if (picked.length > room) rejected.push(`已达到 ${MAX_IMAGE_ATTACHMENTS} 张上限，其余图片未添加。`);
  if (rejected.length) composerNotice(rejected[0]);
  renderImageAttachments();
};

// ── Soft-keyboard handling (mobile web) ───────────────────────────────────────
// iOS Safari/WKWebView still don't implement `interactive-widget` (WebKit bug
// 259770, unimplemented as of iOS 26), so the keyboard always OVERLAYS the page
// and there is no API to control or even pre-measure it. If a bottom-pinned
// input would be covered, Safari scroll-shifts the *visual viewport* up to
// reveal it — which drags every fixed/absolute element (the top bar) up too.
//
// We can't stop that shift after the fact without a laggy, flashy counter-
// translate. So instead we PREVENT it: the instant the composer is focused —
// before the keyboard rises — we lift the composer above where the keyboard
// will land (using the last measured keyboard height, cached). When the
// keyboard then appears, the input is already visible, so Safari has nothing to
// scroll → the top bar never moves → no flash. A CSS transition on the
// composer's `bottom` makes that pre-lift glide up in step with the keyboard.
let cachedKbInset = 0;            // last measured soft-keyboard height (px)
let composerFocused = false;
const isMobileViewport = () => window.matchMedia("(max-width: 1100px)").matches;
const setKbInset = (px) => {
  document.documentElement.style.setProperty("--kb-inset", `${Math.max(0, Math.round(px))}px`);
};

ta.addEventListener("focus", () => {
  // Remember the user's reading position before the keyboard changes the
  // visual viewport. Only follow the latest reply when they were already at
  // the bottom; do not yank them away from older messages they are reading.
  followLatestDuringKeyboard = nearBottom();
  if (followLatestDuringKeyboard) requestAnimationFrame(scrollDown);
  composerFocused = true;
  if (isMobileViewport()) {
    // Pre-lift before the keyboard rises so Safari never needs to scroll. Use
    // the cached real height once known; on the very first focus fall back to a
    // typical keyboard height (~36% of the screen), which self-corrects below.
    setKbInset(cachedKbInset > 0 ? cachedKbInset : Math.round(window.innerHeight * 0.36));
  }
});
ta.addEventListener("blur", () => {
  composerFocused = false;
  if (isMobileViewport()) setKbInset(0);
  // Let the final viewport resize finish before dropping the sticky state.
  setTimeout(() => { followLatestDuringKeyboard = false; }, 180);
});

let _growPending = false;
function grow() {
  if (_growPending) return;
  _growPending = true;
  requestAnimationFrame(() => {
    _growPending = false;
    ta.style.height = "auto";
    const maxRows = window.innerWidth <= 720 ? 6 : 12;
    const h = Math.min(ta.scrollHeight, LH * maxRows);
    ta.style.height = h + "px";
    ta.style.overflowY = ta.scrollHeight > LH * maxRows ? "auto" : "hidden";
  });
}
ta.addEventListener("input", () => { grow(); syncSendBtn(); }); grow(); syncSendBtn();
ta.addEventListener("paste", (e) => {
  const items = Array.from((e.clipboardData && e.clipboardData.items) || []);
  const files = items
    .filter((it) => it.kind === "file" && it.type && it.type.startsWith("image/"))
    .map((it) => it.getAsFile())
    .filter(Boolean);
  if (!files.length) return;
  e.preventDefault();
  addImageFiles(files);
});
["dragenter", "dragover"].forEach((name) => {
  ta.addEventListener(name, (e) => {
    e.preventDefault();
    $("composer").classList.add("dragging");
  });
});
["dragleave", "drop"].forEach((name) => {
  ta.addEventListener(name, () => $("composer").classList.remove("dragging"));
});
ta.addEventListener("drop", (e) => {
  e.preventDefault();
  addImageFiles(e.dataTransfer && e.dataTransfer.files);
});

// ── slash-command completion (/ triggers; ↑↓ select, Enter/Tab complete) ─────
const cmpMenu = $("cmpMenu");
let CMDS = [], cmpItems = [], cmpSel = -1;
async function loadCommands() {
  const d = await api("GET", "/api/commands");
  CMDS = d.commands || [];
}
function cmpOpen() { return !cmpMenu.hidden; }
function closeCmp() { cmpMenu.hidden = true; cmpItems = []; cmpSel = -1; }
function updateCmp() {
  const m = ta.value.match(/^\/(\S*)$/);   // slash + single token, no whitespace
  if (!m) { closeCmp(); return; }
  const q = m[1].toLowerCase();
  const matches = CMDS.filter((c) => c.name.slice(1).toLowerCase().startsWith(q));
  if (!matches.length) { closeCmp(); return; }
  cmpItems = matches.slice(0, 60); cmpSel = 0;
  renderCmp(); cmpMenu.hidden = false;
}
function renderCmp() {
  cmpMenu.innerHTML = cmpItems.map((c, i) => {
    const hint = (c.hints && c.hints.length) ? `<span class="hint">${esc(c.hints.join(" "))}</span>` : "";
    return `<div class="cmp-item ${i === cmpSel ? "sel" : ""}" data-i="${i}">` +
      `<span class="nm">${esc(c.name)}</span>${hint}<span class="sm">${esc(c.summary || "")}</span></div>`;
  }).join("");
}
function moveCmp(d) {
  if (!cmpItems.length) return;
  cmpSel = (cmpSel + d + cmpItems.length) % cmpItems.length;
  renderCmp();
  const s = cmpMenu.querySelector(".cmp-item.sel");
  if (s) s.scrollIntoView({ block: "nearest" });
}
function completeCmp() {
  const c = cmpItems[cmpSel]; if (!c) return;
  ta.value = c.name + " "; closeCmp(); grow(); ta.focus();
}
cmpMenu.addEventListener("mousedown", (e) => {
  const it = e.target.closest(".cmp-item"); if (!it) return;
  e.preventDefault(); cmpSel = +it.dataset.i; completeCmp();
});
// Skip slash-completion while an IME is composing (the pre-edit string isn't a
// committed command yet); the compositionend input event re-runs it.
ta.addEventListener("input", (e) => { if (!e.isComposing) updateCmp(); });
ta.addEventListener("blur", () => setTimeout(closeCmp, 120));

// Submit the composer text. The backend decides run-now vs enqueue based on
// whether a turn is already running (R15) — so Enter "just works" in both states.
async function notifyNativeAgentResume() {
  const handler = window.webkit?.messageHandlers?.autoverseAgentResume;
  if (!handler) return;
  try {
    await handler.postMessage({ source: "message_submit" });
  } catch (_) {
    // A native bridge failure must never prevent the user's message submission.
  }
}
async function submitText() {
  const text = ta.value.trim();
  const attachments = IMAGE_ATTACHMENTS.map((img) => ({
    media_type: img.media_type,
    data: img.data,
  }));
  if ((!text && !attachments.length) || !STATE.sid) return;
  await notifyNativeAgentResume();
  ta.value = ""; grow(); closeCmp(); clearImageAttachments(); syncSendBtn();
  const out = await api("POST", `/api/sessions/${enc(STATE.sid)}/message`, { text, attachments });
  if (out && out.__ok === false) {
    ta.value = text;
    attachments.forEach((img, i) => IMAGE_ATTACHMENTS.push({
      media_type: img.media_type,
      data: img.data,
      name: `image-${i + 1}`,
    }));
    grow(); renderImageAttachments(); syncSendBtn();
    composerNotice(out.detail || `发送失败：HTTP ${out.__status || ""}`.trim());
  }
}
function stopTurn() {
  if (!STATE.sid) return;
  api("POST", `/api/sessions/${enc(STATE.sid)}/abort`, {});
}

ta.addEventListener("keydown", (e) => {
  // While an IME is composing (e.g. Pinyin), Enter/arrows belong to the IME —
  // pressing Enter to pick a candidate must NOT submit the message. keyCode 229
  // / isComposing flag the in-progress composition across browsers.
  if (e.isComposing || e.keyCode === 229) return;
  if (cmpOpen()) {
    if (e.key === "ArrowDown") { e.preventDefault(); moveCmp(1); return; }
    if (e.key === "ArrowUp")   { e.preventDefault(); moveCmp(-1); return; }
    if (e.key === "Tab")       { e.preventDefault(); completeCmp(); return; }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); completeCmp(); return; }
    if (e.key === "Escape")    { e.preventDefault(); closeCmp(); return; }
  }
  if (e.key === "Escape" && STATE.running) { e.preventDefault(); stopTurn(); return; }
  if (e.key === "Tab") {
    e.preventDefault();
    switchMode(e.shiftKey ? "backward" : "forward");
    return;
  }
  if (e.key === "Enter" && !e.shiftKey) {
    const touchLayout = window.innerWidth <= 720 && matchMedia("(pointer: coarse)").matches;
    if (touchLayout && !e.metaKey && !e.ctrlKey) return;
    e.preventDefault(); submitText();
  }
});

$("promptMeta").addEventListener("click", (e) => {
  if (!e.target.closest("#modeBadge")) return;
  e.preventDefault();
  switchMode(e.shiftKey ? "backward" : "forward");
  ta.focus();
});

// Morph button: Stop while a turn runs, else Send. The Queue button (visible
// only while running) submits a follow-up the backend parks until this turn ends.
$("btnSend").addEventListener("click", () => { if (STATE.running) stopTurn(); else submitText(); });
$("btnQueue").addEventListener("click", () => {
  const hasPayload = ta.value.trim() || IMAGE_ATTACHMENTS.length;
  if (STATE.running) {
    if (!hasPayload) return;
    submitText();
    return;
  }
  if (!hasPayload) {
    ta.value = "/";
    grow();
    updateCmp();
  }
  ta.focus();
});
$("btnImage").addEventListener("click", (e) => {
  if (e.currentTarget && e.currentTarget.tagName === "LABEL") return;
  e.preventDefault();
  const input = $("imageInput");
  if (input) input.click();
});
$("btnImage").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  e.preventDefault();
  const input = $("imageInput");
  if (input) input.click();
});
$("imageInput").addEventListener("change", async (e) => {
  await addImageFiles(e.target.files);
  e.target.value = "";
});
$("imageTray").addEventListener("click", (e) => {
  const image = e.target.closest("img");
  if (image) {
    const chip = image.closest(".img-chip");
    const label = chip?.querySelector("span")?.textContent || "待发送图片";
    openImageLightbox(image.src, label);
    return;
  }
  const btn = e.target.closest("button");
  if (!btn) return;
  const chip = btn.closest(".img-chip");
  if (!chip) return;
  IMAGE_ATTACHMENTS.splice(Number(chip.dataset.i || 0), 1);
  renderImageAttachments();
});
$("imageTray").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const image = e.target.closest("img");
  if (!image) return;
  e.preventDefault();
  const chip = image.closest(".img-chip");
  const label = chip?.querySelector("span")?.textContent || "待发送图片";
  openImageLightbox(image.src, label);
});
$("queued").addEventListener("click", (e) => {
  if (!e.target.closest(".qx")) return;
  const item = e.target.closest(".qitem");
  const qid = item && item.dataset.qid;
  if (qid && STATE.sid) api("POST", `/api/sessions/${enc(STATE.sid)}/queue/cancel`, { id: qid });
});

blocksRoot.addEventListener("click", (e) => {
  const menuButton = e.target.closest("[data-turn-menu]");
  if (menuButton) {
    e.preventDefault();
    const actions = menuButton.closest(".user-actions");
    const opening = !actions.classList.contains("open");
    closeTurnActionMenus();
    actions.classList.toggle("open", opening);
    menuButton.setAttribute("aria-expanded", opening ? "true" : "false");
    return;
  }
  const button = e.target.closest("[data-turn-action]");
  if (!button) return;
  e.preventDefault();
  closeTurnActionMenus();
  rewindSentTurn(button.dataset.rewindId || "", button.dataset.turnAction || "edit");
});

function closeTurnActionMenus() {
  blocksRoot.querySelectorAll(".user-actions.open").forEach((actions) => {
    actions.classList.remove("open");
    actions.querySelector("[data-turn-menu]")?.setAttribute("aria-expanded", "false");
  });
}

document.addEventListener("click", (e) => {
  if (!e.target.closest(".user-actions")) closeTurnActionMenus();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeTurnActionMenus();
});

$("imageLightbox")?.addEventListener("click", (e) => {
  if (e.target.closest(".image-lightbox-panel") && !e.target.closest("[data-close-lightbox]")) return;
  closeImageLightbox();
});

// ── AgentView ────────────────────────────────────────────────────────────────
const center = document.querySelector(".center");
const avTitle = $("av-title"), avBody = $("av-body"), avPos = $("av-pos");
let avJobs = [], avIndex = 0, avJob = null;
let avStateRefreshTimer = null, avPollTimer = null, avRenderSeq = 0;

async function openAgent(job) {
  if (!STATE.sid) return;
  avJobs = (STATE.agents || []).map((a) => a.job);
  if (!avJobs.includes(job)) avJobs = [job, ...avJobs];
  avJob = job;
  await renderAgent(job, { preserveScroll: false });
  closeMobilePanels();
  center.classList.add("av-open");
}
function scheduleAgentRefresh() {
  if (!avJob || !center.classList.contains("av-open")) return;
  // Throttle rather than debounce: token/child events may arrive continuously,
  // and resetting the timer on every event would starve the live view.
  if (avStateRefreshTimer) return;
  avStateRefreshTimer = setTimeout(() => {
    avStateRefreshTimer = null;
    renderAgent(avJob, { preserveScroll: true });
  }, 80);
}
async function renderAgent(job, { preserveScroll = false } = {}) {
  const sid = STATE.sid;
  const seq = ++avRenderSeq;
  const oldTop = avBody.scrollTop;
  const stick = avBody.scrollHeight - avBody.scrollTop - avBody.clientHeight < 80;
  let view;
  try { view = await api("GET", `/api/sessions/${enc(sid)}/agent/${enc(job)}`); }
  catch { return; }
  if (seq !== avRenderSeq || sid !== STATE.sid || job !== avJob) return;
  if (!view || view.detail) return;
  avJob = job;
  avIndex = typeof view.index === "number" ? view.index : avJobs.indexOf(job);
  avTitle.textContent = view.title || "";
  avBody.innerHTML = "";
  (view.blocks || []).forEach((b) => { const n = buildBlock(b); if (n) avBody.appendChild(n); });
  if (view.meta_text) { const m = el(`<div class="av-meta"><span class="mk">▪</span> </div>`); m.appendChild(document.createTextNode(view.meta_text)); avBody.appendChild(m); }
  scheduleMermaidRender(avBody);
  avPos.textContent = (avIndex + 1) + " of " + (view.count || avJobs.length || 1);
  if (!preserveScroll) avBody.scrollTop = 0;
  else if (stick) avBody.scrollTop = avBody.scrollHeight;
  else avBody.scrollTop = Math.min(oldTop, Math.max(0, avBody.scrollHeight - avBody.clientHeight));
  clearTimeout(avPollTimer);
  if (view.status === "running" || view.status === "pending") {
    avPollTimer = setTimeout(() => renderAgent(avJob, { preserveScroll: true }), 750);
  }
}
function closeAgent() {
  center.classList.remove("av-open");
  avJob = null;
  avRenderSeq++;
  clearTimeout(avStateRefreshTimer);
  clearTimeout(avPollTimer);
  avStateRefreshTimer = null;
  avPollTimer = null;
}
async function cycleAgent(d) {
  const records = STATE.agents.map((a) => a.job);
  if (!records.length) return;
  const next = (avIndex + d + records.length) % records.length;
  avJob = records[next];
  await renderAgent(avJob, { preserveScroll: false });
}
document.querySelectorAll(".av-nav").forEach((n) => n.addEventListener("click", () => {
  const act = n.dataset.act;
  if (act === "main") closeAgent();
  else if (act === "prev") cycleAgent(-1);
  else if (act === "next") cycleAgent(1);
}));
document.addEventListener("keydown", (e) => {
  if (!center.classList.contains("av-open")) return;
  if (e.key === "Escape") { e.preventDefault(); closeAgent(); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); cycleAgent(-1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); cycleAgent(1); }
});

// ── boot ─────────────────────────────────────────────────────────────────────
(async function boot() {
  // Throttled viewport height sync — at most one CSS-variable write per frame
  // even when the soft-keyboard animation fires multiple resize+scroll events.
  // On the native iOS shell the WKWebView frame is animated in lockstep with
  // the keyboard (WebView.swift), and the page fills that frame via the
  // `.native-ios` CSS rules. Driving --app-height from visualViewport here too
  // would fight the native animation and reintroduce the stutter — so on native
  // we only keep the log pinned to the bottom and let native own the height.
  const nativeIOS = document.documentElement.classList.contains("native-ios");
  // The native iOS shell animates the WKWebView frame itself (WebView.swift),
  // so WebKit must stay hands-off the viewport — otherwise its own keyboard
  // resize fights the native frame and the page dips/bounces. Web browsers, by
  // contrast, should let the engine resize the layout viewport for the keyboard
  // (interactive-widget=resizes-content in the HTML), so 100dvh shrinks and the
  // bottom-pinned composer rides up smoothly with no JS compensation.
  if (nativeIOS) {
    const mv = document.querySelector('meta[name="viewport"]');
    if (mv) {
      mv.setAttribute(
        "content",
        "width=device-width, initial-scale=1.0, viewport-fit=cover, interactive-widget=overlays-content"
      );
    }
  }
  // We deliberately no longer drive --app-height/--app-top from visualViewport.
  // That manual `position: fixed` compensation lagged the keyboard by a frame
  // (rAF-throttled) and produced the "page dips then snaps back up on focus"
  // jump. The layout now relies on 100dvh + interactive-widget instead; we only
  // keep the log pinned to the latest message while the keyboard animates.
  // WebKit (iOS Safari *and* WKWebView) overlays the soft keyboard without
  // resizing the layout viewport (no interactive-widget support), so the
  // bottom-pinned composer would sit behind the keyboard. Measure the keyboard
  // height — the gap between the layout viewport (window.innerHeight, stable on
  // iOS) and the visual viewport — into --kb-inset, and the CSS lifts ONLY the
  // composer/status/queued chrome by it. The composer is pre-lifted on focus (see
  // the focus handler) so Safari never scroll-shifts the viewport and the top bar
  // stays put; this handler just refines the value to the real keyboard height.
  // On the native shell the WKWebView scroll view is disabled (WebView.swift), so
  // Safari can't shift either way.
  const updateKbInset = () => {
    const vv = window.visualViewport;
    if (!vv) return;
    const measured = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
    // Remember the real keyboard height so the next focus can pre-lift exactly.
    if (measured > 0) cachedKbInset = measured;
    // While focused, never let a mid-rise measurement (smaller than the cached
    // height) drop the composer back down — that dip would re-expose the input
    // and trigger Safari's scroll. Hold at the cached height until it settles.
    const applied = composerFocused ? Math.max(measured, cachedKbInset) : measured;
    setKbInset(applied);
  };
  let _vvRAF = null;
  const syncViewportHeight = () => {
    updateKbInset();
    if (_vvRAF) return;
    _vvRAF = requestAnimationFrame(() => {
      _vvRAF = null;
      if (followLatestDuringKeyboard) scrollDown();
    });
  };
  syncViewportHeight();
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", syncViewportHeight);
    window.visualViewport.addEventListener("scroll", syncViewportHeight);
  }
  if (window.ResizeObserver) {
    const bottomChromeObserver = new ResizeObserver(() => {
      const stick = nearBottom();
      syncMobileBottomChrome();
      if (stick) requestAnimationFrame(scrollDown);
    });
    bottomChromeObserver.observe($("composer"));
    bottomChromeObserver.observe($("statusbar"));
  }
  syncMobileBottomChrome();
  applyAppearance(localStorage.getItem(APPEARANCE_KEY) || DEFAULT_APPEARANCE, false);
  restoreRightRailWidth();
  setupRailResize();
  setupNativeChromeBridge();
  window.autoverseSetNativeBrowserWorkspace(
    Boolean(window.__autoverseNativeBrowserWorkspace)
  );
  setLeftTab(FILE_STATE.tab);
  loadCommands();
  const data = await api("GET", "/api/sessions");
  const sessions = data.sessions || [];
  const remembered = localStorage.getItem(ACTIVE_SESSION_KEY);
  const initial = sessions.find((s) => s.session_id === remembered) || sessions[0];
  if (initial) {
    updateSessionTitle(initial.title_display || initial.title || "AutoVerse");
    await switchSession(initial.session_id);
  }
  else await newSession();
})();
