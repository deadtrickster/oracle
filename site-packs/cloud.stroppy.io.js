/* ORACLE-SITE-TOOLS
{
 "namespace": "__oracle_stroppy",
 "functions": {
  "api": {
   "description": "Call one of this panel's read-only API operations and get the exact JSON back. Use list_operations first to see what exists and what each one takes. This is how you get NUMBERS — a value read off a chart image is a transcription, this is the value.",
   "params": {
    "procedure": "the operation, e.g. /cloud.v1.api.TestRunService/ListTestRuns",
    "message": "object — the request fields, as given by list_operations"
   },
   "allowlist": {"file": "cloud.stroppy.io.readonly.json", "param": "procedure"}
  },
  "list_operations": {
   "description": "List the panel's read-only API operations, with the request fields each one takes. Free and instant — no network, no model swap. Call it whenever you are unsure what to send.",
   "params": {"match": "optional substring filter, e.g. 'run', 'wizard', 'preset'"},
   "local": true
  },
  "where_am_i": {
   "description": "The current tenant slug, run id and view parsed out of the address bar, plus whether an API token has been observed yet. Cheap way to get the ids that almost every API call needs.",
   "params": {}
  },
  "metric_names": {
   "description": "The metric names actually recorded for a run — the real series, not what you assume exists. Call this before writing PromQL so you query something that is there. Filter with `match` (e.g. 'cpu', 'tx', 'latency').",
   "params": {
    "runId": "optional — defaults to the run in the address bar",
    "match": "optional substring filter"
   }
  },
  "promql": {
   "description": "Run a PromQL query against ONE run's metrics and get the numbers back. This is how you answer 'why is it slow' properly: correlate throughput against CPU, find the exact second latency broke, compute a real percentile. Read-only, and the server narrows every query to this one run — you cannot reach another run or tenant. Prefer this to reading values off a Grafana screenshot.",
   "params": {
    "query": "PromQL, e.g. rate(stroppy_tx_total[1m]) or histogram_quantile(0.99, ...)",
    "runId": "optional — defaults to the run in the address bar",
    "start": "optional RFC3339 or unix seconds; defaults to 24h ago and is clamped to the run",
    "end": "optional; defaults to now, clamped to the run",
    "step": "optional resolution like '15s'; omit for instant-value queries",
    "instant": "true for a single value instead of a series"
   }
  }
 }
}
*/
//
// Runs in the PAGE (MAIN world), because the thing it needs — the panel's own access token — lives
// in the page's module scope and is deliberately not reachable from an isolated content script.
//
// ## Why named functions rather than "let the model write JavaScript"
//
// Both give the model the same reach on paper. They differ entirely in what can be reviewed. A
// generated snippet is unbounded: one line in an authenticated session can reach every mutating
// endpoint on the origin, and nothing reads it before it runs. These functions are code we wrote,
// that we can diff, and whose worst case is bounded by an allowlist generated from the API's own
// side-effect markers. The model picks which arm to move; it does not get to build the arm.
//
// ## How the token is obtained, and why not the other ways
//
// The panel holds a bearer in a module-scoped variable and a refresh token in localStorage. Reading
// the refresh token and minting a fresh access token WOULD work and is exactly the wrong thing to
// do: that token is single-use with rotation, so spending it invalidates the copy the app is
// holding, and the user gets signed out by their own assistant. Instead we watch: wrap fetch, and
// keep the Authorization header the app itself sends. Passive, spends nothing, and cannot desync.
//
// The cost is that we need the app to have made at least one request since injection. On a run page
// that is immediate; on a page sitting idle it is not, so `api` waits briefly and then says plainly
// what to do rather than failing with something cryptic.
//
// When this chat is integrated INTO the panel, all of this collapses to the panel handing over its
// own client, and the fetch wrapper should be deleted rather than kept as a fallback.

