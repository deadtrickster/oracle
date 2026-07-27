// Oracle Capture — background service worker.
// ALL network to the receiver happens here (extension origin) — never in the page, because an
// https page cannot fetch http://localhost (mixed-content block). The page only ever gets DOM.

import { pumpSSE } from "./sse.js";

const RECEIVER = "http://127.0.0.1:8788";
const QUEUE_KEY = "oracleQueue";      // captures that never reached the receiver (laptop app off)
const PDF_KEY = "oracleCapturePdf";   // user toggle: attach a print-to-PDF (default on)
// how strongly each interaction feeds the fading-slot memory (H17 sensor) — deliberate > passive
const OBS = { capture: 1.0, explain: 0.8, factcheck: 0.8, dwell: 0.3 };

// ---------------------------------------------------------------- menus + commands

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "cap-page", title: "Capture page to Oracle", contexts: ["page"] });
  chrome.contextMenus.create({ id: "cap-sel", title: "Capture selection to Oracle", contexts: ["selection"] });
  chrome.contextMenus.create({ id: "explain", title: "Explain this with Oracle", contexts: ["selection"] });
  chrome.contextMenus.create({ id: "factcheck", title: "Fact-check this against Oracle", contexts: ["selection"] });
  chrome.contextMenus.create({ id: "shot", title: "Screenshot a region → Oracle vision", contexts: ["page", "image", "selection"] });
  refreshBadge();
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "cap-page") capture(tab);
  else if (info.menuItemId === "cap-sel") captureSelection(tab);
  else if (info.menuItemId === "explain") ground(tab, "explain", info.selectionText || "");
  else if (info.menuItemId === "factcheck") ground(tab, "factcheck", info.selectionText || "");
  else if (info.menuItemId === "shot") screenshotRegion(tab);
});

chrome.commands.onCommand.addListener((cmd) => {
  if (cmd === "capture-page")
    chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => tab && capture(tab));
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "capture") {
    chrome.tabs.query({ active: true, currentWindow: true },
      ([tab]) => capture(tab, { note: msg.note }).then(sendResponse));
    return true;
  }
  if (msg.type === "batchCapture") { batchCapture().then(sendResponse); return true; }
  if (msg.type === "drain") { drainQueue().then(sendResponse); return true; }
  if (msg.type === "queueCount") { getQueue().then((q) => sendResponse(q.length)); return true; }
  if (msg.type === "screenshotRegion") {
    chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => { if (tab) screenshotRegion(tab); sendResponse({ ok: true }); });
    return true;
  }
  if (msg.type === "vision:region") { if (sender.tab) visionRegion(msg, sender.tab); return false; }
  if (msg.type === "oracle:groundVision") { if (sender.tab) groundVision(msg.text, sender.tab); return false; }
  if (msg.type === "dwell") {            // from dwell.js content script — passive signal
    observe(msg.text, OBS.dwell, msg.url, msg.title);
    return false;
  }
});

// The offline queue drains on a periodic alarm. Creating it at top level looked right but is an
// MV3 trap: this worker is torn down and restarted on EVERY event (a dwell message, a menu click,
// the alarm itself), and `alarms.create` with an existing name REPLACES it — restarting the
// countdown. While browsing, the worker wakes far more often than once a minute, so the alarm
// could be reset forever and never fire: queued captures would sit there until a manual drain,
// silently breaking the "it all lands when the backend comes back" promise. Create it only if it
// isn't already scheduled, and (re)assert it on the two lifecycle events that actually matter.
async function ensureDrainAlarm() {
  if (!(await chrome.alarms.get("drain"))) {
    chrome.alarms.create("drain", { periodInMinutes: 1 });
  }
}
chrome.runtime.onInstalled.addListener(ensureDrainAlarm);
chrome.runtime.onStartup.addListener(ensureDrainAlarm);
ensureDrainAlarm();
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "drain") drainQueue(); });

// ---------------------------------------------------------------- capture

function scriptableTab(tab) {
  return tab && tab.id >= 0 && tab.url && /^https?:/i.test(tab.url);
}

