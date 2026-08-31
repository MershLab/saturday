(() => {
"use strict";

const $ = (s) => document.querySelector(s);
const qs = new URLSearchParams(location.search);
const TOKEN = qs.get("k") || "";
// only write the cookie when the URL actually carried a token: after the
// server-side ?k= bootstrap strips the query, an unconditional write would
// clobber the good cookie with an empty value and 401 every API call
if (TOKEN) document.cookie = "df_token=" + TOKEN + "; path=/; SameSite=Strict";

function escHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}
async function api(path, opts = {}) {
  opts.headers = Object.assign({ "X-Saturday-Token": TOKEN }, opts.headers || {});
  if (opts.body && !opts.headers["Content-Type"]) opts.headers["Content-Type"] = "application/json";
  const r = await fetch(path, opts);
  connOk();
  if (!r.ok) {
    let msg = r.status + " " + r.statusText;
    try { msg = (await r.json()).error || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

const state = {
  sid: null,
  busy: false,
  info: null,
  sessions: [],
  projects: [],
  proj: localStorage.getItem("df_proj") || "",
  images: [],
  approvals: new Map(),
  asks: new Map(),
  sessionModels: {},
  queue: [],
  lastUser: "",
  tokBySid: {},
  costBySid: {},
  pins: JSON.parse(localStorage.getItem("df_pins") || "[]"),
  filter: "",
  stepNow: 0,
  ctxPrompt: 0,
  busySince: 0,
  turnSeq: 0,
  showArchived: false,
};

function curProject() {
  return state.projects.find((p) => p.id === state.proj) || null;
}
function fileUrl(p) {
  // img src can't carry headers; the df_token cookie set at boot authenticates
  // the request — never append the token to the URL (history/Referer leak)
  return "/api/file?p=" + encodeURIComponent(p) + "&sid=" + encodeURIComponent(state.sid || "");
}

/* ------------------------------------------------------------------ markdown */

function renderInline(s) {
  const codes = [];
  // SECURITY: stash the ALREADY-ESCAPED html, never the raw capture. Assistant
  // text is attacker-influenceable via tool results; re-inserting raw inline-code
  // content below would let `<img src=x onerror=...>` style payloads chain to
  // the df_token cookie -> /api/chat -> /api/approve (RCE).
  s = String(s).replace(/`([^`\n]+)`/g, (_, c) => { codes.push(escHtml(c)); return "\u0000C" + (codes.length - 1) + "\u0000"; });
  s = escHtml(s);
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  s = s.replace(/\*\*([^*\n][^*\n]*?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^\w*])\*([^*\n]+)\*(?=[^\w*]|$)/g, "$1<em>$2</em>");
  s = s.replace(/(^|\s)__([^_\n]+)__(?=\s|$)/g, "$1<strong>$2</strong>");
  s = s.replace(/(^|\s)_([^_\n]+)_(?=[\s.,!?]|$)/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  s = s.replace(/\u0000C(\d+)\u0000/g, (_, i) => "<code class=\"inline\">" + codes[+i] + "</code>");
  return s;
}

function hl(code, lang) {
  const kw = {
    python: "def|class|return|if|elif|else|for|while|try|except|finally|with|as|import|from|pass|break|continue|and|or|not|in|is|None|True|False|lambda|yield|raise|async|await|global|nonlocal|assert|del",
    javascript: "function|return|if|else|for|while|try|catch|finally|const|let|var|class|extends|new|delete|typeof|instanceof|in|of|this|null|undefined|true|false|async|await|yield|static|get|set|import|export|from|default|throw|switch|case|break|continue|do",
    typescript: "function|return|if|else|for|while|try|catch|finally|const|let|var|class|extends|implements|interface|type|enum|new|typeof|instanceof|in|of|this|null|undefined|true|false|async|await|yield|static|get|set|import|export|from|default|throw|switch|case|break|continue|public|private|protected|readonly|declare|namespace|as",
    json: "true|false|null",
    bash: "if|then|else|elif|fi|for|while|do|done|case|esac|function|return|export|local|echo|cd|set|unset|source",
    css: "",
    html: "",
  };
  const set = kw[lang] || kw.javascript;
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  if (lang === "html" || lang === "xml") {
    return esc(code)
      .replace(/(&lt;!--[\s\S]*?--&gt;)/g, '<span class="c">$1</span>')
      .replace(/(&lt;\/?)([\w-]+)/g, '$1<span class="t">$2</span>')
      .replace(/([\w-]+)=(&quot;[^&]*?&quot;)/g, '<span class="a">$1</span>=$2');
  }
  const re = new RegExp(
    "(#[^\\n]*|//[^\\n]*|/\\*[\\s\\S]*?\\*/)" +
      "|(&quot;(?:[^&]|&(?!quot;))*?&quot;|'(?:[^'\\\\]|\\\\.)*'|\"(?:[^\"\\\\]|\\\\.)*\")" +
      "|\\b(" + set + ")\\b" +
      "|\\b(0x[0-9a-fA-F]+|\\d+(?:\\.\\d+)?)\\b",
    "g"
  );
  let out = "";
  let last = 0;
  let m;
  while ((m = re.exec(code)) !== null) {
    out += esc(code.slice(last, m.index));
    const tok = m[0];
    if (m[1]) out += '<span class="c">' + esc(tok) + "</span>";
    else if (m[2]) out += '<span class="s">' + esc(tok) + "</span>";
    else if (m[3]) out += '<span class="k">' + esc(tok) + "</span>";
    else out += '<span class="n">' + esc(tok) + "</span>";
    last = m.index + tok.length;
  }
  out += esc(code.slice(last));
  return out;
}

function renderMd(src) {
  src = String(src || "");
  const fences = [];
  src = src.replace(/```([^\n`]*)\n?([\s\S]*?)(?:```|$)/g, (_, lang, code) => {
    fences.push({ lang: lang.trim(), code });
    return "\u0000B" + (fences.length - 1) + "\u0000";
  });
  const lines = src.split("\n");
  const out = [];
  let para = [];
  const flushPara = () => {
    if (!para.length) return;
    out.push("<p>" + para.map(renderInline).join("<br>") + "</p>");
    para = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const bm = line.match(/^\s*\u0000B(\d+)\u0000\s*$/);
    if (bm) {
      flushPara();
      const f = fences[+bm[1]];
      const langMap = { py: "python", python: "python", js: "javascript", javascript: "javascript", ts: "typescript", typescript: "typescript", jsx: "javascript", tsx: "typescript", json: "json", sh: "bash", bash: "bash", shell: "bash", zsh: "bash", html: "html", xml: "html", css: "css" };
      const hlLang = langMap[(f.lang || "").toLowerCase()];
      const codeHtml = hlLang ? hl(f.code.replace(/\n$/, ""), hlLang) : escHtml(f.code.replace(/\n$/, ""));
      out.push(
        '<div class="codewrap"><div class="codebar"><span>' + escHtml(f.lang || "text") + '</span><button class="copybtn">copy</button></div><pre><code>' +
        codeHtml + "</code></pre></div>"
      );
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { flushPara(); const lv = Math.min(h[1].length, 4); out.push("<h" + lv + ">" + renderInline(h[2]) + "</h" + lv + ">"); continue; }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flushPara(); out.push("<hr>"); continue; }
    if (/^\s*&gt;\s?/.test(line)) {
      flushPara();
      const quote = [];
      while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) { quote.push(lines[i].replace(/^\s*&gt;\s?/, "")); i++; }
      i--;
      out.push("<blockquote>" + quote.map(renderInline).join("<br>") + "</blockquote>");
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?[\s:|-]*-[\s:|-]*$/.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      flushPara();
      const cells = (r) => r.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|")) { rows.push(cells(lines[i])); i++; }
      i--;
      let t = "<table><thead><tr>" + head.map((c) => "<th>" + renderInline(c) + "</th>").join("") + "</tr></thead><tbody>";
      for (const row of rows) t += "<tr>" + row.map((c) => "<td>" + renderInline(c) + "</td>").join("") + "</tr>";
      out.push(t + "</tbody></table>");
      continue;
    }
    const li = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (li) {
      flushPara();
      const stack = [];
      while (true) {
        const m2 = lines[i] ? lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/) : null;
        if (!m2) break;
        const indent = m2[1].length;
        const type = /\d/.test(m2[2]) ? "ol" : "ul";
        while (stack.length && stack[stack.length - 1].indent > indent) { out.push("</li></" + stack.pop().type + ">"); }
        if (stack.length && stack[stack.length - 1].indent === indent) {
          out.push("</li><li>" + renderInline(m2[3]));
        } else {
          stack.push({ indent, type });
          out.push("<" + type + "><li>" + renderInline(m2[3]));
        }
        i++;
      }
      i--;
      while (stack.length) out.push("</li></" + stack.pop().type + ">");
      continue;
    }
    if (!line.trim()) { flushPara(); continue; }
    para.push(line);
  }
  flushPara();
  return out.join("");
}

function renderDiff(diffText) {
  const box = el("div", "diff");
  for (const ln of String(diffText).split("\n")) {
    const cls = ln.startsWith("+") && !ln.startsWith("+++") ? "add" : ln.startsWith("-") && !ln.startsWith("---") ? "del" : ln.startsWith("@@") ? "hunk" : "meta";
    const d = el("div", "dl " + cls);
    d.textContent = ln || " ";
    box.appendChild(d);
  }
  return box;
}

/* --------------------------------------------------------------- workbench */

const stagePanes = {
  home: $("#stageHome"),
  activity: $("#stageActivity"),
  changes: $("#stageChanges"),
  preview: $("#stagePreview"),
  plan: $("#stagePlan"),
  files: $("#stageFiles"),
  runs: $("#stageRuns"),
  memory: $("#stageMemory"),
  pipelines: $("#stagePipelines"),
};
const stage = {
  tab: "home", manual: false, entries: new Map(), imageSrcs: [],
  counts: { activity: 0, changes: 0, preview: 0, plan: 0 }, running: 0,
  changePaths: new Map(),  // path -> latest {add, del}; badge counts unique files
  activityFilter: "",
  homeRows: new Map(),     // tool card -> Workbench "latest activity" row
  run: null,               // current/last run stats for the Workbench dashboard
};

function stageShow(tab, auto) {
  if (auto && isAssistant()) return; // assistant: no technical stage noise
  if (auto && stage.manual) return;
  if (!auto) stage.manual = true;
  stage.tab = tab;
  for (const [k, elp] of Object.entries(stagePanes)) elp.classList.toggle("on", k === tab);
  document.querySelectorAll(".stage-tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  if (tab === "activity") stagePanes.activity.scrollTop = stagePanes.activity.scrollHeight;
  if (tab === "changes") stagePanes.changes.scrollTop = 0; // newest file sits on top
  // the graph animates, so it only runs while it is the visible pane
  if (tab === "memory") mgOpen(); else mgClose();
  if (tab === "pipelines") loadPipelines();
}

function stageBadge(name, n) {
  const b = $("#badge" + name[0].toUpperCase() + name.slice(1));
  if (!b) return;
  b.textContent = n > 0 ? String(n) : "";
  b.classList.toggle("hidden", !(n > 0));
}

function stageMeta() {
  const m = $("#stageMeta");
  if (state.busy || stage.running > 0) {
    const bits = [];
    if (state.stepNow > 0 && !isAssistant()) bits.push("step " + (state.stepNow + 1));
    if (stage.running > 0) bits.push(stage.running + " tool" + (stage.running > 1 ? "s" : "") + " running");
    if (isAssistant() && state.busySince) {
      const secs = Math.max(1, Math.round((Date.now() - state.busySince) / 1000));
      bits.push(secs < 90 ? secs + "s" : Math.floor(secs / 60) + "m" + String(secs % 60).padStart(2, "0"));
    }
    m.innerHTML = "";
    const dot = el("span", "live-dot");
    m.append(dot, document.createTextNode(bits.join(" \u00b7 ") || (isAssistant() ? "on it" : "working")));
  } else {
    m.textContent = isAssistant() ? "at your service" : "idle";
  }
  activitySub();
}

function stageEnsureHead(pane, title) {
  let head = pane.querySelector(".stage-head");
  if (!head) {
    const emptyNote = pane.querySelector(".stage-empty");
    if (emptyNote) emptyNote.remove();
    head = el("div", "stage-head");
    head.appendChild(el("span", "stage-head-title", title));
    head.appendChild(el("span", "stage-head-sub", ""));
    pane.appendChild(head);
    const list = el("div", "stage-list");
    pane.appendChild(list);
  }
  return pane.querySelector(".stage-list");
}

function stageList(name) {
  const list = stageEnsureHead(stagePanes[name], name === "activity" ? "Activity" : name[0].toUpperCase() + name.slice(1));
  if (name === "activity") activityEnsureToolbar();
  if (name === "changes") changesEnsureToolbar();
  return list;
}

/* Per-edit restore (Cline/Roo/Cursor parity): the file journal records the
   pre-edit content of every write, so any single agent edit can be undone —
   or the whole workspace rewound to a checkpoint state. */
let journalRows = [];

function changesEnsureToolbar() {
  const pane = stagePanes.changes;
  if (pane.querySelector(".stage-toolbar")) return;
  const head = pane.querySelector(".stage-head");
  const bar = el("div", "stage-toolbar");
  const undo = el("button", "st-btn", "undo last edit");
  undo.title = "Restore the most recent journaled file change (/revert)";
  undo.addEventListener("click", async () => {
    if (!state.sid) { toast("No session", "info"); return; }
    if (!(await uiConfirm({ title: "Undo last edit", message: "Undo the most recent file change?", ok: "Undo", danger: true }))) return;
    try {
      const r = await api("/api/journal/restore", { method: "POST", body: JSON.stringify({ session_id: state.sid, index: 0 }) });
      toast(r.message || (r.ok ? "restored" : "restore failed"), r.ok ? "ok" : "err");
      if (r.ok) { renderJournal(); stageBadgeRefresh(); refreshGitChip(); }
    } catch (e) { toast(e.message, "err"); }
  });
  const hist = el("button", "st-btn", "restore history");
  hist.title = "Per-edit checkpoint restore from the file journal";
  hist.addEventListener("click", () => {
    const panel = pane.querySelector(".journal-panel");
    if (panel) { panel.remove(); return; }
    const wrap = el("div", "journal-panel loading", "loading journal\u2026");
    pane.insertBefore(wrap, head.nextSibling);
    renderJournal();
  });
  const review = el("button", "st-btn", "review changes");
  review.title = "Ask the agent to review the changes it just made (Codex /review, Cursor Bugbot parity)";
  review.addEventListener("click", () => {
    const files = [...stage.changePaths.keys()];
    if (!files.length) { toast("No changes to review yet", "info"); return; }
    const input = $("#input");
    input.value = "Review the changes you just made to " + (files.length <= 6 ? files.join(", ") : files.length + " files") +
      ". Look for bugs, edge cases and missed requirements; fix anything you find.";
    autoGrow(input);
    updateSendEnabled();
    send();
  });
  const chip = el("button", "git-chip hidden");
  chip.title = "Git working-tree status";
  chip.addEventListener("click", () => refreshGitChip(true));
  bar.append(undo, hist, review, chip);
  pane.insertBefore(bar, head);
  refreshGitChip();
}

async function renderJournal() {
  const pane = stagePanes.changes;
  const panel = pane.querySelector(".journal-panel");
  if (!panel) return;
  let data;
  try { data = await api("/api/journal?sid=" + encodeURIComponent(state.sid || "")); }
  catch (e) {
    panel.classList.remove("loading");
    panel.textContent = "journal unavailable: " + e.message;
    return;
  }
  journalRows = data.entries || [];
  panel.classList.remove("loading");
  panel.replaceChildren();
  panel.appendChild(el("div", "journal-title", "File journal \u2014 newest first (restore reverts one edit)"));
  if (!journalRows.length) {
    panel.appendChild(el("div", "journal-empty", "no journaled edits yet in this workspace"));
    return;
  }
  journalRows.forEach((e) => {
    const row = el("div", "journal-row");
    row.appendChild(el("span", "journal-tool", e.tool));
    const path = el("span", "journal-path", e.path);
    path.title = e.path;
    row.appendChild(path);
    row.appendChild(el("span", "journal-meta", (e.existed ? "" : "new file \u00b7 ") + (e.chars ? e.chars.toLocaleString() + " chars" : "")));
    const cmp = el("button", "st-btn", "compare");
    cmp.title = "Preview the before/after diff for this edit";
    cmp.addEventListener("click", () => openCompare(e.index, e.path));
    row.appendChild(cmp);
    const btn = el("button", "st-btn", "restore");
    btn.addEventListener("click", async () => {
      if (!(await uiConfirm({ title: "Restore edit", message: "Restore \u201c" + e.path + "\u201d to its pre-" + e.tool + " state?", ok: "Restore", danger: true }))) return;
      try {
        const r = await api("/api/journal/restore", { method: "POST", body: JSON.stringify({ session_id: state.sid, index: e.index }) });
        toast(r.message || (r.ok ? "restored" : "restore failed"), r.ok ? "ok" : "err");
        if (r.ok) renderJournal();
      } catch (err) { toast(err.message, "err"); }
    });
    row.appendChild(btn);
    panel.appendChild(row);
  });
}

function stageBadgeRefresh() {
  // a restore changes the workspace; the diff cards may be stale — keep them
  // (they show what happened) but drop the badge glow
  stageBadge("changes", stage.changePaths.size);
}

function activityEnsureToolbar() {
  const pane = stagePanes.activity;
  if (pane.querySelector(".stage-toolbar")) return;
  const bar = el("div", "stage-toolbar");
  const inp = el("input", "stage-filter");
  inp.type = "text";
  inp.placeholder = "filter calls\u2026";
  inp.spellcheck = false;
  inp.addEventListener("input", () => {
    stage.activityFilter = inp.value.trim().toLowerCase();
    activityApplyFilter();
  });
  const clear = el("button", "st-btn", "clear");
  clear.title = "Clear the activity list (running calls are unaffected; counts restart)";
  clear.addEventListener("click", () => {
    stage.counts.activity = 0;
    stageBadge("activity", 0);
    const list = pane.querySelector(".stage-list");
    if (list) list.replaceChildren();
    activitySub();
  });  bar.append(inp, clear);
  const head = pane.querySelector(".stage-head");
  if (head) head.after(bar);
}

function activityApplyFilter() {
  const list = stagePanes.activity.querySelector(".stage-list");
  if (!list) return;
  const q = stage.activityFilter;
  for (const row of list.children) {
    if (!row.classList.contains("wb-entry")) continue;
    row.classList.toggle("filtered", !!q && !row.textContent.toLowerCase().includes(q));
  }
}

function activitySub() {
  const pane = stagePanes.activity;
  const sub = pane.querySelector(".stage-head-sub");
  if (!sub) return;
  const list = pane.querySelector(".stage-list");
  const n = list ? list.querySelectorAll(".wb-entry").length : 0;
  const bits = [n + " call" + (n === 1 ? "" : "s")];
  if (stage.running) bits.push(stage.running + " running");
  sub.textContent = bits.join(" \u00b7 ");
}

function stageEntry(card, name, args, running, silent) {
  const list = stageList("activity");
  const e = el("div", "wb-entry" + (running ? " running open" : ""));
  const head = el("div", "wb-head");
  head.appendChild(el("div", "wb-glyph", glyph(name)));
  head.appendChild(el("span", "wb-name", name));
  const sum = el("span", "wb-args", argsSummary(name, args));
  head.appendChild(sum);
  const cp = el("button", "wb-copy", "copy");
  cp.title = "Copy tool, arguments and output";
  cp.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const res = e._res || {};
    const text = "tool: " + name + "\narguments: " + JSON.stringify(args || {}, null, 2) + "\n\n" + (res.ok ? res.output : res.error || res.output || "");
    navigator.clipboard.writeText(text).then(() => {
      cp.textContent = "copied";
      setTimeout(() => (cp.textContent = "copy"), 1200);
    });
  });
  head.appendChild(cp);
  const status = el("span", "chip chip-run", "run");
  head.appendChild(status);
  const time = el("span", "wb-time", new Date().toTimeString().slice(0, 5));
  head.appendChild(time);
  e.appendChild(head);
  const body = el("div", "wb-body");
  e.appendChild(body);
  head.addEventListener("click", () => e.classList.toggle("open"));
  e._fill = (res) => {
    e._res = res;
    e.classList.remove("running");
    if (running) { stage.running = Math.max(0, stage.running - 1); stageMeta(); }
    if (res.ok) { status.className = "chip chip-ok"; status.textContent = "ok"; }
    else if (res.error && /denied|BLOCK|BACKGROUND/i.test(res.error)) { status.className = "chip chip-blk"; status.textContent = "blocked"; }
    else { status.className = "chip chip-err"; status.textContent = "error"; }
    body.replaceChildren();
    const secA = el("div", "wb-section");
    secA.appendChild(el("div", "tool-label", "arguments"));
    const termA = el("div", "term");
    const preA = el("pre");
    preA.textContent = JSON.stringify(args || {}, null, 2);
    termA.appendChild(preA);
    secA.appendChild(termA);
    body.appendChild(secA);
    const text = res.ok ? res.output : res.error || res.output || "";
    if (text) {
      const secO = el("div", "wb-section");
      secO.appendChild(el("div", "tool-label", res.ok ? "output" : "error"));
      const termO = el("div", "term");
      const preO = el("pre");
      preO.textContent = text.length > 20000 ? text.slice(0, 20000) + "\n\u2026 [" + (text.length - 20000) + " more chars]" : text;
      termO.appendChild(preO);
      secO.appendChild(termO);
      body.appendChild(secO);
    }
    if (res.images && res.images.length) {
      const secI = el("div", "wb-section");
      secI.appendChild(el("div", "tool-label", "images"));
      const grid = el("div", "tool-imgs");
      for (const p of res.images) {
        const img = document.createElement("img");
        img.src = fileUrl(p);
        img.loading = "lazy";
        img.addEventListener("click", () => window.open(img.src, "_blank"));
        grid.appendChild(img);
      }
      secI.appendChild(grid);
      body.appendChild(secI);
    }
    activitySub();
  };
  list.appendChild(e);
  e.classList.toggle("filtered", !!stage.activityFilter && !e.textContent.toLowerCase().includes(stage.activityFilter));
  stage.entries.set(card, e);
  if (running) { stage.running++; stageMeta(); }
  if (!silent) {
    stage.counts.activity++;
    stageBadge("activity", stage.counts.activity);
    activitySub();
    stageShow("activity", true);
  }
  return e;
}

function stageFocus(card) {
  const e = stage.entries.get(card);
  if (!e || !e.isConnected) return;
  stageShow("activity", false);
  e.classList.add("open");
  e.scrollIntoView({ block: "start", behavior: "smooth" });
}

function statCount(diffText) {
  let add = 0, del = 0;
  for (const ln of String(diffText).split("\n")) {
    if (ln.startsWith("+") && !ln.startsWith("+++")) add++;
    else if (ln.startsWith("-") && !ln.startsWith("---")) del++;
  }
  return { add, del };
}

const MAX_DIFF_LINES = 800;
function appendDiffLines(bodyEl, text, newFile) {
  const lines = String(text || "").split("\n");
  const show = lines.length > MAX_DIFF_LINES ? lines.slice(0, MAX_DIFF_LINES) : lines;
  for (const ln of show) {
    const cls = newFile ? "add" : ln.startsWith("+") && !ln.startsWith("+++") ? "add" : ln.startsWith("-") && !ln.startsWith("---") ? "del" : ln.startsWith("@@") ? "hunk" : "meta";
    const d = el("div", "dl " + cls);
    d.textContent = newFile ? "+ " + ln : ln || " ";
    bodyEl.appendChild(d);
  }
  if (lines.length > show.length) {
    bodyEl.appendChild(el("div", "dl meta", "\u2026 [" + (lines.length - show.length) + " more lines \u2014 use copy for the full text]"));
  }
}

function changesSub() {
  const sub = stagePanes.changes.querySelector(".stage-head-sub");
  if (!sub) return;
  let add = 0, del = 0;
  for (const v of stage.changePaths.values()) { add += v.add; del += v.del; }
  const n = stage.changePaths.size;
  sub.textContent = n ? n + " file" + (n === 1 ? "" : "s") + " \u00b7 +" + add + " -" + del : "";
}

function stageAddChange(c, auto = true) {
  const list = stageList("changes");
  const existing = list.querySelector('[data-path="' + CSS.escape(c.path) + '"]');
  if (existing) existing.remove();
  const stat = c.diff ? statCount(c.diff) : { add: String(c.content || "").split("\n").length, del: 0 };
  stage.changePaths.set(c.path, stat);
  if (stage.run && !stage.run.endedAt) { stage.run.files.add(c.path); homeRunUpdate(); }
  const box = el("div", "change-file");
  box.dataset.path = c.path;
  const head = el("div", "change-head");
  head.appendChild(el("span", "change-caret", "\u25be"));
  head.appendChild(el("span", "change-path", c.path));
  head.title = c.path;
  const st = el("span", "change-stat");
  if (c.diff) st.innerHTML = '<span class="addn">+' + stat.add + "</span> <span class='deln'>-" + stat.del + "</span>";
  else st.innerHTML = '<span class="addn">+' + stat.add + " new</span>";
  head.appendChild(st);
  const full = c.diff || c.content || "";
  const cp = el("button", "wb-copy", "copy");
  cp.title = "Copy " + (c.diff ? "diff" : "file content");
  cp.addEventListener("click", (ev) => {
    ev.stopPropagation();
    navigator.clipboard.writeText(full).then(() => {
      cp.textContent = "copied";
      setTimeout(() => (cp.textContent = "copy"), 1200);
    });
  });
  head.appendChild(cp);
  const attach = el("button", "wb-copy", "attach");
  attach.title = "Insert this diff into the composer as context (Warp parity)";
  attach.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const input = $("#input");
    const fence = "```diff\n" + full.slice(0, 8000) + "\n```";
    input.value = (input.value ? input.value.replace(/\s*$/, "") + "\n\n" : "") + "Regarding " + c.path + ":\n" + fence + "\n";
    autoGrow(input);
    updateSendEnabled();
    input.focus();
  });
  head.appendChild(attach);
  const openB = el("button", "wb-copy", "open");
  openB.title = "Open the current file contents in a new window";
  openB.addEventListener("click", (ev) => { ev.stopPropagation(); openWsFile(c.path); });
  head.appendChild(openB);
  box.appendChild(head);
  const bodyEl = el("div", "change-body" + (c.diff ? "" : " change-new"));
  appendDiffLines(bodyEl, full, !c.diff);
  box.appendChild(bodyEl);
  head.addEventListener("click", () => box.classList.toggle("collapsed"));
  list.prepend(box); // newest file on top so auto-switch shows it immediately
  changesSub();
  stageBadge("changes", stage.changePaths.size);
  if (auto) { stageShow("changes", true); stagePanes.changes.scrollTop = 0; }
}

function stageAddImage(src) {
  stage.imageSrcs.push(src);
  if (stage.imageSrcs.length > 30) stage.imageSrcs.shift(); // cap thumbnail memory
  const pane = stagePanes.preview;
  let wrap = pane.querySelector(".preview-wrap");
  if (!wrap) {
    wrap = el("div", "preview-wrap");
    const main = document.createElement("img");
    main.className = "preview-main";
    wrap.appendChild(main);
    const cap = el("div", "preview-cap");
    const openB = el("button", "st-btn", "open");
    openB.title = "Open the current screenshot in a new window";
    openB.addEventListener("click", () => window.open(main.src, "_blank"));
    const dlB = el("button", "st-btn", "download");
    dlB.title = "Download the current screenshot";
    dlB.addEventListener("click", () => {
      const a = document.createElement("a");
      a.href = main.src;
      a.download = "";
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      a.remove();
    });
    cap.append(openB, dlB, el("span", "preview-count"));
    wrap.appendChild(cap);
    wrap.appendChild(el("div", "preview-strip"));
    pane.replaceChildren(wrap);
  }
  const main = wrap.querySelector(".preview-main");
  main.src = src;
  const strip = wrap.querySelector(".preview-strip");
  strip.replaceChildren();
  stage.imageSrcs.forEach((s) => {
    const th = document.createElement("img");
    th.src = s;
    th.loading = "lazy";
    th.addEventListener("click", () => {
      main.src = s;
      strip.querySelectorAll("img").forEach((x) => x.classList.remove("on"));
      th.classList.add("on");
    });
    strip.appendChild(th);
  });
  const last = strip.lastElementChild;
  if (last) last.classList.add("on");
  wrap.querySelector(".preview-count").textContent =
    stage.imageSrcs.length + " screenshot" + (stage.imageSrcs.length === 1 ? "" : "s");
  stage.counts.preview = stage.imageSrcs.length;
  stageBadge("preview", stage.counts.preview);
}

function parsePlan(text) {
  let goal = "";
  const steps = [];
  for (const ln of String(text || "").split("\n")) {
    const g = ln.match(/^goal:\s*(.*)$/i);
    if (g && !steps.length) { goal = g[1].trim(); continue; }
    const cm = ln.match(/^\s*(?:\d+[.)]|[-*])\s+\[([ xX])\]\s+(.+)$/);
    if (cm) { steps.push({ done: cm[1].toLowerCase() === "x", text: cm[2].trim() }); continue; }
    const nm = ln.match(/^\s*(\d+[.)])\s+(.+)$/);
    if (nm) steps.push({ done: false, text: nm[2].trim() });
  }
  return goal || steps.length ? { goal, steps } : null;
}

function stageSetPlan(text) {
  const pane = stagePanes.plan;
  const panel = el("div", "plan-panel");
  const parsed = parsePlan(text);
  if (parsed) {
    const doneN = parsed.steps.filter((s) => s.done).length;
    const pct = parsed.steps.length ? Math.round((doneN / parsed.steps.length) * 100) : 0;
    const head = el("div", "plan-head");
    if (parsed.goal) head.appendChild(el("div", "plan-goal", parsed.goal));
    const bar = el("div", "plan-bar");
    const fill = el("div", "plan-bar-fill");
    fill.style.width = pct + "%";
    bar.appendChild(fill);
    head.appendChild(bar);
    head.appendChild(el("div", "plan-progress", doneN + "/" + parsed.steps.length + " steps \u00b7 " + pct + "% \u00b7 updated " + new Date().toTimeString().slice(0, 5)));
    panel.appendChild(head);
    if (state.info && state.info.plan_mode) {
      // Replit/Jules parity: an explicit gate between plan and execution
      const act = el("div", "plan-actions");
      const b = el("button", "primary-btn plan-approve", "Approve plan & switch to Act");
      b.title = "Turns plan mode off for this chat so the agent can execute";
      b.addEventListener("click", approvePlan);
      act.appendChild(b);
      panel.appendChild(act);
    }
    const list = el("div", "plan-steps");
    parsed.steps.forEach((s, i) => {
      const row = el("div", "plan-step" + (s.done ? " done" : ""));
      row.appendChild(el("span", "plan-mark", s.done ? "\u2713" : String(i + 1)));
      row.appendChild(el("span", "plan-text", s.text));
      list.appendChild(row);
    });
    panel.appendChild(list);
  } else {
    const pre = el("pre");
    pre.textContent = text;
    panel.appendChild(pre);
  }
  const raw = el("details", "plan-raw");
  raw.appendChild(el("summary", "", "raw output"));
  const rp = el("pre");
  rp.textContent = text;
  raw.appendChild(rp);
  panel.appendChild(raw);
  pane.replaceChildren(panel);
  stage.counts.plan++;
  stageBadge("plan", stage.counts.plan);
}

const STAGE_EMPTY_COPY = {
  activity: "Tool calls, command output and web results appear here while the agent works.",
  changes: "File edits land here \u2014 each one is journaled, so you can undo or rewind it.",
  preview: "Screenshots and live app previews appear here.",
  plan: "The agent's plan appears here \u2014 toggle plan mode to make it plan before acting.",
};

function stageReset() {
  for (const k of ["activity", "changes", "preview", "plan"]) {
    stagePanes[k].replaceChildren(el("div", "stage-empty", STAGE_EMPTY_COPY[k]));
    stage.counts[k] = 0;
    stageBadge(k, 0);
  }
  stage.entries.clear();
  stage.imageSrcs = [];
  stage.running = 0;
  stage.changePaths.clear();
  stage.homeRows.clear();
  stage.run = null;
  $("#homeRun").classList.add("hidden");
  filesLoaded = false;
  filesRel = "";
  stage.manual = false;
  stageMeta();
  stageShow("home", true);
}

/* -- workbench: live run dashboard (steps / tools / tokens / files) -- */

function fmtElapsed(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return s + "s";
  return Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
}

function homeRunInit() {
  stage.run = { step: 0, tools: 0, tokens: 0, files: new Set(), startedAt: Date.now(), endedAt: 0 };
  $("#homeRun").classList.remove("hidden");
  $("#homeActivity").replaceChildren();
  stage.homeRows.clear();
  homeRunUpdate();
}

function homeRunUpdate() {
  const r = stage.run;
  if (!r) return;
  $("#homeStep").textContent = r.step ? String(r.step) : "\u2014";
  $("#homeTools").textContent = String(r.tools);
  $("#homeTokens").textContent = r.tokens >= 1000 ? (r.tokens / 1000).toFixed(1) + "k" : String(r.tokens);
  $("#homeFiles").textContent = String(r.files.size);
  $("#homeElapsed").textContent = fmtElapsed((r.endedAt || Date.now()) - r.startedAt);
  $("#homeStatus").textContent = r.endedAt
    ? "Last run finished \u2014 full traces in the tabs above."
    : "Working \u2014 live tool activity below.";
}

function homeRowAdd(card, name, args) {
  if (!stage.run) return;
  const list = $("#homeActivity");
  const row = el("div", "home-act-row running");
  row.appendChild(el("span", "wb-glyph", glyph(name)));
  row.appendChild(el("span", "home-act-name", name));
  const sum = el("span", "home-act-sum", argsSummary(name, args));
  sum.title = sum.textContent;
  row.appendChild(sum);
  row.appendChild(el("span", "chip chip-run", "run"));
  row.addEventListener("click", () => stageFocus(card));
  list.appendChild(row);
  stage.homeRows.set(card, row);
  while (list.children.length > 6) list.firstElementChild.remove();
}

