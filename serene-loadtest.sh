#!/usr/bin/env bash
# serene-loadtest.sh - load the live corpus into a symbol-bearing SereneDB and profile the write path.
#
#   sudo ./serene-loadtest.sh                 all 16 shards, one writer, profiled
#   sudo CONCURRENCY=8 ./serene-loadtest.sh   8 writers at once
#   sudo ./serene-loadtest.sh 4               only the first 4 shards
#   ./serene-loadtest.sh --schema-only        create table/index, no load, no root needed
#
# ## What this is for
#
# Every SereneDB number we have was measured THROUGH RAGFlow: a Python task executor, an HTTP hop to
# Ollama for embeddings, and a doc-store insert, all in one figure. Two questions stayed open:
#
#   * the compaction bursts. Steady ~100% CPU with periodic spikes to 876% (8.7 cores), which its own
#     log calls "per-index refresh/compaction loops". Same work as the steady state, or different?
#   * whether write cost grows with index size. Ingest fell 95 -> 35 docs/min overnight while the
#     index grew 24k -> 67k documents, but those points were not measured under equal conditions.
#
# Neither is answerable against the running container: that binary is stripped, so perf resolves every
# frame to a bare address. This drives a RelWithDebInfo build instead.
#
# ## Sharding by id prefix, and why not ORDER BY random()
#
# The first version randomised with `ORDER BY random()` over the whole table. That was wrong in a way
# worth recording: SereneDB must materialise and sort every row before emitting the first one - 4.4 GB
# of text plus ~22 GB of vectors - so the source sat at 16.8 GB RSS and the target received NOTHING
# for as long as it ran. Zero rows/s, indefinitely.
#
# The ids are content hashes, so their first hex character is already uniform: 16 shards of ~174k rows
# each, measured. Splitting on it gives insert order that is random with respect to how the rows were
# originally written, with no sort at all - a filter scan that streams immediately.
#
# It also makes each shard its own COPY, which matters for a second reason: a COPY is ONE transaction,
# so rows are invisible to any other connection until it commits. A single whole-table COPY therefore
# cannot be observed in flight - progress would read 0 throughout and then jump at the end. Per-shard
# commits are what make a throughput curve possible at all.
#
# ## Why the loader is psql and not Python
#
# A Python loader would put an interpreter in the middle of the measurement, and the profile would
# then show the loader mixed into the server. `COPY ... TO STDOUT | COPY ... FROM STDIN` is libpq at
# both ends. Both psql processes run inside the oracle-serenedb container because that image ships a
# real psql and the host has none; the bundled `serened psql` is a DuckDB-style shell that renders box
# tables and ignores -t/-A, so it cannot drive a COPY pipe.
#
# The source is only ever read. Nothing is migrated, upgraded or written back.
set -uo pipefail

SRC_CONTAINER="${SRC_CONTAINER:-oracle-serenedb}"
SRC_HOST=127.0.0.1
SRC_PORT=7890
DST_HOST="${DST_HOST:-172.20.0.1}" # docker gateway, as seen from inside the container
DST_PORT="${DST_PORT:-7891}"
TABLE="${TABLE:-ragflow_a73b470e7d6111f1b22afb6d9f0455fb}"
PGPASS="${PGPASS:-oracle-sdb}"
# Resolve against the INVOKING user's home, not root's. This script needs sudo for perf, and under
# sudo $HOME is /root - so the first two runs wrote every flamegraph, curve and log into
# /root/Projects/oracle/profiles/ where the person who ran it cannot read them.
_owner="${SUDO_USER:-$(id -un)}"
_owner_home="$(getent passwd "$_owner" | cut -d: -f6)"
[ -n "$_owner_home" ] || _owner_home="$HOME"
ORACLE="${ORACLE_DIR:-$_owner_home/Projects/oracle}"

