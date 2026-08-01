#!/usr/bin/env bash
# consolidate-profiles.sh - gather every artefact of the SereneDB load work into one place.
#
#   ./consolidate-profiles.sh              move what can be moved, report what needs root
#   sudo ./consolidate-profiles.sh         also move the root-owned captures
#   ./consolidate-profiles.sh --index      rebuild PROFILES.md only, move nothing
#   DEST=/path ./consolidate-profiles.sh   somewhere else
#
# ## Why
#
# The evidence for four upstream findings ended up spread across four places: run directories under
# the oracle repo, ad-hoc `perf record` captures in /tmp, watcher logs in a third place, and the
# server's own logs on the data volume. Any one of them alone proves nothing. They belong next to the
# source they describe, in the serenedb worktree, with an index that says what each file is.
#
# ## The layout, and why it is split by experiment
#
#   single-copy/   the one-statement COPY: 2.79M rows, ~14 hours, 83 GB buffered
#   parallel-8/    the same 2.79M rows as 256 shards, 8 concurrent: 603 seconds
#   earlier/       the exploratory runs that led to those two
#   logs/          server launch logs, docker journal, watchers
#
# The two headline runs are the same data into the same schema with the same index, differing only
# in how the client issued it. Keeping them in sibling directories is the point: the comparison IS
# the finding, and a flat pile of nine timestamped directories buries it.
#
# ## Two things make this less trivial than a `mv`
#
# The load harness ran under sudo, so its captures are owned by root with mode 0600 - unreadable to
# the user who needs to parse them. This script chowns them back to the invoking user when it can.
#
# And the per-run TSVs are the only record of the 1-second sampler that ran alongside every load:
# elapsed / cpu / runnable threads / RSS / disk / MemAvailable. The perf captures say where the
# cycles went, the TSVs say what the machine was doing at the time, and the two are only useful
# together - the TSVs are what showed the load going single-threaded, the buffer growing at
# 0.44 GB/s, and the disk sitting at 1 MB for 77 minutes. They are indexed explicitly so nobody
# clears them out as scratch output.
#
# NOTHING here touches a database directory. Perf samples, logs and TSVs only.
set -uo pipefail

REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
DEST="${DEST:-$REAL_HOME/Projects/serenedb/profiles}"
SRC_PROFILES="${SRC_PROFILES:-$REAL_HOME/Projects/oracle/profiles}"
ORACLE="${ORACLE:-$REAL_HOME/Projects/oracle}"
DATA_LOGS="${DATA_LOGS:-/mnt/data/oracle}"
READ_PERF="$ORACLE/read-perf.sh"

# The two headline runs, by harness timestamp. Everything else is exploratory.
SINGLE_RUN="serene-load-20260731-200043"
PARALLEL_RUN="serene-load-20260731-193135"
# Captures taken by hand during the single COPY, between 20:37 and 09:42 the next morning.
PHASE_RUN="phase-change-20260731-204238"

INDEX_ONLY=0
[ "${1:-}" = "--index" ] && INDEX_ONLY=1
needs_root=0

say() { printf '  %s\n' "$*"; }
hdr() { printf '\n\033[1m%s\033[0m\n' "$*"; }

mkdir -p "$DEST"/{single-copy,parallel-8,earlier,logs} || {
	echo "cannot create $DEST" >&2
	exit 1
}

# Anything created while running under sudo would otherwise be root-owned and unreadable to the user
# who has to parse it - which is exactly the problem this script exists to clean up.
fix_owner() {
	[ -n "${SUDO_USER:-}" ] && chown -R "$REAL_USER" "$1" 2>/dev/null
	return 0
}

