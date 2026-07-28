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
  // Right-clicking an image already identifies the target, so there is nothing to select: skip the
  // rectangle entirely and read that image.
  chrome.contextMenus.create({ id: "img", title: "Explain this image with Oracle", contexts: ["image"] });
  // The whole viewport, both models: page text (summarised) + a screenshot read by qwen3-vl.
  chrome.contextMenus.create({ id: "page-vl", title: "Explain this page with Oracle (vision)", contexts: ["page"] });
  chrome.contextMenus.create({ id: "chat", title: "Chat with Oracle about this site", contexts: ["page", "selection"] });
  chrome.contextMenus.create({ id: "chat-sel", title: "Send selection to Oracle chat", contexts: ["selection"] });
  chrome.contextMenus.create({ id: "chat-region", title: "Send a region to Oracle chat", contexts: ["page", "image", "selection"] });
  refreshBadge();
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "cap-page") capture(tab);
  else if (info.menuItemId === "cap-sel") captureSelection(tab);
  else if (info.menuItemId === "explain") ground(tab, "explain", info.selectionText || "");
  else if (info.menuItemId === "factcheck") ground(tab, "factcheck", info.selectionText || "");
  else if (info.menuItemId === "shot") screenshotRegion(tab);
  else if (info.menuItemId === "img") visionImage(info.srcUrl, tab);
  else if (info.menuItemId === "page-vl") visionPage(tab);
  else if (info.menuItemId === "chat") openChat(tab);
  else if (info.menuItemId === "chat-sel") chatSelection(tab, info.selectionText || "");
  else if (info.menuItemId === "chat-region") screenshotRegion(tab, "chat");
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
  if (msg.type === "vision:region") {
    if (sender.tab) (msg.target === "chat" ? chatRegion : visionRegion)(msg, sender.tab);
    return false;
  }
  if (msg.type === "visionPage") {
    chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => { if (tab) visionPage(tab); sendResponse({ ok: true }); });
    return true;
  }
  if (msg.type === "oracle:chat") { if (sender.tab) chatSend(msg.message, sender.tab, msg.image || ""); return false; }
  if (msg.type === "oracle:chatLoad") { if (sender.tab) chatLoad(sender.tab); return false; }
  if (msg.type === "oracle:chatReset") { if (sender.tab) chatReset(sender.tab); return false; }
  if (msg.type === "openChat") {
    chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => { if (tab) openChat(tab); sendResponse({ ok: true }); });
    return true;
  }
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

// Where the selection sits on the page — the context that disambiguates it.
//
// "It collapses under contention" means one thing under a heading about locks and another under one
// about connection pools, and the selection alone carries neither. So send the enclosing block and
// the heading chain above it.
//
// Deliberately NOT the whole page: the corpus excerpts are what the answer is built from, and a few
// thousand characters of nav, footer and sidebar would dilute them (Axiom 1) while adding nothing
// that helps interpret the phrase. Whole-page text is a fallback for when the selection has no
// usable container at all.
function extractSelectionContext() {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const sel = window.getSelection();
  const out = { around: "", headings: "", page: "" };
  if (!sel || !sel.rangeCount) {
    out.page = clean(document.body && document.body.innerText).slice(0, 2000);
    return out;
  }
  let node = sel.getRangeAt(0).commonAncestorContainer;
  if (node.nodeType === Node.TEXT_NODE) node = node.parentElement;
  // climb until the container holds real surrounding prose, not just the selection again
  let el = node;
  const want = Math.max(400, clean(sel.toString()).length * 3);
  while (el && el !== document.body && clean(el.innerText).length < want) el = el.parentElement;
  out.around = clean(el && el.innerText).slice(0, 1500);

  // the heading chain above it: walk backwards through the document for h1..h3, keeping the
  // most recent of each level, which reconstructs "PostgreSQL > Locks" without a DOM tree walk
  const heads = [...document.querySelectorAll("h1,h2,h3")];
  const anchor = node instanceof Element ? node : node.parentElement;
  const seen = {};
  for (const h of heads) {
    if (anchor && (h.compareDocumentPosition(anchor) & Node.DOCUMENT_POSITION_FOLLOWING)) {
      seen[h.tagName] = clean(h.innerText).slice(0, 120);
    }
  }
  out.headings = ["H1", "H2", "H3"].map((k) => seen[k]).filter(Boolean).join(" › ");
  if (!out.around) out.page = clean(document.body && document.body.innerText).slice(0, 2000);
  return out;
}

