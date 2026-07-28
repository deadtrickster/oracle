// Oracle Capture — region screenshot selector. Injected on demand. Drag a rectangle, optionally type
// a prompt, and it hands the rect (+ devicePixelRatio) to the background worker, which screenshots
// the visible tab, crops to the rect, and sends it to qwen3-vl. Tears its own UI down BEFORE the
// screenshot so the dimmer/toolbar never end up in the captured pixels.
(() => {
  if (window.__oracleRegion) return;
  window.__oracleRegion = true;

  const host = document.createElement("div");
  host.style.cssText = "all:initial;position:fixed;inset:0;z-index:2147483647;cursor:crosshair;";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
    <style>
      .dim { position:fixed; inset:0; background:rgba(0,0,0,.35); }
      .sel { position:fixed; border:1.5px solid #12e0d8; background:rgba(18,224,216,.08);
        box-shadow:0 0 0 9999px rgba(0,0,0,.35); display:none; }
      .hint { position:fixed; top:10px; left:50%; transform:translateX(-50%); background:#111;
        color:#fff; font:12px sans-serif; padding:6px 12px; border-radius:20px; opacity:.92; }
      .bar { position:fixed; display:none; gap:6px; align-items:center; background:#1e1e1e;
        border:1px solid #444; border-radius:8px; padding:6px; box-shadow:0 8px 30px rgba(0,0,0,.4); }
      /* max-width so the bar can never be wider than the viewport it has to be clamped inside */
      .bar input { width:220px; max-width:45vw; border:1px solid #555; background:#111; color:#eee;
        border-radius:6px; padding:5px 7px; font:13px sans-serif; }
      .bar button { border:0; border-radius:6px; padding:5px 9px; font:13px sans-serif; font-weight:600; }
      /* The host paints a full-screen crosshair for dragging, which the toolbar inherits — so the
         Send button looked like more canvas to drag on. Restore normal cursors over the controls. */
      .bar { cursor:default; }
      .bar input { cursor:text; }
      .bar button { cursor:pointer; }
      .go { background:#128a86; color:#fff; } .no { background:#333; color:#ccc; }
      .go:hover { filter:brightness(1.12); } .no:hover { background:#3d3d3d; }
    </style>
    <div class="dim"></div>
    <div class="sel"></div>
    <div class="hint">Drag to select a region · Esc to cancel</div>
    <div class="bar">
      <input type="text" placeholder="Ask about this region… (blank = describe it)">
      <button class="go">Send</button><button class="no">Cancel</button>
    </div>`;
  document.documentElement.appendChild(host);

  const $ = (s) => root.querySelector(s);
  const dim = $(".dim"), sel = $(".sel"), hint = $(".hint"), bar = $(".bar"), input = $("input");
  let sx, sy, drawing = false, rect = null;

  function teardown() {
    host.remove();
    window.__oracleRegion = false;
    window.removeEventListener("keydown", onKey, true);
  }
  function onKey(e) { if (e.key === "Escape") { e.preventDefault(); teardown(); } }
  window.addEventListener("keydown", onKey, true);

  // Keep the LOGICAL rect from the drag itself. Reading it back with getBoundingClientRect()
  // returns the styled box — border included — so a 300x200 drag reported 302x202 and the crop was
  // offset by the selection border. The geometry the user drew should not depend on how we style it.
  let box = null;
  function place(x, y) {
    const l = Math.min(x, sx), t = Math.min(y, sy), w = Math.abs(x - sx), h = Math.abs(y - sy);
    box = { x: l, y: t, w, h };
    sel.style.left = l + "px"; sel.style.top = t + "px"; sel.style.width = w + "px"; sel.style.height = h + "px";
  }

  // Put the toolbar where the hand already is: centred under the pointer that finished the drag,
  // not pinned to the selection's top-left corner. Anchoring to the rect meant a wide selection
  // dropped the Send button a screen-width away from the cursor that drew it.
  //
  // MEASURE, don't assume. The old code clamped against a hardcoded 330px, which is not the bar's
  // width — it renders wider — so near the right edge the buttons went off-screen: the clamp said
  // "fits" while the user saw a cut-off toolbar. Read the real box, then keep it fully inside the
  // viewport, flipping above the cursor when there is no room below.
  const MARGIN = 8;
  function placeBar(cx, cy) {
    bar.style.visibility = "hidden";     // lay it out to measure, without a flash at 0,0
    bar.style.display = "flex";
    const bw = bar.offsetWidth, bh = bar.offsetHeight;
    let left = cx - bw / 2;                                        // centred on the cursor
    left = Math.max(MARGIN, Math.min(left, innerWidth - bw - MARGIN));
    let top = cy + 16;                                             // just clear of the pointer
    if (top + bh > innerHeight - MARGIN) top = cy - bh - 16;        // no room below -> above
    top = Math.max(MARGIN, top);
    bar.style.left = Math.round(left) + "px";
    bar.style.top = Math.round(top) + "px";
    bar.style.visibility = "visible";
  }

  // Shadow DOM RETARGETING: this listener is on the shadow HOST, so for any event originating
  // inside the shadow root `e.target` is the host itself — never the button. The old guard
  // (`e.target.closest(".bar")`) therefore searched the light DOM, found nothing, and let a click
  // on Send start a NEW selection: the bar hid, the tiny drag reset everything, and the click never
  // reached the button. composedPath() is the shadow-aware view and reports the real inner node.
  const inBar = (e) =>
    e.composedPath().some((n) => n instanceof Element && n.classList.contains("bar"));

  host.addEventListener("mousedown", (e) => {
    if (inBar(e)) return;
    drawing = true; sx = e.clientX; sy = e.clientY;
    dim.style.display = "none"; bar.style.display = "none"; sel.style.display = "block";
    place(e.clientX, e.clientY); e.preventDefault();
  });
  host.addEventListener("mousemove", (e) => { if (drawing) place(e.clientX, e.clientY); });
  host.addEventListener("mouseup", (e) => {
    if (inBar(e)) return;
    if (!drawing) return;
    drawing = false;
    const r = box;
    if (!r || r.w < 8 || r.h < 8) { sel.style.display = "none"; dim.style.display = "block"; return; }
    rect = { x: r.x, y: r.y, w: r.w, h: r.h };
    hint.style.display = "none";
    placeBar(e.clientX, e.clientY);
    input.focus();
  });

  $(".no").addEventListener("click", teardown);
  function send() {
    const prompt = input.value.trim(), dpr = window.devicePixelRatio || 1;
    teardown();                                   // remove UI first, then capture clean pixels
    // one selector, two destinations: the transient vision card, or the per-host conversation
    const target = window.__oracleRegionTarget || "vision";
    setTimeout(() => { try { chrome.runtime.sendMessage({ type: "vision:region", rect, dpr, prompt, target }); } catch (_) {} }, 60);
  }
  $(".go").addEventListener("click", send);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
})();
