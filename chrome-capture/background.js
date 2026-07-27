// Oracle Capture — background service worker.
// ALL network to the receiver happens here (extension origin) — never in the page, because an
// https page cannot fetch http://localhost (mixed-content block). The page only ever gets DOM.

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
  refreshBadge();
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "cap-page") capture(tab);
  else if (info.menuItemId === "cap-sel") captureSelection(tab);
  else if (info.menuItemId === "explain") ground(tab, "explain", info.selectionText || "");
  else if (info.menuItemId === "factcheck") ground(tab, "factcheck", info.selectionText || "");
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
  if (msg.type === "dwell") {            // from dwell.js content script — passive signal
    observe(msg.text, OBS.dwell, msg.url, msg.title);
    return false;
  }
});

chrome.alarms.create("drain", { periodInMinutes: 1 });
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
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["overlay.js"] });
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

// ---------------------------------------------------------------- context memory (H17 sensor)

function observe(text, weight, url, title) {
  if (!text || text.length < 40) return;   // too little signal to place
  fetch(RECEIVER + "/observe", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, weight, url: url || "", title: title || "" }),
  }).catch(() => {});                       // fire-and-forget; backend applies the denylist
}

// ---------------------------------------------------------------- SSE + badge + notify

async function pumpSSE(stream, send) {
  const reader = stream.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const ev = parseSSE(buf.slice(0, i));
      buf = buf.slice(i + 2);
      if (ev) send(ev);
    }
  }
}
function parseSSE(chunk) {
  let event = "message", data = "";
  for (const line of chunk.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return null;
  try { return { event, data: JSON.parse(data) }; } catch (_) { return null; }
}

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
