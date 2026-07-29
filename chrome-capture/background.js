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
  if (msg.type === "oracle:chatClosed") {
    if (sender.tab) markChatTab(sender.tab.id, false);
    return false;
  }
  if (msg.type === "chatConfirm") {
    const finish = pendingConfirms.get(msg.id);
    if (finish) finish(Boolean(msg.ok));
    sendResponse({ ok: Boolean(finish) });
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
  if (msg.type === "oracle:chat") {
    if (sender.tab) chatSend(msg.message, sender.tab, msg.image || "", msg.session, msg.source);
    return false;
  }
  if (msg.type === "oracle:chatLoad") { if (sender.tab) chatLoad(sender.tab, msg.session); return false; }
  if (msg.type === "oracle:chatReset") { if (sender.tab) chatReset(sender.tab); return false; }
  if (msg.type === "oracle:chatResume") { if (sender.tab) chatResume(sender.tab); return false; }
  if (msg.type === "oracle:chatAllow") { if (sender.tab) chatAllow(sender.tab, msg.allow); return false; }
  if (msg.type === "oracle:setDebug") { chrome.storage.local.set({ [DEBUG_KEY]: !!msg.on }); return false; }
  if (msg.type === "oracle:chatSessions") { if (sender.tab) chatSessions(sender.tab); return false; }
  if (msg.type === "oracle:chatDelete") {
    if (sender.tab) chatDelete(sender.tab, msg.host, msg.session);
    return false;
  }
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

// Explain and fact-check now go through the CHAT, into a session of their own.
//
// They used to be one-shot cards: an answer appeared, you read it, it closed, and anything you
// wanted to ask next started from nothing. Routing them through the chat means they get the same
// memory, the same tools (they can look at the page rather than guess at it), and a transcript you
// can pick up — while staying separate from the conversation you type in, because "what does this
// phrase mean" and "explain this whole run to me" are not the same thread.
async function ground(tab, mode, selection) {
  selection = (selection || "").trim();
  if (!scriptableTab(tab) || !selection) return;
  observe(selection, OBS[mode] || 0.8, tab.url, tab.title);
  await openChat(tab);
  const framed = mode === "factcheck"
    ? `Fact-check this claim from the page, against the corpus:\n\n"${selection.slice(0, 2000)}"`
    : `Explain this, from the page I'm reading:\n\n"${selection.slice(0, 2000)}"`;
  chrome.tabs.sendMessage(tab.id, { type: "oracle:chatAsk", message: framed, session: QUICK })
    .catch(() => {});
}

// The old one-shot path, still used by the ⚓ "ground this" button on a vision card.
async function groundOnce(tab, mode, selection) {
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
    dbg(tab, { stage: "debug is OFF — tick “debug” in the Oracle popup and ask again to see " +
                      "every event and the full prompt" }, mode);
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

// Tabs the panel is open in, so a navigation can put it back.
//
// The panel is injected into the page, so ANY navigation destroys it — including the model's own
// `navigate`. That produced the worst possible moment for it to vanish: mid-turn, on a step the
// model took deliberately, with the answer still coming. The conversation itself was never lost
// (it lives on the receiver), but you had to know that, and reopening by hand to discover the turn
// had continued without you is not a thing a user should have to learn.
//
// chrome.storage.session, not a module variable: MV3 kills the worker whenever it feels like it,
// and a Set in memory would forget which tabs mattered exactly when a slow turn needed it most. It
// clears on browser restart, which is the right lifetime — a panel open yesterday should not
// reappear on a tab you opened today.
const CHAT_TABS = "oracleChatTabs";

async function chatTabs() {
  try {
    return (await chrome.storage.session.get(CHAT_TABS))[CHAT_TABS] || {};
  } catch (_) {
    return {};
  }
}

async function markChatTab(tabId, open) {
  try {
    const t = await chatTabs();
    if (open) t[tabId] = true; else delete t[tabId];
    await chrome.storage.session.set({ [CHAT_TABS]: t });
  } catch (_) { /* reopening is a convenience; never break the chat over it */ }
}

async function openChat(tab) {
  if (!scriptableTab(tab)) { notify("Chat needs an http/https page."); return; }
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["cite.js", "chat.js"] });
  await markChatTab(tab.id, true);
}

// Put the panel back after the page underneath it was replaced, and restore the transcript so it
// returns showing the conversation rather than an empty box.
async function reopenChat(tab) {
  if (!scriptableTab(tab)) return;
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id },
                                           files: ["cite.js", "chat.js"] });
    await chatLoad(tab);
  } catch (_) { /* the tab may have gone again already */ }
}

chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  // "complete" only: injecting into a document that is still being replaced loses the panel again.
  if (info.status !== "complete") return;
  const open = await chatTabs();
  if (open[tabId]) await reopenChat(tab);
});

chrome.tabs.onRemoved.addListener((tabId) => { markChatTab(tabId, false); });

// Which conversation the panel is showing. "main" is the chat you type in; "quick" is where
// explain / fact-check / a region sent to vision land, so those stop being cards that vanish and
// become a transcript with the same tools and the same memory.
const QUICK = "quick";
let chatSession = "main";

async function chatLoad(tab, session) {
  if (session) chatSession = session;
  // The transcript lives on the receiver, so reopening the panel — in another tab, another window,
  // or after a restart — shows the conversation that is actually stored rather than a fresh one.
  let host = "";
  try { host = new URL(tab.url).host; } catch (_) {}
  try {
    const r = await fetch(`${RECEIVER}/chat/history?host=${encodeURIComponent(host)}` +
                          `&session=${encodeURIComponent(chatSession)}`);
    const d = r.ok ? await r.json() : { turns: [] };
    chrome.tabs.sendMessage(tab.id, { type: "oracle:chatHistory", host, turns: d.turns || [],
                                      epoch: d.epoch, actions: !!d.actions,
                                      session: chatSession,
                                      debug: await debugOn() }).catch(() => {});
  } catch (_) {
    chrome.tabs.sendMessage(tab.id, { type: "oracle:chatHistory", host, turns: [],
                                      session: chatSession }).catch(() => {});
  }
}