function homeRowDone(card, res) {
  const row = stage.homeRows.get(card);
  if (!row) return;
  row.classList.remove("running");
  const chip = row.querySelector(".chip");
  if (!chip) return;
  if (res.ok) { chip.className = "chip chip-ok"; chip.textContent = "ok"; }
  else if (res.error && /denied|BLOCK|BACKGROUND/i.test(res.error)) { chip.className = "chip chip-blk"; chip.textContent = "blocked"; }
  else { chip.className = "chip chip-err"; chip.textContent = "error"; }
}

function stageHomeFill() {
  const info = state.info;
  if (!info) return;
  const ws = $("#homeWorkspace");
  const root = info.workspace_root || "";
  // long absolute paths wrap mid-word in the narrow card; abbreviate the
  // middle segments instead and keep the full path on hover
  const segs = root.split(/[\\/]/).filter(Boolean);
  ws.textContent = segs.length > 2 ? "…\\" + segs.slice(-2).join("\\") : root;
  ws.title = root;
  $("#homeModel").textContent = (info.provider || "") + " / " + (info.model || "");
  $("#homeSafety").textContent = info.safety_mode || "";
  const p = curProject();
  const hp = $("#homeProject");
  if (hp) hp.textContent = p ? p.name : "\u2014";
}

/* --------------------------------------------------------------- transcript */

const thread = $("#thread");
const transcriptEl = $("#transcript");
let stickBottom = true;
transcriptEl.addEventListener("scroll", () => {
  stickBottom = transcriptEl.scrollTop + transcriptEl.clientHeight >= transcriptEl.scrollHeight - 130;
  $("#scrollBtn").classList.toggle("hidden", stickBottom);
});
function scrollDown(force) {
  if (force || stickBottom) {
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
    $("#scrollBtn").classList.add("hidden");
  }
}

function hideEmptyState() { $("#emptyState").classList.add("hidden"); }
function showEmptyState() {
  $("#emptyState").classList.remove("hidden");
  thread.replaceChildren();
  $("#sessionTitle").textContent = "New chat";
  document.title = "Saturday";
}

const GLYPHS = { shell: "$", python: "py", write_file: "wr", edit_file: "ed", read_file: "rd", web_fetch: "wf", web_search: "ws", view_image: "im", todo: "td", memory: "m", task: "tk" };
function glyph(name) { return GLYPHS[name] || (name ? name.slice(0, 2) : "?"); }

/* Assistant mode: full capability underneath, plain-language surface.
   Tool heads show what it's DOING, not which tool does it. */
function isAssistant() { return !!(state.info && state.info.persona_mode === "assistant"); }
const FRIENDLY_VERBS = {
  shell: "running a command",
  python: "working out the logic",
  read_file: "reading a file",
  write_file: "writing a file",
  edit_file: "editing a file",
  list_dir: "looking through folders",
  glob: "finding files",
  grep: "searching files",
  web_search: "searching the web",
  web_fetch: "reading a page",
  browser: "browsing",
  web_browser_js: "using the browser",
  view_image: "looking at an image",
  screen: "taking a look at the screen",
  pointer: "moving the mouse",
  keyboard: "typing",
  clipboard: "using the clipboard",
  window: "arranging windows",
  ui_invoke: "pressing a control",
  app_open: "opening an app",
  todo: "updating its plan",
  memory: "remembering something",
  create_goal: "setting a goal",
  update_goal: "updating a goal",
  show_goals: "checking goals",
  task: "delegating a subtask",
};
function friendlyAction(name, args) {
  const verb = FRIENDLY_VERBS[name];
  if (!verb) return null;
  let target = "";
  try {
    if (name === "web_search") target = args.query || "";
    else if (name === "write_file" || name === "read_file" || name === "edit_file") target = (args.path || "").split(/[\\/]/).pop();
    else if (name === "app_open") target = args.target || "";
    else if (name === "window") target = args.query || "";
    else if (args && (args.window || args.url)) target = args.window || args.url;
  } catch {}
  return target ? verb + ": " + String(target).slice(0, 40) : verb;
}

function argsSummary(name, args) {
  try {
    if (name === "shell") return String(args.command || "");
    if (name === "write_file" || name === "edit_file") return String(args.path || "");
    if (name === "python") return String(args.code || "").split("\n")[0];
    if (args && Object.keys(args).length) return JSON.stringify(args);
  } catch {}
  return "";
}

function makeToolCard(name, args, running) {
  const card = el("div", "tool-card");
  const head = el("div", "tool-head");
  head.appendChild(el("div", "tool-glyph", glyph(name)));
  const label = isAssistant() ? friendlyAction(name, args) : null;
  head.appendChild(el("span", "tool-name", label || name));
  const sum = el("span", "tool-args");
  if (label) {
    sum.textContent = "";
    card.title = name + " " + argsSummary(name, args);
  } else {
    sum.textContent = argsSummary(name, args);
    sum.title = sum.textContent;
  }
  head.appendChild(sum);
  const status = el("span", running ? "chip chip-run" : "chip chip-ok", running ? "" : "ok");
  if (running) { const sp = el("span", "spin"); status.prepend(sp); status.textContent = ""; status.appendChild(sp); status.appendChild(document.createTextNode("run")); }
  head.appendChild(status);
  card.appendChild(head);
  const body = el("div", "tool-body");
  card.appendChild(body);
  head.addEventListener("click", () => card.classList.toggle("open"));
  card._status = status;
  card._fillResult = (res) => {
    if (res.ok) { status.className = "chip chip-ok"; status.textContent = "ok"; }
    else if (res.error && /denied|BLOCK|BACKGROUND/i.test(res.error)) { status.className = "chip chip-blk"; status.textContent = "blocked"; }
    else { status.className = "chip chip-err"; status.textContent = "error"; }
    const secA = el("div", "tool-section");
    secA.appendChild(el("div", "tool-label", "arguments"));
    const preA = el("pre", "tool-pre");
    preA.textContent = JSON.stringify(args || {}, null, 2);
    secA.appendChild(preA);
    body.appendChild(secA);
    const text = res.ok ? res.output : res.error || res.output || "";
    if (text) {
      const secO = el("div", "tool-section");
      secO.appendChild(el("div", "tool-label", res.ok ? "output" : "error"));
      const shownFull = text.length <= 6000;
      const preO = el("pre", "tool-pre");
      preO.textContent = shownFull ? text : text.slice(0, 6000) + "\nâ€¦ [" + (text.length - 6000) + " more chars]";
      if (!shownFull) {
        preO.addEventListener("dblclick", () => { preO.textContent = text; });
        preO.title = "double-click to expand";
      }
      secO.appendChild(preO);
      body.appendChild(secO);
    }
    if (res.images && res.images.length) {
      const secI = el("div", "tool-section");
      secI.appendChild(el("div", "tool-label", "images"));
      const grid = el("div", "tool-imgs");
      for (const p of res.images) {
        const img = document.createElement("img");
        img.src = fileUrl(p);
        img.loading = "lazy";
        img.addEventListener("click", () => window.open(img.src, "_blank"));
        grid.appendChild(img);
      }
      secI.appendChild(grid);
      body.appendChild(secI);
    }
    if (!res.ok && (res.error || res.output)) {
      // v0/Lovable parity: wire the error surface straight to a repair action
      const foot = el("div", "tool-fix");
      const fix = el("button", "st-btn", "fix this");
      fix.title = "Ask the agent to fix this error";
      fix.addEventListener("click", () => fixThisError(name, args, res.error || res.output || ""));
      foot.appendChild(fix);
      body.appendChild(foot);
    }
  };
  return card;
}

function setChipBlocked(card, why) {
  if (!card || !card._status) return;
  card._status.className = "chip chip-blk";
  card._status.textContent = "blocked";
}

/* Interactive clarifying-question card (ask_user tool): option buttons plus a
   free-text answer; resolved via POST /api/ask like an approval. */
function makeAskCard(evt) {
  const box = el("div", "approval ask-card");
  const head = el("div", "appr-head");
  head.innerHTML = '<svg class="appr-icon" viewBox="0 0 24 24"><path d="M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.2 1 2V18a1 1 0 0 0 1 1h4a1 1 0 0 0 1-1v-1.3c0-.8.4-1.5 1-2A7 7 0 0 0 12 2zM9.5 21a1 1 0 0 0 1 1h3a1 1 0 0 0 0-2h-3a1 1 0 0 0-1 1z"/></svg>';
  head.appendChild(el("span", "appr-title", "Question"));
  const ttl = el("span", "appr-ttl", Math.round(evt.ttl / 60) + " min");
  head.appendChild(ttl);
  box.appendChild(head);
  box.appendChild(el("div", "ask-q", evt.q || ""));
  const actions = el("div", "appr-actions ask-actions");
  const resolveLocal = (answer, timeout) => {
    actions.remove();
    ttl.remove();
    const done = el("div", "appr-resolved");
    if (timeout) done.append("No answer \u2014 proceeding on its own");
    else done.append("Answered: ", el("b", "", String(answer).slice(0, 80)));
    box.appendChild(done);
    state.asks.delete(evt.id);
  };
  state.asks.set(evt.id, resolveLocal);
  const decide = (answer) => {
    resolveLocal(answer, false);
    api("/api/ask", { method: "POST", body: JSON.stringify({ id: evt.id, answer }) }).catch(() => {});
  };
  for (const opt of evt.options || []) {
    const b = el("button", "appr-btn allow ask-opt", opt);
    b.addEventListener("click", () => decide(opt));
    actions.appendChild(b);
  }
  const free = el("input", "ask-free");
  free.type = "text";
  free.placeholder = "or type your own answer\u2026";
  free.spellcheck = false;
  free.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const v = free.value.trim();
      if (v) decide(v);
    }
    e.stopPropagation();
  });
  actions.appendChild(free);
  box.appendChild(actions);
  const bar = el("div", "ttlbar");
  box.appendChild(bar);
  requestAnimationFrame(() => {
    bar.style.transform = "scaleX(0)";
    bar.style.transition = "transform " + evt.ttl + "s linear";
  });
  return box;
}

function makeApproval(evt) {
  const box = el("div", "approval");
  const head = el("div", "appr-head");
  head.innerHTML = '<svg class="appr-icon" viewBox="0 0 24 24"><path d="M12 2 1 21h22zM11 10h2v5h-2zm0 7h2v2h-2z"/></svg>';
  head.appendChild(el("span", "appr-title", evt.kind === "file" ? "File change approval" : "Command approval"));
  const ttl = el("span", "appr-ttl", Math.round(evt.ttl / 60) + " min");
  head.appendChild(ttl);
  box.appendChild(head);
  if (evt.command) box.appendChild(el("pre", "appr-cmd", evt.command));
  if (evt.detail && !evt.diff) box.appendChild(el("pre", "appr-cmd", evt.detail));
  if (evt.diff) {
    const dw = el("div");
    dw.style.margin = "9px 13px 0";
    dw.appendChild(renderDiff(evt.diff));
    box.appendChild(dw);
  }
  const actions = el("div", "appr-actions");
  const noteInput = el("input", "ask-free appr-note-input");
  noteInput.type = "text";
  noteInput.placeholder = "optional note \u2014 tell the agent what to do instead (sent with Deny)";
  noteInput.spellcheck = false;
  noteInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); decide("deny"); } e.stopPropagation(); });
  const mkBtn = (cls, label, decision) => {
    const b = el("button", "appr-btn " + cls, label);
    b.addEventListener("click", () => decide(decision));
    return b;
  };
  const bAllow = mkBtn("allow", "Allow", "allow");
  const bAlways = mkBtn("always", evt.kind === "file" ? "Always this file" : "Always this command", "always");
  const bDeny = mkBtn("deny", "Deny", "deny");
  const bNote = el("button", "appr-btn appr-note-btn", "+ note");
  bNote.title = "Attach a note to your denial so the agent knows what to do instead";
  bNote.addEventListener("click", () => {
    noteInput.classList.toggle("hidden");
    if (!noteInput.classList.contains("hidden")) noteInput.focus();
  });
  actions.append(bAllow, bAlways, bDeny, bNote);
  const keys = el("span", "appr-key");
  keys.append("kbd ", mkK("Y"), " ", mkK("A"), " ", mkK("N"));
  actions.appendChild(keys);
  box.appendChild(actions);
  box.appendChild(noteInput);
  noteInput.classList.add("hidden");
  const bar = el("div", "ttlbar");
  box.appendChild(bar);

  function mkK(t) { return el("kbd", "", t); }
  async function decide(decision) {
    try {
      const r = await api("/api/approve", { method: "POST", body: JSON.stringify({ id: evt.id, decision, note: noteInput.value.trim() }) });
      if (!r.ok) toast("Approval already resolved", "info");
    } catch (e) { toast(e.message, "err"); }
  }
  function resolveLocal(allowed, timeout) {
    actions.remove();
    bar.remove();
    ttl.remove();
    const done = el("div", "appr-resolved");
    done.append(allowed ? "Allowed" : timeout ? "Timed out - denied" : "Denied");
    const ic = el("span", allowed ? "chip chip-ok" : "chip chip-blk", allowed ? "approved" : "denied");
    done.appendChild(ic);
    box.appendChild(done);
    state.approvals.delete(evt.id);
  }
  state.approvals.set(evt.id, resolveLocal);
  requestAnimationFrame(() => {
    bar.style.transform = "scaleX(0)";
    bar.style.transition = "transform " + evt.ttl + "s linear";
  });
  return box;
}

function msgActions(getText, extra) {
  const bar = el("div", "msg-actions");
  const mk = (title, svg, fn) => {
    const b = el("button", "msg-act");
    b.title = title;
    b.innerHTML = svg;
    b.addEventListener("click", (e) => { e.stopPropagation(); fn(b); });
    return b;
  };
  bar.appendChild(
    mk("Copy", '<svg viewBox="0 0 24 24"><path d="M16 1H4a2 2 0 0 0-2 2v14h2V3h12zm3 4H8a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2zm0 16H8V7h11z"/></svg>', (b) => {
      navigator.clipboard.writeText(getText()).then(() => { b.classList.add("done"); setTimeout(() => b.classList.remove("done"), 900); });
    })
  );
  if (extra) bar.appendChild(extra);
  return bar;
}

const IC_PENCIL = '<svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.8 9.94l-3.75-3.75zM20.7 7a1 1 0 0 0 0-1.4l-2.3-2.3a1 1 0 0 0-1.4 0l-1.8 1.8 3.7 3.75z"/></svg>';
const IC_RETRY = '<svg viewBox="0 0 24 24"><path d="M17.65 6.35A8 8 0 1 0 19.73 14h-2.08a6 6 0 1 1-1.4-6.24L13 11h7V4z"/></svg>';
const IC_SEND = '<svg viewBox="0 0 24 24"><path d="M3 20.5v-17l18 8.5zM5 17.2L16.9 12 5 6.8z"/></svg>';
const IC_FORK = '<svg viewBox="0 0 24 24"><path d="M6 3a3 3 0 0 1 1 5.83V10a2 2 0 0 0 2 2h6a4 4 0 0 0 4-4V8.83A3 3 0 1 1 17 3a3 3 0 0 1 2 2.83V8a6 6 0 0 1-2 4.47V15a3 3 0 1 1-2 0v-1h-6v3a3 3 0 1 1-2 0V8.83A3 3 0 0 1 6 3zm0 2a1 1 0 1 0 0 2 1 1 0 0 0 0-2zm12 0a1 1 0 1 0 0 2 1 1 0 0 0 0-2zM7 17a1 1 0 1 0 0 2 1 1 0 0 0 0-2zm10 0a1 1 0 1 0 0 2 1 1 0 0 0 0-2z" fill-rule="evenodd"/></svg>';

/* Branch-based history surgery (Goose/OpenHands parity): /api/branch keeps the
   first `keep` raw messages (tool roles included) of this session in a NEW
   session; the original is untouched. msgIdx comes from the server's item
   hydration (msg_idx on user items), so the count is exact. */
let _userIdxCache = null;
async function userMsgIdxs() {
  if (_userIdxCache && _userIdxCache.sid === state.sid) return _userIdxCache.idxs;
  const data = await api("/api/session/" + encodeURIComponent(state.sid || ""));
  const idxs = (data.items || []).filter((it) => it.kind === "user").map((it) => it.msg_idx | 0);
  _userIdxCache = { sid: state.sid, idxs };
  return idxs;
}
function invalidateUserIdxs() { _userIdxCache = null; }

async function branchAt(keep, resendText) {
  if (state.busy) { toast("Wait for the current run to finish", "info"); return false; }
  if (!state.sid) { toast("No session yet", "info"); return false; }
  let body = { session_id: state.sid };
  if (keep != null) body.keep = keep;
  let r;
  try { r = await api("/api/branch", { method: "POST", body: JSON.stringify(body) }); }
  catch (e) { toast(e.message, "err"); return false; }
  invalidateUserIdxs();
  const input = $("#input");
  const carry = resendText != null ? resendText : input.value.trim();
  input.value = "";
  autoGrow(input);
  updateSendEnabled();
  await loadSessions();
  await openSession(r.session_id);
  if (carry) {
    input.value = carry;
    autoGrow(input);
    updateSendEnabled();
    if (resendText != null) send();
  }
  return true;
}

async function editResendAt(wrap, text) {
  const idxs = await userMsgIdxs();
  const umi = Array.prototype.indexOf.call(thread.querySelectorAll(".msg-user"), wrap);
  const msgIdx = idxs[umi];
  if (msgIdx == null) { toast("Could not locate that message", "err"); return; }
  await branchAt(msgIdx, text); // keep everything BEFORE this message, resend it
}

function addUserMsg(text, images, thumbs) {
  hideEmptyState();
  const wrap = el("div", "msg-user");
  wrap.dataset.umi = String(thread.querySelectorAll(".msg-user").length);
  const bub = el("div", "user-bubble");
  bub.textContent = text;
  bub.title = new Date().toLocaleTimeString();
  wrap.appendChild(bub);
  if (thumbs && thumbs.length) {
    const tw = el("div", "user-thumbs");
    for (const u of thumbs) {
      const im = document.createElement("img");
      im.src = u;
      tw.appendChild(im);
    }
    wrap.appendChild(tw);
  } else if (images > 0) {
    wrap.appendChild(el("div", "user-nimg", "+" + images + " image" + (images > 1 ? "s" : "")));
  }
  const actions = msgActions(() => text);
  const editBtn = el("button", "msg-act");
  editBtn.title = "Edit (loads into the composer)";
  editBtn.innerHTML = IC_PENCIL;
  editBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const input = $("#input");
    input.value = text;
    input.focus();
    autoGrow(input);
    updateSendEnabled();
  });
  actions.appendChild(editBtn);
  const resendBtn = el("button", "msg-act");
  resendBtn.title = "Edit & resend (branches here, replaces this exchange)";
  resendBtn.innerHTML = IC_SEND;
  resendBtn.addEventListener("click", async (e) => { e.stopPropagation(); await editResendAt(wrap, text); });
  actions.appendChild(resendBtn);
  const forkBtn = el("button", "msg-act");
  forkBtn.title = "Branch from here (keeps this message)";
  forkBtn.innerHTML = IC_FORK;
  forkBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    const idxs = await userMsgIdxs();
    const umi = Array.prototype.indexOf.call(thread.querySelectorAll(".msg-user"), wrap);
    if (idxs[umi] == null) { toast("Could not locate that message", "err"); return; }
    await branchAt(idxs[umi] + 1, null);
  });
  actions.appendChild(forkBtn);
  wrap.appendChild(actions);
  const turn = newTurn();
  turn.appendChild(wrap);
  scrollDown(true);
}

function newTurn() {
  const t = el("div", "turn");
  thread.appendChild(t);
  return t;
}

function makeAssistantShell(turn) {
  const row = el("div", "msg-assistant");
  const inner = el("div", "assistant-row");
  const av = el("div", "avatar");
  av.innerHTML = '<svg viewBox="0 0 32 32"><path d="M22.3 5.75 5.09 5.75 17.59 21.75 9.7 21.75 9.7 26.25 26.81 26.25 14.31 10.25 22.3 10.25Z"/></svg>';
  inner.appendChild(av);
  const body = el("div", "assistant-body");
  inner.appendChild(body);
  row.appendChild(inner);
  turn.appendChild(row);
  return body;
}

function makeThink(body) {
  const det = el("details", "think");
  const sum = el("summary");
  const chev = el("svg", "chev");
  chev.setAttribute("viewBox", "0 0 24 24");
  chev.innerHTML = '<path d="M9 6l6 6-6 6z"/>';
  sum.appendChild(chev);
  const glyph = el("svg", "think-glyph");
  glyph.setAttribute("viewBox", "0 0 24 24");
  glyph.innerHTML = '<path d="M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.2 1 2V18a1 1 0 0 0 1 1h4a1 1 0 0 0 1-1v-1.3c0-.8.4-1.5 1-2A7 7 0 0 0 12 2zM9.5 21a1 1 0 0 0 1 1h3a1 1 0 0 0 0-2h-3a1 1 0 0 0-1 1z"/>';
  sum.appendChild(glyph);
  sum.appendChild(el("span", "think-label", "Thinking"));
  det.appendChild(sum);
  const bodyDiv = el("div", "think-body");
  det.appendChild(bodyDiv);
  body.appendChild(det);
  det.open = true;
  return { det, bodyDiv, label: sum.querySelector(".think-label") };
}

function makeNotice(text) {
  const n = el("div", "notice");
  const parsed = parseCmdList(text);
  if (parsed) {
    n.classList.add("cmdlist");
    // structured command grid: command cell never wraps, description wraps
    // inside its own column (a pre-wrap ASCII table turns to rubble in a
    // narrow chat pane)
    for (const [name, desc] of parsed.cmds) {
      const row = el("div", "cmd-row");
      row.appendChild(el("code", "cmd-name", name));
      row.appendChild(el("span", "cmd-desc", desc));
      n.appendChild(row);
    }
    if (parsed.tips) n.appendChild(el("div", "cmd-tips", parsed.tips));
  } else {
    n.textContent = text;
  }
  return n;
}

// /help (and friends) arrive as 'commands:\n  /cmd — description' lines; parse
// them into [command, description] pairs. Returns null when the notice isn't
// that shape, so ordinary notices keep plain pre-wrap rendering.
function parseCmdList(text) {
  const lines = String(text || "").split("\n");
  if (!/^commands:\s*$/.test(lines[0] || "")) return null;
  const cmds = [];
  let tips = "";
  for (const line of lines.slice(1)) {
    if (!line.trim()) continue;
    const m = line.trim().match(/^(\/\S+(?: [<[][^>\]]+[>\]])?|exit \/ quit)\s+—\s+(.*)$/);
    if (!m) { tips = (tips ? tips + " " : "") + line.trim(); continue; }
    cmds.push([m[1], m[2]]);
  }
  if (!cmds.length) return null;
  return { cmds, tips };
}
function makeSysline(text, cls) {
  const s = el("div", "sysline" + (cls ? " " + cls : ""));
  s.textContent = text;
  return s;
}
function makeStats(steps, tokens, stop, cost, costTotal) {
  const s = el("div", "turn-stats");
  if (isAssistant()) {
    // assistant: outcome line only - steps/tokens are plumbing
    if (stop && stop !== "done" && stop !== "slash") {
      const b = el("span", "stop-badge", stop);
      s.appendChild(b);
      s.appendChild(document.createTextNode("  "));
    } else {
      s.textContent = "\u2713 done";
    }
  } else {
    const bits = [steps + " step" + (steps === 1 ? "" : "s"), tokens + " tokens"];
    if (costTotal != null) bits.push("~$" + costTotal);
    else if (cost != null) bits.push("~$" + cost);
    if (stop && stop !== "done") {
      const b = el("span", "stop-badge", stop);
      s.append(bits.join(" \u00b7 ") + " \u00b7 ");
      s.appendChild(b);
    } else {
      s.textContent = "\u2713 " + bits.join(" \u00b7 ");
    }
  }
  if (state.lastUser && !state.busy) {
    const retry = el("button", "retry-link");
    retry.title = "Retry: fork without this exchange and resend there";
    retry.innerHTML = IC_RETRY;
    retry.addEventListener("click", async () => {
      if (state.busy) return;
      await branchAt(null, state.lastUser);
    });
    s.appendChild(document.createTextNode("  "));
    s.appendChild(retry);
  }
  // per-turn rating (Lovable parity): a local reward signal recorded for the
  // training-data flywheel — feeds nothing but your own disk
  const fb = el("span", "fb-row");
  for (const [val, sym, title] of [["up", "\u{1F44D}", "Helpful — record a positive reward"], ["down", "\u{1F44E}", "Not helpful — record a negative reward"]]) {
    const b = el("button", "fb-btn", sym);
    b.title = title;
    b.addEventListener("click", async () => {
      if (b.classList.contains("done")) return;
      try {
        await api("/api/feedback", { method: "POST", body: JSON.stringify({ sid: state.sid || "", turn: state.turnSeq, value: val, model: ((state.info || {}).provider || "") + "/" + ((state.info || {}).model || "") }) });
        fb.querySelectorAll(".fb-btn").forEach((x) => x.classList.add("dim"));
        b.classList.add("done");
        toast("Feedback saved locally", "ok");
      } catch (e) { toast(e.message, "err"); }
    });
    fb.appendChild(b);
  }
  s.appendChild(fb);
  state.turnSeq++;
  return s;
}

function toast(msg, kind) {
  const box = $("#toasts");
  const t = el("div", "toast " + (kind || "info"), msg);
  box.appendChild(t);
  setTimeout(() => t.remove(), 4200);
}

/* Approval attention (Cline/Devin parity): pulse the window title while the
   tab/app is hidden and an approval is waiting on you. */
let flashTimer = null;
function baseTitle() {
  const t = $("#sessionTitle") ? $("#sessionTitle").textContent : "";
  return t && t !== "New chat" ? t + " - Saturday" : "Saturday";
}
function notifyAttention(on) {
  if (on && !flashTimer) {
    let f = false;
    flashTimer = setInterval(() => {
      document.title = (f = !f) ? "\u26a0 approval needed \u2014 Saturday" : baseTitle();
    }, 900);
  } else if (!on && flashTimer) {
    clearInterval(flashTimer);
    flashTimer = null;
    document.title = baseTitle();
  }
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !state.approvals.size) notifyAttention(false);
});

/* ------------------------------------------------------------ live turn flow */

let live = null;

function liveBegin() {
  const turn = newTurn();
  const body = makeAssistantShell(turn);
  live = { turn, body, text: "", reason: "", think: null, content: null, cards: new Map(), renderQueued: false, ended: false };
  return live;
}

function liveEnsureContent() {
  if (!live.content) {
    if (live.think && live.think.det.open) live.think.det.open = false;
    if (live.think) live.think.label.textContent = "Thought process";
    live.content = el("div", "md streaming-caret");
    const ref = live.body.querySelector(".tool-card, .approval, .notice, .sysline");
    live.body.insertBefore(live.content, ref);
  }
  return live.content;
}

let lastMdRender = 0;
function liveRenderNow(force) {
  if (!live || !live.content) return;
  const now = performance.now();
  if (!force && now - lastMdRender < 60) return;
  lastMdRender = now;
  live.content.innerHTML = renderMd(live.text);
}

function liveEnd(stats) {
  if (!live || live.ended) return;
  live.ended = true;
  if (stats.final) live.text = stats.final;
  liveEnsureContent();
  live.content.innerHTML = renderMd(live.text);
  live.content.classList.remove("streaming-caret");
  if (live.reason) live.think.label.textContent = "Thought process";
  if (stats.stop_reason !== "slash") live.turn.appendChild(makeStats(stats.steps, stats.tokens, stats.stop_reason, stats.cost, stats.cost_total));
  const finalText = stats.final || live.text || "";
  if (finalText) live.body.appendChild(msgActions(() => finalText));
  live = null;
  stage.running = 0;
  state.stepNow = 0;
  stageMeta();
  scrollDown();
  loadSessions();
}

async function handleEvent(e) {
  switch (e.t) {
    case "hello": {
      adoptSession(e.sid, e.project);
      break;
    }
    case "user": addUserMsg(e.text, e.images, pendingThumbs); pendingThumbs = null; if (!isAssistant()) homeRunInit(); break;
    case "reason": {
      if (!live) liveBegin();
      if (!live.think) live.think = makeThink(live.body);
      live.reason += e.s;
      live.think.bodyDiv.textContent = live.reason;
      live.think.bodyDiv.scrollTop = live.think.bodyDiv.scrollHeight;
      scrollDown();
      break;
    }
    case "delta": {
      if (!live) liveBegin();
      liveEnsureContent();
      live.text += e.s;
      liveRenderNow(false);
      break;
    }
    case "step": {
      state.stepNow = e.n;
      if (stage.run && !stage.run.endedAt) { stage.run.step = e.n + 1; homeRunUpdate(); }
      stageMeta();
      break;
    }
    case "ctx": {
      state.ctxPrompt = e.prompt;
      const m = $("#tokMeter");
      if (m) {
        m.textContent = "\u2306 " + fmtK(e.prompt) + " / " + fmtK(e.compact);
        m.classList.toggle("ctx-hot", e.prompt > e.compact);
        m.title =
          "context this step ~" + e.prompt.toLocaleString() + " tokens" +
          (e.prompt > e.compact ? "\ncompaction will trigger on the next step" : "") +
          "\nclick for the full breakdown";
      }
      break;
    }
    case "tool_start": {
      if (!live) liveBegin();
      const c = makeToolCard(e.name, e.args, true);
      live.cards.set(e.card, c);
      live.body.appendChild(c);
      c.querySelector(".tool-head").addEventListener("click", () => stageFocus(e.card));
      stageEntry(e.card, e.name, e.args, true);
      mgAttend((e.args || {}).path || (e.args || {}).file_path);
      if (!isAssistant()) homeRowAdd(e.card, e.name, e.args);
      scrollDown();
      break;
    }
    case "tool_result": {
      if (!live) liveBegin();
      let card = live.cards.get(e.card);
      if (!card) {
        card = makeToolCard(e.name, e.args || {}, false);
        live.cards.set(e.card, card);
        live.body.appendChild(card);
        card.querySelector(".tool-head").addEventListener("click", () => stageFocus(e.card));
      }
      card.classList.add("open");
      card._fillResult({ ok: e.ok, output: e.output, error: e.error, images: e.images });
      let se = stage.entries.get(e.card);
      if (!se) se = stageEntry(e.card, e.name, e.args || {}, false, true);
      se._fill({ ok: e.ok, output: e.output, error: e.error, images: e.images });
      if (stage.run && !stage.run.endedAt) { stage.run.tools++; homeRowDone(e.card, { ok: e.ok, error: e.error }); homeRunUpdate(); }
      if (e.images && e.images.length) {
        for (const p of e.images) stageAddImage(fileUrl(p));
        stageShow("preview", true);
      } else if ((e.name === "write_file" || e.name === "edit_file") && e.ok) {
        stageAddChange({ path: (e.args && e.args.path) || e.name, content: (e.args && (e.args.content || e.args.new_string)) || "" });
      } else if (e.name === "todo" && e.ok && e.output) {
        stageSetPlan(e.output);
        stageShow("plan", true);
      }
      scrollDown();
      break;
    }
    case "approval": {
      if (!live) liveBegin();
      live.turn.appendChild(makeApproval(e));
      if (e.diff) {
        const path = (e.title.split("->")[1] || "").trim() || "file";
        stageAddChange({ path, diff: e.diff });
        stageShow("changes", true);
      }
      if (document.hidden) {
        notifyAttention(true);
        if (localStorage.getItem("df_sound") === "1") playPing();
      }
      scrollDown(true);
      updateComposerHint();
      break;
    }
    case "approval_done": {
      const fn = state.approvals.get(e.id);
      if (fn) fn(e.allowed, e.timeout);
      if (!state.approvals.size) notifyAttention(false);
      updateComposerHint();
      break;
    }
    case "ask": {
      // Lovable/Windsurf parity: interactive clarifying-question card
      if (!live) liveBegin();
      live.turn.appendChild(makeAskCard(e));
      updateComposerHint();
      if (document.hidden) notifyAttention(true);
      scrollDown(true);
      break;
    }
    case "ask_done": {
      const fn = state.asks.get(e.id);
      if (fn) fn(e.answer || "", !!e.timeout);
      updateComposerHint();
      break;
    }
    case "title": {
      // Zed/OpenHands parity: AI-generated session title (background call)
      const s = state.sessions.find((x) => x.id === e.sid);
      if (s) s.task = e.title;
      if (e.sid === state.sid) {
        const t = e.title || "New chat";
        $("#sessionTitle").textContent = t;
        document.title = t + " - Saturday";
      }
      renderSessions();
      break;
    }
    case "subagent": {
      // Claude Code/Warp parity: live subagent progress nested under the task card
      if (!live) break;
      const parent = live.cards.get(e.parent);
      if (!parent) break;
      if (!parent._subBox) {
        parent._subBox = el("div", "sub-box");
        parent.appendChild(parent._subBox);
      }
      let row = parent._subRows && parent._subRows[e.child];
      if (!row) {
        row = el("div", "sub-row");
        row.appendChild(el("span", "sub-id mono", e.child));
        const txt = el("span", "sub-txt", "starting\u2026");
        row.appendChild(txt);
        row.appendChild(el("span", "chip chip-run", ""));
        parent._subRows = parent._subRows || {};
        parent._subRows[e.child] = row;
        parent._subBox.appendChild(row);
      }
      const txt = row.querySelector(".sub-txt");
      const chip = row.querySelector(".chip");
      if (e.kind === "tool" && e.name) {
        txt.textContent = (e.ok ? "ran " : "failed ") + e.name;
        row.title = (e.ok ? e.output : e.error) || "";
      } else if (e.kind === "start" && e.description) {
        txt.textContent = e.description;
      } else if (e.kind === "step" && typeof e.n === "number") {
        txt.textContent = "step " + (e.n + 1);
      } else if (e.kind === "done") {
        txt.textContent = e.summary || "finished";
        if (chip) { chip.className = "chip chip-ok"; chip.textContent = "ok"; }
        if (parent._status) { parent._status.className = "chip chip-ok"; parent._status.textContent = "ok"; }
      }
      break;
    }
    case "notice": {
      if (!live) liveBegin();
      live.turn.appendChild(makeNotice(e.s));
      scrollDown();
      break;
    }
    case "warning": {
      if (!live) liveBegin();
      live.turn.appendChild(makeSysline(e.s, "warn"));
      scrollDown();
      break;
    }
    case "config": {
      if (state.info) {
        state.info.provider = e.provider;
        state.info.model = e.model;
        if (e.safety_mode) state.info.safety_mode = e.safety_mode;
      }
      renderHeaderPills();
      stageHomeFill();
      break;
    }
    case "ping": break;
    case "done": {
      liveEnd(e);
      setBusy(false);
      if (stage.run) {
        stage.run.tokens += e.tokens || 0;
        stage.run.endedAt = Date.now();
        homeRunUpdate();
      }
      if (state.sid && e.tokens) {
        state.tokBySid[state.sid] = (state.tokBySid[state.sid] || 0) + (e.tokens || 0);
        updateTokMeter();
      }
      if (state.sid && e.cost_total != null) state.costBySid[state.sid] = e.cost_total;
      invalidateUserIdxs();
      loadCtx();
      notifyDone(e.final || "", e.tokens);
      if ((e.stop_reason || "done") === "done") fetchFollowups();
      if (state.queue.length) {
        const next = state.queue.shift();
        renderQueue();
        const input = $("#input");
        input.value = next.text;
        state.images = next.images;
        renderAttachRow();
        send();
      }
      break;
    }
    case "error": {
      if (!live) liveBegin();
      live.turn.appendChild(makeSysline("[error] " + e.message, "error"));
      liveEnd({ final: "", stop_reason: "error", steps: 0, tokens: 0 });
      setBusy(false);
      toast(e.message, "err");
      break;
    }
  }
}

