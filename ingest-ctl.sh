#!/usr/bin/env bash
# ingest-ctl.sh — pause/resume RAGFlow's PARSER without taking the stack down.
#
# The parse work is done by rag/svr/task_executor.py inside the ragflow container, and it will
# happily eat ~20 of the 24 cores. The obvious move — `docker pause docker-ragflow-cpu-1` — also
# freezes the API on :9380, which is what ask_corpus, the corpus browser and the capture extension
# all retrieve through: you would stop ingestion by breaking retrieval. So signal only the executor.
#
# SIGSTOP/SIGCONT is a clean, reversible freeze: the process keeps its memory and its place, and the
# work queue lives in Redis (which persists), so nothing is lost — the same property that let the
# stack survive the power cut. Long pauses may let RAGFlow redeliver an in-flight page task; that is
# idempotent by design (dedup on re-trigger), so it self-heals.
set -euo pipefail

CONTAINER="${ORACLE_RAGFLOW_CONTAINER:-docker-ragflow-cpu-1}"
# Bracket trick: the cmdline of the `bash -c` wrapper we run inside the container CONTAINS the
# pattern, so a naive `pgrep -f task_executor.py` matches its own shell — which reported a phantom
# second "executor" whose PID changed every call and made a successful pause look half-applied.
# Writing it as [t]ask_executor.py matches the real process but never the wrapper's own cmdline.
PATTERN='[t]ask_executor.py'
# The signal target must be the literal name (pkill takes a pattern, not our bracketed form).
KILLPAT='task_executor.py'

usage() {
	cat <<EOF
usage: $(basename "$0") {pause|resume|status}

  pause    SIGSTOP the parser  (API/retrieval stay up; queue is preserved in Redis)
  resume   SIGCONT the parser
  status   show executor state + host CPU
EOF
}

executor_pids() {
	docker exec "$CONTAINER" bash -c "pgrep -f '$PATTERN' || true" 2>/dev/null | tr -d '\r'
}

# T = stopped, R/S/D = running/sleeping. Report the raw state so "paused" is observable, not assumed.
executor_state() {
	docker exec "$CONTAINER" bash -c \
		"for p in \$(pgrep -f '$PATTERN' || true); do awk '{print \$3}' /proc/\$p/stat 2>/dev/null; done" \
		2>/dev/null | tr -d '\r'
}

# Report INSTANTANEOUS system CPU, not the process's %CPU.
# `ps pcpu` is cpu-time/elapsed — a LIFETIME AVERAGE. After 22 h at ~20 cores it still printed
# "2012% CPU" for a process that was already stopped, i.e. the tool said the pause had failed when
# it had worked. Idle% sampled over a second is the honest signal: it answers the question actually
# being asked ("did the machine get its cores back?").
host_cpu() {
	top -bn2 -d1 2>/dev/null | grep "^%Cpu" | tail -1 | sed 's/^/  /'
}

case "${1:-status}" in
pause)
	pids="$(executor_pids)"
	[ -n "$pids" ] || {
		echo "no $PATTERN running in $CONTAINER"
		exit 0
	}
	docker exec "$CONTAINER" bash -c "pkill -STOP -f '$KILLPAT'"
	sleep 1
	echo "paused executor pid(s): $(echo "$pids" | tr '\n' ' ')"
	echo "state: $(executor_state | tr '\n' ' ')  (T = stopped)"
	echo "API still serving:"
	curl -sf --max-time 5 -o /dev/null -w "  ragflow :9380 -> %{http_code}\n" \
		http://localhost:9380/api/v1/datasets?page_size=1 \
		-H "Authorization: Bearer ${ORACLE_RAGFLOW_KEY:-ragflow-smywlJs3drgGxfKztifTmD3iNJ2lP6Uvq2-suiLQTGM}" ||
		echo "  ragflow :9380 -> unreachable"
	;;
resume)
	pids="$(executor_pids)"
	[ -n "$pids" ] || {
		echo "no $PATTERN running in $CONTAINER"
		exit 0
	}
	docker exec "$CONTAINER" bash -c "pkill -CONT -f '$KILLPAT'"
	sleep 1
	echo "resumed executor pid(s): $(echo "$pids" | tr '\n' ' ')"
	echo "state: $(executor_state | tr '\n' ' ')  (S/R = running)"
	;;
status)
	pids="$(executor_pids)"
	if [ -z "$pids" ]; then
		echo "executor: NOT RUNNING"
	else
		echo "executor pid(s): $(echo "$pids" | tr '\n' ' ')"
		echo "state: $(executor_state | tr '\n' ' ')  (T = paused, S/R = running)"
	fi
	echo "host CPU:"
	host_cpu
	;;
*)
	usage
	exit 1
	;;
esac