(() => {
  const KEY = "__oracle_stroppy";
  if (window[KEY]) return; // idempotent: the harness may inject on every call

  let token = null;

  const capture = (headers) => {
    try {
      const h =
        headers instanceof Headers
          ? headers.get("authorization")
          : headers && (headers.Authorization || headers.authorization);
      if (h && /^Bearer\s+\S+/i.test(h)) token = h;
    } catch {
      /* never let observation break the page's own request */
    }
  };

  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      if (init && init.headers) capture(init.headers);
      else if (input instanceof Request) capture(input.headers);
    } catch {
      /* as above */
    }
    return origFetch.apply(this, arguments);
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // The panel re-requests constantly while a run page is open, so a short wait almost always
  // succeeds. If it does not, say which lever the caller has rather than reporting a bare failure —
  // an error a model cannot act on costs a whole turn.
  const waitForToken = async () => {
    for (let i = 0; i < 12 && !token; i++) await sleep(250);
    return token;
  };

  const route = () => {
    const p = location.pathname;
    return {
      tenant: (p.match(/\/t\/([^/]+)/) || [])[1] || null,
      runId: (p.match(/\/runs\/([0-9a-f-]{8,})/i) || [])[1] || null,
      view: new URLSearchParams(location.search).get("view"),
      url: location.href,
    };
  };

  const fns = {
    where_am_i: async () => ({ ...route(), tokenObserved: Boolean(token) }),

    api: async ({ procedure, message }) => {
      if (!(await waitForToken())) {
        return {
          error:
            "No API token seen yet. The panel has not made a request since this helper was " +
            "installed. Click something, or navigate, then call this again.",
        };
      }
      // Connect unary over POST+JSON — the protocol the panel's own client speaks. (The REST paths
      // in the OpenAPI spec are NOT usable here: ogen decodes a JSON body on GET, and browsers do
      // not send bodies on GET.)
      let res;
      try {
        res = await origFetch(location.origin + procedure, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            Authorization: token,
          },
          body: JSON.stringify(message || {}),
        });
      } catch (e) {
        return { error: `request failed: ${e && e.message}` };
      }
      const text = await res.text();
      let body;
      try {
        body = JSON.parse(text);
      } catch {
        body = text.slice(0, 2000);
      }
      if (!res.ok) {
        // Connect puts {code, message} in the body; surface both, because "invalid_argument:
        // tenant_id is required" tells the model how to fix the call and "HTTP 400" does not.
        return {
          error: `${res.status} ${(body && (body.code || body.message)) || ""}`.trim(),
          detail: body && body.message,
        };
      }
      return body;
    },
  };

  // ---------------------------------------------------------------- metrics
  //
  // The panel exposes a Prometheus-compatible read proxy at /public/metrics/, and it is safe to
  // hand a model in a way that is worth spelling out, because "let it write queries" normally is
  // not. The proxy — not us, not the dashboard URL, not the query text — enforces the scope: it
  // strips any client-supplied `extra_filters[]`/`extra_label`, forces `stroppy_run_id=<run>`,
  // clamps the time window to that run, allows only VictoriaMetrics READ endpoints, and 404s
  // indistinguishably when the cookie is missing, unknown, revoked or expired. Because the filter
  // is applied inside VictoriaMetrics, a query that names a foreign run simply returns no rows.
  //
  // So the model controls the QUESTION and never the SCOPE. That is a different risk class from
  // arbitrary JavaScript: the worst outcome of a bad query is an empty or expensive result on one
  // run the user is already looking at.
  //
  // The cookie is minted by the GrafanaSession RPC — the same exchange the panel does for its own
  // iframes, and its own comment notes the token grants no more than the bearer that minted it.
  const tenants = new Map();

  const tenantId = async (slug) => {
    if (tenants.has(slug)) return tenants.get(slug);
    const t = await fns.api({
      procedure: "/cloud.v1.api.IamService/GetTenant",
      message: { slug },
    });
    const id = t && t.tenant && t.tenant.id;
    if (id) tenants.set(slug, id);
    return id;
  };

  let scopedRun = null;
  const ensureScope = async (runId) => {
    if (scopedRun === runId) return true;
    const r = route();
    if (!r.tenant) return false;
    const tid = await tenantId(r.tenant);
    if (!tid) return false;
    const s = await fns.api({
      procedure: "/cloud.v1.api.TestRunOverviewService/GrafanaSession",
      message: { tenantId: tid, runId },
    });
    if (!s || !s.scopeToken) return false;
    document.cookie = `stroppy_share=${s.scopeToken}; path=/; max-age=43200; samesite=lax`;
    scopedRun = runId;
    return true;
  };

  const metricsGet = async (rel, params) => {
    const qs = new URLSearchParams(params).toString();
    const res = await origFetch(`${location.origin}/public/metrics/${rel}?${qs}`, {
      credentials: "same-origin",
    });
    const text = await res.text();
    let body;
    try {
      body = JSON.parse(text);
    } catch {
      body = text.slice(0, 1000);
    }
    if (!res.ok) return { error: `${res.status} from the metrics proxy`, detail: body };
    return body;
  };

  fns.metric_names = async ({ runId, match }) => {
    const run = runId || route().runId;
    if (!run) return { error: "no run id — open a run page or pass runId" };
    if (!(await ensureScope(run))) {
      return { error: "could not obtain a metrics scope for this run (GrafanaSession failed)" };
    }
    const body = await metricsGet("api/v1/label/__name__/values", {});
    if (body.error) return body;
    let names = body.data || [];
    if (match) names = names.filter((n) => n.toLowerCase().includes(String(match).toLowerCase()));
    return { runId: run, count: names.length, names: names.slice(0, 400) };
  };

  fns.promql = async ({ query, runId, start, end, step, instant }) => {
    const run = runId || route().runId;
    if (!query) return { error: "promql needs a query" };
    if (!run) return { error: "no run id — open a run page or pass runId" };
    if (!(await ensureScope(run))) {
      return { error: "could not obtain a metrics scope for this run (GrafanaSession failed)" };
    }
    // Defaults are deliberately wide and left to the proxy to clamp: it pins the window to the
    // run's own start/finish, so "24h ago until now" resolves to exactly the run without us having
    // to fetch its timestamps first.
    const now = Math.floor(Date.now() / 1000);
    if (instant) {
      const body = await metricsGet("api/v1/query", { query, time: end || String(now) });
      return { runId: run, query, result: body.data || body };
    }
    const body = await metricsGet("api/v1/query_range", {
      query,
      start: start || String(now - 24 * 3600),
      end: end || String(now),
      step: step || "15s",
    });
    if (body.error) return body;
    // Series can be thousands of points; a model does not need every sample to see a shape, and
    // pushing them all through the context window would crowd out the actual question (Axiom 1).
    const result = (body.data && body.data.result) || [];
    return {
      runId: run,
      query,
      series: result.slice(0, 20).map((s) => {
        const vals = s.values || (s.value ? [s.value] : []);
        const nums = vals.map((v) => Number(v[1])).filter((n) => Number.isFinite(n));
        const every = Math.max(1, Math.ceil(vals.length / 60));
        return {
          labels: s.metric,
          points: vals.length,
          min: nums.length ? Math.min(...nums) : null,
          max: nums.length ? Math.max(...nums) : null,
          mean: nums.length ? nums.reduce((a, b) => a + b, 0) / nums.length : null,
          last: nums.length ? nums[nums.length - 1] : null,
          // Downsampled evenly so the SHAPE survives — a spike at 40% through the run is still at
          // 40% through this array. Summary statistics alone cannot answer "when did it fall over".
          sampled: vals.filter((_, i) => i % every === 0).map((v) => [Number(v[0]), Number(v[1])]),
        };
      }),
      truncated: result.length > 20 ? `${result.length} series, showing 20` : undefined,
    };
  };

  window[KEY] = {
    version: 1,
    call: async (fn, args) => {
      const f = fns[fn];
      if (!f) return { error: `no such site function: ${fn}` };
      try {
        return await f(args || {});
      } catch (e) {
        return { error: String((e && e.message) || e) };
      }
    },
  };
})();
