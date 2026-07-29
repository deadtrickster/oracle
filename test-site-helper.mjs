// Run the stroppy site helper against a FAKE panel, in node.
//
// The helper's risk is not syntax — `node --check` covers that. It is control flow across four
// hops: observe a bearer, call an RPC to mint a scope token, set a cookie, then query a different
// endpoint that only works because of that cookie. Every one of those is invisible until it runs,
// and the previous way to find out was to reload the extension and ask a real model on a real run.
//
// So: stub fetch/location/document, assert the sequence and the shape.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const dir = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(dir, "site-packs/cloud.stroppy.io.js"), "utf8");

let failed = 0;
const check = (name, cond, detail = "") => {
  console.log(`${cond ? "ok  " : "FAIL"}  ${name}${cond || !detail ? "" : `  — ${detail}`}`);
  if (!cond) failed++;
};

// ---------------------------------------------------------------- fake page
const calls = [];
const RUN = "11111111-2222-3333-4444-555555555555";

// `window` must BE the global object, as it is in a page. Making it a separate `{}` gives the
// helper a `window.fetch` that nothing else calls, so its wrapper sits outside the request path and
// silently observes nothing — which is a property of the fake, not of the helper.
globalThis.window = globalThis;
globalThis.document = { cookie: "" };
globalThis.location = {
  origin: "https://cloud.stroppy.io",
  href: `https://cloud.stroppy.io/t/default/runs/${RUN}?view=grafana`,
  pathname: `/t/default/runs/${RUN}`,
  search: "?view=grafana",
};
globalThis.setTimeout = (fn) => fn();
globalThis.Headers = class {
  constructor(o) { this.o = o || {}; }
  get(k) { return this.o[k] ?? this.o[k.toLowerCase()] ?? null; }
};
globalThis.Request = class {};
globalThis.URLSearchParams = URLSearchParams;

const json = (body) => ({
  ok: true, status: 200, text: async () => JSON.stringify(body),
});

globalThis.fetch = async (url, init) => {
  calls.push({ url: String(url), init });
  const u = String(url);
  if (u.endsWith("/GetTenant")) return json({ tenant: { id: "tenant-abc" } });
  if (u.endsWith("/GrafanaSession")) return json({ scopeToken: "SCOPE-TOKEN-XYZ" });
  if (u.includes("/label/__name__/values")) {
    return json({ data: ["stroppy_tx_total", "node_cpu_seconds_total", "pg_wal_bytes"] });
  }
  if (u.includes("/query_range")) {
    // 300 points, a clean ramp with a cliff at 60% — enough to prove the downsampler keeps SHAPE.
    const values = Array.from({ length: 300 }, (_, i) => [1700000000 + i * 15,
      String(i < 180 ? 1000 + i : 5)]);
    return json({ data: { result: [{ metric: { __name__: "stroppy_tx_total" }, values }] } });
  }
  if (u.endsWith("/ListTestRuns")) return json({ runs: [{ id: RUN }] });
  return { ok: false, status: 404, text: async () => JSON.stringify({ code: "not_found" }) };
};

// ---------------------------------------------------------------- install
new Function(src)();
const site = globalThis.window.__oracle_stroppy;
check("helper installs under its namespace", Boolean(site));

const run = (fn, args) => site.call(fn, args);

// ---------------------------------------------------------------- tests
const noToken = await run("api", { procedure: "/x", message: {} });
check("api refuses clearly before any bearer is seen",
  Boolean(noToken.error) && /token/i.test(noToken.error), JSON.stringify(noToken));

// The panel makes a request; the helper is supposed to LEARN the bearer from it, passively.
await globalThis.fetch("https://cloud.stroppy.io/cloud.v1.api.IamService/GetMyAccount", {
  headers: { Authorization: "Bearer THE-REAL-TOKEN" },
});

const where = await run("where_am_i", {});
check("where_am_i parses tenant and run from the URL",
  where.tenant === "default" && where.runId === RUN, JSON.stringify(where));
check("where_am_i reports the token is now known", where.tokenObserved === true);

calls.length = 0;
const names = await run("metric_names", { match: "cpu" });
check("metric_names filters to matching series",
  names.names?.length === 1 && names.names[0] === "node_cpu_seconds_total", JSON.stringify(names));

