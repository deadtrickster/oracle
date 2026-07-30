#!/usr/bin/env bash
# bump-ollama-parallel.sh — give Ollama more than one embedding lane.
#
#   sudo ./bump-ollama-parallel.sh          set OLLAMA_NUM_PARALLEL (default 8)
#   sudo ./bump-ollama-parallel.sh 4        pick the value
#   sudo ./bump-ollama-parallel.sh --revert remove the drop-in, back to stock
#
# ## Why
#
# Profiled 2026-07-30 with profile-ingest.sh. Eight RAGFlow task executors were all blocked in the
# same place — `read (httpcore/_backends/sync.py:128)`, waiting on HTTP — while the runner Ollama
# spawns for bge-m3 was started with `-np 1`. One parallel slot. Eight producers queueing behind it.
#
# That is why raising RAGFlow's `--workers` from 1 to 8 bought 3.8x rather than 8x: the extra workers
# did not get more embedding throughput, they got a longer line. It also explains the shape of the
# load — 39% of a 24-core box spent in ollama + llama-server, for a model `ollama ps` calls 100% GPU.
#
# `-np` comes from OLLAMA_NUM_PARALLEL, which is unset here and defaults to 1.
#
# ## Why a drop-in and not an edit to the unit
#
# The unit is `/etc/systemd/system/ollama.service`, installed by Ollama, and a package update
# overwrites it — the same class of failure as patching a file inside a container instead of mounting
# it, which cost us a silently reverted tiktoken fix earlier tonight. A drop-in under
# `ollama.service.d/` survives upgrades and is trivially revertible.
#
# ## VRAM
#
# More slots cost more KV cache. bge-m3 is small (982 MB resident) and its context is 8192, so eight
# slots is a few hundred MB — but this is only safe because oracle-qwen-next.service was stopped
# first, taking VRAM from 21.7/24.4 GB down to 1.0 GB. If that model is running, use a smaller value
# or expect Ollama to fall back to CPU, which would make everything dramatically worse.
set -uo pipefail

DROPIN_DIR=/etc/systemd/system/ollama.service.d
DROPIN="$DROPIN_DIR/oracle-parallel.conf"

if [ "${1:-}" = "--revert" ]; then
	rm -f "$DROPIN"
	systemctl daemon-reload
	systemctl restart ollama
	echo "reverted — drop-in removed, ollama restarted"
	exit 0
fi

N="${1:-8}"
case "$N" in
'' | *[!0-9]*)
	echo "usage: sudo $0 [N|--revert]" >&2
	exit 2
	;;
esac

if [ "$(id -u)" -ne 0 ]; then
	echo "needs root (ollama is a system unit):  sudo $0 $*" >&2
	exit 1
fi

free_mib="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | head -1)"
echo "GPU free before: ${free_mib:-unknown} MiB"
if [ -n "$free_mib" ] && [ "$free_mib" -lt 4000 ]; then
	echo "WARNING: under 4 GB free. More slots need more KV cache, and if Ollama cannot fit the"
	echo "         model it silently falls back to CPU — far slower than the single slot you have."
	echo "         Stop the big model first:  systemctl --user stop oracle-qwen-next.service"
fi

mkdir -p "$DROPIN_DIR"
cat >"$DROPIN" <<EOF
# Written by oracle/bump-ollama-parallel.sh
#
# Ollama defaults to one parallel slot, so llama-server starts with -np 1 and every concurrent
# embedding request serialises. With 8 RAGFlow task executors that is the binding constraint on
# ingest throughput — measured: all 8 blocked in httpcore read(), waiting.
[Service]
Environment="OLLAMA_NUM_PARALLEL=$N"
EOF

systemctl daemon-reload
systemctl restart ollama

# Wait for the API, then force a model load — the runner is spawned lazily, so without a request
# there is no llama-server whose command line we could check.
for _ in $(seq 1 30); do
	curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
	sleep 1
done
curl -sf http://127.0.0.1:11434/api/embed \
	-d '{"model":"bge-m3","input":"warm up the runner so -np is observable"}' >/dev/null 2>&1
sleep 3

echo
echo "verifying by reading the runner's actual command line, not the config we just wrote:"
if pgrep -af "llama-server.*-np" | head -3 | grep -o -- "-np [0-9]*" | head -1 | grep -q "$N"; then
	echo "  OK: -np $N"
else
	echo "  could not confirm -np $N — current runners:"
	pgrep -af "llama-server" | sed 's/^/    /' | cut -c1-160
fi
echo
echo "GPU free after: $(nvidia-smi --query-gpu=memory.free --format=csv,noheader 2>/dev/null | head -1)"
echo
echo "then restart the feeder and watch throughput:"
echo "  systemctl --user start oracle-arxiv-ingest.service"
