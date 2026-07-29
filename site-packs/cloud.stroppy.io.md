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

### Every metric has four numbers — say which one you mean

`run_report` returns each metric as `avg`, `min`, `max` and `last` over the whole run. A chart shows
ONE of those, and which one is rarely labelled. Measured on a real completed run:

    db_tps   avg 889.13   max 2523.93   min 6.13   last 1845.33   txn/s

Those are the same series. This is the origin of a genuine failure: one run was reported as doing
"2.36K tx/s", "841 tx/s" and "1.8K tx/s" from three different views, and it looked like the panel
contradicting itself. It was max, avg and last — three correct answers to three different questions,
read off charts and presented as rivals.

So: never state a throughput or latency without saying which statistic it is. "Averaged 889 txn/s,
peaking at 2.5K" is an answer; "did 2.36K txn/s" is a number that will not reproduce. `min 6.13` is
also worth noticing — it is the ramp-up, not a stall.

Groups you will see: `throughput` (`db_tps`, `db_qps`), `connections` (`db_connections`), plus
latency and engine groups. Use the `key`, not the display name, when you go on to write PromQL.

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
| compare runs | `/t/{slug}/compare?runIds={id1},{id2}` — **two or more ids required**; `runs=` is accepted as an alias |
| new run | `/t/{slug}/runs/new` |
| suites | `/t/{slug}/suites`, `/suites/{id}` |
| quotas | `/t/{slug}/quotas` |
| presets | `/t/{slug}/presets/database` · `/presets/workload` · `/presets/test` (+ `/{id}`) |
| packages | `/t/{slug}/packages`, `/packages/{id}` |
| a shared run (no tenant) | `/shared/{token}` |

**Run detail tabs** — `?view=` one of `overview`, `pipeline`, `topology`, `logs`, `metrics`,
`quotas`, `grafana`, `agents`. Logs additionally take `&steps={id}&q={text}`, so
`?view=logs&q=error` goes straight to the errors.

**The runs list's filter UI is unreachable — use the URL.** Each filter lives inside a column-header
popover and is not in the DOM until that header is opened, so `form_state` shows no controls and
there is nothing to type into. That is not a bug and not worth investigating: the whole filter set is
in the query string below, and building a URL is one step instead of open-popover-type-apply.

**Run rows carry their ids.** In `form_state`, each row's text includes the run's UUID, and its link
is `/t/{slug}/runs/{id}` — so you can pick a run without `read_page` and without clicking.

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

### Building a run in the wizard — the flow, verified end to end

This was driven on the live panel and every step below is what actually worked, in order. Do not
improvise around it; the wizard is progressive and each stage only reveals the next once the
previous one is satisfied.

1. `goto /t/{slug}/runs/new`. The bare page has ONE control — the run-name field — and a `Start`
   button, plus a `Resume` per existing draft. There may be twenty of those; they are all labelled
   `Resume` and are indistinguishable by text, so do not try to click one by name.
2. `type_text` the run name, then `click "Start"`. This creates a draft and moves you to
   `?draft={id}&step=database`.
3. **Database step.** `click` the engine (`OrioleDB`, `PostgreSQL`, `MySQL`, `MariaDB`, `Picodata`,
   `YDB`, `YDB Managed`, `CockroachDB`, `No-DB`, `pg-noop`, `External`) — this only reveals the
   preset list. Then `click` a preset: `OrioleDB single` (one container) or `OrioleDB HA` (master +
   streaming replica + HAProxy). `Next`.
4. **Workload step.** Pick a stroppy VERSION FIRST — the preset list does not exist until you do.
   Release tags are listed newest-first (`5.7.2`, `5.7.1`, …) under a `release`/`commit` toggle.
   Then `click` a workload preset, e.g. `TPC-C split (bootstrap + workload)`. `Next`.
5. **Infrastructure step.** `click` a provider: `Docker` (local containers, single host, smoke
   tests) or `Yandex Cloud` (Terraform-provisioned VMs, real multi-node). Then `click`
   `Validate topology` — the plan must be confirmed per node, and `Next` DOES NOT APPEAR until it
   is. When it succeeds the page gains `READY` and `Next`.
6. **Review step.** Stop. Launching provisions real machines and spends real time; hand the user the
   configuration and let them press it.

**`wizard_draft` is how you know whether a step worked.** The server returns an `errors` list naming
exactly what is still missing — `database is required`, `workload is required`, `provider is
required`, `confirm machine settings for every node: …` — and entries disappear as you satisfy them.
That is a precise, machine-readable answer to "did my click take effect", and it beats screenshotting
the page or trusting the click's own report. Call it after each choice.

**Clicking `Next` when the step is unsatisfied does nothing at all**, silently. If a click reports
that the page did not change, read `wizard_draft` rather than clicking again.

**On the new-run page, the form is readable as data.** `form_state` gives every control: its label,
type, current value, the options a dropdown offers, whether it is disabled, and the selector to use
with `type_text`.

Three things about this panel specifically, learned by driving it:

- **The engine and preset cards are `<button>`s, not a `<select>`** — they come back under
  `choices`, not `controls`. `controls` on the database step is empty, and that is normal.
- **`selected` is often absent.** The wizard marks the chosen card with a border colour and nothing
  else — no `aria-pressed`, no `data-state` — so `form_state` infers it and can be wrong. When it
  matters, `wizard_draft` is authoritative: it says what the SERVER has, which is what the run will
  actually use.
- **Click cards by their leading words.** A card's text includes its whole description (`OrioleDB
  single BUILTIN Single OrioleDB container (docker).`), and `click` matches on a substring, so
  `"OrioleDB single"` is enough and is far more robust than pasting the full label. `wizard_draft` gives the configuration as the SERVER holds it — the form is a
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