take() { # take SRC DESTDIR
	# ## Why this checks ownership before moving anything
	#
	# The load harness ran under sudo, so its run directories are root:root and the captures inside
	# are 0600. Moving one as the ordinary user fails in a way that is worse than not trying:
	#
	#   rename(2) on a directory has to rewrite that directory's ".." entry, so it needs WRITE on
	#   the directory itself - not just on the two parents. `drwxr-xr-x root root` denies that even
	#   on the same filesystem, so `mv` silently degrades to a recursive copy, which then hits
	#   Permission denied on each 0600 capture. What lands is a partial directory holding the
	#   world-readable TSVs and none of the perf data, while the source stays put and gets picked
	#   up again by the next pass. That is how one run ended up half-copied into two places.
	#
	# So: refuse up front and say what to run, rather than half-doing it.
	local src="$1" dst="$2"
	[ -e "$src" ] || return 0
	if [ -z "${SUDO_USER:-}" ] && [ ! -O "$src" ]; then
		say "NEEDS ROOT  $src  (owned by $(stat -c %U "$src"))"
		needs_root=1
		return 0
	fi
	mkdir -p "$dst"
	mv "$src" "$dst/" 2>/dev/null || cp -a "$src" "$dst/" 2>/dev/null || {
		say "FAILED      $src"
		needs_root=1
		return 0
	}
	fix_owner "$dst/$(basename "$src")"
	say "  -> $(basename "$dst")/$(basename "$src")"
}

if [ "$INDEX_ONLY" -eq 0 ]; then
	hdr "SINGLE COPY  (one statement, 2.79M rows, ~14 hours)"
	take "$SRC_PROFILES/$SINGLE_RUN" "$DEST/single-copy"
	take "$DEST/$SINGLE_RUN" "$DEST/single-copy"
	take "$SRC_PROFILES/$PHASE_RUN" "$DEST/single-copy"
	take "$DEST/$PHASE_RUN" "$DEST/single-copy"
	# The commit-now series: ten hand-taken captures spanning the whole 14 hours, from the first
	# buffering minutes to the index build in the final twelve. They only mean anything as a series.
	for f in /tmp/commit-now*.data "$DEST"/manual-captures/commit-now*.data; do
		[ -e "$f" ] || continue
		take "$f" "$DEST/single-copy/commit-now"
	done
	# The watcher that ran unattended through the night belongs with the run it watched.
	for f in "$SRC_PROFILES"/watch-load-*.log; do
		[ -f "$f" ] || continue
		cp -a "$f" "$DEST/single-copy/" 2>/dev/null && say "  -> single-copy/$(basename "$f")"
	done

	hdr "PARALLEL 8  (256 shards, 8 concurrent, the same 2.79M rows, 603 seconds)"
	take "$SRC_PROFILES/$PARALLEL_RUN" "$DEST/parallel-8"
	take "$DEST/$PARALLEL_RUN" "$DEST/parallel-8"

	hdr "EARLIER  (exploratory runs)"
	for d in "$SRC_PROFILES"/serene-load-* "$SRC_PROFILES"/phase-change-* "$SRC_PROFILES"/2026* \
		"$DEST"/serene-load-* "$DEST"/phase-change-* "$DEST"/2026*; do
		[ -d "$d" ] || continue
		# A headline run that failed to move above is still sitting in the source directory. Without
		# this it gets picked up here and lands in earlier/ as well - the duplicate that happened.
		case "$(basename "$d")" in
		"$SINGLE_RUN" | "$PARALLEL_RUN" | "$PHASE_RUN") continue ;;
		esac
		take "$d" "$DEST/earlier"
	done
	for f in /tmp/spin*.data "$DEST"/manual-captures/*.data; do
		[ -e "$f" ] || continue
		take "$f" "$DEST/earlier/manual-captures"
	done
	rmdir "$DEST/manual-captures" 2>/dev/null

	hdr "LOGS"
	for f in "$SRC_PROFILES"/*.log "$ORACLE"/overnight.log; do
		[ -f "$f" ] || continue
		cp -a "$f" "$DEST/logs/" 2>/dev/null && say "  -> logs/$(basename "$f")"
	done
	# The server's own launch logs live on the data volume, not in either repo.
	for f in "$DATA_LOGS"/*.log; do
		[ -f "$f" ] || continue
		cp -a "$f" "$DEST/logs/" 2>/dev/null && say "  -> logs/$(basename "$f")"
	done
	# The containerised instance keeps its log in the docker journal and nowhere else.
	if command -v docker >/dev/null 2>&1; then
		while read -r c; do
			[ -n "$c" ] || continue
			docker logs "$c" >"$DEST/logs/docker-$c.log" 2>&1 &&
				say "  -> logs/docker-$c.log ($(wc -l <"$DEST/logs/docker-$c.log") lines)"
		done < <(docker ps -a --filter 'name=serene' --format '{{.Names}}' 2>/dev/null)
	fi
	fix_owner "$DEST"
fi

# ---------------------------------------------------------------------------------------------
# Index
#
# Cycle-weighted top symbol per capture, via read-perf.sh. Not `perf report | head`: this box is a
# hybrid CPU, perf emits one symbol table per PMU, and reading the first one reported 35.60% for a
# symbol that was 93.39% on the cores that mattered.
# ---------------------------------------------------------------------------------------------
hdr "INDEX"
IDX="$DEST/PROFILES.md"

# shellcheck disable=SC2016  # the backticks in the printf formats below are markdown, not command
# substitution - these functions emit a markdown table where file paths are code-spanned.
section_captures() { # section_captures DIR TITLE
	local dir="$1"
	[ -d "$dir" ] || return 0
	find "$dir" -name '*.data' -size +100k 2>/dev/null | sort | while IFS= read -r f; do
		[ -r "$f" ] || {
			printf '| `%s` | %s | needs root to read |\n' "${f#"$DEST"/}" "$(du -h "$f" | cut -f1)"
			continue
		}
		top="unreadable"
		[ -x "$READ_PERF" ] && top="$("$READ_PERF" "$f" 2>/dev/null | sed -n '3p' | sed 's/^ *//' | cut -c1-70)"
		printf '| `%s` | %s | %s |\n' "${f#"$DEST"/}" "$(du -h "$f" | cut -f1)" "${top:-no symbols}"
	done
}