/* ------------------------------------------------------------------- sending */

let aborter = null;
let pendingThumbs = null;

/* ---------------------------------------------------- session stream switching
   The UI may watch any session while others keep running: switching away
   detaches the reader (the server run continues), and opening a busy session
   re-attaches to its live event tail with the in-flight turn replayed. */

let tailAbort = null;

function detachTail() {
  if (tailAbort) {
    const t = tailAbort;
    tailAbort = null;
    try { t.abort(); } catch {}
  }
}

function detachAllStreams() {
  if (aborter) {
    const a = aborter;
    aborter = null;
    try { a.abort(); } catch {}
  }
  detachTail();
}

async function attachTail(sid) {
  detachTail();
  if (!sid) return;
  tailAbort = new AbortController();
  const mine = tailAbort;
  try {
    const r = await fetch("/api/stream/" + encodeURIComponent(sid) + "?from=run", {
      headers: { "X-Saturday-Token": TOKEN },
      signal: mine.signal,
    });
    if (!r.ok || !r.body) {
      if (tailAbort === mine) tailAbort = null;
      return;
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const lineTxt = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!lineTxt) continue;
        try { await handleEvent(JSON.parse(lineTxt)); } catch (err) { console.warn("bad tail event", lineTxt, err); }
      }
    }
  } catch (e) {
    if (e && e.name !== "AbortError") connOff();
  } finally {
    if (tailAbort === mine) tailAbort = null;
    recoverAfterStreamEnd(sid);
    loadSessions();
  }
}

// A stream that ends without a terminal event (server killed, keep-alive
// abort) must not leave the composer stuck in "working" forever: re-check the
// server's busy flag once, re-attach if the run is genuinely still going, and
// otherwise release the UI. Runs after BOTH send() and attachTail teardown.
function recoverAfterStreamEnd(sid) {
  if (!state.busy || state.sid !== sid) return;
  setTimeout(async () => {
    await loadSessions();
    if (state.sid !== sid) return;
    const still = (state.sessions.find((s) => s.id === sid) || {}).busy;
    if (still) { attachTail(sid); return; } // run continues server-side
    setBusy(false);
  }, 1500);
}

async function send() {
  const input = $("#input");
  const first = input.value.trim();
  const info = state.info || {};
  if (first && !first.startsWith("/") && !info.has_key && !isLocalProvider(info.provider)) {
    onboardOpen();
    const w = $("#obWarn");
    w.textContent = "Connect a provider first \u2014 paste your API key here";
    w.classList.remove("hidden");
    return;
  }
  let text = input.value.trim();
  const imgs = state.images.slice();
  if (!text && imgs.length) text = "(image)";
  if (!text) return;
  if (state.busy) {
    state.queue.push({ text, images: imgs });
    renderQueue();
    input.value = "";
    autoGrow(input);
    saveDraft();
    return;
  }
  input.value = "";
  autoGrow(input);
  saveDraft();
  clearFollowups();
  closeSlash();
  closeAt();
  closeFind();
  invalidateUserIdxs();
  detachTail(); // any live tail on this session is replaced by the chat stream
  state.images = [];
  renderAttachRow();
  setBusy(true);
  hideEmptyState();
  stickBottom = true;
  state.lastUser = text;
  pendingThumbs = imgs.map((i) => i.dataUrl);
  aborter = new AbortController();
  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      signal: aborter.signal,
      headers: { "Content-Type": "application/json", "X-Saturday-Token": TOKEN },
      body: JSON.stringify({ session_id: state.sid, text, images: imgs.map((i) => i.dataUrl), project_id: state.proj || "" }),
    });
    if (r.status === 409) { toast("That session is still running", "err"); setBusy(false); return; }
    if (!r.ok || !r.body) {
      let msg = r.status + " " + r.statusText;
      try { msg = (await r.json()).error || msg; } catch {}
      throw new Error(msg);
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const lineTxt = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!lineTxt) continue;
        try { await handleEvent(JSON.parse(lineTxt)); } catch (err) { console.warn("bad event", lineTxt, err); }
      }
    }
  } catch (err) {
    if (err.name === "AbortError") {
      if (live) liveEnd({ final: "", stop_reason: "stopped", steps: 0, tokens: 0 });
      setBusy(false);
    } else {
      connOff();
      if (live) live.turn.appendChild(makeSysline("[connection lost] " + err.message, "error"));
      if (live) liveEnd({ final: "", stop_reason: "error", steps: 0, tokens: 0 });
      setBusy(false);
      toast(err.message, "err");
    }
  } finally {
    aborter = null;
    recoverAfterStreamEnd(state.sid);
    loadSessions();
  }
}

function renderQueue() {
  const row = $("#queueRow");
  row.replaceChildren();
  state.queue.forEach((q, i) => {
    const chip = el("div", "att-chip q-chip");
    chip.draggable = true;
    chip.dataset.qi = String(i);
    chip.appendChild(el("span", "q-num", String(i + 1)));
    const txt = el("span", "q-text", q.text.length > 40 ? q.text.slice(0, 38) + "\u2026" : q.text);
    txt.title = "Click to edit \u00b7 drag to reorder";
    txt.addEventListener("click", () => {
      state.queue.splice(i, 1);
      renderQueue();
      const input = $("#input");
      input.value = q.text;
      input.focus();
      autoGrow(input);
      updateSendEnabled();
    });
    chip.appendChild(txt);
    const x = el("button", "", "\u00d7");
    x.title = "remove";
    x.addEventListener("click", () => { state.queue.splice(i, 1); renderQueue(); });
    chip.appendChild(x);
    chip.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/qidx", String(i));
      e.dataTransfer.effectAllowed = "move";
      chip.classList.add("dragging");
    });
    chip.addEventListener("dragend", () => chip.classList.remove("dragging"));
    chip.addEventListener("dragover", (e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; chip.classList.add("drop-before"); });
    chip.addEventListener("dragleave", () => chip.classList.remove("drop-before"));
    chip.addEventListener("drop", (e) => {
      e.preventDefault();
      chip.classList.remove("drop-before");
      const from = parseInt(e.dataTransfer.getData("text/qidx"), 10);
      if (isNaN(from) || from === i) return;
      const [moved] = state.queue.splice(from, 1);
      state.queue.splice(i, 0, moved);
      renderQueue();
    });
    row.appendChild(chip);
  });
  row.classList.toggle("hidden", !state.queue.length);
}

/* ---------------------------------------------------------------- find in chat */

let findState = { matches: [], cur: -1, q: "" };

function openFind() {
  $("#findBar").classList.remove("hidden");
  const inp = $("#findInput");
  inp.focus();
  inp.select();
}
function closeFind() {
  const bar = $("#findBar");
  if (!bar || bar.classList.contains("hidden")) return;
  bar.classList.add("hidden");
  clearFindMarks();
  findState = { matches: [], cur: -1, q: "" };
  $("#findCount").textContent = "";
}
function clearFindMarks() {
  document.querySelectorAll("#thread mark.find-hit").forEach((m) => {
    const parent = m.parentNode;
    if (!parent) return;
    parent.replaceChild(document.createTextNode(m.textContent), m);
    parent.normalize();
  });
}
function runFind(q) {
  clearFindMarks();
  findState = { matches: [], cur: -1, q };
  if (!q || q.length < 2) { $("#findCount").textContent = ""; return; }
  const low = q.toLowerCase();
  const walker = document.createTreeWalker(thread, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => {
      const p = n.parentElement;
      if (!p || p.closest("mark, script, style")) return NodeFilter.FILTER_REJECT;
      if (!n.nodeValue || !n.nodeValue.toLowerCase().includes(low)) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  let n;
  while ((n = walker.nextNode()) && findState.matches.length < 999) nodes.push(n);
  for (const node of nodes) {
    const text = node.nodeValue;
    const frag = document.createDocumentFragment();
    let i = 0;
    for (;;) {
      const hit = text.toLowerCase().indexOf(low, i);
      if (hit < 0) {
        frag.appendChild(document.createTextNode(text.slice(i)));
        break;
      }
      frag.appendChild(document.createTextNode(text.slice(i, hit)));
      const mark = document.createElement("mark");
      mark.className = "find-hit";
      mark.textContent = text.slice(hit, hit + q.length);
      frag.appendChild(mark);
      findState.matches.push(mark);
      i = hit + q.length;
    }
    node.parentNode.replaceChild(frag, node);
  }
  if (findState.matches.length) findGo(0);
  $("#findCount").textContent = findState.matches.length ? (findState.cur + 1) + "/" + findState.matches.length : "0";
}
function findGo(i) {
  if (!findState.matches.length) return;
  findState.cur = (i + findState.matches.length) % findState.matches.length;
  findState.matches.forEach((m, j) => m.classList.toggle("on", j === findState.cur));
  findState.matches[findState.cur].scrollIntoView({ block: "center" });
  $("#findCount").textContent = (findState.cur + 1) + "/" + findState.matches.length;
}

/* ---------------------------------------------------------------- context */

function fmtK(n) { return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n || 0); }

async function loadCtx() {
  try {
    const bd = await api("/api/context?sid=" + encodeURIComponent(state.sid || ""));
    state.ctx = bd;
    renderTokMeter(bd);
    if (!$("#ctxModal").classList.contains("hidden")) renderCtxPanel(bd);
  } catch {}
}

function renderTokMeter(bd) {
  const m = $("#tokMeter");
  if (!m || !bd) return;
  m.classList.remove("hidden");
  const prompt = typeof bd.prompt_tokens === "number" ? bd.prompt_tokens : bd.total;
  m.textContent = "\u2306 " + fmtK(prompt) + " / " + fmtK(bd.compact_above);
  const sess = state.tokBySid[state.sid] || 0;
  const cost = state.costBySid[state.sid];
  m.title =
    "context next step ~" + prompt.toLocaleString() + " tokens (" +
    (typeof bd.prompt_pct === "number" ? bd.prompt_pct : bd.usage_pct) + "% of the " + fmtK(bd.compact_above) + " compaction point)" +
    (sess ? "\ngenerated this session: " + sess.toLocaleString() : "") +
    (cost != null ? "\nsession cost ~$" + cost.toFixed(4) + " (list-price est.)" : "") +
    "\nclick for the full breakdown";
  m.classList.toggle("ctx-hot", !!bd.will_compact);
}

const CTX_SEG_CLASS = {
  system: "cs-system",
  tools: "cs-tools",
  user: "cs-user",
  assistant: "cs-assistant",
  tool: "cs-tool",
  images: "cs-images",
};

function renderCtxPanel(bd) {
  if (!bd) bd = state.ctx;
  if (!bd) return;
  $("#ctxTotal").textContent = "~" + bd.total.toLocaleString() + " tokens";
  $("#ctxBudget").textContent = bd.usage_pct + "% of " + bd.budget.toLocaleString() + " budget \u00b7 compacts at " + bd.compact_above.toLocaleString();
  $("#ctxCompactMark").textContent = fmtK(bd.compact_above);
  $("#ctxBudgetMark").textContent = fmtK(bd.budget);
  const bar = $("#ctxBar");
  bar.replaceChildren();
  const scale = bd.compact_above / bd.budget * 100;
  // reserve slice = reply headroom shown hatched to the compact threshold
  const reservePct = Math.min(100, bd.max_reply / bd.budget * 100);
  for (const s of bd.sections) {
    if (s.tokens <= 0) continue;
    const seg = el("div", "ctx-seg " + (CTX_SEG_CLASS[s.key] || ""));
    seg.style.width = Math.max(0.6, Math.min(100, s.tokens / bd.budget * 100)) + "%";
    seg.title = s.label + ": ~" + s.tokens.toLocaleString();
    bar.appendChild(seg);
  }
  const res = el("div", "ctx-seg cs-reserve");
  res.style.width = reservePct + "%";
  res.style.marginLeft = "auto";
  res.title = "reserved for the next reply (~" + bd.max_reply.toLocaleString() + ")";
  bar.appendChild(res);

  const legend = $("#ctxLegend");
  legend.replaceChildren();
  for (const s of bd.sections) {
    if (s.tokens <= 0 && !(s.detail && Object.keys(s.detail).length)) continue;
    const row = el("div", "ctx-row");
    const dot = el("span", "ctx-dot " + (CTX_SEG_CLASS[s.key] || "cs-reserve"));
    row.appendChild(dot);
    row.appendChild(el("span", "", s.label));
    row.appendChild(el("span", "ctx-t", s.tokens > 0 ? "~" + s.tokens.toLocaleString() : "\u2014"));
    row.appendChild(el("span", "ctx-pct", s.tokens > 0 ? (100 * s.tokens / bd.total).toFixed(1) + "%" : ""));
    const detailBits = Object.entries(s.detail || {}).filter(([, v]) => v).map(([k, v]) => k + ": " + v);
    if (detailBits.length) {
      const drow = el("div", "ctx-row ctx-sub");
      drow.appendChild(el("span", ""));
      drow.appendChild(el("span", "ctx-detail", detailBits.join(" \u00b7 ")));
      legend.appendChild(drow);
    }
    legend.appendChild(row);
  }
  const msgs = bd.messages || {};
  const mrow = el("div", "ctx-row ctx-total-row");
  mrow.appendChild(el("span", ""));
  mrow.appendChild(el("span", "", "messages"));
  mrow.appendChild(el("span", "ctx-detail", "user " + (msgs.user || 0) + " \u00b7 assistant " + (msgs.assistant || 0) + " \u00b7 tool " + (msgs.tool || 0) + (bd.images ? " \u00b7 images " + bd.images : "")));
  mrow.appendChild(el("span", ""));
  legend.appendChild(mrow);
}

function ctxOpen() {
  $("#ctxModal").classList.remove("hidden");
  renderCtxPanel(state.ctx);
  loadCtx();
}
function ctxClose() { $("#ctxModal").classList.add("hidden"); }

/* Goose-style one-click compaction: runs the /compact slash command (which
   never records a user message) and refreshes the breakdown. */
async function compactNow() {
  if (state.busy) { toast("Wait for the current run to finish", "info"); return; }
  if (!state.sid) { toast("Nothing to compact yet", "info"); return; }
  const btn = $("#ctxCompact");
  if (btn) { btn.disabled = true; btn.textContent = "compacting\u2026"; }
  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Saturday-Token": TOKEN },
      body: JSON.stringify({ session_id: state.sid, text: "/compact" }),
    });
    if (!r.ok) {
      let msg = r.status + " " + r.statusText;
      try { msg = (await r.json()).error || msg; } catch {}
      throw new Error(msg);
    }
    if (r.body) {
      const reader = r.body.getReader();
      for (;;) {
        const { done } = await reader.read();
        if (done) break;
      }
    }
    await loadCtx();
    toast("Context compacted", "ok");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Compact now"; }
    ctxClose();
  }
}

function updateTokMeter() {
  if (state.ctx) renderTokMeter(state.ctx);
}

function playPing() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.connect(g);
    g.connect(ctx.destination);
    o.frequency.value = 880;
    g.gain.setValueAtTime(0.06, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.35);
    o.start();
    o.stop(ctx.currentTime + 0.36);
    setTimeout(() => ctx.close(), 600);
  } catch {}
}

function notifyDone(final, tokens) {
  if (localStorage.getItem("df_sound") === "1") playPing();
  if (document.hidden && localStorage.getItem("df_notify") !== "0" && window.Notification && Notification.permission === "granted") {
    try { new Notification("Saturday finished", { body: (final || "Turn complete").slice(0, 140) }); } catch {}
  }
  if (localStorage.getItem("df_tts") === "1" && final && window.speechSynthesis) {
    try {
      speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(final.slice(0, 400));
      u.rate = parseFloat(localStorage.getItem("df_tts_rate") || "1.05");
      speechSynthesis.speak(u);
    } catch {}
  }
}

async function stopRun() {
  if (!state.sid) return;
  try { await api("/api/stop", { method: "POST", body: JSON.stringify({ session_id: state.sid }) }); } catch {}
  if (aborter) aborter.abort();
  detachTail(); // a re-attached tail ends with this stop as well
}

function setBusy(v) {
  state.busy = v;
  if (v) {
    state.stepNow = 0;
    state.busySince = Date.now();
  } else {
    state.busySince = 0;
  }
  const btn = $("#sendBtn");
  btn.disabled = v ? false : !$("#input").value.trim();
  btn.classList.toggle("stopping", v);
  btn.title = v ? "Stop" : "Send";
  $(".ic-send").classList.toggle("hidden", v);
  $(".ic-stop").classList.toggle("hidden", !v);
  stageMeta();
  refreshSidebarBusy();
  updateComposerHint();
  // common sense: when a run finishes and nothing else has your attention,
  // put the caret back in the composer
  if (!v && !document.hidden && !document.querySelector(".modal:not(.hidden)")) {
    const ae = document.activeElement;
    if (!ae || ae === document.body || ae.id === "transcript") $("#input").focus();
  }
}

/* Contextual composer hint: the most common "what can I do now?" question. */
function updateComposerHint() {
  const hint = $("#composerHint");
  if (!hint || isAssistant()) return; // assistant mode has its own flavor
  if (state.approvals.size) {
    hint.innerHTML = 'approval waiting \u2014 <kbd>Y</kbd> allow \u00b7 <kbd>A</kbd> always \u00b7 <kbd>N</kbd> deny';
  } else if (state.busy) {
    hint.textContent = "working \u2014 Enter queues a follow-up \u00b7 Esc stops";
  } else {
    hint.textContent = "Enter to send \u00b7 Shift+Enter newline";
  }
}

/* ------------------------------------------------------------------ sessions */

function sessTitleFromId(id) {
  return id;
}

async function loadSessions() {
  try {
    const data = await api("/api/sessions");
    state.sessions = data.sessions || [];
    renderSessions();
  } catch {}
}

async function loadProjects() {
  try {
    const data = await api("/api/projects");
    state.projects = data.projects || [];
    if (state.proj && !state.projects.some((p) => p.id === state.proj)) {
      state.proj = "";
      localStorage.removeItem("df_proj");
      updateProjChip();
    }
    renderSessions();
  } catch {}
}

function curProject() {
  return state.projects.find((p) => p.id === state.proj) || null;
}

function selectProject(id) {
  state.proj = id || "";
  if (state.proj) localStorage.setItem("df_proj", state.proj);
  else localStorage.removeItem("df_proj");
  renderSessions();
  updateProjChip();
  stageHomeFill();
}

function relTime(id) {
  const m = String(id).match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/);
  if (!m) return "";
  const then = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime();
  const dt = Date.now() - then;
  if (isNaN(then)) return "";
  const mins = Math.floor(dt / 60000);
  if (mins < 1) return "now";
  if (mins < 60) return mins + "m ago";
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return hrs + "h ago";
  const days = Math.floor(hrs / 24);
  if (days < 7) return days + "d ago";
  return m[2] + "/" + m[3];
}

function sessBucket(id) {
  const m = String(id).match(/^(\d{4})(\d{2})(\d{2})/);
  if (!m) return "Earlier";
  const d = new Date(+m[1], +m[2] - 1, +m[3]);
  if (isNaN(d)) return "Earlier";
  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const t = d.getTime();
  if (t >= startToday) return "Today";
  if (t >= startToday - 86400000) return "Yesterday";
  if (t >= startToday - 7 * 86400000) return "Previous 7 days";
  if (t >= startToday - 30 * 86400000) return "Previous 30 days";
  return "Older";
}

function renderSessions() {
  const list = $("#sessionList");
  const filterBox = $("#sessFilter");
  list.replaceChildren(filterBox);
  filterBox.value = state.filter;
  const q = state.filter.toLowerCase();

  /* projects block */
  const projWrap = el("div", "proj-list");
  let projs = state.projects.slice();
  projs.sort((a, b) => (projPinned(b.id) ? 1 : 0) - (projPinned(a.id) ? 1 : 0));
  if (q) projs = projs.filter((p) => p.name.toLowerCase().includes(q));
  for (const p of projs) projWrap.appendChild(projRow(p));
  if (!state.projects.length) {
    // an empty list should offer the way out of being empty, not just say so
    const cta = el("button", "proj-empty-cta");
    cta.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 5h5.6l2 2H20a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z"/></svg>';
    cta.appendChild(el("span", "", "Open a folder"));
    cta.addEventListener("click", () => folderOpen());
    projWrap.appendChild(cta);
  }
  list.appendChild(projWrap);

  /* sessions: scoped to the selected project, else only unprojected ones */
  let rows = state.sessions;
  if (state.proj) rows = rows.filter((s) => s.project === state.proj);
  else rows = rows.filter((s) => !s.project);
  if (!state.showArchived) rows = rows.filter((s) => !s.archived);
  if (q) rows = rows.filter((s) => (s.task || "").toLowerCase().includes(q) || s.id.toLowerCase().includes(q));

  if (state.proj) {
    const back = el("div", "all-chats", "\u2190 all chats");
    back.addEventListener("click", () => selectProject(""));
    back.title = "Show unprojected chats";
    back.addEventListener("dragover", (e) => { e.preventDefault(); back.classList.add("drop"); });
    back.addEventListener("dragleave", () => back.classList.remove("drop"));
    back.addEventListener("drop", (e) => {
      e.preventDefault();
      back.classList.remove("drop");
      const sid = e.dataTransfer.getData("text/plain");
      if (sid) assignSession(sid, "");
    });
    list.appendChild(back);
    const p = curProject();
    if (p) {
      const head = el("div", "proj-open-head");
      const nm = el("div", "proj-open-name" + (p.color ? " pc-" + p.color : ""), p.name);
      nm.title = (p.workspace || p.name) + ((p.files || []).length ? "\n" + p.files.length + " knowledge file(s)" : "");
      head.appendChild(nm);
      const nb = el("button", "icon-btn sm", "+");
      nb.title = "New chat in project";
      nb.addEventListener("click", () => newChat());
      head.appendChild(nb);
      const ex = el("button", "icon-btn sm", "\u2b73");
      ex.title = "Export project chats as Markdown";
      ex.addEventListener("click", exportProject);
      head.appendChild(ex);
      const gear = el("button", "icon-btn sm", "\u2699");
      gear.title = "Project settings";
      gear.addEventListener("click", () => openProjModal(p));
      head.appendChild(gear);
      list.appendChild(head);
    }
  }
  if (pinnedCount(rows)) {
    const g = el("div", "sess-group");
    g.appendChild(el("div", "sess-group-label", "Pinned"));
    for (const s of pinnedRows(rows)) g.appendChild(mkSessItem(s));
    list.appendChild(g);
  }
  const rest = unpinnedRows(rows);
  if (!rows.length) {
    list.appendChild(el("div", "empty", q ? "No matches." : state.proj ? "No chats in this project yet." : "No sessions yet. Say something to begin."));
    return;
  }
  let curBucket = null;
  let group = null;
  for (const s of rest) {
    const bucket = sessBucket(s.id);
    if (bucket !== curBucket) {
      curBucket = bucket;
      group = el("div", "sess-group");
      group.appendChild(el("div", "sess-group-label", bucket));
      list.appendChild(group);
    }
    group.appendChild(mkSessItem(s));
  }
  renderArchToggle();
}

function pinnedRows(rows) { return rows.filter((s) => state.pins.includes(s.id)); }
function unpinnedRows(rows) { return rows.filter((s) => !state.pins.includes(s.id)); }
function pinnedCount(rows) { return pinnedRows(rows).length; }

function projRow(p) {
  const active = p.id === state.proj;
  const row = el("div", "proj-item" + (active ? " active" : "") + (p.color ? " pc-" + p.color : "") + (projPinned(p.id) ? " pinned" : ""));
  row.title = p.workspace ? p.name + "\n" + p.workspace : p.name;
  const ic = el("span", "proj-ic");
  ic.innerHTML = '<svg viewBox="0 0 24 24"><path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
  row.appendChild(ic);
  row.appendChild(el("span", "proj-name", (projPinned(p.id) ? "\u2605 " : "") + p.name));
  if ((p.files || []).length) row.appendChild(el("span", "proj-kcount", String(p.files.length)));
  row.appendChild(el("span", "proj-count", String(p.sessions || 0)));
  const acts = el("div", "proj-acts");
  const addB = el("button", "", "+");
  addB.title = "New chat in this project";
  addB.addEventListener("click", (e) => { e.stopPropagation(); selectProject(p.id); newChat(); });
  const starB = el("button", "", projPinned(p.id) ? "\u2605" : "\u2606");
  starB.title = projPinned(p.id) ? "Unstar project" : "Star project";
  starB.addEventListener("click", (e) => { e.stopPropagation(); toggleProjPin(p.id); });
  const gearB = el("button", "", "\u2699");
  gearB.title = "Project settings";
  gearB.addEventListener("click", (e) => { e.stopPropagation(); openProjModal(p); });
  acts.append(addB, starB, gearB);
  row.appendChild(acts);
  row.addEventListener("click", () => selectProject(active ? "" : p.id));
  /* drag a chat onto a project to move it there */
  row.addEventListener("dragover", (e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; row.classList.add("drop"); });
  row.addEventListener("dragleave", () => row.classList.remove("drop"));
  row.addEventListener("drop", (e) => {
    e.preventDefault();
    row.classList.remove("drop");
    const sid = e.dataTransfer.getData("text/plain");
    if (sid) assignSession(sid, p.id);
  });
  return row;
}

function mkSessItem(s) {
  const item = el("div", "sess-item" + (s.id === state.sid ? " active" : "") + (s.archived ? " archived" : ""));
  item.draggable = true;
  item.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData("text/plain", s.id);
    e.dataTransfer.effectAllowed = "move";
  });
  item.appendChild(el("div", "sess-title", (state.pins.includes(s.id) ? "\u2605 " : "") + (s.task || "(interactive)")));
  const meta = el("div", "sess-meta");
  if (s.busy) meta.appendChild(el("span", "sess-busy"));
  meta.appendChild(el("span", "", relTime(s.id)));
  if (s.busy) meta.appendChild(el("span", "", "running"));
  if (s.archived) meta.appendChild(el("span", "sess-arch-tag", "archived"));
  if (finishedSids.includes(s.id)) meta.appendChild(el("span", "sess-done", "finished"));
  if (s.project && !state.proj) {
    const pr = state.projects.find((p) => p.id === s.project);
    if (pr) meta.appendChild(el("span", "sess-proj-tag", pr.name));
  }
  item.appendChild(meta);
  const acts = el("div", "sess-acts");
  const pinB = el("button", "", "\u2605");
  pinB.title = state.pins.includes(s.id) ? "Unpin" : "Pin";
  pinB.addEventListener("click", (e) => { e.stopPropagation(); togglePin(s.id); });
  const delB = el("button", "", "\u00d7");
  delB.title = "Delete session";
  delB.addEventListener("click", (e) => { e.stopPropagation(); deleteSession(s.id); });
  acts.append(pinB, delB);
  item.appendChild(acts);
  item.title = s.id;
  item.addEventListener("click", () => openSession(s.id));
  return item;
}

function togglePin(sid) {
  const i = state.pins.indexOf(sid);
  if (i >= 0) state.pins.splice(i, 1);
  else state.pins.push(sid);
  localStorage.setItem("df_pins", JSON.stringify(state.pins));
  renderSessions();
}

async function deleteSession(sid) {
  if (!(await uiConfirm({ title: "Delete session", message: "Delete session " + sid + "? This cannot be undone.", ok: "Delete", danger: true }))) return;
  try {
    await api("/api/session/" + encodeURIComponent(sid), { method: "DELETE" });
    if (state.sid === sid) newChat();
    toast("Session deleted", "ok");
    loadSessions();
  } catch (e) {
    toast(e.message, "err");
  }
}

