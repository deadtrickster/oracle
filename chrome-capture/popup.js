import { pumpSSE } from "./sse.js";

const RECEIVER = "http://127.0.0.1:8788";
const PDF_KEY = "oracleCapturePdf";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// minimal, safe markdown for the ask answer
function md(t) {
  let s = esc(t);
  s = s.replace(/```(\w*)\n([\s\S]*?)```/g, (_m, _l, c) => "<pre><code>" + c + "</code></pre>");
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/\n{2,}/g, "</p><p>").replace(/\n/g, "<br>");
  return "<p>" + s + "</p>";
}

async function activeTab() {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  return t;
}

// ---------------------------------------------------------------- ask (streams into the popup)

async function ask() {
  const q = $("q").value.trim();
  if (!q) return;
  $("ask").disabled = true;
  const out = $("answer");
  let acc = "", sources = null, citations = null, errored = false;
  const paint = (streaming) => {
    // [n] markers become superscript links to a footnote list; each footnote links into the corpus
    // browser at the page the claim came from. linkify runs on the RENDERED html (md() has already
    // escaped the text), so the anchors it inserts are the only markup added.
    let body = md(acc);
    let notes = "";
    if (citations && citations.length) {
      const pl = OracleCite.plan(acc, citations);   // excerpt -> sequential display number
      body = OracleCite.linkify(body, pl, "ask");
      if (!streaming) notes = OracleCite.footnotes(pl, "ask");
    }
    // While streaming, a half-written answer has no stable footnote set, so show the plain source
    // line and swap in the numbered list once the answer is complete.
    const src = streaming && sources && sources.length
      ? `<div class="src">Grounded in: ${sources.map(esc).join(", ")}</div>` : "";
    out.innerHTML = body + (streaming ? '<span class="caret"></span>' : "") + src + notes;
  };
  out.innerHTML = '<p><span class="spin"></span> &nbsp;Consulting the corpus…</p>';
  try {
    const r = await fetch(RECEIVER + "/ask", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q }),
    });
    if (!r.ok || !r.body) { out.innerHTML = "<p>receiver error " + r.status + "</p>"; $("ask").disabled = false; return; }
    await pumpSSE(r.body, (ev) => {
      if (ev.event === "sources") { sources = ev.data.sources || []; citations = ev.data.citations || []; }
      else if (ev.event === "delta") { acc += ev.data.text || ""; paint(true); }
      else if (ev.event === "done") paint(false);
      else if (ev.event === "error") { errored = true; out.innerHTML = "<p>" + esc(ev.data.error) + "</p>"; }
    });
    // Only repaint if no error was shown: the final paint() would otherwise overwrite the error
    // with an empty answer, turning "the corpus backend is unreachable" into a blank box.
    if (!errored) paint(false);
  } catch (_) {
    out.innerHTML = "<p>Oracle receiver offline — start oracle-capture-receiver.py.</p>";
  }
  $("ask").disabled = false;
}

// ---------------------------------------------------------------- capture + ingest confirmation

async function pollJob(stem, tries = 12) {
  // Every terminal state must be REPORTED, not spun on: a duplicate name (already in the KB) and a
  // parse FAIL both used to sit on "queued for ingest…" and then claim "still running" — a capture
  // that never landed, shown as one that did.
  for (let i = 0; i < tries; i++) {
    try {
      const j = await (await fetch(RECEIVER + "/job?stem=" + encodeURIComponent(stem))).json();
      if (j.status === "failed") { $("cap-msg").textContent = "ingest failed: " + (j.error || "?"); return; }
      if (j.parse === "FAIL") { $("cap-msg").textContent = "parse FAILED in RAGFlow — not retrievable"; return; }
      if (j.status === "duplicate") {
        $("cap-msg").textContent = j.parse === "DONE"
          ? `already in the corpus (${j.chunks} chunks) — not re-ingested`
          : "already in the corpus — not re-ingested";
        return;
      }
      if (j.parse === "DONE") { $("cap-msg").textContent = `parsed ✓ (${j.chunks} chunks) — retrievable`; return; }
      if (j.status === "done") $("cap-msg").innerHTML = '<span class="spin"></span> parsing…';
      else $("cap-msg").innerHTML = '<span class="spin"></span> queued for ingest…';
    } catch (_) { /* keep trying */ }
    await new Promise((r) => setTimeout(r, 2500));
  }
  $("cap-msg").textContent = "captured — parse still running (check later)";
}

function capture() {
  $("capture").disabled = true;
  $("cap-msg").innerHTML = '<span class="spin"></span> capturing…';
  chrome.runtime.sendMessage({ type: "capture", note: $("note").value.trim() }, (res) => {
    $("capture").disabled = false;
    if (res && res.ok) { $("note").value = ""; if (res.stem) pollJob(res.stem); else $("cap-msg").textContent = "captured ✓"; }
    else $("cap-msg").textContent = "queued (receiver offline)";
    refreshStatus();
  });
}