# Writers running at once. 1 measures a single writer; the RAGFlow regime that produced the 876%
# bursts was 8 concurrent task executors, so CONCURRENCY=8 is the comparable setting.
CONCURRENCY="${CONCURRENCY:-1}"
DATA_DIR="${DATA_DIR:-/mnt/data/oracle/serenedb-main-perf}"
# Must fit INSIDE one batch, or the write window spills into the commit/compaction phase and
# stops measuring what it claims to. At depth 2 a batch is ~87k rows, roughly 17s at the measured
# 5.2k rows/s.
PERF_WINDOW="${PERF_WINDOW:-12}"
# Profile every Nth batch. 32 batches x 2 phases would be 64 captures; every 8th gives 4 pairs
# spread across the whole index-size range, which is what the comparison needs.
PERF_EVERY_N="${PERF_EVERY_N:-8}"
# Transient capture. The engine oscillates: 2-3 runnable threads while COPYing, 9-10 during the merge
# that follows, and occasional collapses to a single thread for ~9s. The last is the interesting one
# and no fixed schedule will catch it, so it is trapped on the condition instead.
STALL_THREADS="${STALL_THREADS:-1}" # collapse to this many runnable threads counts as a stall
STALL_MIN="${STALL_MIN:-3}"         # sustained this many seconds before recording
STALL_WINDOW="${STALL_WINDOW:-10}"
STALL_MAX="${STALL_MAX:-6}" # cap, so a long stally run does not write hundreds of files

# Shard granularity. The ids are content hashes, so any hex prefix partitions them uniformly:
# depth 1 -> 16 shards of ~174k rows, depth 2 -> 256 shards of ~10.9k. Depth drives how many points
# the throughput curve has, WITHOUT changing concurrency - which matters because concurrency is
# itself a variable we are holding fixed. At depth 2 and CONCURRENCY=8 the run yields 32 batches of
# ~87k rows, so 32 curve points instead of 2.
SHARD_DEPTH="${SHARD_DEPTH:-2}"
_hex=(0 1 2 3 4 5 6 7 8 9 a b c d e f)
SHARDS=()
if [ "$SHARD_DEPTH" -le 1 ]; then
	SHARDS=("${_hex[@]}")
else
	for _a in "${_hex[@]}"; do for _b in "${_hex[@]}"; do SHARDS+=("$_a$_b"); done; done