async function ground(tab, mode, selection) {
  selection = (selection || "").trim();
  if (!scriptableTab(tab) || !selection) return;
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["cite.js", "overlay.js"] });
    await chrome.tabs.sendMessage(tab.id, { type: "oracle:loading", mode, selection });
  } catch (_) { return; }
  observe(selection, OBS[mode] || 0.8, tab.url, tab.title);
  const endpoint = mode === "factcheck" ? "/factcheck" : "/explain";
  const agents_md = await agentsMd(tab.url);
  const debug = await debugOn();
  let where = { around: "", headings: "", page: "" };
  try {
    const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id },
                                                       func: extractSelectionContext });
    where = r?.result || where;
  } catch (_) { /* injection refused; the selection alone still works */ }
  // Always say SOMETHING, so the Debug tab can never be ambiguous between "switched off" and
  // "broken". An empty pane looked the same in both cases, and the honest answer to "why is this
  // empty" is usually the boring one.
  if (debug) {
    dbg(tab, { stage: "selection context", mode, around_chars: where.around.length,
               headings: where.headings, page_fallback_chars: where.page.length,
               text: where.around || where.page }, mode);
  } else {
    dbg(tab, { stage: "debug is OFF — tick “debug” in the Oracle popup and ask again to see every "
                      "event and the full prompt" }, mode);
  }
  const body = mode === "factcheck"
    ? { claim: selection, url: tab.url, title: tab.title, agents_md, debug, where }
    : { selection, url: tab.url, title: tab.title, agents_md, debug, where };
  const send = (ev) => chrome.tabs.sendMessage(tab.id, { type: "oracle:event", mode, ev }).catch(() => {});
  // Mark the phase boundary. Everything above talks to the PAGE's host; everything below talks to
  // the receiver. When it hangs, that one word is the difference between "the site is slow" and
  // "the model is loading", and the card used to show the same text for both.
  send({ event: "status", data: { text: "asking Oracle…" } });
  try {
    const r = await fetch(RECEIVER + endpoint, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!r.ok || !r.body) { send({ event: "error", data: { error: "receiver error " + r.status } }); return; }
    await pumpOrFail(r.body, send);
  } catch (_) {
    send({ event: "error", data: { error: "Oracle receiver offline — start oracle-capture-receiver.py." } });
  }
}

// ---------------------------------------------------------------- per-host chat

async function openChat(tab) {
  if (!scriptableTab(tab)) { notify("Chat needs an http/https page."); return; }
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["cite.js", "chat.js"] });
}

async function chatLoad(tab) {
  // The transcript lives on the receiver, so reopening the panel — in another tab, another window,
  // or after a restart — shows the conversation that is actually stored rather than a fresh one.
  let host = "";
  try { host = new URL(tab.url).host; } catch (_) {}
  try {
    const r = await fetch(`${RECEIVER}/chat/history?host=${encodeURIComponent(host)}`);
    const d = r.ok ? await r.json() : { turns: [] };
    chrome.tabs.sendMessage(tab.id, { type: "oracle:chatHistory", host, turns: d.turns || [],
                                      epoch: d.epoch }).catch(() => {});
  } catch (_) {
    chrome.tabs.sendMessage(tab.id, { type: "oracle:chatHistory", host, turns: [] }).catch(() => {});
  }
}

