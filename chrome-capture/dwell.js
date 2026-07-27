// Oracle Capture — dwell sensor. Runs on every http(s) page (document_idle). If you actually spend
// time on a page (visible, not just an open tab), it sends the page once to the background, which
// folds it into the fading-slot memory at a FRACTION of a capture's weight (H17 tiered signals).
// The backend applies the denylist, so excluded sites never land even though we report them.
(() => {
  if (window.__oracleDwell) return;
  window.__oracleDwell = true;

  const TICK = 2000, THRESHOLD_MS = 20000;   // ~20s of *visible* time before it counts as "read"
  let visibleMs = 0, last = Date.now(), fired = false;

  const timer = setInterval(() => {
    const now = Date.now();
    if (document.visibilityState === "visible") visibleMs += now - last;
    last = now;
    if (fired || visibleMs < THRESHOLD_MS) return;
    fired = true;
    clearInterval(timer);
    const text = ((document.body && document.body.innerText) || "").replace(/\s+/g, " ").trim().slice(0, 1500);
    if (text.length < 200) return;           // too thin to be worth a topic
    try {
      chrome.runtime.sendMessage({ type: "dwell", url: location.href, title: document.title, text });
    } catch (_) { /* extension reloaded / context gone */ }
  }, TICK);
})();
