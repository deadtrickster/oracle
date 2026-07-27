#!/usr/bin/env bash
# oracle-vram.sh — own the ONE big GPU slot: text model XOR vision model.
#
# The card is 24 GB. The text model (qwen3-coder-next, MoE offload) sits at ~20.6 GB; qwen3-vl is
# ~17 GB. 20.6 + 17 does not fit, so running both means either a CUDA OOM or a silent spill to
# system memory that turns a 10 s page into minutes. They are mutually exclusive by arithmetic, not
# by policy — so make the exclusivity explicit and switchable instead of leaving it to whoever
# starts a server last.
#
# bge-m3 (embeddings, ~660 MB, keep_alive=forever) is deliberately NOT touched: it coexists with
# either big model and BOTH retrieval and the H17 topic memory need it.
#
#   ./oracle-vram.sh text     # text resident  -> ask/explain/fact-check work, vision does not
#   ./oracle-vram.sh vl       # vision resident-> screenshot→vision works, ask/explain do not
#   ./oracle-vram.sh none     # free the slot  (gaming, or a big CPU job)
#   ./oracle-vram.sh status
#
# Nothing here polls a flag file: the capture receiver PROBES :18081, so it notices a swap by
# itself. One source of truth — what is actually listening.
set -uo pipefail

TEXT_UNIT="oracle-qwen-next"
VL_UNIT="oracle-qwen-vl"
TEXT_URL="http://127.0.0.1:18080/health"
VL_URL="http://127.0.0.1:18081/health"
# Loading is DISK-bound, not GPU-bound: the text model runs with --no-mmap and MoE expert offload,
# so starting it reads tens of GB into RAM — and right after a swap the page cache is cold because
# the other model just evicted it. 180 s was far too short; the script reported failure while the
# model was still loading and became healthy a minute later with nobody listening.
WAIT_S="${ORACLE_VRAM_WAIT:-900}"

usage() {
	sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'
	exit 1
}

healthy() { curl -sf --max-time 3 "$1" >/dev/null 2>&1; }

vram() {
	nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null |
		awk -F'[ ,]+' '{printf "  VRAM %s / %s MiB used\n", $1, $3}'
}

# Stop and WAIT for the memory to actually come back. `systemctl stop` returns when the process is
# reaped, but the driver can take a moment to release; starting the other model too early is exactly
# how you get an OOM that looks random.
stop_unit() {
	local unit="$1" url="$2"
	systemctl --user is-active --quiet "$unit" || return 0
	echo "→ stopping $unit"
	systemctl --user stop "$unit"
	for _ in $(seq 30); do
		healthy "$url" || return 0
		sleep 1
	done
	echo "  WARNING: $unit still answering on its port" >&2
}

# Readiness is HEALTH, not exit code: llama-server accepts the socket long before the weights are
# loaded and 503s meanwhile, so `systemctl start` returning success proves nothing.
start_unit() {
	local unit="$1" url="$2"
	echo "→ starting $unit (loading weights; first load reads from disk)"
	systemctl --user start "$unit" || {
		echo "  FAILED to start $unit" >&2
		return 1
	}
	for _ in $(seq "$WAIT_S"); do
		if healthy "$url"; then
			echo "  ready"
			return 0
		fi
		if ! systemctl --user is-active --quiet "$unit"; then
			echo "  $unit died while loading — journalctl --user -u $unit -n 40" >&2
			return 1
		fi
		sleep 1
	done
	echo "  timed out after ${WAIT_S}s waiting for $url" >&2
	return 1
}

case "${1:-status}" in
text)
	stop_unit "$VL_UNIT" "$VL_URL"
	start_unit "$TEXT_UNIT" "$TEXT_URL" || {
		echo "rolling back: nothing resident" >&2
		exit 1
	}
	vram
	;;
vl)
	stop_unit "$TEXT_UNIT" "$TEXT_URL"
	if ! start_unit "$VL_UNIT" "$VL_URL"; then
		# Never leave the box with NO model: a failed swap must restore what it evicted, or one
		# bad vision request silently takes the whole assistant down.
		echo "vision failed to load — restoring the text model" >&2
		start_unit "$TEXT_UNIT" "$TEXT_URL"
		exit 1
	fi
	vram
	;;
none)
	stop_unit "$VL_UNIT" "$VL_URL"
	stop_unit "$TEXT_UNIT" "$TEXT_URL"
	vram
	;;
status)
	printf "  %-18s %s\n" "text (:18080)" "$(healthy "$TEXT_URL" && echo RESIDENT || echo -)"
	printf "  %-18s %s\n" "vision (:18081)" "$(healthy "$VL_URL" && echo RESIDENT || echo -)"
	printf "  %-18s %s\n" "embeddings" "$(curl -sf --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && echo up || echo -)"
	vram
	;;
*) usage ;;
esac
