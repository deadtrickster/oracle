#!/usr/bin/env bash
# workers-sweep.sh - find the task-executor count that maximises ingest throughput.
#
#   ./workers-sweep.sh                    measure 4, 2, 8 with 480s windows
#   ./workers-sweep.sh 600 4 2 8          custom window and values
#
# The papers ingested during the sweep are kept, so the only cost of running this is the measurement
# overhead, not the parsing.
#
# ## What is being measured, and why it is not obvious
#
# Two measured effects pull opposite ways:
#
#   parse/chunk   parallelises across workers. 1 -> 8 workers took 25 -> 95 docs/min.
#   embed         does not. bge-m3@Ollama is overhead-bound (the card sits near 0% during embed) and
#                 Ollama pins embedding models to one slot on purpose, in server/sched.go:
#                 "Embedding models should always be loaded with parallel=1". Concurrent requests
#                 measure worse: ~100 chunks/s single stream, 52 at x4, 33 at x8.
#
# The optimum is where those cross, and arithmetic will not find it. The crude product model predicts
# 38 docs/min at x8 where the measured value was 95, because RAGFlow batches many chunks per request
# and a worker does not hold one open continuously.
#
# ## Why it drains before every change
#
# The first version of this script changed --workers while documents were in flight. Recreating
# ragflow-cpu SIGKILLs the executors, which cannot write a terminal status, so every in-flight
# document was orphaned in RUNNING. requeue-orphans.py then marked them FAIL and re-queued, and the
# next step orphaned them again before they finished. Two of three windows measured a queue of dead
# rows and reported NO PROGRESS, and 2,721 arXiv documents needed re-ingesting.
#
# So: the container is only ever recreated when the queue is EMPTY. No documents are in flight, so
# none can be orphaned, and no repair step is needed. Draining costs wall-clock but the drain is
# ingestion, which is the point of running this at all.
#
# ## Why the last value repeats the first
#
# The index grows throughout, and insert cost grows with it, so a value measured late is handicapped
# against one measured early. Re-measuring the first value at the end says how big that drift is. If
# the two readings for the same worker count disagree, the comparison between different counts is
# worth that much less.
set -uo pipefail

ORACLE="${ORACLE_DIR:-$HOME/Projects/oracle}"
COMPOSE="$ORACLE/ragflow/docker/docker-compose.yml"
WINDOW="${1:-480}"
shift || true
VALUES=("$@")
[ "${#VALUES[@]}" -eq 0 ] && VALUES=(4 2 8)

BATCH="${SWEEP_BATCH:-900}" # papers queued per step; must outlast one window
DRAIN_MAX="${SWEEP_DRAIN_MAX:-2400}"

cd "$ORACLE" || exit 1

mysql_q() {
	docker exec docker-mysql-1 mysql -uroot -pinfini_rag_flow -N -e "$1" 2>/dev/null |
		tr -d '[:space:]'
}
pending() { mysql_q "select count(*) from rag_flow.document where run in ('0','1');"; }
done_n() { mysql_q "select count(*) from rag_flow.document where run='3';"; }
chunks_n() { mysql_q "select coalesce(sum(chunk_num),0) from rag_flow.document where run='3';"; }
stamp() { date '+%H:%M:%S'; }

key() { grep -rhoE 'ragflow-[A-Za-z0-9_-]{20,}' ingest-ctl.sh 2>/dev/null | grep suiLQTGM | head -1; }

original="$(grep -oE '^\s*-\s*--workers=[0-9]+' "$COMPOSE" | grep -oE '[0-9]+' | head -1)"
if [ -z "$original" ]; then
	echo "no '- --workers=N' line in $COMPOSE (see patch-ragflow.sh patch 3)" >&2
	exit 1
fi

if systemctl --user is-active --quiet oracle-arxiv-ingest.service; then
	echo "oracle-arxiv-ingest is running. Stop it first, or its refills and thermal gate will" >&2
	echo "add work at times unrelated to the measurement:" >&2
	echo "  systemctl --user stop oracle-arxiv-ingest.service" >&2
	exit 1
fi

echo "current setting: --workers=$original, window ${WINDOW}s, batch $BATCH"

set_workers() {
	sed -i -E "s|^([[:space:]]*-[[:space:]]*)--workers=[0-9]+|\1--workers=$1|" "$COMPOSE"
	docker compose -p docker --project-directory "$ORACLE/ragflow/docker" \
		-f "$COMPOSE" up -d ragflow-cpu >/dev/null 2>&1
}

# Wait for the queue to empty. Returns 1 on timeout so the caller can refuse to recreate the
# container, which is the whole point: recreating with work in flight is what caused the damage.
drain() {
	local waited=0 p
	while [ "$waited" -lt "$DRAIN_MAX" ]; do
		p="$(pending)"
		[ "${p:-0}" -le 0 ] && return 0
		printf '\r%s  draining: %s pending   ' "$(stamp)" "$p"
		sleep 20
		waited=$((waited + 20))
	done
	printf '\n'
	return 1
}

restore() {
	printf '\n'
	echo "$(stamp)  restoring --workers=$original"
	set_workers "$original"
}
trap restore INT TERM EXIT

printf '\n%-9s %-11s %-11s %s\n' workers docs/min chunks/s note

for n in "${VALUES[@]}"; do
	if ! drain; then
		echo "queue did not drain within ${DRAIN_MAX}s - refusing to recreate with work in flight"
		break
	fi
	printf '\r%s  queue empty, switching to --workers=%s\n' "$(stamp)" "$n"
	set_workers "$n"

	# Refill AFTER the recreate, so the batch is parsed entirely by the new worker count.
	python3 arxiv-select.py --all --max "$BATCH" >/dev/null 2>&1
	python3 ingest-corpus.py --api-key "$(key)" --only arxiv >/dev/null 2>&1

	waited=0
	while [ "$waited" -lt 600 ]; do
		[ "$(pending)" -gt 50 ] && break
		sleep 15
		waited=$((waited + 15))
	done

	t0="$(done_n)"
	c0="$(chunks_n)"
	sleep "$WINDOW"
	t1="$(done_n)"
	c1="$(chunks_n)"
	p_end="$(pending)"

	docs=$(((t1 - t0) * 60 / WINDOW))
	chunks=$(((c1 - c0) / WINDOW))

	# A window that ran out of work measured the drain, not the throughput. Say so rather than
	# reporting a number that looks comparable and is not.
	note=""
	[ "${p_end:-0}" -le 0 ] && note="queue emptied mid-window, UNDERSTATED"
	[ "$((t1 - t0))" -le 0 ] && note="no progress, discard"
	printf '%-9s %-11s %-11s %s\n' "$n" "$docs" "$chunks" "$note"
done
