// Oracle Capture — in-page "explain" overlay. Injected on demand by the background worker.
// Idempotent: re-injection just re-uses the existing listener. Renders inside a Shadow DOM so the
// host page's CSS can't touch it. Does NO network (background does that) — only DOM.
(() => {
  if (window.__oracleOverlayInstalled) return;
  window.__oracleOverlayInstalled = true;

  let host, root;

  function ensureHost() {
    if (host && document.documentElement.contains(host)) return;
    host = document.createElement("div");
    host.style.cssText = "all:initial;position:absolute;z-index:2147483647;";
    root = host.attachShadow({ mode: "open" });
    document.documentElement.appendChild(host);
  }

  function place() {
    // glue to the current selection; fall back to viewport center
    const sel = window.getSelection();
    let top = window.scrollY + 80, left = window.scrollX + 40;
    if (sel && sel.rangeCount) {
      const r = sel.getRangeAt(0).getBoundingClientRect();
      if (r.width || r.height) {
        top = window.scrollY + r.bottom + 8;
        left = window.scrollX + r.left;
      }
    }
    const maxLeft = window.scrollX + document.documentElement.clientWidth - 420;
    host.style.top = top + "px";
    host.style.left = Math.max(window.scrollX + 8, Math.min(left, maxLeft)) + "px";
  }

  // tiny, safe markdown: escape first, then re-introduce a limited set of formatting
  function md(text) {
    let s = String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    s = s.replace(/```(\w*)\n([\s\S]*?)```/g, (_m, _l, code) => "<pre><code>" + code + "</code></pre>");
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/^\s*[-*]\s+(.*)$/gm, "<li>$1</li>").replace(/(<li>[\s\S]*?<\/li>)/g, "<ul>$1</ul>");
    s = s.replace(/\n{2,}/g, "</p><p>").replace(/\n/g, "<br>");
    return "<p>" + s + "</p>";
  }

  let bodyEl = null, dbgEl = null;
  // Every debug event, in arrival order, from BOTH sides — the extension says what it captured and
  // sent, the receiver says what it composed. Kept raw: a debug view that summarises is a debug
  // view you cannot trust to answer "was the page text actually in there?".
  let dbgEvents = [];

  // build the card shell ONCE (at loading) so streamed deltas only repaint .body — position,
  // scroll, and drag state survive token-by-token updates.
  function open() {
    ensureHost();
    place();
    root.innerHTML = `
      <style>
        ${OracleCite.CSS}
        /* :target does not fire inside a shadow root, so the jump highlight is class-driven */
        .oc-fn.oc-hit { background: rgba(255,220,80,.35); border-radius: 3px; }
        .card { width:400px; max-width:92vw; max-height:60vh; overflow:auto;
          font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
          background:#fff; color:#1a1a1a; border:1px solid #d0d0d0; border-radius:10px;
          box-shadow:0 8px 30px rgba(0,0,0,.25); }
        @media (prefers-color-scheme: dark) {
          .card { background:#1e1e1e; color:#e6e6e6; border-color:#3a3a3a; }
          pre,code { background:#2a2a2a !important; } a { color:#6ab0ff; }
        }
        .bar { display:flex; align-items:center; gap:8px; padding:8px 12px; cursor:move;
          border-bottom:1px solid rgba(128,128,128,.25); position:sticky; top:0; background:inherit; }
        .bar b { font-size:12px; letter-spacing:.02em; opacity:.85; flex:1; }
        .x { cursor:pointer; border:0; background:transparent; color:inherit; font-size:16px; opacity:.6; }
        .x:hover { opacity:1; }
        .body { padding:10px 14px; }
        .body p { margin:0 0 8px; } .body ul { margin:6px 0; padding-left:18px; }
        pre { background:#f4f4f4; padding:8px; border-radius:6px; overflow:auto; }
        code { background:#f0f0f0; padding:1px 4px; border-radius:4px; font-family:ui-monospace,monospace; }
        pre code { background:transparent; padding:0; }
        .src { margin-top:10px; padding-top:8px; border-top:1px solid rgba(128,128,128,.25);
          font-size:11px; opacity:.7; }
        .spin { display:inline-block; width:12px; height:12px; border:2px solid rgba(128,128,128,.4);
          border-top-color:#888; border-radius:50%; animation:sp .8s linear infinite; }
        @keyframes sp { to { transform:rotate(360deg); } }
        .caret { display:inline-block; width:6px; height:1.05em; vertical-align:text-bottom;
          background:currentColor; opacity:.55; animation:bl 1s steps(2) infinite; margin-left:1px; }
        @keyframes bl { 50% { opacity:0; } }
        .verdict { font-size:10px; font-weight:700; letter-spacing:.04em; padding:2px 6px;
          border-radius:10px; color:#fff; }
        .v-sup { background:#2e7d32; } .v-con { background:#c0392b; }
        .v-par { background:#b7791f; } .v-non { background:#6b7280; }
        .ground-btn { margin-top:10px; width:100%; padding:6px; border:0; border-radius:7px;
          background:#128a86; color:#fff; font:inherit; font-weight:600; cursor:pointer; }
        .ground-btn:hover { filter:brightness(1.08); }
        .tabs[hidden] { display:none; }   /* an author display: rule outranks UA [hidden] */
        .tabs { display:flex; gap:2px; padding:0 10px; border-bottom:1px solid rgba(128,128,128,.25);
          position:sticky; top:33px; background:inherit; }
        .tab { border:0; background:transparent; color:inherit; font:inherit; font-size:11px;
          padding:5px 9px; cursor:pointer; opacity:.55; border-bottom:2px solid transparent; }
        .tab:hover { opacity:.85; }
        .tab.on { opacity:1; border-bottom-color:#128a86; font-weight:600; }
        .tab .n { font-size:9px; opacity:.7; margin-left:3px; }
        .dbg { padding:8px 12px; font:11px/1.45 ui-monospace,monospace; }
        .dbg .ev { border-top:1px solid rgba(128,128,128,.2); padding:6px 0; }
        .dbg .ev:first-child { border-top:0; }
        .dbg .hd { cursor:pointer; display:flex; gap:6px; align-items:baseline; }
        .dbg .side { font-size:9px; padding:0 4px; border-radius:6px; background:rgba(128,128,128,.22); }
        .dbg .st { font-weight:700; }
        .dbg .meta { opacity:.65; font-size:10px; }
        .dbg pre { white-space:pre-wrap; word-break:break-word; max-height:340px; overflow:auto;
          margin:6px 0 0; font-size:10.5px; }
        .dbg .empty { opacity:.6; font-family:inherit; }
        .gsec { margin-top:12px; padding-top:10px; border-top:1px dashed rgba(128,128,128,.4); }
        .ghdr { font-size:11px; font-weight:700; opacity:.7; margin-bottom:6px; }
      </style>
      <div class="card">
        <div class="bar"><b class="ttl">Oracle</b><span class="verdict" hidden></span><button class="x" title="Close">×</button></div>
        <div class="tabs">
          <button class="tab on" data-pane="body">Answer</button>
          <button class="tab" data-pane="dbg">Debug<span class="n"></span></button>
        </div>
        <div class="body"></div>
        <div class="dbg" hidden></div>
      </div>`;
    bodyEl = root.querySelector(".body");
    dbgEl = root.querySelector(".dbg");
    root.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
      root.querySelectorAll(".tab").forEach((o) => o.classList.toggle("on", o === t));
      const wantDbg = t.dataset.pane === "dbg";
      bodyEl.hidden = wantDbg;
      dbgEl.hidden = !wantDbg;
    }));
    root.querySelector(".x").addEventListener("click", () => host.remove());
    makeDraggable(root.querySelector(".bar"));
    renderDbg();
  }

  function setBody(html) {
    if (!bodyEl) return;
    bodyEl.innerHTML = html;
    // A [n] link cannot use href="#fn" here: this card lives in a Shadow DOM, where fragment
    // navigation is a no-op (the browser would look for the id in the HOST document and, failing
    // that, jump the page). Scroll to the footnote inside the shadow root ourselves, and highlight
    // it briefly so the eye lands on the right line.
    bodyEl.querySelectorAll("a[data-oc-fn]").forEach((a) => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        const t = bodyEl.querySelector(`#${CSS.escape(a.getAttribute("href").slice(1))}`);
        if (!t) return;
        t.scrollIntoView({ block: "nearest", behavior: "smooth" });
        t.classList.add("oc-hit");
        setTimeout(() => t.classList.remove("oc-hit"), 1400);
      });
    });
  }

  function makeDraggable(handle) {
    let sx, sy, ox, oy, drag = false;
    handle.addEventListener("mousedown", (e) => {
      if (e.target.classList.contains("x")) return;
      drag = true; sx = e.clientX; sy = e.clientY;
      ox = parseFloat(host.style.left); oy = parseFloat(host.style.top);
      e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!drag) return;
      host.style.left = ox + (e.clientX - sx) + "px";
      host.style.top = oy + (e.clientY - sy) + "px";
    });
    window.addEventListener("mouseup", () => { drag = false; });
  }

  const CARET = '<span class="caret"></span>';

  // primary stream state (explain / ask / factcheck / vision), reset per invocation
  let acc = "", sources = null, reranked = true, curMode = "explain", thumb = null, primaryDone = false;
  // grounded sub-stream (vision → "Ground this in the corpus")
  let gStarted = false, gStreaming = false, gDone = false, gAcc = "", gSources = null, gReranked = true;
  // index-aligned [n] -> {doc,page,url} for the primary answer and the grounded sub-answer
  let cites = null, gCites = null;

  function esc(s) { return String(s).replace(/</g, "&lt;"); }
  function thumbHtml() {
    return (curMode === "vision" && thumb)
      ? `<img src="${thumb}" style="max-width:100%;border-radius:6px;margin-bottom:8px;display:block">` : "";
  }
  function footer(src, rr) {
    if (!src || !src.length) return "";
    return `<div class="src">Grounded in: ${src.map(esc).join(", ")}${rr ? "" : " (reranker busy)"}</div>`;
  }

  const VERDICTS = {
    "SUPPORTED": ["v-sup", "SUPPORTED"], "CONTRADICTED": ["v-con", "CONTRADICTED"],
    "PARTIAL": ["v-par", "PARTIAL"], "NOT COVERED": ["v-non", "NOT COVERED"],
  };
  // fact-check answers start with a [VERDICT] tag — lift it into a colored chip, strip from body
  function bodyText() {
    if (curMode !== "factcheck") return acc;
    const m = acc.match(/^\s*\[([A-Z ]+)\]/);
    const chip = root && root.querySelector(".verdict");
    if (m && chip) {
      const v = VERDICTS[m[1].trim()];
      if (v) { chip.className = "verdict " + v[0]; chip.textContent = v[1]; chip.hidden = false; }
    }
    return acc.replace(/^\s*\[[A-Z ]+\]\s*/, "");
  }

  // Wikipedia-style: [n] in the prose becomes a superscript link to a footnote, and the footnote
  // links into the corpus browser at the page the claim came from. Footnotes are rendered only when
  // the answer is COMPLETE — a half-streamed answer has no stable set of cited numbers.
  function withCites(text, citeList, prefix, done) {
    let body = md(text);
    if (!citeList || !citeList.length) return body;
    const pl = OracleCite.plan(text, citeList);   // excerpt -> sequential display number
    body = OracleCite.linkify(body, pl, prefix);
    return done ? body + OracleCite.footnotes(pl, prefix) : body;
  }

  function render() {
    let h = thumbHtml() + withCites(bodyText(), cites, "oc", primaryDone);
    // the plain "Grounded in:" line is redundant once numbered footnotes are shown
    h += primaryDone ? ((cites && cites.length) ? "" : footer(sources, reranked)) : CARET;
    // vision answers can be grounded: turn qwen3-vl's read of the pixels into a cited corpus answer
    if (curMode === "vision" && primaryDone && !gStarted && acc.trim())
      h += `<button class="ground-btn">⚓ Ground this in the corpus</button>`;
    if (gStarted) {
      h += `<div class="gsec"><div class="ghdr">⚓ Grounded in the corpus</div>`;
      h += gAcc ? withCites(gAcc, gCites, "ocg", gDone) : `<p><span class="spin"></span> &nbsp;retrieving…</p>`;
      if (gStreaming) h += CARET;
      if (gDone && !(gCites && gCites.length)) h += footer(gSources, gReranked);
      h += `</div>`;
    }
    setBody(h);
    const gb = root.querySelector(".ground-btn");
    if (gb) gb.addEventListener("click", () => {
      gStarted = true; gStreaming = true; gDone = false; render();
      try { chrome.runtime.sendMessage({ type: "oracle:groundVision", text: acc }); } catch (_) {}
    });
  }

  function renderDbg() {
    if (!dbgEl) return;
    const n = root.querySelector(".tab .n");
    if (n) n.textContent = dbgEvents.length ? `(${dbgEvents.length})` : "";
    if (!dbgEvents.length) {
      dbgEl.innerHTML = `<p class="empty">Nothing recorded. Turn on <b>Debug</b> in the Oracle
        popup, then run this again — every event and every block of injected context shows up
        here.</p>`;
      return;
    }
    dbgEl.innerHTML = dbgEvents.map((e, i) => {
      const { side, stage, text, sections, ...rest } = e;
      const meta = Object.entries(rest).filter(([, v]) => v !== undefined && v !== null && v !== "")
        .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join("  ");
      const detail = text != null ? `<pre hidden data-i="${i}">${esc(String(text))}</pre>` : "";
      const secs = sections ? `<div class="meta">${esc(sections.map((x) =>
        `${x.section} ${x.chars}c`).join(" · "))}</div>` : "";
      return `<div class="ev"><div class="hd" data-i="${i}">
          <span class="side">${esc(side || "receiver")}</span>
          <span class="st">${esc(stage || "?")}</span>
          ${text != null ? `<span class="meta">▸ ${String(text).length}c</span>` : ""}
        </div><div class="meta">${esc(meta)}</div>${secs}${detail}</div>`;
    }).join("");
    dbgEl.querySelectorAll(".hd").forEach((h) => h.addEventListener("click", () => {
      const pre = dbgEl.querySelector(`pre[data-i="${h.dataset.i}"]`);
      if (pre) pre.hidden = !pre.hidden;
    }));
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "oracle:loading") {
      acc = ""; sources = null; reranked = true; curMode = msg.mode || "explain"; thumb = msg.thumb || null;
      primaryDone = false; gStarted = false; gStreaming = false; gDone = false; gAcc = ""; gSources = null; gReranked = true;
      cites = null; gCites = null; dbgEvents = [];
      open();
      root.querySelector(".ttl").textContent =
        curMode === "factcheck" ? "Oracle · fact-check" : curMode === "vision" ? "Oracle · vision" : "Oracle";
      const loading = curMode === "vision" ? "Looking…"
        : curMode === "factcheck" ? "Checking against the corpus…" : "Consulting the corpus…";
      setBody(thumbHtml() + `<p><span class="spin"></span> &nbsp;${loading}</p>`);
      return;
    }
    if (msg.type !== "oracle:event") return;
    const { event, data } = msg.ev || {};
    // debug events are handled for BOTH streams before the mode split — the grounded sub-stream
    // reports its own prompt, and those belong in the same tab, in arrival order
    if (event === "debug") { dbgEvents.push(data || {}); renderDbg(); return; }
    if (msg.mode === "ground") {                              // grounded sub-stream
      if (event === "status") { setBody(thumbHtml() + `<p><span class="spin"></span> &nbsp;${esc(data.text||"")}</p>`); return; }
    if (event === "sources") { gSources = data.sources || []; gCites = data.citations || []; gReranked = data.reranked !== false; render(); }
      else if (event === "delta") { gAcc += data.text || ""; render(); }
      else if (event === "done") { gStreaming = false; gDone = true; render(); }
      else if (event === "error") { gStreaming = false; gDone = true; gAcc += "\n\n_" + (data.error || "error") + "_"; render(); }
      return;
    }
    // a GPU swap takes 30-60s; say so, or it is indistinguishable from a hang
    if (event === "status") { setBody(thumbHtml() + `<p><span class="spin"></span> &nbsp;${esc(data.text||"")}</p>`); return; }
    if (event === "sources") {
      sources = data.sources || []; cites = data.citations || []; reranked = data.reranked !== false;
      // Retrieval is done but the first token is still ~40 s away on this box. Saying so is the
      // difference between "working" and "hung" — the card used to hold one static line for the
      // whole 80 s and looked identical to a stall.
      if (acc) render();
      else setBody(thumbHtml() + `<p><span class="spin"></span> &nbsp;retrieved ${sources.length}
        source${sources.length === 1 ? "" : "s"} — writing the answer…</p>`);
      return;
    }
    else if (event === "delta") { acc += data.text || ""; render(); }
    else if (event === "done") { primaryDone = true; render(); }
    else if (event === "error") { primaryDone = true; acc = ""; setBody(thumbHtml() + `<p>${esc(data && data.error || "error")}</p>`); }
  });
})();
