<!--
MAINTENANCE NOTES — stripped before this file reaches a model, because context spent on how the
file is maintained is context not spent on the question (Axiom 1).

Covers stroppy.io and every subdomain (docs., cloud., app.). Loaded by oracle_sitectx.py when a
page on one of those hosts is explained, fact-checked, or read by the vision model.

Why this file rather than a slice of llms-full.txt: a domain pack's job is VOCABULARY, not
documentation. Truncating 60 KB of docs to fit the budget keeps whichever section happens to come
first — for Stroppy that is the Go driver interface — when a reader of a run report needs to know
what a VU is. Facts come from stroppy-mcp/llms-full.txt and the stroppy repo's AGENTS.md; correct
them there first, then here.

The "How to read the numbers" half is taken VERBATIM (headings + the "For benchmarking:" paragraphs
+ the comparison bullets) from stroppy-mcp/instructions.md sections "## PostgreSQL" and
"## OrioleDB" — copied rather than paraphrased so no fact degrades in transit. The full 31 KB of
those sections cannot go in: it would dominate every prompt (Axiom 1). Regenerate by re-extracting
the same lines if upstream changes.
-->
# Stroppy — what this site is about

## What it is

Stroppy is an open-source (Apache 2.0) **database stress-testing CLI**, built as an extension to
**k6** — Grafana's load-testing engine. Tests are TypeScript scripts; data generators produce rows
with specified statistical distributions; runs emit metrics and a self-contained HTML report.
Repo: `github.com/stroppy-io/stroppy`.

## Vocabulary (what page text will assume you know)

- **VU — virtual user.** k6's unit of concurrency: each VU runs the test script independently. "50
  VUs" means 50 concurrent scripted clients, not 50 connections or 50 threads.
- **Scenario** — how load changes over time: constant VUs, ramping VUs, shared iterations, per-VU
  iterations.
- **Threshold** — a pass/fail criterion on a metric ("p95 < 200 ms"). A run that breaches a
  threshold *fails*, regardless of whether it completed.
- **Driver** — the database adapter. Presets: `pg` (PostgreSQL, pgxpool; supports COPY),
  `mysql`, `pico` (Picodata — no transactions, isolation `none`), `ydb`, `noop`, `csv`.
- **Workload / preset** — a bundled test: `simple`, `tpcb`, `tpcc`, `tpch`, `tpcds`, `execute_sql`.
  TPC-B and TPC-C are write-heavy OLTP; TPC-H and TPC-DS are analytical.
- **Datagen** — the relational data-generation runtime (uniform, normal, zipfian distributions,
  cohorts, lookups, seeds), so generated rows have realistic key skew instead of uniform noise.
- **`stroppy gen | run | probe | version`** — scaffold a workspace, run a benchmark, inspect a
  database, print the version.

## Reading a report or dashboard

- Numbers are meaningless alone — Stroppy's own guidance is to compare deltas (before/after
  tuning, low/high concurrency, workload A/B). Treat a single figure as unanchored.
- Stock PostgreSQL ships tuned for a ~512 MB machine, so an untuned baseline says more about the
  config than the database.
- Latency is usually reported as percentiles (p50/p95/p99). A p99 far above p95 points at
  contention or flushing, not at average throughput.
- Runs must not overlap: concurrent benchmarks skew each other.

## How to read the numbers — PostgreSQL and OrioleDB

From the Stroppy MCP's curated engine notes. These are the consequences that change how a
benchmark page should be read; the mechanisms behind them are in the upstream docs.

### PostgreSQL

- **Freezing and transaction ID wraparound.** freezing is rarely visible in short tests, but understanding it explains why autovacuum runs even on append-only tables with zero dead tuples.
- **Buffer cache and the OS page cache.** when the dataset fits in the OS page cache (which it usually does at scale factor 1), tuning `shared_buffers` shows minimal effect. The data is always in RAM regardless. To see meaningful cache tuning effects, the dataset must exceed total RAM.
- **WAL (Write-Ahead Log).** WAL flush is often the bottleneck for write-heavy workloads with synchronous commit. If throughput plateaus and latency rises linearly with VUs, check whether the bottleneck is WAL sync (visible as `WalSync` wait events). The fix is faster storage, async commit (if data loss risk is acceptable), or group commit tuning.
- **Locks.** TPC-B at scale factor 1 has a single branch row updated by every transaction — pure row-lock contention. TPC-C distributes across warehouses and districts. When throughput collapses as VUs increase, check whether the cause is lock contention (visible as `Lock` wait events) or connection pool exhaustion (visible as errors or `Client` wait events).
- **Query execution and the cost model.** the queries in stress tests are typically simple (point lookups, range scans). But if a workload uses JOINs, the planner's choice between nested loop and hash join can cause dramatic throughput differences. Run ANALYZE after loading test data to ensure the planner has fresh statistics.
- **Connection pooling and pool size.** the parameter sweep that found a 14x bottleneck was caused by pool starvation — 198 VUs sharing 100 connections. Always pass `POOL_SIZE` equal to or greater than VUs, and ensure `max_connections` accommodates it.
- **Replication.** synchronous vs. asynchronous is the single most important variable for write latency distribution under replication. A workload that looks fine with async may become unusable under sync replication with a high-latency standby link. When testing read-scaling on standbys, measure replication lag alongside read throughput — high read QPS on a standby means nothing if the data is minutes stale. If the workload includes bulk operations, expect lag spikes under logical replication that don't appear under streaming replication.
- **Parallel query.** parallel query is a read-path optimization only. Write-heavy OLTP workloads get zero benefit. For read-heavy analytical queries, the speedup depends on the number of workers, data skew (uneven partition sizes stall some workers while others finish), and whether blocking operators (like hash table build) dominate. Adding more workers does not linearly reduce latency. Also, on multicore machines, memory bandwidth can saturate before CPU does — many cores sharing one memory bus hit a ceiling that more parallelism can't push past.
- **Distributed PostgreSQL and two-phase commit.** the dominant cost in distributed queries is data transfer volume — a well-filtered query ships little data, a full-table join across nodes ships everything. Partition/shard key choice determines whether queries are local (single-node) or distributed (cross-node). A benchmark that only hits co-located data dramatically understates the real cost of distribution. Synthetic uniform data hides skew problems that production workloads will trigger — skewed key distributions reveal how the system handles uneven load across nodes.