async function chatSend(message, tab, image = "", session = null, source = "") {
  if (session) chatSession = session;
  // Stamp every event with the session it belongs to, so the panel can ignore ones for a
  // conversation it is no longer showing.
  const forSession = chatSession;
  const send = (ev) => chrome.tabs
    .sendMessage(tab.id, { type: "oracle:chatEvent", ev, session: forSession }).catch(() => {});
  let host = "";
  try { host = new URL(tab.url).host; } catch (_) {}
  const debug = await debugOn();
  let where = { around: "", headings: "", page: "" };
  try {
    const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id },
                                                       func: extractSelectionContext });
    where = r?.result || where;
  } catch (_) { /* injection refused; the question still works */ }
  // Always emit one, so an empty Debug tab can never mean two different things. The overlay card
  // got this fix; the chat panel did not, and it looked broken for exactly the same reason.
  send({ event: "debug", data: debug
    ? { side: "extension", stage: "chat send", host, around_chars: where.around.length,
        headings: where.headings, page_fallback_chars: where.page.length }
    : { side: "extension", stage: "debug is OFF — click the 🐞 in the panel's title bar (or tick " +
                                  "“debug” in the Oracle popup) and ask again" } });
  // Keepalive for the WHOLE turn, not just the tool steps. A first model call with tools, history
  // and a possible GPU swap runs well past the ~30s idle timeout, and a worker killed here leaves
  // the receiver's turn recorded, the panel spinning, and nothing to explain either — which is
  // exactly what "thinking for 7 minutes" was.
  keepaliveStart();
  try {
    const r = await fetch(RECEIVER + "/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, host, url: tab.url, title: tab.title,
                             agents_md: await agentsMd(tab.url), debug, where,
                             image, mime: "image/png", session: chatSession,
                             source: source || "region" }),
    });
    if (!r.ok || !r.body) { send({ event: "error", data: { error: "receiver error " + r.status } }); return; }
    await chatPump(r.body, send, tab);
  } catch (e) {
    send({ event: "error", data: { error: "Lost the connection to Oracle: " + (e.message || e) } });
  } finally {
    keepaliveStop();
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
                                      image: b64, thumb: await thumbnail(b64),
                                      session: QUICK })
      .catch(() => {});
  } catch (e) {
    chrome.tabs.sendMessage(tab.id, { type: "oracle:chatEvent", ev: { event: "error",
      data: { error: "Could not capture that region: " + (e.message || e) } } }).catch(() => {});
  }
}

// ---------------------------------------------------------------- browser tools
//
// The receiver decides WHAT to do and cannot do any of it: it has no DOM. So it ends a turn with a
// tool_request, and this is the half that owns the hand.
//
// Every tool returns what ACTUALLY happened, not what was attempted — "clicked LOGS; the page is
// now …" or "no element matches LOGS; the clickable labels are …". A tool that reports its
// intention rather than its outcome is how a model ends up confidently narrating a click that
// missed, which is this repo's oldest failure mode wearing gloves.
const CHAT_MAX_HANDOFFS = 12;

// KEEPALIVE. An MV3 service worker is torn down after ~30s of inactivity, and a long-running fetch
// does not reliably count — a look_at_page round trip is a screenshot, a GPU swap, a vision read
// and a swap back, comfortably two minutes, and if the worker dies mid-flight nobody ever posts the
// result. The receiver is left holding an unanswered tool call and the panel spins forever, which
// is exactly what he saw twice.
//
// The documented way to stay alive is to keep touching an extension API. So do that, explicitly,
// for as long as a tool is running — a harness that needs to be awake should keep itself awake
// rather than hope.
let _keepalive = null;
let _keepaliveDepth = 0;

function keepaliveStart() {
  _keepaliveDepth++;
  if (_keepalive) return;
  _keepalive = setInterval(() => chrome.runtime.getPlatformInfo().catch(() => {}), 20000);
}

function keepaliveStop() {
  _keepaliveDepth = Math.max(0, _keepaliveDepth - 1);
  if (_keepaliveDepth === 0 && _keepalive) { clearInterval(_keepalive); _keepalive = null; }
}

// injected: read the page, or one part of it
function toolReadPage(selector) {
  // Hide Oracle's own UI for the duration of the read, exactly as a screenshot does.
  //
  // The panel normally hangs off <html> rather than <body>, so a plain read never saw it — but the
  // model has used `read_page` with `:root`, which does, and since the panel is now restored after
  // every navigation it is reliably present. Reading its own transcript back in as page content is
  // the worst kind of context pollution: it is plausible, it is about the right topic, and it
  // rewards the model for describing what it already said.
  // Inlined deliberately: this function is serialised and injected on its own, so a call to a
  // sibling defined here in the worker would be a ReferenceError in the page.
  // Declared INSIDE the function: this is serialised and injected on its own, so a
  // module-scope constant would be a ReferenceError in the page. ~6k tokens against a
  // 131,072-token slot; the old 8,000 was ~1.5% of it and made a heavy page unreadable in
  // one go, pushing the model toward a screenshot for text it could have been handed.
  const READ_PAGE_CHARS = 24000;
  const ours = [...document.querySelectorAll("[data-oracle-ui]")];
  const prev = ours.map((n) => n.style.display);
  ours.forEach((n) => { n.style.display = "none"; });
  const restore = () => ours.forEach((n, i) => { n.style.display = prev[i]; });

  const el = selector ? document.querySelector(selector) : document.body;
  if (!el) {
    // A miss must be as useful as a hit, or the model just guesses another selector — which is
    // exactly what happened: it invented div[data-testid="metrics-panel"], got a bare "no element
    // matches", and had nothing to correct itself with. Selectors are GUESSES about a page nobody
    // showed it; the reply should make the next attempt unnecessary rather than merely possible.
    const text = (document.body.innerText || "").replace(/\s+/g, " ").trim();
    restore();
    return `no element matches ${selector} — that selector was a guess and the page does not have ` +
           `it. Call read_page with NO selector to get the whole page, which is usually what you ` +
           `want. Here it is anyway:\n${text.slice(0, READ_PAGE_CHARS)}`;
  }
  const t = (el.innerText || "").replace(/\s+/g, " ").trim();
  restore();
  return t ? t.slice(0, READ_PAGE_CHARS) : "(that element renders no text)";
}

