// Oracle Capture — background service worker.
// ALL network to the receiver happens here (extension origin) — never in the page, because an
// https page cannot fetch http://localhost (mixed-content block). The page only ever gets DOM.

const RECEIVER = "http://127.0.0.1:8788";
const QUEUE_KEY = "oracleQueue";      // captures that never reached the receiver (laptop app off)
const PDF_KEY = "oracleCapturePdf";   // user toggle: attach a print-to-PDF (default on)

// ---------------------------------------------------------------- menus + commands

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "oracle-capture", title: "Capture page to Oracle", contexts: ["page"] });
  chrome.contextMenus.create({ id: "oracle-explain", title: "Explain this with Oracle", contexts: ["selection"] });
  refreshBadge();
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "oracle-capture") capture(tab);
  else if (info.menuItemId === "oracle-explain") explain(tab, info.selectionText || "");
});

chrome.commands.onCommand.addListener((cmd) => {
  if (cmd === "capture-page") {
    chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => tab && capture(tab));
  }
});

// popup -> background
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "capture") {
    chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => {
      capture(tab).then(sendResponse);
    });
    return true;
  }
  if (msg.type === "drain") { drainQueue().then(sendResponse); return true; }
  if (msg.type === "queueCount") { getQueue().then((q) => sendResponse(q.length)); return true; }
});

// retry the local queue periodically (receiver may have come up)
chrome.alarms.create("drain", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "drain") drainQueue(); });

// ---------------------------------------------------------------- capture

function scriptableTab(tab) {
  return tab && tab.id >= 0 && tab.url && /^https?:/i.test(tab.url);
}

async function capture(tab) {
  if (!scriptableTab(tab)) {
    notify("Can't capture this page (only http/https pages).");
    return { ok: false, error: "unscriptable" };
  }
  flashBadge("…", "#888");
  let page;
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => ({
        url: location.href,
        title: document.title,
        // rendered, authenticated DOM — with a <base> so relative links resolve offline
        html: "<base href=\"" + location.href + "\">\n" + document.documentElement.outerHTML,
      }),
    });
    page = res.result;
  } catch (e) {
    notify("Capture failed: " + e.message);
    flashBadge("!", "#c0392b");
    return { ok: false, error: String(e) };
  }

  const wantPdf = (await chrome.storage.local.get(PDF_KEY))[PDF_KEY] !== false;
  const pdf_base64 = wantPdf ? await printToPdf(tab.id).catch(() => null) : null;

  const payload = {
    url: page.url, title: page.title, html: page.html,
    pdf_base64, captured_at: new Date().toISOString(),
  };

  const ok = await postCapture(payload);
  if (ok) {
    flashBadge("✓", "#2e7d32");
  } else {
    await enqueue(payload);           // receiver unreachable -> buffer locally, retry on alarm
    notify("Receiver offline — capture queued, will ingest when it's back.");
    refreshBadge();
  }
  return { ok };
}

// Real PDF of the rendered, logged-in page via the DevTools protocol (only reliable way from an
// extension). Briefly shows Chrome's "started debugging" banner, then detaches.
async function printToPdf(tabId) {
  const target = { tabId };
  await chrome.debugger.attach(target, "1.3");
  try {
    const r = await chrome.debugger.sendCommand(target, "Page.printToPDF", {
      printBackground: true, preferCSSPageSize: true,
    });
    return r.data; // base64
  } finally {
    try { await chrome.debugger.detach(target); } catch (_) {}
  }
}

async function postCapture(payload) {
  try {
    const r = await fetch(RECEIVER + "/capture", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return r.ok;
  } catch (_) {
    return false;
  }
}

// ---------------------------------------------------------------- local queue (receiver unreachable)

async function getQueue() {
  return (await chrome.storage.local.get(QUEUE_KEY))[QUEUE_KEY] || [];
}
async function enqueue(payload) {
  const q = await getQueue();
  q.push(payload);
  await chrome.storage.local.set({ [QUEUE_KEY]: q });
}
async function drainQueue() {
  let q = await getQueue();
  if (!q.length) { refreshBadge(); return { drained: 0, remaining: 0 }; }
  const still = [];
  let drained = 0;
  for (const p of q) {
    if (await postCapture(p)) drained++; else still.push(p);
  }
  await chrome.storage.local.set({ [QUEUE_KEY]: still });
  refreshBadge();
  return { drained, remaining: still.length };
}

// ---------------------------------------------------------------- explain (glued popup)

async function explain(tab, selection) {
  selection = (selection || "").trim();
  if (!scriptableTab(tab) || !selection) return;
  // 1) inject the overlay module (idempotent) and show a loading card at the selection
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["overlay.js"] });
    await chrome.tabs.sendMessage(tab.id, { type: "oracle:explain:loading", selection });
  } catch (_) { return; }
  // 2) ask the corpus (from the extension origin), 3) hand the answer back to the page
  let payload;
  try {
    const r = await fetch(RECEIVER + "/explain", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selection, url: tab.url, title: tab.title }),
    });
    payload = r.ok ? await r.json() : { error: "receiver error " + r.status };
  } catch (_) {
    payload = { error: "Oracle receiver offline — start oracle-capture-receiver.py on the backend." };
  }
  chrome.tabs.sendMessage(tab.id, { type: "oracle:explain:answer", payload }).catch(() => {});
}

// ---------------------------------------------------------------- badge / notify

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
  // lightweight: title tooltip; avoids the notifications permission
  chrome.action.setTitle({ title: "Oracle Capture — " + message });
  setTimeout(() => chrome.action.setTitle({ title: "Oracle Capture" }), 4000);
}