# shellcheck disable=SC2016  # markdown backticks, as above
section_tsv() {
	local dir="$1"
	[ -d "$dir" ] || return 0
	find "$dir" -name '*.tsv' 2>/dev/null | sort | while IFS= read -r f; do
		rows=$(($(wc -l <"$f") - 1))
		span="$(awk -F'\t' 'NR>1 && $1 ~ /^[0-9]+$/ {last=$1} END{print (last==""?"-":last"s")}' "$f")"
		printf '| `%s` | %s | %s | %s |\n' "${f#"$DEST"/}" "$rows" "$span" "$(head -1 "$f" | tr '\t' ' ')"
	done
}

{
	cat <<'EOF'
# SereneDB load-test artefacts

Everything measured on `main` (`3b8983e9`), RelWithDebInfo via the `perf` preset, on an Intel Core
Ultra 9 275HX with 128 GB RAM and 192 GB swap. Regenerate this file with
`oracle/consolidate-profiles.sh --index`.

Percentages are **cycle-weighted across both PMUs**. A hybrid CPU records `cpu_core/cycles` and
`cpu_atom/cycles` separately and `perf report` prints a full symbol table for each; reading only the
first gives the E-core view, which is the minority of the cycles. One capture here reads 35.60% for
a symbol that is 93.39% on the cores that matter. Reparse with `oracle/read-perf.sh`, never with
`perf report | head`.

## The two runs

Same 2,791,123 rows, same schema, same 47 columns with two 1024-float vectors each, same target
instance with the same index. The only difference is how the client issued them.

| | statements | elapsed | peak committed memory | outcome |
|---|---|---|---|---|
| `single-copy/` | 1 | ~14 hours | 83 GB (85 GB into swap) | correct, ~90x slower |
| `parallel-8/` | 256, 8 concurrent | 603 seconds | ~5 GB | correct |

The comparison is the finding. See `serenedb/COPY-UNBOUNDED-RECV-BUFFER-HANDOFF.md` in the
hands-off repo.

## single-copy - one statement, ~14 hours

Phases, measured end to end: buffer the whole wire stream (83 GB, zero bytes written, 77 minutes)
-> parse at ~3% efficiency (~13.5 h, ~97% of cycles in the feeder spin) -> columnar write (bursts,
disk 3 -> 40 GB) -> index build (final ~12 min) -> commit.

`commit-now/` is ten captures taken by hand across the whole run, from the first buffering minutes
to the index build at the end. They only mean anything as a series: the feeder spin is 74-98% in
the nine taken during the load and 4.8% in the last one, which is the cleanest evidence that the
spin is confined to the feeder polling for `CopyData`.

| capture | size | top symbol (cycle-weighted) |
|---|---|---|
EOF
} >"$IDX"
# shellcheck disable=SC2129  # each section is appended separately on purpose: the heredocs above and
# these calls interleave, and a single redirect block would put the tables in the wrong order.
section_captures "$DEST/single-copy" >>"$IDX"