### OrioleDB

- **Index-organized tables (no heap).** workloads dominated by primary key lookups (point reads, range scans by PK) benefit significantly. Workloads that heavily use secondary indexes for non-covering queries may not see the same gains. The absence of a heap eliminates heap bloat, and page merging eliminates index bloat — OrioleDB tables don't degrade over time the way PostgreSQL tables do under churn.
- **Dual pointers and the buffer mapping problem.** the dual-pointer advantage is most visible at high concurrency (hundreds of VUs) on workloads with high page access rates. At low concurrency, the buffer mapping isn't a bottleneck and the difference is negligible.
- **Undo log MVCC (no VACUUM).** OrioleDB should show more stable throughput over time because there's no autovacuum competing for I/O. Long-running write tests that would cause table bloat in PostgreSQL won't cause bloat in OrioleDB. However, the undo log itself can grow under sustained write pressure with long-running read transactions (same horizon problem as PostgreSQL, different manifestation).
- **Row-level WAL.** write-heavy workloads should show dramatically lower WAL volume and higher throughput on storage-constrained systems. When testing under replication, OrioleDB's parallel WAL replay scales with recovery workers — PostgreSQL's page-level WAL replays sequentially on a single process, which often makes the replica the bottleneck. Skewed workloads that concentrate writes on a small key range will bottleneck fewer replay workers, so key distribution matters for replication throughput testing. Multimaster benchmarking is not possible today.
- **Copy-on-write checkpoints.** checkpoint-related throughput dips that are common in PostgreSQL write benchmarks (periodic latency spikes every `checkpoint_timeout` seconds) should be less pronounced or absent with OrioleDB.
- **S3 decoupled storage.** S3 mode benchmarks are single-instance only. The key tuning knobs are `s3_num_workers` (recommended 20 — controls upload parallelism) and `s3_desired_size` (local cache threshold). Undersizing the cache creates eviction pressure and S3 read latency on misses. Interesting measurements: checkpoint-to-S3 sync latency, read performance with varying cache sizes, cold-start recovery time from S3. You cannot currently benchmark shared-storage multi-compute or multiple instances against the same bucket.
- **Bridged indexes.** if the workload relies on non-B-tree index types, those will use PostgreSQL's standard index implementation with heap-like behavior even on an OrioleDB table.

#### PostgreSQL vs OrioleDB — what to expect when comparing them

- **Expect PK-heavy workloads to favor OrioleDB.** Index-organized tables eliminate the heap fetch. Published TPC-C results: 2.3-5.5x faster (beta7, 64-core ARM), 22-162% faster (beta12, varying instance sizes). Largest gains appear at higher core counts where PostgreSQL's buffer mapping becomes a bottleneck.
- **Expect write-heavy workloads to show more stable latency.** No autovacuum pauses, no full page image bursts after checkpoints, no table bloat. WAL IOPS reported 22x lower.
- **Expect secondary index lookups to be comparable or slightly slower.** The extra PK tree traversal (bookmark lookup) adds cost that PostgreSQL's direct CTID fetch avoids.
- **Expect replication lag advantages.** Compact row-level WAL reduces replication bandwidth. Parallel replay on standbys removes PostgreSQL's single-process replay bottleneck. No published replication benchmarks exist yet — this is an open area for testing.
- **Don't compare stock PostgreSQL against OrioleDB.** Tune PostgreSQL properly first (shared_buffers, effective_cache_size, random_page_cost, WAL settings). An untuned PostgreSQL baseline makes OrioleDB look artificially better.
- **Watch for extension maturity issues.** OrioleDB is younger than PostgreSQL's heap engine. Edge cases, crash recovery behavior, and replication compatibility may differ. Benchmark results that seem too good (or too bad) warrant investigation into whether the test exercised a known limitation.
- **Multimaster is not available yet.** Active-active replication via raft consensus is on the roadmap but not implemented. The only multi-node configuration testable today is standard PostgreSQL streaming replication with OrioleDB's enhanced WAL.

## Related hosts

`cloud.stroppy.io` is the hosted control plane for runs; `docs.stroppy.io` mirrors the
documentation this pack condenses; Grafana dashboards alongside a run show the *database's* view
(pg_exporter and friends) while the report shows the *client's*.