fi
SCHEMA_ONLY=0
[ "${1:-}" = "--schema-only" ] && SCHEMA_ONLY=1
# One COPY, one transaction, the whole table. The sharded mode commits every batch, so nothing
# accumulates for long; this deliberately removes every commit boundary to find what grows without
# one - buffers, WAL, whatever the engine holds until it can flush.
SINGLE=0
[ "${1:-}" = "--single" ] && SINGLE=1
# Abort if the server's RSS passes this. The box also runs production RAGFlow and a production
# SereneDB; letting the OOM killer choose a victim would take those down too. 0 disables.
MAX_RSS_GB="${MAX_RSS_GB:-100}"
NSHARDS="${1:-${#SHARDS[@]}}"
case "$NSHARDS" in '' | *[!0-9]*) NSHARDS=${#SHARDS[@]} ;; esac

say() { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*"; }
psql_src() { docker exec -e PGPASSWORD="$PGPASS" "$SRC_CONTAINER" psql -h "$SRC_HOST" -p "$SRC_PORT" -U postgres "$@"; }
psql_dst() { docker exec -e PGPASSWORD="$PGPASS" "$SRC_CONTAINER" psql -h "$DST_HOST" -p "$DST_PORT" -U postgres "$@"; }

psql_src -t -A -c "select 1" >/dev/null 2>&1 || {
	say "source unreachable"
	exit 1
}
psql_dst -t -A -c "select 1" >/dev/null 2>&1 || {
	say "target unreachable"
	exit 1
}
say "source and target reachable"

# Stop the container-side writers on the way out. `docker exec` does not forward signals to the
# process it started without -t, so Ctrl-C on this script kills the host-side wrapper and leaves the
# in-container psql streaming into the target. That is not just untidy: an abandoned COPY client is
# the exact shape that stranded a session and left serened spinning a core for 3.6 hours
# (see ~/Projects/serenedb/IDLE-COPY-SPIN-HANDOFF.md).
#
# pkill -x psql is deliberately blunt - it takes every psql in that container, not only ours. The
# container exists to run this load, so that is the right trade against leaking a writer.
cleanup() {
	docker exec "$SRC_CONTAINER" pkill -x psql 2>/dev/null
	# Defined later, so guard: cleanup is trapped before OUT exists.
	command -v hand_back >/dev/null 2>&1 && hand_back
}
trap 'cleanup; say "interrupted - container-side writers stopped"; exit 130' INT TERM
trap cleanup EXIT

# ── schema, from the source's own information_schema so it cannot drift ─────────────────────────
COLS="$(psql_src -t -A -c "
select string_agg(column_name || ' ' ||
    case when data_type='ARRAY' and udt_name='_float4' then 'FLOAT[1024]'
         when data_type='ARRAY' then 'TEXT[]'
         when data_type='double precision' then 'DOUBLE'
         when data_type='integer' then 'INTEGER'
         when data_type='json' then 'JSON'
         else 'TEXT' end, ', ' order by ordinal_position)
from information_schema.columns where table_name='$TABLE';" 2>/dev/null | tr -d '\r')"
[ -z "$COLS" ] && {
	say "could not read the source schema"
	exit 1
}

psql_dst -q -c "CREATE TABLE IF NOT EXISTS $TABLE ($COLS)" >/dev/null 2>&1
psql_dst -q -c "CREATE TEXT SEARCH DICTIONARY IF NOT EXISTS rf_scored_delim (template = 'delimiter', delimiter = ' ', frequency = true, position = true, norm = true)" >/dev/null 2>&1
# Verbatim from rag/utils/serenedb_conn.py: frequency/position/norm are what make BM25() score at
# all, and the ivf metric/quant pair is what retrieval actually queries. Without this index we would
# be profiling a different system.
psql_dst -q -c "CREATE INDEX IF NOT EXISTS idx_$TABLE ON $TABLE USING inverted (id, title_tks rf_scored_delim, important_tks rf_scored_delim, question_tks rf_scored_delim, content_ltks rf_scored_delim, q_1024_vec_n ivf (metric = 'ip', quant = 'sq8')) WITH (optimize_top_k = 'bm25(1.2, 0.75)')" >/dev/null 2>&1
say "schema ready"
[ "$SCHEMA_ONLY" -eq 1 ] && exit 0

if [ "$(id -u)" -ne 0 ]; then
	say "not root: loading WITHOUT a profile (perf_event_paranoid=$(sysctl -n kernel.perf_event_paranoid 2>/dev/null))"
	PERF=0
else
	PERF=1
fi

# Match on the DATA DIR, not just the binary: more than one instance of this build can be running
# (a stranded specimen is deliberately kept alive on another port), and profiling the wrong pid would
# silently measure a different server.
TARGET_PID="$(pgrep -f "[s]erened .*$(basename "$DATA_DIR")" | head -1)"
[ -z "$TARGET_PID" ] && TARGET_PID="$(pgrep -f '[b]uild_perf/bin/serened' | head -1)"
[ -z "$TARGET_PID" ] && {
	say "the symbol-bearing serened is not running"
	exit 1
}

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${OUT_DIR:-$ORACLE/profiles/serene-load-$STAMP}"
# Fail loudly. profiles/ can end up root-owned from an earlier sudo run, and a silent mkdir failure
# means every log, the curve and the perf data all vanish while the run appears to work.
if ! mkdir -p "$OUT" 2>/dev/null || [ ! -w "$OUT" ]; then
	say "cannot write to $OUT (owner: $(stat -c '%U' "$(dirname "$OUT")" 2>/dev/null))"
	say "either run with sudo, or set OUT_DIR=/some/writable/path"
	exit 1
fi

# ## Hand the output back to the user who ran this
#
# perf needs root, so everything this script writes lands as root:root, and `perf record` creates its
# .data at 0600. That output is unreadable to the person who invoked it - they cannot parse a capture
# while the run is going, and cannot even MOVE the directory afterwards: rename(2) on a directory
# needs write permission on the directory itself, so a root-owned run directory refuses to move even
# within the same filesystem, and `mv` quietly degrades to a recursive copy that then fails on each
# 0600 file. What lands is a half-copied directory with the TSVs and none of the perf data.
#
# There used to be a single `chown -R` at the very end of the script, inside the flamegraph block.
# That is the one place it does no good: the 14-hour single-COPY run was interrupted, the EXIT trap
# fired, the chown never ran, and 643 MB of captures stayed root:root.
#
# So chown on three occasions: now, so the directory is ours from the start; after every capture, so
# it can be parsed while the run is still going; and from `cleanup`, which runs on EVERY exit path.
hand_back() {
	[ -n "${SUDO_USER:-}" ] && chown -R "$_owner" "$OUT" 2>/dev/null
	return 0
}
hand_back

# Symbols and flamegraphs for every capture. This used to be inline at the bottom of the sharded
# path, which meant --single never reached it: that branch exits as soon as the COPY finishes. So a
# fourteen-hour single-COPY run produced .data files and nothing readable beside them - and, since
# the chown lived in the same block, left them root-owned as well. Both paths call this now.
postprocess() {
	[ "$PERF" -eq 1 ] || return 0
	local COLLAPSE FLAME d b
	COLLAPSE="$(command -v inferno-collapse-perf || echo "$_owner_home/.cargo/bin/inferno-collapse-perf")"
	FLAME="$(command -v inferno-flamegraph || echo "$_owner_home/.cargo/bin/inferno-flamegraph")"
	for d in "$OUT"/write-*.data "$OUT"/compact-*.data "$OUT"/stall*.data "$OUT"/single-fp.data; do
		[ -s "$d" ] || continue
		b="$(basename "$d" .data)"
		# read-perf.sh aggregates across both PMUs. A plain `perf report | head` on this hybrid CPU
		# reads the E-core table only, and understated one symbol as 35.60% when it was 93.39%.
		if [ -x "$ORACLE/read-perf.sh" ]; then
			"$ORACLE/read-perf.sh" "$d" 2>/dev/null | tail -n +3 >"$OUT/$b.symbols.txt"
		else
			perf report -i "$d" --stdio --sort symbol --no-children -g none 2>/dev/null |
				grep -E "^ +[0-9]" | head -25 >"$OUT/$b.symbols.txt"
		fi
		if [ -x "$COLLAPSE" ] || command -v "$COLLAPSE" >/dev/null 2>&1; then
			perf script -i "$d" 2>/dev/null | "$COLLAPSE" 2>/dev/null |
				"$FLAME" --title "serened main@3b8983e9 $b" >"$OUT/$b.svg" 2>/dev/null
		fi
		hand_back
	done
}

say "target pid $TARGET_PID, concurrency $CONCURRENCY, $NSHARDS shard(s) -> $OUT"

# True CPU percentage over an interval, from /proc/<pid>/stat deltas. `ps -o pcpu` reports the
# average over the process's whole LIFETIME, which in the previous version produced a column that
# crept 0.3 -> 2.4% and meant nothing.
cpu_snapshot() { awk '{print $14+$15}' "/proc/$TARGET_PID/stat" 2>/dev/null || echo 0; }
HZ="$(getconf CLK_TCK 2>/dev/null || echo 100)"

disk_now() { du -sm "$DATA_DIR" 2>/dev/null | cut -f1; }

load_shard() {
	local s="$1"
	docker exec -e PGPASSWORD="$PGPASS" "$SRC_CONTAINER" bash -c "
      psql -h $SRC_HOST -p $SRC_PORT -U postgres -c \"COPY (SELECT * FROM $TABLE WHERE substr(id,1,${#s})='$s') TO STDOUT\" |
      psql -h $DST_HOST -p $DST_PORT -U postgres -c \"COPY $TABLE FROM STDIN\"
    " >>"$OUT/copy-$s.log" 2>&1
}

loaded=0
batch_no=0
# 1s sampler, for the whole load, in the background.
#
# The per-batch curve samples every ~17s and that is coarse enough to alias away the thing worth
# seeing: throughput oscillates on a ~15s period (2-3 threads at ~300% while COPYing, 9-10 threads at
# ~1100% during the merge that follows), with occasional single-threaded phases of ~9s. A per-batch
# number averages all of that into one figure and shows none of it.
#
# CPU, running-thread count and RSS all come from /proc and cost nothing. Disk deliberately does NOT
# appear here: du -sm over a 55 GB directory stats every file, and doing that once a second would
# perturb the very thing being measured. It stays on the per-batch curve.
sample_1s() {
	local prev cur cpu running rss
	local stall_run=0 stalls=0
	prev="$(cpu_snapshot)"
	printf 'elapsed_s\tcpu_pct\trunning_threads\trss_gb\n' >"$OUT/timeline.tsv"
	local t=0
	while :; do
		sleep 1
		t=$((t + 1))
		cur="$(cpu_snapshot)"
		cpu=$(awk -v a="$prev" -v b="$cur" -v hz="$HZ" 'BEGIN{printf "%.0f", (b-a)/hz*100}')
		prev="$cur"
		# Threads currently in R. Cheaper than a per-thread utime delta and enough to show the
		# collapse to one runnable thread that the batch average hides.
		running=$(grep -lc '^[0-9]* ([^)]*) R' /proc/"$TARGET_PID"/task/*/stat 2>/dev/null | wc -l)
		rss=$(awk '/VmRSS/{printf "%.1f", $2/1048576}' /proc/"$TARGET_PID"/status 2>/dev/null)
		printf '%s\t%s\t%s\t%s\n' "$t" "$cpu" "$running" "${rss:-0}" >>"$OUT/timeline.tsv"

		# Catch the transient. A fixed-schedule perf window cannot sample a phase that lasts nine
		# seconds and arrives unpredictably - by construction it either misses it or averages it
		# with everything else. So the window is triggered BY the condition instead: when the
		# engine collapses to STALL_THREADS runnable threads for STALL_MIN consecutive seconds,
		# start recording immediately and label the file with when it happened.
		if [ "$PERF" -eq 1 ] && [ "${running:-0}" -le "$STALL_THREADS" ]; then
			stall_run=$((stall_run + 1))
			if [ "$stall_run" -eq "$STALL_MIN" ] && [ "$stalls" -lt "$STALL_MAX" ]; then
				stalls=$((stalls + 1))
				perf record -F 199 -g --call-graph fp -p "$TARGET_PID" \
					-o "$OUT/stall${stalls}-at-${t}s.data" -- sleep "$STALL_WINDOW" \
					>/dev/null 2>&1 &
				say "  [stall $stalls] ${running} runnable thread(s) at ${t}s, cpu ${cpu}% - recording ${STALL_WINDOW}s"
			fi
		else
			stall_run=0
		fi
	done
}
sample_1s &
SAMPLER_PID=$!
# shellcheck disable=SC2064  # expand SAMPLER_PID now, not at trap time
trap "kill $SAMPLER_PID 2>/dev/null; cleanup; exit 130" INT TERM
# shellcheck disable=SC2064
trap "kill $SAMPLER_PID 2>/dev/null; cleanup" EXIT

printf 'shard\tbatch_rows\ttotal_rows\tseconds\trows_per_s\tcpu_pct\tdisk_mb\n' >"$OUT/curve.tsv"
say "$(printf '%-7s %10s %8s %9s %8s %8s' shard rows secs rows/s cpu% disk_mb)"

if [ "$SINGLE" -eq 1 ]; then
	say "SINGLE mode: one COPY, one transaction, entire table - no commit boundaries"
	free_gb="$(free -g | awk 'NR==2{print $7}')"
	say "  available memory before: ${free_gb} GB, abort threshold ${MAX_RSS_GB} GB RSS"
	printf 'elapsed_s\trss_gb\tdisk_mb\tcpu_pct\tavail_gb\n' >"$OUT/single.tsv"

	docker exec -e PGPASSWORD="$PGPASS" "$SRC_CONTAINER" bash -c "
      psql -h $SRC_HOST -p $SRC_PORT -U postgres -c \"COPY (SELECT * FROM $TABLE) TO STDOUT\" |
      psql -h $DST_HOST -p $DST_PORT -U postgres -c \"COPY $TABLE FROM STDIN\"
    " >"$OUT/copy-single.log" 2>&1 &
	COPY_PID=$!

	if [ "$PERF" -eq 1 ]; then
		perf record -F 99 -g --call-graph fp -p "$TARGET_PID" -o "$OUT/single-fp.data" >/dev/null 2>&1 &
		FP_PID=$!
	fi

	t0=$(date +%s)
	prev_c="$(cpu_snapshot)"
	prev_t=$t0
	while kill -0 "$COPY_PID" 2>/dev/null; do
		sleep 5
		now=$(date +%s)
		rss_kb="$(awk '/VmRSS/{print $2}' "/proc/$TARGET_PID/status" 2>/dev/null)"
		rss_gb=$(((${rss_kb:-0}) / 1048576))
		disk="$(disk_now)"
		avail="$(free -g | awk 'NR==2{print $7}')"
		c="$(cpu_snapshot)"
		dt=$((now - prev_t))
		[ "$dt" -lt 1 ] && dt=1
		cpu=$(awk -v a="$prev_c" -v b="$c" -v s="$dt" -v hz="$HZ" 'BEGIN{printf "%.0f", (b-a)/hz/s*100}')
		prev_c="$c"
		prev_t="$now"
		printf '%s\t%s\t%s\t%s\t%s\n' "$((now - t0))" "$rss_gb" "$disk" "$cpu" "$avail" >>"$OUT/single.tsv"
		say "  $((now - t0))s  rss ${rss_gb}GB  disk ${disk}MB  cpu ${cpu}%  avail ${avail}GB"
		if [ "$MAX_RSS_GB" -gt 0 ] && [ "$rss_gb" -ge "$MAX_RSS_GB" ]; then
			say "  RSS ${rss_gb}GB hit the ${MAX_RSS_GB}GB limit - aborting before the OOM killer picks a victim"
			cleanup
			break
		fi
	done
	wait "$COPY_PID" 2>/dev/null
	[ "$PERF" -eq 1 ] && [ -n "${FP_PID:-}" ] && kill -INT "$FP_PID" 2>/dev/null && wait "$FP_PID" 2>/dev/null
	hand_back
	say "single COPY finished after $(($(date +%s) - t0))s"
	tail -2 "$OUT/copy-single.log" 2>/dev/null
	say "curve: $OUT/single.tsv"
	postprocess
	hand_back
	say "output in $OUT"
	exit 0
fi

i=0
while [ "$i" -lt "$NSHARDS" ]; do
	batch=()
	for _ in $(seq 1 "$CONCURRENCY"); do
		[ "$i" -ge "$NSHARDS" ] && break
		batch+=("${SHARDS[$i]}")
		i=$((i + 1))
	done
	[ "${#batch[@]}" -eq 0 ] && break
	batch_no=$((batch_no + 1))

	c0="$(cpu_snapshot)"
	t0=$(date +%s)

	# fp, not dwarf: the build is -O3 with -fno-omit-frame-pointer AND
	# -mno-omit-leaf-frame-pointer (the project's own `perf` preset), so frame-pointer stacks are
	# correct here, cost no stack copies per sample, and stay small enough for one recording each.
	if [ "$PERF" -eq 1 ] && [ $((batch_no % PERF_EVERY_N)) -eq 0 ]; then
		perf record -F 99 -g --call-graph fp -p "$TARGET_PID" \
			-o "$OUT/write-${batch[0]}-at-${loaded}rows.data" -- sleep "$PERF_WINDOW" >/dev/null 2>&1 &
	fi

	# Wait for THESE shards only. A bare `wait` waits for every background job, which includes the
	# 1s sampler - an infinite loop - so the first batch would hang the run forever. That is exactly
	# what happened the first time the sampler was added.
	shard_pids=()
	for s in "${batch[@]}"; do
		load_shard "$s" &
		shard_pids+=($!)
	done
	wait "${shard_pids[@]}"

	t1=$(date +%s)
	c1="$(cpu_snapshot)"

	# Rows come from psql's own "COPY <n>" line, not from a count(*) query. Every probe during a
	# load is another client session, and it was exactly such a probe - a count(*) behind a wedged
	# docker exec - that stranded a session and left the server spinning a core. The load must run
	# with no connections open other than the COPY writers themselves.
	batch_rows=0
	for s2 in "${batch[@]}"; do
		n="$(grep -oE '^COPY [0-9]+' "$OUT/copy-$s2.log" 2>/dev/null | tail -1 | awk '{print $2}')"
		batch_rows=$((batch_rows + ${n:-0}))
	done
	loaded=$((loaded + batch_rows))

	# THE BURST IS HERE, not during the write. Observed: serened sits at ~171% while a shard is
	# being COPYed, then jumps to 780-910% (nine cores, 103 threads) AFTER the transaction commits,
	# with the data directory size flat - so it is CPU-bound index maintenance, not segment
	# rewriting. Profiling only during the load, as the first version did, would have measured the
	# quiet half and missed the entire phenomenon this experiment exists to explain.
	#
	# Taken after the row count is read, so the file is labelled with the index size it profiled.
	if [ "$PERF" -eq 1 ] && [ $((batch_no % PERF_EVERY_N)) -eq 0 ]; then
		perf record -F 99 -g --call-graph fp -p "$TARGET_PID" \
			-o "$OUT/compact-${batch[0]}-at-${loaded}rows.data" -- sleep "$PERF_WINDOW" >/dev/null 2>&1
		cpu_after=$(top -bn1 -p "$TARGET_PID" 2>/dev/null | tail -1 | awk '{print $9}')
		say "  compaction window captured (serened at ${cpu_after:-?}%)"
	fi
	# Per batch, so captures are parseable while the run is still going rather than only at the end.
	hand_back
	secs=$((t1 - t0))
	[ "$secs" -lt 1 ] && secs=1
	cpu=$(awk -v a="$c0" -v b="$c1" -v s="$secs" -v hz="$HZ" 'BEGIN{printf "%.0f", (b-a)/hz/s*100}')
	rate=$((batch_rows / secs))
	disk="$(disk_now)"

	printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$(
		IFS=,
		echo "${batch[*]}"
	)" "$batch_rows" "$loaded" "$secs" "$rate" "$cpu" "$disk" \
		>>"$OUT/curve.tsv"
	say "$(printf '%-10s %10s %8s %9s %8s %8s' "${batch[0]}..${batch[-1]}" "$loaded" "$secs" "$rate" "$cpu" "$disk")"
done

# Stop the sampler before anything else waits. A bare `wait` here would block on it forever, since
# it is an infinite loop - the same trap as inside the batch loop.
kill "$SAMPLER_PID" 2>/dev/null
say "load done"

postprocess
hand_back

say "throughput curve:"
column -t "$OUT/curve.tsv" 2>/dev/null | sed 's/^/    /'
say "output in $OUT"