{
	cat <<'EOF'

## parallel-8 - 256 shards, 8 concurrent, 603 seconds

Write throughput is flat across a 32x growth in index size: 4,590-5,470 rows/s per batch, mean
5,033. `curve.tsv` is the per-batch record of that. The `compact-*` and `write-*` captures are
condition-triggered - taken when the harness saw the phase change, not on a fixed schedule.

| capture | size | top symbol (cycle-weighted) |
|---|---|---|
EOF
} >>"$IDX"
section_captures "$DEST/parallel-8" >>"$IDX"

{
	cat <<'EOF'

## earlier - exploratory runs

The runs that led to the two above: single-shard, 2-way, 4-way, 16-shard and 256-shard-at-1x
loads, plus the first ingest profile of the RAGFlow path.

| capture | size | top symbol (cycle-weighted) |
|---|---|---|
EOF
} >>"$IDX"
section_captures "$DEST/earlier" >>"$IDX"

{
	cat <<'EOF'

## The 1-second sampler

The harness sampled once per second for the whole of every run, alongside the perf captures. This
is the only record of what the machine was doing at the moment each capture was taken, and it is
what showed the single COPY going single-threaded, the buffer growing at 0.44 GB/s, and the disk
sitting at 1 MB for 77 minutes while RSS climbed.

Columns vary by harness version but are always headed. `timeline.tsv` is cpu / runnable threads /
RSS; `single.tsv` adds disk and system MemAvailable; `progress.tsv` and `curve.tsv` are per-batch
rather than per-second.

| file | rows | span | columns |
|---|---|---|---|
EOF
} >>"$IDX"
section_tsv "$DEST"

{
	cat <<'EOF'

## Logs

| file | size | what |
|---|---|---|
EOF
} >>"$IDX"
# shellcheck disable=SC2016  # markdown backticks, as above
find "$DEST" -name '*.log' 2>/dev/null | sort | while IFS= read -r f; do
	case "$(basename "$f")" in
	watch-load-*) what="unattended watcher through the night of the single COPY" ;;
	overnight.log) what="arXiv ingestion via RAGFlow, overnight" ;;
	serene-clean.log) what="serened launch, the clean instance on port 7892" ;;
	serenedb-main-perf.log) what="serened launch, perf-preset build at main 3b8983e9" ;;
	docker-*) what="containerised instance, docker journal" ;;
	copy-single.log) what="psql client output of the single COPY - the final row count" ;;
	copy*.log) what="psql client output, per-shard COPY row counts" ;;
	*) what="-" ;;
	esac
	printf '| `%s` | %s | %s |\n' "${f#"$DEST"/}" "$(du -h "$f" | cut -f1)" "$what"
done >>"$IDX"

fix_owner "$IDX"
say "wrote $IDX"

# Captures written by a harness run under sudo are root:root 0600. They relocate fine but cannot be
# parsed until they are chowned, and an unparseable capture is a capture that will be deleted by
# whoever tidies up next.
unreadable="$(find "$DEST" -name '*.data' ! -readable 2>/dev/null | wc -l)"
if [ "$unreadable" -gt 0 ] || [ "$needs_root" -eq 1 ]; then
	hdr "NEEDS ROOT"
	say "$unreadable captures are root-owned 0600 and cannot be parsed. Fix with:"
	say "  sudo $0"
	say "or just: sudo chown -R $REAL_USER $DEST"
fi
