#!/usr/bin/env bash
# qwen-status — what is Ollama serving for the GPU-only qwen (qwen3-coder:30b)?
# Shows Ollama health, which backend the agent+synth currently point at, the resident
# models (VRAM, GPU/CPU split, context, keep-alive countdown), and GPU/VRAM. Read-only.
# (The qwen-next equivalent is qwen-next-status — that one reads the llama-server journal;
# Ollama exposes no per-request timing, so this reports residency + keep-alive instead.)
#
#   qwen-status        one snapshot
#   qwen-status -w     watch, refresh every 3s
#   qwen-status -w 10  watch every 10s
#
# Override the endpoint with ORACLE_OLLAMA_URL (default http://localhost:11434).
set -uo pipefail

URL="${ORACLE_OLLAMA_URL:-http://localhost:11434}"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/oracle"

snapshot() {
	local ver be gpu

	# 1) ollama health ---------------------------------------------------
	ver="$(curl -s --max-time 3 "$URL/api/version" 2>/dev/null |
		python3 -c 'import sys,json;print(json.load(sys.stdin).get("version","?"))' 2>/dev/null || true)"
	if [ -z "$ver" ]; then
		printf '  ollama : DOWN (%s)\n' "$URL"
		return
	fi
	printf '  ollama : up v%s  (systemd: %s)\n' "$ver" "$(systemctl is-active ollama 2>/dev/null || echo '?')"

	# 2) which backend the agent + synth point at (from the oracle-backend symlink) --
	be="unset"
	[ -L "$CFG/backend.env" ] && be="$(basename "$(readlink "$CFG/backend.env")" | sed 's/^backend-//; s/\.env$//')"
	printf '  backend: agent+synth -> %s\n' "$be"

	# 3) resident models: VRAM, GPU/CPU split, context, keep-alive -------
	curl -s --max-time 3 "$URL/api/ps" 2>/dev/null | python3 -c '
import sys, json, datetime, re
try:
    d = json.load(sys.stdin)
except Exception:
    print("  loaded : (api/ps unavailable)"); sys.exit()
ms = d.get("models", [])
if not ms:
    print("  loaded : none resident (next request cold-loads the model)"); sys.exit()
now = datetime.datetime.now(datetime.timezone.utc)
for m in ms:
    sz = m.get("size", 0) or 0
    vram = m.get("size_vram", 0) or 0
    gpu = round(100 * vram / sz) if sz else 0
    proc = ("%d%% GPU" % gpu) if gpu >= 100 else ("%d%% GPU / %d%% CPU" % (gpu, 100 - gpu))
    ka = re.sub(r"(\.\d{6})\d+", r"\1", m.get("expires_at", ""))  # ns -> us so fromisoformat parses
    try:
        secs = (datetime.datetime.fromisoformat(ka) - now).total_seconds()
        rem = "forever" if secs > 3.15e9 else ("expiring" if secs <= 0 else "%dm%02ds" % (secs // 60, secs % 60))
    except Exception:
        rem = ka[:19] or "?"
    print("  loaded : %-22s %4.1fGB  %-18s ctx=%-6s keep-alive=%s" % (
        m.get("name", "?"), vram / 1e9, proc, m.get("context_length", "?"), rem))
' 2>/dev/null || echo "  loaded : (api/ps parse failed)"

	# 4) GPU -------------------------------------------------------------
	gpu="$(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader 2>/dev/null | head -1)"
	[ -n "$gpu" ] && printf '  gpu    : %s\n' "$gpu"
}

main() {
	if [ "${1:-}" = "-w" ] || [ "${1:-}" = "--watch" ]; then
		local secs="${2:-3}"
		while true; do
			clear
			printf '== qwen (Ollama) @ %s ==  (%s)\n' "$URL" "$(date '+%H:%M:%S')"
			snapshot
			sleep "$secs"
		done
	else
		printf '== qwen (Ollama) @ %s ==\n' "$URL"
		snapshot
	fi
}

main "$@"
