#!/usr/bin/env bash
# catch-phase-change.sh - profile whatever a long single COPY does AFTER it finishes parsing.
#
#   sudo ./catch-phase-change.sh                    watch the running target, record on transition
#   sudo ./catch-phase-change.sh --pid 12345        explicit pid
#
# ## Why this exists
#
# A single `COPY FROM STDIN` of 2.79M rows buffered 83 GB of wire text and then spent 35+ minutes
# parsing it on ONE thread. Everything after that - the actual insert, the columnar encode, the index
# build, the commit - has not been observed at all, because:
#
#   * it has not happened yet, so no capture taken so far contains it;
#   * a continuous recording covers it but averages it with the 35 minutes of parsing that precede
#     it, which drowns it;
#   * a scheduled window cannot be aimed at a transition whose time is unknown.
#
# So this watches for the transition and records when it fires.
#
# ## How the transition is detected
#
# Two independent signals, either of which is sufficient:
#
#   threads   parsing runs on exactly one runnable thread. The columnar/index work in the sharded
#             runs used 9-10. So >= THREAD_TRIGGER runnable threads means parsing is no longer what
#             is happening.
#   disk      nothing at all has been written during parsing - the data directory sat at 1 MB for
#             35 minutes. Any real growth means the write path has started.
#
# Both are cheap /proc and du reads. Neither opens a connection to the server: a probe was what
# stranded a session and pinned a core earlier today.
set -uo pipefail

TARGET_PID=""
[ "${1:-}" = "--pid" ] && TARGET_PID="${2:-}"
DATA_DIR="${DATA_DIR:-/mnt/data/oracle/serene-clean}"
OUT_DIR="${OUT_DIR:-$HOME/Projects/oracle/profiles}"

THREAD_TRIGGER="${THREAD_TRIGGER:-3}" # runnable threads that mean "not parsing any more"
DISK_TRIGGER_MB="${DISK_TRIGGER_MB:-20}"
WINDOW="${WINDOW:-20}"    # seconds per capture
CAPTURES="${CAPTURES:-6}" # take several: the post-parse work is itself multi-phase
GAP="${GAP:-25}"          # seconds between captures
POLL="${POLL:-2}"
MAX_WAIT="${MAX_WAIT:-14400}"

if [ "$(id -u)" -ne 0 ]; then
	echo "needs root for perf:  sudo $0 $*" >&2
	exit 1
fi

owner="${SUDO_USER:-$(id -un)}"
owner_home="$(getent passwd "$owner" | cut -d: -f6)"
[ -n "$owner_home" ] && OUT_DIR="${OUT_DIR/#$HOME/$owner_home}"

if [ -z "$TARGET_PID" ]; then
	for p in $(pgrep -x serened); do
		if tr '\0' ' ' <"/proc/$p/cmdline" 2>/dev/null | grep -q "$(basename "$DATA_DIR")"; then
			TARGET_PID="$p"
			break
		fi
	done
fi
[ -z "$TARGET_PID" ] && {
	echo "no serened found for $DATA_DIR" >&2
	exit 1
}

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$OUT_DIR/phase-change-$STAMP"
mkdir -p "$OUT" || exit 1

# perf needs root, so everything below lands root:root with the .data files at 0600 - unreadable to
# whoever ran this. A single chown at the end is not enough: it does not run when the script is
# interrupted, and a root-owned directory cannot even be MOVED by its owner afterwards, because
# rename(2) needs write permission on the directory itself. So hand it back now, after each capture,
# and on every exit path.
hand_back() {
	[ -n "${SUDO_USER:-}" ] && chown -R "$owner" "$OUT" 2>/dev/null
	return 0
}
trap hand_back EXIT INT TERM
hand_back
say() { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$OUT/watch.log"; }

runnable() { grep -lc '^[0-9]* ([^)]*) R' /proc/"$TARGET_PID"/task/*/stat 2>/dev/null | wc -l; }
disk_mb() { du -sm "$DATA_DIR" 2>/dev/null | cut -f1; }

base_disk="$(disk_mb)"
say "watching pid $TARGET_PID, baseline disk ${base_disk}MB"
say "trigger: >=${THREAD_TRIGGER} runnable threads, or disk +${DISK_TRIGGER_MB}MB"

waited=0
fired=""
while [ "$waited" -lt "$MAX_WAIT" ]; do
	# The process ending is itself the transition - commit finished, or it died. Either way whatever
	# came after parsing is over and there is nothing left to sample, so say so rather than spin.
	kill -0 "$TARGET_PID" 2>/dev/null || {
		say "target exited before any transition was seen"
		exit 0
	}
	r="$(runnable)"
	d="$(disk_mb)"
	if [ "${r:-0}" -ge "$THREAD_TRIGGER" ]; then
		fired="threads=${r}"
	elif [ "$((${d:-0} - base_disk))" -ge "$DISK_TRIGGER_MB" ]; then
		fired="disk=${d}MB (+$((d - base_disk)))"
	fi
	[ -n "$fired" ] && break
	sleep "$POLL"
	waited=$((waited + POLL))
done

[ -z "$fired" ] && {
	say "no transition within ${MAX_WAIT}s"
	exit 0
}

say "TRANSITION: $fired after ${waited}s of watching"

# Several captures rather than one. The sharded runs showed the post-write work is at least two
# distinct phases (DuckDB columnar re-encode, then IResearch postings + BM25 merge), so a single
# window would catch one and call it "the commit".
for i in $(seq 1 "$CAPTURES"); do
	kill -0 "$TARGET_PID" 2>/dev/null || {
		say "target exited after capture $((i - 1))"
		break
	}
	r="$(runnable)"
	d="$(disk_mb)"
	f="$OUT/after-parse-${i}-threads${r}-disk${d}MB.data"
	say "capture $i/$CAPTURES: ${r} runnable threads, disk ${d}MB"
	perf record -F 199 -g --call-graph fp -p "$TARGET_PID" -o "$f" -- sleep "$WINDOW" >/dev/null 2>&1
	perf report -i "$f" --stdio --sort symbol --no-children -g none 2>/dev/null |
		grep -E "^ +[0-9]+\.[0-9]+%" | head -12 >"${f%.data}.symbols.txt"
	say "  top: $(head -1 "${f%.data}.symbols.txt" | sed 's/^ *//' | cut -c1-70)"
	[ "$i" -lt "$CAPTURES" ] && sleep "$GAP"
	hand_back
done

say "done - $OUT"
