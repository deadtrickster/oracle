// Oracle chat — a persistent panel, one conversation per HOST.
//
// Deliberately not the same object as overlay.js. That card is transient and glued to a selection:
// it opens on an action, shows one answer, and is meant to be dismissed. A conversation is the
// opposite — it stays, it scrolls, it remembers, and reopening it must show what was already said.
// Sharing one component would have meant a card that is sometimes ephemeral and sometimes not,
// which is how both behaviours end up half-right.
//
// Scoped to the host, not the tab: reading a run report, then the docs, then another run is one
// line of thought about one system. The transcript lives on the receiver, so it survives the tab,
// the browser, and the machine being turned off.
(() => {
  if (window.__oracleChat) { window.__oracleChat.show(); return; }

  const host = document.createElement("div");
  host.style.cssText = "all:initial;position:fixed;z-index:2147483646;right:18px;bottom:18px;";
  const root = host.attachShadow({ mode: "open" });
  document.documentElement.appendChild(host);

  let turns = [];          // {role, content, cites?}
  let streaming = false;
  let acc = "";
  let cites = null, sources = null;
  let dbgEvents = [];
  let status = "";

  root.innerHTML = `
    <style>
      ${OracleCite.CSS}
      .oc-fn.oc-hit { background: rgba(255,220,80,.35); border-radius:3px; }
      .panel { width:400px; max-width:94vw; height:520px; max-height:78vh; display:flex;
        flex-direction:column; font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
        background:#fff; color:#1a1a1a; border:1px solid #d0d0d0; border-radius:12px;
        box-shadow:0 10px 40px rgba(0,0,0,.28); overflow:hidden; }
      @media (prefers-color-scheme: dark) {
        .panel { background:#1e1e1e; color:#e6e6e6; border-color:#3a3a3a; }
        pre,code { background:#2a2a2a !important; } a { color:#6ab0ff; }
        .in { background:#141414 !important; border-color:#3a3a3a !important; color:#e6e6e6; }
        .msg.me .bub { background:#22333b !important; }
      }
      .bar { display:flex; align-items:center; gap:8px; padding:8px 12px; cursor:move; flex:none;
        border-bottom:1px solid rgba(128,128,128,.25); }
      .bar b { font-size:12px; flex:1; letter-spacing:.02em; }
      .bar .h { font-weight:400; opacity:.6; font-size:11px; }
      .ico { cursor:pointer; border:0; background:transparent; color:inherit; font-size:14px;
        opacity:.6; padding:2px 4px; } .ico:hover { opacity:1; }
      .tabs { display:flex; gap:2px; padding:0 10px; flex:none;
        border-bottom:1px solid rgba(128,128,128,.25); }
      .tab { border:0; background:transparent; color:inherit; font:inherit; font-size:11px;
        padding:5px 9px; cursor:pointer; opacity:.55; border-bottom:2px solid transparent; }
      .tab.on { opacity:1; border-bottom-color:#128a86; font-weight:600; }
      .scroll[hidden], .dbg[hidden] { display:none; }   /* author rules outrank UA [hidden] */
      .scroll { flex:1; overflow:auto; padding:10px 12px; }
      .msg { margin:0 0 10px; }
      .msg .who { font-size:10px; opacity:.5; margin-bottom:2px; }
      .msg .bub { padding:7px 10px; border-radius:9px; background:rgba(128,128,128,.10); }
      .msg.me .bub { background:#e8f3f2; }
      .thumb { display:block; max-width:100%; max-height:180px; border-radius:6px; margin-bottom:6px;
        border:1px solid rgba(128,128,128,.35); }
      .msg p { margin:0 0 6px; } .msg p:last-child { margin:0; }
      .msg ul { margin:5px 0; padding-left:18px; }
      pre { background:#f4f4f4; padding:8px; border-radius:6px; overflow:auto; }
      code { background:#f0f0f0; padding:1px 4px; border-radius:4px; font-family:ui-monospace,monospace; }
      pre code { background:transparent; padding:0; }
      .src { margin-top:6px; font-size:10px; opacity:.65; }
      .foot { flex:none; border-top:1px solid rgba(128,128,128,.25); padding:8px; display:flex; gap:6px; }
      .in { flex:1; resize:none; height:38px; max-height:120px; padding:8px 9px; border-radius:8px;
        border:1px solid #ccc; background:#fff; font:inherit; color:inherit; }
      .go { border:0; border-radius:8px; padding:0 13px; background:#128a86; color:#fff;
        font:inherit; font-weight:600; cursor:pointer; }
      .go[disabled] { opacity:.5; cursor:default; }
      .spin { display:inline-block; width:11px; height:11px; border:2px solid rgba(128,128,128,.4);
        border-top-color:#888; border-radius:50%; animation:sp .8s linear infinite; }
      @keyframes sp { to { transform:rotate(360deg); } }
      .caret { display:inline-block; width:6px; height:1.05em; vertical-align:text-bottom;
        background:currentColor; opacity:.55; animation:bl 1s steps(2) infinite; }
      @keyframes bl { 50% { opacity:0; } }
      .empty { opacity:.6; font-size:12px; }
      .dbg { flex:1; overflow:auto; padding:8px 12px; font:11px/1.45 ui-monospace,monospace; }
      .dbg .ev { border-top:1px solid rgba(128,128,128,.2); padding:6px 0; }
      .dbg .hd { cursor:pointer; display:flex; gap:6px; align-items:baseline; }
      .dbg .side { font-size:9px; padding:0 4px; border-radius:6px; background:rgba(128,128,128,.22); }
      .dbg .st { font-weight:700; } .dbg .meta { opacity:.65; font-size:10px; }
      .dbg pre { white-space:pre-wrap; word-break:break-word; max-height:320px; overflow:auto;
        margin:6px 0 0; font-size:10.5px; }
    </style>
    <div class="panel">
      <div class="bar">
        <b>Oracle · chat <span class="h"></span></b>
        <button class="ico newt" title="New topic (keeps the old one)">⎌</button>
        <button class="ico min" title="Hide">–</button>
      </div>
      <div class="tabs">
        <button class="tab on" data-pane="chat">Conversation</button>
        <button class="tab" data-pane="dbg">Debug<span class="n"></span></button>
      </div>
      <div class="scroll"></div>
      <div class="dbg" hidden></div>
      <div class="foot">
        <textarea class="in" rows="1" placeholder="Ask about this site… (Enter to send)"></textarea>
        <button class="go">Ask</button>
      </div>
    </div>`;

  const $ = (s) => root.querySelector(s);
  const scroll = $(".scroll"), dbgEl = $(".dbg"), input = $(".in"), go = $(".go");
  const esc = (s) => String(s).replace(/</g, "&lt;");

  function md(text) {
    let s = esc(text);
    s = s.replace(/```(\w+)?\n([\s\S]*?)```/g, (_, l, c) => `<pre><code>${c}</code></pre>`);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    s = s.replace(/^\s*[-*]\s+(.*)$/gm, "<li>$1</li>").replace(/(<li>[\s\S]*?<\/li>)/g, "<ul>$1</ul>");
    s = s.replace(/\n{2,}/g, "</p><p>").replace(/\n/g, "<br>");
    return "<p>" + s + "</p>";
  }

  function bubble(t, i) {
    let body = md(t.content);
    if (t.role === "user") {
      body = `<p>${esc(t.content)}</p>`;
    } else if (t.cites && t.cites.length) {
      const pl = OracleCite.plan(t.content, t.cites);   // excerpt -> sequential display number
      body = OracleCite.linkify(body, pl, "occ" + i) + OracleCite.footnotes(pl, "occ" + i);
    }
    const thumb = t.thumb ? `<img class="thumb" src="${t.thumb}">` : "";
    return `<div class="msg ${t.role === "user" ? "me" : ""}">
      <div class="who">${t.role === "user" ? "you" : "oracle"}</div>
      <div class="bub">${thumb}${body}</div></div>`;
  }

  function render() {
    let h = turns.map(bubble).join("");
    if (streaming) {
      h += `<div class="msg"><div class="who">oracle</div><div class="bub">` +
        (acc ? md(acc) + `<span class="caret"></span>`
             // a GPU swap runs for minutes; the status line is the difference between "working"
             // and "hung", and it is the same channel the other cards use
             : `<span class="spin"></span> &nbsp;${esc(status || "consulting the corpus…")}`) +
        `</div></div>`;
    }
    if (!h) h = `<p class="empty">Ask anything about this site. Answers are grounded in your
      offline corpus and cited; the page you are on is used as context, not as evidence.
      The conversation is kept per site and survives restarts.</p>`;
    scroll.innerHTML = h;
    // fragment links are a no-op inside a shadow root — scroll to the footnote ourselves
    scroll.querySelectorAll("a[data-oc-fn]").forEach((a) => a.addEventListener("click", (e) => {
      e.preventDefault();
      const t = scroll.querySelector(`#${CSS.escape(a.getAttribute("href").slice(1))}`);
      if (!t) return;
      t.scrollIntoView({ block: "nearest", behavior: "smooth" });
      t.classList.add("oc-hit");
      setTimeout(() => t.classList.remove("oc-hit"), 1400);
    }));
    scroll.scrollTop = scroll.scrollHeight;
  }

  function renderDbg() {
    const n = root.querySelector(".tab .n");
    if (n) n.textContent = dbgEvents.length ? `(${dbgEvents.length})` : "";
    if (!dbgEvents.length) {
      dbgEl.innerHTML = `<p class="empty">Nothing recorded. Turn on <b>Debug</b> in the Oracle
        popup and ask again — every event, and every block of context that went into the prompt,
        shows up here.</p>`;
      return;
    }
    dbgEl.innerHTML = dbgEvents.map((e, i) => {
      const { side, stage, text, sections, ...rest } = e;
      const meta = Object.entries(rest).filter(([, v]) => v !== undefined && v !== null && v !== "")
        .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join("  ");
      return `<div class="ev"><div class="hd" data-i="${i}">
          <span class="side">${esc(side || "receiver")}</span>
          <span class="st">${esc(stage || "?")}</span>
          ${text != null ? `<span class="meta">▸ ${String(text).length}c</span>` : ""}
        </div><div class="meta">${esc(meta)}</div>
        ${text != null ? `<pre hidden data-i="${i}">${esc(String(text))}</pre>` : ""}</div>`;
    }).join("");
    dbgEl.querySelectorAll(".hd").forEach((h) => h.addEventListener("click", () => {
      const pre = dbgEl.querySelector(`pre[data-i="${h.dataset.i}"]`);
      if (pre) pre.hidden = !pre.hidden;
    }));
  }

  function send(preset, image, thumb) {
    const q = preset !== undefined ? preset : input.value.trim();
    // A region with no typed question is a legitimate turn — the picture IS the question — so only
    // a typed-and-empty send is a no-op.
    if (streaming || (!q && !image)) return;
    if (preset === undefined) input.value = "";
    turns.push({ role: "user", content: q || "(region sent)", thumb });
    streaming = true; acc = ""; status = ""; cites = null; sources = null;
    dbgEvents = []; renderDbg(); render();
    go.disabled = true;
    try { chrome.runtime.sendMessage({ type: "oracle:chat", message: q, image }); } catch (_) {}
  }

  root.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
    root.querySelectorAll(".tab").forEach((o) => o.classList.toggle("on", o === t));
    const wantDbg = t.dataset.pane === "dbg";
    scroll.hidden = wantDbg;
    dbgEl.hidden = !wantDbg;
  }));

  go.addEventListener("click", () => send());
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  $(".min").addEventListener("click", () => { host.style.display = "none"; });
  $(".newt").addEventListener("click", () => {
    if (streaming) return;
    turns = []; render();
    try { chrome.runtime.sendMessage({ type: "oracle:chatReset" }); } catch (_) {}
  });

  // drag by the title bar
  (() => {
    const bar = $(".bar");
    let sx, sy, ox, oy, on = false;
    bar.addEventListener("mousedown", (e) => {
      if (e.target.classList.contains("ico")) return;
      on = true; sx = e.clientX; sy = e.clientY;
      const r = host.getBoundingClientRect(); ox = r.left; oy = r.top;
      host.style.right = "auto"; host.style.bottom = "auto";
      host.style.left = ox + "px"; host.style.top = oy + "px";
      e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!on) return;
      host.style.left = ox + e.clientX - sx + "px";
      host.style.top = oy + e.clientY - sy + "px";
    });
    window.addEventListener("mouseup", () => { on = false; });
  })();

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "oracle:chatHistory") {
      turns = (msg.turns || []).map((t) => ({ role: t.role, content: t.content }));
      root.querySelector(".bar .h").textContent = msg.host ? `· ${msg.host}` : "";
      render();
      return;
    }
    if (msg.type === "oracle:chatAsk") { send(msg.message || "", msg.image || "", msg.thumb); return; }
    if (msg.type !== "oracle:chatEvent") return;
    const { event, data } = msg.ev || {};
    if (event === "debug") { dbgEvents.push(data || {}); renderDbg(); return; }
    if (event === "status") { status = data.text || ""; render(); return; }
    if (event === "sources") { sources = data.sources || []; cites = data.citations || []; return; }
    if (event === "delta") { acc += data.text || ""; render(); return; }
    if (event === "done") {
      streaming = false; go.disabled = false;
      if (acc.trim()) turns.push({ role: "assistant", content: acc, cites });
      acc = ""; render();
      return;
    }
    if (event === "error") {
      streaming = false; go.disabled = false;
      turns.push({ role: "assistant", content: "_" + (data && data.error || "error") + "_" });
      acc = ""; render();
    }
  });

  window.__oracleChat = { show: () => { host.style.display = ""; input.focus(); } };
  render(); renderDbg(); input.focus();
  try { chrome.runtime.sendMessage({ type: "oracle:chatLoad" }); } catch (_) {}
})();
