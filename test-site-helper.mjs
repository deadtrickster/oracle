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

const bad = await run("no_such_function", {});
check("an unknown function name is refused, not evaluated",
  /no such site function/.test(bad.error || ""), JSON.stringify(bad));

const noQuery = await run("promql", {});
check("promql without a query says so", Boolean(noQuery.error));

console.log();
console.log(failed ? `${failed} failed` : "all passed");
process.exit(failed ? 1 : 0);