// injected: click by visible text, or by selector
function toolClick(arg) {
  const want = (arg.text || "").trim().toLowerCase();
  let el = null;
  if (arg.selector) {
    // querySelector THROWS on invalid CSS, which killed the whole injected step and returned
    // nothing. `:contains()` is the usual culprit: it is jQuery, it looks like CSS, and models
    // reach for it constantly because it expresses exactly what they want. Answer with the thing
    // that does work here rather than with an exception.
    try {
      el = document.querySelector(arg.selector);
    } catch (e) {
      return `NOT CLICKED: ${JSON.stringify(arg.selector)} is not valid CSS (${e.message}). ` +
             (/contains/i.test(arg.selector)
               ? "`:contains(...)` is jQuery and does not exist in CSS. "
               : "") +
             "To click by visible text, pass `text` instead of `selector` — this tool matches " +
             "visible labels itself and prefers the smallest matching element.";
    }
  }
  if (!el && want) {
    const cand = [...document.querySelectorAll(
      "button, a, [role=tab], [role=button], input[type=submit], summary, li, div, span")];
    const vis = (n) => n.getClientRects().length > 0;
    // exact visible label first, then a contains-match; prefer the smallest matching element so a
    // wrapping <div> never wins over the actual control inside it
    const exact = cand.filter((n) => vis(n) && (n.innerText || n.value || "").trim().toLowerCase() === want);
    const part = cand.filter((n) => vis(n) && (n.innerText || "").trim().toLowerCase().includes(want));

    // ASK THE FRAMEWORK which element owns the click.
    //
    // Diagnosed on the live wizard: clicking "Start" reported success and did nothing, every time.
    // It was matching a <div> that merely contained the word. A wrapper and the button inside it
    // have identical innerText, so "smallest text wins" is a tie and document order hands it to the
    // wrapper — and `closest()` cannot rescue it, because the button is a DESCENDANT.
    //
    // Guessing from tag names only half-fixes that: this app renders controls through shadcn/Radix,
    // where `asChild` can put the handler on an <a>, a <div>, or anything else. But React stores a
    // node's props ON the node, under a `__reactProps$<hash>` key — so we can simply ask which
    // element has an onClick. That is the ground truth the DOM shape only approximates, and it
    // works for anything React renders rather than for a list of tags we happened to think of.
    const reactProps = (n) => {
      const k = Object.keys(n).find((x) => x.startsWith("__reactProps$"));
      return k ? n[k] : null;
    };
    const hasHandler = (n) => {
      const p = reactProps(n);
      return Boolean(p && (p.onClick || p.onPointerDown || p.onMouseDown));
    };
    // Fallback for non-React pages: things that are clickable by their nature.
    const nativelyClickable = (n) =>
      /^(BUTTON|A|SUMMARY|LABEL|SELECT|INPUT|TEXTAREA)$/.test(n.tagName) ||
      Boolean(n.getAttribute("role")) || typeof n.onclick === "function";

    el = (exact.length ? exact : part).sort((a, b) => {
      const byHandler = Number(hasHandler(b)) - Number(hasHandler(a));
      if (byHandler) return byHandler;
      const byNative = Number(nativelyClickable(b)) - Number(nativelyClickable(a));
      if (byNative) return byNative;
      return (a.innerText || "").length - (b.innerText || "").length;
    })[0] || null;

    // If the winner still owns no handler, look INSIDE it for the control it wraps.
    if (el && !hasHandler(el) && !nativelyClickable(el)) {
      const inner = [...el.querySelectorAll("*")].filter(
        (n) => vis(n) && (hasHandler(n) || nativelyClickable(n)) &&
               (n.innerText || "").trim().toLowerCase().includes(want));
      if (inner.length) el = inner[0];
    }
  }
  if (!el) {
    const labels = [...document.querySelectorAll("button, a, [role=tab]")]
      .filter((n) => n.getClientRects().length)
      .map((n) => (n.innerText || "").replace(/\s+/g, " ").trim()).filter(Boolean).slice(0, 40);
    return `NOT CLICKED: nothing matches ${JSON.stringify(arg.text || arg.selector)}. ` +
           `Clickable labels on this page: ${labels.join(" | ")}`;
  }
  // A disabled control accepts .click() silently and does nothing, which reads to the model as "I
  // clicked it and the page ignored me" — and it responded to that by clicking Start a second time
  // and then deciding the page was stuck. Whether a button is disabled is the single most useful
  // fact about a click that appears to do nothing, and it is knowable before clicking.
  const off = el.disabled || el.getAttribute("aria-disabled") === "true" ||
              el.closest("[disabled], [aria-disabled=true]");
  if (off) {
    return `NOT CLICKED: ${JSON.stringify((el.innerText || el.value || el.tagName).trim().slice(0, 60))} ` +
           `is DISABLED, so clicking it does nothing. This is not a stuck page — the form is not ` +
           `satisfied yet. Find what it is still missing (required fields, an unfinished step) ` +
           `rather than clicking again.`;
  }
  // Click the thing that HANDLES the click, not the text node inside it. A label matched by text is
  // often a <span> inside the real control, and shadcn's `asChild` puts the handler on whatever
  // element it was given — so walk up to the nearest actionable ancestor when there is one.
  const target = el.closest("button, [role=button], a[href], [role=tab], [role=menuitem], [role=option], label, summary") || el;

  const before = { url: location.href, title: document.title };
  target.scrollIntoView({ block: "center" });

  // A REAL click, not just `.click()`.
  //
  // `.click()` dispatches a lone `click` event. This app is built on Radix (dialog, dropdown-menu,
  // collapsible, select), and Radix triggers act on POINTERDOWN — so a bare click lands on nothing
  // and the tool cheerfully reports success. That is exactly what was observed: every click
  // reported `{"clicked":"Start"}` and the page never moved.
  //
  // So send the sequence a mouse actually produces. Plain React onClick handlers are unaffected;
  // pointer-driven components finally respond.
  const r = target.getBoundingClientRect();
  const base = {
    bubbles: true, cancelable: true, composed: true, view: window,
    clientX: Math.round(r.left + r.width / 2), clientY: Math.round(r.top + r.height / 2),
    button: 0, detail: 1,
  };
  const pointer = { ...base, pointerId: 1, pointerType: "mouse", isPrimary: true };
  try {
    target.dispatchEvent(new PointerEvent("pointerover", { ...pointer, buttons: 0 }));
    target.dispatchEvent(new MouseEvent("mouseover", { ...base, buttons: 0 }));
    target.dispatchEvent(new PointerEvent("pointerdown", { ...pointer, buttons: 1 }));
    target.dispatchEvent(new MouseEvent("mousedown", { ...base, buttons: 1 }));
    if (typeof target.focus === "function") target.focus();
    target.dispatchEvent(new PointerEvent("pointerup", { ...pointer, buttons: 0 }));
    target.dispatchEvent(new MouseEvent("mouseup", { ...base, buttons: 0 }));
  } catch (_) {
    /* PointerEvent is universally available in Chrome; never let the sequence block the click */
  }
  target.click();

  // Report WHAT was hit, precisely. "clicked: Start" was true and useless — it did not say whether
  // the thing clicked was a button or a div that merely contained the word.
  const ident = {
    tag: target.tagName.toLowerCase(),
    role: target.getAttribute("role") || undefined,
    type: target.getAttribute("type") || undefined,
    actionable: target !== el || /^(BUTTON|A|SUMMARY|LABEL)$/.test(target.tagName) ||
                Boolean(target.getAttribute("role")),
  };
  return JSON.stringify({
    clicked: (target.innerText || target.value || target.tagName).trim().slice(0, 80),
    element: ident, before,
    ...(ident.actionable ? {} : {
      warning: "the matched element is not a button, link or anything with a role — the text may " +
               "have been found in a plain container that has no click handler at all",
    }),
    note: "a full pointer sequence was sent (Radix and similar act on pointerdown, not click)",
  });
}

