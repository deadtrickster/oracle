#!/usr/bin/env bash
# watch-load.sh - unattended watcher for a long SereneDB load. No harness, no prompts.
#
#   ./watch-load.sh                       watch the default target, log to profiles/
#   ./watch-load.sh --pid 1234            explicit pid
#   DATA_DIR=/path ./watch-load.sh        different instance
#   ./watch-load.sh --tail                follow the log of a watcher already running
#
# Start it detached so it survives the terminal:
#   setsid nohup ./watch-load.sh > /dev/null 2>&1 &
#
# ## Why a plain script
#
# The first version of this was an agent-driven monitor. It stopped at a permission prompt overnight
# and reported nothing until morning, which is precisely the case it existed for. A watcher for a
# multi-hour run must not depend on anything interactive: no prompts, no session, no approvals. This
# writes a line to a log and to the terminal, and that is all.
#
# ## What it reports, and why only these
#
# A long single-statement load has almost no observable state, so the few signals that exist matter:
#
#   phase change   runnable threads > 1. Parsing is single-threaded; the columnar and index work in
#                  the sharded runs used 9-10 threads. Any real parallelism means the phase changed.
#   disk           the data directory. Flat for 77 minutes at the start, then bursts.
#   index          engine_search specifically. The columnar store growing tells you nothing about
#                  whether the inverted/IVF build has begun - after 13 hours it had not.
#   memory         VmRSS + VmSwap, NOT RSS. RSS falls while swap rises and the run looks like it is
#                  finishing when it is not. This is the number that does not lie.
#   pressure       system MemAvailable, because on a box with swap the process never hits an RSS
#                  ceiling, it just drags everything down.
#   completion     the client connection closing.
#
# Quiet by default: it prints a line only when something changes by more than the thresholds, plus a
# heartbeat every HEARTBEAT seconds so a silent log is distinguishable from a dead watcher.
set -uo pipefail

DATA_DIR="${DATA_DIR:-/mnt/data/oracle/serene-clean}"
PORT="${PORT:-7892}"
TARGET_PID="${TARGET_PID:-}"
POLL="${POLL:-20}"
HEARTBEAT="${HEARTBEAT:-1800}"
DISK_STEP_MB="${DISK_STEP_MB:-500}"
MEM_STEP_GB="${MEM_STEP_GB:-5}"
LOW_AVAIL_GB="${LOW_AVAIL_GB:-4}"
OUT_DIR="${OUT_DIR:-$HOME/Projects/oracle/profiles}"

case "${1:-}" in
--pid)
	TARGET_PID="${2:-}"
	;;
--tail)
	f="$(find "$OUT_DIR" -maxdepth 1 -name 'watch-load-*.log' -printf '%T@ %p\n' 2>/dev/null |
		sort -rn | head -1 | cut -d' ' -f2-)"
	[ -n "$f" ] && exec tail -f "$f"
	echo "no watcher log in $OUT_DIR" >&2
	exit 1
	;;
esac

if [ -z "$TARGET_PID" ]; then
	for p in $(pgrep -x serened 2>/dev/null); do
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

mkdir -p "$OUT_DIR" 2>/dev/null
LOG="$OUT_DIR/watch-load-$(date +%Y%m%d-%H%M%S).log"
say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }

committed_gb() { awk '/^VmRSS:|^VmSwap:/{s+=$2} END{printf "%.1f", s/1048576}' "/proc/$TARGET_PID/status" 2>/dev/null; }
runnable() { grep -lc '^[0-9]* ([^)]*) R' /proc/"$TARGET_PID"/task/*/stat 2>/dev/null | wc -l; }
disk_mb() { du -sm "$DATA_DIR" 2>/dev/null | cut -f1; }
index_files() { find "$DATA_DIR/engine_search" -type f 2>/dev/null | wc -l; }
avail_gb() { free -g | awk 'NR==2{print $7}'; }
swap_gb() { free -g | awk '/Swap/{print $3}'; }
conns() { ss -tn 2>/dev/null | grep -c ":$PORT"; }

say "watching pid $TARGET_PID, data $DATA_DIR, port $PORT"
say "log: $LOG"
last_disk="$(disk_mb)"
last_mem="$(committed_gb)"
last_idx="$(index_files)"
last_beat=0
started="$(date +%s)"
warned=0
say "start: disk ${last_disk}MB, committed ${last_mem}GB, index ${last_idx} files, threads $(runnable)"

while :; do
	sleep "$POLL"
	now=$(date +%s)
	el=$(((now - started) / 60))

	kill -0 "$TARGET_PID" 2>/dev/null || {
		say "TARGET EXITED after ${el}m"
		exit 0
	}

	d="$(disk_mb)"
	m="$(committed_gb)"
	i="$(index_files)"
	r="$(runnable)"
	a="$(avail_gb)"
	s="$(swap_gb)"

	# The index starting is the single most informative event here: everything before it is the
	# columnar store, and the inverted/IVF build is the part that was never reached.
	if [ "${i:-0}" -gt "${last_idx:-0}" ]; then
		say "INDEX STARTED: engine_search ${last_idx} -> ${i} files  (${el}m, disk ${d}MB)"
		last_idx="$i"
	fi

	if [ "${r:-1}" -gt 1 ]; then
		say "PARALLELISM: ${r} runnable threads  (${el}m, disk ${d}MB) - phase changed"
	fi

	if [ $((${d:-0} - last_disk)) -ge "$DISK_STEP_MB" ]; then
		say "disk ${last_disk} -> ${d}MB  committed ${m}GB  threads ${r}  avail ${a}G  (${el}m)"
		last_disk="$d"
	fi

	if awk -v a="$last_mem" -v b="$m" -v st="$MEM_STEP_GB" 'BEGIN{exit !((a-b)>=st)}'; then
		say "committed ${last_mem} -> ${m}GB  disk ${d}MB  swap ${s}G  (${el}m)"
		last_mem="$m"
	fi

	if [ "${a:-99}" -lt "$LOW_AVAIL_GB" ] && [ "$warned" -eq 0 ]; then
		say "LOW MEMORY: available ${a}G, swap ${s}G - the box is at risk"
		warned=1
	fi

	if [ "$(conns)" -eq 0 ]; then
		say "FINISHED after ${el}m: disk ${d}MB, index ${i} files, committed ${m}GB"
		exit 0
	fi

	# Heartbeat, so a quiet log means "nothing changed" rather than "the watcher died".
	if [ $((now - last_beat)) -ge "$HEARTBEAT" ]; then
		say "alive ${el}m: disk ${d}MB, committed ${m}GB, index ${i}, threads ${r}, avail ${a}G, swap ${s}G"
		last_beat=$now
	fi
done