async function chatSend(message, tab, image = "") {
  const send = (ev) => chrome.tabs.sendMessage(tab.id, { type: "oracle:chatEvent", ev }).catch(() => {});
  let host = "";
  try { host = new URL(tab.url).host; } catch (_) {}
  const debug = await debugOn();
  let where = { around: "", headings: "", page: "" };
  try {
    const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id },
                                                       func: extractSelectionContext });
    where = r?.result || where;
  } catch (_) { /* injection refused; the question still works */ }
  if (debug) {
    chrome.tabs.sendMessage(tab.id, { type: "oracle:chatEvent", ev: { event: "debug", data: {
      side: "extension", stage: "chat send", host, around_chars: where.around.length,
      headings: where.headings, page_fallback_chars: where.page.length } } }).catch(() => {});
  }
  try {
    const r = await fetch(RECEIVER + "/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, host, url: tab.url, title: tab.title,
                             agents_md: await agentsMd(tab.url), debug, where,
                             image, mime: "image/png" }),
    });
    if (!r.ok || !r.body) { send({ event: "error", data: { error: "receiver error " + r.status } }); return; }
    await pumpOrFail(r.body, send);
  } catch (_) {
    send({ event: "error", data: { error: "Oracle receiver offline." } });
  }
  observe(message, OBS.explain, tab.url, tab.title);
}

// Selection -> chat. Same gesture as "Explain this", different destination: the answer lands in
// the conversation, so the follow-up question already knows what you were looking at.
async function chatSelection(tab, selection) {
  selection = (selection || "").trim();
  if (!selection) return;
  await openChat(tab);
  const msg = `Explain this, from the page I'm reading:\n\n"${selection.slice(0, 2000)}"`;
  chrome.tabs.sendMessage(tab.id, { type: "oracle:chatAsk", message: msg }).catch(() => {});
}

// Region -> chat. The pixels are read by qwen3-vl on the receiver and the READING is what enters
// the transcript, so three turns later "that spike" still refers to something.
async function chatRegion(msg, tab) {
  const { rect, dpr, prompt } = msg;
  await openChat(tab);
  try {
    const shot = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
    const bmp = await createImageBitmap(await (await fetch(shot)).blob());
    const sx = Math.round(rect.x * dpr), sy = Math.round(rect.y * dpr);
    const sw = Math.max(1, Math.round(rect.w * dpr)), sh = Math.max(1, Math.round(rect.h * dpr));
    const canvas = new OffscreenCanvas(sw, sh);
    canvas.getContext("2d").drawImage(bmp, sx, sy, sw, sh, 0, 0, sw, sh);
    const b64 = await blobToB64(await canvas.convertToBlob({ type: "image/png" }));
    chrome.tabs.sendMessage(tab.id, { type: "oracle:chatAsk", message: (prompt || "").trim(),
                                      image: b64, thumb: "data:image/png;base64," + b64 })
      .catch(() => {});
  } catch (e) {
    chrome.tabs.sendMessage(tab.id, { type: "oracle:chatEvent", ev: { event: "error",
      data: { error: "Could not capture that region: " + (e.message || e) } } }).catch(() => {});
  }
}

async function chatReset(tab) {
  let host = "";
  try { host = new URL(tab.url).host; } catch (_) {}
  try {
    await fetch(RECEIVER + "/chat/reset", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host }),
    });
  } catch (_) {}
}

// ---------------------------------------------------------------- per-domain context (AGENTS.md)
//
// A page rarely explains its own vocabulary, and a model without that context produces something
// fluent and wrong. So before answering about a page, try the site's own agent brief at
// https://<host>/AGENTS.md — the convention this repo bets on — and send it along.
//
// The FETCH happens here, not in the receiver: the extension has host permissions and the page has
// already loaded from this host, so an authenticated or intranet site is reachable. The receiver is
// expected to work on a plane; it should not be the thing making outbound requests.
//
// Cached in chrome.storage, INCLUDING MISSES. Nearly every site lacks the file, and without a
// negative entry every explain/vision would re-request a 404 before it could answer.
const SITE_TTL = 24 * 3600 * 1000;
const AGENTS_TIMEOUT_MS = 2500;   // a bonus, never a dependency — see agentsMd()
const DEBUG_KEY = "oracleDebug";