// injected: whole rendered page (authenticated DOM) + a visible-text snippet for the memory
function extractPage() {
  return {
    url: location.href,
    title: document.title,
    html: "<base href=\"" + location.href + "\">\n" + document.documentElement.outerHTML,
    text: (document.body ? document.body.innerText : "").replace(/\s+/g, " ").trim().slice(0, 2000),
  };
}
// injected: just the current selection (as HTML + text)
function extractSelection() {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount || sel.isCollapsed) return null;
  const div = document.createElement("div");
  for (let i = 0; i < sel.rangeCount; i++) div.appendChild(sel.getRangeAt(i).cloneContents());
  return {
    url: location.href, title: document.title,
    html: "<base href=\"" + location.href + "\">\n" + div.innerHTML,
    text: sel.toString().replace(/\s+/g, " ").trim().slice(0, 2000),
  };
}

// opts: { note, partial, pdf (bool), page (pre-extracted {url,title,html,text}) }
async function capture(tab, opts = {}) {
  if (!scriptableTab(tab)) { notify("Can't capture this page (only http/https)."); return { ok: false }; }
  flashBadge("…", "#888");
  let page = opts.page;
  if (!page) {
    try {
      const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: extractPage });
      page = res.result;
    } catch (e) { notify("Capture failed: " + e.message); flashBadge("!", "#c0392b"); return { ok: false }; }
  }

  const wantPdf = opts.pdf !== false && (await chrome.storage.local.get(PDF_KEY))[PDF_KEY] !== false;
  const pdf_base64 = wantPdf ? await printToPdf(tab.id).catch(() => null) : null;
  const payload = {
    url: page.url, title: page.title, html: page.html, note: opts.note || "",
    partial: !!opts.partial, pdf_base64, captured_at: new Date().toISOString(),
  };
  const res = await postCapture(payload);
  if (res.ok) {
    flashBadge("✓", "#2e7d32");
    observe(page.text, OBS.capture, page.url, page.title);
  } else {
    await enqueue(payload); notify("Receiver offline — capture queued."); refreshBadge();
  }
  return { ok: res.ok, stem: res.stem };
}

async function captureSelection(tab) {
  if (!scriptableTab(tab)) return { ok: false };
  const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: extractSelection });
  const s = res && res.result;
  if (!s) { notify("No selection to capture."); return { ok: false }; }
  // selection capture skips the PDF (a fragment PDF is meaningless); keep the fragment HTML
  return capture(tab, { partial: true, pdf: false, page: s });
}

// batch: every capturable tab in this window, Markdown-only (fast; no per-tab PDF banners)
async function batchCapture() {
  const tabs = await chrome.tabs.query({ currentWindow: true });
  let ok = 0, skipped = 0;
  for (const tab of tabs) {
    if (!scriptableTab(tab)) { skipped++; continue; }
    const r = await capture(tab, { pdf: false }).catch(() => ({ ok: false }));
    if (r.ok) ok++; else skipped++;
  }
  notify(`Batch: ${ok} captured, ${skipped} skipped.`);
  return { ok, skipped, total: tabs.length };
}

async function printToPdf(tabId) {
  const target = { tabId };
  await chrome.debugger.attach(target, "1.3");
  try {
    const r = await chrome.debugger.sendCommand(target, "Page.printToPDF",
      { printBackground: true, preferCSSPageSize: true });
    return r.data;
  } finally { try { await chrome.debugger.detach(target); } catch (_) {} }
}

