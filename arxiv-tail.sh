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

# THERMAL IDLE. The machine is a laptop and this workload pins every core for hours; it runs hot.
#
# A gap between refills is not by itself a rest: the heat comes from RAGFlow parsing the queue, not
# from the tailer, so refilling on a timer while thousands of documents are still queued means the
# CPU never actually stops. The rest has to come AFTER the queue drains.
#
# So each cycle is: refill -> wait for the parser to finish -> do nothing for COOLDOWN -> refill.
# Set ARXIV_COOLDOWN=0 to disable.
COOLDOWN="${ARXIV_COOLDOWN:-60}"
DRAIN_POLL="${ARXIV_DRAIN_POLL:-10}"
DRAIN_MAX="${ARXIV_DRAIN_MAX:-60}" # cap the drain wait; the pause is the point, not the waiting
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

	echo "$(stamp)  extracting up to $BATCH new papers"
	python3 arxiv-select.py --all --max "$BATCH" 2>&1 | tail -3

	echo "$(stamp)  ingesting"
	python3 ingest-corpus.py --api-key "$k" --only arxiv 2>&1 | tail -2

	# Wait for the parser to actually finish before resting. Without this the "rest" is spent with
	# thousands of documents still queued and every core busy — a pause that pauses nothing.
	if [ "$COOLDOWN" -gt 0 ]; then
		# Wait for the queue to drain, but CAP the wait (his call: "limit 1 minute wait for now").
		# At 8 workers a 500-document batch takes minutes to parse, so waiting for a full drain
		# would idle the feeder far longer than the thermal pause needs — and the pause is the
		# point, not the waiting. Bounded here, so one cycle rests for about a minute whether or
		# not the parser has caught up.
		local waited=0 d
		while [ "$waited" -lt "$DRAIN_MAX" ]; do
			d="$(pending_docs)"
			[ "${d:-0}" -le 0 ] && break
			sleep "$DRAIN_POLL"
			waited=$((waited + DRAIN_POLL))
		done
		echo "$(stamp)  queue check done after ${waited}s — idling ${COOLDOWN}s to cool off"
		sleep "$COOLDOWN"
	fi
	echo "$(stamp)  cycle done"
}

trap 'echo "$(stamp)  stopping"; exit 0' INT TERM

while true; do
	cycle
	[ "$ONCE" -eq 1 ] && break
	sleep "$INTERVAL"
done