async function renameSession(sid) {
  const cur = (state.sessions.find((s) => s.id === sid) || {}).task || "";
  const title = await uiPrompt({ title: "Rename session", message: "Session title:", value: cur });
  if (!title || !title.trim()) return;
  try {
    await api("/api/rename", { method: "POST", body: JSON.stringify({ session_id: sid, title: title.trim() }) });
    await loadSessions();
    if (state.sid === sid) $("#sessionTitle").textContent = title.trim();
    toast("Renamed", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
}

async function branchSession(sid) {
  try {
    const out = await api("/api/branch", { method: "POST", body: JSON.stringify({ session_id: sid }) });
    await loadSessions();
    toast("Branched -> " + out.session_id, "ok");
    openSession(out.session_id);
  } catch (e) {
    toast(e.message, "err");
  }
}

function buildSessionMarkdown(data) {
  const lines = ["# " + ((data.meta && data.meta.task) || data.id), "", "> session `" + data.id + "`", ""];
  for (const it of data.items || []) {
    if (it.kind === "user") {
      lines.push("## User", "", it.text, "");
      if (it.images) lines.push("_" + it.images + " image(s) attached_", "");
    } else if (it.kind === "assistant") {
      if (it.reasoning) lines.push("<details><summary>Reasoning</summary>", "", it.reasoning, "", "</details>", "");
      if (it.text) lines.push("### Assistant", "", it.text, "");
      for (const call of it.calls || []) {
        lines.push("- **tool**: `" + call.name + "`");
        const res = (it.results || {})[call.id];
        if (res) lines.push("  - " + (res.ok ? "ok: " : "error: ") + String(res.body).slice(0, 300).replace(/\n/g, "\n    "));
      }
      if ((it.calls || []).length) lines.push("");
    }
  }
  return lines.join("\n");
}

function download(name, content, type) {
  const blob = new Blob([content], { type });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

async function exportSession(fmt) {
  if (!state.sid) { toast("No session open", "info"); return; }
  try {
    const data = await api("/api/session/" + encodeURIComponent(state.sid));
    if (fmt === "md") download(state.sid + ".md", buildSessionMarkdown(data), "text/markdown");
    else download(state.sid + ".json", JSON.stringify(data, null, 2), "application/json");
    toast("Exported", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
}

/* ---------------------------------------------------------------- menus */

function closeMenus() {
  $("#kebabMenu").classList.add("hidden");
  $("#modelMenu").classList.add("hidden");
  $("#themeMenu").classList.add("hidden");
  $("#safetyMenu").classList.add("hidden");
  $("#projPickMenu").classList.add("hidden");
}

/* Placement parity (Cursor/ChatGPT/Claude): a dropdown opens anchored to the
   control that triggered it — below it, edge-aligned, flipped above when
   there is no room below, always clamped to the viewport. One menu at a
   time; clicking the trigger again closes it. */
function openDropdown(menu, anchor, opts = {}) {
  if (!anchor || !anchor.isConnected) anchor = $("#kebabBtn");
  const wasOpen = !menu.classList.contains("hidden");
  closeMenus();
  if (wasOpen) return;
  menu.classList.remove("hidden");
  let r = anchor.getBoundingClientRect();
  if (!r.width && !r.height) { anchor = $("#kebabBtn"); r = anchor.getBoundingClientRect(); }
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let left = opts.align === "left" ? r.left : r.right - mw;
  left = Math.max(8, Math.min(left, window.innerWidth - mw - 8));
  let top = r.bottom + 6;
  if (top + mh > window.innerHeight - 8) top = r.top - mh - 6;
  top = Math.max(8, Math.min(top, window.innerHeight - mh - 8));
  menu.style.left = Math.round(left) + "px";
  menu.style.top = Math.round(top) + "px";
  menu.style.right = "auto";
}

function openKebab() {
  const m = $("#kebabMenu");
  // archive label follows the open session's current flag
  const cur = state.sessions.find((s) => s.id === state.sid);
  const archBtn = m.querySelector('[data-act="archive"]');
  if (archBtn) archBtn.textContent = cur && cur.archived ? "Unarchive session" : "Archive session";
  openDropdown(m, $("#kebabBtn"));
}

function openModelMenu() {
  const m = $("#modelMenu");
  const recents = JSON.parse(localStorage.getItem("df_recent") || "[]");
  const favs = favModels();
  m.replaceChildren();
  const mkRow = (label, model) => {
    const row = el("div", "mm-row");
    const b = el("button", "", label);
    b.addEventListener("click", async () => {
      closeMenus();
      try {
        const body = { model };
        if (state.sid) body.session_id = state.sid; // Cline parity: per-chat model
        const out = await api("/api/config", { method: "POST", body: JSON.stringify(body) });
        if (out.session_only) {
          state.sessionModels[state.sid] = out.model;
          toast("Model for this chat: " + out.model, "ok");
        } else {
          state.info = out;
          rememberModel(out.model);
          toast("Model: " + out.model, "ok");
        }
        renderHeaderPills();
      } catch (e) { toast(e.message, "err"); }
    });
    const eff = state.sessionModels[state.sid] || (state.info && state.info.model);
    if (model === eff) b.classList.add("cur");
    row.appendChild(b);
    const on = favs.includes(model);
    const star = el("button", "mm-star" + (on ? " on" : ""), on ? "\u2605" : "\u2606");
    star.title = on ? "Remove from favorites (Alt+M cycles favorites)" : "Pin to favorites (Alt+M cycles favorites)";
    star.addEventListener("click", (e) => {
      e.stopPropagation();
      const cur = favModels();
      const next = cur.includes(model) ? cur.filter((x) => x !== model) : [model].concat(cur).slice(0, 6);
      localStorage.setItem("df_favmodels", JSON.stringify(next));
      openModelMenu();
    });
    row.appendChild(star);
    m.appendChild(row);
  };
  if (favs.length) {
    m.appendChild(el("div", "mm-head", "favorites \u00b7 Alt+M cycles"));
    favs.forEach((r) => mkRow(r, r));
    m.appendChild(el("div", "mm-head", "recent"));
  }
  for (const r of recents.filter((x) => !favs.includes(x))) mkRow(r, r);
  if (state.info && state.info.model && !recents.includes(state.info.model) && !favs.includes(state.info.model)) mkRow(state.info.model + "  (current)", state.info.model);
  const more = el("button", "", "All settings\u2026");
  more.addEventListener("click", () => { closeMenus(); openSettings(); });
  m.appendChild(more);
  openDropdown(m, $("#modelPill"));
}

function rememberModel(model) {
  if (!model) return;
  let rec = JSON.parse(localStorage.getItem("df_recent") || "[]");
  rec = [model].concat(rec.filter((x) => x !== model)).slice(0, 6);
  localStorage.setItem("df_recent", JSON.stringify(rec));
}

/* ---------------------------------------------------------------- palette */

let palItems = [];
let palSel = 0;
let chordPrefix = null;   // a pressed Ctrl+K waiting for its second key
let chordTimer = null;

function paletteOpen(commandMode) {
  $("#paletteOverlay").classList.remove("hidden");
  const inp = $("#paletteInput");
  // VS Code has one overlay in two modes: Ctrl+P finds files, and a leading
  // ">" turns it into the command palette. Ctrl+Shift+P is that with the ">"
  // already typed, so both keys land here and the input decides.
  palFileMode = !commandMode;
  inp.value = commandMode ? "> " : "";
  inp.placeholder = commandMode
    ? "Run a command, or delete the > to find a file\u2026"
    : "Go to file, or type > for commands\u2026";
  paletteBuild(inp.value);
  if (palFileMode) palFilesLoad();
  $("#palSearch").classList.add("hidden");
  setTimeout(() => { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }, 30);
}

let palFileMode = false;
let palFiles = [];

async function palFilesLoad() {
  try {
    palFiles = await workspaceFiles();
  } catch { palFiles = []; }
  const inp = $("#paletteInput");
  // the overlay may already be closed, or moved on to a command query
  if (!$("#paletteOverlay").classList.contains("hidden") && !inp.value.trimStart().startsWith(">")) {
    paletteBuild(inp.value);
  }
}

function palFuzzy(path, q) {
  if (!q) return 0;
  const hay = path.toLowerCase();
  const base = hay.slice(hay.lastIndexOf("/") + 1);
  let i = 0, score = 0, run = 0;
  for (const ch of q) {
    const at = hay.indexOf(ch, i);
    if (at < 0) return -1;                       // not a subsequence: reject
    run = at === i ? run + 1 : 0;                // consecutive chars score higher
    score += 1 + run * 2 + (at >= hay.length - base.length ? 2 : 0);
    i = at + 1;
  }
  return score - hay.length * 0.01;              // shorter paths win ties
}
function paletteClose() { $("#paletteOverlay").classList.add("hidden"); $("#palSearch").classList.add("hidden"); }

let palSearchTimer = null;
let palSearchSeq = 0;
function paletteSearchAsync(q) {
  clearTimeout(palSearchTimer);
  const box = $("#palSearch");
  if (!q || q.length < 2) { box.classList.add("hidden"); return; }
  const seq = ++palSearchSeq;
  palSearchTimer = setTimeout(async () => {
    try {
      const data = await api("/api/search?q=" + encodeURIComponent(q) + "&limit=8");
      if (seq !== palSearchSeq) return; // a newer keystroke superseded this query
      box.replaceChildren();
      if (!data.results || !data.results.length) { box.classList.add("hidden"); return; }
      box.appendChild(el("div", "pal-search-head", "in chat content"));
      for (const r of data.results) {
        const row = el("div", "pal-item");
        row.appendChild(el("span", "pal-icon", "\u2632"));
        const label = el("span", "", (r.task || r.sid) + " \u00b7 " + r.hits + " hit" + (r.hits > 1 ? "s" : ""));
        row.appendChild(label);
        if (r.snippet) {
          const sn = el("span", "pal-snippet", r.snippet);
          row.appendChild(sn);
        }
        row.addEventListener("click", () => { paletteClose(); openSession(r.sid); });
        box.appendChild(row);
      }
      box.classList.remove("hidden");
    } catch {}
  }, 220);
}

function paletteBuild(raw) {
  const commandMode = raw.trimStart().startsWith(">") || !palFileMode;
  if (!commandMode) return palBuildFiles(raw.trim().toLowerCase());
  let q = raw.trimStart();
  if (q.startsWith(">")) q = q.slice(1);
  q = q.trim().toLowerCase();
  palItems = [];
  const cmds = [
    ["New chat", () => newChat()],
    ["Open folder\u2026", () => folderOpen()],
    ["New project\u2026", () => openProjModal(null)],
    ["Toggle sidebar", () => toggleSidebar()],
    ["Toggle theme", () => toggleTheme()],
    ["Open settings", () => openSettings()],
    ["Keyboard shortcuts", () => shortcutsOpen()],
    ["Go to file\u2026", () => paletteOpen(false)],
    ["Export session as Markdown", () => exportSession("md")],
    ["Export session as JSON", () => exportSession("json")],
    ["Show Activity tab", () => stageShow("activity", false)],
    ["Show Changes tab", () => stageShow("changes", false)],
    ["Show Preview tab", () => stageShow("preview", false)],
    ["Show Plan tab", () => stageShow("plan", false)],
    ["Show Files tab", () => { stageShow("files", false); filesEnsure(true); }],
    ["Show Memory graph", () => stageShow("memory", false)],
    ["Show Pipelines", () => stageShow("pipelines", false)],
    ["Show Runs tab", () => stageShow("runs", false)],
    ["Memory: reindex the workspace", () => { stageShow("memory", false); mgLoad(true); }],
  ];
  for (const p of state.projects) {
    cmds.push(["Open project: " + p.name, () => selectProject(p.id)]);
  }
  for (const [label, fn] of cmds) {
    if (!q || label.toLowerCase().includes(q)) palItems.push({ icon: "\u25b8", label, run: fn });
  }
  for (const s of state.sessions) {
    if (q && !((s.task || "").toLowerCase().includes(q) || s.id.toLowerCase().includes(q))) continue;
    palItems.push({ icon: "\u25a4", label: (s.task || "(interactive)") + "  \u00b7 " + s.id, run: () => openSession(s.id) });
  }
  palRender();
}

function palRender() {
  palSel = 0;
  const list = $("#paletteList");
  list.replaceChildren();
  palItems.slice(0, 40).forEach((it, i) => {
    const row = el("div", "pal-item" + (i === palSel ? " sel" : ""));
    row.appendChild(el("span", "pal-icon", it.icon));
    row.appendChild(el("span", "pal-label", it.label));
    row.addEventListener("click", () => { paletteClose(); it.run(); });
    list.appendChild(row);
  });
}

function palBuildFiles(q) {
  palItems = [];
  const scored = [];
  for (const f of palFiles) {
    if (f.endsWith("/")) continue;               // Quick Open lists files, not folders
    const sc = q ? palFuzzy(f, q) : -f.length * 0.01;
    if (sc > -1) scored.push([sc, f]);
  }
  scored.sort((a, b) => b[0] - a[0]);
  for (const [, f] of scored.slice(0, 40)) {
    palItems.push({
      icon: "\u25a1",
      label: f,
      run: () => { stageShow("files", false); openWsFile(f); },
    });
  }
  if (!palItems.length) {
    palItems.push({ icon: "\u00b7", label: palFiles.length ? "no matching file" : "reading the workspace\u2026", run: () => {} });
  }
  palRender();
}

function paletteKey(k) {
  const rows = $("#paletteList").children;
  if (k === "ArrowDown") { palSel = Math.min(palSel + 1, Math.min(palItems.length, 40) - 1); }
  else if (k === "ArrowUp") { palSel = Math.max(palSel - 1, 0); }
  else if (k === "Enter") { const it = palItems[palSel]; if (it) { paletteClose(); it.run(); } return true; }
  else if (k === "Escape") { paletteClose(); return true; }
  else return false;
  for (let i = 0; i < rows.length; i++) rows[i].classList.toggle("sel", i === palSel);
  const sel = rows[palSel];
  if (sel) sel.scrollIntoView({ block: "nearest" });
  return true;
}

/* ---------------------------------------------------------------- shortcuts */

function shortcutsOpen() { $("#shortcutsModal").classList.remove("hidden"); }
function shortcutsClose() { $("#shortcutsModal").classList.add("hidden"); }

/* ---------------------------------------------------------------- theme */

/* Themes shipped by Omarchy Linux (https://omarchy.org/manual/themes/) —
   palettes generated from the colors.toml of each basecamp/omarchy theme. */
const THEMES = [
  { id: "dark", name: "Saturday Dark", mode: "dark" },
  { id: "light", name: "Saturday Light", mode: "light" },
  { id: "tokyo-night", name: "Tokyo Night", mode: "dark" },
  { id: "catppuccin", name: "Catppuccin", mode: "dark" },
  { id: "lumon", name: "Lumon", mode: "dark" },
  { id: "ethereal", name: "Ethereal", mode: "dark" },
  { id: "everforest", name: "Everforest", mode: "dark" },
  { id: "gruvbox", name: "Gruvbox", mode: "dark" },
  { id: "miasma", name: "Miasma", mode: "dark" },
  { id: "hackerman", name: "Hackerman", mode: "dark" },
  { id: "osaka-jade", name: "Osaka Jade", mode: "dark" },
  { id: "kanagawa", name: "Kanagawa", mode: "dark" },
  { id: "nord", name: "Nord", mode: "dark" },
  { id: "matte-black", name: "Matte Black", mode: "dark" },
  { id: "vantablack", name: "Vantablack", mode: "dark" },
  { id: "ristretto", name: "Ristretto", mode: "dark" },
  { id: "retro-82", name: "Retro 82", mode: "dark" },
  { id: "flexoki-light", name: "Flexoki Light", mode: "light" },
  { id: "rose-pine", name: "Rose Pine", mode: "light" },
  { id: "catppuccin-latte", name: "Catppuccin Latte", mode: "light" },
  { id: "white", name: "White", mode: "light" },
];
function themeById(id) { return THEMES.find((t) => t.id === id) || null; }

function resolveTheme(t) {
  if (t !== "system") return t;
  try { return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"; } catch { return "dark"; }
}
function applyTheme(t) {
  const resolved = resolveTheme(t);
  const meta = themeById(resolved);
  document.documentElement.dataset.theme = resolved;
  document.documentElement.dataset.mode = meta ? meta.mode : resolved === "light" ? "light" : "dark";
  localStorage.setItem("df_theme", t);
  rememberModeTheme(resolved);
}
function rememberModeTheme(id) {
  const m = themeById(id);
  if (!m || id === "system") return;
  localStorage.setItem(m.mode === "light" ? "df_theme_light" : "df_theme_dark", id);
}
function lastModeTheme(mode) {
  return localStorage.getItem(mode === "light" ? "df_theme_light" : "df_theme_dark") || (mode === "light" ? "light" : "dark");
}
function toggleTheme() {
  const cur = themeById(document.documentElement.dataset.theme);
  const other = cur && cur.mode === "light" ? "dark" : "light";
  applyTheme(lastModeTheme(other));
}
function openThemeMenu() {
  const m = $("#themeMenu");
  m.replaceChildren();
  for (const t of THEMES) {
    const b = el("button", "", t.name);
    if (t.id === document.documentElement.dataset.theme) b.classList.add("cur");
    b.addEventListener("click", () => { closeMenus(); applyTheme(t.id); toast("Theme: " + t.name, "ok"); });
    m.appendChild(b);
  }
  const sys = el("button", "", "System default");
  if ((localStorage.getItem("df_theme") || "dark") === "system") sys.classList.add("cur");
  sys.addEventListener("click", () => { closeMenus(); applyTheme("system"); toast("Theme follows the OS", "ok"); });
  m.appendChild(sys);
  openDropdown(m, $("#themeBtn"));
}
function initThemeListener() {
  try {
    window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
      if ((localStorage.getItem("df_theme") || "dark") === "system") applyTheme("system");
    });
  } catch {}
}

/* ---------------------------------------------------------------- files tab */

let filesLoaded = false;
let filesRel = "";
const FILE_IMG_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"]);

async function filesEnsure(force) {
  if (!force && filesLoaded) return;
  filesLoaded = true;
  await filesList(filesRel || "");
}

function fmtFileTime(mtime) {
  if (!mtime) return "";
  const d = new Date(mtime * 1000);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
    d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

async function filesList(rel) {
  filesRel = rel || "";
  const pane = stagePanes.files;
  pane.replaceChildren();
  const head = el("div", "stage-head");
  head.appendChild(el("span", "stage-head-title", curProject() ? "Project files" : "Workspace files"));
  const crumbs = el("div", "crumbs");
  const rootB = el("button", "crumb", "/");
  rootB.title = "Workspace root";
  rootB.addEventListener("click", () => filesList(""));
  crumbs.appendChild(rootB);
  const parts = filesRel ? filesRel.split("/") : [];
  parts.forEach((p, i) => {
    crumbs.appendChild(el("span", "crumb-sep", "\u203a"));
    const b = el("button", "crumb", p);
    b.addEventListener("click", () => filesList(parts.slice(0, i + 1).join("/")));
    crumbs.appendChild(b);
  });
  head.appendChild(crumbs);
  const refresh = el("button", "st-btn", "refresh");
  refresh.title = "Reload this folder";
  refresh.addEventListener("click", () => filesList(filesRel));
  head.appendChild(refresh);
  pane.appendChild(head);
  const bar = el("div", "stage-toolbar");
  const inp = el("input", "stage-filter");
  inp.type = "text";
  inp.placeholder = "filter this folder\u2026";
  inp.spellcheck = false;
  bar.appendChild(inp);
  const sub = el("span", "stage-head-sub", "");
  bar.appendChild(sub);
  pane.appendChild(bar);
  const wrap = el("div", "files-wrap");
  pane.appendChild(wrap);
  inp.addEventListener("input", () => {
    const q = inp.value.trim().toLowerCase();
    for (const row of wrap.querySelectorAll(".file-row")) {
      row.classList.toggle("filtered", !!q && !row.dataset.name.includes(q));
    }
  });
  let data;
  try { data = await api("/api/ws?path=" + encodeURIComponent(filesRel) + "&sid=" + encodeURIComponent(state.sid || "")); }
  catch (e) {
    wrap.appendChild(el("div", "stage-empty", "Cannot list workspace: " + e.message));
    return;
  }
  if (!data.entries.length) wrap.appendChild(el("div", "stage-empty", "Empty directory"));
  sub.textContent = data.entries.length + " item" + (data.entries.length === 1 ? "" : "s");
  for (const ent of data.entries) {
    const full = filesRel ? filesRel + "/" + ent.name : ent.name;
    const row = el("button", "file-row" + (ent.dir ? " dir" : ""));
    row.dataset.name = ent.name.toLowerCase();
    const sizeTxt = ent.dir ? "\u2014" : ent.size >= 1024 ? (ent.size / 1024).toFixed(1) + " KB" : ent.size + " B";
    row.append(
      el("span", "file-name", (ent.dir ? "\ud83d\udcc1 " : "\ud83d\udcc4 ") + ent.name),
      el("span", "file-mtime mono", fmtFileTime(ent.mtime)),
      el("span", "file-size mono", sizeTxt)
    );
    row.title = ent.name + (ent.path ? "\n" + ent.path : "");
    if (ent.dir) row.addEventListener("click", () => filesList(full));
    else row.addEventListener("click", () => filePreview(ent, full, row));
    wrap.appendChild(row);
  }
  const prev = el("div", "file-preview hidden");
  wrap.appendChild(prev);
}

async function filePreview(ent, rel, row) {
  const pane = stagePanes.files;
  let prev = pane.querySelector(".file-preview");
  if (prev && prev.dataset.path === rel && !prev.classList.contains("hidden")) { prev.classList.add("hidden"); return; }
  if (!prev) { prev = el("div", "file-preview hidden"); pane.querySelector(".files-wrap").appendChild(prev); }
  prev.dataset.path = rel;
  prev.classList.remove("hidden");
  prev.replaceChildren(el("div", "stage-empty", "Loading " + rel + "\u2026"));
  prev.scrollIntoView({ block: "nearest" });
  const ext = rel.includes(".") ? rel.slice(rel.lastIndexOf(".")).toLowerCase() : "";
  if (FILE_IMG_EXTS.has(ext) && ent.path) {
    prev.replaceChildren();
    const head = el("div", "file-preview-head");
    head.appendChild(el("span", "mono", rel + "  \u00b7 " + (ent.size >= 1024 ? (ent.size / 1024).toFixed(1) + " KB" : ent.size + " B")));
    const openB = el("button", "copybtn", "open");
    openB.addEventListener("click", () => window.open(fileUrl(ent.path), "_blank"));
    head.appendChild(openB);
    prev.appendChild(head);
    const img = document.createElement("img");
    img.className = "file-img";
    img.alt = rel;
    img.src = fileUrl(ent.path);
    img.addEventListener("click", () => window.open(img.src, "_blank"));
    prev.appendChild(img);
    return;
  }
  try {
    const data = await api("/api/wsfile?path=" + encodeURIComponent(rel) + "&sid=" + encodeURIComponent(state.sid || ""));
    prev.replaceChildren();
    const head = el("div", "file-preview-head");
    head.appendChild(el("span", "mono", rel + "  \u00b7 " + data.size + " bytes" + (data.truncated ? " (truncated)" : "")));
    const cp = el("button", "copybtn", "copy");
    cp.addEventListener("click", () => navigator.clipboard.writeText(data.content));
    head.appendChild(cp);
    prev.appendChild(head);
    const term = el("div", "term");
    const pre = el("pre");
    pre.textContent = data.content;
    term.appendChild(pre);
    prev.appendChild(term);
  } catch (e) {
    prev.replaceChildren(el("div", "stage-empty", e.message));
  }
}

/* ---------------------------------------------------------------- voice */

let recog = null;
let recogOn = false;

function micToggle() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { toast("Speech recognition not available in this browser", "info"); return; }
  if (recogOn) { recog.stop(); return; }
  recog = new SR();
  recog.lang = localStorage.getItem("df_mic_lang") || "en-US";
  recog.interimResults = true;
  recog.continuous = true;
  // re-anchored on every result: a base captured once clobbers anything the
  // user types while dictating
  recog.onresult = (ev) => {
    let txt = "";
    for (const r of ev.results) txt += r[0].transcript;
    const input = $("#input");
    const base = input.dataset.micBase ?? "";
    input.value = (base ? base + " " : "") + txt;
    autoGrow(input);
    updateSendEnabled();
  };
  recog.onend = () => {
    recogOn = false;
    $("#micBtn").classList.remove("rec");
    // hands-free loop: auto-send a beat after speech ends
    const v = $("#input").value.trim();
    if (v && localStorage.getItem("df_voice_autosend") !== "0" && !state.busy) {
      setTimeout(() => {
        if (!state.busy && $("#input").value.trim() && !recogOn) send();
      }, 700);
    }
  };
  recog.onerror = () => { recogOn = false; $("#micBtn").classList.remove("rec"); };
  $("#input").dataset.micBase = $("#input").value;
  recog.start();
  recogOn = true;
  $("#micBtn").classList.add("rec");
}

function refreshSidebarBusy() {
  for (const item of $("#sessionList").children) {
    item.classList.toggle("active", item.title === state.sid);
  }
}

function adoptSession(sid, proj) {
  if (!sid) return;
  const changed = state.sid !== sid;
  state.sid = sid;
  if (changed) { state.ctx = null; state.ctxPrompt = 0; }
  localStorage.setItem("df_sid", sid);
  if (proj != null && proj !== state.proj) {
    state.proj = proj || "";
    if (state.proj) localStorage.setItem("df_proj", state.proj);
    else localStorage.removeItem("df_proj");
    updateProjChip();
    stageHomeFill();
  }
  const hs = $("#homeSession");
  if (hs) hs.textContent = sid;
  updateTokMeter();
  if (changed) renderSessions();
}

async function openSession(sid) {
  // switching is always allowed now: a busy current stream detaches (the run
  // keeps going server-side and stays visible in Runs), and a busy target
  // re-attaches to its live tail
  if (state.busy) {
    if (state.sid === sid) return; // already here, already live
    markDetached(state.sid); // the run continues server-side; badge it in the sidebar
    detachAllStreams();
    setBusy(false);
    live = null;
  }
  let data;
  try { data = await api("/api/session/" + encodeURIComponent(sid)); }
  catch { toast("Could not load session", "err"); return; }
  state.sid = sid;
  clearRunFlags(sid);
  state.ctx = null;
  state.ctxPrompt = 0;
  state.lastUser = "";
  invalidateUserIdxs();
  closeFind();
  closeCompare();
  localStorage.setItem("df_sid", sid);
  const proj = (data.meta && data.meta.project) || "";
  if (proj !== state.proj) {
    state.proj = proj;
    if (state.proj) localStorage.setItem("df_proj", state.proj);
    else localStorage.removeItem("df_proj");
    updateProjChip();
    stageHomeFill();
  }
  // an empty stored session (e.g. created but never sent to) would otherwise
  // render a blank thread — the welcome hero with suggestions is friendlier
  if (!(data.items || []).length) showEmptyState(); else hideEmptyState();
  thread.replaceChildren();
  live = null;
  stageReset();
  const title = (data.meta && data.meta.task) || sid;
  $("#sessionTitle").textContent = title;
  document.title = title + " - Saturday";
  for (const it of data.items || []) hydrateItem(it);
  renderSessions();
  scrollDown(true);
  loadCtx();
  restoreDraft();
  // declared BEFORE first use: a const referenced above its declaration is a
  // TDZ ReferenceError that silently killed startup restore, busy re-attach
  // and edit-&-resend (openSession threw right after hydrating)
  const meta = state.sessions.find((s) => s.id === sid);
  if (meta && meta.busy) clearFollowups();
  // a session that is still running re-attaches to its live event tail
  if (meta && meta.busy) {
    setBusy(true);
    attachTail(sid);
  }
}

function hydrateItem(it) {
  if (it.kind === "user") {
    const wrap = el("div", "msg-user");
    const bub = el("div", "user-bubble");
    bub.textContent = it.text;
    wrap.appendChild(bub);
    if (it.images > 0) wrap.appendChild(el("div", "user-nimg", "+" + it.images + " image" + (it.images > 1 ? "s" : "")));
    const actions = msgActions(() => it.text);
    const resendBtn = el("button", "msg-act");
    resendBtn.title = "Edit & resend (branches here, replaces this exchange)";
    resendBtn.innerHTML = IC_SEND;
    resendBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      await branchAt(typeof it.msg_idx === "number" ? it.msg_idx : null, it.text);
    });
    actions.appendChild(resendBtn);
    if (typeof it.msg_idx === "number") {
      const forkBtn = el("button", "msg-act");
      forkBtn.title = "Branch from here (keeps this message)";
      forkBtn.innerHTML = IC_FORK;
      forkBtn.addEventListener("click", async (e) => { e.stopPropagation(); await branchAt(it.msg_idx + 1, null); });
      actions.appendChild(forkBtn);
    }
    wrap.appendChild(actions);
    thread.appendChild(wrap);
    return;
  }
  if (it.kind === "assistant") {
    const turn = newTurn();
    const body = makeAssistantShell(turn);
    if (it.reasoning) {
      const th = makeThink(body);
      th.label.textContent = "Thought process";
      th.bodyDiv.textContent = it.reasoning;
      th.det.open = false;
    }
    if (it.text) {
      const c = el("div", "md");
      c.innerHTML = renderMd(it.text);
      body.appendChild(c);
      body.appendChild(msgActions(() => it.text));
    }
    for (const call of it.calls || []) {
      let args = {};
      if (typeof call.args_raw === "object" && call.args_raw) args = call.args_raw;
      else { try { args = JSON.parse(call.args_raw); } catch {} }
      const card = makeToolCard(call.name, args, false);
      const res = (it.results || {})[call.id];
      if (res) card._fillResult({ ok: res.ok, output: res.body, error: res.ok ? "" : res.body, images: [] });
      else { card._status.className = "chip chip-blk"; card._status.textContent = "blocked"; }
      body.appendChild(card);
      const se = stageEntry("h-" + call.id, call.name, args, false, true);
      if (res) se._fill({ ok: res.ok, output: res.body, error: res.ok ? "" : res.body, images: [] });
      else se._fill({ ok: false, output: "", error: "blocked (no result recorded)" });
      if ((call.name === "write_file" || call.name === "edit_file") && res && res.ok) {
        stageAddChange({ path: args.path || call.name, content: args.content || args.new_string || "" }, false);
      }
      if (call.name === "todo" && res && res.ok && res.body) stageSetPlan(res.body);
    }
  }
}

function newChat() {
  // allowed even while a run streams: detach the reader; the run keeps going
  // server-side and stays watchable in the Runs tab
  if (state.busy) {
    markDetached(state.sid);
    detachAllStreams();
    setBusy(false);
    toast("Previous chat keeps running \u2014 watch it in Runs", "info");
  }
  state.sid = null;
  clearRunFlags("new");
  localStorage.removeItem("df_sid");
  invalidateUserIdxs();
  closeFind();
  showEmptyState();
  live = null;
  stageReset();
  const hs = $("#homeSession");
  if (hs) hs.textContent = "\u2014";
  renderSessions();
  loadCtx();
  restoreDraft();
  clearFollowups();
  $("#input").focus();
}
/* ------------------------------------------------------------------ settings */

// Fallback only: the live list comes from the backend via /api/state
// (info.slash_commands) so newly added commands always appear here.
const SLASH_COMMANDS_FALLBACK = [
  ["/help", "show available commands"],
];

function slashCommandList() {
  const builtins = (state.info && Array.isArray(state.info.slash_commands) && state.info.slash_commands.length)
    ? state.info.slash_commands
    : SLASH_COMMANDS_FALLBACK;
  const custom = Object.entries((state.info && state.info.custom_commands) || {})
    .map(([k, v]) => ["/" + k, v.description || "custom command"]);
  return builtins.concat(custom);
}

function slashMaybe() {
  const input = $("#input");
  const v = input.value;
  const pop = $("#slashPop");
  const caretToken = v.split(" ")[0];
  if (!v.startsWith("/") || v.includes("\n")) { closeSlash(); return; }
  const q = caretToken.slice(1).toLowerCase();
  slashItems = slashCommandList().filter(([c]) => c.slice(1).startsWith(q));
  if (!slashItems.length) { closeSlash(); return; }
  slashSel = 0;
  const customKeys = new Set(Object.keys((state.info && state.info.custom_commands) || {}));
  pop.replaceChildren();
  slashItems.forEach(([cmd, desc], i) => {
    const row = el("div", "slash-item" + (i === slashSel ? " sel" : ""));
    row.appendChild(el("span", "slash-cmd" + (customKeys.has(cmd.slice(1).toLowerCase()) ? " custom" : ""), cmd));
    row.appendChild(el("span", "slash-desc", desc));
    row.addEventListener("click", () => slashPick(cmd));
    pop.appendChild(row);
  });
  pop.classList.remove("hidden");
  positionSlash();
}
function positionSlash() {
  const wrap = $("#composerWrap");
  const pop = $("#slashPop");
  pop.style.left = Math.max(14, (wrap.clientWidth - 430) / 2 + 14) + "px";
}
function closeSlash() { $("#slashPop").classList.add("hidden"); slashItems = []; }
function slashPick(cmd) {
  const input = $("#input");
  const custom = (state.info && state.info.custom_commands) || {};
  const key = cmd.slice(1).toLowerCase();
  if (custom[key]) {
    // Warp-Drive-style prompt expansion: $ARGS swallows the text typed after
    // the command; templates without $ARGS simply fill the composer
    const rest = input.value.trim().replace(/^\S+/, "").trim();
    const tpl = custom[key].prompt || "";
    input.value = tpl.includes("$ARGS") ? tpl.replace(/\$ARGS/g, rest) : (rest ? tpl + " " + rest : tpl);
  } else {
    input.value = cmd + " ";
  }
  closeSlash();
  input.focus();
  autoGrow(input);
  updateSendEnabled();
}

function renderHeaderPills() {
  const info = state.info;
  if (!info) return;
  const sessModel = state.sessionModels[state.sid];
  $("#modelLabel").textContent = (info.provider || "") + " / " + (sessModel || info.model || "?");
  if (sessModel) $("#modelPill").title = "Model for THIS chat: " + sessModel + " (global: " + info.model + ") — click to switch";
  else $("#modelPill").title = "Switch model";
  const sb = $("#safetyBadge");
  sb.textContent = info.safety_mode;
  sb.className = "pill safety-" + info.safety_mode;
  const sc = $("#safetyChip");
  if (sc) {
    sc.firstChild.textContent = info.safety_mode;
    sc.className = "mode-chip safety-" + info.safety_mode;
  }
  const mb = $("#modeBadge");
  if (mb) mb.classList.toggle("hidden", info.persona_mode !== "assistant");
  updatePlanBadge();
  const hint = $("#keyHint");
  if (hint) {
    if (!info.has_key && !isLocalProvider(info.provider)) {
      hint.classList.remove("hidden");
      hint.textContent = "Connect an API key to start \u00b7 open Settings \u2192";
    } else {
      hint.classList.add("hidden");
    }
  }
}
function isLocalProvider(p) { return p === "ollama" || p === "vllm"; }

function filterSettings() {
  const term = ($("#cfgSearch").value || "").trim().toLowerCase();
  const panes = document.querySelectorAll(".set-pane");
  let first = null;
  panes.forEach((pane) => {
    const els = pane.querySelectorAll(".field, .toggle, .field-row, details.adv");
    let hits = 0;
    els.forEach((el) => {
      const show = !term || el.textContent.toLowerCase().includes(term);
      el.classList.toggle("hide", !show);
      if (show && term) hits++;
    });
    if (term && hits && !first) first = pane.dataset.sec;
  });
  if (term && first) {
    document.querySelectorAll("#setNav button").forEach((b) => b.classList.toggle("on", b.dataset.sec === first));
    panes.forEach((p) => p.classList.toggle("on", p.dataset.sec === first));
  }
}

function openSettings() {
  const info = state.info;
  if (!info) return;
  const sel = $("#cfgProvider");
  sel.replaceChildren();
  for (const p of info.providers) {
    const o = el("option", "", p.name + (p.has_key ? "  [key]" : ""));
    o.value = p.name;
    if (p.name === info.provider) o.selected = true;
    sel.appendChild(o);
  }
  const tsel = $("#cfgThemeSel");
  tsel.replaceChildren();
  const mkOpt = (v, label) => {
    const o = el("option", "", label);
    o.value = v;
    return o;
  };
  tsel.appendChild(mkOpt("dark", "Saturday Dark"));
  tsel.appendChild(mkOpt("light", "Saturday Light"));
  const og = document.createElement("optgroup");
  og.label = "Omarchy";
  for (const t of THEMES) {
    if (t.id === "dark" || t.id === "light") continue;
    og.appendChild(mkOpt(t.id, t.name));
  }
  tsel.appendChild(og);
  tsel.appendChild(mkOpt("system", "System default"));
  $("#cfgModel").value = info.model || "";
  $("#cfgSteps").value = info.max_steps;
  $("#cfgAssistant").checked = info.persona_mode === "assistant";
  $("#cfgAssistantName").value = info.assistant_name || "";
  $("#cfgAssistantTitle").value = info.assistant_user_title || "";
  $("#cfgTemp").value = info.temperature;
  $("#cfgTempVal").textContent = Number(info.temperature).toFixed(2);
  if ($("#cfgTopP")) {
    $("#cfgTopP").value = info.top_p ?? 0.95;
    $("#cfgTopPVal").textContent = Number(info.top_p ?? 0.95).toFixed(2);
  }
  if ($("#cfgMaxCtx")) $("#cfgMaxCtx").value = info.max_context_tokens || "";
  if ($("#cfgCompactAbove")) $("#cfgCompactAbove").value = info.compact_above_tokens || "";
  if ($("#cfgReqTimeout")) $("#cfgReqTimeout").value = info.request_timeout ?? 300;
  if ($("#cfgToolTimeout")) $("#cfgToolTimeout").value = info.tool_timeout ?? 120;
  if ($("#cfgRetries")) $("#cfgRetries").value = info.max_retries ?? 4;
  if ($("#cfgMemChars")) $("#cfgMemChars").value = info.memory_max_chars ?? 12000;
  if ($("#cfgStream")) $("#cfgStream").checked = info.stream !== false;
  if ($("#cfgShNet")) $("#cfgShNet").checked = info.shell_allow_network !== false;
  $("#cfgMaxTokens").value = info.max_tokens || 8192;
  $("#cfgFallback").value = (info.fallback_models || []).join(", ");
  $("#cfgWorkspace").textContent = info.workspace_root;
  $("#cfgPersona").value = info.persona_extra || "";
  $("#cfgBgOnly").checked = !!info.background_only;
  if ($("#cfgGuardrails")) $("#cfgGuardrails").checked = info.guardrails !== false;
  $("#cfgSound").checked = localStorage.getItem("df_sound") === "1";
  $("#cfgNotify").checked = localStorage.getItem("df_notify") !== "0";
  $("#cfgTts").checked = localStorage.getItem("df_tts") === "1";
  const rate = parseFloat(localStorage.getItem("df_tts_rate") || "1.05");
  $("#cfgTtsRate").value = rate;
  $("#cfgTtsRateVal").textContent = Number(rate).toFixed(2);
  $("#cfgMicLang").value = localStorage.getItem("df_mic_lang") || "en-US";
  $("#cfgVoiceAutosend").checked = localStorage.getItem("df_voice_autosend") !== "0";
  segSet("cfgSafety", info.safety_mode);
  populateToolToggles(info);
  if ($("#cfgSandboxed")) $("#cfgSandboxed").checked = info.sandboxed === true;
  if ($("#cfgMaxRunTokens")) $("#cfgMaxRunTokens").value = info.max_run_tokens || 0;
  if ($("#cfgProvenance")) $("#cfgProvenance").value = info.provenance_marking || "metadata";
  if ($("#cfgVerifyCmd")) $("#cfgVerifyCmd").value = info.verify_command || "";
  if ($("#cfgKeepReasoning")) $("#cfgKeepReasoning").checked = info.keep_reasoning_in_history === true;
  if ($("#cfgAutoTitle")) $("#cfgAutoTitle").checked = info.auto_title_sessions !== false;
  if ($("#cfgFollowups")) $("#cfgFollowups").checked = info.suggest_followups !== false;
  if ($("#cfgLspServers")) $("#cfgLspServers").value = JSON.stringify(info.lsp_servers || {}, null, 2);
  renderApprovalRules(info.approvals_allow || []);
  if ($("#cfgHooks")) {
    const h = info.hooks || {};
    $("#cfgHooks").value = JSON.stringify({ pre_tool_call: h.pre_tool_call || [], post_tool_call: h.post_tool_call || [] }, null, 2);
  }
  updatePlanBadge();
  const savedTheme = localStorage.getItem("df_theme") || "dark";
  if (savedTheme === "system") $("#cfgThemeSel").value = "system";
  else $("#cfgThemeSel").value = themeById(savedTheme) ? savedTheme : "dark";
  $("#dataPathConfig").textContent = info.config_dir || "~/.saturday";
  $("#dataPathSessions").textContent = info.sessions_dir || "~/.saturday/sessions";
  $("#dataPathWorkspace").textContent = info.workspace_root;
  $("#aboutVersion").textContent = "Saturday v" + info.version;
  $("#aboutProvider").textContent = (info.provider || "") + " / " + (info.model || "");
  renderUsage(info.usage || { turns: 0, total_tokens: 0, days: [], models: [] });
  loadMcp(false);
  settingsShow("general");
  providerHint();
  loadSchedules();
  loadCommands();
  loadAgents();
  loadRemote();
  $("#settingsWarn").classList.add("hidden");
  $("#settingsModal").classList.remove("hidden");
  $("#cfgModel").focus();
}
function closeSettings() { $("#settingsModal").classList.add("hidden"); }

/* ------------------------------------------------- auto-delegation agents */

async function loadAgents() {
  const wrap = $("#agentsList");
  if (!wrap) return;
  wrap.replaceChildren(el("span", "field-hint", "Checking\u2026"));
  let data;
  try { data = await api("/api/agents"); }
  catch { wrap.replaceChildren(el("span", "field-hint", "Could not load agents.")); return; }
  const rows = data.agents || [];
  wrap.replaceChildren();
  if (!rows.length) { wrap.appendChild(el("span", "field-hint", "No agents known.")); return; }
  for (const a of rows) {
    const row = el("div", "agent-row" + (a.installed ? "" : " agent-missing"));
    const lbl = el("label", "toggle");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = !!a.enabled;
    cb.disabled = !a.installed;
    cb.addEventListener("change", async () => {
      try {
        await api("/api/agents", { method: "POST", body: JSON.stringify({ agent: a.agent, enabled: cb.checked }) });
      } catch { cb.checked = !cb.checked; }
    });
    lbl.appendChild(cb);
    lbl.appendChild(el("span", "", a.agent));
    row.appendChild(lbl);
    row.appendChild(el("span", "agent-tier tier-" + a.tier_name, a.tier_name));
    const stat = a.runs
      ? Math.round(a.success * 100) + "% over " + a.runs + " run" + (a.runs === 1 ? "" : "s")
      : (a.installed ? "not used yet" : "not installed");
    row.appendChild(el("span", "agent-stat", stat));
    wrap.appendChild(row);
  }
}

async function loadModels() {
  const wrap = $("#modelsList");
  if (!wrap) return;
  wrap.replaceChildren(el("span", "field-hint", "Probing providers\u2026"));
  const free = $("#modelsFreeOnly").checked ? "?free=1" : "";
  let data;
  try { data = await api("/api/models" + free); }
  catch { wrap.replaceChildren(el("span", "field-hint", "Could not reach providers.")); return; }
  const providers = data.providers || {};
  wrap.replaceChildren();
  const names = Object.keys(providers).sort();
  if (!names.length) { wrap.appendChild(el("span", "field-hint", "No reachable providers \u2014 add an API key first.")); return; }
  for (const prov of names) {
    wrap.appendChild(el("div", "models-provider", prov + "  (" + providers[prov].length + ")"));
    for (const m of providers[prov]) {
      const lbl = el("label", "toggle model-row");
      const cb = el("input");
      cb.type = "checkbox";
      cb.dataset.provider = prov;
      cb.dataset.model = m.id;
      cb.checked = !!m.free;
      lbl.appendChild(cb);
      lbl.appendChild(el("span", "", m.id + (m.free ? "  [free]" : "")));
      wrap.appendChild(lbl);
    }
  }
}

async function loadRemote() {
  const st = $("#remoteState");
  const btn = $("#btnRemoteToggle");
  const box = $("#remoteUrl");
  if (!st) return;
  let d;
  try { d = await api("/api/remote"); }
  catch { st.textContent = "Could not check remote state."; return; }
  if (d.running) {
    st.textContent = "Running via " + d.provider;
    btn.textContent = "Stop";
    box.classList.remove("hidden");
    box.replaceChildren();
    const a = el("a", "", d.url);
    a.href = d.url; a.target = "_blank"; a.rel = "noopener";
    box.appendChild(a);
    box.appendChild(el("div", "field-hint", "Open this on your phone. Anyone with the link and token can drive this agent."));
  } else {
    box.classList.add("hidden");
    btn.textContent = "Start";
    st.textContent = d.available && d.available.length
      ? "Ready \u2014 will use " + d.available[0]
      : "No tunnel provider installed (cloudflared needs no account).";
    btn.disabled = !(d.available && d.available.length);
  }
}

function wireRemoteUi() {
  const btn = $("#btnRemoteToggle");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const starting = btn.textContent === "Start";
    btn.disabled = true;
    btn.textContent = starting ? "Starting\u2026" : "Stopping\u2026";
    try {
      const out = await api("/api/remote", { method: "POST", body: JSON.stringify({ start: starting }) });
      if (!out.ok && out.error) $("#remoteState").textContent = out.error;
    } catch (e) {
      $("#remoteState").textContent = "Failed to " + (starting ? "start" : "stop") + " the tunnel.";
    }
    btn.disabled = false;
    await loadRemote();
  });
}

