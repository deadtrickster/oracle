// Injected functions must be SELF-CONTAINED.
//
// `chrome.scripting.executeScript({func})` serialises the function and evaluates it in the page.
// Anything it closes over in background.js — a const, a helper, another tool function — simply does
// not exist there, and the failure is a runtime ReferenceError inside the page, invisible to
// `node --check` and to every other check in this repo.
//
// Hit twice in one day: once splitting toolReadPage into a helper it then could not call, once
// hoisting a size constant to module scope "for tidiness". Both looked correct in the editor.
import { readFileSync } from "node:fs";

const src = readFileSync(new URL("./background.js", import.meta.url), "utf8");

// Every function actually used as an injected body: `func: toolX` or `func: (…) => …`.
// Three ways this file injects: `func: toolX`, the local `exec(toolX, …)` wrapper, and any
// function named tool* (the convention for injected bodies here). Catching only `func:` missed
// toolReadPage/toolClick/toolType — i.e. every one that had actually broken.
const injectedNames = [
  ...[...src.matchAll(/func:\s*([A-Za-z_$][\w$]*)/g)].map((m) => m[1]),
  ...[...src.matchAll(/\bexec\(\s*([A-Za-z_$][\w$]*)/g)].map((m) => m[1]),
  ...[...src.matchAll(/^(?:async )?function (tool[A-Za-z0-9_$]*)/gm)].map((m) => m[1]),
];

// Module-scope const/let/function names — the things an injected body must NOT reference.
const moduleScope = new Set(
  [...src.matchAll(/^(?:const|let|var|async function|function)\s+([A-Za-z_$][\w$]*)/gm)]
    .map((m) => m[1]),
);

let failed = 0;
for (const name of new Set(injectedNames)) {
  const re = new RegExp(`\\n(?:async )?function ${name}\\s*\\([^)]*\\)\\s*\\{`);
  const at = src.search(re);
  if (at < 0) continue; // an inline arrow, checked by hand at its call site
  // Walk braces to find the function body.
  let i = src.indexOf("{", at), depth = 0, end = i;
  for (; end < src.length; end++) {
    if (src[end] === "{") depth++;
    else if (src[end] === "}" && --depth === 0) break;
  }
  // Strip comments and string literals before looking for identifiers. Without this the check reads
  // prose: a comment containing the words "ground truth" was reported as referencing a module-scope
  // `ground`. A checker that cries wolf about English is one people start ignoring, which costs
  // more than the bug it was written to catch.
  const body = src.slice(i, end + 1)
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/\/\/[^\n]*/g, " ")
    .replace(/`(?:\\.|[^`\\])*`/g, '""')
    .replace(/"(?:\\.|[^"\\])*"/g, '""')
    .replace(/'(?:\\.|[^'\\])*'/g, '""');

  const leaks = [...moduleScope].filter((id) => {
    if (id === name) return false;
    if (!new RegExp(`\\b${id}\\b`).test(body)) return false;
    // Declared locally inside the body? Then it is fine.
    return !new RegExp(`(?:const|let|var|function)\\s+${id}\\b`).test(body);
  });

  if (leaks.length) {
    console.log(`FAIL  ${name}() references module scope: ${leaks.join(", ")}`);
    console.log(`      It is injected into the page, where those do not exist. Inline them.`);
    failed++;
  } else {
    console.log(`ok    ${name}() is self-contained`);
  }
}

console.log();
console.log(failed ? `${failed} injected function(s) would ReferenceError in the page` : "all injected functions are self-contained");
process.exit(failed ? 1 : 0);