// "I'm not sure it ever injects page context" should be answerable by looking, not by reading code.
// With debugging on, both sides report what they did: the extension says what it captured and sent,
// the receiver says what it composed. Same event stream, one tab in the card.
async function debugOn() {
  return (await chrome.storage.local.get(DEBUG_KEY))[DEBUG_KEY] === true;
}
function dbg(tab, data, mode = "vision") {
  chrome.tabs.sendMessage(tab.id, { type: "oracle:event", mode,
                                    ev: { event: "debug", data: { side: "extension", ...data } } })
    .catch(() => {});
}

async function agentsMd(url) {
  let host;
  try { host = new URL(url).host; } catch (_) { return null; }
  if (!host) return null;
  const key = "agentsmd:" + host;
  const hit = (await chrome.storage.local.get(key))[key];
  if (hit && Date.now() - hit.at < SITE_TTL) return hit.text;
  let text = "";
  try {
    // TIMEOUT, not optional. This is a request to the site the user happens to be on, and it sits
    // in front of every explain / fact-check / vision call. Without a deadline, any host that
    // accepts the connection and never answers hangs the feature forever — the card stays on
    // "Consulting the corpus…" and nothing says why, because the receiver was never even asked.
    // A page must not get to decide how long our own tooling takes.
    const r = await fetch(new URL("/AGENTS.md", url).href, {
      credentials: "omit", redirect: "follow", signal: AbortSignal.timeout(AGENTS_TIMEOUT_MS),
    });
    if (r.ok) {
      const ct = (r.headers.get("content-type") || "").toLowerCase();
      const body = (await r.text()).slice(0, 40000);
      // A single-page app answers 200 with its index.html for ANY path, so "the request succeeded"
      // is not evidence the file exists. Reject anything that is served as HTML or opens like it —
      // otherwise every SPA on the internet contributes its own markup as "site context".
      const looksHtml = ct.includes("text/html") || /^\s*(<!doctype|<html|<head|<script)/i.test(body);
      if (!looksHtml && body.trim().length > 40) text = body;
    }
  } catch (_) { /* offline, blocked, or no such host — a miss, cached like any other */ }
  await chrome.storage.local.set({ [key]: { text, at: Date.now() } });
  return text;
}

// ---------------------------------------------------------------- screenshot region -> vision model

async function screenshotRegion(tab, target = "vision") {
  if (!scriptableTab(tab)) { notify("Can't screenshot this page (only http/https)."); return; }
  // NO pre-flight check on /status here. An earlier version refused when status.vision was false
  // and it silently broke the feature the moment auto-swap landed: `vision:false` now means only
  // "not resident at this instant", not "unavailable", so the guard aborted before the selector was
  // ever injected and the crosshair never appeared. The request itself swaps the model in.
  try {
    // tell the selector where its result should go before it exists, so one selector serves both
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, args: [target],
                                           func: (t) => { window.__oracleRegionTarget = t; } });
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["regionselect.js"] });
  } catch (e) { notify("Region select failed: " + e.message); }
}

