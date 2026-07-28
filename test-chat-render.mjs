// Drive chat.js's message handler with a realistic event sequence and inspect what it renders.
//
// The panel is where several bugs have now lived — duplicated replies, steps rendered out of order,
// a fold that hid the evidence — and none of them are visible to `node --check` or to a Python test
// of the receiver. This loads the real file against a stub DOM, feeds it the exact events the
// background worker sends, and asserts on the HTML that comes out.
//
//   node test-chat-render.mjs
import { readFileSync } from "node:fs";

const src = readFileSync(new URL("./chrome-capture/chat.js", import.meta.url), "utf8");

// --- a DOM stub that is just real enough -------------------------------------------------------
function makeEl(tag = "div") {
  const el = {
    tagName: tag.toUpperCase(), children: [], _html: "", style: {}, dataset: {}, hidden: false,
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
                 toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); },
                 contains(c) { return this._s.has(c); } },
    setAttribute() {}, getAttribute: () => "", removeAttribute() {}, hasAttribute: () => false,
    addEventListener() {}, removeEventListener() {}, appendChild(c) { el.children.push(c); return c; },
    append() {}, remove() {}, focus() {}, click() {}, scrollIntoView() {},
    getBoundingClientRect: () => ({ left: 100, top: 100, width: 400, height: 520 }),
    querySelector: () => makeEl(), querySelectorAll: () => [],
    get innerHTML() { return el._html; },
    set innerHTML(v) { el._html = v; },
    textContent: "", value: "", scrollTop: 0, scrollHeight: 0,
  };
  return el;
}

const scroll = makeEl();          // the .scroll pane, whose innerHTML is the conversation
const root = makeEl();
root.querySelector = (sel) => (sel === ".scroll" ? scroll : makeEl());
root.querySelectorAll = () => [];
const hostEl = makeEl();
hostEl.attachShadow = () => root;

let onMessage = null;
const listeners = [];
const ctx = {
  window: { __oracleChat: null, addEventListener() {}, removeEventListener() {},
            innerWidth: 1400, innerHeight: 900 },
  document: { createElement: () => hostEl, documentElement: hostEl, body: makeEl(),
              querySelector: () => makeEl(), querySelectorAll: () => [], addEventListener() {} },
  chrome: {
    runtime: {
      onMessage: { addListener(fn) { listeners.push(fn); onMessage = fn; } },
      sendMessage() {},
    },
    storage: { local: { get: () => Promise.resolve({}), set() {} } },
  },
  OracleCite: { CSS: "", plan: () => ({}), linkify: (h) => h, footnotes: () => "",
                used: () => [], esc: (s) => s },
  setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
  Date, Math, JSON, console, CSS: { escape: (s) => s },
};
ctx.globalThis = ctx;
new Function(...Object.keys(ctx), src)(...Object.values(ctx));

const fails = [];
const check = (name, cond, detail = "") => {
  console.log((cond ? "  ok   " : "  FAIL ") + name + (!cond && detail ? `  <- ${detail}` : ""));
  if (!cond) fails.push(name);
};
const ev = (event, data, session = "main") =>
  onMessage({ type: "oracle:chatEvent", ev: { event, data }, session });
const html = () => scroll.innerHTML;
const count = (s, needle) => s.split(needle).length - 1;

console.log(`\nlisteners registered: ${listeners.length}`);
check("exactly one message listener", listeners.length === 1, String(listeners.length));

console.log("\n1. a single-step reply renders once");
onMessage({ type: "oracle:chatHistory", host: "x.example", turns: [], session: "main" });
ev("delta", { text: "Hello there." });
ev("done", { epoch: 1 });
check("the text appears exactly once", count(html(), "Hello there.") === 1,
      `${count(html(), "Hello there.")}×`);

console.log("\n1b. and it renders once WHILE STREAMING, not only after it finishes");
onMessage({ type: "oracle:chatHistory", host: "x.example", turns: [], session: "main" });
ev("delta", { text: "Mid-flight text." });
check("no double while the answer is still arriving", count(html(), "Mid-flight text.") === 1,
      `${count(html(), "Mid-flight text.")}× — the live item and the streaming bubble both drew it`);
ev("delta", { text: " More." });
check("still once after another delta", count(html(), "Mid-flight text.") === 1,
      `${count(html(), "Mid-flight text.")}×`);
ev("done", { epoch: 1 });

console.log("\n2. a TOOL-USING reply renders each part once, in order");
onMessage({ type: "oracle:chatHistory", host: "x.example", turns: [], session: "main" });
ev("delta", { text: "I'll read the page." });
ev("tool_request", { calls: [{ id: "c1", name: "read_page", says: "read the page" }] });
ev("tool_done", { id: "c1", failed: false });
ev("delta", { text: "I can see 5 emails." });
ev("done", { epoch: 1 });
const h2 = html();
check("the first line appears once", count(h2, "I'll read the page.") === 1, `${count(h2, "I'll read the page.")}×`);
check("the step appears once", count(h2, "read the page") >= 1);
check("the answer appears once", count(h2, "I can see 5 emails.") === 1, `${count(h2, "I can see 5 emails.")}×`);
check("order is prose, step, answer",
      h2.indexOf("I'll read the page.") < h2.indexOf("I can see 5 emails."));

console.log("\n3. a reply that arrives AFTER a history reload is not doubled");
onMessage({ type: "oracle:chatHistory", host: "x.example", turns: [], session: "main" });
ev("delta", { text: "Partial answer" });
// mid-turn reload — what a session switch does
onMessage({ type: "oracle:chatHistory", host: "x.example", session: "main",
            turns: [{ role: "assistant", content: "Partial answer" }] });
ev("delta", { text: " continued." });
ev("done", { epoch: 1 });
check("the reloaded text is not duplicated", count(html(), "Partial answer") === 1,
      `${count(html(), "Partial answer")}×`);

console.log("\n4. two model calls in one turn (browser tool in between)");
onMessage({ type: "oracle:chatHistory", host: "x.example", turns: [], session: "main" });
ev("delta", { text: "Step one text." });
ev("tool_request", { calls: [{ id: "t1", name: "click", says: 'clicked "METRICS"', acting: true }] });
ev("tool_done", { id: "t1", failed: false });
ev("delta", { text: "Step two text." });
ev("done", { epoch: 2 });
const h4 = html();
check("first call's text once", count(h4, "Step one text.") === 1, `${count(h4, "Step one text.")}×`);
check("second call's text once", count(h4, "Step two text.") === 1, `${count(h4, "Step two text.")}×`);

console.log();
if (fails.length) {
  console.log(`FAILED: ${fails.length} -> ${fails.join(", ")}`);
  process.exit(1);
}
console.log("all chat render tests passed");
