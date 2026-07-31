#!/usr/bin/env bash
# overnight.sh - wait for the workers sweep, apply its result, restart ingestion, then build serene.
#
#   ./overnight.sh              run it (intended detached, see below)
#   ./overnight.sh --no-build   apply the sweep and restart ingestion, skip the serenedb build
#
# Launched detached so it survives the terminal closing:
#   systemd-run --user --unit=oracle-overnight --working-directory="$HOME/Projects/oracle" \
#       "$HOME/Projects/oracle/overnight.sh"
#
# Read it in the morning with:
#   cat ~/Projects/oracle/overnight.log
#   journalctl --user -u oracle-overnight -o cat
#
# ## Order, and why this one
#
# His: apply the sweep result, restart ingestion, and only then build serenedb. Ingestion first
# because the box should be doing useful work while a 24-core C++ build hogs it, and because a build
# that fails should not also mean nothing ingested overnight.
#
# ## What it will not do
#
#   * touch the running SereneDB instance. It stays on 26.07.4. The build is a separate worktree and
#     a separate binary; nothing is upgraded, migrated or restarted.
#   * pick a worker count from a window the sweep itself flagged. A row carrying a note measured a
#     drained queue or made no progress, and is discarded rather than averaged in.
#   * let a build failure stop ingestion. The build runs last and its exit status is logged, not acted
#     on.
set -uo pipefail

ORACLE="${ORACLE_DIR:-$HOME/Projects/oracle}"
COMPOSE="$ORACLE/ragflow/docker/docker-compose.yml"
SWEEP_OUT="${SWEEP_OUT:-/tmp/claude-1000/-home-dead-Projects-oracle/a22a06f3-8d19-4776-9cea-9d524a109758/scratchpad/sweep2.txt}"
LOG="$ORACLE/overnight.log"
SERENE="$HOME/Projects/serenedb/serenedb"
WORKTREE="$HOME/Projects/serenedb/serenedb-main"
BUILD=1
[ "${1:-}" = "--no-build" ] && BUILD=0

# The count to fall back to. 8 is what is configured now and is known to work; the sweep exists to
# find something better, not to be trusted blindly when its output is unreadable.
FALLBACK_WORKERS=8

cd "$ORACLE" || exit 1
exec >>"$LOG" 2>&1

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

say "=============================================================="
say "overnight run starting"

# ── 1. wait for the sweep ───────────────────────────────────────────────────────────────────────
waited=0
while pgrep -f "workers-sweep.sh" >/dev/null; do
	sleep 30
	waited=$((waited + 30))
	if [ "$waited" -gt 7200 ]; then
		say "sweep still running after 2h - proceeding with the fallback rather than waiting further"
		break
	fi
done
say "sweep finished (waited ${waited}s)"

# ── 2. read its result ──────────────────────────────────────────────────────────────────────────
# Rows look like:  "4         57          41"  with an optional trailing note. Take the highest
# docs/min among rows with NO note; a noted row measured something other than throughput.
best_n=""
best_rate=-1
if [ -r "$SWEEP_OUT" ]; then
	while read -r n rate _rest; do
		case "$n" in '' | *[!0-9]*) continue ;; esac
		case "$rate" in '' | *[!0-9]*) continue ;; esac
		if [ "$rate" -gt "$best_rate" ]; then
			best_rate="$rate"
			best_n="$n"
		fi
	done < <(tr '\r' '\n' <"$SWEEP_OUT" |
		awk 'NF==3 && $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ {print $1, $2, $3}')
fi

say "sweep table:"
tr '\r' '\n' <"$SWEEP_OUT" 2>/dev/null | grep -E "^[[:space:]]*[0-9]+[[:space:]]" | sed 's/^/    /'

if [ -z "$best_n" ] || [ "$best_rate" -le 0 ]; then
	say "no usable rows - falling back to --workers=$FALLBACK_WORKERS"
	best_n="$FALLBACK_WORKERS"
else
	say "winner: --workers=$best_n at $best_rate docs/min"
fi

# ── 3. apply it ─────────────────────────────────────────────────────────────────────────────────
current="$(grep -oE '^\s*-\s*--workers=[0-9]+' "$COMPOSE" | grep -oE '[0-9]+' | head -1)"
if [ "$current" = "$best_n" ]; then
	say "compose already at --workers=$best_n, no recreate needed"
else
	say "setting --workers=$best_n (was $current)"
	sed -i -E "s|^([[:space:]]*-[[:space:]]*)--workers=[0-9]+|\1--workers=$best_n|" "$COMPOSE"
	docker compose -p docker --project-directory "$ORACLE/ragflow/docker" \
		-f "$COMPOSE" up -d ragflow-cpu
	sleep 20
	# A recreate SIGKILLs the executors, so anything mid-parse is orphaned in RUNNING with no
	# terminal status. Left alone it sits there forever and the queue never drains.
	say "re-queueing anything orphaned by the recreate"
	python3 requeue-orphans.py --stale-minutes 1
fi

# ── 4. ingestion back on ────────────────────────────────────────────────────────────────────────
systemctl --user start oracle-arxiv-ingest.service
sleep 5
say "feeder: $(systemctl --user is-active oracle-arxiv-ingest.service)"

# ── 5. his tooling back, since it was evicted for the OCR run ───────────────────────────────────
for u in oracle-qwen-next.service oracle-qwen-vl.service; do
	systemctl --user start "$u" 2>/dev/null
	say "$u: $(systemctl --user is-active "$u")"
done

# ── 6. serenedb main, in a worktree ─────────────────────────────────────────────────────────────
if [ "$BUILD" -eq 0 ]; then
	say "build skipped (--no-build)"
	say "overnight run done"
	exit 0
fi

while pgrep -f "git fetch" >/dev/null; do
	say "waiting for the serenedb fetch to finish"
	sleep 60
done

if ! git -C "$SERENE" rev-parse origin/main >/dev/null 2>&1; then
	say "no origin/main in $SERENE - skipping the build"
	say "overnight run done"
	exit 0
fi

head_sha="$(git -C "$SERENE" rev-parse --short origin/main)"
say "building serenedb main ($head_sha) in $WORKTREE - the RUNNING instance is untouched"

if [ ! -d "$WORKTREE" ]; then
	git -C "$SERENE" worktree add --detach "$WORKTREE" origin/main || {
		say "worktree add failed - skipping the build"
		exit 0
	}
fi

# Submodules are the bulk of this and the most likely thing to fail; keep it separate so the log
# says which step died.
say "initialising submodules"
git -C "$WORKTREE" submodule update --init --recursive --jobs 4 || say "submodule init reported errors"

# RelWithDebInfo, because the point is a profile with symbols: -O2 like the release plus -g, rather
# than a debug build whose profile would describe different code.
say "configuring"
cmake -S "$WORKTREE" -B "$WORKTREE/build" \
	-DCMAKE_BUILD_TYPE=RelWithDebInfo \
	-DCMAKE_EXPORT_COMPILE_COMMANDS=ON || {
	say "cmake configure FAILED - see above"
	exit 0
}

# Leave a core free. The ingest is running and the thermal gate throttles refills, not this.
jobs="$(($(nproc) - 2))"
[ "$jobs" -lt 1 ] && jobs=1
say "building with $jobs jobs (this is the long part)"
if cmake --build "$WORKTREE/build" -j "$jobs"; then
	say "build OK"
	find "$WORKTREE/build" -maxdepth 2 -name serened -type f -exec ls -la {} \; 2>/dev/null
else
	say "build FAILED - ingestion is unaffected"
fi

say "overnight run done"
