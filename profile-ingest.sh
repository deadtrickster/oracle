#!/usr/bin/env bash
# profile-ingest.sh — where does the ingest actually spend its CPU?
#
#   sudo ./profile-ingest.sh              profile one full round (~3 min)
#   sudo ./profile-ingest.sh 120          longer samples, per stage
#
# Needs root: the interesting processes belong to containers (uid 0), `yama/ptrace_scope=1` stops a
# normal user attaching to them, and `perf_event_paranoid=4` blocks perf outright. Root bypasses all
# three, so nothing here changes a system setting — it just needs to be run with sudo.
#
# ## Why profile at all
#
# We know the SHAPE of the load: 8 task executors, SereneDB averaging ~300% with compaction bursts to
# 876%, the GPU at 68-72%, and 8 workers buying only 3.8x rather than 8x. Every one of those is a
# percentage — they say how much, never what. Three specific questions this is meant to answer:
#
#   1. Where does a task executor's CPU go? Chunking, tokenising, HTTP to Ollama, or insert?
#   2. What is SereneDB doing during a compaction burst — and is it the same work as the steady 100%?
#   3. What else is on the box? pdftotext from the extractor and llama-server were both visibly hot
#      while "the ingest" was supposedly the only thing running.
#
# ## What it produces
#
#   profiles/<stamp>/executor-<pid>.svg   py-spy flamegraph, Python frames, one task executor
#   profiles/<stamp>/executor-dump.txt    instant stack of ALL executors — cheap, often enough
#   profiles/<stamp>/serened.svg          perf flamegraph of the storage engine
#   profiles/<stamp>/system.svg           system-wide, everything, the honest overall split
#   profiles/<stamp>/*.txt                text reports, readable without a browser
#
# SVGs are interactive: click to zoom, Ctrl-F to search.
set -uo pipefail

DURATION="${1:-60}"
ORACLE="${ORACLE_DIR:-$HOME/Projects/oracle}"

if [ "$(id -u)" -ne 0 ]; then
	echo "needs root (container processes, ptrace_scope=1, perf_event_paranoid=4):" >&2
	echo "  sudo $0 $*" >&2
	exit 1
fi

# Run as root but write as the human, so the output is readable afterwards without another sudo.
OWNER="${SUDO_USER:-root}"
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
[ -n "$OWNER_HOME" ] || OWNER_HOME="$HOME"
[ "$ORACLE" = "$HOME/Projects/oracle" ] && ORACLE="$OWNER_HOME/Projects/oracle"

PY_SPY="$OWNER_HOME/.local/bin/py-spy"
COLLAPSE="$OWNER_HOME/.cargo/bin/inferno-collapse-perf"
FLAME="$OWNER_HOME/.cargo/bin/inferno-flamegraph"
command -v inferno-collapse-perf >/dev/null 2>&1 && COLLAPSE=inferno-collapse-perf
command -v inferno-flamegraph >/dev/null 2>&1 && FLAME=inferno-flamegraph

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$ORACLE/profiles/$STAMP"
mkdir -p "$OUT"

say() { printf '%s\n' "$*"; }

have_flame=1
if [ ! -x "$COLLAPSE" ] && ! command -v "$COLLAPSE" >/dev/null 2>&1; then
	say "note: inferno not found — perf.data and text reports still produced, no SVG."
	say "      install with:  cargo install inferno"
	have_flame=0
fi

# perf's stack unwinding: --call-graph dwarf is far more accurate than frame pointers for optimised
# C++ (which is built without frame pointers as a rule), at the cost of much larger perf.data. For a
# storage engine this is the difference between a readable flamegraph and a wall of [unknown].
PERF_CG="${PERF_CALL_GRAPH:-dwarf}"

flamegraph_from_perf() {
	local data="$1" out="$2" title="$3"
	[ "$have_flame" -eq 1 ] || return 0
	perf script -i "$data" 2>/dev/null | "$COLLAPSE" 2>/dev/null |
		"$FLAME" --title "$title" >"$out" 2>/dev/null
	[ -s "$out" ] || say "  (flamegraph came out empty — check $data with: perf report -i $data)"
}