// ---------------------------------------------------------------- topics + exclusions (H17 controls)

async function loadTopics() {
  let s;
  try { s = await (await fetch(RECEIVER + "/slots")).json(); }
  catch (_) { $("topics").innerHTML = '<span class="empty">receiver offline</span>'; return; }
  const p = s.params || {};
  $("topic-params").textContent = `· K=${p.K} τ=${p.tau_hours}h`;
  const max = Math.max(1, ...s.slots.map((t) => t.weight));
  $("topics").innerHTML = s.slots.length
    ? s.slots.map((t) => `
      <div class="topic">
        <span class="lbl" title="${esc(t.label)} (weight ${t.weight}, ${t.hits} hits)">${esc(t.label)}</span>
        <span class="bar" style="width:${Math.round((t.weight / max) * 60)}px"></span>
        <button class="ghost pin" data-l="${esc(t.label)}" title="pin (freeze decay)">${t.pinned ? "📌" : "📍"}</button>
        <button class="ghost forget" data-l="${esc(t.label)}" title="forget">×</button>
      </div>`).join("")
    : '<span class="empty">none yet — browse, capture, or explain something</span>';
  $("exclusions").innerHTML = (s.exclusions || []).map((v) => `
      <div class="excl"><span class="v">${esc(v)}</span>
        <button class="ghost unexcl" data-v="${esc(v)}">×</button></div>`).join("");

  $("topics").querySelectorAll(".forget").forEach((b) =>
    b.onclick = () => post("/forget", { label: b.dataset.l }).then(loadTopics));
  $("topics").querySelectorAll(".pin").forEach((b) =>
    b.onclick = () => post("/pin", { label: b.dataset.l, pinned: b.textContent === "📍" }).then(loadTopics));
  $("exclusions").querySelectorAll(".unexcl").forEach((b) =>
    b.onclick = () => post("/exclude", { action: "remove", value: b.dataset.v }).then(loadTopics));
}

function post(path, body) {
  return fetch(RECEIVER + path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  }).catch(() => {});
}

async function excludeSite() {
  const t = await activeTab();
  try {
    const host = new URL(t.url).hostname;
    await post("/exclude", { action: "add", value: host });
    loadTopics();
  } catch (_) {}
}

// ---------------------------------------------------------------- status

async function refreshStatus() {
  chrome.runtime.sendMessage({ type: "queueCount" }, (n) => { $("qn").textContent = n || 0; });
  try {
    const s = await (await fetch(RECEIVER + "/status")).json();
    $("d-recv").className = "dot ok";
    $("d-rag").className = "dot " + (s.ragflow ? "ok" : "bad");
    $("d-syn").className = "dot " + (s.synth ? "ok" : "bad");
    // Vision is deliberately off (VRAM is exclusive), so it stays grey-with-a-reason rather than
    // red: "off on purpose" and "broken" must not look the same.
    $("d-vl").className = "dot" + (s.vision ? " ok" : "");
    $("d-vl").title = s.vision ? "vision (qwen3-vl)" : (s.vision_note || "vision disabled");
  } catch (_) {
    ["d-recv", "d-rag", "d-syn", "d-vl"].forEach((id) => $(id).className = "dot bad");
  }
}

// ---------------------------------------------------------------- wire up

$("ask").addEventListener("click", ask);
$("q").addEventListener("keydown", (e) => { if (e.key === "Enter") ask(); });
$("capture").addEventListener("click", capture);
$("batch").addEventListener("click", () => {
  $("cap-msg").innerHTML = '<span class="spin"></span> capturing all tabs…';
  chrome.runtime.sendMessage({ type: "batchCapture" }, (r) => {
    $("cap-msg").textContent = r ? `${r.ok} captured, ${r.skipped} skipped` : "done";
    refreshStatus();
  });
});
$("drain").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "drain" }, (r) => {
    if (r) $("cap-msg").textContent = `sent ${r.drained}, ${r.remaining} left`;
    refreshStatus();
  });
});
$("shot").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "screenshotRegion" });
  window.close();   // get the popup out of the way so you can drag on the page
});
$("exclude-site").addEventListener("click", excludeSite);
$("refresh-topics").addEventListener("click", loadTopics);

chrome.storage.local.get(PDF_KEY).then((v) => { $("pdf").checked = v[PDF_KEY] !== false; });
$("pdf").addEventListener("change", (e) => chrome.storage.local.set({ [PDF_KEY]: e.target.checked }));

// One definition of the citation styling, shared with the overlay (see cite.js).
document.head.appendChild(Object.assign(document.createElement("style"), { textContent: OracleCite.CSS }));

refreshStatus();
loadTopics();
