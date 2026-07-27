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
      .bar input { width:220px; border:1px solid #555; background:#111; color:#eee; border-radius:6px;
        padding:5px 7px; font:13px sans-serif; }
      .bar button { border:0; border-radius:6px; padding:5px 9px; font:13px sans-serif; font-weight:600; cursor:pointer; }
      .go { background:#128a86; color:#fff; } .no { background:#333; color:#ccc; }
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

  function place(x, y) {
    const l = Math.min(x, sx), t = Math.min(y, sy), w = Math.abs(x - sx), h = Math.abs(y - sy);
    sel.style.left = l + "px"; sel.style.top = t + "px"; sel.style.width = w + "px"; sel.style.height = h + "px";
  }

  host.addEventListener("mousedown", (e) => {
    if (e.target.closest(".bar")) return;
    drawing = true; sx = e.clientX; sy = e.clientY;
    dim.style.display = "none"; bar.style.display = "none"; sel.style.display = "block";
    place(e.clientX, e.clientY); e.preventDefault();
  });
  host.addEventListener("mousemove", (e) => { if (drawing) place(e.clientX, e.clientY); });
  host.addEventListener("mouseup", () => {
    if (!drawing) return;
    drawing = false;
    const r = sel.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) { sel.style.display = "none"; dim.style.display = "block"; return; }
    rect = { x: r.left, y: r.top, w: r.width, h: r.height };
    hint.style.display = "none";
    let top = r.bottom + 8; if (top > innerHeight - 46) top = r.top - 46;
    bar.style.top = Math.max(8, top) + "px";
    bar.style.left = Math.min(Math.max(8, r.left), innerWidth - 330) + "px";
    bar.style.display = "flex"; input.focus();
  });

  $(".no").addEventListener("click", teardown);
  function send() {
    const prompt = input.value.trim(), dpr = window.devicePixelRatio || 1;
    teardown();                                   // remove UI first, then capture clean pixels
    setTimeout(() => { try { chrome.runtime.sendMessage({ type: "vision:region", rect, dpr, prompt }); } catch (_) {} }, 60);
  }
  $(".go").addEventListener("click", send);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
})();
