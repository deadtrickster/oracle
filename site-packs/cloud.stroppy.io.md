<!--
MAINTENANCE NOTES — stripped before this file reaches a model.

Covers cloud.stroppy.io and every subdomain (stage.cloud., etc). COMPOSES with stroppy.io.md,
which supplies the vocabulary and the PostgreSQL/OrioleDB engine notes; this file is only about
the CLOUD PANEL — what it has and how to reach it.

Written from the source, not from clicking around: routes from web/src/App.tsx, the runs-list query
grammar from the encoding block in web/src/pages/Runs.tsx, run-detail views from the `View` union in
web/src/pages/RunDetail.tsx, compare params from web/src/pages/Compare.tsx. When those files change,
this is stale — and a URL grammar that is stale is worse than absent, because the model will use it
confidently. Re-read those four files rather than trusting this.
-->
# stroppy-cloud — the control panel

A web UI for running Stroppy benchmarks against managed databases and reading the results. Runs are
scoped to a TENANT; every working URL starts `/t/{slug}/`, and the slug is in the address bar of
whatever page the user is on (`/t/default/...` → slug is `default`).

## The panel has an API, and you can call it — prefer it to looking

`site_call` reaches this panel's own read-only API and hands back exact JSON. Start with
`list_operations` (free, instant, no network) to see what exists and what each operation takes, then
`api` to call one.

Reach for it FIRST whenever the question is about values — throughput, latency, durations, config,
status, which runs exist. A number read off a chart image is a transcription, and transcriptions go
wrong quietly: the same run has been reported at three different throughputs from three different
tabs, and a percentile has been read as p95 off a legend that only shows p50/p90/p99. `GetRunMetrics`
does not have that failure mode. It is also far cheaper — no screenshot, no vision model, no swap.

Only the panel's read-only operations are callable, and that list is generated from the API's own
spec, so an operation that changes something is not merely discouraged, it is absent. If you need
something the list does not have — starting a run, editing a preset — say so and let the user do it.

### You can query the metrics directly — use it

`metric_names` lists the series a run actually recorded; `promql` runs a query against them and
returns real numbers. Only that one run's series are reachable — the server narrows every query
itself — so you cannot look at another run or tenant, and you do not have to be careful about it.

This is the difference between describing a chart and analysing a run:

- `metric_names(match: "cpu")` first. Query what exists, not what you assume is named
  `stroppy_tx_total`.
- `promql("rate(...[1m])")` returns min/max/mean/last over the WHOLE window plus an evenly
  downsampled series, so you can say *when* something happened, not just that it did.
- Correlate. Throughput falling while CPU is flat means something other than the box was the limit;
  falling together means it was. That comparison is one question to the data and is not reliably
  answerable by looking at two screenshots.
- `histogram_quantile(0.99, ...)` computes the percentile. Do not read a percentile off a legend
  and do not assume which one a line is — that has already produced a confident "p95" from a chart
  showing only p50/p90/p99.

Grafana is still worth a look for the shape of a run at a glance. But when the answer is a number,
a threshold, or a moment in time, query it.

Where each tool still wins:

| question | use |
|---|---|
| any number, id, config, status, list | `site_call` → `api` |
| a metric over time, a percentile, a correlation | `site_call` → `promql` |
| what a dashboard LOOKS like overall, or something not in the metrics | `look_at_page` |
| prose the page renders, error text, labels | `read_page` |
| something only reachable by clicking | `click` |

The API gives you the values; the dashboards give you the shape over time. A good answer about a run
usually needs both, and the honest order is values first — then look at the charts already knowing
what the numbers are, so you are reading the picture rather than guessing at it.

### Move with `goto`, not `navigate`

This app is a single-page router. `site_call` → `goto("/t/{slug}/runs?...")` changes the view
through the app's own routing: instant, no reload, and the chat panel stays where it is. `navigate`
sets the browser's URL, which reloads the whole application — the page flickers, the panel is
destroyed and rebuilt, and the user watches all of it. Same destination, much worse.

So on this site: `goto` for anything in the URL grammar below, `click` when the thing you want has
no URL, and `navigate` only to leave the app entirely.