// A stream that ends without `done` or `error` is a stream that DIED — the receiver restarted, the
// socket dropped, the service worker was recycled. pumpSSE returns normally either way, so the card
// simply stopped updating and showed a half-answer or nothing at all, with no way to tell that from
// "still thinking". Say it instead.
async function pumpOrFail(body, send, what = "The answer") {
  let terminal = false;
  await pumpSSE(body, (ev) => {
    if (ev.event === "done" || ev.event === "error") terminal = true;
    send(ev);
  });
  if (!terminal) {
    send({ event: "error", data: { error:
      `${what} was cut off — the connection to the Oracle receiver ended early (it may have been ` +
      `restarted). Nothing was lost; ask again.` } });
  }
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
          // crop: the LOCATED text — that is what the walk is for.
          // page: plain innerText, the SAME extraction the right-click-an-image route uses.
          // It used to be walk(() => true), whose dedup + 400-node cap compress a dashboard to a
          // fraction of its prose — often under the receiver's 2500-char summarise threshold. Same
          // page, two entry points, one summarised and one not, for no reason a user could see. The
          // extractor should not decide whether the page gets summarised.
          return {
            crop: walk(hit).join(" · ").slice(0, 3000),
            page: clean(document.body && document.body.innerText).slice(0, 6000),
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
        source: "region", agents_md: await agentsMd(tab.url), debug: await debugOn(),
      }),
    });
    if (!r.ok || !r.body) { send({ event: "error", data: { error: "receiver error " + r.status } }); return; }
    await pumpOrFail(r.body, send);
  } catch (e) {
    // make sure the card exists to show the error
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["cite.js", "overlay.js"] });
      await chrome.tabs.sendMessage(tab.id, { type: "oracle:loading", mode: "vision", thumb });
    } catch (_) {}
    send({ event: "error", data: { error: "Vision failed: " + (e.message || e) + " (is the receiver + qwen3-vl up?)" } });
  }
}

// "Explain this page" — the region flow with the rectangle already drawn around the whole viewport.
//
// Both halves of the machine, on whatever is on screen: the page's own text (summarised by the text
// model first, while it is still the resident one and therefore free) and a screenshot of the
// visible area read by qwen3-vl. Neither half is sufficient on a real dashboard — the text knows
// the datasource and dashboard ID, the pixels know which line went vertical at 14:20.
//
// Capture BEFORE injecting the card, or the overlay ends up inside the screenshot it is about.
// Capture the WHOLE page, not just the viewport: scroll, shoot, stitch.
//
// captureVisibleTab only ever returns the visible rectangle, so a full page means driving the
// scroll ourselves. Three things this has to get right, none of them optional:
//
//   * A CAP. An infinite-scroll feed has no bottom; scrollHeight grows as you approach it. Bounded
//     by slice count AND by pixels, so "explain this page" on Twitter terminates.
//   * STICKY CHROME. A fixed nav bar is painted into every single slice, so a naive stitch shows
//     the same header six times and the model dutifully describes a page with six headers. Hidden
//     after the first slice, restored afterwards.
//   * PUTTING THE SCROLL BACK. The user did not ask to be moved to the bottom of the page.
const FULLPAGE_MAX_SLICES = 6;
const FULLPAGE_MAX_CSS_PX = 12000;
const FULLPAGE_MAX_DEVICE_PX = 9000;   // beyond this the image costs more tokens than it informs