function wireAgentsUi() {
  const refresh = $("#btnRefreshAgents");
  if (refresh) refresh.addEventListener("click", loadAgents);
  const browse = $("#btnBrowseModels");
  if (browse) browse.addEventListener("click", () => {
    $("#modelBrowser").classList.toggle("hidden");
    if (!$("#modelBrowser").classList.contains("hidden")) loadModels();
  });
  const freeOnly = $("#modelsFreeOnly");
  if (freeOnly) freeOnly.addEventListener("change", loadModels);
  const add = $("#btnAddFreeModels");
  if (add) add.addEventListener("click", async () => {
    const picks = {};
    for (const cb of $("#modelsList").querySelectorAll("input[type=checkbox]:checked")) {
      (picks[cb.dataset.provider] = picks[cb.dataset.provider] || []).push(cb.dataset.model);
    }
    if (!Object.keys(picks).length) return;
    add.disabled = true;
    try {
      const out = await api("/api/models", { method: "POST", body: JSON.stringify({ models: picks }) });
      add.textContent = "Added " + (out.added || []).length;
      await loadAgents();
    } catch { add.textContent = "Failed"; }
    setTimeout(() => { add.textContent = "Add selected to auto-delegation"; add.disabled = false; }, 2500);
  });
}

function settingsShow(sec) {
  for (const b of document.querySelectorAll("#setNav button")) b.classList.toggle("on", b.dataset.sec === sec);
  for (const p of document.querySelectorAll(".set-pane")) p.classList.toggle("on", p.dataset.sec === sec);
}

function segSet(id, v) {
  for (const b of $(("#" + id)).children) b.classList.toggle("on", b.dataset.v === v);
}
function segGet(id) {
  for (const b of $(("#" + id)).children) if (b.classList.contains("on")) return b.dataset.v;
  return "";
}
function providerHint() {
  const name = $("#cfgProvider").value;
  const p = (state.info.providers || []).find((x) => x.name === name);
  $("#cfgKeyHint").textContent = p ? (p.has_key ? "API key detected" : "no key found in environment") : "";
  $("#cfgModel").placeholder = p ? p.default_model : "";
}

// Tool toggles: checkbox = family ENABLED; unchecked members join disabled_tools.
const TOOL_TOGGLE_MAP = {
  cfgToolWebSearch: ["web_search"],
  cfgToolWebFetch: ["web_fetch"],
  cfgToolBrowser: ["browser"],
  cfgToolShell: ["shell"],
  cfgToolPython: ["python"],
  cfgToolFileWrites: ["write_file", "edit_file"],
  cfgToolComputerUse: ["pointer", "keyboard", "ui_invoke", "app_open", "window", "clipboard", "screen", "ui_tree"],
  cfgToolSubagents: ["task"],
  cfgToolMemory: ["memory"],
  cfgToolSkills: ["skill_save", "skill_load", "skills_index"],
  cfgToolRepoSearch: ["repo_search"],
};

function disabledToolsPatch() {
  if (!$("#cfgToolWebSearch")) return {};
  const disabled = new Set(state.info && state.info.disabled_tools ? state.info.disabled_tools : []);
  for (const [id, names] of Object.entries(TOOL_TOGGLE_MAP)) {
    const el = $("#" + id);
    if (!el) continue;
    for (const n of names) {
      if (el.checked) disabled.delete(n);
      else disabled.add(n);
    }
  }
  document.querySelectorAll("#cfgToolOther input[data-tool]").forEach((cb) => {
    if (cb.checked) disabled.delete(cb.dataset.tool);
    else disabled.add(cb.dataset.tool);
  });
  return { disabled_tools: [...disabled] };
}

function mappedToolNames() {
  return new Set(Object.values(TOOL_TOGGLE_MAP).flat());
}

function populateToolToggles(info) {
  const disabled = new Set((info && info.disabled_tools) || []);
  for (const [id, names] of Object.entries(TOOL_TOGGLE_MAP)) {
    const el = $("#" + id);
    if (el) el.checked = !names.some(n => disabled.has(n));
  }
  // dynamic "other tools" (MCP, lsp_*, view_image, ...): anything the backend
  // serves that has no dedicated checkbox above
  const wrap = $("#cfgToolOther");
  if (!wrap) return;
  wrap.replaceChildren();
  const mapped = mappedToolNames();
  const others = ((info && info.tool_names) || []).filter((n) => !mapped.has(n));
  for (const name of others) {
    const lbl = el("label", "toggle");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.dataset.tool = name;
    cb.checked = !disabled.has(name);
    lbl.appendChild(cb);
    lbl.appendChild(el("span", "", name));
    wrap.appendChild(lbl);
  }
  loadToolDescriptions();
}

// The checklist has always shown names. What each tool DOES lived only in
// `saturday tools`, so pull the descriptions in and hang them off every
// toggle - as a tooltip for the mapped families, and as visible text for the
// dynamic ones, whose names alone (lsp_*, mcp aliases) mean the least.
async function loadToolDescriptions() {
  let d;
  try { d = await api("/api/tools"); } catch { return; }
  const by = new Map(d.tools.map((t) => [t.name, t.description]));
  for (const [id, names] of Object.entries(TOOL_TOGGLE_MAP)) {
    const lbl = $("#" + id);
    if (!lbl) continue;
    const desc = names.map((n) => by.get(n)).filter(Boolean);
    if (desc.length && lbl.parentElement) lbl.parentElement.title = desc[0];
  }
  document.querySelectorAll("#cfgToolOther input[data-tool]").forEach((cb) => {
    const desc = by.get(cb.dataset.tool);
    if (!desc) return;
    cb.parentElement.title = desc;
    if (!cb.parentElement.querySelector(".tool-desc")) {
      cb.parentElement.appendChild(el("span", "tool-desc", desc.split(/(?<=\.)\s/)[0].slice(0, 90)));
    }
  });
}

function renderApprovalRules(rules) {
  const wrap = $("#approvalRules");
  if (!wrap) return;
  wrap.replaceChildren();
  if (!rules.length) {
    wrap.appendChild(el("span", "field-hint", "None saved yet. Approve a command with \u201cAlways\u201d in chat to save it here."));
    return;
  }
  for (const r of rules) {
    const row = el("div", "rule-row");
    row.appendChild(el("code", "", r));
    const x = el("button", "rule-x", "\u00d7");
    x.title = "Remove rule";
    x.addEventListener("click", async () => {
      try {
        const out = await api("/api/approvals/remove", { method: "POST", body: JSON.stringify({ rule: r }) });
        renderApprovalRules(out.approvals_allow || []);
        toast("Rule removed", "ok");
      } catch (e) { toast(e.message, "err"); }
    });
    row.appendChild(x);
    wrap.appendChild(row);
  }
}

function updatePlanBadge() {
  const on = !!(state.info && state.info.plan_mode);
  const b = $("#planBadge");
  if (b) b.classList.toggle("hidden", !on);
  const chip = $("#planChip");
  if (chip) chip.classList.toggle("on", on);
}

async function togglePlanMode() {
  const sid = state.sid || "";
  try {
    if (!sid) {
      // no open session yet: flip the global default
      const out = await api("/api/config", { method: "POST", body: JSON.stringify({ plan_mode: !(state.info && state.info.plan_mode) }) });
      state.info = out;
      toast("Plan mode (default for new chats): " + (out.plan_mode ? "ON" : "off"), "ok");
      updatePlanBadge();
      return;
    }
    const out = await api("/api/plan", { method: "POST", body: JSON.stringify({ session_id: sid, on: !(state.info && state.info.plan_mode) }) });
    state.info = Object.assign({}, state.info, { plan_mode: out.plan_mode });
    updatePlanBadge();
    toast(out.plan_mode ? "Plan mode ON - read-only tools, the agent will plan only" : "Plan mode off - full capability", "ok");
  } catch (e) { toast(e.message, "err"); }
}

async function saveSettings() {  const patch = {
    provider: $("#cfgProvider").value,
    model: $("#cfgModel").value.trim(),
    safety_mode: segGet("cfgSafety") || "ask",
    max_steps: parseInt($("#cfgSteps").value, 10),
    temperature: parseFloat($("#cfgTemp").value),
    max_tokens: parseInt($("#cfgMaxTokens").value, 10),
    fallback_models: $("#cfgFallback").value,
    top_p: parseFloat($("#cfgTopP").value),
    request_timeout: parseInt($("#cfgReqTimeout").value, 10),
    tool_timeout: parseInt($("#cfgToolTimeout").value, 10),
    max_retries: parseInt($("#cfgRetries").value, 10),
    memory_max_chars: parseInt($("#cfgMemChars").value, 10),
    stream: $("#cfgStream").checked,
    shell_allow_network: $("#cfgShNet").checked,
    max_context_tokens: $("#cfgMaxCtx").value ? parseInt($("#cfgMaxCtx").value, 10) : null,
    compact_above_tokens: $("#cfgCompactAbove").value ? parseInt($("#cfgCompactAbove").value, 10) : null,
    persona_extra: $("#cfgPersona").value,
    desktop_background_only: $("#cfgBgOnly").checked,
    destructive_guardrails: $("#cfgGuardrails") ? $("#cfgGuardrails").checked : true,
    persona_mode: $("#cfgAssistant") && $("#cfgAssistant").checked ? "assistant" : "agent",
    assistant_name: $("#cfgAssistantName") ? $("#cfgAssistantName").value.trim() : "",
    assistant_user_title: $("#cfgAssistantTitle") ? $("#cfgAssistantTitle").value.trim() : "",
    sandboxed: $("#cfgSandboxed") ? $("#cfgSandboxed").checked : false,
    max_run_tokens: parseInt($("#cfgMaxRunTokens") && $("#cfgMaxRunTokens").value || "0", 10) || 0,
    provenance_marking: $("#cfgProvenance") ? $("#cfgProvenance").value : "metadata",
    verify_command: $("#cfgVerifyCmd") ? $("#cfgVerifyCmd").value.trim() : "",
    keep_reasoning_in_history: !$("#cfgKeepReasoning") || $("#cfgKeepReasoning").checked,
    auto_title_sessions: !$("#cfgAutoTitle") || $("#cfgAutoTitle").checked,
    suggest_followups: !$("#cfgFollowups") || $("#cfgFollowups").checked,
    ...disabledToolsPatch(),
  };
  if ($("#cfgLspServers")) {
    const raw = $("#cfgLspServers").value.trim();
    if (raw) {
      try {
        patch.lsp_servers = JSON.parse(raw);
      } catch {
        const w = $("#settingsWarn");
        w.textContent = "Language servers is not valid JSON";
        w.classList.remove("hidden");
        return;
      }
    } else {
      patch.lsp_servers = {};
    }
  }
  if ($("#cfgHooks")) {
    try {
      patch.hooks = JSON.parse($("#cfgHooks").value || "{}");
    } catch {
      const w = $("#settingsWarn");
      w.textContent = "Hooks is not valid JSON";
      w.classList.remove("hidden");
      return;
    }
  }
  const themeChoice = $("#cfgThemeSel").value || "dark";
  applyTheme(themeChoice === "system" || themeById(themeChoice) ? themeChoice : "dark");
  localStorage.setItem("df_tts_rate", String(parseFloat($("#cfgTtsRate").value || "1.05")));
  localStorage.setItem("df_mic_lang", $("#cfgMicLang").value || "en-US");
  localStorage.setItem("df_voice_autosend", $("#cfgVoiceAutosend").checked ? "1" : "0");
  localStorage.setItem("df_sound", $("#cfgSound").checked ? "1" : "0");
  localStorage.setItem("df_tts", $("#cfgTts").checked ? "1" : "0");
  const wantNotify = $("#cfgNotify").checked;
  localStorage.setItem("df_notify", wantNotify ? "1" : "0");
  if (wantNotify && window.Notification && Notification.permission === "default") {
    try { Notification.requestPermission(); } catch {}
  }
  try {
    const out = await api("/api/config", { method: "POST", body: JSON.stringify(patch) });
    state.info = out;
    renderHeaderPills();
    stageHomeFill();
    applyModeFlavor();
    rememberModel(out.model);
    await saveCommands();
    closeSettings();
    // silent-rejection guard: the backend skips (not errors on) values that
    // fail validation — surface anything it did NOT apply so "Saved" never lies
    const ignored = Object.keys(patch).filter((k) => k !== "hooks" && !(out.applied || []).includes(k));
    if (ignored.length) {
      toast("Saved, but not applied: " + ignored.join(", ") + " (check ranges/format)", "err");
    } else {
      toast("Settings saved", "ok");
    }
    if (out.warnings && out.warnings.length) toast(out.warnings[0], "info");
  } catch (e) {
    const w = $("#settingsWarn");
    w.textContent = e.message;
    w.classList.remove("hidden");
  }
}

async function revealFolder(target) {
  try {
    const out = await api("/api/reveal", { method: "POST", body: JSON.stringify({ target }) });
    toast("Opened " + out.path, "ok");
  } catch (e) { toast(e.message, "err"); }
}

async function exportAllData() {
  try {
    const data = await api("/api/export/all");
    download("saturday-export-" + new Date().toISOString().slice(0, 10) + ".json", JSON.stringify(data, null, 2), "application/json");
    toast("Exported " + (data.exported || 0) + " session(s)", "ok");
  } catch (e) { toast(e.message, "err"); }
}

async function clearAllData() {
  if (!(await uiConfirm({ title: "Delete everything", message: "Delete ALL sessions and projects? This cannot be undone.", ok: "Delete all", danger: true }))) return;
  if (!(await uiConfirm({ title: "Are you sure?", message: "Really sure? Every chat will be permanently removed.", ok: "Delete all", danger: true }))) return;
  try {
    const out = await api("/api/sessions/all", { method: "DELETE" });
    state.pins = [];
    localStorage.setItem("df_pins", "[]");
    state.projPins = [];
    localStorage.setItem("df_projpins", "[]");
    await loadProjects();
    await loadSessions();
    newChat();
    toast("Cleared " + (out.removed || 0) + " session(s)", "ok");
  } catch (e) { toast(e.message, "err"); }
}

function copyDiagnostics() {
  const i = state.info || {};
  const usage = i.usage || {};
  const rate = usage.success_rate != null ? "  success: " + Math.round(usage.success_rate * 100) + "%" : "";
  const cost = usage.est_cost_usd_14d != null ? "  est-$14d: " + Number(usage.est_cost_usd_14d).toFixed(2) : "";
  const text = [
    "Saturday v" + (i.version || "?"),
    "provider: " + (i.provider || "?") + " / " + (i.model || "?"),
    "safety: " + (i.safety_mode || "?") + "  bg-only: " + (!!i.background_only),
    "turns(14d): " + (usage.turns || 0) + rate + cost,
    "platform: " + navigator.platform + "  ua: " + navigator.userAgent.slice(0, 80),
  ].join("\n");
  navigator.clipboard.writeText(text).then(() => toast("Diagnostics copied", "ok"));
}

/* ------------------------------------------------------------- pipelines */

async function loadPipelines() {
  const box = $("#pipeList");
  box.replaceChildren(el("div", "field-hint", "reading\u2026"));
  let d;
  try { d = await api("/api/pipelines"); }
  catch (e) { box.replaceChildren(el("div", "pipe-bad", "could not read: " + e.message)); return; }

  box.replaceChildren();
  if (!d.pipelines.length) {
    box.appendChild(el("div", "field-hint",
      "No pipelines yet. Drop a .json into " + (d.dir || "~/.saturday/pipelines") + "."));
    return;
  }
  for (const p of d.pipelines) {
    const row = el("div", "pipe-item");
    const head = el("div", "pipe-head");
    head.appendChild(el("span", "pipe-dot " + (p.valid ? "ok" : "bad")));
    head.appendChild(el("span", "pipe-name", p.name));
    head.appendChild(el("span", "pipe-meta mono", p.nodes + " nodes, " + (p.edges || 0) + " edges"));
    row.appendChild(head);
    // an invalid graph says why, and offers no run button: starting it would
    // spend real calls on the nodes before the broken wire
    for (const problem of p.problems || []) row.appendChild(el("div", "pipe-bad", problem));
    if (p.valid) {
      const form = el("div", "pipe-run");
      const input = document.createElement("input");
      input.type = "text";
      input.className = "pipe-input";
      input.placeholder = "what should it work on?";
      const go = el("button", "mini-btn", "run");
      const start = async () => {
        const task = input.value.trim();
        if (!task) { input.focus(); return; }
        go.disabled = true;
        try {
          const out = await api("/api/pipelines/run", {
            method: "POST",
            body: JSON.stringify({ name: p.name, input: task, sid: state.sid || "" }),
          });
          toast("Running " + p.name, "ok");
          if (out.session_id && out.session_id !== state.sid) openSession(out.session_id);
          stageShow("activity", false);
        } catch (e) {
          toast("Could not start: " + e.message, "error");
        } finally { go.disabled = false; }
      };
      go.addEventListener("click", start);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); start(); }
        e.stopPropagation();
      });
      form.appendChild(input);
      form.appendChild(go);
      row.appendChild(form);
    }
    box.appendChild(row);
  }
}

/* ---------------------------------------------------------------- update */

async function loadUpdate() {
  const btn = $("#updCheck"), out = $("#updOut");
  btn.disabled = true;
  btn.textContent = "checking\u2026";
  out.replaceChildren();
  let d;
  try { d = await api("/api/update"); }
  catch (e) { d = { error: e.message }; }
  btn.disabled = false;
  btn.textContent = "check for updates";

  if (d.error) {
    out.replaceChildren(el("div", "upd-line warn", d.error));
    return;
  }
  if (!d.newer) {
    out.replaceChildren(el("div", "upd-line ok",
      "Up to date \u2014 " + d.current + " is the latest release."));
    return;
  }
  out.replaceChildren();
  const head = el("div", "upd-line new", d.current + " \u2192 " + d.latest + " available");
  out.appendChild(head);
  // applying replaces the package this server runs from, so the UI hands over
  // the command rather than pulling the floor out from under itself
  const row = el("div", "upd-cmd");
  row.appendChild(el("code", "mono", d.channel === "pip" || d.channel === "pipx"
    ? d.command : d.manual));
  const copy = el("button", "mini-btn", "copy");
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(row.querySelector("code").textContent);
      toast("Copied", "ok");
    } catch { toast("Could not copy", "error"); }
  });
  row.appendChild(copy);
  out.appendChild(row);
  out.appendChild(el("div", "upd-hint",
    "Run this in a terminal. Saturday updates itself in place, so it cannot "
    + "replace the package it is currently serving from."));
  if (d.url) {
    const a2 = document.createElement("a");
    a2.className = "upd-link";
    a2.href = d.url;
    a2.target = "_blank";
    a2.rel = "noopener noreferrer";
    a2.textContent = "release notes";
    out.appendChild(a2);
  }
}

/* ---------------------------------------------------------------- doctor */

async function loadDoctor() {
  const btn = $("#doctorRun"), out = $("#doctorOut");
  btn.disabled = true;
  btn.textContent = "checking\u2026";
  out.replaceChildren(el("div", "field-hint", "probing the provider endpoint\u2026"));
  let d;
  try { d = await api("/api/doctor"); }
  catch (e) {
    out.replaceChildren(el("div", "doctor-row fail", "could not run checks: " + e.message));
    btn.disabled = false; btn.textContent = "run checks";
    return;
  }
  btn.disabled = false; btn.textContent = "run checks";

  out.replaceChildren();
  const head = el("div", "doctor-head " + (d.failures ? "fail" : "ok"),
    d.failures ? d.failures + " problem" + (d.failures === 1 ? "" : "s") + " found"
               : "all checks passed");
  out.appendChild(head);
  for (const c of d.checks) {
    const row = el("div", "doctor-row " + c.status);
    row.appendChild(el("span", "doctor-dot " + c.status));
    row.appendChild(el("span", "doctor-label mono", c.label));
    row.appendChild(el("span", "doctor-detail", c.detail));
    out.appendChild(row);
    // a failure that does not say what to do next is only half a diagnostic
    if (c.hint) out.appendChild(el("div", "doctor-hint", c.hint));
  }
}

/* ----------------------------------------------------------------- audit */

async function loadAudit() {
  const btn = $("#auditRun"), sum = $("#auditSummary"), list = $("#auditList");
  btn.disabled = true;
  const was = btn.textContent;
  btn.textContent = "verifying\u2026";
  sum.replaceChildren();
  list.replaceChildren(el("div", "field-hint", "reading every transcript\u2026"));
  let d;
  try { d = await api("/api/audit"); }
  catch (e) {
    list.replaceChildren(el("div", "audit-bad", "could not verify: " + e.message));
    btn.disabled = false; btn.textContent = was;
    return;
  }
  btn.disabled = false; btn.textContent = was;

  const bad = d.tampered;
  sum.replaceChildren();
  const head = el("div", "audit-head" + (bad ? " bad" : " ok"));
  head.appendChild(el("span", "audit-badge", bad ? "\u2715" : "\u2713"));
  head.appendChild(el("span", "", bad
    ? bad + " of " + d.checked + " session" + (d.checked === 1 ? "" : "s") + " no longer match their chain"
    : "all " + d.checked + " session" + (d.checked === 1 ? "" : "s") + " verified intact"));
  sum.appendChild(head);
  if (d.truncated) sum.appendChild(el("div", "field-hint", "showing the newest 400"));

  list.replaceChildren();
  // a clean run is a single line; only the broken ones are worth enumerating
  const rows = d.sessions.filter((s) => s.ok === false).concat(d.sessions.filter((s) => s.ok === null));
  for (const s of rows) {
    const row = el("div", "audit-row");
    row.appendChild(el("span", "audit-dot " + (s.ok === false ? "bad" : "unknown")));
    row.appendChild(el("span", "audit-txt", s.task || s.id));
    row.appendChild(el("span", "audit-meta mono", s.ok === false
      ? "record " + s.broken_at + " of " + s.records
      : "not verifiable"));
    const dl = el("button", "mini-btn", "bundle");
    dl.title = "Download the audit bundle for this session";
    dl.addEventListener("click", () => auditBundle(s.id));
    row.appendChild(dl);
    list.appendChild(row);
  }
}

function auditBundle(sid) {
  // same-origin navigation so the token cookie rides along and the server's
  // Content-Disposition drives the save
  window.location.href = "/api/audit?export=1&sid=" + encodeURIComponent(sid);
}

/* ------------------------------------------------------------------- MCP */

async function loadMcp(probe) {
  const box = $("#cfgMcp");
  const warn = $("#mcpWarn");
  const btn = $("#mcpTest");
  box.replaceChildren(el("span", "field-hint", probe ? "starting each server\u2026" : "reading config\u2026"));
  if (btn) { btn.disabled = true; btn.textContent = probe ? "testing\u2026" : "test all"; }
  let d;
  try {
    d = await api("/api/mcp?sid=" + encodeURIComponent(state.sid || "") + (probe ? "&probe=1" : ""));
  } catch (e) {
    box.replaceChildren(el("span", "field-hint", "could not read MCP config: " + e.message));
    if (btn) { btn.disabled = false; btn.textContent = "test all"; }
    return;
  }
  if (btn) { btn.disabled = false; btn.textContent = "test all"; }

  warn.replaceChildren();
  warn.classList.toggle("hidden", !(d.warnings || []).length);
  for (const w of d.warnings || []) warn.appendChild(el("div", "", w));

  box.replaceChildren();
  if (!d.servers.length) {
    box.appendChild(el("span", "field-hint",
      "None configured yet. Add .saturday/mcp.json in this folder with " +
      '{"servers": {"name": {"command": "npx", "args": ["-y", "some-mcp-server"]}}}'));
    return;
  }
  for (const srv of d.servers) {
    const row = el("div", "mcp-item");
    const head = el("div", "mcp-head");
    // dot meanings: grey untested, green serving, red failed, amber blocked
    head.appendChild(el("span", "mcp-dot " + srv.status));
    head.appendChild(el("span", "mcp-name", srv.alias));
    head.appendChild(el("span", "mcp-tag", srv.source));
    head.appendChild(el("span", "mcp-tag", srv.transport));
    if (srv.status === "ok") {
      head.appendChild(el("span", "mcp-count",
        srv.tools.length + " tool" + (srv.tools.length === 1 ? "" : "s")));
    }
    row.appendChild(head);
    row.appendChild(el("div", "mcp-cmd mono", srv.command));
    if (srv.error) row.appendChild(el("div", "mcp-err", srv.error));
    if (srv.server_name) {
      row.appendChild(el("div", "mcp-cmd", srv.server_name + " " + (srv.server_version || "")));
    }
    if (srv.tools && srv.tools.length) {
      const tools = el("div", "mcp-tools");
      for (const t of srv.tools) {
        const ti = el("div", "mcp-tool");
        ti.appendChild(el("span", "mcp-tool-name mono", t.name));
        if (t.description) ti.appendChild(el("span", "mcp-tool-desc", t.description));
        tools.appendChild(ti);
      }
      row.appendChild(tools);
    }
    box.appendChild(row);
  }
}

/* ---------------------------------------------------------------- folder picker */

const folderState = { path: "", busy: false };

async function folderOpen(start) {
  $("#folderModal").classList.remove("hidden");
  await folderGo(start || folderState.path || "");
}
function folderClose() { $("#folderModal").classList.add("hidden"); }

async function folderGo(path) {
  const list = $("#folderList");
  list.replaceChildren(el("div", "folder-empty", "reading\u2026"));
  let d;
  try { d = await api("/api/browse?path=" + encodeURIComponent(path || "")); }
  catch (e) {
    list.replaceChildren(el("div", "folder-empty", "could not read that folder: " + e.message));
    return;
  }
  folderState.path = d.path;
  $("#folderHere").textContent = d.path;
  $("#folderHere").title = d.path;
  $("#folderUp").disabled = !d.parent;

  const crumbs = $("#folderCrumbs");
  crumbs.replaceChildren();
  d.crumbs.forEach((c, i) => {
    // the root crumb IS a slash, so a separator after it reads as "//"
    if (i && d.crumbs[i - 1].name !== "/") crumbs.appendChild(el("span", "folder-sep", "/"));
    const b = el("button", "folder-crumb", c.name);
    b.addEventListener("click", () => folderGo(c.path));
    crumbs.appendChild(b);
  });

  list.replaceChildren();
  if (d.denied) {
    list.appendChild(el("div", "folder-empty", "no permission to read this folder"));
    return;
  }
  if (!d.dirs.length) {
    list.appendChild(el("div", "folder-empty", "no folders in here \u2014 open it with the button below"));
    return;
  }
  for (const dir of d.dirs) {
    const row = el("button", "folder-row");
    row.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 5h5.6l2 2H20a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z"/></svg>';
    row.appendChild(el("span", "", dir.name));
    // a git repo is almost always the folder someone means, so say so
    if (dir.repo) row.appendChild(el("span", "repo-tag", "repo"));
    row.addEventListener("click", () => folderGo(dir.path));
    list.appendChild(row);
  }
}

async function folderUse() {
  const path = folderState.path;
  if (!path || folderState.busy) return;
  folderState.busy = true;
  try {
    // reuse an existing project for this folder rather than making a second one
    const known = state.projects.find((p) => p.workspace === path);
    if (known) { folderClose(); selectProject(known.id); return; }
    const name = path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;
    const out = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name, workspace: path }),
    });
    state.projects = out.projects || state.projects;
    folderClose();
    renderSessions();
    selectProject(out.project.id);
    toast("Opened " + name, "ok");
  } catch (e) {
    toast("Could not open that folder: " + e.message, "error");
  } finally {
    folderState.busy = false;
  }
}

/* ---------------------------------------------------------------- onboarding */

function maybeOnboard() {
  const info = state.info;
  if (!info) return;
  if (sessionStorage.getItem("df_onboard_skip") === "1") return;
  if (info.has_key || isLocalProvider(info.provider)) return;
  onboardOpen();
}