async function postCapture(payload) {
  try {
    const r = await fetch(RECEIVER + "/capture", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (!r.ok) return { ok: false };
    const j = await r.json().catch(() => ({}));
    return { ok: true, stem: j.stem };
  } catch (_) { return { ok: false }; }
}

// ---------------------------------------------------------------- local queue (receiver unreachable)

async function getQueue() { return (await chrome.storage.local.get(QUEUE_KEY))[QUEUE_KEY] || []; }
async function enqueue(p) { const q = await getQueue(); q.push(p); await chrome.storage.local.set({ [QUEUE_KEY]: q }); }
async function drainQueue() {
  let q = await getQueue();
  if (!q.length) { refreshBadge(); return { drained: 0, remaining: 0 }; }
  const still = []; let drained = 0;
  for (const p of q) { (await postCapture(p)).ok ? drained++ : still.push(p); }
  await chrome.storage.local.set({ [QUEUE_KEY]: still });
  refreshBadge();
  return { drained, remaining: still.length };
}

// ---------------------------------------------------------------- explain / fact-check (glued popup)

async function ground(tab, mode, selection) {
  selection = (selection || "").trim();
  if (!scriptableTab(tab) || !selection) return;
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["cite.js", "overlay.js"] });
    await chrome.tabs.sendMessage(tab.id, { type: "oracle:loading", mode, selection });
  } catch (_) { return; }
  observe(selection, OBS[mode] || 0.8, tab.url, tab.title);
  const endpoint = mode === "factcheck" ? "/factcheck" : "/explain";
  const body = mode === "factcheck"
    ? { claim: selection, url: tab.url, title: tab.title }
    : { selection, url: tab.url, title: tab.title };
  const send = (ev) => chrome.tabs.sendMessage(tab.id, { type: "oracle:event", mode, ev }).catch(() => {});
  try {
    const r = await fetch(RECEIVER + endpoint, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!r.ok || !r.body) { send({ event: "error", data: { error: "receiver error " + r.status } }); return; }
    await pumpSSE(r.body, send);
  } catch (_) {
    send({ event: "error", data: { error: "Oracle receiver offline — start oracle-capture-receiver.py." } });
  }
}

// ---------------------------------------------------------------- screenshot region -> vision model

async function screenshotRegion(tab) {
  if (!scriptableTab(tab)) { notify("Can't screenshot this page (only http/https)."); return; }
  // Ask the receiver whether vision is actually available before making the user drag a rectangle.
  // The GPU holds the text model OR qwen3-vl, never both, so vision ships disabled until the VRAM
  // broker exists — say that up front rather than after the selection dance.
  try {
    const st = await (await fetch(RECEIVER + "/status")).json();
    if (st && st.vision === false) { notify(st.vision_note || "Vision is disabled on this machine."); return; }
  } catch (_) { /* receiver down — fall through; the card will report it */ }
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["regionselect.js"] });
  } catch (e) { notify("Region select failed: " + e.message); }
}

async function blobToB64(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}