## URL grammar — prefer this to clicking

This app puts its state in the URL. Building a URL and navigating is one step, exact, and leaves the
user somewhere they can check; clicking through the same filters is five steps and depends on
guessing labels. Use `navigate` when the grammar below answers the question.

| what | URL |
|---|---|
| dashboard | `/t/{slug}` |
| all runs | `/t/{slug}/runs` |
| one run | `/t/{slug}/runs/{id}` |
| compare runs | `/t/{slug}/compare?runIds={id1},{id2}` — **two or more ids required** |
| new run | `/t/{slug}/runs/new` |
| suites | `/t/{slug}/suites`, `/suites/{id}` |
| quotas | `/t/{slug}/quotas` |
| presets | `/t/{slug}/presets/database` · `/presets/workload` · `/presets/test` (+ `/{id}`) |
| packages | `/t/{slug}/packages`, `/packages/{id}` |
| a shared run (no tenant) | `/shared/{token}` |

**Run detail tabs** — `?view=` one of `overview`, `pipeline`, `topology`, `logs`, `metrics`,
`quotas`, `grafana`, `agents`. Logs additionally take `&steps={id}&q={text}`, so
`?view=logs&q=error` goes straight to the errors.

**Runs list filters** — `?q=` free text, `status=`, `db=`, `sv=` (stroppy version), `proto=`,
`trigger=`, `author=` (csv), `sa=`/`sb=` started after/before, `fa=`/`fb=` finished after/before,
`pmin=`/`pmax=` progress 0..100, `dmin=`/`dmax=` duration in seconds, `sort=`+`dir=`, `size=`,
`page=`. Toggles: `sa_only=true` (standalone runs only), `fav=1`, `del=1`, `ff=0` (disable
favourites-first).

**Sortable fields** — `name`, `created_at`, `status`, `db_kind`, `workload`, `trigger`, `duration`,
`started_at`, `finished_at`. Newest first is `?sort=created_at&dir=desc`.

## Recipes

**"Compare the two most recent OrioleDB runs."** Two ways, and the first is better.

*Through the API* — one call to `ListTestRuns` (filtered and sorted) for the ids, one to
`CompareRuns` for the diff. Exact, two steps, no swaps. Use `list_operations` for the request fields
rather than guessing them.

*Through the UI* — worth doing when the user wants to SEE it, or to leave them on a page they can
check:
1. `navigate` to `/t/{slug}/runs?db=orioledb&status=completed&sort=created_at&dir=desc`
2. `read_page` and take the top two run ids (the id is in each row's link, and shown in full on the
   run page as the long uuid under the title).
3. `navigate` to `/t/{slug}/compare?runIds={newest},{second}` and read the result.
Compare needs at least two ids and refuses fewer — do not send one and hope.

**"How did this run go?" / "why was it slow?" — start with `run_report`.** One call returns the
run's identity, status, configuration, the panel's computed metrics and its quota usage. That is the
whole factual basis for an answer, exact, in one step. Then:

1. `run_report` — what it was and what it scored.
2. `metric_names` then `promql` — for anything that is a shape over time. "Throughput fell" is in
   the summary; *when* it fell, and whether CPU moved with it, is only in the series. Query
   throughput and CPU over the same window before concluding what the limit was.
3. `look_at_page` on the Grafana tabs — last, and only for what the numbers cannot show.

Answer the "why", not just the "what". Throughput without CPU and disk cannot distinguish "the
database was slow" from "the client or the box ran out"; latency without the engine view cannot
tell a lock from a flush. You now have the data to separate those, so separate them.

**When you only have screenshots — LOOK AT EVERYTHING.** An open question about
a run means the whole run, and a partial look produces a confident partial answer, which is worse
than a slow one. The default is: `?view=overview` for config and identity, `?view=metrics` for the
numbers as text, then EVERY Grafana sub-dashboard (below). Only narrow this when the user narrows
it — "briefly", "just the throughput", "did it crash?" — and then say what you skipped.

