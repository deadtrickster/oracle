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
INTERVAL="${ARXIV_INTERVAL:-900}" # seconds between cycles
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

cycle() {
	local k
	k="$(key)"
	if [ -z "$k" ]; then
		echo "$(stamp)  no RAGFlow API key found — cannot ingest"
		return 1
	fi

	# Don't pile work onto a parser that is still chewing. Without this the queue only ever grows:
	# each cycle adds BATCH more while the executor is still on the last lot.
	local pending
	pending="$(python3 ingest-status.py 2>/dev/null |
		grep -oE 'UNSTART:[0-9]+|RUNNING:[0-9]+' | grep -oE '[0-9]+' |
		paste -sd+ | bc 2>/dev/null || echo 0)"
	pending="${pending:-0}"
	if [ "$pending" -gt "$BATCH" ]; then
		echo "$(stamp)  $pending still queued/parsing — skipping this cycle"
		return 0
	fi

	echo "$(stamp)  extracting up to $BATCH new papers"
	python3 arxiv-select.py --all --max "$BATCH" 2>&1 | tail -3

	echo "$(stamp)  ingesting"
	python3 ingest-corpus.py --api-key "$k" --only arxiv 2>&1 | tail -2
	echo "$(stamp)  cycle done"
}

trap 'echo "$(stamp)  stopping"; exit 0' INT TERM

while true; do
	cycle
	[ "$ONCE" -eq 1 ] && break
	sleep "$INTERVAL"
done
