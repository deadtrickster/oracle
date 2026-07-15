#!/usr/bin/env bash
# Oracle control — free the GPU for gaming, or stop the whole stack for perf testing.
# No sudo needed (ollama stop / docker compose / systemctl --user).
#
#   ./oracle-ctl.sh game    # unload LLM models -> free VRAM (stack stays up, idle). For gaming.
#   ./oracle-ctl.sh stop    # stop EVERYTHING (RAGFlow + services + models). For perf testing.
#   ./oracle-ctl.sh start   # bring the stack back up
#   ./oracle-ctl.sh status  # what's running + VRAM
set -uo pipefail

DOCKER_DIR="$HOME/Projects/oracle/ragflow/docker"
SERVICES=(oracle-ask-bridge oracle-lsp-bridge oracle-claude-shim oracle-ingest-bridge oracle-browser
          oracle-reranker codebase-memory-bridge source-grep-bridge emacs-bridge git-bridge
          oracle-docs)
MODELS=(qwen3-coder:30b bge-m3 codestral)

free_vram() {
  echo "→ unloading Ollama models (freeing VRAM)…"
  for m in "${MODELS[@]}"; do ollama stop "$m" 2>/dev/null; done
  sleep 1
  echo "  VRAM: $(nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader)"
  if [ "$(ollama ps 2>/dev/null | grep -c GPU)" -gt 0 ]; then
    echo "  NOTE: a model reloaded — RAGFlow is probably still parsing/embedding."
    echo "        Use './oracle-ctl.sh stop' to halt that too before gaming."
  fi
}

stop_all() {
  echo "→ stopping the whole Oracle stack…"
  for s in "${SERVICES[@]}"; do systemctl --user stop "$s" 2>/dev/null && echo "  stopped $s"; done
  echo "  stopping RAGFlow containers…"; ( cd "$DOCKER_DIR" && docker compose stop 2>&1 | tail -1 )
  for m in "${MODELS[@]}"; do ollama stop "$m" 2>/dev/null; done
  sleep 1
  echo "  VRAM: $(nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader)"
  echo "  (the Ollama systemd service still runs but idles at ~0 with no model loaded;"
  echo "   for a truly bare machine: sudo systemctl stop ollama)"
}

start_all() {
  echo "→ starting the Oracle stack…"
  ( cd "$DOCKER_DIR" && docker compose start 2>&1 | tail -1 )
  for s in "${SERVICES[@]}"; do systemctl --user start "$s" 2>/dev/null && echo "  started $s"; done
  echo "  done. Models load on first request; warm qwen with: ollama run qwen3-coder:30b hi"
}

resume_all() {
  # Everything you need after a REBOOT. systemd user units + docker come back on their own,
  # but two things do NOT: models aren't resident (lazy-loaded -> slow first query), and any
  # doc that was mid-parse is stuck marked RUNNING with a dead worker — RAGFlow never retries
  # those, so they sit forever until re-queued.
  echo "→ resuming the Oracle stack after a restart…"
  start_all
  echo "→ warming models (so the first query isn't a 21 GB load)…"
  ollama run qwen3-coder:30b "hi" >/dev/null 2>&1 || true
  curl -sf http://localhost:11434/api/embed -d '{"model":"bge-m3","input":"warm"}' >/dev/null 2>&1 || true
  echo "  ✓ qwen + bge-m3 resident"
  echo "→ re-queuing parse tasks orphaned by the restart…"
  ( cd "$(dirname "$0")" && ./requeue-orphans.py )
  echo
  status
}

status() {
  echo "=== Ollama (VRAM):"; ollama ps 2>/dev/null | sed 's/^/  /'
  echo "  $(nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader)"
  echo "=== RAGFlow containers:"; docker ps --filter name=docker- --format '  {{.Names}} {{.Status}}' 2>/dev/null | sort
  echo "=== Oracle services:"; for s in "${SERVICES[@]}"; do printf "  %-24s %s\n" "$s" "$(systemctl --user is-active "$s" 2>/dev/null)"; done
}

case "${1:-}" in
  game|free-vram|vram) free_vram ;;
  stop|perf|down)      stop_all ;;
  start|up)            start_all ;;
  resume|reboot)       resume_all ;;
  status|st)           status ;;
  *) echo "usage: $0 {game|stop|start|resume|status}"
     echo "  game   - unload LLM models -> free VRAM (stack stays up idle). For gaming."
     echo "  stop   - stop EVERYTHING (RAGFlow + services + models). For perf testing."
     echo "  start  - bring the stack back up"
     echo "  resume - AFTER A REBOOT: start + warm models + re-queue parse tasks the restart"
     echo "           orphaned (RAGFlow never retries those on its own)"
     echo "  status - what's running + VRAM" ;;
esac