function obProviderChanged() {
  const info = state.info || {};
  const sel = $("#obProvider");
  const p = (info.providers || []).find((x) => x.name === sel.value) || null;
  const local = isLocalProvider(sel.value);
  $("#obKey").disabled = local;
  $("#obKeyLabel").textContent = local ? "API key (not needed)" : "API key";
  $("#obKey").placeholder = local ? "no key needed" : "sk-...";
  if (!local) $("#obKey").focus();
  $("#obModel").placeholder = (p && p.default_model) || "provider default";
  $("#obModelHint").textContent = local ? "Uses your local endpoint \u00b7 start the server before chatting" : "";
  const dl = $("#obModels");
  dl.replaceChildren();
}

function onboardOpen() {
  const sel = $("#obProvider");
  sel.replaceChildren();
  const cloud = document.createElement("optgroup");
  cloud.label = "Cloud providers";
  const local = document.createElement("optgroup");
  local.label = "Local (no key needed)";
  for (const p of state.info.providers || []) {
    const o = el("option", "", p.name + (p.has_key ? "  [key detected]" : ""));
    o.value = p.name;
    (isLocalProvider(p.name) ? local : cloud).appendChild(o);
  }
  sel.appendChild(cloud);
  sel.appendChild(local);
  const cur = state.info && state.info.provider;
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
  $("#obKey").value = "";
  $("#obModel").value = "";
  obProviderChanged();
  $("#obWarn").classList.add("hidden");
  const st = $("#obStatus");
  st.classList.add("hidden");
  st.classList.remove("ok", "err");
  st.textContent = "";
  $("#onboardModal").classList.remove("hidden");
}

async function onboardSave() {
  const payload = {
    provider: $("#obProvider").value,
    api_key: $("#obKey").value.trim(),
    model: $("#obModel").value.trim(),
  };
  if (!isLocalProvider(payload.provider) && !payload.api_key) {
    const w = $("#obWarn");
    w.textContent = "paste an API key first";
    w.classList.remove("hidden");
    return;
  }
  const btn = $("#obSave");
  const st = $("#obStatus");
  btn.disabled = true;
  st.classList.remove("hidden", "ok", "err");
  st.textContent = "Testing connection\u2026";
  try {
    const out = await api("/api/onboard", { method: "POST", body: JSON.stringify(payload) });
    if (out.ok === false) throw new Error(out.error || "connection test failed");
    const dl = $("#obModels");
    dl.replaceChildren();
    for (const m of out.models || []) {
      const o = el("option", "", m);
      o.value = m;
      dl.appendChild(o);
    }
    state.info = out;
    renderHeaderPills();
    stageHomeFill();
    sessionStorage.removeItem("df_onboard_skip");
    rememberModel(out.model);
    closeOnboard();
    toast("Connected to " + out.provider + " \u00b7 " + out.model, "ok");
  } catch (e) {
    st.textContent = e.message;
    st.classList.add("err");
  } finally {
    btn.disabled = false;
  }
}

async function cfgTestConnection() {
  const btn = $("#cfgTest");
  const st = $("#cfgTestStatus");
  btn.disabled = true;
  st.classList.remove("ok", "err");
  st.textContent = "testing\u2026";
  try {
    const out = await api("/api/onboard", {
      method: "POST",
      body: JSON.stringify({ provider: $("#cfgProvider").value, api_key: "", model: $("#cfgModel").value || "" }),
    });
    if (out.ok === false) throw new Error(out.error || "connection test failed");
    const dl = $("#cfgModels");
    dl.replaceChildren();
    for (const m of out.models || []) {
      const o = el("option", "", m);
      o.value = m;
      dl.appendChild(o);
    }
    st.textContent = "\u2713 " + (out.probe || "reachable");
    st.classList.add("ok");
  } catch (e) {
    st.textContent = e.message;
    st.classList.add("err");
  } finally {
    btn.disabled = false;
  }
}

function closeOnboard() { $("#onboardModal").classList.add("hidden"); }

/* ---------------------------------------------------------------- trust */

function showTrustModal() {
  return new Promise((resolve) => {
    // Fetch pending items to populate the modal body.
    api("/api/trust").then((data) => {
      const body = $("#trustBody");
      const ws = data.workspace || "this folder";
      let html = `<p class="trust-workspace"><strong>${escHtml(ws)}</strong></p>`;
      html += `<p class="trust-desc">This folder contains project configuration files that could redirect API traffic or run local commands. Trust only folders you control.</p>`;
      if (data.pending && data.pending.length) {
        html += `<ul class="trust-items">`;
        for (const item of data.pending) {
          const label = item.kind === "env" ? ".env"
            : item.kind === "hooks" ? ".saturday/hooks.json"
            : ".saturday/mcp.json";
          html += `<li class="trust-item"><span class="trust-item-kind">${escHtml(label)}</span><span class="trust-item-path">${escHtml(item.path)}</span>`;
          if (item.detail && item.detail.length) {
            html += `<ul class="trust-detail">`;
            for (const d of item.detail) html += `<li>${escHtml(d)}</li>`;
            html += `</ul>`;
          }
          html += `</li>`;
        }
        html += `</ul>`;
      }
      body.innerHTML = html;
    }).catch(() => {
      $("#trustBody").innerHTML = `<p class="trust-desc">Unable to load project details. You can still choose whether to trust this folder.</p>`;
    });

    $("#trustModal").classList.remove("hidden");
    $("#trustApproveBtn").focus();

    const finish = async (decision) => {
      $("#trustApproveBtn").disabled = true;
      $("#trustDenyBtn").disabled = true;
      try {
        await api("/api/trust", { method: "POST", body: JSON.stringify({ decision }) });
      } catch (e) {
        toast("Trust decision could not be saved: " + e.message, "err");
      }
      $("#trustModal").classList.add("hidden");
      $("#trustApproveBtn").disabled = false;
      $("#trustDenyBtn").disabled = false;
      resolve(decision);
    };

    $("#trustApproveBtn").onclick = () => finish("trust");
    $("#trustDenyBtn").onclick   = () => finish("deny");
  });
}

function renderUsage(usage) {
  const wrap = $("#usageDays");
  if (!wrap) return;
  wrap.replaceChildren();
  const days = usage.days || [];
  const max = Math.max(1, ...days.map((d) => d.tokens || 0));
  for (let i = DAYS_BACK - 1; i >= 0; i--) {
    const day = new Date(Date.now() - i * 86_400_000);
    const key = day.toISOString().slice(0, 10);
    const found = days.find((d) => d.day === key);
    const bar = el("div", "usage-day" + (found ? "" : " empty"));
    const h = found ? Math.max(4, Math.round(42 * (found.tokens || 0) / max)) : 2;
    bar.style.height = h + "px";
    bar.title = key + ": " + (found ? found.tokens.toLocaleString() + " tokens" : "no turns");
    wrap.appendChild(bar);
  }
  const modelsTxt = (usage.models || []).map((m) => m.model + " " + fmtK(m.tokens)).join(" \u00b7 ");
  const costTxt = usage.est_cost_usd_14d != null ? " \u00b7 ~$" + Number(usage.est_cost_usd_14d).toFixed(2) + " est. (14d list price)" : "";
  $("#usageTotals").textContent =
    (usage.turns || 0) + " turns \u00b7 " + (usage.total_tokens || 0).toLocaleString() + " tokens" + (modelsTxt ? " — " + modelsTxt : "") + costTxt;
  const mEl = $("#usageMetrics");
  if (mEl) {
    if (!usage.turns) { mEl.textContent = ""; }
    else {
      const rate = usage.success_rate != null ? Math.round(usage.success_rate * 100) + "% completed" : "";
      const avg = usage.avg_tokens_per_turn ? " \u00b7 ~" + usage.avg_tokens_per_turn.toLocaleString() + " tokens/turn" : "";
      const stops = Object.entries(usage.stop_reasons || {}).map(([k, v]) => k + " " + v).join(", ");
      mEl.textContent = [rate + avg, stops ? "outcomes: " + stops : ""].filter(Boolean).join(" \u00b7 ");
    }
  }
}

const DAYS_BACK = 14;

/* ---------------------------------------------------------------- projects */

/* ------------------------------------------------------- schedules + commands */

async function loadSchedules() {
  const box = $("#schedList");
  if (!box) return;
  box.replaceChildren(el("div", "sched-none", "loading\u2026"));
  let data;
  try { data = await api("/api/schedules"); }
  catch (e) { box.replaceChildren(el("div", "sched-none", "unavailable: " + e.message)); return; }
  box.replaceChildren();
  const rows = data.schedules || [];
  if (!rows.length) box.appendChild(el("div", "sched-none", "no schedules yet"));
  for (const s of rows) {
    const row = el("div", "sched-row");
    row.appendChild(el("span", "sched-expr mono", s.expr));
    const task = el("span", "sched-task", s.task);
    task.title = s.task;
    row.appendChild(task);
    if (s.model) row.appendChild(el("span", "sched-meta", s.model));
    if (s.last_fired_minute) row.appendChild(el("span", "sched-meta", "last " + s.last_fired_minute));
    const rm = el("button", "icon-btn sm", "\u00d7");
    rm.title = "Remove schedule";
    rm.addEventListener("click", async () => {
      try {
        await api("/api/schedules", { method: "POST", body: JSON.stringify({ action: "remove", id: s.id }) });
        loadSchedules();
        toast("Schedule removed", "ok");
      } catch (e) { toast(e.message, "err"); }
    });
    row.appendChild(rm);
    box.appendChild(row);
  }
  $("#schedHint").textContent = data.watcher
    ? "In-app watcher is running: due schedules fire as one-shot agent runs while the app is open."
    : "In-app watcher is OFF (SATURDAY_SCHEDULE_WATCHER=0 or no schedules yet); the CLI's `saturday schedule watch` covers always-on use.";
}

async function addSchedule() {
  const expr = $("#schedExpr").value.trim();
  const task = $("#schedTask").value.trim();
  if (!task) { toast("Describe what the schedule should run", "info"); return; }
  try {
    await api("/api/schedules", { method: "POST", body: JSON.stringify({ action: "add", expr, task }) });
    $("#schedExpr").value = "";
    $("#schedTask").value = "";
    loadSchedules();
    toast("Schedule added", "ok");
  } catch (e) { toast(e.message, "err"); }
}

let cmdDraft = null; // null = clean, {} = editing in progress

async function loadCommands() {
  const box = $("#cmdList");
  if (!box) return;
  if (cmdDraft == null) cmdDraft = Object.assign({}, (state.info && state.info.custom_commands) || {});
  renderCommands();
}

function renderCommands() {
  const box = $("#cmdList");
  box.replaceChildren();
  const names = Object.keys(cmdDraft).sort();
  if (!names.length) box.appendChild(el("div", "cmd-none", "no custom commands yet"));
  for (const name of names) {
    const v = cmdDraft[name];
    const row = el("div", "cmd-row");
    const head = el("div", "cmd-row-head");
    head.appendChild(el("span", "cmd-name mono", "/" + name));
    const desc = el("input", "cmd-desc");
    desc.placeholder = "description (shows in / autocomplete)";
    desc.value = v.description || "";
    desc.addEventListener("input", () => { cmdDraft[name].description = desc.value; });
    head.appendChild(desc);
    const rm = el("button", "icon-btn sm", "\u00d7");
    rm.title = "Remove command";
    rm.addEventListener("click", () => { delete cmdDraft[name]; renderCommands(); });
    head.appendChild(rm);
    row.appendChild(head);
    const ta = el("textarea", "cmd-prompt");
    ta.rows = 2;
    ta.spellcheck = false;
    ta.placeholder = "prompt template\u2026 $ARGS inserts the text typed after /" + name;
    ta.value = v.prompt || "";
    ta.addEventListener("input", () => { cmdDraft[name].prompt = ta.value; });
    row.appendChild(ta);
    box.appendChild(row);
  }
}

async function saveCommands() {
  if (cmdDraft == null) return;
  const cleaned = {};
  for (const [name, v] of Object.entries(cmdDraft)) {
    if (v && String(v.prompt || "").trim()) cleaned[name] = { prompt: String(v.prompt).trim(), description: v.description || "" };
  }
  try {
    const out = await api("/api/commands", { method: "POST", body: JSON.stringify({ commands: cleaned }) });
    cmdDraft = Object.assign({}, out.commands || {});
    if (state.info) state.info.custom_commands = cmdDraft;
    renderCommands();
    toast("Commands saved", "ok");
  } catch (e) { toast(e.message, "err"); }
}

/* ------------------------------------------------------------- export as HTML */

function exportHtml() {
  if (!state.sid || !thread.children.length) { toast("Nothing to export yet", "info"); return; }
  const title = $("#sessionTitle").textContent || "Saturday session";
  const clone = thread.cloneNode(true);
  clone.querySelectorAll(".msg-actions, .appr-actions, .copybtn, .fb-row, .retry-link, .appr-key, .ttlbar, img").forEach((n) => n.remove());
  const meta = "Exported from Saturday \u00b7 " + new Date().toLocaleString() + " \u00b7 session " + state.sid;
  const html = "<!doctype html>\n<html><head><meta charset=\"utf-8\"><title>" + escHtml(title) + "</title><style>" + EXPORT_CSS + "</style></head><body>" +
    "<header><h1>" + escHtml(title) + "</h1><p>" + escHtml(meta) + "</p></header>" +
    "<main>" + clone.innerHTML + "</main></body></html>";
  download(title.replace(/[^\w\- ]+/g, "").trim().replace(/\s+/g, "-").toLowerCase() + ".html", html, "text/html");
}

const EXPORT_CSS = `
  body{margin:0;font:15px/1.6 -apple-system,"Segoe UI",system-ui,sans-serif;color:#e9e4d8;background:#101013;padding:32px 16px}
  header{max-width:820px;margin:0 auto 24px;border-bottom:1px solid #2a2b30;padding-bottom:14px}
  header h1{margin:0 0 6px;font-size:20px}
  header p{margin:0;color:#a29c8f;font-size:13px}
  main{max-width:820px;margin:0 auto}
  .turn{margin:0 0 26px}
  .msg-user{display:flex;justify-content:flex-end;margin-bottom:12px}
  .user-bubble{background:#1f2024;border:1px solid #2a2b30;border-radius:12px;padding:9px 13px;max-width:80%;white-space:pre-wrap;overflow-wrap:anywhere}
  .assistant-row{display:flex;gap:10px}
  .avatar{width:26px;height:26px;flex:0 0 26px;border:1px solid #2a2b30;border-radius:8px;display:flex;align-items:center;justify-content:center}
  .avatar svg{width:14px;height:14px;fill:#00d4ff}
  .assistant-body{min-width:0;flex:1}
  .md p{margin:.5em 0}.md pre{background:#0c0d10;border:1px solid #2a2b30;border-radius:8px;padding:10px 12px;overflow:auto;font:12.5px/1.5 ui-monospace,Consolas,monospace}
  .md code{font-family:ui-monospace,Consolas,monospace}
  .md code.inline{background:#1f2024;border-radius:4px;padding:1px 5px}
  .md table{border-collapse:collapse}.md th,.md td{border:1px solid #2a2b30;padding:4px 10px}
  .md blockquote{border-left:3px solid #2a2b30;margin:.5em 0;padding:2px 12px;color:#a29c8f}
  .tool-card{border:1px solid #2a2b30;border-radius:8px;margin:8px 0;overflow:hidden}
  .tool-head{display:flex;gap:8px;align-items:center;padding:6px 10px;background:#141417;font:12px ui-monospace,monospace}
  .tool-args{color:#a29c8f;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
  .tool-body{display:none;padding:8px 10px}.tool-card.open .tool-body{display:block}
  .tool-pre{margin:4px 0;white-space:pre-wrap;font:12px/1.5 ui-monospace,monospace;color:#c9c4b8}
  .tool-label{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#6e6a5f}
  .think{border:1px solid #2a2b30;border-radius:8px;margin:8px 0}
  .think summary{cursor:pointer;padding:6px 10px;font-size:13px;color:#a29c8f}
  .think-body{padding:0 12px 10px;white-space:pre-wrap;color:#a29c8f;font-size:13.5px}
  .turn-stats{color:#6e6a5f;font-size:12px;margin-top:6px}
  .notice{border:1px solid #2a2b30;border-radius:8px;padding:7px 11px;color:#a29c8f;font-size:13px;margin:8px 0;white-space:pre-wrap}
  .sysline{color:#6e6a5f;font-size:12.5px;margin:6px 0;white-space:pre-wrap}
  .sysline.error{color:#e05252}.sysline.warn{color:#d9a441}
  .dl{font:12px/1.5 ui-monospace,monospace;white-space:pre-wrap}.dl.add{color:#7fb069}.dl.del{color:#e05252}
  .user-nimg,.user-thumbs{color:#6e6a5f;font-size:12px}
`;

/* ==================================================== round 2: runs monitor */

let runsLoadSeq = 0;
async function runsLoad() {
  const pane = stagePanes.runs;
  if (!pane) return;
  const list = stageEnsureHead(pane, "Runs");
  // sequence guard: the tab poller, the detached watcher and stop-button
  // refreshes all hit /api/runs — a slow older response must never render
  // over a newer one
  const seq = ++runsLoadSeq;
  let data;
  try { data = await api("/api/runs"); } catch { return; }
  if (seq !== runsLoadSeq) return;
  const rows = (data.runs || []).sort((a, b) => (b.busy ? 1 : 0) - (a.busy ? 1 : 0) || (b.mtime || 0) - (a.mtime || 0));
  stageBadge("runs", rows.filter((r) => r.busy).length);
  list.replaceChildren();
  if (!rows.length) {
    list.appendChild(el("div", "runs-empty", "no sessions yet"));
    return;
  }
  for (const r of rows) {
    const row = el("div", "runs-row" + (r.busy ? " live" : "") + (r.id === state.sid ? " cur" : ""));
    const dot = el("span", "runs-dot" + (r.busy ? " run" : "") + (r.stopping ? " stop" : ""));
    dot.title = r.busy ? (r.stopping ? "stopping…" : "running") : "idle";
    row.appendChild(dot);
    const main = el("div", "runs-main");
    const title = el("div", "runs-title", r.task || r.id);
    title.title = r.id;
    main.appendChild(title);
    const meta = [];
    if (r.model) meta.push(r.model);
    if (r.project) meta.push("project");
    if (r.busy && r.started_at) meta.push("up " + fmtElapsed(Date.now() - r.started_at * 1000));
    else if (r.mtime) meta.push(relTime(String(r.id)));
    if (r.archived) meta.push("archived");
    main.appendChild(el("div", "runs-meta", meta.join(" \u00b7 ")));
    row.appendChild(main);
    const open = el("button", "st-btn", "open");
    open.title = "Open this session";
    open.addEventListener("click", () => openSession(r.id));
    row.appendChild(open);
    if (r.busy) {
      const stop = el("button", "st-btn", "stop");
      stop.title = "Stop this run";
      stop.addEventListener("click", async () => {
        try { await api("/api/stop", { method: "POST", body: JSON.stringify({ session_id: r.id }) }); toast("Stop requested", "ok"); }
        catch (e) { toast(e.message, "err"); }
        setTimeout(runsLoad, 400);
      });
      row.appendChild(stop);
    }
    list.appendChild(row);
  }
}
setInterval(() => { if (stage.tab === "runs") runsLoad(); }, 4000);

/* ------------------------------------------------- one-click error fix (v0) */

function fixThisError(name, args, errText) {
  const target = (args && (args.path || args.command || args.code || "")) || "";
  const msg = "Fix this error from " + name + (target ? " (" + String(target).slice(0, 120) + ")" : "") + ":\n```\n" + String(errText).slice(0, 1200) + "\n```";
  const input = $("#input");
  input.value = msg;
  autoGrow(input);
  updateSendEnabled();
  send();
}

/* --------------------------------------------------- plan approval (Replit) */

async function approvePlan() {
  if (!state.sid) { toast("No session", "info"); return; }
  try {
    const out = await api("/api/plan", { method: "POST", body: JSON.stringify({ session_id: state.sid, on: false }) });
    state.info = Object.assign({}, state.info, { plan_mode: out.plan_mode });
    updatePlanBadge();
    toast("Plan approved \u2014 Act mode on", "ok");
  } catch (e) { toast(e.message, "err"); }
}

/* ------------------------------------------------------------ git status chip */

async function refreshGitChip(toastOnFail) {
  const chip = document.querySelector("#stageChanges .git-chip");
  if (!chip) return;
  if (!state.sid) { chip.classList.add("hidden"); return; }
  try {
    const g = await api("/api/git/status?sid=" + encodeURIComponent(state.sid));
    if (!g.available) {
      chip.classList.add("hidden");
      if (toastOnFail) toast("Not a git repository", "info");
      return;
    }
    chip.classList.remove("hidden");
    chip.textContent = "\u2394 " + g.branch + " \u00b7 " + g.changed + " changed +" + g.adds + " \u2212" + g.dels;
    chip.title = "git working tree (read-only)\n" + (g.files || []).slice(0, 20).join("\n") + "\nclick to refresh";
  } catch (e) {
    chip.classList.add("hidden");
    if (toastOnFail) toast(e.message, "err");
  }
}

/* ------------------------------------------------------------- archive flows */

async function archiveSession(sid, flag) {
  if (!sid) { toast("No session", "info"); return; }
  if (flag && !(await uiConfirm({ title: "Archive session", message: "Archive this session? It stays on disk, hidden from the sidebar until you show archived chats.", ok: "Archive" }))) return;
  try {
    await api("/api/archive", { method: "POST", body: JSON.stringify({ session_id: sid, archived: flag }) });
    toast(flag ? "Session archived" : "Session restored", "ok");
    if (flag && sid === state.sid) newChat();
    await loadSessions();
  } catch (e) { toast(e.message, "err"); }
}

function renderArchToggle() {
  const b = $("#archToggle");
  if (!b) return;
  const n = state.sessions.filter((s) => s.archived).length;
  b.classList.toggle("hidden", !n);
  b.textContent = n ? (state.showArchived ? "hide archived (" + n + ")" : "show archived (" + n + ")") : "";
}

/* ----------------------------------------------------------- journal compare */

let cmpEntry = null;

function miniUnifiedDiff(aText, bText) {
  const a = String(aText || "").split("\n");
  const b = String(bText || "").split("\n");
  const n = a.length, m = b.length;
  if (n * m > 4000000) return ["--- journal snapshot", "+++ current", "@@ files too large to diff inline \u2014 restore and re-check instead @@"].join("\n");
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out = ["--- journaled (before)", "+++ current (after)"];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push(" " + a[i]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push("-" + a[i]); i++; }
    else { out.push("+" + b[j]); j++; }
  }
  while (i < n) { out.push("-" + a[i]); i++; }
  while (j < m) { out.push("+" + b[j]); j++; }
  return out.join("\n");
}

async function openCompare(idx, path) {
  let data;
  try { data = await api("/api/journal?sid=" + encodeURIComponent(state.sid || "") + "&entry=" + idx); }
  catch (e) { toast(e.message, "err"); return; }
  const ent = data.entry || {};
  const before = String(ent.before || "");
  let current = "(file missing \u2014 it may have been deleted)";
  try {
    const cur = await api("/api/wsfile?sid=" + encodeURIComponent(state.sid || "") + "&path=" + encodeURIComponent(ent.path || path));
    current = cur.content || "";
  } catch {}
  cmpEntry = { index: idx, path: ent.path || path };
  $("#cmpPath").textContent = cmpEntry.path;
  const body = $("#cmpDiff");
  body.replaceChildren();
  appendDiffLines(body, miniUnifiedDiff(before, current), false);
  $("#cmpModal").classList.remove("hidden");
}
function closeCompare() { $("#cmpModal").classList.add("hidden"); cmpEntry = null; }

async function compareRestore() {
  if (!cmpEntry) return;
  if (!(await uiConfirm({ title: "Restore this version", message: "Restore \"" + cmpEntry.path + "\" to the journaled (pre-edit) state?", ok: "Restore", danger: true }))) return;
  try {
    const r = await api("/api/journal/restore", { method: "POST", body: JSON.stringify({ session_id: state.sid, index: cmpEntry.index }) });
    toast(r.message || (r.ok ? "restored" : "restore failed"), r.ok ? "ok" : "err");
    if (r.ok) { closeCompare(); renderJournal(); }
  } catch (e) { toast(e.message, "err"); }
}

/* ----------------------------------------------------------- model favorites */

function favModels() { return JSON.parse(localStorage.getItem("df_favmodels") || "[]"); }

function cycleFavoriteModel() {
  const favs = favModels();
  if (!favs.length) { toast("No favorite models \u2014 star one in the model menu (Ctrl+M)", "info"); return; }
  const cur = (state.info && state.info.model) || "";
  const next = favs[(favs.indexOf(cur) + 1) % favs.length];
  (async () => {
    try {
      const out = await api("/api/config", { method: "POST", body: JSON.stringify({ model: next }) });
      state.info = out;
      renderHeaderPills();
      rememberModel(out.model);
      toast("Model: " + out.model, "ok");
    } catch (e) { toast(e.message, "err"); }
  })();
}

/* ---------------------------------------------------------------- live preview */

function previewEnsureUrlBar() {
  const pane = stagePanes.preview;
  if (!pane || pane.querySelector(".live-bar")) return;
  const bar = el("div", "live-bar");
  const inp = el("input", "live-url");
  inp.type = "text";
  inp.spellcheck = false;
  inp.placeholder = "live app URL (e.g. http://localhost:3000)";
  const go = el("button", "st-btn", "load");
  const clear = el("button", "st-btn", "\u00d7");
  clear.title = "Close the live app preview";
  const frame = document.createElement("iframe");
  frame.className = "live-frame hidden";
  frame.setAttribute("sandbox", "allow-scripts allow-same-origin allow-forms allow-popups");
  const shotWrap = () => pane.querySelector(".preview-wrap");
  const load = () => {
    let u = inp.value.trim();
    if (!u) return;
    if (!/^https?:\/\//i.test(u)) u = "http://" + u;
    inp.value = u;
    localStorage.setItem("df_liveurl_" + (state.sid || ""), u);
    frame.src = u;
    frame.classList.remove("hidden");
    const w = shotWrap();
    if (w) w.classList.add("hidden");
  };
  const clearAll = () => {
    frame.src = "about:blank";
    frame.classList.add("hidden");
    const w = shotWrap();
    if (w) w.classList.remove("hidden");
    localStorage.removeItem("df_liveurl_" + (state.sid || ""));
  };
  go.addEventListener("click", load);
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); load(); } e.stopPropagation(); });
  clear.addEventListener("click", clearAll);
  bar.append(inp, go, clear);
  pane.insertBefore(bar, pane.firstChild);
  pane.appendChild(frame);
  const saved = localStorage.getItem("df_liveurl_" + (state.sid || ""));
  if (saved) { inp.value = saved; load(); }
}

/* ------------------------------------------------- open a workspace file raw */

async function openWsFile(path) {
  try {
    const data = await api("/api/wsfile?sid=" + encodeURIComponent(state.sid || "") + "&path=" + encodeURIComponent(path));
    const w = window.open("", "_blank");
    if (!w) { toast("Popup blocked", "info"); return; }
    w.document.write("<pre style='font:12.5px/1.5 ui-monospace,Consolas,monospace;padding:16px;white-space:pre-wrap;margin:0'>" + escHtml(data.content || "") + "</pre>");
    w.document.title = path;
    w.document.close();
  } catch (e) { toast("open failed: " + e.message, "err"); }
}

/* ---------------------------------------------------------------- projects */

let projEditing = null;
let projFilesDraft = [];
let projColorDraft = "";
state.projPins = JSON.parse(localStorage.getItem("df_projpins") || "[]");

function projPinned(id) { return state.projPins.includes(id); }
function toggleProjPin(id) {
  const i = state.projPins.indexOf(id);
  if (i >= 0) state.projPins.splice(i, 1);
  else state.projPins.push(id);
  localStorage.setItem("df_projpins", JSON.stringify(state.projPins));
  renderSessions();
}

function paintSwatches() {
  for (const b of document.querySelectorAll("#projColors .swatch")) b.classList.toggle("on", b.dataset.c === projColorDraft);
}
function renderKfiles() {
  const box = $("#projFiles");
  box.replaceChildren();
  projFilesDraft.forEach((fp, i) => {
    const chip = el("div", "kfile-chip");
    chip.appendChild(el("span", "", fp.replace(/^.*[\\/]/, "")));
    chip.title = fp;
    const x = el("button", "", "\u00d7");
    x.title = "remove";
    x.addEventListener("click", () => { projFilesDraft.splice(i, 1); renderKfiles(); });
    chip.appendChild(x);
    box.appendChild(chip);
  });
}

function updateProjChip() {
  const chip = $("#projChip");
  const p = curProject();
  chip.classList.toggle("hidden", !p);
  if (p) $("#projChipName").textContent = p.name;
}

function parseScopeList(v) {
  return (v || "").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean).slice(0, 40);
}
function scopeListText(arr) { return (arr || []).join(", "); }

function openProjModal(p) {
  projEditing = p ? p.id : null;
  $("#projTitleH").textContent = p ? "Project settings" : "New project";
  $("#projName").value = p ? p.name : "";
  $("#projWs").value = p ? p.workspace : "";
  $("#projInstr").value = p ? p.instructions : "";
  projColorDraft = p ? p.color || "" : "";
  paintSwatches();
  projFilesDraft = p ? [...(p.files || [])] : [];
  renderKfiles();
  $("#projFileInput").value = "";
  const sc = (p && p.scopes) || {};
  $("#projScopesReserved").value = scopeListText(sc.reserved);
  $("#projScopesApproval").value = scopeListText(sc.approval);
  $("#projScopesAutonomous").value = scopeListText(sc.autonomous);
  $("#projDelete").classList.toggle("hidden", !p);
  $("#projDup").classList.toggle("hidden", !p);
  $("#projWarn").classList.add("hidden");
  $("#projModal").classList.remove("hidden");
  setTimeout(() => $("#projName").focus(), 30);
}
function closeProjModal() { $("#projModal").classList.add("hidden"); projEditing = null; }

async function saveProj() {
  const scopes = {};
  const reserved = parseScopeList($("#projScopesReserved").value);
  const approval = parseScopeList($("#projScopesApproval").value);
  const autonomous = parseScopeList($("#projScopesAutonomous").value);
  if (reserved.length) scopes.reserved = reserved;
  if (approval.length) scopes.approval = approval;
  if (autonomous.length) scopes.autonomous = autonomous;
  const body = {
    name: $("#projName").value.trim(),
    workspace: $("#projWs").value.trim(),
    instructions: $("#projInstr").value,
    color: projColorDraft,
    files: projFilesDraft.slice(),
    scopes,
  };
  try {
    let out;
    if (projEditing) out = await api("/api/project/" + encodeURIComponent(projEditing), { method: "PATCH", body: JSON.stringify(body) });
    else out = await api("/api/projects", { method: "POST", body: JSON.stringify(body) });
    state.projects = out.projects || state.projects;
    const created = out.project;
    closeProjModal();
    renderSessions();
    toast("Project saved", "ok");
    if (!projEditing && created && created.id) selectProject(created.id);
    else updateProjChip();
  } catch (e) {
    const w = $("#projWarn");
    w.textContent = e.message;
    w.classList.remove("hidden");
  }
}

async function deleteProj() {
  if (!projEditing) return;
  const pid = projEditing;
  const p = state.projects.find((x) => x.id === pid);
  if (!(await uiConfirm({ title: "Delete project", message: "Delete project \"" + (p ? p.name : pid) + "\"? Its chats are kept but left without a project.", ok: "Delete", danger: true }))) return;
  try {
    await api("/api/project/" + encodeURIComponent(pid), { method: "DELETE" });
    closeProjModal();
    if (state.proj === pid) selectProject("");
    await loadProjects();
    toast("Project deleted", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
}

async function duplicateProj() {
  const p = state.projects.find((x) => x.id === projEditing);
  if (!p) return;
  try {
    const out = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name: p.name + " copy", instructions: p.instructions, workspace: p.workspace, color: p.color, files: p.files, scopes: p.scopes || {} }),
    });
    state.projects = out.projects || state.projects;
    closeProjModal();
    renderSessions();
    toast("Project duplicated", "ok");
  } catch (e) { toast(e.message, "err"); }
}

async function assignSession(sid, pid) {
  try {
    await api("/api/assign", { method: "POST", body: JSON.stringify({ session_id: sid, project_id: pid }) });
    toast(pid ? "Moved to project" : "Removed from project", "ok");
    await loadSessions();
    await loadProjects();
    if (state.sid === sid) {
      state.proj = pid;
      if (state.proj) localStorage.setItem("df_proj", state.proj);
      else localStorage.removeItem("df_proj");
      updateProjChip();
      stageHomeFill();
    }
  } catch (e) { toast(e.message, "err"); }
}

async function exportProject() {
  const p = curProject();
  if (!p) return;
  const rows = state.sessions.filter((s) => s.project === p.id);
  if (!rows.length) { toast("No chats in this project yet", "info"); return; }
  const lines = ["# Project: " + p.name, ""];
  if (p.instructions) lines.push("> instructions: " + p.instructions.replace(/\n/g, "\n> "), "");
  for (const r of rows) {
    try {
      const data = await api("/api/session/" + encodeURIComponent(r.id));
      lines.push("---", "", buildSessionMarkdown(data), "");
    } catch {}
  }
  download(p.id + "-project.md", lines.join("\n"), "text/markdown");
  toast("Exported project (" + rows.length + " chat" + (rows.length > 1 ? "s" : "") + ")", "ok");
}

let moveTargetSid = null;
function openProjPick(sid, anchor) {
  moveTargetSid = sid;
  const m = $("#projPickMenu");
  m.replaceChildren();
  const mk = (label, pid, cur) => {
    const b = el("button", "", label);
    if (cur) b.classList.add("cur");
    b.addEventListener("click", () => { m.classList.add("hidden"); assignSession(moveTargetSid, pid); });
    return b;
  };
  const cur = (state.sessions.find((s) => s.id === sid) || {}).project || "";
  m.appendChild(mk("no project", "", !cur));
  for (const p of state.projects) m.appendChild(mk(p.name, p.id, p.id === cur));
  const newB = el("button", "", "New project\u2026");
  newB.addEventListener("click", () => { m.classList.add("hidden"); openProjModal(null); });
  m.appendChild(newB);
  openDropdown(m, anchor || $("#kebabBtn"), { align: "left" });
}


/* -------------------------------------------------------------- slash popup */

let slashItems = [];
let slashSel = 0;

