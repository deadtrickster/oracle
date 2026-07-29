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
  "run_report": {
   "description": "EVERYTHING about one run in a single call — identity, status, config, the metrics the panel computed, and quota usage. Start here for 'how did this run go' or 'why is it slow': it is one step instead of five, and every number is exact. Follow up with promql only for what this does not answer.",
   "params": {"runId": "optional — defaults to the run in the address bar"}
  },
  "design_context": {
   "description": "Everything needed to CONFIGURE a run, in one call: the workloads this stroppy version actually embeds, the database/workload presets this tenant has, the stroppy versions available, and the quota headroom. Use this before proposing parameters, so the proposal is made of things that exist and fit.",
   "params": {"version": "optional stroppy version for the probe catalog, e.g. 5.4.0"}
  },
  "form_state": {
   "description": "The FORM as data — the fields, the choices (cards/tabs/toggles) with which one is already selected, what is disabled, and the exact selector for type_text. Scoped to the main content, so site navigation does not drown the form. Use this instead of screenshotting a form: a picture has no selectors and cannot be quoted.",
   "params": {"selector": "optional — limit to one region, e.g. 'form' or '.wizard'"}
  },
  "wizard_draft": {
   "description": "The new-run wizard's draft as the SERVER holds it — the whole configuration, whichever step is on screen. This is the ground truth for what a run is currently configured with; the form is a rendering of it. Reads ?draft= from the URL when you do not pass an id.",
   "params": {"draftId": "optional — defaults to the draft in the address bar"}
  },
  "goto": {
   "description": "Move around the panel WITHOUT reloading the page — this drives the app's own router, so it is instant, nothing flickers, and the chat panel stays put. Always prefer this to `navigate` on this site. Takes a path like /t/default/runs?db=orioledb&sort=created_at&dir=desc.",
   "params": {"path": "an in-app path beginning with /", "wait": "optional ms to let the view render (default 500)"},
   "acts": true
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

  // The bearer lives on `window`, not in this closure, so it SURVIVES a re-install.
  //
  // Re-installing on a source change (see the hash guard in background.js) rebuilds the closure,
  // which threw away the captured token — so the first call after any helper edit failed with "no
  // token", and because run_report resolves the tenant first, it surfaced as "could not resolve the
  // tenant from the URL". A fix to one thing quietly broke another and then lied about which.
  //
  // Re-capture would eventually happen anyway, but only after the app makes another request, which
  // on an idle page can be never.
  const TOKEN_KEY = "__oracle_stroppy_bearer";
  let token = window[TOKEN_KEY] || null;

  const capture = (headers) => {
    try {
      const h =
        headers instanceof Headers
          ? headers.get("authorization")
          : headers && (headers.Authorization || headers.authorization);
      if (h && /^Bearer\s+\S+/i.test(h)) {
        token = h;
        window[TOKEN_KEY] = h;      // survives a helper re-install
      }
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

  // ---------------------------------------------------------------- bundled questions
  //
  // One helper per QUESTION a user actually asks, rather than one per endpoint.
  //
  // Not sugar. Every round trip through the text model is a chance to emit a malformed tool call —
  // this repo carries salvage code because qwen leaks `<function=…>` — and a chain of five calls
  // fails five times as often as one. It is also five model invocations, each holding a GPU lease,
  // to answer a question the server can answer in a single batch of parallel requests.
  //
  // The composition is fixed here, in code, where it can be read and corrected, instead of being
  // re-derived by the model from prose every time. The model still chooses to ASK the question;
  // it no longer has to remember which five endpoints constitute an answer.
  //
  // Failures are per-part and reported: half an answer that says which half is missing beats a
  // rejected promise that loses the four calls that worked.
  const settle = async (parts) => {
    const keys = Object.keys(parts);
    const got = await Promise.all(keys.map((k) => parts[k].catch((e) => ({
      error: String((e && e.message) || e) }))));
    const out = {};
    keys.forEach((k, i) => { out[k] = got[i]; });
    return out;
  };

  // Returns the id, or an {error} explaining WHY there isn't one.
  //
  // It used to return null for every cause, so "the API call failed" and "there is no tenant in the
  // URL" arrived as the same sentence — and the one that actually happened (no bearer yet) was the
  // one the message ruled out. An error that names the wrong cause is worse than a vague one.
  const tenantFromRoute = async () => {
    const r = route();
    if (!r.tenant) {
      return { error: "no tenant in the URL — open a /t/{slug}/… page first" };
    }
    if (tenants.has(r.tenant)) return tenants.get(r.tenant);
    const t = await fns.api({
      procedure: "/cloud.v1.api.IamService/GetTenant",
      message: { slug: r.tenant },
    });
    if (t && t.error) return { error: `tenant lookup failed: ${t.error}` };
    const id = t && t.tenant && t.tenant.id;
    if (!id) return { error: `tenant ${r.tenant} did not resolve to an id` };
    tenants.set(r.tenant, id);
    return id;
  };

  // Shape the report; do not dump it.
  //
  // The first version returned four raw API responses and blew straight through the 60,000-char
  // tool-result cap on a REAL run — the JSON came back truncated mid-object, which is the one
  // failure mode this whole layer exists to avoid. The overview snapshot alone carries topology,
  // every stage and every event; almost none of that answers "how did this run go".
  //
  // So pick. Metrics keep their full summary (that is the answer). Everything else is reduced to
  // identity, status and counts, with a pointer to the call that returns the detail.
  const pick = (o, keys) => {
    const out = {};
    for (const k of keys) if (o && o[k] !== undefined) out[k] = o[k];
    return out;
  };

  fns.run_report = async ({ runId }) => {
    const run = runId || route().runId;
    if (!run) return { error: "no run id — open a run page or pass runId" };
    const tid = await tenantFromRoute();
    if (!tid || tid.error) return tid && tid.error ? tid : { error: "no tenant" };
    const call = (procedure, message) => fns.api({ procedure, message });
    const got = await settle({
      overview: call("/cloud.v1.api.TestRunOverviewService/GetTestRunOverview",
                     { tenantId: tid, runId: run }),
      metrics: call("/cloud.v1.api.TestRunOverviewService/GetRunMetrics",
                    { tenantId: tid, runId: run }),
      run: call("/cloud.v1.api.TestRunService/GetTestRun", { tenantId: tid, id: run }),
      quota: call("/cloud.v1.api.QuotaService/GetRunQuotaUsage", { tenantId: tid, runId: run }),
    });

    const err = (v) => v && v.error ? { error: v.error } : null;
    const metrics = got.metrics;
    const list = ((metrics || {}).metrics || {}).metrics || [];
    const snap = ((got.overview || {}).snapshot) || {};
    // GetTestRun returns {run: {...}} and its `deploymentPlan` alone is ~60k characters — every
    // provisioning step for every node. Reach past it deliberately: the identity is what a reader
    // wants, and the plan is summarised to node + status below.
    const rec = ((got.run || {}).run) || {};
    const nodes = (((rec.deploymentPlan || {}).components) || [])
      .map((c) => ({ node: c.nodeId, status: c.status, role: (c.labels || {}).role }));

    return {
      runId: run,
      // Every metric with all four statistics, because which one a chart shows is exactly what gets
      // misread: avg 889, max 2524 and last 1845 of ONE series were once reported as three
      // conflicting throughputs for the same run.
      metrics: err(metrics) || list.map((m) => pick(m,
        ["key", "name", "group", "unit", "avg", "min", "max", "last", "higherIsBetter"])),
      run: err(got.run) || pick(rec, ["id", "name", "status", "createdAt", "startedAt",
                                      "finishedAt", "duration", "trigger", "dbKind", "workload",
                                      "stroppyVersion", "provider", "progress", "entity"]),
      nodes,
      status: snap.run?.status || rec.status,
      stages: err(got.overview) ||
        (snap.stages || []).map((s) => pick(s, ["name", "status", "startedAt", "finishedAt"])),
      events: (snap.events || []).length,
      degraded: snap.degradedReasons || [],
      quota: err(got.quota) || got.quota,
      note: "Summaries, not series. `avg`/`min`/`max`/`last` are of the WHOLE run — say which one " +
            "you are quoting. For shape over time use promql; for stage detail or the event log " +
            "call GetTestRunOverview or QueryLogs directly.",
    };
  };

  fns.design_context = async ({ version }) => {
    const tid = await tenantFromRoute();
    if (!tid || tid.error) return tid && tid.error ? tid : { error: "no tenant" };
    const call = (procedure, message) => fns.api({ procedure, message });
    return await settle({
      // The catalog comes from the stroppy BINARY (the server runs `stroppy probe -o json`), so it
      // is the truth about what this version can run — not a remembered list of workload names.
      workloads: call("/cloud.v1.api.TestWizardService/ProbeCatalog", { version: version || "" }),
      stroppyVersions: call("/cloud.v1.api.StroppyService/ListStroppyVersions", { tenantId: tid }),
      databasePresets: call("/cloud.v1.api.DatabasePresetService/ListDatabasePresets",
                            { tenantId: tid, page: { size: 50 } }),
      workloadPresets: call("/cloud.v1.api.WorkloadPresetService/ListWorkloadPresets",
                            { tenantId: tid, page: { size: 50 } }),
      testPresets: call("/cloud.v1.api.TestPresetService/ListTestPresets",
                        { tenantId: tid, page: { size: 50 } }),
      quotas: call("/cloud.v1.api.QuotaService/ListQuotas", { tenantId: tid }),
    });
  };

  // ---------------------------------------------------------------- forms as data
  //
  // Why this exists, and why the alternative was not "tell it to stop":
  //
  // Asked to start an OrioleDB run, the model routed to /runs/new correctly, then screenshotted the
  // form and sent it to the vision model. That is not a discipline failure, it is the only move it
  // had. `read_page` returns innerText, and a form's meaning is not in its text: the current value
  // of a select, the options it offers, whether a field is disabled, and — critically — the
  // selector needed to type into it are all invisible to innerText and all visible in a picture.
  // Vision was the strictly better tool for the information available.
  //
  // Writing "do not screenshot the form" into the prompt would have left that true and asked the
  // model to behave worse anyway. So: give it the thing it actually needed. With labels, values,
  // options AND selectors in hand, the screenshot stops being attractive without anyone being told.
  //
  // The selectors matter as much as the values. Guessing them is a known failure here — a model
  // invented `div[data-testid="metrics-panel"]` and got nothing back — and every selector returned
  // by this function is one that resolved a moment ago.
  const labelFor = (el) => {
    const aria = el.getAttribute("aria-label");
    if (aria) return aria.trim();
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const n = document.getElementById(by);
      if (n) return (n.innerText || "").replace(/\s+/g, " ").trim();
    }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return (l.innerText || "").replace(/\s+/g, " ").trim();
    }
    const wrap = el.closest("label");
    if (wrap) return (wrap.innerText || "").replace(/\s+/g, " ").trim();
    return (el.getAttribute("placeholder") || el.getAttribute("name") || "").trim();
  };

  // A selector that will still resolve when the model uses it. Prefer the stable, app-authored
  // hooks; fall back to nth-of-type only when there is nothing better.
  const selectorFor = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    for (const attr of ["data-testid", "name", "aria-label"]) {
      const v = el.getAttribute(attr);
      if (v) return `${el.tagName.toLowerCase()}[${attr}="${v.replace(/"/g, '\\"')}"]`;
    }
    const same = [...document.querySelectorAll(el.tagName)];
    return `${el.tagName.toLowerCase()}:nth-of-type(${same.indexOf(el) + 1})`;
  };

  // Chrome is not the form. The first version returned everything clickable on the page, in DOM
  // order, so a model asking "what can I set here?" got: stroppy-cloud, default, Test Runs, admin,
  // Dashboard, Quotas, Suites… twelve navigation links before anything belonging to the wizard. It
  // reported "the form only shows the name field" — correctly, from what it was given.
  //
  // Two corrections. Scope to the main region, dropping nav/header/aside and our own panel. And
  // report SELECTION STATE: this wizard picks a database and a provider with <button> cards, not a
  // <select>, so "which one is chosen" lives in aria-pressed/aria-selected/data-state and is
  // invisible to anything that only reads labels.
  const mainRegion = () =>
    document.querySelector("main, [role=main], form") || document.body;

  const isChrome = (el) =>
    el.closest("nav, header, aside, [role=navigation], [data-oracle-ui]") !== null;

  // Which card is chosen — three ways, because this app uses none of the obvious one.
  //
  // The wizard renders its engine and preset cards as plain <button>s and expresses selection
  // ENTIRELY in the class list:
  //
  //     sel ? "border-primary/50 bg-primary/[0.07]" : "border-zinc-800 bg-surface-tile …"
  //
  // No aria-pressed, no data-state, no aria-current. So the first version reported no `selected`
  // on anything while the user was looking at a blue border and a checkmark — the state was on
  // screen and invisible to the tool, which is the worst combination: it invites the model to
  // re-click something already chosen.
  //
  // 1) ARIA / data attributes, when a component bothers to set them.
  // 2) Tailwind-ish "this one is active" tokens.
  // 3) ODD ONE OUT among siblings — framework-agnostic and the reason this generalises. Cards in a
  //    picker are rendered from one list with one class string; the chosen one differs. If exactly
  //    one sibling's classes differ from the shared majority, that is the selection, whatever the
  //    design system happens to call it.
  const ACTIVE_CLASS = /\b(border-primary|bg-primary|ring-2|ring-primary|bg-accent|is-active|selected|active)\b/;

  const selectedState = (el) => {
    for (const a of ["aria-pressed", "aria-selected", "aria-checked", "aria-current", "data-state",
                     "data-selected", "data-active"]) {
      const v = el.getAttribute(a);
      if (v === "true" || v === "on" || v === "checked" || v === "active" || v === "selected") return true;
      if (v === "false" || v === "off" || v === "inactive") return false;
    }
    const cls = el.className && el.className.baseVal !== undefined
      ? el.className.baseVal : String(el.className || "");
    if (ACTIVE_CLASS.test(cls)) return true;

    // Odd one out.
    const parent = el.parentElement;
    if (!parent) return undefined;
    const sibs = [...parent.children].filter(
      (n) => n.tagName === el.tagName && n.getClientRects().length);
    if (sibs.length < 3) return undefined;      // too few to establish a majority
    const classOf = (n) => String(n.className || "").replace(/\s+/g, " ").trim();
    const counts = {};
    for (const n of sibs) counts[classOf(n)] = (counts[classOf(n)] || 0) + 1;
    const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    // A clear majority plus exactly one dissenter.
    if (entries.length === 2 && entries[1][1] === 1 && entries[0][1] >= sibs.length - 1) {
      return classOf(el) === entries[1][0];
    }
    return undefined;
  };

  fns.form_state = async ({ selector }) => {
    const root = selector ? document.querySelector(selector) : mainRegion();
    if (!root) return { error: `no element matches ${selector}` };
    const vis = (n) => n.getClientRects().length > 0 && !isChrome(n);
    const controls = [...root.querySelectorAll("input, select, textarea, [role=combobox], [role=switch], [role=radio], [contenteditable=true]")]
      .filter(vis)
      .slice(0, 120)
      .map((el) => {
        const tag = el.tagName.toLowerCase();
        const type = el.getAttribute("type") || el.getAttribute("role") || tag;
        const out = {
          label: labelFor(el).slice(0, 80),
          type,
          selector: selectorFor(el),
          disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true"),
        };
        if (tag === "select") {
          out.value = el.value;
          out.options = [...el.options].slice(0, 60).map((o) => o.value || o.text);
        } else if (type === "checkbox" || type === "radio" || type === "switch") {
          out.checked = Boolean(el.checked || el.getAttribute("aria-checked") === "true");
        } else {
          out.value = (el.value !== undefined ? el.value : el.innerText || "").slice(0, 200);
        }
        return out;
      });
    // Cards and toggles the wizard is actually made of. `selected` is the whole point: without it
    // you can list every database option and still not know which one is currently picked.
    const choices = [...root.querySelectorAll("button, [role=button], [role=tab], [role=option], [role=radio]")]
      .filter(vis)
      .map((el) => {
        const sel = selectedState(el);
        return {
          text: (el.innerText || el.value || "").replace(/\s+/g, " ").trim().slice(0, 80),
          disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true"),
          ...(sel === undefined ? {} : { selected: sel }),
        };
      })
      .filter((b) => b.text)
      .slice(0, 80);
    // Links out of the form, kept separate and short. They are navigation, and mixing them in with
    // the choices is what made the form unreadable.
    const links = [...document.querySelectorAll("a[href]")]
      .filter((n) => n.getClientRects().length > 0)
      .map((el) => ({ text: (el.innerText || "").replace(/\s+/g, " ").trim().slice(0, 40),
                      href: el.getAttribute("href") }))
      .filter((l) => l.text)
      .slice(0, 25);
    return { url: location.href, region: root === document.body ? "whole page" : "main content",
             controls, choices, links,
             note: "type_text takes a control's selector verbatim; click takes a choice's text. " +
                   "`selected` shows what is already picked. Navigation links are listed " +
                   "separately because they are not part of the form — prefer goto for those." };
  };

  fns.wizard_draft = async ({ draftId }) => {
    const id = draftId || new URLSearchParams(location.search).get("draft");
    if (!id) return { error: "no draft in the URL — open /runs/new first, or pass draftId" };
    const r = route();
    if (!r.tenant) return { error: "no tenant in the URL" };
    const tid = await tenantId(r.tenant);
    const draft = await fns.api({
      procedure: "/cloud.v1.api.TestWizardService/GetTestWizardDraft",
      message: { tenantId: tid, draftId: id },
    });
    return { step: new URLSearchParams(location.search).get("step") || "database", draft };
  };

  // ---------------------------------------------------------------- in-app routing
  //
  // `navigate` sets tab.url, which is a full document load: the SPA tears down and re-bootstraps,
  // the page flickers, and the chat panel — injected into that document — is destroyed and has to
  // be put back. It works, and it looks broken.
  //
  // The panel is a react-router-dom v7 BrowserRouter, whose history listens for `popstate`. So
  // pushState + a popstate event is a route change the app performs ITSELF: same result, no
  // reload, no flicker, panel untouched. This is the payoff of being allowed to read the app's
  // source — the difference is invisible in the URL bar and total in how it feels.
  //
  // Marked `acts: true`: it changes what the user is looking at. Cheap and reversible, but still
  // their screen, so it goes through the same per-host gate as click.
  fns.goto = async ({ path, wait }) => {
    if (!path || typeof path !== "string" || !path.startsWith("/")) {
      return { error: "goto needs an in-app path starting with /" };
    }
    const before = location.href;
    const url = new URL(path, location.origin);
    if (url.origin !== location.origin) return { error: "goto only moves within this site" };
    history.pushState({}, "", url.pathname + url.search + url.hash);
    window.dispatchEvent(new PopStateEvent("popstate", { state: {} }));
    await sleep(Math.max(100, Math.min(5000, Number(wait) || 500)));
    // Report what the app actually rendered. A router that ignored the event leaves the URL changed
    // and the view stale, which would otherwise look like success and produce an answer about the
    // wrong page.
    const h1 = document.querySelector("h1, [role=heading]");
    return {
      url: location.href,
      changed: location.href !== before,
      title: document.title,
      heading: (h1 && h1.innerText || "").replace(/\s+/g, " ").trim().slice(0, 120),
      note: "routed in-app; the page did not reload",
    };
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
