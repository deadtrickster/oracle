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

**"Compare the two most recent OrioleDB runs."**
1. `navigate` to `/t/{slug}/runs?db=orioledb&status=completed&sort=created_at&dir=desc`
2. `read_page` and take the top two run ids (the id is in each row's link, and shown in full on the
   run page as the long uuid under the title).
3. `navigate` to `/t/{slug}/compare?runIds={newest},{second}` and read the result.
Compare needs at least two ids and refuses fewer — do not send one and hope.

**"How did this run go?"** — you are probably already on it. `?view=overview` has the config and
identity; `?view=metrics` has the numbers as text; `?view=grafana` is an EMBEDDED DASHBOARD, so
`read_page` will only ever say "Loading" — use `look_at_page` there. Its **Open** button launches
Grafana in a NEW TAB, which these tools cannot reach; it is not broken, it is elsewhere.

**"Why did it fail?"** — `?view=logs&q=error`, then `?view=pipeline` for which stage stopped, then
`?view=agents` if the agents were offline.

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