const seq = calls.map((c) => c.url.replace("https://cloud.stroppy.io", ""));
check("it resolves the tenant, mints a scope, THEN queries",
  seq[0].endsWith("/GetTenant") && seq[1].endsWith("/GrafanaSession") &&
  seq[2].includes("/public/metrics/"), JSON.stringify(seq));
check("the scope cookie was actually set",
  globalThis.document.cookie.includes("stroppy_share=SCOPE-TOKEN-XYZ"),
  globalThis.document.cookie);

const authed = calls.find((c) => c.url.endsWith("/GrafanaSession"));
check("RPCs carry the observed bearer",
  authed?.init?.headers?.Authorization === "Bearer THE-REAL-TOKEN");
check("RPCs speak the Connect protocol",
  authed?.init?.headers?.["Connect-Protocol-Version"] === "1");

calls.length = 0;
const q = await run("promql", { query: "rate(stroppy_tx_total[1m])" });
const s = q.series?.[0];
check("promql returns a series with statistics", Boolean(s) && s.points === 300,
  JSON.stringify(q).slice(0, 200));
check("statistics are computed over the WHOLE series, not the sample",
  s.max === 1179 && s.min === 5, `min=${s?.min} max=${s?.max}`);
check("the sample is small enough for a prompt", s.sampled.length <= 60, String(s.sampled?.length));
// The cliff is at 60% of the run. If downsampling preserved shape it is still at ~60% of the array.
const cliff = s.sampled.findIndex(([, v]) => v < 100) / s.sampled.length;
check("downsampling preserves WHEN it fell over", cliff > 0.5 && cliff < 0.7, `at ${cliff}`);

check("the scope is reused, not re-minted per query",
  !calls.some((c) => c.url.endsWith("/GrafanaSession")), JSON.stringify(calls.map((c) => c.url)));

// --- bundled questions: one call, several endpoints, failures reported per part -----
calls.length = 0;
const report = await run("run_report", {});
check("run_report gathers the whole run in one call",
  report.metrics && report.run && report.quota && report.stages !== undefined,
  JSON.stringify(Object.keys(report)));
// It must SHAPE the response, not forward it. The raw four-call bundle was 60k+ chars on a real
// run and came back truncated mid-JSON — the exact failure this layer exists to prevent.
check("and returns a shaped summary, not raw API dumps",
  JSON.stringify(report).length < 20000, `${JSON.stringify(report).length} chars`);
const hit = calls.map((c) => c.url.split("/").pop());
check("it asks for overview, metrics, run and quota",
  ["GetTestRunOverview", "GetRunMetrics", "GetTestRun", "GetRunQuotaUsage"]
    .every((p) => hit.includes(p)), JSON.stringify(hit));
check("it resolves the tenant once and reuses it",
  hit.filter((p) => p === "GetTenant").length === 0, "tenant should already be cached");

// One endpoint failing must not lose the three that worked — the common real case is a permission
// the user lacks on ONE of these, and losing the whole report to it would be a bad trade.
const realFetch = globalThis.fetch;
globalThis.fetch = async (url, init) => {
  if (String(url).endsWith("/GetRunQuotaUsage")) {
    return { ok: false, status: 403, text: async () => JSON.stringify({ code: "permission_denied" }) };
  }
  return realFetch(url, init);
};
const partial = await run("run_report", {});
check("a failing part does not sink the others",
  Boolean(partial.metrics && partial.run), JSON.stringify(Object.keys(partial)));
check("and the failing part says so", Boolean(partial.quota?.error),
  JSON.stringify(partial.quota));
globalThis.fetch = realFetch;

calls.length = 0;
const design = await run("design_context", {});
check("design_context gathers what a run is configured FROM",
  design.workloads && design.databasePresets && design.quotas && design.stroppyVersions,
  JSON.stringify(Object.keys(design)));
const dhit = calls.map((c) => c.url.split("/").pop());
check("it asks the stroppy BINARY what it can run", dhit.includes("ProbeCatalog"),
  JSON.stringify(dhit));

