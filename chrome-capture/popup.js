const RECEIVER = "http://127.0.0.1:8788";
const PDF_KEY = "oracleCapturePdf";
const $ = (id) => document.getElementById(id);
const msg = (t) => { $("msg").textContent = t || ""; };

function dot(el, ok) { el.className = "dot " + (ok ? "ok" : "bad"); }

async function refreshStatus() {
  chrome.runtime.sendMessage({ type: "queueCount" }, (n) => { $("qn").textContent = n || 0; });
  try {
    const r = await fetch(RECEIVER + "/status");
    const s = await r.json();
    dot($("d-recv"), true);
    dot($("d-rag"), s.ragflow);
    dot($("d-syn"), s.synth);
  } catch (_) {
    dot($("d-recv"), false); dot($("d-rag"), false); dot($("d-syn"), false);
    msg("Receiver offline — start oracle-capture-receiver.py");
  }
}

$("capture").addEventListener("click", () => {
  msg("Capturing…");
  $("capture").disabled = true;
  chrome.runtime.sendMessage({ type: "capture" }, (res) => {
    $("capture").disabled = false;
    msg(res && res.ok ? "Captured ✓" : "Queued (receiver offline)");
    refreshStatus();
  });
});

$("drain").addEventListener("click", () => {
  msg("Retrying…");
  chrome.runtime.sendMessage({ type: "drain" }, (res) => {
    msg(res ? `Sent ${res.drained}, ${res.remaining} left` : "");
    refreshStatus();
  });
});

chrome.storage.local.get(PDF_KEY).then((v) => { $("pdf").checked = v[PDF_KEY] !== false; });
$("pdf").addEventListener("change", (e) => chrome.storage.local.set({ [PDF_KEY]: e.target.checked }));

refreshStatus();