async function fullPageShot(tab) {
  const run = (func, args) => chrome.scripting.executeScript({ target: { tabId: tab.id }, func, args })
    .then(([r]) => r?.result);

  const m = await run(() => ({
    y0: window.scrollY, vh: window.innerHeight, dpr: window.devicePixelRatio || 1,
    h: Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0),
    vw: window.innerWidth,
  }));
  if (!m) throw new Error("could not measure the page");

  const total = Math.min(m.h, FULLPAGE_MAX_CSS_PX, m.vh * FULLPAGE_MAX_SLICES);
  const capped = total < m.h;
  const shots = [];
  try {
    for (let y = 0, n = 0; y < total && n < FULLPAGE_MAX_SLICES; y += m.vh, n++) {
      // hide sticky/fixed chrome from the second slice on — it is already in the first
      const at = await run((args) => {
        window.scrollTo(0, args.y);
        if (args.hide) {
          for (const el of document.querySelectorAll("body *")) {
            const p = getComputedStyle(el).position;
            if ((p === "fixed" || p === "sticky") && el.getClientRects().length) {
              el.setAttribute("data-oracle-hidden", el.style.visibility || "");
              el.style.visibility = "hidden";
            }
          }
        }
        return window.scrollY;
      }, [{ y, hide: n > 0 }]);
      // give the page a beat to paint and to fire lazy-loading, and stay under the
      // captureVisibleTab rate limit
      await new Promise((r) => setTimeout(r, 260));
      shots.push({ y: at ?? y, data: await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" }) });
      if (at !== null && at + m.vh >= m.h - 1) break;      // hit the bottom
    }
  } finally {
    await run((y0) => {
      document.querySelectorAll("[data-oracle-hidden]").forEach((el) => {
        el.style.visibility = el.getAttribute("data-oracle-hidden") || "";
        el.removeAttribute("data-oracle-hidden");
      });
      window.scrollTo(0, y0);
    }, [m.y0]).catch(() => {});
  }
  if (!shots.length) throw new Error("no slices captured");

  const bmps = await Promise.all(shots.map(async (s) =>
    createImageBitmap(await (await fetch(s.data)).blob())));
  const w = bmps[0].width;
  const lastBottom = shots[shots.length - 1].y * m.dpr + bmps[bmps.length - 1].height;
  let h = Math.min(Math.round(lastBottom), FULLPAGE_MAX_DEVICE_PX);
  const canvas = new OffscreenCanvas(w, h);
  const ctx = canvas.getContext("2d");
  shots.forEach((s, i) => ctx.drawImage(bmps[i], 0, Math.round(s.y * m.dpr)));
  const b64 = await blobToB64(await canvas.convertToBlob({ type: "image/png" }));
  return {
    b64, slices: shots.length,
    capped: capped || h >= FULLPAGE_MAX_DEVICE_PX,
    pageCssHeight: m.h, capturedCssHeight: Math.round(h / m.dpr),
  };
}

async function visionPage(tab) {
  if (!scriptableTab(tab)) { notify("Can't screenshot this page (only http/https)."); return; }
  const send = (ev) => chrome.tabs.sendMessage(tab.id, { type: "oracle:event", mode: "vision", ev }).catch(() => {});
  let thumb = null;
  try {
    let pageText = "";
    try {
      const [pt] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => (document.body && document.body.innerText || "").replace(/\s+/g, " ").trim().slice(0, 6000),
      });
      pageText = pt?.result || "";
    } catch (_) { /* some pages refuse injection; the pixels still work */ }

    const debug = await debugOn();
    const shot = await fullPageShot(tab);
    thumb = "data:image/png;base64," + shot.b64;
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["cite.js", "overlay.js"] });
    await chrome.tabs.sendMessage(tab.id, { type: "oracle:loading", mode: "vision", thumb });
    if (debug) {
      dbg(tab, { stage: "full-page screenshot", slices: shot.slices, capped: shot.capped,
                 page_css_height: shot.pageCssHeight, captured_css_height: shot.capturedCssHeight,
                 png_kb: Math.round(shot.b64.length * 0.75 / 1024) });
      dbg(tab, { stage: "sent to receiver", page_text_chars: pageText.length, source: "fullpage",
                 text: pageText.slice(0, 4000) });
    }
    const r = await fetch(RECEIVER + "/vision", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        image: shot.b64, mime: "image/png",
        prompt: "Explain this page: what is it, what is it showing, and what should I notice?",
        url: tab.url, title: tab.title, page_text: pageText, crop_text: "", source: "fullpage",
        agents_md: await agentsMd(tab.url), debug,
      }),
    });
    if (!r.ok || !r.body) { send({ event: "error", data: { error: "receiver error " + r.status } }); return; }
    await pumpOrFail(r.body, send);
  } catch (e) {
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["cite.js", "overlay.js"] });
      await chrome.tabs.sendMessage(tab.id, { type: "oracle:loading", mode: "vision", thumb });
    } catch (_) {}
    send({ event: "error", data: { error: "Vision failed: " + (e.message || e) + " (is the receiver + qwen3-vl up?)" } });
  }
}