// --- goto: must move the app WITHOUT reloading it ---------------------------------
// A stub that behaves like react-router's BrowserRouter: it listens for popstate and re-renders
// from window.location. If goto only set the URL, `routed` would stay false — which is exactly the
// failure that looks like success, because the address bar would be right and the view stale.
let routed = false;
globalThis.PopStateEvent = class { constructor(t, o) { this.type = t; Object.assign(this, o); } };
const routerListeners = [];
globalThis.addEventListener = (t, fn) => { if (t === "popstate") routerListeners.push(fn); };
globalThis.dispatchEvent = (e) => { routerListeners.forEach((fn) => fn(e)); return true; };
globalThis.addEventListener("popstate", () => { routed = true; });
globalThis.history = {
  pushState: (_s, _t, url) => {
    const u = new URL(url, "https://cloud.stroppy.io");
    globalThis.location.pathname = u.pathname;
    globalThis.location.search = u.search;
    globalThis.location.href = "https://cloud.stroppy.io" + u.pathname + u.search;
  },
};
globalThis.document.querySelector = () => ({ innerText: "Runs" });

let reloaded = false;
globalThis.location.assign = () => { reloaded = true; };
globalThis.location.reload = () => { reloaded = true; };

const nav = await run("goto", { path: "/t/default/runs?db=orioledb", wait: 100 });
check("goto reports the new location", nav.url?.includes("/t/default/runs?db=orioledb"),
  JSON.stringify(nav));
check("goto drives the app's router", routed === true, "popstate never reached the router");
check("goto does NOT reload the document", reloaded === false, "it triggered a full page load");
check("goto reports whether the view actually changed", nav.changed === true, JSON.stringify(nav));

const off = await run("goto", { path: "https://evil.example/x" });
check("goto refuses to leave the site", Boolean(off.error), JSON.stringify(off));
const rel = await run("goto", { path: "runs" });
check("goto refuses a path that is not absolute", Boolean(rel.error), JSON.stringify(rel));

// --- form_state: the FORM, not the site furniture ---------------------------------
// Reproduces the wizard from the failed transcript: a nav sidebar full of links, and a form whose
// database choice is a <button> card carrying aria-pressed rather than a <select>.
const mk = (tag, props = {}) => ({
  tagName: tag.toUpperCase(),
  attrs: props.attrs || {},
  innerText: props.text || "",
  value: props.value,
  disabled: props.disabled || false,
  options: props.options,
  _chrome: props.chrome || false,
  getClientRects: () => ({ length: 1 }),
  getAttribute(a) { return this.attrs[a] ?? null; },
  closest(sel) {
    if (sel.includes("nav") && this._chrome) return { tag: "nav" };
    return null;
  },
});
const navLinks = ["stroppy-cloud", "default", "Test Runs", "admin", "Dashboard", "Quotas"]
  .map((t) => mk("a", { text: t, chrome: true, attrs: { href: "/x" } }));
const nameField = mk("input", { attrs: { placeholder: "e.g. pg16 tpcc baseline", type: "text" }, value: "" });
const dbCards = [
  mk("button", { text: "OrioleDB single", attrs: { "aria-pressed": "true" } }),
  mk("button", { text: "OrioleDB HA", attrs: { "aria-pressed": "false" } }),
  mk("button", { text: "Postgres 17", attrs: { "aria-pressed": "false" } }),
];
const main = {
  querySelectorAll: (sel) => {
    if (sel.includes("input")) return [nameField];
    if (sel.includes("button")) return dbCards;
    return [];
  },
};
globalThis.document.querySelector = (sel) =>
  (sel.includes("main") ? main : null);
globalThis.document.querySelectorAll = (sel) => (sel === "a[href]" ? navLinks : []);
globalThis.CSS = { escape: (s) => s };

const form = await run("form_state", {});
check("form_state finds the text field", form.controls?.length === 1, JSON.stringify(form.controls));
check("it reports the choice CARDS, not just inputs", form.choices?.length === 3,
  JSON.stringify(form.choices));
check("it says which choice is already selected",
  form.choices?.find((c) => c.text === "OrioleDB single")?.selected === true,
  JSON.stringify(form.choices));
check("and which are not",
  form.choices?.find((c) => c.text === "OrioleDB HA")?.selected === false);
check("navigation is kept OUT of the form", !form.choices.some((c) => c.text === "Dashboard"),
  JSON.stringify(form.choices.map((c) => c.text)));
check("navigation is still available, listed separately", form.links?.length === 6,
  String(form.links?.length));
check("it says which region it read", form.region === "main content", form.region);

const bad = await run("no_such_function", {});
check("an unknown function name is refused, not evaluated",
  /no such site function/.test(bad.error || ""), JSON.stringify(bad));

const noQuery = await run("promql", {});
check("promql without a query says so", Boolean(noQuery.error));

console.log();
console.log(failed ? `${failed} failed` : "all passed");
process.exit(failed ? 1 : 0);