**The Grafana tab is several dashboards, not one.** `?view=grafana` shows sub-tabs, and each
answers a different question. One of them alone cannot tell you whether the client or the server was
the limit:

| sub-tab | whose view | what only it can tell you |
|---|---|---|
| **WORKLOAD** | the client (k6/stroppy) | tx/s, latency percentiles, per-transaction rates, errors |
| **SYSTEM** | the host (node_exporter) | CPU, memory, disk I/O, network — whether the machine was saturated |
| **ORIOLEDB** / **POSTGRES** / *engine* | the database | buffers, WAL, locks, vacuum, engine internals |

Throughput without CPU and disk cannot distinguish "the database was slow" from "the client or the
box ran out". Latency without the engine view cannot tell a lock from a flush. So for an open
question, click through all of them.

Do it in ONE reply: `click` a sub-tab, `look_at_page`, `click` the next, `look_at_page` — tool calls
in the same reply run back-to-back without the text model in between, so three views cost one GPU
swap rather than three. Asking for one look per turn is what makes surveying a run feel expensive.

`read_page` will only ever say "Loading" on these — they are iframes. The **Open** button launches
Grafana in a NEW TAB, which these tools cannot reach; that is not a broken link, it is elsewhere.

**"Why did it fail?"** — `?view=logs&q=error`, then `?view=pipeline` for which stage stopped, then
`?view=agents` if the agents were offline.

**"Help me design a run."** Start with `design_context` — one call giving the workloads this
stroppy version actually embeds, the presets this tenant has, the available versions and the quota
headroom. Then narrow:

- `api` → `ProbeScript` — one script's real parameters, straight out of `stroppy probe -o json`,
  with defaults, types and ranges. `includeHuman` adds stroppy's own prose rendering. This is where
  parameter meaning comes from; do not recite knobs from memory, they are version-specific.
- `api` → `GetTenantRating` / `GetSystemRating` — how comparable configurations actually performed
  here, which beats a guess about what will be fast.
- `search_corpus` — the WHY. What a TPC-C scale factor means, why a pool size interacts with
  connection limits, how the engine behaves under the pattern being proposed. The panel tells you
  what the knobs ARE; only the corpus tells you what turning one DOES. A configuration proposed
  without that is a list of numbers.

Combine them: probe for the knobs, corpus for the meaning, presets and quotas for what is
realistic, ratings for what has worked. Then say what you would run AND WHY — naming the parameter,
its effect, and what result would confirm or refute the hypothesis being tested. A benchmark
configuration without a hypothesis is just numbers you will be unable to interpret afterwards.

**On the new-run page, the form is readable as data.** `form_state` gives every control: its label,
type, current value, the options a dropdown offers, whether it is disabled, and the selector to use
with `type_text`. `wizard_draft` gives the configuration as the SERVER holds it — the form is a
rendering of that draft, and the draft is what a run is actually built from. Between them you can
see what is set, what is missing and what is still greyed out, and you get selectors that resolved a
moment ago rather than guesses.

**You cannot start it, and should not pretend otherwise.** Creating or launching a run changes
things, so those operations are not available to you at all. Finish by handing the user a concrete
configuration — every field, ready to paste or fill into `/t/{slug}/runs/new` — and let them press
the button. That is the right split anyway: the run spends real hardware, and the person spending it
should be the one who commits.

**"What was it configured with?"** — `?view=overview`, and the preset pages under
`/t/{slug}/presets/...` for what a named preset actually contains.

## Reading a run page

The left sidebar carries IDENTITY (status, id, timestamps, duration, trigger), DATABASE (`kind` —
`orioledb`, `pg`, …), WORKLOAD (name, protocol, bootstrap and workload strings like
`tpcc/tx · pool=400 · scale=5000`), INFRASTRUCTURE (provider, nodes) and RUNTIME (agents, stages,
events, progress). Quote those verbatim rather than inferring them from the charts — they are the
run's ground truth, and the charts are a rendering of it.

`status: completed` with `progress: 98%` is normal and not a partial run; progress is a UI estimate.
`agents offline:N` on a finished run means they were released afterwards, not that the run lost
them — check `?view=agents` before saying anything went wrong there.
