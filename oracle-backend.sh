#!/usr/bin/env bash
# oracle-backend — switch the agent + ask_corpus/ask_code serving backend between:
#   30b   GPU-only qwen3-coder:30b (Ollama)  — use while the CPU is busy (e.g. ingest);
#         all weights stay on the GPU, so it does not fight DeepDoc for CPU. Launch: qwen
#   next  tuned qwen-next (raw llama.cpp, MoE offload) — fast / long-context, but its
#         token-gen uses the CPU. Launch: qwen-next
#
# It flips ONE symlinked EnvironmentFile that both the shim and the ask-bridge read, and
# starts/stops the qwen-next server. No hand-editing of unit files.
#
#   oracle-backend 30b
#   oracle-backend next
#   oracle-backend          # show the current backend
set -euo pipefail

CFG="${XDG_CONFIG_HOME:-$HOME/.config}/oracle"
LINK="$CFG/backend.env"

current() {
	if [ -L "$LINK" ]; then
		basename "$(readlink "$LINK")" | sed 's/^backend-//; s/\.env$//'
	else
		echo "unset"
	fi
}

switch() {
	local name="$1" target="$CFG/backend-$1.env"
	[ -r "$target" ] || {
		echo "missing config: $target" >&2
		exit 1
	}
	ln -sfn "$target" "$LINK"
	case "$name" in
	30b) systemctl --user stop oracle-qwen-next ;;
	next) systemctl --user start oracle-qwen-next ;;
	esac
	# EnvironmentFile is re-read on start, so a restart picks up the flipped symlink.
	systemctl --user restart oracle-claude-shim oracle-ask-bridge
}

case "${1:-}" in
30b)
	switch 30b
	echo "backend -> 30b (Ollama qwen3-coder:30b, GPU-only); qwen-next stopped.  Launch: qwen"
	;;
next)
	switch next
	echo "backend -> next (llama.cpp qwen-next, MoE offload); qwen-next started.  Launch: qwen-next"
	;;
"")
	echo "current backend: $(current)"
	echo "usage: oracle-backend {30b|next}"
	;;
*)
	echo "unknown backend '$1' (use: 30b | next)" >&2
	exit 2
	;;
esac