function slashKey(k) {
  if (!slashItems.length) return false;
  if (k === "ArrowDown" || k === "ArrowUp") {
    slashSel = (slashSel + (k === "ArrowDown" ? 1 : slashItems.length - 1)) % slashItems.length;
    const rows = $("#slashPop").children;
    for (let i = 0; i < rows.length; i++) rows[i].classList.toggle("sel", i === slashSel);
    return true;
  }
  if (k === "Enter" || k === "Tab") {
    slashPick(slashItems[slashSel][0]);
    return true;
  }
  if (k === "Escape") { closeSlash(); return true; }
  return false;
}

/* ------------------------------------------------------------- @-file mentions */

let atItems = [];
let atSel = 0;
let atToken = "";
let atFilesCache = { key: "", files: [], at: 0 };

// build artefacts and vendored trees are noise in both Quick Open and an
// @-mention; this mirrors repo_index.SKIP_DIRS on the Python side
const WS_SKIP_DIRS = new Set([
  "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
  ".pytest_cache", ".mypy_cache", ".ruff_cache", "target", ".next", "coverage",
]);

async function workspaceFiles() {
  const key = (state.proj || "") + "|" + (state.sid || "");
  const now = Date.now();
  if (atFilesCache.key === key && now - atFilesCache.at < 30000) return atFilesCache.files;
  const files = [];
  async function walk(rel, depth) {
    if (depth > 3 || files.length >= 400) return;
    let data;
    try { data = await api("/api/ws?sid=" + encodeURIComponent(state.sid || "") + "&path=" + encodeURIComponent(rel)); }
    catch { return; }
    for (const e of data.entries || []) {
      if (files.length >= 400) break;
      if (WS_SKIP_DIRS.has(e.name)) continue;
      const rpath = (rel ? rel.replace(/\/+$/, "") + "/" : "") + e.name;
      if (e.dir) { files.push(rpath + "/"); await walk(rpath + "/", depth + 1); }
      else files.push(rpath);
    }
  }
  await walk("", 0);
  atFilesCache = { key, files, at: now };
  return files;
}

function atMaybe() {
  const input = $("#input");
  if (input.value.startsWith("/")) { closeAt(); return; } // slash popup owns "/"
  const pos = input.selectionStart || 0;
  const lineStart = input.value.lastIndexOf("\n", pos - 1) + 1;
  const m = input.value.slice(lineStart, pos).match(/(?:^|\s)@([\w\-./\\]*)$/);
  if (!m) { closeAt(); return; }
  atToken = m[1];
  const q = atToken.toLowerCase().replace(/\\/g, "/");
  workspaceFiles().then((files) => {
    atItems = files.filter((f) => f.toLowerCase().includes(q)).slice(0, 12);
    if (!atItems.length) { closeAt(); return; }
    atSel = 0;
    const pop = $("#atPop");
    pop.replaceChildren();
    atItems.forEach((f, i) => {
      const row = el("div", "slash-item" + (i === atSel ? " sel" : ""));
      row.appendChild(el("span", "slash-cmd", "@" + f));
      row.addEventListener("click", () => atPick(f));
      pop.appendChild(row);
    });
    pop.classList.remove("hidden");
  });
}

/* ------------------------------------------------- workspace tree picker */

const treeState = { open: new Set(), cache: new Map() };

async function treeChildren(rel) {
  if (treeState.cache.has(rel)) return treeState.cache.get(rel);
  let data;
  try { data = await api("/api/ws?sid=" + encodeURIComponent(state.sid || "") + "&path=" + encodeURIComponent(rel)); }
  catch { return []; }
  const entries = (data.entries || []).slice().sort((a, b) =>
    (a.dir === b.dir) ? a.name.localeCompare(b.name) : (a.dir ? -1 : 1));
  treeState.cache.set(rel, entries);
  return entries;
}

function treeInsert(path) {
  const input = $("#input");
  const pos = input.selectionStart || input.value.length;
  const before = input.value.slice(0, pos);
  const sep = (before && !before.endsWith(" ")) ? " " : "";
  input.value = before + sep + path + " " + input.value.slice(pos);
  const caret = (before + sep + path + " ").length;
  input.setSelectionRange(caret, caret);
  closeTree();
  input.focus();
  autoGrow(input);
  updateSendEnabled();
}

async function treeRender() {
  const pop = $("#treePop");
  pop.replaceChildren();
  const head = el("div", "tree-head");
  head.appendChild(el("span", "hint", "Click a name to add it to your message"));
  const close = el("button", "btn-sub", "Close");
  close.addEventListener("click", closeTree);
  head.appendChild(close);
  pop.appendChild(head);

  async function level(rel, depth) {
    for (const e of await treeChildren(rel)) {
      const path = (rel ? rel.replace(/\/+$/, "") + "/" : "") + e.name;
      const row = el("div", "tree-row" + (e.dir ? " tree-dir" : ""));
      row.style.paddingLeft = (8 + depth * 14) + "px";
      const isOpen = treeState.open.has(path);
      const caret = el("span", "tree-caret", e.dir ? (isOpen ? "\u25be" : "\u25b8") : "");
      if (e.dir) {
        caret.addEventListener("click", (ev) => {
          ev.stopPropagation();
          isOpen ? treeState.open.delete(path) : treeState.open.add(path);
          treeRender();
        });
      }
      row.appendChild(caret);
      row.appendChild(el("span", "tree-name", e.name + (e.dir ? "/" : "")));
      row.addEventListener("click", () => treeInsert(path + (e.dir ? "/" : "")));
      pop.appendChild(row);
      if (e.dir && isOpen) await level(path + "/", depth + 1);
    }
  }
  await level("", 0);
  if (pop.childElementCount === 1) pop.appendChild(el("div", "hint", "Workspace is empty."));
}

function closeTree() { $("#treePop").classList.add("hidden"); }

async function toggleTree() {
  const pop = $("#treePop");
  if (!pop.classList.contains("hidden")) { closeTree(); return; }
  treeState.cache.clear();
  pop.classList.remove("hidden");
  await treeRender();
}

function atPick(file) {
  const input = $("#input");
  const pos = input.selectionStart || 0;
  const start = Math.max(0, pos - atToken.length - 1); // '@' + typed token
  const after = input.value.slice(pos);
  input.value = input.value.slice(0, start) + file + " " + after;
  const caret = start + file.length + 1;
  input.setSelectionRange(caret, caret);
  closeAt();
  input.focus();
  autoGrow(input);
  updateSendEnabled();
}
function closeAt() { $("#atPop").classList.add("hidden"); atItems = []; }
function atKey(k) {
  if (!atItems.length) return false;
  if (k === "ArrowDown" || k === "ArrowUp") {
    atSel = (atSel + (k === "ArrowDown" ? 1 : atItems.length - 1)) % atItems.length;
    const rows = $("#atPop").children;
    for (let i = 0; i < rows.length; i++) rows[i].classList.toggle("sel", i === atSel);
    return true;
  }
  if (k === "Enter" || k === "Tab") { atPick(atItems[atSel]); return true; }
  if (k === "Escape") { closeAt(); return true; }
  return false;
}

/* --------------------------------------------------------------- attachments */

const TEXT_EXTS = [".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml", ".toml", ".csv", ".log", ".html", ".css", ".sh", ".bat", ".ps1"];

function addImages(files) {
  let imgCount = 0;
  for (const f of files) {
    if (f.type.startsWith("image/")) {
      if (state.images.length >= 4) { toast("Max 4 images per message", "info"); break; }
      const rd = new FileReader();
      rd.onload = () => state.images.push({ name: f.name || "pasted.png", dataUrl: rd.result });
      rd.readAsDataURL(f);
      imgCount++;
    } else if (TEXT_EXTS.some((x) => f.name.toLowerCase().endsWith(x))) {
      if (f.size > 200_000) { toast(f.name + " too large (max 200 KB)", "info"); continue; }
      const rd = new FileReader();
      rd.onload = () => {
        const input = $("#input");
        const lang = f.name.split(".").pop().toLowerCase();
        const block = "\n\n```" + lang + "\n" + String(rd.result).slice(0, 60_000) + "\n```";
        input.value = (input.value ? input.value.replace(/\s*$/, "") : "Review this file:") + block + "\n";
        autoGrow(input);
        updateSendEnabled();
        toast("Attached " + f.name + " as text", "ok");
      };
      rd.readAsText(f);
    }
  }
  if (imgCount) setTimeout(renderAttachRow, 120);
}
function renderAttachRow() {
  const row = $("#attachRow");
  row.replaceChildren();
  state.images.forEach((img, i) => {
    const chip = el("div", "att-chip");
    const im = document.createElement("img");
    im.src = img.dataUrl;
    chip.appendChild(im);
    chip.appendChild(el("span", "", img.name.length > 22 ? img.name.slice(0, 20) + "â€¦" : img.name));
    const x = el("button", "", "\u00d7");
    x.title = "remove";
    x.addEventListener("click", () => { state.images.splice(i, 1); renderAttachRow(); });
    chip.appendChild(x);
    row.appendChild(chip);
  });
  row.classList.toggle("hidden", !state.images.length);
  updateSendEnabled();
}

/* --------------------------------------------------- safety mode (explicit) */

const SAFETY_MODES = [
  ["ask", "ask \u2014 confirm risky actions in chat"],
  ["deny", "deny \u2014 block risky actions outright"],
  ["off", "off \u2014 no gating (destructive guardrails stay on)"],
  ["autonomous", "yolo \u2014 fully autonomous, zero prompts (hardline blocks still apply)"],
];

function openSafetyMenu(anchor) {
  const m = $("#safetyMenu");
  m.replaceChildren();
  for (const [v, label] of SAFETY_MODES) {
    const b = el("button", "", label);
    if (v === (state.info && state.info.safety_mode)) b.classList.add("cur");
    b.addEventListener("click", async () => {
      closeMenus();
      try {
        const out = await api("/api/config", { method: "POST", body: JSON.stringify({ safety_mode: v }) });
        state.info = out;
        renderHeaderPills();
        toast("Safety mode: " + v, "ok");
      } catch (e) { toast(e.message, "err"); }
    });
    m.appendChild(b);
  }
  const chip = $("#safetyChip");
  openDropdown(m, anchor || (chip && chip.offsetParent ? chip : $("#safetyBadge")), { align: "left" });
}

/* ------------------------------------------------- prompt enhancer (Bolt) */

let enhanceUndo = null; // {orig, at} while an undo window is open

async function enhancePrompt() {
  const input = $("#input");
  const btn = $("#enhanceBtn");
  const text = input.value.trim();
  if (!text) return;
  // second click within the undo window restores the original text
  if (enhanceUndo && enhanceUndo.enhanced === text && Date.now() - enhanceUndo.at < 60000) {
    input.value = enhanceUndo.orig;
    autoGrow(input);
    updateSendEnabled();
    enhanceUndo = null;
    toast("Original prompt restored", "ok");
    return;
  }
  if (state.busy) { toast("Wait for the current run to finish", "info"); return; }
  btn.disabled = true;
  btn.classList.add("spin-ic");
  try {
    const out = await api("/api/enhance", { method: "POST", body: JSON.stringify({ text }) });
    enhanceUndo = { orig: input.value, enhanced: out.text, at: Date.now() };
    input.value = out.text;
    autoGrow(input);
    updateSendEnabled();
    input.focus();
    toast("Prompt enhanced \u2014 click the wand again to undo", "ok");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    btn.disabled = false;
    btn.classList.remove("spin-ic");
  }
}

/* ------------------------------------------- follow-up suggestions (Devin) */

let followupsSeq = 0;

function clearFollowups() {
  const row = $("#followRow");
  if (row && !row.classList.contains("hidden")) { row.classList.add("hidden"); row.replaceChildren(); }
}

async function fetchFollowups() {
  if (!state.sid || state.busy) return;
  if (state.info && state.info.suggest_followups === false) return;
  const seq = ++followupsSeq;
  let out;
  try {
    out = await api("/api/suggest", { method: "POST", body: JSON.stringify({ session_id: state.sid }) });
  } catch { return; } // best-effort chrome: never surface model errors here
  if (seq !== followupsSeq || state.busy) return;
  const row = $("#followRow");
  if (!out.suggestions || !out.suggestions.length) { row.classList.add("hidden"); return; }
  row.replaceChildren();
  for (const s of out.suggestions.slice(0, 3)) {
    const b = el("button", "follow-chip", s);
    b.title = "Send this follow-up";
    b.addEventListener("click", () => {
      clearFollowups();
      const input = $("#input");
      input.value = s;
      updateSendEnabled();
      send();
    });
    row.appendChild(b);
  }
  row.classList.remove("hidden");
}

/* ------------------------------------------------ composer drafts (Cursor) */

function draftKey(sid) { return "df_draft_" + (sid || "new"); }
function saveDraft() {
  const v = $("#input").value;
  try {
    if (v) localStorage.setItem(draftKey(state.sid), v);
    else localStorage.removeItem(draftKey(state.sid));
  } catch {}
}
function restoreDraft() {
  const input = $("#input");
  let v = "";
  try { v = localStorage.getItem(draftKey(state.sid)) || ""; } catch {}
  input.value = v;
  autoGrow(input);
  updateSendEnabled();
}

/* -------------------------------- detached-run finish badges (Devin/Cursor) */

let detachedSids = [];
let finishedSids = [];
try { detachedSids = JSON.parse(localStorage.getItem("df_detached") || "[]"); } catch {}
try { finishedSids = JSON.parse(localStorage.getItem("df_finished") || "[]"); } catch {}

function persistDetachedFlags() {
  try {
    localStorage.setItem("df_detached", JSON.stringify(detachedSids));
    localStorage.setItem("df_finished", JSON.stringify(finishedSids));
  } catch {}
}
function markDetached(sid) {
  if (!sid || detachedSids.includes(sid)) return;
  detachedSids.push(sid);
  persistDetachedFlags();
}
function clearRunFlags(sid) {
  if (!detachedSids.includes(sid) && !finishedSids.includes(sid)) return;
  detachedSids = detachedSids.filter((x) => x !== sid);
  finishedSids = finishedSids.filter((x) => x !== sid);
  persistDetachedFlags();
}
// a detached run that leaves /api/runs' busy set finished while we were away
setInterval(async () => {
  if (!detachedSids.length) return;
  let runs;
  try { runs = (await api("/api/runs")).runs || []; } catch { return; }
  const busy = new Set(runs.filter((r) => r.busy).map((r) => r.id));
  let changed = false;
  for (const sid of [...detachedSids]) {
    if (busy.has(sid)) continue;
    detachedSids = detachedSids.filter((x) => x !== sid);
    if (!finishedSids.includes(sid)) finishedSids.push(sid);
    changed = true;
    if (sid === state.sid) continue; // watching it live; the stream already notified
    if (localStorage.getItem("df_sound") === "1") playPing();
    if (document.hidden && window.Notification && Notification.permission === "granted" && localStorage.getItem("df_notify") !== "0") {
      try { new Notification("Background session finished"); } catch {}
    }
  }
  if (changed) { persistDetachedFlags(); renderSessions(); }
}, 6000);

/* ------------------------------------------------- image lightbox (Devin) */

function lightboxClose() { $("#lightbox").classList.add("hidden"); }
function lightboxOpen(src) {
  $("#lightbox").querySelector("img").src = src;
  $("#lightbox").classList.remove("hidden");
}

/* --------------------------------------------------------------------- misc */

/* Dialog parity (every competitor): promise-based in-app confirm/prompt
   replacing the native browser dialogs, which are unstyled and unreliable
   inside the desktop shell. Enter confirms, Esc / Cancel / outside click
   dismisses; destructive actions get the red button. */
let askResolve = null;

function askClose(result) {
  const m = $("#askModal");
  if (m.classList.contains("hidden")) return;
  m.classList.add("hidden");
  if (askResolve) { const r = askResolve; askResolve = null; r(result); }
}

function askOpen(opts) {
  const m = $("#askModal");
  m.classList.remove("hidden");
  $("#askTitle").textContent = opts.title || "Confirm";
  const msg = $("#askMsg");
  msg.textContent = opts.message || "";
  msg.classList.toggle("hidden", !opts.message);
  const inp = $("#askInput");
  const hasInput = opts.value !== undefined;
  inp.classList.toggle("hidden", !hasInput);
  inp.value = hasInput ? opts.value : "";
  const okB = $("#askOk");
  okB.textContent = opts.ok || "OK";
  okB.classList.toggle("danger-solid", !!opts.danger);
  okB.classList.toggle("primary-btn", !opts.danger);
  setTimeout(() => (hasInput ? inp : okB).focus(), 30);
  return new Promise((resolve) => { askResolve = resolve; });
}

function uiConfirm(opts) {
  return askOpen({ ok: "Confirm", ...opts }).then((v) => v === true);
}

async function uiPrompt(opts) {
  const ok = await askOpen({ ok: "Save", ...opts, value: opts.value === undefined ? "" : opts.value });
  return ok === true ? $("#askInput").value : null;
}

function updateSendEnabled() {
  const btn = $("#sendBtn");
  if (!state.busy) btn.disabled = !$("#input").value.trim() && !state.images.length;
}
function autoGrow(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 220) + "px";
}
function connOk() { $("#connDot").classList.remove("off"); }
function connOff() { $("#connDot").classList.add("off"); }

function toggleSidebar() {
  $("#sidebar").classList.toggle("collapsed");
  localStorage.setItem("df_sb", $("#sidebar").classList.contains("collapsed") ? "0" : "1");
}

function bindEvents() {
  const input = $("#input");
  input.addEventListener("input", () => {
    autoGrow(input);
    saveDraft();
    clearFollowups();
    slashMaybe();
    atMaybe();
    updateSendEnabled();
    const eb = $("#enhanceBtn");
    if (eb) eb.classList.toggle("hidden", !input.value.trim() || state.busy);
  });
  $("#enhanceBtn").addEventListener("click", enhancePrompt);
  input.addEventListener("keydown", (e) => {
    if (slashItems.length && slashKey(e.key)) { e.preventDefault(); return; }
    if (atItems.length && atKey(e.key)) { e.preventDefault(); return; }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  $("#sendBtn").addEventListener("click", () => (state.busy ? stopRun() : send()));
  $("#newChatBtn").addEventListener("click", newChat);
  $("#sbToggle").addEventListener("click", toggleSidebar);
  // the collapsed-sidebar scrim is clickable: tapping outside is the standard
  // mobile dismiss gesture, not just a visual dim layer
  $("#sbScrim").addEventListener("click", () => {
    if ($("#sidebar").classList.contains("collapsed")) toggleSidebar();
  });
  $("#settingsClose").addEventListener("click", closeSettings);
  $("#settingsModal").addEventListener("mousedown", (e) => { if (e.target === $("#settingsModal")) closeSettings(); });
  $("#settingsSave").addEventListener("click", saveSettings);
  for (const b of document.querySelectorAll("#setNav button")) {
    b.addEventListener("click", () => settingsShow(b.dataset.sec));
  }
  $("#btnRevealConfig").addEventListener("click", () => revealFolder("config"));
  $("#btnRevealSessions").addEventListener("click", () => revealFolder("sessions"));
  $("#btnRevealWorkspace").addEventListener("click", () => revealFolder("workspace"));
  $("#btnExportAll").addEventListener("click", exportAllData);
  $("#btnClearSessions").addEventListener("click", clearAllData);
  $("#btnDiag").addEventListener("click", copyDiagnostics);
  $("#obSkip").addEventListener("click", () => { sessionStorage.setItem("df_onboard_skip", "1"); closeOnboard(); });
  $("#obSave").addEventListener("click", onboardSave);
  $("#obKey").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); onboardSave(); } });
  $("#obProvider").addEventListener("change", obProviderChanged);
  $("#cfgTest").addEventListener("click", cfgTestConnection);
  $("#cfgProvider").addEventListener("change", providerHint);
  $("#cfgAssistant").addEventListener("change", () => {
    // an assistant works while you work: surface background-first visibly,
    // and default the hands-free loop on (unless explicitly turned off before)
    if ($("#cfgAssistant").checked) {
      if (!$("#cfgBgOnly").checked) $("#cfgBgOnly").checked = true;
      if (!localStorage.getItem("df_tts")) $("#cfgTts").checked = true;
      if (localStorage.getItem("df_voice_autosend") === null) $("#cfgVoiceAutosend").checked = true;
    }
  });
  $("#cfgModel").addEventListener("input", () => {});
  $("#cfgTemp").addEventListener("input", () => { $("#cfgTempVal").textContent = Number($("#cfgTemp").value).toFixed(2); });
  $("#cfgTopP").addEventListener("input", () => { $("#cfgTopPVal").textContent = Number($("#cfgTopP").value).toFixed(2); });
  $("#cfgSearch").addEventListener("input", filterSettings);
  $("#cfgTtsRate").addEventListener("input", () => { $("#cfgTtsRateVal").textContent = Number($("#cfgTtsRate").value).toFixed(2); });
  for (const b of $("#cfgSafety").children) b.addEventListener("click", () => segSet("cfgSafety", b.dataset.v));

  $("#modeBadge").addEventListener("click", () => openSettings());
  const pb = $("#planBadge");
  if (pb) pb.addEventListener("click", (e) => { e.stopPropagation(); togglePlanMode(); });
  $("#safetyBadge").addEventListener("click", (e) => {
    e.stopPropagation();
    openSafetyMenu($("#safetyBadge")); // explicit menu, not blind cycling (misclick safety)
  });
  $("#planChip").addEventListener("click", (e) => { e.stopPropagation(); togglePlanMode(); });
  $("#safetyChip").addEventListener("click", (e) => { e.stopPropagation(); openSafetyMenu($("#safetyChip")); });
  $("#sideSettings").addEventListener("click", openSettings);

  $("#askOk").addEventListener("click", () => askClose(true));
  $("#askCancel").addEventListener("click", () => askClose(false));
  $("#askModal").addEventListener("mousedown", (e) => { if (e.target === $("#askModal")) askClose(false); });
  $("#askInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); askClose(true); }
    e.stopPropagation();
  });
  $("#lightbox").addEventListener("mousedown", (e) => { if (e.target.id === "lightbox" || e.target.tagName === "IMG") lightboxClose(); });

  $("#attachBtn").addEventListener("click", () => $("#fileInput").click());
  $("#micBtn").addEventListener("click", micToggle);
  $("#keyHint").addEventListener("click", openSettings);
  $("#themeBtn").addEventListener("click", (e) => { e.stopPropagation(); openThemeMenu(); });
  $("#tokMeter").addEventListener("click", ctxOpen);
  $("#ctxClose").addEventListener("click", ctxClose);
  $("#ctxModal").addEventListener("mousedown", (e) => { if (e.target === $("#ctxModal")) ctxClose(); });
  $("#ctxCompact").addEventListener("click", compactNow);
  $("#archToggle").addEventListener("click", () => { state.showArchived = !state.showArchived; renderSessions(); });
  $("#cmpClose").addEventListener("click", closeCompare);
  $("#cmpRestore").addEventListener("click", compareRestore);
  $("#cmpModal").addEventListener("mousedown", (e) => { if (e.target === $("#cmpModal")) closeCompare(); });
  $("#scrollBtn").addEventListener("click", () => scrollDown(true));
  $("#kebabBtn").addEventListener("click", (e) => { e.stopPropagation(); openKebab(); });
  $("#kebabMenu").addEventListener("click", (e) => {
    const act = e.target.closest("button")?.dataset.act;
    if (!act) return;
    closeMenus();
     if (act === "rename") { if (state.sid) renameSession(state.sid); else toast("No session", "info"); }
     else if (act === "delete") { if (state.sid) deleteSession(state.sid); else toast("No session", "info"); }
     else if (act === "pin") { if (state.sid) { togglePin(state.sid); toast("Pin updated", "ok"); } }
     else if (act === "branch") { if (state.sid) branchSession(state.sid); else toast("No session", "info"); }
     else if (act === "archive") { if (state.sid) { const cur = state.sessions.find((s) => s.id === state.sid); archiveSession(state.sid, !(cur && cur.archived)); } else toast("No session", "info"); }
     else if (act === "move") { if (state.sid) openProjPick(state.sid, $("#kebabBtn")); else toast("No session", "info"); }
    else if (act === "export-md") exportSession("md");
    else if (act === "export-json") exportSession("json");
    else if (act === "export-html") exportHtml();
    else if (act === "shortcuts") shortcutsOpen();
    else if (act === "settings") openSettings();
  });
  $("#modelPill").addEventListener("click", (e) => { e.stopPropagation(); openModelMenu(); });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#kebabMenu") && !e.target.closest("#kebabBtn")) $("#kebabMenu").classList.add("hidden");
    if (!e.target.closest("#modelMenu") && !e.target.closest("#modelPill")) $("#modelMenu").classList.add("hidden");
    if (!e.target.closest("#themeMenu") && !e.target.closest("#themeBtn")) $("#themeMenu").classList.add("hidden");
    if (!e.target.closest("#safetyMenu") && !e.target.closest("#safetyChip") && !e.target.closest("#safetyBadge")) $("#safetyMenu").classList.add("hidden");
    if (!e.target.closest("#projPickMenu") && !e.target.closest("#kebabMenu")) $("#projPickMenu").classList.add("hidden");
    if (!e.target.closest("#atPop") && !e.target.closest("#input")) closeAt();
  });

  $("#newProjBtn").addEventListener("click", () => openProjModal(null));
  $("#projChip").addEventListener("click", () => { const p = curProject(); if (p) openProjModal(p); });
  $("#projClose").addEventListener("click", closeProjModal);
  $("#projSave").addEventListener("click", saveProj);
  $("#projDelete").addEventListener("click", deleteProj);
  $("#projDup").addEventListener("click", duplicateProj);
  $("#projModal").addEventListener("mousedown", (e) => { if (e.target === $("#projModal")) closeProjModal(); });
  for (const b of document.querySelectorAll("#projColors .swatch")) {
    b.addEventListener("click", () => { projColorDraft = b.dataset.c; paintSwatches(); });
  }
  $("#projFileAdd").addEventListener("click", () => {
    const v = $("#projFileInput").value.trim();
    if (!v) return;
    if (projFilesDraft.includes(v)) { toast("Already added", "info"); return; }
    projFilesDraft.push(v);
    $("#projFileInput").value = "";
    renderKfiles();
  });
  $("#projFileInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); $("#projFileAdd").click(); }
    e.stopPropagation();
  });
  $("#shortcutsClose").addEventListener("click", shortcutsClose);
  $("#findNext").addEventListener("click", () => findGo(findState.cur + 1));
  $("#findPrev").addEventListener("click", () => findGo(findState.cur - 1));
  $("#findClose").addEventListener("click", closeFind);
  $("#findInput").addEventListener("input", (e) => runFind(e.target.value.trim()));
  $("#findInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); findGo(findState.cur + (e.shiftKey ? -1 : 1)); }
    if (e.key === "Escape") { e.preventDefault(); closeFind(); }
  });
  $("#schedAdd").addEventListener("click", addSchedule);
  for (const id of ["schedExpr", "schedTask"]) {
    $("#" + id).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addSchedule(); } });
  }
  $("#cmdAdd").addEventListener("click", () => {
    if (cmdDraft == null) cmdDraft = {};
    let n = 1;
    while (cmdDraft["cmd" + n]) n++;
    cmdDraft["cmd" + n] = { prompt: "", description: "" };
    renderCommands();
    const rows = document.querySelectorAll("#cmdList .cmd-name");
    if (rows.length) rows[rows.length - 1].closest(".cmd-row").querySelector(".cmd-prompt").focus();
  });
  $("#sessFilter").addEventListener("input", (e) => { state.filter = e.target.value; renderSessions(); });
  $("#sessFilter").addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      e.target.value = "";
      state.filter = "";
      renderSessions();
      e.target.blur();
    }
  });
  $("#mcpTest").addEventListener("click", () => loadMcp(true));
  $("#auditRun").addEventListener("click", () => loadAudit());
  $("#doctorRun").addEventListener("click", () => loadDoctor());
  $("#updCheck").addEventListener("click", () => loadUpdate());
  $("#pipeReload").addEventListener("click", () => loadPipelines());
  $("#openFolderBtn").addEventListener("click", () => folderOpen());
  $("#folderClose").addEventListener("click", () => folderClose());
  $("#folderOpen").addEventListener("click", () => folderUse());
  $("#folderUp").addEventListener("click", () => {
    const up = folderState.path.replace(/[\\/]+$/, "").replace(/[\\/][^\\/]+$/, "");
    folderGo(up || "/");
  });
  $("#paletteInput").addEventListener("input", (e) => {
    const raw = e.target.value;
    paletteBuild(raw);
    // the chat-content search belongs to command mode; in Quick Open the list
    // is files, and a second results block underneath is just noise
    const cmd = raw.trimStart();
    paletteSearchAsync(cmd.startsWith(">") ? cmd.slice(1).trim() : (palFileMode ? "" : cmd.trim()));
  });
  $("#paletteInput").addEventListener("keydown", (e) => { if (paletteKey(e.key)) e.preventDefault(); });
  $("#paletteOverlay").addEventListener("mousedown", (e) => { if (e.target === $("#paletteOverlay")) paletteClose(); });
  for (const b of document.querySelectorAll(".stage-tab")) {
    b.addEventListener("click", () => {
      stageShow(b.dataset.tab, false);
      if (b.dataset.tab === "files") filesEnsure(true);
      else if (b.dataset.tab === "preview") previewEnsureUrlBar();
      else if (b.dataset.tab === "runs") runsLoad();
    });
  }
  $("#fileInput").addEventListener("change", (e) => { addImages([...e.target.files]); e.target.value = ""; });
  document.addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.items || [])].filter((i) => i.kind === "file").map((i) => i.getAsFile()).filter(Boolean);
    if (files.length) { addImages(files); e.preventDefault(); }
  });
  let dragDepth = 0;
  window.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; showDrop(); });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; hideDrop(); } });
  window.addEventListener("drop", (e) => {
    e.preventDefault();
    dragDepth = 0;
    hideDrop();
    const files = [...(e.dataTransfer?.files || [])];
    if (files.length) addImages(files); // images attach; text files drop into the composer
  });
  function showDrop() {
    let ov = $("#dropOverlay");
    if (!ov) {
      ov = el("div", "", "Drop images or text files to attach");
      ov.id = "dropOverlay";
      document.body.appendChild(ov);
    }
  }
  function hideDrop() { const ov = $("#dropOverlay"); if (ov) ov.remove(); }

  document.addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey;
    const key = (e.key || "").toLowerCase();
    const panel = (tab, extra) => { e.preventDefault(); stageShow(tab, false); if (extra) extra(); };

    // Ctrl+K is a chord PREFIX in VS Code, not a command of its own; resolve a
    // pending one before anything else, and let any other key cancel it.
    if (chordPrefix === "k") {
      clearTimeout(chordTimer);
      chordPrefix = null;
      if (mod && key === "s") { e.preventDefault(); shortcutsOpen(); return; }
      if (mod && key === "h") { panel("activity"); return; }   // Show Output, Linux
      if (mod && key === "o") { e.preventDefault(); folderOpen(); return; }  // Open Folder
    }
    if (mod && !e.shiftKey && !e.altKey && key === "k") {
      e.preventDefault();
      chordPrefix = "k";
      clearTimeout(chordTimer);
      chordTimer = setTimeout(() => { chordPrefix = null; }, 2000);
      return;
    }

    // Quick Open and the command palette are one overlay, as in VS Code
    if (e.key === "F1") { e.preventDefault(); paletteOpen(true); return; }
    if (mod && e.shiftKey && key === "p") { e.preventDefault(); paletteOpen(true); return; }
    if (mod && !e.shiftKey && key === "p") { e.preventDefault(); paletteOpen(false); return; }

    // the panels, on VS Code's activity-bar and panel keys
    if (mod && e.shiftKey && key === "e") { panel("files", () => filesEnsure(true)); return; }
    if (mod && e.shiftKey && key === "g") { panel("changes"); return; }
    if (mod && e.shiftKey && key === "u") { panel("activity"); return; }
    if (mod && e.shiftKey && key === "d") { panel("runs"); return; }
    if (mod && e.shiftKey && key === "v") { panel("preview"); return; }
    if (mod && !e.shiftKey && key === ",") { e.preventDefault(); openSettings(); return; }

    if (mod && key === "f") { e.preventDefault(); openFind(); return; }
    if (mod && key === "m") { e.preventDefault(); closeMenus(); openModelMenu(); return; }
    if (e.altKey && key === "m") { e.preventDefault(); cycleFavoriteModel(); return; }
    if (mod && key === "n") { e.preventDefault(); newChat(); return; }
    if (mod && key === "b") { e.preventDefault(); toggleSidebar(); return; }
    if (mod && e.shiftKey && key === "c") { e.preventDefault(); ctxOpen(); return; }
    if (e.key === "Escape") {
      // Trust modal is non-dismissible: require an explicit button click.
      if (!$("#trustModal").classList.contains("hidden")) return;
      if (!$("#paletteOverlay").classList.contains("hidden")) { paletteClose(); return; }
      if (!$("#findBar").classList.contains("hidden")) { closeFind(); return; }
      if (!$("#cmpModal").classList.contains("hidden")) { closeCompare(); return; }
      if (!$("#shortcutsModal").classList.contains("hidden")) { shortcutsClose(); return; }
      if (!$("#settingsModal").classList.contains("hidden")) { closeSettings(); return; }
      if (!$("#folderModal").classList.contains("hidden")) { folderClose(); return; }
      if (!$("#projModal").classList.contains("hidden")) { closeProjModal(); return; }
      if (!$("#ctxModal").classList.contains("hidden")) { ctxClose(); return; }
      if (!$("#askModal").classList.contains("hidden")) { askClose(false); return; }
      if (!$("#lightbox").classList.contains("hidden")) { lightboxClose(); return; }
      closeMenus();
      closeSlash();
      closeAt();
      // nothing left to dismiss: Esc means "stop what you're doing"
      if (state.busy) { stopRun(); return; }
      return;
    }    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
    const anyModal = ["#settingsModal", "#projModal", "#ctxModal", "#onboardModal", "#paletteOverlay", "#shortcutsModal", "#cmpModal", "#trustModal", "#askModal", "#lightbox"].some(
      (s) => !$(s).classList.contains("hidden")
    );
    if (!typing && !anyModal && state.approvals.size) {
      const k = e.key.toLowerCase();
      const firstId = [...state.approvals.keys()][0];
      const fn = state.approvals.get(firstId);
      const decide = (decision) => api("/api/approve", { method: "POST", body: JSON.stringify({ id: firstId, decision }) }).catch(() => {});
      if (k === "y") { fn(true, false); decide("allow"); }
      else if (k === "a") { fn(true, false); decide("always"); }
      else if (k === "n") { fn(false, false); decide("deny"); }
    }
  });

  thread.addEventListener("click", (e) => {
    if (e.target.tagName === "IMG") { lightboxOpen(e.target.currentSrc || e.target.src); return; }
    if (e.target.classList && e.target.classList.contains("copybtn")) {
      const pre = e.target.closest(".codewrap").querySelector("code");
      navigator.clipboard.writeText(pre.textContent).then(() => { e.target.textContent = "copied"; setTimeout(() => (e.target.textContent = "copy"), 1200); });
    }
  });

  applyModeFlavor();

  window.addEventListener("online", connOk);
  window.addEventListener("offline", connOff);
  // anchored menus track their trigger at open time — dismiss instead of drifting (VS Code/ChatGPT behavior)
  window.addEventListener("resize", closeMenus);
  $("#transcript").addEventListener("scroll", closeMenus, { passive: true });
}
function buildSuggestions(items) {
  const input = $("#input");
  const sugBox = $("#suggests");
  sugBox.replaceChildren();
  for (const s of items) {
    const chip = el("button", "suggest-chip", s);
    chip.addEventListener("click", () => {
      if (isAssistant()) { input.value = s; updateSendEnabled(); send(); }
      else { input.value = s; input.focus(); autoGrow(input); updateSendEnabled(); }
    });
    sugBox.appendChild(chip);
  }
}