// injected: type into a field
//
// Setting `el.value` does not type into a React input, and this cost a real run.
//
// Observed on the new-run wizard: the model filled in the run name, the tool replied "its value is
// now 'orioledb tpcc baseline'", it clicked Start, and nothing happened — twice, after which it
// concluded the page was stuck. It was not stuck. React replaces `value` on the node with its own
// setter that also updates an internal `_valueTracker`; assigning through it updates the tracker
// too, so when the input event arrives React compares tracker against value, sees no change, and
// never dispatches onChange. The DOM said one thing and the application state said another, and the
// form submitted the state.
//
// So write through the NATIVE prototype setter, which React did not override. The tracker keeps the
// old value, the input event looks like a real change, onChange fires, state updates. Same trick
// works for Vue and Svelte, which track values the same way.
//
// The second bug is worse than the first: the old version "verified" by reading back the property
// it had just assigned, which cannot disagree. It reported success for an edit the application
// never saw. Verification now happens AFTER a tick, so a framework that re-renders and reverts the
// field is caught and reported — the tool tells you it failed instead of letting the model spend
// three steps discovering it.
async function toolType(arg) {
  let el = null;
  try {
    el = document.querySelector(arg.selector);
  } catch (e) {
    return `NOT TYPED: ${JSON.stringify(arg.selector)} is not valid CSS (${e.message}). ` +
           "`:contains(...)` is jQuery, not CSS. Read the form's controls first — each one comes " +
           "back with a selector that already resolved.";
  }
  if (!el) {
    const fields = [...document.querySelectorAll("input, textarea, select")]
      .filter((n) => n.getClientRects().length)
      .map((n) => n.getAttribute("placeholder") || n.getAttribute("name") || n.id || n.tagName)
      .filter(Boolean).slice(0, 25);
    return `NOT TYPED: no element matches ${arg.selector}. Fields on this page: ${fields.join(" | ")}`;
  }
  if (el.disabled || el.getAttribute("aria-disabled") === "true") {
    return `NOT TYPED: ${arg.selector} is disabled. Something earlier in the form probably has to ` +
           `be set first — read the form's state rather than retrying this.`;
  }
  const want = arg.clear === false ? (el.value || "") + (arg.text || "") : (arg.text || "");
  el.focus();
  const proto = el instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const native = Object.getOwnPropertyDescriptor(proto, "value");
  if (native && native.set) native.set.call(el, want);
  else el.value = want;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));

  // Let the framework re-render, then look at what is actually there.
  await new Promise((r) => setTimeout(r, 120));
  const got = el.value || "";
  if (got !== want) {
    return `NOT TYPED: the field did not keep the text — it now reads ${JSON.stringify(got.slice(0, 80))} ` +
           `instead of ${JSON.stringify(want.slice(0, 80))}. The page is controlling this input and ` +
           `rejected or rewrote the value. Do NOT retry the same way; check the form's state and ` +
           `whether some other field has to be set first.`;
  }
  return `typed into ${arg.selector}; the field now reads ${JSON.stringify(got.slice(0, 200))} ` +
         `and the page accepted it (verified after re-render)`;
}

// ---------------------------------------------------------------- confirmed actions
//
// "Safe click": the model picks the target, the human presses the key.
//
// The split is along the natural seam. Choosing WHICH element is what the model is good at — it
// has just read the page and knows what the user asked for. Deciding WHETHER to press it is what
// carries the consequence, and in a mail client Archive, Delete and Send sit a few pixels apart.
// The old binary made you choose between a model that could see the button but not press it, and
// one that could press anything at all.
//
// It also changes WHEN you find out. With allow-all, the record of an action is a line of text
// after the fact. Here the actual element lights up on the actual page, before anything happens,
// and doing nothing is the safe default: no keypress, no click.
const pendingConfirms = new Map();

// injected: find what a click WOULD hit, outline it, and report it — without clicking.
function toolPreview(arg) {
  const want = (arg.text || "").trim().toLowerCase();
  let el = null;
  if (arg.selector) {
    try {
      el = document.querySelector(arg.selector);
    } catch (_) {
      return { found: false, invalidSelector: arg.selector };
    }
  }
  if (!el && want) {
    const cand = [...document.querySelectorAll(
      "button, a, [role=tab], [role=button], input[type=submit], summary, li, div, span")];
    const vis = (n) => n.getClientRects().length > 0;
    const exact = cand.filter((n) => vis(n) && (n.innerText || n.value || "").trim().toLowerCase() === want);
    const part = cand.filter((n) => vis(n) && (n.innerText || "").trim().toLowerCase().includes(want));
    // Prefer something CLICKABLE over the smallest match.
    //
    // Diagnosed on the live wizard: clicking "Start" reported success and did nothing, over and
    // over. It was matching a <div> that merely contained the word. A wrapper div and the button
    // inside it have identical innerText, so "smallest text wins" is a tie, and document order puts
    // the wrapper first — the container always beat the control. `closest()` cannot fix it either,
    // because the button is a DESCENDANT of the div, not an ancestor.
    //
    // So rank by whether the element can plausibly handle a click, and only then by how tight the
    // match is. A container that wraps the real control is the one thing that must never win.
    const actionable = (n) =>
      /^(BUTTON|A|SUMMARY|LABEL|SELECT|TEXTAREA|INPUT)$/.test(n.tagName) ||
      Boolean(n.getAttribute("role")) || typeof n.onclick === "function" ||
      n.hasAttribute("data-testid");
    el = (exact.length ? exact : part).sort((a, b) => {
      const byAction = Number(actionable(b)) - Number(actionable(a));
      if (byAction) return byAction;
      return (a.innerText || "").length - (b.innerText || "").length;
    })[0] || null;
    // If the winner is still a plain container, look INSIDE it for the control it wraps.
    if (el && !actionable(el)) {
      const inner = [...el.querySelectorAll("button, a[href], [role=button], [role=tab], summary")]
        .filter((n) => vis(n) && (n.innerText || "").trim().toLowerCase().includes(want));
      if (inner.length) el = inner[0];
    }
  }
  document.querySelectorAll("[data-oracle-target]").forEach((n) => {
    n.removeAttribute("data-oracle-target");
    n.style.outline = n.dataset.oraclePrevOutline || "";
    delete n.dataset.oraclePrevOutline;
  });
  if (!el) return { found: false };
  el.dataset.oraclePrevOutline = el.style.outline;
  el.setAttribute("data-oracle-target", "1");
  el.style.outline = "3px solid #f5a623";
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  const r = el.getBoundingClientRect();
  return {
    found: true,
    label: (el.innerText || el.value || el.tagName).replace(/\s+/g, " ").trim().slice(0, 90),
    tag: el.tagName.toLowerCase(),
    href: el.getAttribute("href") || "",
    rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
  };
}

