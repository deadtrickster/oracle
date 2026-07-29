#!/usr/bin/env bash
# cooldown-probe.sh — how long does the machine stay busy AFTER the parse queue empties?
#
#   ./cooldown-probe.sh [seconds]      default 600, sampling every 5s
#
# ## Why this exists
#
# arxiv-tail.sh rests for ARXIV_COOLDOWN seconds between refills, and that number was a guess (60s).
# The guess assumes the machine goes quiet when RAGFlow runs out of documents. It does not: SereneDB
# keeps working after the last insert — its own log calls the mechanism `maintenance: per-index
# refresh/compaction loops`, and it was measured sustaining ~3 cores with bursts to 5.5 while
# ingesting. If that decay outlasts the pause, then the "thermal idle" is idle for the feeder and
# nothing else, which is the failure mode this probe is meant to expose.
#
# So: sample the queue depth and both containers' CPU together, and find the lag between "queue
# empty" and "CPU actually low". The cooldown should be set from that lag, not from a round number.
#
# Output is one line per sample plus a summary. Read-only — it samples, it changes nothing.
set -uo pipefail

DURATION="${1:-600}"
EVERY="${PROBE_EVERY:-5}"
# "Quiet" for SereneDB. Idle is not 0%: the process has background loops that tick regardless, so a
# threshold near zero would never be reached and the summary would report "never settled" forever.
QUIET="${PROBE_QUIET:-60}"

MYSQL=(docker exec docker-mysql-1 mysql -uroot -pinfini_rag_flow -N -e)

pending() {
	local n
	n="$("${MYSQL[@]}" "select count(*) from rag_flow.document where run in ('0','1');" 2>/dev/null |
		tr -d '[:space:]')"
	printf '%s' "${n:-0}"
}

# One docker stats call for both containers — two separate --no-stream calls would sample ~1s apart
# and cost twice the overhead, which matters at a 5s interval.
cpus() {
	docker stats --no-stream --format '{{.Name}} {{.CPUPerc}}' \
		oracle-serenedb docker-ragflow-cpu-1 2>/dev/null |
		sed 's/%//' | awk '{printf "%s ", $2}'
}

printf '%-8s  %8s  %9s  %9s\n' elapsed pending serene ragflow

t=0
empty_at=-1
settled_at=-1
peak_after_empty=0
while [ "$t" -lt "$DURATION" ]; do
	p="$(pending)"
	read -r s r <<<"$(cpus)"
	s="${s:-0}"
	r="${r:-0}"
	printf '%-8s  %8s  %8.0f%%  %8.0f%%\n' "${t}s" "$p" "$s" "$r"

	# First moment the parser has nothing left. Everything after this is the tail we care about.
	if [ "$empty_at" -lt 0 ] && [ "${p:-0}" -le 0 ]; then
		empty_at="$t"
		echo "         ^ queue empty — measuring how long the machine stays busy"
	fi

	if [ "$empty_at" -ge 0 ]; then
		# Track the worst spike seen after the queue emptied: the average understates a workload
		# whose whole signature is periodic bursts.
		awk -v a="$s" -v b="$peak_after_empty" 'BEGIN{exit !(a>b)}' && peak_after_empty="$s"
		if [ "$settled_at" -lt 0 ] && awk -v a="$s" -v q="$QUIET" 'BEGIN{exit !(a<q)}'; then
			settled_at="$t"
		fi
	fi

	sleep "$EVERY"
	t=$((t + EVERY))
done

echo
echo "SUMMARY"
if [ "$empty_at" -lt 0 ]; then
	echo "  queue never emptied within ${DURATION}s — the parser is still behind, so the cooldown"
	echo "  is not the binding constraint yet. Re-run when it has caught up."
	exit 0
fi
echo "  queue emptied at            ${empty_at}s"
printf '  peak serene CPU after that  %.0f%%\n' "$peak_after_empty"
if [ "$settled_at" -lt 0 ]; then
	echo "  serene never dropped below ${QUIET}% — the tail is longer than this probe ran."
	echo "  => ARXIV_COOLDOWN is too small; it is resting the feeder, not the machine."
else
	echo "  serene fell below ${QUIET}% at    ${settled_at}s"
	echo "  => decay after the queue emptied: $((settled_at - empty_at))s. Set ARXIV_COOLDOWN at"
	echo "     or above that; below it, each 'rest' begins while compaction is still running."
fi
