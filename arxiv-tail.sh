#!/usr/bin/env bash
# arxiv-tail.sh — keep the arXiv KB following the mirror, forever.
#
# The mirror at /mnt/data/arxiv is still downloading (a ~1.1M-paper target, ~230k in as of
# 2026-07-29) and will be for days. A one-shot ingest would capture whatever happened to be on disk
# at that moment and then quietly go stale. So this loops: extract whatever is newly downloaded,
# hand it to RAGFlow, sleep, repeat.
#
#   ./arxiv-tail.sh            run in the foreground (Ctrl-C to stop)
#   ./arxiv-tail.sh --once     one cycle, then exit — for testing
#
# Normally run as a systemd user unit: oracle-arxiv-ingest.service
#
# ## What it does per cycle
#
#   1. arxiv-select.py --all --max N   -> pdftotext the next N papers not yet extracted
#   2. ingest-corpus.py --only arxiv   -> upload + trigger parse (existing filenames are skipped)
#   3. sleep
#
# Both steps are idempotent and resumable, which is what makes a crash or a reboot a non-event: the
# extracted .txt on disk is the record of step 1, and RAGFlow's document table is the record of
# step 2. Neither needs a ledger of its own.
#
# ## Why batches, and why it waits
#
# BATCH bounds one cycle so a restart never has to redo hours of work, and so the queue in front of
# the parser stays interpretable — `ingest-status.py` showing 900 UNSTART is fine; showing 200,000
# would make the number useless.
#
# The wait matters more than it looks. Parsing is not free (~52 chunks/paper, embedding on the GPU
# that vision and chat also want), the download is competing for the same disk, and there is no
# deadline here. Feeding faster than the parser drains only grows a queue.
set -uo pipefail

ORACLE="${ORACLE_DIR:-$HOME/Projects/oracle}"
BATCH="${ARXIV_BATCH:-2000}"
INTERVAL="${ARXIV_INTERVAL:-900}"      # seconds between cycles
MAX_PENDING="${ARXIV_MAX_PENDING:-50}" # skip a cycle if the parser is this busy already

# THERMAL GATE. The machine is a laptop and this workload pins every core for hours; it runs hot.
#
# This started as a fixed pause between refills and that was the wrong instrument twice over. First,
# a gap between refills is not a rest at all: the heat comes from RAGFlow parsing the queue, not from
# the tailer, so pausing while thousands of documents are still in flight pauses nothing. Second —
# and this is why the fixed pause had to go — even once the queue empties the machine is still busy,
# because SereneDB keeps running `maintenance: per-index refresh/compaction loops` after the last
# insert. A 60-second timer was resting the feeder, not the machine.
#
# So the gate is now the thing we actually care about: don't start a batch until the CPU package is
# below TEMP_MAX. Time and CPU% were only ever proxies for temperature; this reads the sensor.
#
# If it never cools below the threshold, the cycle is SKIPPED rather than started hot — the loop
# stays alive and retries next interval, so a hot afternoon throttles ingestion instead of either
# cooking the machine or wedging the unit. Set ARXIV_TEMP_MAX=0 to disable the gate entirely.
# An unconditional settle after handing off a batch, so the next cycle's temperature reading is not
# taken during the spike from the work just queued.
COOLDOWN="${ARXIV_COOLDOWN:-60}"
# Degrees C. His call: start a batch below this, not above.
TEMP_MAX="${ARXIV_TEMP_MAX:-75}"
TEMP_POLL="${ARXIV_TEMP_POLL:-15}"
# How long it must STAY under TEMP_MAX before a batch starts. Measured, not guessed: after RAGFlow
# went idle, SereneDB held ~100% CPU for a further ~60s before dropping to zero, so a minute is the
# length of the tail this is meant to sit out. TEMP_POLL divides it evenly on purpose.
TEMP_SUSTAIN="${ARXIV_TEMP_SUSTAIN:-60}"
# Upper bound on the whole wait, so a hot ambient throttles ingestion instead of hanging the cycle.
# Must exceed TEMP_SUSTAIN or the gate could never be satisfied.
TEMP_WAIT_MAX="${ARXIV_TEMP_WAIT_MAX:-900}"

# Resolve the sensor ONCE, by name. /sys/class/hwmon numbering is assigned in probe order and moves
# across reboots, so a hardcoded hwmon8 would silently start reading the NVMe or the battery — and a
# thermal gate that reads the wrong sensor is worse than no gate, because it looks like it works.
# coretemp's temp1_input is "Package id 0"; x86_pkg_temp is the same number via the thermal zones.
TEMP_SENSOR=""
for _h in /sys/class/hwmon/hwmon*; do
	[ "$(cat "$_h/name" 2>/dev/null)" = "coretemp" ] || continue
	[ -r "$_h/temp1_input" ] && TEMP_SENSOR="$_h/temp1_input" && break
done
if [ -z "$TEMP_SENSOR" ]; then
	for _z in /sys/class/thermal/thermal_zone*; do
		[ "$(cat "$_z/type" 2>/dev/null)" = "x86_pkg_temp" ] || continue
		[ -r "$_z/temp" ] && TEMP_SENSOR="$_z/temp" && break
	done
fi
ONCE=0
[ "${1:-}" = "--once" ] && ONCE=1

cd "$ORACLE" || exit 1

# The key lives in the repo's own scripts; read it rather than duplicating it in a unit file where
# it would end up in `systemctl show` output and journald.
key() {
	grep -rhoE 'ragflow-[A-Za-z0-9_-]{20,}' ingest-ctl.sh 2>/dev/null |
		grep suiLQTGM | head -1
}