// injected: drop the highlight again, whatever the user decided.
function toolPreviewClear() {
  document.querySelectorAll("[data-oracle-target]").forEach((n) => {
    n.removeAttribute("data-oracle-target");
    n.style.outline = n.dataset.oraclePrevOutline || "";
    delete n.dataset.oraclePrevOutline;
  });
}

// Ask the panel, and wait. Resolves true (do it), false (skip it).
//
// A timeout is required, not optional: this runs inside a tool loop that is holding a model lease,
// and a user who walks away must not wedge the queue for everyone. Timing out counts as NOT
// confirmed, because the whole design rests on inaction being the safe outcome.
function askConfirm(call, preview, send) {
  return new Promise((resolve) => {
    const id = call.id;
    const finish = (ok) => {
      if (!pendingConfirms.has(id)) return;
      clearTimeout(timer);
      pendingConfirms.delete(id);
      resolve(ok);
    };
    const timer = setTimeout(() => finish(false), CONFIRM_TIMEOUT_MS);
    pendingConfirms.set(id, finish);
    send({ event: "confirm_request",
           data: { id, says: call.says, name: call.name, args: call.args, preview } });
  });
}

const CONFIRM_TIMEOUT_MS = 120000;

// A site's own helper functions, run in the page's world.
//
// MAIN world, not the extension's isolated one, and that is the entire reason this exists: the
// thing a site helper needs — the app's live credentials, its client, its router — is in the page's
// module scope, and an isolated content script is isolated from precisely that. The DOM is shared;
// the JavaScript state is not.
//
// The source is NOT bundled into the extension. It arrives with each tool request from the
// receiver, which reads it from `site-packs/<domain>.js`. So the code that runs is always the code
// whose manifest the model was shown, and editing a helper does not need an extension reload.
//
// What the model supplies is a function NAME and arguments — never source. The page-side entry
// point looks up the name in a fixed table and returns `no such site function` for anything else,
// which is what keeps this from being `eval` with extra steps.
async function runSiteCall(call, tab) {
  const a = call.args || {};
  const ns = call.ns || "__oracle_site";
  if (!call.code) return "no helper code was provided for this site";
  const [res] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "MAIN",
    args: [call.code, ns, a.fn || "", a.args || {}],
    func: async (code, key, fn, args) => {
      try {
        // Re-install when the SOURCE has changed, not merely when nothing is installed.
        //
        // The guard used to be `if (!window[key])`, which made the helper immortal: edit
        // site-packs/<domain>.js, call it again, and the page keeps running the copy it installed
        // the first time. The receiver dutifully ships the new source and nothing uses it. It cost
        // a debugging round — a fix was verified as "shipped", the live behaviour was identical,
        // and the obvious conclusion (the fix is wrong) was the wrong one.
        //
        // A cheap content hash makes the claim true: same source, keep it; different source,
        // replace it.
        let h = 0;
        for (let i = 0; i < code.length; i++) h = (h * 31 + code.charCodeAt(i)) | 0;
        if (window[key] && window[key].__srcHash !== h) delete window[key];
        if (!window[key]) {
          // Indirect eval so the helper evaluates at global scope. It is idempotent and guards on
          // its own namespace, so re-injection on every call is cheap and keeps a page that
          // navigated (losing the previous injection) working without special-casing it.
          (0, eval)(code);
        }
        if (!window[key]) return { error: "helper did not install" };
        window[key].__srcHash = h;
        return await window[key].call(fn, args);
      } catch (e) {
        const msg = String((e && e.message) || e);
        // The helper is evaluated under the PAGE's Content-Security-Policy, not the extension's.
        // The panel sends no CSP today, so this works; the day it sends one with a script-src that
        // omits 'unsafe-eval', this is the line that breaks, and a bare "EvalError" would send
        // whoever debugs it looking in the wrong repo entirely.
        if (/eval|unsafe-eval|Content Security Policy/i.test(msg)) {
          return { error: `the page's Content-Security-Policy blocked the site helper (${msg}). ` +
                          `The other tools still work — fall back to read_page/look_at_page.` };
        }
        return { error: msg };
      }
    },
  });
  const out = res?.result;
  if (out && out.error) return `site_call ${a.fn} failed: ${out.error}`;
  // Returned as JSON text, because that is the point of the whole exercise: the model reads exact
  // values rather than transcribing them off a rendering of the same values.
  const text = JSON.stringify(out, null, 1);
  const LIMIT = 60000;
  return text.length > LIMIT
    ? text.slice(0, LIMIT) + `\n…[truncated at ${LIMIT} chars of ${text.length}. Narrow the ` +
      `request — most list operations take a page size and a filter.]`
    : text;
}

