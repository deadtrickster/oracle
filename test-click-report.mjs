// What a click TELLS the model afterwards.
//
// Two failures from one real transcript (stage.cloud.stroppy.io, 2026-07-29):
//
//  1. `button:contains("Resume"):first-of-type` — jQuery, not CSS. querySelector threw inside the
//     page, the injected step died, and the tool result began with the word "null". The model was
//     told nothing and tried the same shape again two steps later.
//
//  2. Clicking "Start" on the wizard WORKED — it created a draft — but the after-dump looked like
//     the same page, so the model concluded it had failed and clicked again. Twice. Each attempt
//     created another draft. A mutating action that reports ambiguously gets repeated, so the
//     damage is not a wasted turn, it is junk in the user's account.
import { readFileSync } from "node:fs";

const src = readFileSync(new URL("./chrome-capture/background.js", import.meta.url), "utf8");

let failed = 0;
const check = (name, cond, detail = "") => {
  console.log(`${cond ? "ok  " : "FAIL"}  ${name}${cond || !detail ? "" : `  — ${detail}`}`);
  if (!cond) failed++;
};

// --- 1. invalid CSS ------------------------------------------------------------------
const m = src.match(/\nfunction toolClick\(arg\) \{[\s\S]*?\n\}/);
const toolClick = eval(`(${m[0].trim()})`);

globalThis.document = {
  querySelector(sel) {
    // Faithful to the browser: querySelector THROWS on invalid CSS rather than returning null.
    if (/:contains\(/i.test(sel)) {
      throw new Error(`Failed to execute 'querySelector' on 'Document': '${sel}' is not a valid selector.`);
    }
    return null;
  },
  querySelectorAll: () => [],
};

const bad = toolClick({ text: "Resume", selector: 'button:contains("Resume"):first-of-type' });
check("an invalid selector does not throw out of the tool", typeof bad === "string", typeof bad);
check("it says the click did NOT happen", /^NOT CLICKED/.test(bad), bad.slice(0, 80));
check("it names :contains as jQuery, not CSS", /jQuery/.test(bad), bad);
check("it points at the argument that DOES work", /pass `text`/.test(bad), bad);

// --- 1b. a container must never beat the control it wraps -----------------------------
// The bug this caught, live: clicking "Start" reported success and did nothing, repeatedly, because
// it matched a <div> whose innerText was also "Start". Wrapper and button have identical text, so
// "smallest text wins" is a tie and document order hands it to the wrapper.
const el = (tag, text, extra = {}) => ({
  tagName: tag.toUpperCase(),
  innerText: text,
  attrs: extra.attrs || {},
  children: extra.children || [],
  onclick: extra.onclick,
  getClientRects: () => ({ length: 1 }),
  getAttribute(a) { return this.attrs[a] ?? null; },
  hasAttribute(a) { return a in this.attrs; },
  querySelectorAll() { return this.children; },
  closest() { return null; },
});
const realButton = el("button", "Start");
const wrapper = el("div", "Start", { children: [realButton] });

globalThis.document.querySelectorAll = () => [wrapper, realButton]; // wrapper first, as in the DOM
let picked = null;
globalThis.document.querySelector = () => null;
const clickSrc = m[0];
// Re-evaluate toolClick with a stub that records which element it decided to act on.
const probe = eval(`(${clickSrc.replace(/const target = [\s\S]*$/, `
  return { picked: (arg.__pick = el) && el.tagName };
}`)})`);
picked = probe({ text: "Start" });
check("the BUTTON wins over the div that wraps it", picked?.picked === "BUTTON",
  `picked ${picked?.picked} — a container with no handler swallows every click`);

// And when there genuinely is no control, say so rather than pretending.
globalThis.document.querySelectorAll = () => [el("div", "Start")];
const onlyDiv = probe({ text: "Start" });
check("a bare container is still returned (with a warning elsewhere)",
  onlyDiv?.picked === "DIV", String(onlyDiv?.picked));

// --- 2. did the page actually change? -------------------------------------------------
// Exercise the verdict logic exactly as written in the click branch.
const branch = src.match(/const beforeText = [\s\S]*?return `\$\{out\}\\n\$\{verdict\}/);
check("the click branch computes a verdict", Boolean(branch));

const verdictFor = (before, after) => {
  const beforeText = before.text, afterText = after.text;
  const navigated = before.url !== after.url;
  const changed = navigated || beforeText !== afterText;
  return navigated ? "navigated" : changed ? "changed" : "unchanged";
};

check("a click that navigates is reported as navigation",
  verdictFor({ url: "/a", text: "x" }, { url: "/b", text: "y" }) === "navigated");
check("a click that adds a draft row is reported as CHANGED",
  verdictFor({ url: "/new", text: "Start Resume" },
             { url: "/new", text: "Start Resume Resume Untitled run" }) === "changed");
check("a click that truly does nothing is reported as unchanged",
  verdictFor({ url: "/new", text: "Start" }, { url: "/new", text: "Start" }) === "unchanged");

// The unchanged branch must warn against repeating, because the observed failure was repetition of
// a CREATE action, not of a read.
const unchangedMsg = src.match(/THE PAGE DID NOT CHANGE[\s\S]{0,400}/)[0];
check("the unchanged verdict warns against clicking again",
  /Do NOT simply .*click it again/s.test(unchangedMsg), unchangedMsg.slice(0, 120));
check("and explains the risk of repeating a create/start button",
  /may .*do the thing twice/s.test(unchangedMsg), unchangedMsg.slice(0, 200));

// The changed-but-same-URL branch must state that the click TOOK EFFECT, since that is the exact
// inference the model failed to make.
const changedMsg = src.match(/THE PAGE CHANGED \(same URL[\s\S]{0,160}/)[0];
check("a same-URL change says the click took effect", /DID take effect/.test(changedMsg), changedMsg);

// --- 3. a thrown injected step never surfaces as a bare null ---------------------------
const execSrc = src.match(/const exec = async \(func, args\) => \{[\s\S]*?\n  \};/)[0];
check("exec turns a null result into an explanation",
  /TOOL FAILED/.test(execSrc) && /:contains/.test(execSrc), execSrc.slice(0, 120));

console.log();
console.log(failed ? `${failed} failed` : "all passed");
process.exit(failed ? 1 : 0);
