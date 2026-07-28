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
  // Marked so a screenshot can hide it. Oracle's own UI must never appear in a picture Oracle is
  // about to reason about — it would be describing itself, and worse, describing a stale copy of
  // its own last answer as if it were part of the page.
  host.dataset.oracleUi = "chat";
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
  let live = [];              // prose and steps, in the order they happened
  let confirming = null;      // an action waiting on the user's key, in "confirm" mode
  let queued = [];            // questions typed while a turn was still running
  let stepsOpen = false;      // folded once the turn is done; click to reopen
  let turnFailed = false;     // ...but never folded when it ended badly
  let actions = "off";      // "off" | "confirm" | "allow"
  let debugOn = false;
  // A running clock, because "how long has this been going" is the question a blinking cursor
  // cannot answer. A screenshot plus two GPU swaps is genuinely minutes; the difference between
  // slow and stuck should be readable at a glance rather than inferred.
  let startedAt = 0;
  let ticker = null;
  // Two conversations per site: the one you type in, and the one the quick surfaces write to.
  // Separate because "what does this phrase mean" and "explain this whole run" are not one thread,
  // and interleaving them would make both harder to read.
  let session = "main";
  let currentHost = "";

  // Last sign of life from the receiver. A turn is legitimately slow — a GPU swap and a vision read
  // are minutes — but SILENT for minutes is different, and the panel used to be unable to tell the
  // difference. If the service worker is killed mid-turn nothing ever arrives, and without this the
  // spinner runs until the tab is closed.
  let lastEventAt = 0;
  const STALL_WARN_MS = 150000;
  const STALL_GIVEUP_MS = 420000;

  function tick() {
    const el = root.querySelector(".elapsed");
    if (!el || !startedAt) return;
    const s = Math.round((Date.now() - startedAt) / 1000);
    el.textContent = s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
    const quiet = Date.now() - (lastEventAt || startedAt);
    if (streaming && quiet > STALL_GIVEUP_MS) {
      streaming = false;
      go.disabled = false;
      clearInterval(ticker); ticker = null; startedAt = 0;
      turns.push({ role: "assistant", content:
        "_No response for " + Math.round(quiet / 60000) + " minutes. The browser most likely " +
        "dropped the connection to the receiver (Chrome can stop the extension's background " +
        "worker during a long request). Your question was recorded — ask again to continue._" });
      acc = ""; render();
    } else if (streaming && quiet > STALL_WARN_MS && !status.startsWith("still working")) {
      status = `still working — nothing heard for ${Math.round(quiet / 1000)}s`;
      render();
    }
  }

  root.innerHTML = `
    <style>
      ${OracleCite.CSS}
      .oc-fn.oc-hit { background: rgba(255,220,80,.35); border-radius:3px; }
      .panel { width:400px; max-width:94vw; height:520px; max-height:88vh; display:flex;
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
      .ico.ask { opacity:1; }
      .step { font-size:11px; opacity:.7; margin:0 0 6px; padding-left:8px;
        border-left:2px solid rgba(128,128,128,.35); }
      .step.act { border-left-color:#b7791f; }
      /* The pending-action bar. Deliberately the loudest thing in the panel: it is the only
         element that is waiting on the user rather than reporting to them, and a confirmation you
         can miss is a confirmation that trains you to hit Enter without reading. */
      .confirm { border:1px solid #b7791f; background:#2a2113; border-radius:6px;
                 padding:8px 10px; margin:0 0 8px; }
      .confirm.bad { border-color:#a33; background:#2a1616; }
      .confirm .ca { font-size:12px; font-weight:600; color:#f5c26b; }
      .confirm .ct { font-size:12px; margin-top:3px; word-break:break-word; }
      .confirm .cb { margin-top:7px; display:flex; gap:6px; }
      .confirm button { font-size:11px; padding:3px 9px; border-radius:4px; cursor:pointer;
                        border:1px solid #555; background:#333; color:#eee; }
      .confirm button.go { background:#b7791f; border-color:#b7791f; color:#1a1a1a;
                           font-weight:600; }
      .step.bad { border-left-color:#c0392b; opacity:.85; }
      .step .why { display:block; opacity:.7; font-size:10px; margin-top:2px; }
      .step.fold { cursor:pointer; border-left-color:transparent; opacity:.55; }
      .step.fold:hover { opacity:.9; }
      .step.res .more { margin-left:8px; opacity:.55; text-decoration:underline; cursor:pointer; }
      .step.res .more:hover { opacity:1; }
      .rawout { font:10px/1.4 ui-monospace,monospace; white-space:pre-wrap; word-break:break-word;
        max-height:220px; overflow:auto; margin:2px 0 8px 10px; padding:6px 8px; opacity:.8;
        background:rgba(128,128,128,.10); border-radius:5px; }
      .rawout[hidden] { display:none; }
      .tabs { display:flex; gap:2px; padding:0 10px; flex:none;
        border-bottom:1px solid rgba(128,128,128,.25); }
      .tab { border:0; background:transparent; color:inherit; font:inherit; font-size:11px;
        padding:5px 9px; cursor:pointer; opacity:.55; border-bottom:2px solid transparent; }
      .tab.on { opacity:1; border-bottom-color:#128a86; font-weight:600; }
      .scroll[hidden], .dbg[hidden], .ses[hidden] { display:none; }  /* outranks UA [hidden] */
      .ses { flex:1; overflow:auto; padding:8px 12px; }
      .ses .row { display:flex; align-items:baseline; gap:8px; padding:7px 0;
        border-bottom:1px solid rgba(128,128,128,.18); }
      .ses .h { flex:1; font-weight:600; word-break:break-all; }
      .ses .h .me { font-weight:400; opacity:.6; font-size:10px; margin-left:6px; }
      .ses .meta { font-size:10px; opacity:.6; white-space:nowrap; }
      .ses .del { border:0; background:transparent; color:#c0392b; cursor:pointer; font:inherit;
        font-size:11px; opacity:.75; padding:2px 4px; }
      .ses .del:hover { opacity:1; text-decoration:underline; }
      .scroll { flex:1; overflow:auto; padding:10px 12px; }
      .msg { margin:0 0 10px; }
      .msg .who { font-size:10px; opacity:.5; margin-bottom:2px; }
      .msg .bub { padding:7px 10px; border-radius:9px; background:rgba(128,128,128,.10); }
      .msg.me .bub { background:#e8f3f2; }
      .bub.queued { opacity:.55; border:1px dashed rgba(128,128,128,.5); }
      .thumb { display:block; max-width:100%; max-height:180px; border-radius:6px; margin-bottom:6px;
        border:1px solid rgba(128,128,128,.35); cursor:zoom-in; }
      .bub.tool { font:11px/1.45 ui-monospace,monospace; opacity:.8; white-space:pre-wrap;
        word-break:break-word; }
      .sesbar { display:flex; gap:6px; padding:5px 12px; font-size:11px; align-items:center;
        border-bottom:1px solid rgba(128,128,128,.18); }
      .sesbar b { font-weight:600; }
      .sesbar .pick { border:0; background:rgba(128,128,128,.16); color:inherit; font:inherit;
        border-radius:10px; padding:2px 9px; cursor:pointer; opacity:.65; display:inline-flex;
        align-items:center; gap:5px; }
      .sesbar .pick.on { opacity:1; background:#128a86; color:#fff; }
      /* The × only appears on the session you are looking at, and only once it has something to
         clear. An always-visible destructive control on every tab invites the accident. */
      .sesbar .pick .x { display:none; opacity:.7; font-size:12px; line-height:1; }
      .sesbar .pick.on .x { display:inline; }
      .sesbar .pick .x:hover { opacity:1; }
      .sesbar .pick .x.armed { color:#ffd7d0; font-weight:700; }
      .msg p { margin:0 0 6px; } .msg p:last-child { margin:0; }
      /* Tight: these are short factual bullets in a narrow panel, not prose. The default list
         spacing plus a paragraph margin made every item look like its own section. */
      .msg ul, .msg ol { margin:4px 0; padding-left:17px; }
      .msg li { margin:1px 0; }
      .msg li > p { margin:0; }
      /* Headings, sized for a 400px panel: the point is hierarchy, not scale. A model's "###" in
         here is a section label, and rendering it at document sizes would shout. */
      .mdh { font-weight:700; margin:9px 0 4px; line-height:1.3; }
      .mdh:first-child { margin-top:0; }
      .mdh.h1 { font-size:14px; }
      .mdh.h2 { font-size:13px; }
      .mdh.h3 { font-size:12px; opacity:.85; letter-spacing:.02em; }
      /* A comparison table in a 400px panel will not fit, and squeezing it makes it unreadable —
         so let it keep its width and scroll sideways inside its own box, rather than forcing the
         whole conversation to scroll. */
      .tw { overflow-x:auto; margin:6px 0; }
      .msg table { border-collapse:collapse; font-size:11px; line-height:1.35; }
      .msg th, .msg td { border:1px solid rgba(128,128,128,.30); padding:3px 7px;
        text-align:left; white-space:nowrap; vertical-align:top; }
      .msg th { font-weight:700; background:rgba(128,128,128,.12); }
      .msg tbody tr:nth-child(even) td { background:rgba(128,128,128,.06); }
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
      .resume { display:block; margin-top:8px; border:0; border-radius:7px; padding:5px 10px;
        background:#128a86; color:#fff; font:inherit; font-size:12px; font-weight:600;
        cursor:pointer; }
      .resume:hover { filter:brightness(1.1); }
      /* Grips on the two corners that make sense for a panel anchored bottom-right: the top-left
         one grows it up and to the left (where the space is), the bottom-right one is the corner
         everyone reaches for. */
      .grip { position:absolute; width:14px; height:14px; z-index:2; }
      .grip.tl { left:0; top:0; cursor:nwse-resize; }
      .grip.br { right:0; bottom:0; cursor:nwse-resize; }
      .grip.br::after { content:""; position:absolute; right:3px; bottom:3px; width:6px; height:6px;
        border-right:2px solid rgba(128,128,128,.6); border-bottom:2px solid rgba(128,128,128,.6); }
      /* Clicking a screenshot opens it full-size. The panel is 400px wide by default and a
         full-page stitch is unreadable at that scale, so an inline thumbnail can only ever say
         "a picture was taken" — this is where you actually check what it looked at. */
      .lb { position:fixed; inset:0; background:rgba(0,0,0,.88); z-index:2147483647;
        overflow:auto; padding:24px; box-sizing:border-box; cursor:zoom-out; }
      .lb img { display:block; margin:0 auto; max-width:100%; height:auto;
        box-shadow:0 4px 40px rgba(0,0,0,.6); }
      .lb .hint { position:fixed; top:8px; left:0; right:0; text-align:center; color:#fff;
        font:11px sans-serif; opacity:.65; }
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
      .dbg .hd { cursor:default; display:flex; gap:6px; align-items:baseline; }
      .dbg .hd.has { cursor:pointer; }
      .dbg .hd.has:hover .st { text-decoration:underline; }
      .dbg .side { font-size:9px; padding:0 4px; border-radius:6px; background:rgba(128,128,128,.22); }
      .dbg .st { font-weight:700; } .dbg .meta { opacity:.65; font-size:10px; }
      .dbg pre { white-space:pre-wrap; word-break:break-word; max-height:320px; overflow:auto;
        margin:6px 0 0; font-size:10.5px; }
    </style>
    <div class="panel">
      <div class="grip tl"></div>
      <div class="grip br"></div>
      <div class="bar">
        <b>Oracle · chat <span class="h"></span></b>
        <button class="ico dbgt" title="Record every tool call and the full prompt">🐞</button>
        <button class="ico act" title="Allow Oracle to click and type on this site">🖐</button>
        <button class="ico newt" title="New topic (keeps the old one)">✚</button>
        <button class="ico min" title="Hide (keeps this panel loaded)">–</button>
        <button class="ico close" title="Close">×</button>
      </div>
      <div class="tabs">
        <button class="tab on" data-pane="chat">Conversation</button>
        <button class="tab" data-pane="dbg">Debug<span class="n"></span></button>
        <button class="tab" data-pane="ses">Sessions</button>
      </div>
      <div class="sesbar">
        <b>session</b>
        <button class="pick" data-session="main">chat<span class="x"
          title="Clear this conversation. The tab stays — it is where typed questions always go.">×</span></button>
        <button class="pick" data-session="quick">quick<span class="x"
          title="Clear this conversation. The tab stays — it is where explains and regions always go.">×</span></button>
      </div>
      <div class="scroll"></div>
      <div class="dbg" hidden></div>
      <div class="ses" hidden></div>
      <div class="foot">
        <textarea class="in" rows="1" placeholder="Ask about this site… (Enter to send)"></textarea>
        <button class="go">Ask</button>
      </div>
    </div>`;

  const $ = (s) => root.querySelector(s);
  const scroll = $(".scroll"), dbgEl = $(".dbg"), sesEl = $(".ses"),
        input = $(".in"), go = $(".go");
  const esc = (s) => String(s).replace(/</g, "&lt;");

  // Block-aware, because the line-by-line version could not grow a header without breaking.
  // The old one turned every blank line into </p><p> and wrapped the lot in one <p>, so any block
  // element added to it — a heading, a list, a code fence — ended up illegally nested inside a
  // paragraph and rendered wherever the browser decided to close it. `### Foo` simply came out as
  // literal hashes. Split into blocks first, then decide what each block IS.
  function md(text) {
    let s = esc(text);
    // Fenced code is lifted out before anything else touches it, so a `#` or `*` inside a code
    // sample is never mistaken for markup, and put back at the end.
    // A VISIBLE sentinel. The first version used NUL bytes, which worked and was a bad idea in a
    // repo that has an entire design note about NULs surviving into places that cannot hold them —
    // and which are invisible in every editor, so the mistake is undiscoverable by reading. The
    // text is already HTML-escaped here, so `<` cannot appear in it and this cannot collide.
    const fenced = [];
    s = s.replace(/```(\w+)?\n([\s\S]*?)```/g, (_, lang, code) => {
      fenced.push(`<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
      return `<<FENCE${fenced.length - 1}>>`;
    });
    const inline = (t) => t
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<i>$2</i>");

    // `out` and `list` live ACROSS blocks, not within one. Models write bullets separated by blank
    // lines, and closing the list at every blank line produced a separate <ul> per item — two
    // margins between every bullet, which is exactly why a list read like a run of paragraphs. A
    // list now continues through blank lines and is closed by something that is not a list item.
    const out = [];
    let list = null;
    const flush = () => {
      if (list) { out.push(`<${list.tag}>${list.items.join("")}</${list.tag}>`); list = null; }
    };
    const isRow = (l) => /^\s*\|.*\|\s*$/.test(l);
    const isSep = (l) => /^\s*\|[\s:|-]+\|\s*$/.test(l) && l.includes("-");
    const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

    for (const block of s.split(/\n{2,}/)) {
      const lines = block.split("\n").filter((l) => l.trim() !== "");
      for (let li = 0; li < lines.length; li++) {
        const line = lines[li];

        // TABLES. A header row followed by a |---|---| separator, then rows until something that
        // is not a row. Comparing two runs is exactly the question that produces one, and without
        // this the answer arrived as a wall of pipes — correct markdown, rendered as literal text,
        // so the model assumed IT had got the syntax wrong and re-emitted the same thing.
        if (isRow(line) && li + 1 < lines.length && isSep(lines[li + 1])) {
          flush();
          const head = cells(line);
          const body = [];
          let j = li + 2;
          for (; j < lines.length && isRow(lines[j]); j++) body.push(cells(lines[j]));
          out.push(
            `<div class="tw"><table><thead><tr>` +
            head.map((c) => `<th>${inline(c)}</th>`).join("") +
            `</tr></thead><tbody>` +
            body.map((r) => `<tr>` +
              head.map((_, k) => `<td>${inline(r[k] ?? "")}</td>`).join("") + `</tr>`).join("") +
            `</tbody></table></div>`);
          li = j - 1;
          continue;
        }
        const head = line.match(/^(#{1,6})\s+(.*)$/);
        const item = line.match(/^\s*(?:[-*+]|\d+[.)])\s+(.*)$/);
        if (head) {
          flush();
          out.push(`<div class="mdh h${Math.min(head[1].length, 3)}">${inline(head[2])}</div>`);
        } else if (item) {
          const tag = /^\s*\d/.test(line) ? "ol" : "ul";
          if (list && list.tag !== tag) flush();
          list = list || { tag, items: [] };
          list.items.push(`<li>${inline(item[1])}</li>`);
        } else if (/^<<FENCE\d+>>$/.test(line)) {
          flush();
          out.push(line);
        } else if (list && li > 0) {
          // A plain line DIRECTLY under a bullet — same block, no blank line — continues that
          // bullet. Across a blank line it is a new paragraph instead, which is what markdown
          // means and what stopped "And then prose." being swallowed into the last item.
          list.items[list.items.length - 1] =
            list.items[list.items.length - 1].slice(0, -5) + " " + inline(line) + "</li>";
        } else {
          flush();
          const prev = out[out.length - 1];
          if (prev && prev.startsWith("<p>") && li > 0) {
            out[out.length - 1] = prev.slice(0, -4) + "<br>" + inline(line) + "</p>";
          } else {
            out.push(`<p>${inline(line)}</p>`);
          }
        }
      }
    }
    flush();

    return out.join("").replace(/<<FENCE(\d+)>>/g, (_, i) => fenced[Number(i)]);
  }

  const toolFailed = (raw) =>
    /^(NOT CLICKED|NOT TYPED|no element matches|error|unknown tool|look_at_page failed)/i
      .test((raw || "").trim());

  // One line that says what came back, from payloads that were written for a model to read.
  function toolGist(raw) {
    const s = (raw || "").trim();
    if (!s) return "(nothing)";
    if (toolFailed(s)) return s.split("\n")[0].slice(0, 120);
    try {
      const d = JSON.parse(s.split("\nAFTER:")[0]);
      if (d.clicked) return `clicked “${d.clicked}”`;
      if (d.typed) return "typed";
    } catch (_) { /* not JSON — a page dump or a vision reading */ }
    const words = s.replace(/\s+/g, " ").trim();
    return `${words.length.toLocaleString()} chars — ${words.slice(0, 90)}…`;
  }

  function bubble(t, i) {
    let body = md(t.content);
    if (t.role === "user") {
      body = `<p>${esc(t.content)}</p>`;
    } else if (t.cites && t.cites.length) {
      const pl = OracleCite.plan(t.content, t.cites);   // excerpt -> sequential display number
      body = OracleCite.linkify(body, pl, "occ" + i) + OracleCite.footnotes(pl, "occ" + i);
    }
    // Show the picture the turn was about — the region you selected, or the screenshot a tool took
    // of its own accord. The model never sees these; it works from the vision model's reading. But
    // an answer about an image you cannot see is an answer you cannot check.
    const img = t.image ? `<img class="thumb" src="${esc(t.image)}">` : "";

    // A step committed from the live view keeps looking exactly like it did while it ran.
    if (t.role === "livestep") {
      return `<p class="step${t.acting ? " act" : ""}${t.failed ? " bad" : ""}">` +
        `${t.done ? (t.failed ? "✗ " : "✓ ") : "· "}${esc(t.says || "")}` +
        (t.failed && t.detail ? `<span class="why">${esc(t.detail)}</span>` : "") +
        (t.image ? `<img class="thumb" src="${esc(t.image)}">` : "") + `</p>`;
    }
    // A tool RESULT is a machine payload — a JSON blob, a page dump — and pasting it into the
    // conversation is how a reloaded panel turned into a wall of URLs and nav text. It gets the
    // same one-line treatment as a live step, with the raw output one click away for when the
    // question is "what did it actually get back".
    if (t.role === "tool") {
      const raw = t.content || "";
      return `<p class="step${toolFailed(raw) ? " bad" : ""} res" data-i="${i}">` +
        `${toolFailed(raw) ? "✗" : "✓"} ${esc(t.tool || "tool")}: ${esc(toolGist(raw))}` +
        `<span class="more">show output</span></p>` +
        `<pre class="rawout" data-i="${i}" hidden>${esc(raw.slice(0, 4000))}</pre>` + img;
    }
    // An assistant turn that only asked for tools is not a message; it is the steps themselves.
    if (t.role === "assistant" && (t.calls || []).length && !(t.content || "").trim()) {
      return (t.calls || []).map((c) => `<p class="step">· ${esc(c)}</p>`).join("");
    }
    // A dropped connection is not lost work — the transcript is on the receiver, tool results and
    // all — so offer to carry on instead of making the user retype a question the system still has.
    const cont = t.resume ? `<button class="resume">Continue this answer</button>` : "";
    return `<div class="msg ${t.role === "user" ? "me" : ""}">
      <div class="who">${t.role === "user" ? "you" : "oracle"}</div>
      <div class="bub">${img}${body}${cont}</div></div>`;
  }

  function render() {
    let h = turns.map(bubble).join("");
    // LIVE ITEMS, in the order they happened. Prose used to accumulate into one bubble at the
    // bottom while every step rendered above it, so a turn that went text → click → text → look
    // came out as [all the steps][all the prose] — the transcript had it right and the live view
    // did not, which is why a reload "fixed" the order.
    for (const it of live) {
      if (it.t === "text") {
        if (it.s.trim()) h += `<div class="msg"><div class="who">oracle</div>
          <div class="bub">${md(it.s)}</div></div>`;
      } else {
        h += `<p class="step${it.acting ? " act" : ""}${it.failed ? " bad" : ""}">` +
          `${it.done ? (it.failed ? "✗ " : "✓ ") : "· "}${esc(it.says)}` +
          (it.failed && it.detail ? `<span class="why">${esc(it.detail)}</span>` : "") +
          (it.image ? `<img class="thumb" src="${esc(it.image)}">` : "") + `</p>`;
      }
    }
    // A pending action, if one is waiting on you. Rendered as a distinct bar rather than a step,
    // because it is the one thing on screen that is asking rather than reporting.
    if (confirming) {
      const p = confirming.preview || {};
      h += `<div class="confirm${p.found ? "" : " bad"}">` +
        `<div class="ca">${esc(confirming.says || "act on the page")}</div>` +
        (p.found
          ? `<div class="ct">→ <b>${esc(p.label || p.tag || "element")}</b>` +
            (p.href ? ` <span class="why">${esc(p.href)}</span>` : "") +
            `<span class="why">highlighted on the page</span></div>`
          : `<div class="ct">⚠ nothing on the page matches — allowing this will probably do ` +
            `nothing</div>`) +
        `<div class="cb"><button class="go" data-confirm="1">Enter — do it</button>` +
        `<button class="no" data-confirm="0">Esc — skip</button></div></div>`;
    }
    if (streaming) {
      // The status line must appear even when text has ALREADY streamed. The model says what it is
      // about to do and then calls the tool, so `acc` is non-empty exactly when the slow thing
      // starts — and showing the status only when acc was empty meant a multi-minute screenshot
      // plus two GPU swaps was represented by a blinking caret and nothing else. "It feels like it
      // does something and stuck" is the correct reading of that UI; the caret says a token is
      // coming, and no token was coming.
      //
      // Not "consulting the corpus" either: the model decides whether to search at all.
      // ONLY the indicator. The text is already on screen as the last live item — rendering `acc`
      // here too drew every streaming reply twice, which is why it happened on every reply and
      // resolved itself on reload: after `done` the live items are committed and `acc` is cleared,
      // so the second copy disappears with the bubble that was drawing it.
      const busy = `<div class="busy"><span class="spin"></span> &nbsp;${esc(status || "thinking…")}` +
        `<span class="elapsed"></span></div>`;
      h += acc
        ? `<div class="step">${busy}</div>`
        : `<div class="msg"><div class="who">oracle</div><div class="bub">${busy}</div></div>`;
    }
    // Questions typed while this turn was running. Shown so they are visibly WAITING rather than
    // apparently lost — the alternative was refusing to accept them at all.
    for (const q of queued) {
      h += `<div class="msg me"><div class="who">you · queued</div>` +
        `<div class="bub queued">${esc(q.q || "(image)")}</div></div>`;
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

  function ago(t) {
    if (!t) return "";
    const s = Math.max(0, Math.round(Date.now() / 1000 - t));
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  }

  function renderSessions(list, here) {
    // THIS HOST only. Conversations for every other site are somebody else's business here; they
    // are managed from the extension's own settings page, where a global list belongs.
    //
    // Both fixed sessions are always listed, even when empty, because they are always THERE: the
    // tabs do not come and go, and a list that omitted an empty one implied it had been removed.
    const byName = new Map(list.map((s) => [s.session, s]));
    const rows = ["main", "quick"].map((name) =>
      byName.get(name) || { host: here, session: name, turns: 0, at: 0, preview: "" })
      .concat(list.filter((s) => s.session !== "main" && s.session !== "quick"));
    sesEl.innerHTML = `<p class="empty" style="margin-bottom:6px">Conversations for
      ${esc(here)}. Other sites are in the Oracle toolbar popup.</p>` + rows.map((s) => `
      <div class="row">
        <span class="h">${esc(s.session)}${s.session === session ? '<span class="me">open</span>' : ""}
          <span class="me">${esc(s.preview || "")}</span></span>
        <span class="meta">${s.turns ? `${s.turns} turn${s.turns === 1 ? "" : "s"} · ${esc(ago(s.at))}`
                                     : "empty"}</span>
        ${s.turns ? `<button class="del" data-host="${esc(s.host)}"
                             data-session="${esc(s.session)}">clear</button>` : ""}
      </div>`).join("");
    sesEl.querySelectorAll(".del").forEach((b) => b.addEventListener("click", () => {
      // Deliberately two clicks: this is the one control here that destroys something. "New topic"
      // (✚) keeps everything and only moves the window; this does not.
      if (b.dataset.armed !== "1") {
        b.dataset.armed = "1";
        b.textContent = "really clear?";
        setTimeout(() => { b.dataset.armed = "0"; b.textContent = "clear"; }, 4000);
        return;
      }
      b.textContent = "clearing…";
      try {
        chrome.runtime.sendMessage({ type: "oracle:chatDelete", host: b.dataset.host,
                                     session: b.dataset.session });
      } catch (_) {}
    }));
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
      // Only rows with something to expand LOOK expandable. The cursor used to say "clickable" on
      // every row, including the ones carrying nothing but their metadata — so clicking did
      // nothing and the UI had promised otherwise.
      const has = text != null;
      return `<div class="ev"><div class="hd${has ? " has" : ""}" data-i="${i}">
          <span class="side">${esc(side || "receiver")}</span>
          <span class="st">${esc(stage || "?")}</span>
          ${has ? `<span class="meta">▸ ${String(text).length}c</span>` : ""}
        </div><div class="meta">${esc(meta)}</div>
        ${has ? `<pre hidden data-i="${i}">${esc(String(text))}</pre>` : ""}</div>`;
    }).join("");
    dbgEl.querySelectorAll(".hd").forEach((h) => h.addEventListener("click", () => {
      const pre = dbgEl.querySelector(`pre[data-i="${h.dataset.i}"]`);
      if (pre) pre.hidden = !pre.hidden;
    }));
  }

  function send(preset, image, thumb, source) {
    const q = preset !== undefined ? preset : input.value.trim();
    // A region with no typed question is a legitimate turn — the picture IS the question — so only
    // a typed-and-empty send is a no-op.
    if (!q && !image) return;
    // QUEUE instead of refusing. A turn can run for minutes, and being unable to type the obvious
    // follow-up while watching it work means either losing the thought or interrupting. The
    // question is recorded now and asked when the current turn finishes.
    if (streaming) {
      if (preset === undefined) input.value = "";
      queued.push({ q, image, thumb, source });
      render();
      return;
    }
    if (preset === undefined) input.value = "";
    turns.push({ role: "user", content: q || "(region sent)", image: thumb });
    streaming = true; acc = ""; status = ""; cites = null; sources = null;
    live = []; turnFailed = false; stepsOpen = false; dbgEvents = []; renderDbg();
    startedAt = Date.now();
    lastEventAt = Date.now();
    clearInterval(ticker);
    ticker = setInterval(tick, 1000);
    render(); tick();
    // Ask stays ENABLED: the next question queues rather than being refused.
    try {
      chrome.runtime.sendMessage({ type: "oracle:chat", message: q, image, session, source });
    } catch (_) {}
  }

  root.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
    root.querySelectorAll(".tab").forEach((o) => o.classList.toggle("on", o === t));
    const pane = t.dataset.pane;
    scroll.hidden = pane !== "chat";
    dbgEl.hidden = pane !== "dbg";
    sesEl.hidden = pane !== "ses";
    // Fetched on open rather than kept in sync: the list changes rarely and staleness here would
    // mean offering to delete something that is already gone.
    if (pane === "ses") {
      sesEl.innerHTML = `<p class="empty">loading…</p>`;
      try { chrome.runtime.sendMessage({ type: "oracle:chatSessions" }); } catch (_) {}
    }
  }));

  function flushQueued() {
    if (streaming || !queued.length) return;
    const next = queued.shift();
    // Straight back through send(), so a queued question is indistinguishable from one typed now.
    send(next.q, next.image, next.thumb, next.source);
  }

  function decide(ok) {
    if (!confirming) return;
    const id = confirming.id;
    confirming = null;
    status = ok ? "acting…" : "skipped — carrying on";
    render();
    try { chrome.runtime.sendMessage({ type: "chatConfirm", id, ok }); } catch (_) {}
  }

  // Keys are captured at the document level so they work wherever focus happens to be — the whole
  // point is that you answer without hunting for a button. Enter allows, Esc skips; anything else
  // falls through, and walking away skips it when the worker's timeout fires.
  document.addEventListener("keydown", (e) => {
    if (!confirming) return;
    if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); decide(true); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); decide(false); }
  }, true);
  scroll.addEventListener("click", (e) => {
    const b = e.target.closest("[data-confirm]");
    if (b) decide(b.dataset.confirm === "1");
  });

  go.addEventListener("click", () => send());
  input.addEventListener("keydown", (e) => {
    // While an action is pending, Enter belongs to the decision, not to the message box.
    if (confirming && e.key === "Enter") { e.preventDefault(); decide(true); return; }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  // Three settings, cycled by one button: look only → ask me → allow all.
  //
  // "Ask me" is the one worth having and is therefore the FIRST step away from off: the model picks
  // the target, the page highlights it, you press Enter. It is the setting that makes acting useful
  // on a site where a wrong click matters, which is most sites. Allow-all stays reachable for a
  // benchmark UI you own and are watching, and stays two clicks from off so it cannot be arrived at
  // by accident.
  const ACT_MODES = ["off", "confirm", "allow"];
  // Distinct GLYPHS per mode, not one glyph in three colours: this button decides whether a model
  // may press things in the user's session, and "which shade of amber is it" is not a way to find
  // that out at a glance.
  const ACT_UI = {
    off: { icon: "👁", cls: "",
           title: "Look only — Oracle cannot click or type here. Click: ask me before each action" },
    confirm: { icon: "🙋", cls: "ask",
               title: "Ask me — Oracle highlights what it wants to click and waits for Enter. " +
                      "Click: allow everything without asking" },
    allow: { icon: "🖐", cls: "on",
             title: "Allow all — Oracle may click and type here WITHOUT asking. Click: look only" },
  };
  function paintAct() {
    const b = $(".act");
    const ui = ACT_UI[actions] || ACT_UI.off;
    b.classList.remove("on", "ask");
    if (ui.cls) b.classList.add(ui.cls);
    b.textContent = ui.icon;
    b.title = ui.title;
  }
  $(".act").addEventListener("click", () => {
    actions = ACT_MODES[(ACT_MODES.indexOf(actions) + 1) % ACT_MODES.length] || "off";
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
  // Click any screenshot to see it full size. Delegated, because thumbnails are re-rendered on
  // every token and per-element listeners would be re-attached hundreds of times a turn.
  function openLightbox(src) {
    const lb = document.createElement("div");
    lb.className = "lb";
    lb.innerHTML = `<div class="hint">click anywhere, or press Esc, to close</div>`;
    const img = document.createElement("img");
    img.src = src;
    lb.appendChild(img);
    const close = () => { lb.remove(); window.removeEventListener("keydown", onKey, true); };
    const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); close(); } };
    lb.addEventListener("click", close);
    window.addEventListener("keydown", onKey, true);
    root.appendChild(lb);
  }
  root.addEventListener("click", (e) => {
    const t = e.target;
    if (!(t instanceof Element)) return;
    if (t.classList.contains("thumb")) {
      e.preventDefault();
      e.stopPropagation();
      openLightbox(t.getAttribute("src"));
      return;
    }
    if (t.classList.contains("fold")) { stepsOpen = !stepsOpen; render(); return; }
    if (t.classList.contains("resume")) {
      turns = turns.filter((x) => !x.resume);          // drop the error we are answering
      streaming = true; status = "picking up where it stopped…";
      turnFailed = false; startedAt = Date.now(); lastEventAt = Date.now();
      clearInterval(ticker); ticker = setInterval(tick, 1000);
      render();
      try { chrome.runtime.sendMessage({ type: "oracle:chatResume" }); } catch (_) {}
      return;
    }
    if (t.classList.contains("more")) {
      const pre = scroll.querySelector(`pre.rawout[data-i="${t.closest(".res").dataset.i}"]`);
      if (pre) { pre.hidden = !pre.hidden; t.textContent = pre.hidden ? "show output" : "hide"; }
    }
  });

  function paintSession() {
    root.querySelectorAll(".pick").forEach(
      (b) => b.classList.toggle("on", b.dataset.session === session));
  }
  root.querySelectorAll(".pick").forEach((b) => {
    b.addEventListener("click", () => {
      if (b.dataset.session === session) return;
      // Deliberately NOT blocked while a turn is in flight. It was, and one stuck turn made the
      // whole switcher dead — you could not even go and look at the other conversation. The turn
      // keeps running on the receiver and is recorded in the session it started in; switching just
      // changes what this panel is showing.
      session = b.dataset.session;
      streaming = false;
      go.disabled = false;
      clearInterval(ticker); ticker = null; startedAt = 0;
      turns = []; live = []; acc = ""; status = "";
      paintSession();
      render();
      try { chrome.runtime.sendMessage({ type: "oracle:chatLoad", session }); } catch (_) {}
    });
    // CLEAR, not delete. `chat` and `quick` are fixed surfaces — typed questions always land in
    // one, explains and regions in the other — so the tab is permanent and only its contents go.
    // Calling this "delete" promised the tab would disappear, and then it didn't.
    //
    // Still two clicks, and still distinct from ✚: "new topic" keeps the old turns and only moves
    // the window; this throws them away.
    const x = b.querySelector(".x");
    x.addEventListener("click", (e) => {
      e.stopPropagation();
      if (streaming) return;
      if (x.dataset.armed !== "1") {
        x.dataset.armed = "1";
        x.textContent = "clear?";
        x.classList.add("armed");
        setTimeout(() => { x.dataset.armed = "0"; x.textContent = "×"; x.classList.remove("armed"); },
                   4000);
        return;
      }
      x.dataset.armed = "0"; x.textContent = "×"; x.classList.remove("armed");
      turns = []; live = []; acc = ""; render();
      try {
        chrome.runtime.sendMessage({ type: "oracle:chatDelete", host: currentHost,
                                     session: b.dataset.session });
      } catch (_) {}
    });
  });

  $(".min").addEventListener("click", () => { host.style.display = "none"; });
  // Close removes the panel; the CONVERSATION is untouched, because it lives on the receiver and
  // reopening must show it again. Closing a window is not the same as ending a thought.
  $(".close").addEventListener("click", () => {
    clearInterval(ticker);
    host.remove();
    window.__oracleChat = null;
    // Closing is a decision, and it has to outlive the page: without this the worker would restore
    // the panel on the next navigation and closing it would look broken.
    try { chrome.runtime.sendMessage({ type: "oracle:chatClosed" }); } catch (_) {}
  });
  $(".newt").addEventListener("click", () => {
    if (streaming) return;
    turns = []; render();
    try { chrome.runtime.sendMessage({ type: "oracle:chatReset" }); } catch (_) {}
  });

  // ---------------------------------------------------------------- geometry: drag, resize, remember
  //
  // Where the panel sits and how big it is are the user's decisions, and a panel that forgets them
  // on every page is one that has to be re-arranged before it can be used. Stored once, applied
  // everywhere, clamped to the viewport on restore — a saved position from a 4K monitor must not
  // put the panel off-screen on a laptop.
  const GEOM_KEY = "oracleChatGeom";
  const MIN_W = 300, MIN_H = 240;
  const panel = $(".panel");
  let geom = null;                       // {left, top, w, h} once the user has touched it

  const clamp = (g) => ({
    w: Math.max(MIN_W, Math.min(g.w, window.innerWidth - 16)),
    h: Math.max(MIN_H, Math.min(g.h, window.innerHeight - 16)),
    left: Math.max(0, Math.min(g.left, window.innerWidth - Math.max(MIN_W, Math.min(g.w, window.innerWidth - 16)) - 8)),
    top: Math.max(0, Math.min(g.top, window.innerHeight - 60)),
  });

  function applyGeom() {
    if (!geom) return;
    const g = clamp(geom);
    host.style.right = "auto";
    host.style.bottom = "auto";
    host.style.left = g.left + "px";
    host.style.top = g.top + "px";
    panel.style.width = g.w + "px";
    panel.style.height = g.h + "px";
    panel.style.maxWidth = "none";
    panel.style.maxHeight = "none";
  }

  function saveGeom() {
    if (!geom) return;
    try { chrome.storage.local.set({ [GEOM_KEY]: clamp(geom) }); } catch (_) {}
  }

  // Turn whatever the CSS is doing into explicit numbers, so drag and resize have something to
  // work from. Until this runs the panel is anchored bottom-right by stylesheet.
  function pin() {
    if (geom) return;
    const r = panel.getBoundingClientRect();
    geom = { left: r.left, top: r.top, w: r.width, h: r.height };
    applyGeom();
  }

  try {
    chrome.storage.local.get(GEOM_KEY).then((v) => {
      if (v && v[GEOM_KEY]) { geom = v[GEOM_KEY]; applyGeom(); }
    }).catch(() => {});
  } catch (_) {}

  // One handler for both gestures: a drag moves, a grip resizes, and the only difference is which
  // numbers the delta is added to.
  (() => {
    let mode = null, sx = 0, sy = 0, start = null;

    const begin = (m) => (e) => {
      if (m === "move" && e.target.classList.contains("ico")) return;
      pin();
      mode = m; sx = e.clientX; sy = e.clientY; start = { ...geom };
      e.preventDefault();
      e.stopPropagation();
    };
    $(".bar").addEventListener("mousedown", begin("move"));
    $(".grip.tl").addEventListener("mousedown", begin("tl"));
    $(".grip.br").addEventListener("mousedown", begin("br"));

    window.addEventListener("mousemove", (e) => {
      if (!mode) return;
      const dx = e.clientX - sx, dy = e.clientY - sy;
      if (mode === "move") {
        geom = { ...start, left: start.left + dx, top: start.top + dy };
      } else if (mode === "br") {
        geom = { ...start, w: start.w + dx, h: start.h + dy };
      } else {
        // Top-left: the far corner must stay put, so width grows as left shrinks.
        const w = Math.max(MIN_W, start.w - dx), h = Math.max(MIN_H, start.h - dy);
        geom = { left: start.left + (start.w - w), top: start.top + (start.h - h), w, h };
      }
      applyGeom();
    });
    window.addEventListener("mouseup", () => {
      if (!mode) return;
      mode = null;
      saveGeom();
    });
    // A window that shrinks under a saved geometry would strand the panel off-screen.
    window.addEventListener("resize", () => { if (geom) applyGeom(); });
  })();

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "oracle:chatHistory") {
      turns = (msg.turns || []).map((t) => ({ role: t.role, content: t.content,
                                              image: t.image || "", tool: t.tool || "",
                                              calls: t.calls || [] }));
      // The transcript is authoritative; live items are provisional copies of what it now contains.
      // Keeping both after a reload rendered the same paragraph twice — once committed, once still
      // streaming — which is what a mid-turn session switch produced.
      live = [];
      acc = "";
      if (msg.session) session = msg.session;
      paintSession();
      currentHost = msg.host || "";
      root.querySelector(".bar .h").textContent = msg.host ? `· ${msg.host}` : "";
      actions = typeof msg.actions === "string" ? msg.actions
                : (msg.actions ? "allow" : "off");
      debugOn = !!msg.debug;
      paintAct();
      paintDbg();
      render();
      renderDbg();
      return;
    }
    if (msg.type === "oracle:chatSessions") { renderSessions(msg.sessions || [], msg.here || ""); return; }
    if (msg.type === "oracle:chatAsk") {
      // A quick query (explain / fact-check / a region) arriving from elsewhere: switch the panel to
      // the session it belongs to first, so the answer appears in the conversation that will keep
      // it rather than in whichever one happened to be open.
      if (msg.session && msg.session !== session) {
        session = msg.session;
        turns = [];
        paintSession();
      }
      send(msg.message || "", msg.image || "", msg.thumb, msg.source || "");
      return;
    }
    if (msg.type !== "oracle:chatEvent") return;
    // Events from a turn that belongs to a session you have since switched away from must not be
    // painted into the one you are looking at. The background worker keeps running it; the
    // transcript will show it when you switch back.
    if (msg.session && msg.session !== session) return;
    lastEventAt = Date.now();
    const { event, data } = msg.ev || {};

    // An event arriving for THIS session means a turn is running, whatever the panel currently
    // thinks. Switching sessions clears `streaming` — correctly, the panel stopped showing that
    // turn — but coming back to a session whose turn never stopped left it marked idle while
    // events kept landing: steps piled up unrendered, the fold engaged, and it read as a stale
    // conversation with "0 steps". Events are the evidence; trust them over the flag.
    if (!streaming && event !== "done" && event !== "error" && event !== "debug") {
      streaming = true;
      turnFailed = false;
      stepsOpen = false;
      if (!startedAt) startedAt = Date.now();
      clearInterval(ticker);
      ticker = setInterval(tick, 1000);
    }
    if (event === "debug") { dbgEvents.push(data || {}); renderDbg(); return; }
    if (event === "status") { status = data.text || ""; render(); return; }
    if (event === "tool_request") {
      (data.calls || []).forEach((c) =>
        live.push({ t: "step", id: c.id, says: c.says, acting: c.acting }));
      acc = "";                       // the next prose starts a new block, after these steps
      render();
      return;
    }
    if (event === "confirm_request") {
      // The model wants to act. Show WHAT, and wait for a key. Enter allows it, Esc skips it, and
      // doing nothing skips it too — the safe outcome must be the one that requires no decision.
      confirming = data;
      status = "waiting for you…";
      render();
      return;
    }
    if (event === "tool_image") {
      // The screenshot a tool took on its own initiative, shown next to the step that took it.
      const s = live.find((x) => x.t === "step" && x.id === data.id);
      if (s) s.image = data.image; else live.push({ t: "step", says: data.says, image: data.image });
      render();
      return;
    }
    if (event === "tool_done") {
      const s = live.find((x) => x.t === "step" && x.id === data.id);
      if (s) { s.done = true; s.failed = data.failed; s.detail = data.detail; }
      // A finished tool means the model is thinking again, not that the turn stalled. Say so, or
      // the panel sits on the last tool's label while nothing appears to happen.
      status = data.failed ? "that step failed — deciding what to do instead…" : "thinking…";
      render();
      return;
    }
    if (event === "sources") { sources = data.sources || []; cites = data.citations || []; return; }
    if (event === "delta") {
      // Prose goes into the ordered list at the position it arrived, not into one bucket that
      // renders after every step.
      acc += data.text || "";
      const last = live[live.length - 1];
      if (last && last.t === "text") last.s = acc; else live.push({ t: "text", s: acc });
      render();
      return;
    }
    if (event === "done") {
      streaming = false; go.disabled = false;
      // A turn cannot end with an action still awaiting a keypress: the worker that was waiting is
      // gone, so the bar would sit there forever and Enter would go nowhere.
      confirming = null;
      clearInterval(ticker); ticker = null; startedAt = 0;
      // Commit the live items into the transcript IN ORDER, so what you were watching is what
      // stays on screen. Flattening them into one assistant turn is what reordered them.
      for (const it of live) {
        if (it.t === "text") {
          if (it.s.trim()) turns.push({ role: "assistant", content: it.s, cites });
        } else {
          turns.push({ role: "livestep", says: it.says, acting: it.acting, done: it.done,
                       failed: it.failed, detail: it.detail, image: it.image });
        }
      }
      live = []; acc = ""; render();
      flushQueued();
      return;
    }
    if (event === "error") {
      streaming = false; go.disabled = false;
      clearInterval(ticker); ticker = null; startedAt = 0;
      const msgText = (data && data.error) || "error";
      // A dropped connection is not lost work: the transcript lives on the receiver, so the tool
      // results and everything before them are still there. Offer to carry on rather than making
      // the user retype a question the system already has.
      const recoverable = /cut off|ended early|connection|dropped|offline/i.test(msgText);
      turnFailed = true;
      turns.push({ role: "assistant", content: "_" + msgText + "_", resume: recoverable });
      acc = ""; render();
    }
  });

  window.__oracleChat = { show: () => { host.style.display = ""; input.focus(); } };
  render(); renderDbg(); input.focus();
  try { chrome.runtime.sendMessage({ type: "oracle:chatLoad" }); } catch (_) {}
})();