// regionselect.js -> here: screenshot the visible tab, crop to the rect, stream qwen3-vl into the card
async function visionRegion(msg, tab) {
  const { rect, dpr, prompt } = msg;
  const send = (ev) => chrome.tabs.sendMessage(tab.id, { type: "oracle:event", mode: "vision", ev }).catch(() => {});
  let thumb;
  try {
    const shot = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
    const bmp = await createImageBitmap(await (await fetch(shot)).blob());
    const sx = Math.round(rect.x * dpr), sy = Math.round(rect.y * dpr);
    const sw = Math.max(1, Math.round(rect.w * dpr)), sh = Math.max(1, Math.round(rect.h * dpr));
    const canvas = new OffscreenCanvas(sw, sh);
    canvas.getContext("2d").drawImage(bmp, sx, sy, sw, sh, 0, 0, sw, sh);
    const b64 = await blobToB64(await canvas.convertToBlob({ type: "image/png" }));
    thumb = "data:image/png;base64," + b64;
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["cite.js", "overlay.js"] });
    await chrome.tabs.sendMessage(tab.id, { type: "oracle:loading", mode: "vision", thumb });
    // Send the page's text WITH the pixels. A cropped region is nearly self-describing to a human
    // and almost opaque to a model: a Grafana panel is a line and some ticks, and without the
    // dashboard name, panel titles, legend and units the model will confabulate a plausible system.
    // That text is on the page; grab it rather than making the model guess.
    let pageText = "", cropText = "";
    try {
      const [pt] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        args: [rect],
        func: (box) => {
          // Walk TEXT NODES and place each by its own Range box.
          //
          // The reason is not that innerText misses SVG — measured in headless Chrome, innerText
          // does include SVG <text>, so chart axis labels survive either way. The reason is
          // LOCATION: innerText gives one flat string for the whole document, so on a page of
          // twelve panels it names twelve metrics and cannot say which one you cropped. A text-node
          // walk can ask WHERE each string is rendered and keep the ones inside the rectangle you
          // dragged. Pure geometry — no selectors, no hostnames, nothing site-specific.
          const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
          const walk = (test) => {
            const out = [];
            const seen = new Set();
            const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            const rng = document.createRange();
            while (w.nextNode()) {
              const t = clean(w.currentNode.nodeValue);
              if (t.length < 2) continue;
              rng.selectNodeContents(w.currentNode);
              const r = rng.getBoundingClientRect();
              if (!r.width && !r.height) continue;      // not rendered
              if (!test(r)) continue;
              const k = t.toLowerCase();
              if (seen.has(k)) continue;                 // charts repeat tick labels
              seen.add(k);
              out.push(t);
              if (out.length > 400) break;
            }
            return out;
          };
          // Containment by CENTRE, not by intersection: a bare intersection test counts a one-pixel
          // touch, which pulled the whole nav bar into a crop of the panel just below it. The
          // centre of a label is inside the region the user meant iff the label is in that region.
          const hit = (r) => {
            const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
            return cx >= box.x && cx <= box.x + box.w && cy >= box.y && cy <= box.y + box.h;
          };
          return {
            crop: walk(hit).join(" · ").slice(0, 3000),
            page: walk(() => true).join(" · ").slice(0, 6000),
          };
        },
      });
      cropText = pt?.result?.crop || "";
      pageText = pt?.result?.page || "";
    } catch (_) { /* some pages refuse injection; context is a bonus, not a requirement */ }
    const r = await fetch(RECEIVER + "/vision", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        image: b64, mime: "image/png", prompt,
        url: tab.url, title: tab.title, page_text: pageText, crop_text: cropText,
      }),
    });
    if (!r.ok || !r.body) { send({ event: "error", data: { error: "receiver error " + r.status } }); return; }
    await pumpSSE(r.body, send);
  } catch (e) {
    // make sure the card exists to show the error
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["cite.js", "overlay.js"] });
      await chrome.tabs.sendMessage(tab.id, { type: "oracle:loading", mode: "vision", thumb });
    } catch (_) {}
    send({ event: "error", data: { error: "Vision failed: " + (e.message || e) + " (is the receiver + qwen3-vl up?)" } });
  }
}

// qwen3-vl read some pixels; now ground that text in the corpus -> a cited answer (streamed as
// mode:"ground" into the same card, below the vision answer).
async function groundVision(text, tab) {
  text = (text || "").trim();
  if (!text) return;
  observe(text, OBS.explain, tab.url, tab.title);
  const send = (ev) => chrome.tabs.sendMessage(tab.id, { type: "oracle:event", mode: "ground", ev }).catch(() => {});
  try {
    const r = await fetch(RECEIVER + "/explain", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selection: text.slice(0, 1500), url: tab.url, title: tab.title }),
    });
    if (!r.ok || !r.body) { send({ event: "error", data: { error: "receiver error " + r.status } }); return; }
    await pumpSSE(r.body, send);
  } catch (_) {
    send({ event: "error", data: { error: "Oracle receiver offline." } });
  }
}

// ---------------------------------------------------------------- context memory (H17 sensor)

function observe(text, weight, url, title) {
  if (!text || text.length < 40) return;   // too little signal to place
  fetch(RECEIVER + "/observe", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, weight, url: url || "", title: title || "" }),
  }).catch(() => {});                       // fire-and-forget; backend applies the denylist
}

// ---------------------------------------------------------------- SSE + badge + notify

async function refreshBadge() {
  const n = (await getQueue()).length;
  chrome.action.setBadgeBackgroundColor({ color: "#c0392b" });
  chrome.action.setBadgeText({ text: n ? String(n) : "" });
}
function flashBadge(text, color) {
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setBadgeText({ text });
  setTimeout(refreshBadge, 1500);
}
function notify(message) {
  chrome.action.setTitle({ title: "Oracle Capture — " + message });
  setTimeout(() => chrome.action.setTitle({ title: "Oracle Capture" }), 4000);
}
