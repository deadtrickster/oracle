// Does type_text actually type into a React-controlled input?
//
// This reproduces the exact failure that cost a run on the new-run wizard: the tool reported
//   typed into input[...]; its value is now "orioledb tpcc baseline"
// the model clicked Start, and nothing happened — because React never saw an onChange, so its state
// still held the empty name and the form submitted the state, not the DOM.
//
// The mechanism, modelled faithfully below: React defines its own `value` accessor on the node and
// keeps a `_valueTracker`. Assigning through that accessor updates the tracker too, so when the
// input event arrives React compares tracker-against-value, sees no change, and skips onChange.
// Writing through the NATIVE prototype setter leaves the tracker stale, which is what makes the
// change look real.
import { readFileSync } from "node:fs";

const src = readFileSync(new URL("./chrome-capture/background.js", import.meta.url), "utf8");
const m = src.match(/async function toolType\(arg\) \{[\s\S]*?\n\}/);
if (!m) { console.log("FAIL: toolType not found"); process.exit(1); }

let failed = 0;
const check = (name, cond, detail = "") => {
  console.log(`${cond ? "ok  " : "FAIL"}  ${name}${cond || !detail ? "" : `  — ${detail}`}`);
  if (!cond) failed++;
};

// --- a React-ish controlled input --------------------------------------------------------------
class FakeInput {
  constructor(state) {
    this._v = "";
    this._tracker = "";
    this.disabled = false;
    this.state = state;              // stands in for React component state
    this.rects = 1;
    // React's instance-level accessor: assignment updates the tracker, which is what suppresses
    // the change detection later.
    Object.defineProperty(this, "value", {
      configurable: true,
      get: () => this._v,
      set: (v) => { this._v = String(v); this._tracker = String(v); },
    });
  }
  focus() {}
  getClientRects() { return { length: this.rects }; }
  getAttribute() { return null; }
  closest() { return null; }
  dispatchEvent(e) {
    if (e.type !== "input") return true;
    // React: only fire onChange when the DOM value differs from what we last tracked.
    if (this._v !== this._tracker) {
      this._tracker = this._v;
      this.state.name = this._v;     // <- the thing the form actually submits
    }
    return true;
  }
}

const NATIVE_PROTO = {};
Object.defineProperty(NATIVE_PROTO, "value", {
  configurable: true,
  get() { return this._v; },
  set(v) { this._v = String(v); },   // native setter: does NOT touch the tracker
});

const state = { name: "" };
const input = new FakeInput(state);
Object.setPrototypeOf(FakeInput.prototype, NATIVE_PROTO);

globalThis.HTMLInputElement = { prototype: NATIVE_PROTO };
globalThis.HTMLTextAreaElement = function () {};
globalThis.HTMLTextAreaElement.prototype = {};
globalThis.Event = class { constructor(t, o) { this.type = t; Object.assign(this, o || {}); } };
globalThis.document = {
  querySelector: (s) => (s === "input[name=run]" ? input : null),
  querySelectorAll: () => [],
};
globalThis.setTimeout = (fn) => fn();

const toolType = eval(`(${m[0]})`);

const res = await toolType({ selector: "input[name=run]", text: "orioledb tpcc baseline" });

check("the DOM field holds the text", input._v === "orioledb tpcc baseline", input._v);
check("REACT STATE received it — this is what the form submits",
  state.name === "orioledb tpcc baseline",
  `state.name = ${JSON.stringify(state.name)} — onChange never fired, so Start would do nothing`);
check("the tool reports success", /the page accepted it/.test(res), res);

// --- a field the page controls and reverts ------------------------------------------------------
const stubborn = new FakeInput({ name: "" });
Object.defineProperty(stubborn, "value", {
  configurable: true,
  get: () => "",                     // always reads back empty, as a rejecting field would
  set: () => {},
});
globalThis.document.querySelector = () => stubborn;
const res2 = await toolType({ selector: "input[name=run]", text: "hello" });
check("a field that refuses the text is reported as NOT TYPED", res2.startsWith("NOT TYPED"), res2);
check("and the model is told not to retry it the same way", /do NOT retry/i.test(res2), res2);

// --- a disabled field ---------------------------------------------------------------------------
const off = new FakeInput({ name: "" });
off.disabled = true;
globalThis.document.querySelector = () => off;
const res3 = await toolType({ selector: "input[name=run]", text: "x" });
check("a disabled field says so instead of silently succeeding",
  res3.startsWith("NOT TYPED") && /disabled/.test(res3), res3);

console.log();
console.log(failed ? `${failed} failed` : "all passed");
process.exit(failed ? 1 : 0);
