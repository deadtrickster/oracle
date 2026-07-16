#!/usr/bin/env bash
# qwen-next — the same local Claude Code as `qwen`, but on the larger `qwen3-coder-next`
# model, with its OWN separate history. See CODER-NEXT-HANDOFF.md.
#
# Everything that makes qwen work (the shim at :11435, the MCP servers, the schema-discipline
# prompt, the tool trim) lives in qwen.sh — this wrapper only overrides the three things that
# differ, then execs qwen.sh. qwen.sh reads all three as `${VAR:-default}`, so setting them in
# the environment here takes precedence:
#   - ANTHROPIC_MODEL          -> the big model (MoE-offload: GPU + ~50 GB RAM; unloads the 30B)
#   - ANTHROPIC_SMALL_FAST_MODEL-> stays on the small GPU-only 30B for the fast/background slot
#   - CLAUDE_CONFIG_DIR        -> ~/.claude-next, so its sessions/memory never mix with qwen's
#
# Prereq (one-time, on real bandwidth — see the handoff):
#   ollama pull hf.co/unsloth/Qwen3-Coder-Next-GGUF:UD-Q4_K_XL
#   ollama create qwen3-coder-next -f ~/models/qwen3-coder-next/Modelfile
export ANTHROPIC_MODEL="qwen3-coder-next"
export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-qwen3-coder:30b}"
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude-next}"

# resolve our real location (may be invoked through ~/bin/qwen-next) and hand off to qwen.sh
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
exec "$HERE/qwen.sh" "$@"