async function runBrowserTool(call, tab, notify = () => {}) {
  const a = call.args || {};
  // An injected function that THROWS must not become a bare `null`.
  //
  // Observed: the model passed `button:contains("Resume"):first-of-type` — jQuery syntax, not CSS —
  // querySelector threw a SyntaxError inside the page, and the tool result began with the word
  // "null". The model was told nothing at all: not that the selector was invalid, not that its
  // click never happened. It tried the same shape again two steps later. A silent null is the worst
  // possible answer, strictly worse than "not found", which at least says something happened.
  const exec = async (func, args) => {
    let frames;
    try {
      frames = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func, args });
    } catch (e) {
      return `TOOL FAILED: the page rejected the injected step (${e && e.message}).`;
    }
    const r = frames && frames[0];
    if (r && r.error) return `TOOL FAILED in the page: ${r.error.message || r.error}`;
    if (!r || r.result === undefined || r.result === null) {
      return "TOOL FAILED: the step threw inside the page and produced no result. If you passed a " +
             "`selector`, it is most likely not valid CSS — `:contains(...)` is jQuery and does " +
             "NOT exist in CSS. To click by visible text use the `text` argument instead of a " +
             "selector; that is what it is for.";
    }
    return r.result;
  };
  try {
    if (call.name === "read_page") return String(await exec(toolReadPage, [a.selector || ""]));
    if (call.name === "click") {
      // Watch for a NEW TAB. A link with target=_blank succeeds, this tab does not change, and the
      // model concluded "the dashboard link appears to be broken" — a false statement caused
      // entirely by the harness seeing one tab. If a click opens another one, say so.
      const opened = [];
      const onCreated = (t) => opened.push(t);
      chrome.tabs.onCreated.addListener(onCreated);
      // Capture BEFORE, so the result can state whether anything actually changed. Handing back a
      // fresh dump of similar-looking page text is not an answer to "did that work?".
      const before = await exec(() => ({
        url: location.href,
        text: (document.body.innerText || "").replace(/\s+/g, " ").trim(),
      }));
      const out = String(await exec(toolClick, [a]));
      // Report the OUTCOME: a click that navigates or swaps a tab changes the page, and the model
      // needs the after, not the before.
      await new Promise((r) => setTimeout(r, 900));
      chrome.tabs.onCreated.removeListener(onCreated);
      if (opened.length) {
        const t = opened[opened.length - 1];
        return `${out}\nA NEW TAB OPENED: ${t.pendingUrl || t.url || "(url not yet known)"}\n` +
               `Your tools only act on the page you started from, so you cannot read or screenshot ` +
               `that tab. Do NOT conclude the link is broken — it worked. Say that it opened in a ` +
               `new tab and continue with what is visible here, or ask the user to switch to it.`;
      }
      const after = await exec(() => ({
        url: location.href, title: document.title,
        text: (document.body.innerText || "").replace(/\s+/g, " ").trim(),
      }));
      // Say plainly WHETHER it changed, and if so WHAT APPEARED.
      //
      // This one cost real damage rather than just a wasted turn. Clicking "Start" on the wizard
      // created a draft — it worked — but the AFTER dump looked like the same page, so the model
      // concluded it had failed and clicked again. And again. Each attempt created another draft;
      // the page ended up with three "Resume" buttons that had not existed before. A mutating
      // action that reports ambiguously is an action that gets repeated.
      const beforeText = (before && before.text) || "";
      const afterText = (after && after.text) || "";
      const navigated = before && after && before.url !== after.url;
      const changed = navigated || beforeText !== afterText;
      let delta = "";
      if (!navigated && changed) {
        // What is on the page now that was not before. Cheap word-level diff — enough to show a new
        // row, a new button, an error message.
        const was = new Set(beforeText.split(" "));
        const fresh = afterText.split(" ").filter((w) => w && !was.has(w));
        delta = fresh.length ? `\nNEW ON THE PAGE: ${fresh.slice(0, 60).join(" ")}` : "";
      }
      const verdict = navigated
        ? `THE PAGE CHANGED: navigated to ${after.url}`
        : changed
          ? "THE PAGE CHANGED (same URL — content updated). The click DID take effect."
          : "THE PAGE DID NOT CHANGE. The click landed but nothing visibly happened. Do NOT simply " +
            "click it again — if it is a button that creates or starts something, repeating it may " +
            "do the thing twice. Read the page or the form's state to find what it is waiting for.";
      return `${out}\n${verdict}${delta}\nAFTER: ${after?.url} — ${after?.title}\n` +
             `${afterText.slice(0, 1500)}`;
    }
    if (call.name === "type_text") return String(await exec(toolType, [a]));
    if (call.name === "site_call") return await runSiteCall(call, tab);
    if (call.name === "navigate") {
      // Same-origin only. A site's URL grammar is a tool for using THAT site; letting a page's own
      // reference material send the tab anywhere would make "context from the site" into "control
      // of the browser", which is not the trade the per-host permission was granted for.
      let target;
      try { target = new URL(a.url, tab.url); } catch (_) { return `not a URL: ${a.url}`; }
      const here = new URL(tab.url);
      if (target.origin !== here.origin) {
        return `REFUSED: ${target.origin} is a different site. navigate only works within ` +
               `${here.origin}; ask the user to open that themselves.`;
      }
      await chrome.tabs.update(tab.id, { url: target.href });
      await new Promise((r) => setTimeout(r, 1200));
      const after = await exec(() => ({
        url: location.href, title: document.title,
        text: (document.body.innerText || "").replace(/\s+/g, " ").trim().slice(0, 3000) }));
      return `navigated to ${after?.url} — ${after?.title}\n${after?.text || ""}`;
    }
    if (call.name === "wait") {
      const s = Math.max(1, Math.min(15, Number(a.seconds) || 3));
      await new Promise((r) => setTimeout(r, s * 1000));
      const after = await exec(() => ({ url: location.href, title: document.title }));
      return `waited ${s}s. The page is now ${after?.url} — ${after?.title}. ` +
             `Look again; if an embedded dashboard still reads as "Loading", it is in an iframe ` +
             `and read_page will never see it — use look_at_page.`;
    }
    if (call.name === "look_at_page") {
      const shot = await withOracleHidden(tab, async () => (
        a.full_page ? (await fullPageShot(tab)).b64
          : (await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" })).split(",")[1]));
      // Hand the picture back with the reading. When the model decides to look at something on its
      // own, the person supervising it should see the same thing it saw — otherwise the transcript
      // says "I looked" and shows nothing, which is a claim rather than evidence.
      // Show it NOW, not when the reading comes back. The vision leg is a GPU swap plus a read —
      // a minute or more — and emitting the picture at the end meant it appeared after the answer,
      // which is precisely when it is no longer useful for following along.
      call.image = await thumbnail(shot);
      if (call.image) notify(call.image);
      const r = await fetch(RECEIVER + "/vision", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image: shot, mime: "image/png", url: tab.url, title: tab.title,
                               source: a.full_page ? "fullpage" : "page", page_text: "",
                               prompt: a.question || "Describe what this shows, with the numbers." }),
      });
      if (!r.ok || !r.body) return `look_at_page failed: receiver error ${r.status}`;
      let text = "";
      await pumpSSE(r.body, (ev) => {
        if (ev.event === "delta") text += ev.data.text || "";
        if (ev.event === "error") text += `\n[error: ${ev.data.error}]`;
      });
      return text.trim() || "the vision model returned nothing";
    }
    return `unknown tool ${call.name}`;
  } catch (e) {
    return `${call.name} failed: ${e.message || e}`;
  }
}