# ── wait until there is actually work to profile ────────────────────────────────────────────────
# The thermal gate means the machine has genuine idle windows. Sampling one of those produces a
# beautiful flamegraph of a process waiting on a queue, which answers nothing.
say "waiting for the parser to be busy (so we profile work, not the gate's idle window)…"
waited=0
while [ "$waited" -lt 900 ]; do
	pending="$(docker exec docker-mysql-1 mysql -uroot -pinfini_rag_flow -N \
		-e "select count(*) from rag_flow.document where run in ('0','1');" 2>/dev/null |
		tr -d '[:space:]')"
	[ "${pending:-0}" -gt 20 ] && break
	sleep 10
	waited=$((waited + 10))
done
say "  pending=${pending:-0} after ${waited}s — starting"
say

# ── 1. task executors (Python) ──────────────────────────────────────────────────────────────────
mapfile -t EXEC_PIDS < <(pgrep -f "task_executor.py" | head -8)
if [ "${#EXEC_PIDS[@]}" -eq 0 ]; then
	say "no task_executor.py processes found — is the ragflow container up?"
else
	say "1/3  task executors: ${#EXEC_PIDS[@]} found"

	# An instant stack of every executor first. This is nearly free and frequently answers the
	# question on its own: if all eight are sitting in the same read() the bottleneck is not CPU.
	{
		for p in "${EXEC_PIDS[@]}"; do
			echo "===== pid $p ====="
			"$PY_SPY" dump --pid "$p" 2>&1
			echo
		done
	} >"$OUT/executor-dump.txt"
	say "     instant stacks -> executor-dump.txt"

	target="${EXEC_PIDS[0]}"
	say "     sampling pid $target for ${DURATION}s…"
	# --idle includes threads blocked in IO. Without it a worker that spends its life waiting on
	# Ollama or the doc store looks like it is barely running, which is the opposite of the finding.
	"$PY_SPY" record --pid "$target" --duration "$DURATION" --rate 99 --idle \
		--output "$OUT/executor-$target.svg" >/dev/null 2>&1 ||
		say "     py-spy record failed on $target"
	"$PY_SPY" record --pid "$target" --duration 15 --rate 99 --idle --format speedscope \
		--output "$OUT/executor-$target.speedscope.json" >/dev/null 2>&1 || true
	say "     -> executor-$target.svg"
fi
say

# ── 2. SereneDB (native) ────────────────────────────────────────────────────────────────────────
SERENE_PID="$(pgrep -x serened | head -1)"
if [ -z "$SERENE_PID" ]; then
	say "2/3  serened not running — skipped"
else
	say "2/3  serened (pid $SERENE_PID): sampling ${DURATION}s with --call-graph $PERF_CG…"
	perf record -F 99 -g --call-graph "$PERF_CG" -p "$SERENE_PID" \
		-o "$OUT/serened.data" -- sleep "$DURATION" >/dev/null 2>&1
	perf report -i "$OUT/serened.data" --stdio --sort symbol 2>/dev/null |
		head -60 >"$OUT/serened.txt"
	flamegraph_from_perf "$OUT/serened.data" "$OUT/serened.svg" "serened — $STAMP"
	say "     -> serened.svg, serened.txt"
fi
say

# ── 3. everything ───────────────────────────────────────────────────────────────────────────────
# System-wide, because the two profiles above assume we already know who the players are — and that
# assumption has been wrong twice tonight (pdftotext at 98%, llama-server at 59%, neither of them
# "the ingest"). Frame pointers here rather than dwarf: -a with dwarf writes enormous files.
say "3/3  system-wide: sampling ${DURATION}s…"
perf record -F 99 -g -a -o "$OUT/system.data" -- sleep "$DURATION" >/dev/null 2>&1
perf report -i "$OUT/system.data" --stdio --sort comm 2>/dev/null | head -30 >"$OUT/system-by-process.txt"
perf report -i "$OUT/system.data" --stdio --sort symbol 2>/dev/null | head -60 >"$OUT/system-by-symbol.txt"
flamegraph_from_perf "$OUT/system.data" "$OUT/system.svg" "whole machine — $STAMP"
say "     -> system.svg, system-by-process.txt, system-by-symbol.txt"

chown -R "$OWNER" "$OUT" 2>/dev/null

say
say "written to $OUT"
say
say "start here:"
say "  head -30 $OUT/system-by-process.txt     # who is actually burning the machine"
say "  xdg-open $OUT/system.svg                # then drill in"
