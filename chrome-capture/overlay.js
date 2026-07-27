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

  let bodyEl = null;

  // build the card shell ONCE (at loading) so streamed deltas only repaint .body — position,
  // scroll, and drag state survive token-by-token updates.
  function open() {
    ensureHost();
    place();
    root.innerHTML = `
      <style>
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
      </style>
      <div class="card">
        <div class="bar"><b class="ttl">Oracle</b><span class="verdict" hidden></span><button class="x" title="Close">×</button></div>
        <div class="body"></div>
      </div>`;
    bodyEl = root.querySelector(".body");
    root.querySelector(".x").addEventListener("click", () => host.remove());
    makeDraggable(root.querySelector(".bar"));
  }

  function setBody(html) {
    if (bodyEl) bodyEl.innerHTML = html;
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

  // streaming state, reset per invocation
  let acc = "", sources = null, reranked = true, curMode = "explain";

  function esc(s) { return String(s).replace(/</g, "&lt;"); }
  function footer() {
    if (!sources || !sources.length) return "";
    return `<div class="src">Grounded in: ${sources.map(esc).join(", ")}` +
           `${reranked ? "" : " (reranker busy)"}</div>`;
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
  function paint(streaming) {
    setBody(md(bodyText()) + (streaming ? `<span class="caret"></span>` : "") + footer());
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "oracle:loading") {
      acc = ""; sources = null; reranked = true; curMode = msg.mode || "explain";
      open();
      root.querySelector(".ttl").textContent = curMode === "factcheck" ? "Oracle · fact-check" : "Oracle";
      setBody(`<p><span class="spin"></span> &nbsp;${curMode === "factcheck"
        ? "Checking against the corpus…" : "Consulting the corpus…"}</p>`);
      return;
    }
    if (msg.type !== "oracle:event") return;
    const { event, data } = msg.ev || {};
    if (event === "sources") {
      sources = data.sources || [];
      reranked = data.reranked !== false;
      if (acc) paint(true);
    } else if (event === "delta") {
      acc += data.text || "";
      paint(true);
    } else if (event === "done") {
      paint(false);
    } else if (event === "error") {
      setBody(`<p>${esc(data && data.error || "error")}</p>`);
    }
  });
})();