async function chatHandleTools(calls, tab, send, depth) {
  if (depth > CHAT_MAX_HANDOFFS) {
    send({ event: "error", data: { error: "too many tool steps — stopping." } });
    return;
  }
  keepaliveStart();
  const results = [];
  try {
    for (const c of calls) {
      // Confirmed actions: show the user what is about to happen, on the page, and wait.
      if (c.confirm) {
        const [pv] = await chrome.scripting.executeScript({
          target: { tabId: tab.id }, func: toolPreview, args: [c.args || {}] });
        const preview = pv?.result || { found: false };
        const ok = await askConfirm(c, preview, send);
        await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: toolPreviewClear })
          .catch(() => {});
        if (!ok) {
          // Report the decision as a RESULT, not an error. The model is told elsewhere that a skip
          // is an answer; here it gets the fact in the same channel as every other outcome, so it
          // continues the conversation rather than treating it as a failed attempt to retry.
          const content = `NOT DONE — the user declined this action (${c.says}). This is their ` +
            `decision, not a failure. Do not retry it and do not try to achieve it another way. ` +
            `Carry on without it, or ask them what they would prefer.`;
          send({ event: "tool_done", data: { id: c.id, failed: false, detail: "declined" } });
          results.push({ id: c.id, name: c.name, content, image: "" });
          continue;
        }
      }
      send({ event: "status", data: { text: c.says + "…" } });
      const content = await runBrowserTool(c, tab, (image) =>
        send({ event: "tool_image", data: { id: c.id, image, says: c.says } }));
      // Report the OUTCOME of each step, not just that it was attempted. A tool that misses and a
      // model that quietly tries something else is the pair that produced "it recovered, without
      // telling me" — the prose described a plan it had already abandoned, and the only record of
      // what really happened was a step line that said the attempt was made.
      const failed = /^(NOT CLICKED|NOT TYPED|no element matches|error|unknown tool|look_at_page failed)/i
        .test(content.trim());
      send({ event: "tool_done", data: { id: c.id, failed,
                                         detail: failed ? content.split("\n")[0].slice(0, 160) : "" } });
      if (c.image) send({ event: "tool_image", data: { id: c.id, image: c.image, says: c.says } });
      results.push({ id: c.id, name: c.name, content, image: c.image || "" });
    }
  } finally {
    keepaliveStop();
  }
  let host = "";
  try { host = new URL(tab.url).host; } catch (_) {}
  try {
    const r = await fetch(RECEIVER + "/chat/tool", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host, results, url: tab.url, title: tab.title,
                             session: chatSession, debug: await debugOn() }),
    });
    if (!r.ok || !r.body) { send({ event: "error", data: { error: "receiver error " + r.status } }); return; }
    await chatPump(r.body, send, tab, depth + 1);
  } catch (_) {
    send({ event: "error", data: { error: "Oracle receiver offline." } });
  }
}

// Pump a chat stream, executing any tool request it makes and continuing the loop.
async function chatPump(body, send, tab, depth = 0) {
  let pending = null;
  let terminal = false;
  await pumpSSE(body, (ev) => {
    if (ev.event === "tool_request") { pending = ev.data.calls || []; send(ev); return; }
    if (ev.event === "done" || ev.event === "error") terminal = true;
    // a `done` that only means "over to you" must not end the panel's spinner
    if (ev.event === "done" && ev.data && ev.data.pending_tools) return;
    send(ev);
  });
  if (pending && pending.length) { await chatHandleTools(pending, tab, send, depth); return; }
  if (!terminal) {
    send({ event: "error", data: { error:
      "The answer was cut off — the connection to the Oracle receiver ended early." } });
  }
}

// Every stored conversation, so the panel can show them and let you throw them away. They live on
// the receiver, which is the only component that knows about the ones this browser never opened.
async function chatSessions(tab) {
  let host = "";
  try { host = new URL(tab.url).host; } catch (_) {}
  let sessions = [];
  try {
    // Scoped to this host: the panel lists the conversations for the site you are on. The global
    // list lives in the extension's settings page, where a global list belongs.
    const r = await fetch(`${RECEIVER}/chat/sessions?host=${encodeURIComponent(host)}`);
    if (r.ok) sessions = (await r.json()).sessions || [];
  } catch (_) {}
  chrome.tabs.sendMessage(tab.id, { type: "oracle:chatSessions", sessions, here: host })
    .catch(() => {});
}

async function chatDelete(tab, host, session) {
  try {
    await fetch(RECEIVER + "/chat/delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host, session: session || "main" }),
    });
  } catch (_) {}
  await chatSessions(tab);
  // Deleting the conversation you are looking at should empty the panel, not leave a ghost of it
  // on screen that the next question would silently contradict.
  let here = "";
  try { here = new URL(tab.url).host; } catch (_) {}
  if (host === here && (session || "main") === chatSession) chatLoad(tab);
}

// The per-host gate for acting tools. Stored on the RECEIVER, not in the extension, because the
// receiver is what decides which tools to offer the model — and a gate enforced only in the UI is
// not a gate.
async function chatAllow(tab, allow) {
  let host = "";
  try { host = new URL(tab.url).host; } catch (_) {}
  try {
    await fetch(RECEIVER + "/chat/allow", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host, allow }),   // "off" | "confirm" | "allow"
    });
  } catch (_) {}
}