function applyModeFlavor() {
  const assistant = !!(state.info && state.info.persona_mode === "assistant");
  // visible simplification, not just relabeling: assistant mode drops the
  // whole technical stage + developer pills; the chat becomes the app
  document.body.classList.toggle("mode-assistant", assistant);
  const tag = document.querySelector("#emptyState .tagline");
  if (tag) {
    tag.textContent = assistant
      ? ((state.info && state.info.assistant_name) || "your assistant") + " is ready \u2014 tell it what you need."
      : "the auditable minimal harness.";
  }
  const input = $("#input");
  if (input) input.placeholder = assistant
    ? "Tell me what you need\u2026"
    : "Message Saturday\u2026  ( / for commands )";
  const hint = $("#composerHint");
  if (hint) hint.textContent = assistant
    ? "it runs in the background \u00b7 replies land here"
    : "Enter to send \u00b7 Shift+Enter newline";
  buildSuggestions(assistant
    ? [
        "Open Calculator and work out 7 \u00d7 6 without touching my mouse",
        "Find today's top tech headlines and save a summary to news.md",
        "Tidy the screenshots on my Desktop into one folder",
        "Remember: I prefer short answers and no interruptions after 6pm",
      ]
    : [
        "Explore this workspace and summarize what you find",
        "Find TODO comments and fix one",
        "Write tests for the most brittle module",
        "What tools do you have? Show /help",
      ]);
}

setInterval(() => { if (state.busy && isAssistant()) stageMeta(); }, 1000);

/* ---------------------------------------------------------------------- init */

function enableTitleBar(api) {
  document.body.classList.add("embedded");
  if ($("#tbCtx")) $("#tbCtx").textContent = (state.info && state.info.workspace_root) || "";
  const maxBtn = $("#tbMax");
  const setMaxIcon = (maxed) => {
    if (!maxBtn) return;
    maxBtn.title = maxed ? "Restore" : "Maximize";
    maxBtn.innerHTML = maxed
      ? '<svg viewBox="0 0 12 12"><path d="M3.5 3.5v-1h6v6h-1z" fill="none" stroke="currentColor" stroke-width="1.2"/><rect x="2" y="5" width="5" height="5" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>'
      : '<svg viewBox="0 0 12 12"><rect x="2" y="2" width="8" height="8" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>';
  };
  $("#tbMin").addEventListener("click", () => api.win_min());
  maxBtn.addEventListener("click", async () => setMaxIcon(await api.win_max()));
  $("#tbClose").addEventListener("click", () => api.win_close());
  // Windows convention: double-clicking the title bar toggles maximize/restore
  const brand = document.querySelector(".titlebar-brand");
  if (brand) brand.addEventListener("dblclick", async () => setMaxIcon(await api.win_max()));
  setMaxIcon(false);
}

function initTitleBar() {
  const api = window.pywebview && window.pywebview.api;
  if (api) { enableTitleBar(api); return; }
  window.addEventListener("pywebviewready", () => enableTitleBar(window.pywebview.api), { once: true });
}

window.df = { state, openProjPick, applyTheme, toggleTheme, THEMES };

async function init() {
  applyTheme(localStorage.getItem("df_theme") || "dark");
  initThemeListener();
  initTitleBar();
  bindEvents();
  if (!(window.SpeechRecognition || window.webkitSpeechRecognition)) $("#micBtn").classList.add("hidden");
  try {
    state.info = await api("/api/state");
    if (state.info.trust_pending) {
      // Project has untrusted config files — show the VS Code-style trust gate
      // before anything else becomes interactive. Re-fetch state after the
      // decision so provider/model reflect the newly loaded .env.
      await showTrustModal();
      state.info = await api("/api/state");
    }
    state.sessionModels = Object.assign({}, (state.info && state.info.session_models) || {});
    renderHeaderPills();
    stageHomeFill();
    applyModeFlavor();
    const ver = $("#verLabel");
    ver.textContent = "Saturday v" + state.info.version;
    if (state.info.warnings && state.info.warnings.length) toast(state.info.warnings[0], "info");
    setTimeout(maybeOnboard, 350);
  } catch (e) {
    connOff();
    toast("Cannot reach the Saturday server: " + e.message, "err");
  }
  stageMeta();
  await loadProjects();
  await loadSessions();
  const last = localStorage.getItem("df_sid");
  if (last && state.sessions.some((s) => s.id === last)) await openSession(last);
  else showEmptyState();
  updateProjChip();
  // sidebar starts collapsed only by stored preference — or on phone-width
  // viewports, where the expanded sidebar is a full-screen overlay that would
  // hide the chat on first load (the ☰ button or scrim reveals it)
  const sbPref = localStorage.getItem("df_sb");
  const narrow = window.matchMedia("(max-width: 900px)").matches;
  if (sbPref === "0" || (narrow && sbPref !== "1")) $("#sidebar").classList.add("collapsed");
  updateTokMeter();
  loadCtx();
  wireAgentsUi();
  $("#treeBtn").addEventListener("click", toggleTree);
  wireRemoteUi();
  // Workbench dashboard elapsed clock (only touches the DOM while a run is live)
  setInterval(() => {
    if (stage.run && !stage.run.endedAt && stage.tab === "home") homeRunUpdate();
  }, 1000);
  // deep link: #memory, #files, #changes... opens that panel straight away,
  // so a link or a bookmark can point at one
  const linkTab = (location.hash || "").replace(/^#/, "");
  if (linkTab && stagePanes[linkTab]) {
    stageShow(linkTab, false);
    if (linkTab === "files") filesEnsure(true);
  }
  window.addEventListener("hashchange", () => {
    const t = (location.hash || "").replace(/^#/, "");
    if (t && stagePanes[t]) stageShow(t, false);
  });
  $("#input").focus();
}

/* ------------------------------------------------------------------ *
 * Memory graph
 *
 * Everything Saturday knows, drawn as one picture: files and folders from
 * the repo index, past sessions, remembered facts, installed skills. The
 * layout is a Barnes-Hut force simulation (no library - the app ships no
 * dependencies) and the render is additive, so dense regions bloom into
 * the bright hubs that make structure readable at a glance.
 * ------------------------------------------------------------------ */

const G_COLOR = {
  file:    [120, 205, 255],
  dir:     [255, 255, 255],
  session: [196, 132, 255],
  fact:    [255, 168,  84],
  skill:   [ 96, 240, 190],
};
const G_KIND_LABEL = { file: "files", dir: "folders", session: "chats", fact: "facts", skill: "skills" };

const mg = {
  on: false, loaded: false, loading: false,
  nodes: [], edges: [], deg: null,
  x: null, y: null, vx: null, vy: null, fixed: null,
  alpha: 0, view: { x: 0, y: 0, k: 1 },
  hover: -1, sel: -1, drag: -1, panning: null,
  hidden: new Set(), query: "", match: null,
  heat: null, raf: 0, canvas: null, ctx: null, dpr: 1,
  labels: true, err: "", idle: 0, named: new Set(), framed: false,
};

function mgRGBA(kind, a) {
  const c = G_COLOR[kind] || G_COLOR.file;
  return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")";
}

async function mgLoad(refresh) {
  if (mg.loading) return;
  mg.loading = true;
  mgStatus(refresh ? "reindexing the workspace…" : "reading the index…");
  try {
    const g = await api("/api/memgraph?sid=" + encodeURIComponent(state.sid || "") +
                        (refresh ? "&refresh=1" : ""));
    mgAdopt(g);
    mg.err = "";
  } catch (e) {
    mg.err = String(e.message || e);
    mgStatus("could not build the graph: " + mg.err);
  } finally {
    mg.loading = false;
  }
}

function mgAdopt(g) {
  const n = g.nodes.length;
  mg.nodes = g.nodes;
  mg.edges = g.edges;
  mg.loaded = true;
  mg.x = new Float32Array(n); mg.y = new Float32Array(n);
  mg.vx = new Float32Array(n); mg.vy = new Float32Array(n);
  mg.fixed = new Uint8Array(n);
  mg.deg = new Float32Array(n);
  mg.heat = new Float32Array(n);
  for (const e of mg.edges) { mg.deg[e.s] += e.w; mg.deg[e.t] += e.w; }
  // seed on a ring: a random cloud takes far longer to untangle than one
  // that already has every node outside every other node
  const R = 40 * Math.sqrt(n);
  for (let i = 0; i < n; i++) {
    const a = i * 2.399963;                       // golden angle: no clumps
    const r = R * Math.sqrt((i + 0.5) / n);
    mg.x[i] = r * Math.cos(a);
    mg.y[i] = r * Math.sin(a);
  }
  // name only the most connected things: every label is unreadable, none is
  // useless, and a fixed set stops names flickering as the layout settles
  mg.named = new Set(
    Array.from(mg.nodes.keys()).sort((a, b) => mg.deg[b] - mg.deg[a]).slice(0, 28)
  );
  mg.alpha = 1;
  mg.framed = false;
  mg.sel = -1; mg.hover = -1;
  mgFit();
  mgStats(g.stats || {});
  mgTick();
}

/* --- Barnes-Hut quadtree --------------------------------------------- */

function mgTree(order) {
  // flat-array quadtree. A cell is empty (occupied=0), a leaf (body>=0) or
  // internal (body=-1 with children). Children always get a higher index than
  // their parent, so one downward pass computes every centre of mass.
  const cap = order.length * 4 + 64;
  const ch = new Int32Array(cap * 4).fill(-1);
  const cx = new Float32Array(cap), cy = new Float32Array(cap), cm = new Float32Array(cap);
  const body = new Int32Array(cap).fill(-1);
  const cnt = new Float32Array(cap);
  const occupied = new Uint8Array(cap);
  const half = new Float32Array(cap), ox = new Float32Array(cap), oy = new Float32Array(cap);
  let used = 1;

  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (const i of order) {
    if (mg.x[i] < minx) minx = mg.x[i];
    if (mg.y[i] < miny) miny = mg.y[i];
    if (mg.x[i] > maxx) maxx = mg.x[i];
    if (mg.y[i] > maxy) maxy = mg.y[i];
  }
  half[0] = Math.max(maxx - minx, maxy - miny, 1) * 0.55;
  ox[0] = (minx + maxx) / 2;
  oy[0] = (miny + maxy) / 2;

  const quad = (cell, i) => (mg.x[i] > ox[cell] ? 1 : 0) + (mg.y[i] > oy[cell] ? 2 : 0);

  function makeChild(cell, q) {
    if (used >= cap) return -1;
    const c = used++;
    const h = half[cell] / 2;
    half[c] = h;
    ox[c] = ox[cell] + (q & 1 ? h : -h);
    oy[c] = oy[cell] + (q & 2 ? h : -h);
    ch[cell * 4 + q] = c;
    return c;
  }

  for (const i of order) {
    let cell = 0, depth = 0;
    while (true) {
      if (!occupied[cell]) { occupied[cell] = 1; body[cell] = i; cnt[cell] = 1; break; }
      if (body[cell] !== -1) {
        // leaf: push the sitting body down one level, then place ours below
        const j = body[cell];
        if (depth > 20) { cnt[cell] += 1; break; }  // coincident points: pile up
        body[cell] = -1;
        const qj = quad(cell, j);
        const cj = ch[cell * 4 + qj] !== -1 ? ch[cell * 4 + qj] : makeChild(cell, qj);
        if (cj === -1) { body[cell] = j; cnt[cell] += 1; break; }
        occupied[cj] = 1; body[cj] = j; cnt[cj] = 1;
      }
      const q = quad(cell, i);
      const c = ch[cell * 4 + q] !== -1 ? ch[cell * 4 + q] : makeChild(cell, q);
      if (c === -1) { cnt[cell] += 1; break; }
      cell = c; depth++;
    }
  }

  for (let c = used - 1; c >= 0; c--) {
    if (!occupied[c]) continue;
    if (body[c] !== -1) { cm[c] = cnt[c]; cx[c] = mg.x[body[c]]; cy[c] = mg.y[body[c]]; continue; }
    let m = cnt[c], sx = 0, sy = 0;
    for (let q = 0; q < 4; q++) {
      const k = ch[c * 4 + q];
      if (k === -1) continue;
      m += cm[k]; sx += cx[k] * cm[k]; sy += cy[k] * cm[k];
    }
    if (m > 0) { cm[c] = m; cx[c] = sx / m; cy[c] = sy / m; }
  }
  return { ch, cx, cy, cm, half, body, used };
}

const MG_THETA2 = 0.81;   // theta 0.9, squared

const mgStack = new Int32Array(4096);

function mgRepel(t, i, strength) {
  let fx = 0, fy = 0, sp = 0;
  mgStack[sp++] = 0;
  while (sp > 0) {
    const c = mgStack[--sp];
    const m = t.cm[c];
    if (m === 0) continue;
    let dx = t.cx[c] - mg.x[i], dy = t.cy[c] - mg.y[i];
    let d2 = dx * dx + dy * dy;
    if (d2 < 1e-6) { dx = (Math.random() - 0.5) * 0.1; dy = (Math.random() - 0.5) * 0.1; d2 = dx * dx + dy * dy + 1e-6; }
    const w = t.half[c] * 2;
    if (t.body[c] !== -1 || (w * w) / d2 < MG_THETA2) {
      if (t.body[c] === i) continue;
      const f = (strength * m) / d2;
      fx -= dx * f; fy -= dy * f;
      continue;
    }
    for (let q = 0; q < 4; q++) {
      const k = t.ch[c * 4 + q];
      if (k !== -1 && sp < mgStack.length) mgStack[sp++] = k;
    }
  }
  return [fx, fy];
}

function mgStep() {
  const n = mg.nodes.length;
  if (!n) return;
  const order = [];
  for (let i = 0; i < n; i++) if (!mg.hidden.has(mg.nodes[i].kind)) order.push(i);
  if (!order.length) return;
  const t = mgTree(order);
  const a = mg.alpha;

  for (const i of order) {
    const strength = 620 * (1 + Math.min(6, mg.deg[i] * 0.05));
    const [fx, fy] = mgRepel(t, i, strength);
    mg.vx[i] += fx * a * 0.02;
    mg.vy[i] += fy * a * 0.02;
  }
  for (const e of mg.edges) {
    const s = e.s, d = e.t;
    if (mg.hidden.has(mg.nodes[s].kind) || mg.hidden.has(mg.nodes[d].kind)) continue;
    let dx = mg.x[d] - mg.x[s], dy = mg.y[d] - mg.y[s];
    const dist = Math.sqrt(dx * dx + dy * dy) || 1e-3;
    const rest = 34;
    const k = 0.0038 * Math.min(6, e.w) * a;
    const f = (dist - rest) * k;
    dx /= dist; dy /= dist;
    mg.vx[s] += dx * f; mg.vy[s] += dy * f;
    mg.vx[d] -= dx * f; mg.vy[d] -= dy * f;
  }
  for (const i of order) {
    mg.vx[i] -= mg.x[i] * 0.0022 * a;      // gravity keeps it one body
    mg.vy[i] -= mg.y[i] * 0.0022 * a;
    if (mg.fixed[i]) { mg.vx[i] = mg.vy[i] = 0; continue; }
    mg.vx[i] *= 0.82; mg.vy[i] *= 0.82;
    mg.x[i] += Math.max(-40, Math.min(40, mg.vx[i]));
    mg.y[i] += Math.max(-40, Math.min(40, mg.vy[i]));
  }
  mg.alpha = Math.max(0, mg.alpha - 0.0035);
}

/* --- render ----------------------------------------------------------- */

function mgResize() {
  const c = mg.canvas;
  if (!c) return;
  const r = c.parentElement.getBoundingClientRect();
  mg.dpr = Math.min(2, window.devicePixelRatio || 1);
  c.width = Math.max(1, Math.round(r.width * mg.dpr));
  c.height = Math.max(1, Math.round(r.height * mg.dpr));
  c.style.width = r.width + "px";
  c.style.height = r.height + "px";
}

function mgFit() {
  const c = mg.canvas;
  if (!c || !mg.nodes.length) return;
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (let i = 0; i < mg.nodes.length; i++) {
    if (mg.hidden.has(mg.nodes[i].kind)) continue;
    minx = Math.min(minx, mg.x[i]); maxx = Math.max(maxx, mg.x[i]);
    miny = Math.min(miny, mg.y[i]); maxy = Math.max(maxy, mg.y[i]);
  }
  if (!isFinite(minx)) return;
  const w = c.width / mg.dpr, h = c.height / mg.dpr;
  const k = Math.min(w / (maxx - minx + 120), h / (maxy - miny + 120));
  mg.view.k = Math.max(0.02, Math.min(4, k));
  mg.view.x = w / 2 - ((minx + maxx) / 2) * mg.view.k;
  mg.view.y = h / 2 - ((miny + maxy) / 2) * mg.view.k;
}

function mgVisible(i) {
  if (mg.hidden.has(mg.nodes[i].kind)) return false;
  if (mg.match && !mg.match.has(i)) return false;
  return true;
}

function mgRadius(i) {
  const n = mg.nodes[i];
  const base = n.kind === "dir" ? 2.6 : n.kind === "session" ? 2.4 : 1.7;
  return base + Math.min(9, Math.sqrt(mg.deg[i]) * 0.55) + (n.weight || 1) * 0.25;
}

function mgDraw() {
  const ctx = mg.ctx, c = mg.canvas;
  if (!ctx) return;
  const w = c.width, h = c.height;
  const dark = document.documentElement.getAttribute("data-theme") !== "light";
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = dark ? "#08080b" : "#f7f5f0";
  ctx.fillRect(0, 0, w, h);
  if (!mg.nodes.length) return;

  const k = mg.view.k * mg.dpr;
  const ox = mg.view.x * mg.dpr, oy = mg.view.y * mg.dpr;
  const X = (i) => mg.x[i] * k + ox;
  const Y = (i) => mg.y[i] * k + oy;

  const focus = mg.sel >= 0 ? mg.sel : mg.hover;
  const near = new Set();
  if (focus >= 0) {
    near.add(focus);
    for (const e of mg.edges) {
      if (e.s === focus) near.add(e.t);
      else if (e.t === focus) near.add(e.s);
    }
  }

  ctx.globalCompositeOperation = dark ? "lighter" : "source-over";

  // edges
  ctx.lineWidth = Math.max(0.4, 0.55 * mg.dpr);
  for (const e of mg.edges) {
    if (!mgVisible(e.s) || !mgVisible(e.t)) continue;
    const lit = focus >= 0 && (e.s === focus || e.t === focus);
    if (focus >= 0 && !lit) {
      ctx.strokeStyle = dark ? "rgba(120,160,190,0.02)" : "rgba(60,70,90,0.03)";
    } else {
      const kd = mg.nodes[e.t].kind === "dir" ? mg.nodes[e.s].kind : mg.nodes[e.t].kind;
      const a = lit ? 0.7 : Math.min(0.34, 0.09 + e.w * 0.022);
      ctx.strokeStyle = mgRGBA(kd, dark ? a : a * 1.6);
    }
    ctx.beginPath();
    ctx.moveTo(X(e.s), Y(e.s));
    ctx.lineTo(X(e.t), Y(e.t));
    ctx.stroke();
  }

  // nodes
  for (let i = 0; i < mg.nodes.length; i++) {
    if (!mgVisible(i)) continue;
    const n = mg.nodes[i];
    const r = Math.max(0.9, mgRadius(i) * Math.sqrt(k) * 0.9);
    const dimmed = focus >= 0 && !near.has(i);
    const heat = mg.heat[i];
    const px = X(i), py = Y(i);

    if (!dimmed && (r > 3 || heat > 0.02)) {
      const glow = r * (heat > 0.02 ? 7 : 4);
      const g = ctx.createRadialGradient(px, py, 0, px, py, glow);
      const col = heat > 0.02 ? [255, 230, 120] : (G_COLOR[n.kind] || G_COLOR.file);
      g.addColorStop(0, "rgba(" + col[0] + "," + col[1] + "," + col[2] + "," + (dark ? 0.30 + heat * 0.5 : 0.16) + ")");
      g.addColorStop(1, "rgba(" + col[0] + "," + col[1] + "," + col[2] + ",0)");
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(px, py, glow, 0, 6.2832); ctx.fill();
    }
    ctx.fillStyle = dimmed ? mgRGBA(n.kind, 0.10)
                 : heat > 0.02 ? "rgba(255,238,170," + (0.7 + heat * 0.3) + ")"
                 : mgRGBA(n.kind, dark ? 0.92 : 0.85);
    ctx.beginPath(); ctx.arc(px, py, r, 0, 6.2832); ctx.fill();
  }

  // labels: only where they can be read
  ctx.globalCompositeOperation = "source-over";
  if (mg.labels) {
    ctx.font = (11 * mg.dpr) + "px ui-monospace, monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const boxes = [];   // drawn label rects: a name nobody can read is noise
    for (let i = 0; i < mg.nodes.length; i++) {
      if (!mgVisible(i)) continue;
      if (!mg.named.has(i) && i !== focus && !near.has(i)) continue;
      if (focus >= 0 && !near.has(i)) continue;
      const n = mg.nodes[i];
      const label = n.label.length > 26 ? n.label.slice(0, 25) + "…" : n.label;
      const py = Y(i) - mgRadius(i) * Math.sqrt(k) - 7 * mg.dpr;
      const px = X(i);
      const hw = ctx.measureText(label).width / 2 + 3 * mg.dpr;
      const hh = 8 * mg.dpr;
      if (boxes.some((b) => Math.abs(b[0] - px) < b[2] + hw && Math.abs(b[1] - py) < b[3] + hh)) continue;
      boxes.push([px, py, hw, hh]);
      ctx.lineWidth = 3 * mg.dpr;
      ctx.strokeStyle = dark ? "rgba(6,6,9,0.85)" : "rgba(250,248,244,0.9)";
      ctx.strokeText(label, px, py);
      ctx.fillStyle = dark ? "rgba(233,228,216,0.88)" : "rgba(42,39,33,0.9)";
      ctx.fillText(label, px, py);
    }
  }
}

function mgTick() {
  cancelAnimationFrame(mg.raf);
  const frame = () => {
    if (!mg.on) return;
    let busy = false;
    for (let s = 0; s < 2 && mg.alpha > 0.002; s++) { mgStep(); busy = true; }
    for (let i = 0; i < mg.heat.length; i++) {
      if (mg.heat[i] > 0.005) { mg.heat[i] *= 0.985; busy = true; }
      else if (mg.heat[i]) mg.heat[i] = 0;
    }
    mgDraw();
    if (busy || mg.drag >= 0 || mg.panning) mg.idle = 0;
    else {
      // the ring it starts from is nothing like where it ends up, so frame it
      // once the forces have actually resolved
      if (!mg.framed) { mg.framed = true; mgFit(); mgDraw(); }
      if (++mg.idle > 45) { mg.raf = 0; return; }     // settled: stop burning frames
    }
    mg.raf = requestAnimationFrame(frame);
  };
  mg.raf = requestAnimationFrame(frame);
}

function mgWake() {
  mg.idle = 0;
  if (!mg.raf && mg.on) mgTick();
}

/* --- picking + interaction -------------------------------------------- */

function mgPick(px, py) {
  const k = mg.view.k, ox = mg.view.x, oy = mg.view.y;
  let best = -1, bd = 18 * 18;
  for (let i = 0; i < mg.nodes.length; i++) {
    if (!mgVisible(i)) continue;
    const dx = (mg.x[i] * k + ox) - px, dy = (mg.y[i] * k + oy) - py;
    const d = dx * dx + dy * dy;
    const r = Math.max(6, mgRadius(i) * Math.sqrt(k * mg.dpr));
    if (d < Math.max(bd, r * r) && d < bd) { bd = d; best = i; }
  }
  return best;
}

function mgWire() {
  const c = mg.canvas;
  let last = null;

  c.addEventListener("pointerdown", (ev) => {
    c.setPointerCapture(ev.pointerId);
    const r = c.getBoundingClientRect();
    const px = ev.clientX - r.left, py = ev.clientY - r.top;
    const hit = mgPick(px, py);
    if (hit >= 0) { mg.drag = hit; mg.fixed[hit] = 1; mg.alpha = Math.max(mg.alpha, 0.25); }
    else mg.panning = { x: px, y: py, vx: mg.view.x, vy: mg.view.y, moved: false };
    last = { px, py };
    mgWake();
  });

  c.addEventListener("pointermove", (ev) => {
    const r = c.getBoundingClientRect();
    const px = ev.clientX - r.left, py = ev.clientY - r.top;
    if (mg.drag >= 0) {
      mg.x[mg.drag] = (px - mg.view.x) / mg.view.k;
      mg.y[mg.drag] = (py - mg.view.y) / mg.view.k;
      mg.alpha = Math.max(mg.alpha, 0.2);
      mgWake();
      return;
    }
    if (mg.panning) {
      mg.panning.moved = true;
      mg.view.x = mg.panning.vx + (px - mg.panning.x);
      mg.view.y = mg.panning.vy + (py - mg.panning.y);
      mgWake();
      return;
    }
    const hit = mgPick(px, py);
    if (hit !== mg.hover) { mg.hover = hit; mgTip(hit, px, py); mgWake(); }
    else if (hit >= 0) mgTip(hit, px, py);
    last = { px, py };
  });

  const release = (ev) => {
    if (mg.drag >= 0) { mg.fixed[mg.drag] = 0; mg.drag = -1; }
    else if (mg.panning && !mg.panning.moved && last) {
      const hit = mgPick(last.px, last.py);
      mg.sel = hit === mg.sel ? -1 : hit;
      mgDetail(mg.sel);
    }
    mg.panning = null;
    mgWake();
  };
  c.addEventListener("pointerup", release);
  c.addEventListener("pointercancel", release);
  c.addEventListener("pointerleave", () => { mg.hover = -1; mgTip(-1); mgWake(); });

  c.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const r = c.getBoundingClientRect();
    const px = ev.clientX - r.left, py = ev.clientY - r.top;
    const f = Math.exp(-ev.deltaY * 0.0016);
    const nk = Math.max(0.02, Math.min(8, mg.view.k * f));
    mg.view.x = px - (px - mg.view.x) * (nk / mg.view.k);
    mg.view.y = py - (py - mg.view.y) * (nk / mg.view.k);
    mg.view.k = nk;
    mgWake();
  }, { passive: false });

  c.addEventListener("dblclick", () => { mgFit(); mgWake(); });
}

function mgTip(i, px, py) {
  const t = $("#mgTip");
  if (!t) return;
  if (i < 0) { t.classList.add("hidden"); return; }
  const n = mg.nodes[i];
  const m = n.meta || {};
  const bits = [];
  if (n.kind === "file") bits.push(m.path || "", (m.lines || 0) + " terms");
  if (n.kind === "dir") bits.push(m.path || "");
  if (n.kind === "session") bits.push(m.turns + " turns", m.files + " files touched");
  if (n.kind === "fact") bits.push(m.text || "");
  t.innerHTML = '<b>' + escHtml(n.label) + '</b><span>' +
                escHtml(bits.filter(Boolean).join("  ·  ")) + '</span>';
  t.style.left = Math.round(px + 14) + "px";
  t.style.top = Math.round(py + 14) + "px";
  t.classList.remove("hidden");
}

function mgDetail(i) {
  const box = $("#mgDetail");
  if (!box) return;
  if (i < 0) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  const n = mg.nodes[i], m = n.meta || {};
  const links = [];
  for (const e of mg.edges) {
    const o = e.s === i ? e.t : e.t === i ? e.s : -1;
    if (o >= 0) links.push({ o, w: e.w, kind: e.kind });
  }
  links.sort((a, b) => b.w - a.w);
  const rows = links.slice(0, 14).map((l) =>
    '<button class="mg-link" data-i="' + l.o + '"><i class="mg-dot" style="background:' +
    mgRGBA(mg.nodes[l.o].kind, 1) + '"></i>' + escHtml(mg.nodes[l.o].label) +
    '<em>' + escHtml(l.kind) + '</em></button>').join("");
  box.innerHTML =
    '<header><i class="mg-dot" style="background:' + mgRGBA(n.kind, 1) + '"></i>' +
    escHtml(n.label) + '<button class="mg-x" id="mgClose" title="close">×</button></header>' +
    (m.path ? '<p class="mono">' + escHtml(m.path) + '</p>' : "") +
    (m.text ? '<p>' + escHtml(m.text) + '</p>' : "") +
    (m.symbols && m.symbols.length
      ? '<p class="mg-syms mono">' + m.symbols.map(escHtml).join("  ") + '</p>' : "") +
    '<div class="mg-links-h">' + links.length + ' connection' + (links.length === 1 ? "" : "s") + '</div>' +
    '<div class="mg-links">' + rows + '</div>' +
    (n.kind === "file" && m.path
      ? '<button class="mg-open" id="mgOpen">open in files</button>' : "");
  box.classList.remove("hidden");
  box.querySelectorAll(".mg-link").forEach((b) =>
    b.addEventListener("click", () => { mg.sel = +b.dataset.i; mgDetail(mg.sel); mgWake(); }));
  const x = $("#mgClose");
  if (x) x.addEventListener("click", () => { mg.sel = -1; mgDetail(-1); mgWake(); });
  const op = $("#mgOpen");
  if (op) op.addEventListener("click", () => { stageShow("files"); openWsFile(m.path); });
}

function mgStatus(text) {
  const s = $("#mgStatus");
  if (s) s.textContent = text;
}

function mgStats(stats) {
  const kinds = stats.kinds || {};
  const wrap = $("#mgLegend");
  if (wrap) {
    wrap.innerHTML = Object.keys(G_KIND_LABEL).map((k) => {
      const n = kinds[k] || 0;
      return '<button class="mg-chip' + (n ? "" : " empty") +
        (mg.hidden.has(k) ? " off" : "") + '" data-kind="' + k + '">' +
        '<i class="mg-dot" style="background:' + mgRGBA(k, 1) + '"></i>' +
        G_KIND_LABEL[k] + '<em>' + n + '</em></button>';
    }).join("");
    wrap.querySelectorAll(".mg-chip").forEach((b) => b.addEventListener("click", () => {
      const k = b.dataset.kind;
      if (mg.hidden.has(k)) mg.hidden.delete(k); else mg.hidden.add(k);
      b.classList.toggle("off", mg.hidden.has(k));
      mg.alpha = Math.max(mg.alpha, 0.3);
      mgWake();
    }));
  }
  const clashes = mg.edges.filter((e) => e.kind === "contradicts").length;
  mgStatus(stats.nodes + " things, " + stats.edges + " connections"
    + (clashes ? "  \u00b7  " + clashes + " in red disagree" : ""));
}

function mgSearch(q) {
  mg.query = q.trim().toLowerCase();
  if (!mg.query) { mg.match = null; mgWake(); return; }
  const hit = new Set();
  for (let i = 0; i < mg.nodes.length; i++) {
    const n = mg.nodes[i];
    if (n.label.toLowerCase().includes(mg.query) ||
        String((n.meta || {}).path || "").toLowerCase().includes(mg.query)) hit.add(i);
  }
  // keep one hop of context so a match is not a lone dot in the void
  const grown = new Set(hit);
  for (const e of mg.edges) {
    if (hit.has(e.s)) grown.add(e.t);
    if (hit.has(e.t)) grown.add(e.s);
  }
  mg.match = grown;
  mgWake();
}

/* attention: light up whatever the agent is reading right now */
function mgAttend(path) {
  if (!mg.loaded || !path) return;
  const want = String(path).replace(/\\/g, "/").replace(/^\.\//, "");
  for (let i = 0; i < mg.nodes.length; i++) {
    const p = (mg.nodes[i].meta || {}).path;
    if (p && (p === want || want.endsWith("/" + p) || p.endsWith("/" + want))) {
      mg.heat[i] = 1;
      mgWake();
      return;
    }
  }
}

function mgOpen() {
  mg.on = true;
  if (!mg.canvas) {
    mg.canvas = $("#mgCanvas");
    if (!mg.canvas) return;
    mg.ctx = mg.canvas.getContext("2d");
    mgWire();
    new ResizeObserver(() => { mgResize(); mgWake(); }).observe(mg.canvas.parentElement);
    const s = $("#mgSearch");
    if (s) s.addEventListener("input", () => mgSearch(s.value));
    const r = $("#mgRefresh");
    if (r) r.addEventListener("click", () => mgLoad(true));
    const f = $("#mgFit");
    if (f) f.addEventListener("click", () => { mgFit(); mgWake(); });
    const l = $("#mgLabels");
    if (l) l.addEventListener("click", () => {
      mg.labels = !mg.labels; l.classList.toggle("on", mg.labels); mgWake();
    });
  }
  mgResize();
  if (!mg.loaded) mgLoad(false);
  else { mgWake(); }
}

function mgClose() { mg.on = false; cancelAnimationFrame(mg.raf); mg.raf = 0; }

init();
})();