stamp() { date '+%Y-%m-%d %H:%M:%S'; }

# Documents RAGFlow has not finished with: UNSTART(0) or RUNNING(1), across every dataset — a task
# from another knowledge base heats the same CPU.
pending_docs() {
	local n
	n="$(docker exec docker-mysql-1 mysql -uroot -pinfini_rag_flow -N \
		-e "select count(*) from rag_flow.document where run in ('0','1');" 2>/dev/null |
		tr -d '[:space:]')"
	printf '%s' "${n:-0}"
}

# CPU package temperature in whole degrees C, or empty if there is no readable sensor. Whole degrees
# because bash has no floats and 0.5°C does not change any decision here.
cpu_temp() {
	local milli
	[ -n "$TEMP_SENSOR" ] || return 1
	milli="$(cat "$TEMP_SENSOR" 2>/dev/null | tr -d '[:space:]')"
	case "$milli" in
	'' | *[!0-9]*) return 1 ;; # unreadable or not a number — treat as "no sensor", never as "cold"
	esac
	printf '%s' "$((milli / 1000))"
}

# Returns 0 when it is cool enough to start a batch, 1 when the caller should skip this cycle.
#
# The condition is SUSTAINED, not instantaneous (his call: "below 75 at least for a minute"). A
# single reading is not evidence the machine has cooled — this workload's signature is periodic
# bursts, so sampling between two compaction spikes reads cold and would start a batch straight into
# the next one. Requiring TEMP_SUSTAIN seconds of unbroken sub-threshold readings makes the gate
# describe a state rather than a moment; any single reading at or above the limit resets the clock.
wait_for_cool() {
	local t waited=0 cool_for=0 announced=0
	[ "$TEMP_MAX" -gt 0 ] || return 0
	while :; do
		if ! t="$(cpu_temp)"; then
			# No sensor is not permission to hammer the machine — but it is also not a reason to
			# stop ingesting. Say so, so it lands in the journal rather than passing silently.
			echo "$(stamp)  no readable CPU temperature sensor — thermal gate inactive"
			return 0
		fi
		if [ "$t" -lt "$TEMP_MAX" ]; then
			if [ "$cool_for" -ge "$TEMP_SUSTAIN" ]; then
				echo "$(stamp)  ${t}°C, held under ${TEMP_MAX}°C for ${cool_for}s — starting a batch"
				return 0
			fi
		else
			if [ "$cool_for" -gt 0 ]; then
				echo "$(stamp)  ${t}°C — back over ${TEMP_MAX}°C after ${cool_for}s, clock reset"
			fi
			cool_for=0
		fi
		if [ "$announced" -eq 0 ]; then
			echo "$(stamp)  ${t}°C — need < ${TEMP_MAX}°C held ${TEMP_SUSTAIN}s before a batch"
			announced=1
		fi
		[ "$waited" -ge "$TEMP_WAIT_MAX" ] && break
		sleep "$TEMP_POLL"
		waited=$((waited + TEMP_POLL))
		# Credited AFTER the sleep, against the reading taken before it, so cool_for counts elapsed
		# observed-cool time. Crediting on the read itself would call the very first sample a full
		# poll interval of evidence and let one lucky reading satisfy a minute-long requirement.
		[ "$t" -lt "$TEMP_MAX" ] && cool_for=$((cool_for + TEMP_POLL))
	done
	echo "$(stamp)  still ${t}°C after ${waited}s — skipping this cycle rather than starting hot"
	return 1
}

cycle() {
	local k
	k="$(key)"
	if [ -z "$k" ]; then
		echo "$(stamp)  no RAGFlow API key found — cannot ingest"
		return 1
	fi

	# Don't pile work onto a parser that is still chewing. Without this the queue only ever grows:
	# each cycle adds BATCH more while the executor is still on the last lot.
	#
	# The threshold is deliberately well BELOW one batch. Waiting for a genuinely quiet queue means
	# a re-parse or another KB's ingest finishes before this adds to it, and since nothing is
	# waiting on arXiv, a skipped cycle costs nothing — the next one is fifteen minutes away. The
	# first version compared against BATCH itself, which would have added 2,000 documents on top of
	# a 1,288-document re-parse that was mid-verification.
	local pending
	pending="$(pending_docs)"
	if [ "$pending" -gt "$MAX_PENDING" ]; then
		echo "$(stamp)  $pending documents still queued/parsing (limit $MAX_PENDING) — skipping"
		return 0
	fi

	# Cool first, then work. Ordered this way deliberately: the queue-depth check above is cheap and
	# a busy parser makes the temperature question moot, so there is no point waiting several minutes
	# for a sensor only to then skip on MAX_PENDING.
	wait_for_cool || return 0

	echo "$(stamp)  extracting up to $BATCH new papers"
	python3 arxiv-select.py --all --max "$BATCH" 2>&1 | tail -3

	echo "$(stamp)  ingesting"
	python3 ingest-corpus.py --api-key "$k" --only arxiv 2>&1 | tail -2

	# A short unconditional settle. This is NOT the thermal rest any more — wait_for_cool at the top
	# of the next cycle is. Its only job is to keep the next temperature reading honest: sampling the
	# sensor immediately after handing 500 documents to eight executors would read the spike from the
	# work just queued, decide the machine is hot, and wait out a tail that had barely begun.
	[ "$COOLDOWN" -gt 0 ] && sleep "$COOLDOWN"
	echo "$(stamp)  cycle done"
}

trap 'echo "$(stamp)  stopping"; exit 0' INT TERM

while true; do
	cycle
	[ "$ONCE" -eq 1 ] && break
	sleep "$INTERVAL"
done