// Continue a turn that was cut off. Nothing was lost — the transcript is on the receiver — so this
// is another model call over what is already recorded, not a re-ask.
async function chatResume(tab) {
  const send = (ev) => chrome.tabs
    .sendMessage(tab.id, { type: "oracle:chatEvent", ev, session: chatSession }).catch(() => {});
  let host = "";
  try { host = new URL(tab.url).host; } catch (_) {}
  keepaliveStart();
  try {
    const r = await fetch(RECEIVER + "/chat/resume", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host, session: chatSession, url: tab.url, title: tab.title,
                             agents_md: await agentsMd(tab.url), debug: await debugOn() }),
    });
    if (!r.ok || !r.body) { send({ event: "error", data: { error: "receiver error " + r.status } }); return; }
    await chatPump(r.body, send, tab);
  } catch (e) {
    send({ event: "error", data: { error: "Could not continue: " + (e.message || e) } });
  } finally {
    keepaliveStop();
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

// Default target is the CHAT now: a region you selected belongs in a conversation you can continue,
// not in a card that closes and takes the picture with it.
async function screenshotRegion(tab, target = "chat") {
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

// A full-page stitch is ~800 KB of base64. That is fine to POST to the receiver and NOT fine to
// push through chrome.tabs.sendMessage, which drops oversized messages — and the drop is silent, so
// the screenshot simply never appeared in the panel while everything else looked correct. It is
// also more than a transcript needs: this is for a human to check what the model looked at, not for
// the model, which only ever gets the reading.
async function thumbnail(b64, maxW = 1100) {
  try {
    const bmp = await createImageBitmap(await (await fetch("data:image/png;base64," + b64)).blob());
    const scale = Math.min(1, maxW / bmp.width);
    const w = Math.max(1, Math.round(bmp.width * scale));
    const h = Math.max(1, Math.round(bmp.height * scale));
    const c = new OffscreenCanvas(w, h);
    c.getContext("2d").drawImage(bmp, 0, 0, w, h);
    const out = await blobToB64(await c.convertToBlob({ type: "image/jpeg", quality: 0.72 }));
    return "data:image/jpeg;base64," + out;
  } catch (_) {
    return "";            // no thumbnail is better than a broken one; the reading still lands
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

// Take a picture of the PAGE, not of us. Every Oracle surface is tagged data-oracle-ui, so it can
// be hidden for the duration of a capture and put back afterwards. Without this the model reads a
// screenshot containing its own previous answer and treats it as part of the site — describing
// itself, from a stale copy, with no way to tell that is what it is doing.
async function withOracleHidden(tab, fn) {
  const set = (on) => chrome.scripting.executeScript({
    target: { tabId: tab.id }, args: [on],
    func: (hide) => {
      for (const el of document.querySelectorAll("[data-oracle-ui]")) {
        if (hide) {
          el.setAttribute("data-oracle-vis", el.style.visibility || "");
          el.style.visibility = "hidden";
        } else if (el.hasAttribute("data-oracle-vis")) {
          el.style.visibility = el.getAttribute("data-oracle-vis");
          el.removeAttribute("data-oracle-vis");
        }
      }
    },
  }).catch(() => {});
  await set(true);
  // A repaint has to land before the capture, or the panel is still in the pixels.
  await new Promise((r) => setTimeout(r, 90));
  try {
    return await fn();
  } finally {
    await set(false);
  }
}

async function fullPageShot(tab) {
  const run = (func, args) => chrome.scripting.executeScript({ target: { tabId: tab.id }, func, args })
    .then(([r]) => r?.result);

  // WHAT SCROLLS is not always the document. An app shell — a fixed layout with its content in an
  // inner div — reports scrollHeight === innerHeight, so "full page" captured exactly one screenful
  // and looked like a bug. Find the element that actually scrolls: the document if it does,
  // otherwise the largest scrollable box on screen.
  const m = await run(() => {
    const docH = Math.max(document.documentElement.scrollHeight,
                          document.body ? document.body.scrollHeight : 0);
    let scroller = null;
    if (docH <= window.innerHeight + 4) {
      let best = 0;
      for (const el of document.querySelectorAll("div,main,section,article")) {
        const r = el.getBoundingClientRect();
        if (r.width < window.innerWidth * 0.4 || r.height < window.innerHeight * 0.4) continue;
        const over = el.scrollHeight - el.clientHeight;
        if (over > 80 && over > best) { best = over; scroller = el; }
      }
    }
    if (scroller) {
      scroller.setAttribute("data-oracle-scroller", "1");
      return { y0: scroller.scrollTop, vh: scroller.clientHeight,
               dpr: window.devicePixelRatio || 1, h: scroller.scrollHeight,
               vw: window.innerWidth, inner: true };
    }
    return { y0: window.scrollY, vh: window.innerHeight, dpr: window.devicePixelRatio || 1,
             h: docH, vw: window.innerWidth, inner: false };
  });
  if (!m) throw new Error("could not measure the page");

  const total = Math.min(m.h, FULLPAGE_MAX_CSS_PX, m.vh * FULLPAGE_MAX_SLICES);
  const capped = total < m.h;
  const shots = [];
  try {
    for (let y = 0, n = 0; y < total && n < FULLPAGE_MAX_SLICES; y += m.vh, n++) {
      // hide sticky/fixed chrome from the second slice on — it is already in the first
      const at = await run((args) => {
        const inner = document.querySelector("[data-oracle-scroller]");
        if (inner) inner.scrollTop = args.y; else window.scrollTo(0, args.y);
        if (args.hide) {
          for (const el of document.querySelectorAll("body *")) {
            const p = getComputedStyle(el).position;
            if ((p === "fixed" || p === "sticky") && el.getClientRects().length) {
              el.setAttribute("data-oracle-hidden", el.style.visibility || "");
              el.style.visibility = "hidden";
            }
          }
        }
        return inner ? inner.scrollTop : window.scrollY;
      }, [{ y, hide: n > 0 }]);
      // give the page a beat to paint and to fire lazy-loading, and stay under the
      // captureVisibleTab rate limit
      await new Promise((r) => setTimeout(r, 260));
      shots.push({ y: at ?? y, data: await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" }) });
      if (at !== null && at + m.vh >= m.h - 1) break;      // hit the bottom
    }
  } finally {
    await run((y0) => {
      const inner = document.querySelector("[data-oracle-scroller]");
      if (inner) { inner.scrollTop = y0; inner.removeAttribute("data-oracle-scroller"); }
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
  await openChat(tab);
  try {
    const shot = await withOracleHidden(tab, () => fullPageShot(tab));
    if (await debugOn()) {
      chrome.tabs.sendMessage(tab.id, { type: "oracle:chatEvent", ev: { event: "debug", data: {
        side: "extension", stage: "full-page screenshot", slices: shot.slices, capped: shot.capped,
        page_css_height: shot.pageCssHeight, captured_css_height: shot.capturedCssHeight,
        png_kb: Math.round(shot.b64.length * 0.75 / 1024) } } }).catch(() => {});
    }
    chrome.tabs.sendMessage(tab.id, {
      type: "oracle:chatAsk", session: QUICK, image: shot.b64,
      thumb: await thumbnail(shot.b64), source: "fullpage",
      message: "Explain this page: what is it, what is it showing, and what should I notice?",
    }).catch(() => {});
  } catch (e) {
    chrome.tabs.sendMessage(tab.id, { type: "oracle:chatEvent", ev: { event: "error",
      data: { error: "Could not capture the page: " + (e.message || e) } } }).catch(() => {});
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

    await openChat(tab);
    // The image's own markup travels in the QUESTION, since the chat path carries a message rather
    // than the /vision payload's named fields. It is still the first thing the vision model reads.
    const described = [["alt", meta.alt], ["title", meta.title], ["caption", meta.caption]]
      .filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`).join("\n");
    chrome.tabs.sendMessage(tab.id, {
      type: "oracle:chatAsk", session: QUICK, image: b64, thumb: await thumbnail(b64),
      source: "image",
      message: "Explain this image from the page." +
        (described ? `\n\nThe page describes it as:\n${described}` : "") +
        (meta.near ? `\n\nText around it: ${meta.near.slice(0, 600)}` : ""),
    }).catch(() => {});
  } catch (e) {
    chrome.tabs.sendMessage(tab.id, { type: "oracle:chatEvent", ev: { event: "error",
      data: { error: "Image read failed: " + (e.message || e) } } }).catch(() => {});
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
