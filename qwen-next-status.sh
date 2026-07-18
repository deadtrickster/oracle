#!/usr/bin/env bash
# qwen-next-status — what is the tuned qwen-next llama-server doing right now?
# Shows service health, slot/context config, current activity (prompt-processing vs
# token-generation + rate), GPU/VRAM, and the worker process. Read-only; safe to run anytime.
#
#   qwen-next-status           one snapshot
#   qwen-next-status -w        watch, refresh every 3s (Ctrl-C to stop)
#   qwen-next-status -w 10     watch every 10s
#
# Override the endpoint with ORACLE_QWEN_NEXT_URL (default http://127.0.0.1:18080).
set -uo pipefail

URL="${ORACLE_QWEN_NEXT_URL:-http://127.0.0.1:18080}"
UNIT="oracle-qwen-next"

snapshot() {
	local active recent cfg gpu proc last ts_now ts_last busy="idle"

	# 1) service ---------------------------------------------------------
	active="$(systemctl --user is-active "$UNIT" 2>/dev/null || true)"
	printf '  service : %s (%s)\n' "$UNIT" "${active:-unknown}"
	if [ "$active" != "active" ]; then
		printf '  (not running — start with: systemctl --user start %s)\n' "$UNIT"
		return
	fi

	# 2) slot/context config (from the last model load) ------------------
	cfg="$(journalctl --user -u "$UNIT" --no-pager 2>/dev/null |
		grep -oE 'n_slots = [0-9]+, n_ctx_slot = [0-9]+' | tail -1)"
	[ -n "$cfg" ] && printf '  config  : %s\n' "$cfg"

	# 3) current activity — last timing line that actually carries a rate --
	last="$(journalctl --user -u "$UNIT" --no-pager -n 200 2>/dev/null |
		grep 'tokens per second' | tail -1)"
	if [ -n "$last" ]; then
		ts_last="$(date -d "$(printf '%s' "$last" | awk '{print $1, $2, $3}')" +%s 2>/dev/null || echo 0)"
		ts_now="$(date +%s)"
		[ "$ts_last" -gt 0 ] && [ $((ts_now - ts_last)) -lt 12 ] && busy="BUSY"
	fi
	if [ "$busy" = "BUSY" ]; then
		local kind rate prog
		case "$last" in
		*"prompt processing"* | *"prompt eval"*) kind="prompt-processing (PP)" ;;
		*"eval time"*) kind="token-generation (TG)" ;;
		*) kind="processing" ;;
		esac
		rate="$(printf '%s' "$last" | grep -oE '[0-9.]+ tokens per second' | tail -1)"
		prog="$(printf '%s' "$last" | grep -oE 'progress = [0-9.]+' | tail -1)"
		printf '  state   : BUSY — %s' "$kind"
		[ -n "$prog" ] && printf '  %s' "$prog"
		[ -n "$rate" ] && printf '  @ %s' "$rate"
		printf '\n'
	else
		printf '  state   : idle (no task in the last 12s)\n'
	fi

	# 4) live slot state (best-effort; endpoint may omit fields) ----------
	recent="$(curl -s --max-time 3 "$URL/slots" 2>/dev/null |
		python3 -c 'import sys,json
try: d=json.load(sys.stdin)
except Exception: sys.exit()
for s in d: print("  slot %s : n_ctx=%s n_past=%s" % (s.get("id"), s.get("n_ctx"), s.get("n_past")))' 2>/dev/null || true)"
	[ -n "$recent" ] && printf '%s\n' "$recent"

	# 5) GPU + worker process --------------------------------------------
	gpu="$(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total \
		--format=csv,noheader 2>/dev/null | head -1)"
	[ -n "$gpu" ] && printf '  gpu     : %s\n' "$gpu"
	proc="$(ps -o %cpu,stat,etime -C llama-server --no-headers 2>/dev/null | sort -rn | head -1)"
	[ -n "$proc" ] && printf '  process : %%cpu/stat/uptime = %s\n' "$(printf '%s' "$proc" | tr -s ' ')"
}

main() {
	if [ "${1:-}" = "-w" ] || [ "${1:-}" = "--watch" ]; then
		local secs="${2:-3}"
		while true; do
			clear
			printf '== qwen-next @ %s ==  (%s)\n' "$URL" "$(date '+%H:%M:%S')"
			snapshot
			sleep "$secs"
		done
	else
		printf '== qwen-next @ %s ==\n' "$URL"
		snapshot
	fi
}

main "$@"
