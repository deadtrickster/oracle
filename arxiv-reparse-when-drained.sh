#!/usr/bin/env bash
# arxiv-reparse-when-drained.sh — wait for the stale queue to empty, then re-parse the arXiv KB.
#
# WHY THIS EXISTS
#
# RAGFlow snapshots a knowledge base's `parser_config` into each TASK when the task is queued. The
# arXiv documents were queued with a broken chunk delimiter — one that made the text parser split on
# (and consume) every letter `n`, so chunks came out as "i creasi gly i volves i formatio ". The
# config is fixed now, but ~800 tasks were already in flight carrying the old snapshot, and they
# keep writing corrupt chunks until they drain. Re-parsing while they run is refused outright
# ("N tasks are ahead in the queue").
#
# So: wait, then re-parse everything once.
#
# NOTHING NEEDS CLEANING UP FIRST. On re-queue RAGFlow deletes the document's previous tasks and
# their chunks from the document store (task_service.py: filter_delete + docStoreConn.delete on
# pre_chunk_ids), so the corrupt rows are removed as part of this rather than accumulating. And the
# chunk-REUSE optimisation cannot mask the fix: reuse is keyed on a digest that includes the
# chunking config, which is exactly what changed, so every task is genuinely redone.
#
# THE 51 NUL FAILURES WILL FAIL AGAIN, ON PURPOSE. Those papers have a broken CID font map, so
# pdftotext emits U+0000 and the insert is rejected. They are kept and allowed to fail visibly
# rather than dropped — a failure that vanishes is amnesia, not a decision. Their ids are in
# corpus/arxiv-needs-ocr.txt, waiting for a deliberate OCR pass.
#
#   ./arxiv-reparse-when-drained.sh              wait, then re-parse
#   ./arxiv-reparse-when-drained.sh --now        skip the wait (will fail if tasks are queued)
#   ./arxiv-reparse-when-drained.sh --check      report the queue depth and exit
set -uo pipefail

ORACLE="${ORACLE_DIR:-$HOME/Projects/oracle}"
POLL="${ARXIV_POLL:-120}"
KB_NAME="arxiv"
cd "$ORACLE" || exit 1

mysql_q() {
	docker exec docker-mysql-1 mysql -uroot -pinfini_rag_flow -N -e "$1" 2>/dev/null
}

key() {
	grep -rhoE 'ragflow-[A-Za-z0-9_-]{20,}' ingest-ctl.sh 2>/dev/null | grep suiLQTGM | head -1
}

stamp() { date '+%Y-%m-%d %H:%M:%S'; }

# How many documents are still RUNNING or UNSTART anywhere — not just in this KB. A task from
# another dataset is equally "ahead in the queue" as far as re-parse is concerned.
queue_depth() {
	mysql_q "select count(*) from rag_flow.document where run in ('0','1');" | tr -d '[:space:]'
}

case "${1:-}" in
--check)
	echo "queue depth: $(queue_depth) documents running or unstarted"
	exit 0
	;;
--now) ;;
"")
	echo "$(stamp)  waiting for the queue to drain (polling every ${POLL}s)"
	last=""
	while true; do
		d="$(queue_depth)"
		[ -z "$d" ] && d=0
		if [ "$d" -eq 0 ]; then
			echo "$(stamp)  queue empty"
			break
		fi
		# Only print when it moves, so a long wait does not fill the log with identical lines.
		if [ "$d" != "$last" ]; then
			echo "$(stamp)  $d still queued/running"
			last="$d"
		fi
		sleep "$POLL"
	done
	;;
*)
	echo "usage: $0 [--now|--check]" >&2
	exit 2
	;;
esac

K="$(key)"
if [ -z "$K" ]; then
	echo "no RAGFlow API key found" >&2
	exit 1
fi

KB="$(mysql_q "select id from rag_flow.knowledgebase where name='$KB_NAME';" | tr -d '[:space:]')"
if [ -z "$KB" ]; then
	echo "no '$KB_NAME' knowledge base" >&2
	exit 1
fi

echo "$(stamp)  re-parsing every document in '$KB_NAME' with the corrected delimiter"
mysql_q "select id from rag_flow.document where kb_id='$KB';" | tr -d '\r' | grep -v '^$' >/tmp/arxiv-doc-ids.$$

total=$(wc -l </tmp/arxiv-doc-ids.$$)
echo "$(stamp)  $total documents"

# Batched: one request with several thousand ids is refused, and a smaller batch also means a crash
# costs one batch rather than the run.
split -l 200 /tmp/arxiv-doc-ids.$$ /tmp/arxiv-batch.$$.
for b in /tmp/arxiv-batch."$$".*; do
	payload=$(awk 'BEGIN{printf "{\"document_ids\":["} {printf "%s\"%s\"", sep, $0; sep=","} END{print "]}"}' "$b")
	code=$(curl -s -m 120 -o /dev/null -w '%{http_code}' -X POST \
		"http://localhost:9380/api/v1/datasets/$KB/chunks" \
		-H "Authorization: Bearer $K" -H 'Content-Type: application/json' -d "$payload")
	echo "$(stamp)  batch $(basename "$b"): $(wc -l <"$b") docs, HTTP $code"
	rm -f "$b"
done
rm -f /tmp/arxiv-doc-ids.$$

echo "$(stamp)  re-parse triggered. Watch with: ./ingest-status.py"
echo "$(stamp)  expect the 51 NUL papers to fail again — that is intended, see corpus/arxiv-needs-ocr.txt"
