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
  // What the model DID this turn — read the page, clicked a tab, searched the corpus. Shown as it
  // happens, because a harness that acts silently is one you cannot supervise.
  let steps = [];
  let actions = false;
  let debugOn = false;
  // A running clock, because "how long has this been going" is the question a blinking cursor
  // cannot answer. A screenshot plus two GPU swaps is genuinely minutes; the difference between
  // slow and stuck should be readable at a glance rather than inferred.
  let startedAt = 0;
  let ticker = null;

  function tick() {
    const el = root.querySelector(".elapsed");
    if (!el || !startedAt) return;
    const s = Math.round((Date.now() - startedAt) / 1000);
    el.textContent = s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
  }

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
        opacity:.35; padding:2px 4px; } .ico:hover { opacity:1; }
      .ico.on { opacity:1; }
      .step { font-size:11px; opacity:.7; margin:0 0 6px; padding-left:8px;
        border-left:2px solid rgba(128,128,128,.35); }
      .step.act { border-left-color:#b7791f; }
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
      .busy { margin-top:8px; font-size:11px; opacity:.75; }
      .elapsed { opacity:.55; margin-left:6px; font-variant-numeric:tabular-nums; }
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
        <button class="ico dbgt" title="Record every tool call and the full prompt">🐞</button>
        <button class="ico act" title="Allow Oracle to click and type on this site">🖐</button>
        <button class="ico newt" title="New topic (keeps the old one)">✚</button>
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
    if (steps.length) h += steps.map((s) =>
      `<p class="step${s.acting ? " act" : ""}">${esc(s.says)}</p>`).join("");
    if (streaming) {
      // The status line must appear even when text has ALREADY streamed. The model says what it is
      // about to do and then calls the tool, so `acc` is non-empty exactly when the slow thing
      // starts — and showing the status only when acc was empty meant a multi-minute screenshot
      // plus two GPU swaps was represented by a blinking caret and nothing else. "It feels like it
      // does something and stuck" is the correct reading of that UI; the caret says a token is
      // coming, and no token was coming.
      //
      // Not "consulting the corpus" either: the model decides whether to search at all.
      const busy = `<div class="busy"><span class="spin"></span> &nbsp;${esc(status || "thinking…")}` +
        `<span class="elapsed"></span></div>`;
      h += `<div class="msg"><div class="who">oracle</div><div class="bub">` +
        (acc ? md(acc) + busy : busy) + `</div></div>`;
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
      dbgEl.innerHTML = debugOn
        ? `<p class="empty">Recording is <b>on</b>. Ask something — every tool call, and every
           block of context that went into the prompt, will show up here.</p>`
        : `<p class="empty">Recording is <b>off</b>. Click the 🐞 in this panel's title bar (or
           tick <b>debug</b> in the Oracle toolbar popup), then ask again.</p>`;
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
    steps = []; dbgEvents = []; renderDbg();
    startedAt = Date.now();
    clearInterval(ticker);
    ticker = setInterval(tick, 1000);
    render(); tick();
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
  function paintAct() {
    const b = $(".act");
    b.classList.toggle("on", actions);
    b.title = actions
      ? "Oracle MAY click and type on this site — click to revoke"
      : "Oracle can look but not touch on this site — click to allow clicking and typing";
  }
  $(".act").addEventListener("click", () => {
    actions = !actions;
    paintAct();
    try { chrome.runtime.sendMessage({ type: "oracle:chatAllow", allow: actions }); } catch (_) {}
  });
  // Debug lives HERE as well as in the toolbar popup. The empty Debug tab told you to go and find
  // a checkbox in another surface, which is a poor answer to "why is this empty" when the switch
  // could simply be next to the thing it controls.
  function paintDbg() {
    const b = $(".dbgt");
    b.classList.toggle("on", debugOn);
    b.title = debugOn ? "Recording tool calls and prompts — click to stop"
                      : "Record every tool call and the full prompt (off)";
  }
  $(".dbgt").addEventListener("click", () => {
    debugOn = !debugOn;
    paintDbg();
    try { chrome.runtime.sendMessage({ type: "oracle:setDebug", on: debugOn }); } catch (_) {}
    renderDbg();
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
      actions = !!msg.actions;
      debugOn = !!msg.debug;
      paintAct();
      paintDbg();
      render();
      renderDbg();
      return;
    }
    if (msg.type === "oracle:chatAsk") { send(msg.message || "", msg.image || "", msg.thumb); return; }
    if (msg.type !== "oracle:chatEvent") return;
    const { event, data } = msg.ev || {};
    if (event === "debug") { dbgEvents.push(data || {}); renderDbg(); return; }
    if (event === "status") { status = data.text || ""; render(); return; }
    if (event === "tool_request") {
      (data.calls || []).forEach((c) => steps.push({ says: c.says, acting: c.acting }));
      render();
      return;
    }
    if (event === "sources") { sources = data.sources || []; cites = data.citations || []; return; }
    if (event === "delta") { acc += data.text || ""; render(); return; }
    if (event === "done") {
      streaming = false; go.disabled = false;
      clearInterval(ticker); ticker = null; startedAt = 0;
      if (acc.trim()) turns.push({ role: "assistant", content: acc, cites });
      acc = ""; render();
      return;
    }
    if (event === "error") {
      streaming = false; go.disabled = false;
      clearInterval(ticker); ticker = null; startedAt = 0;
      turns.push({ role: "assistant", content: "_" + (data && data.error || "error") + "_" });
      acc = ""; render();
    }
  });

  window.__oracleChat = { show: () => { host.style.display = ""; input.focus(); } };
  render(); renderDbg(); input.focus();
  try { chrome.runtime.sendMessage({ type: "oracle:chatLoad" }); } catch (_) {}
})();
