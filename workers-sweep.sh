#!/usr/bin/env bash
# workers-sweep.sh — find the task-executor count that actually maximises ingest throughput.
#
#   ./workers-sweep.sh              measure 8, 4, 2 (in that order), 10 min each
#   ./workers-sweep.sh 600 8 4      custom window and values
#
# ## Why this is not obvious from the numbers we already have
#
# Two effects pull in opposite directions and both are measured:
#
#   parse/chunk   parallelises across workers. 1 -> 8 workers took 25 -> 95 docs/min.
#   embed         does NOT. bge-m3@Ollama is overhead-bound (the card sits near 0% during embed) and
#                 Ollama pins embedding models to one slot on purpose — server/sched.go:
#                 "Embedding models should always be loaded with parallel=1". Concurrent requests
#                 make it worse, measured: ~100 chunks/s single stream, 52 at x4, 33 at x8.
#
# So more workers buy parse throughput and lose embed throughput, and the optimum is wherever those
# cross. Arithmetic will not find it: the crude product model predicts 38 docs/min at x8 and the
# measured value was 95, because RAGFlow batches many chunks per request and a worker does not hold a
# request open continuously. Hence: measure.
#
# ## What it costs
#
# Each step edits the vendored docker-compose.yml and recreates ragflow-cpu. Recreation ORPHANS
# whatever was parsing (SIGKILL cannot write a terminal status), so every step runs
# requeue-orphans.py afterwards. Nothing is lost, but the run takes roughly (window + 3 min) per
# value. It restores the original worker count on exit, including on Ctrl-C.
set -uo pipefail

ORACLE="${ORACLE_DIR:-$HOME/Projects/oracle}"
COMPOSE="$ORACLE/ragflow/docker/docker-compose.yml"
WINDOW="${1:-600}"
shift || true
VALUES=("$@")
[ "${#VALUES[@]}" -eq 0 ] && VALUES=(8 4 2)

cd "$ORACLE" || exit 1

mysql_q() {
	docker exec docker-mysql-1 mysql -uroot -pinfini_rag_flow -N -e "$1" 2>/dev/null | tr -d '[:space:]'
}

original="$(grep -oE '^\s*-\s*--workers=[0-9]+' "$COMPOSE" | grep -oE '[0-9]+' | head -1)"
if [ -z "$original" ]; then
	echo "no '- --workers=N' line in $COMPOSE — add one first (see patch-ragflow.sh patch 3)" >&2
	exit 1
fi
echo "current setting: --workers=$original"

set_workers() {
	# Rewrite in place. The line is ours (patch 3), so the pattern is narrow on purpose: a loose
	# sed here would happily rewrite some unrelated --workers in another service block.
	sed -i -E "s|^([[:space:]]*-[[:space:]]*)--workers=[0-9]+|\1--workers=$1|" "$COMPOSE"
	docker compose -p docker --project-directory "$ORACLE/ragflow/docker" \
		-f "$COMPOSE" up -d ragflow-cpu >/dev/null 2>&1
}

restore() {
	echo
	echo "restoring --workers=$original"
	set_workers "$original"
	python3 requeue-orphans.py >/dev/null 2>&1
	echo "restored."
}
trap restore INT TERM EXIT

printf '\n%-9s %-12s %-12s %s\n' workers docs/min chunks/s note

for n in "${VALUES[@]}"; do
	set_workers "$n"

	# The container was just recreated, so anything mid-parse is orphaned. Clear it before measuring,
	# or the window starts with a stalled queue and understates throughput.
	python3 requeue-orphans.py >/dev/null 2>&1

	# Wait until work is genuinely flowing. Measuring during the upload phase reports zero — that
	# mistake already cost one bogus data point tonight.
	waited=0
	while [ "$waited" -lt 420 ]; do
		p="$(mysql_q "select count(*) from rag_flow.document where run in ('0','1');")"
		[ "${p:-0}" -gt 50 ] && break
		sleep 15
		waited=$((waited + 15))
	done

	t0="$(mysql_q "select count(*) from rag_flow.document where run='3';")"
	c0="$(mysql_q "select coalesce(sum(chunk_num),0) from rag_flow.document where run='3';")"
	sleep "$WINDOW"
	t1="$(mysql_q "select count(*) from rag_flow.document where run='3';")"
	c1="$(mysql_q "select coalesce(sum(chunk_num),0) from rag_flow.document where run='3';")"

	docs=$(((t1 - t0) * 60 / WINDOW))
	chunks=$(((c1 - c0) / WINDOW))
	note=""
	[ "$((t1 - t0))" -le 0 ] && note="NO PROGRESS — queue empty or stalled, discard"
	printf '%-9s %-12s %-12s %s\n' "$n" "$docs" "$chunks" "$note"
done