// Right-click an image -> read THAT image, no rectangle to drag.
//
// The image's own markup is the best context there is: alt text, title, and a <figcaption> are
// written precisely to say what the picture means, which is the thing pixels cannot state. A model
// given the picture alone would re-derive (or invent) what the author already wrote down.
async function visionImage(srcUrl, tab) {
  if (!srcUrl) { notify("No image found under the cursor."); return; }
  const send = (ev) => chrome.tabs.sendMessage(tab.id, { type: "oracle:event", mode: "vision", ev }).catch(() => {});
  let thumb = null;
  try {
    // Ask the PAGE for the metadata and, if it can, the bytes. Doing the encode in the page reuses
    // the already-decoded image and works for blob:/data: sources the worker cannot fetch.
    let meta = { alt: "", title: "", caption: "", near: "", dataUrl: "" };
    try {
      const [r] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        args: [srcUrl],
        func: (src) => {
          const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
          const imgs = [...document.images];
          const el = imgs.find((i) => i.currentSrc === src) || imgs.find((i) => i.src === src);
          if (!el) return { alt: "", title: "", caption: "", near: "", dataUrl: "" };
          const fig = el.closest("figure");
          const cap = fig && fig.querySelector("figcaption");
          // a little surrounding prose, for images whose alt is empty or decorative
          const host = el.closest("figure, article, section, div") || el.parentElement;
          const near = clean(host && host.innerText).slice(0, 800);
          let dataUrl = "";
          try {
            const c = document.createElement("canvas");
            c.width = el.naturalWidth || el.width;
            c.height = el.naturalHeight || el.height;
            if (c.width && c.height) {
              c.getContext("2d").drawImage(el, 0, 0);
              dataUrl = c.toDataURL("image/png");   // throws if the canvas is cross-origin tainted
            }
          } catch (_) { dataUrl = ""; }
          return {
            alt: clean(el.alt), title: clean(el.title),
            caption: clean(cap && cap.textContent), near, dataUrl,
            page: clean(document.body && document.body.innerText).slice(0, 6000),
          };
        },
      });
      meta = r?.result || meta;
    } catch (_) { /* injection refused; fall back to fetching the URL */ }

    let b64 = "", mime = "image/png";
    if (meta.dataUrl) {
      b64 = meta.dataUrl.split(",")[1];
    } else {
      // Cross-origin images taint the canvas, so fetch the bytes here instead — the worker has
      // host permissions the page context does not.
      const resp = await fetch(srcUrl);
      const blob = await resp.blob();
      mime = blob.type || "image/png";
      b64 = await blobToB64(blob);
    }
    if (!b64) { notify("Could not read that image."); return; }
    thumb = `data:${mime};base64,${b64}`;

    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["cite.js", "overlay.js"] });
    await chrome.tabs.sendMessage(tab.id, { type: "oracle:loading", mode: "vision", thumb });
    const r = await fetch(RECEIVER + "/vision", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        image: b64, mime, prompt: "",
        url: tab.url, title: tab.title,
        image_alt: meta.alt, image_title: meta.title, image_caption: meta.caption,
        // `near` is prose AROUND the image, not text inside it — `source` tells the receiver to
        // label it as such instead of claiming it was rendered inside a cropped rectangle.
        crop_text: meta.near, page_text: meta.page || "", source: "image",
        agents_md: await agentsMd(tab.url), debug: await debugOn(),
      }),
    });
    if (!r.ok || !r.body) { send({ event: "error", data: { error: "receiver error " + r.status } }); return; }
    await pumpOrFail(r.body, send);
  } catch (e) {
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["cite.js", "overlay.js"] });
      await chrome.tabs.sendMessage(tab.id, { type: "oracle:loading", mode: "vision", thumb });
    } catch (_) {}
    send({ event: "error", data: { error: "Image read failed: " + (e.message || e) } });
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
      body: JSON.stringify({ selection: text.slice(0, 1500), url: tab.url, title: tab.title,
                             agents_md: await agentsMd(tab.url) }),
    });
    if (!r.ok || !r.body) { send({ event: "error", data: { error: "receiver error " + r.status } }); return; }
    await pumpOrFail(r.body, send);
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
