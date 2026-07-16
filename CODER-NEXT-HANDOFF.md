# Handoff — add Qwen3-Coder-Next as a local model (`qwen-next`)

**Status (2026-07-16): CODE WIRED. Only the download + smoke test remain (bandwidth-gated).**
Resumed in Oracle (it was started in the wrong session — a RAGFlow-PR session). Steps 2 & 3 below are
done, shellcheck/shfmt-clean, and verified with a stubbed `claude`: `qwen-next` resolves to
`qwen3-coder-next` + `~/.claude-next` (own history), the fast slot stays on the 30B, and plain `qwen`
is unchanged (`qwen3-coder:30b` + `~/.claude-local`). What's LEFT: step 1 (`ollama pull` + `create`)
and step 4 (smoke test) — both need the model, i.e. real wifi.

Goal: register `qwen3-coder-next` in Ollama and add a `qwen-next` launcher that mirrors `qwen`
(`~/bin/qwen` → `qwen.sh`) but with its **own history** — exactly like qwen's `~/.claude-local`.

## Decisions (settled)
- **Quant: Unsloth `UD-Q4_K_XL` (49.6 GB, single GGUF).** Unsloth-dynamic Q4; the r/LocalLLaMA
  sweet spot on a 24 GB card (~30 tok/s). Alternatives: `UD-IQ4_XS` (38.4 GB, more fits on GPU →
  faster), `UD-Q5_K_XL` (59.5 GB, more quality). Avoid Q8/BF16 (a 5090 user got 6–9 tok/s).
- **Runs MoE-offload, NOT GPU-only.** 49.6 GB > 24 GB VRAM, so attention/dense on GPU, the ~50 GB
  of experts in the 125 GB RAM (it's 3B-active MoE, so this is still fast). Ollama auto-offloads and
  loads on demand (unloading the 30B). This deliberately departs from DESIGN's "24 GB holds the LLM
  and nothing else" — `qwen-next` uses GPU + ~50 GB RAM.
- **temp 0** (deterministic, matches precision-over-speed). Unsloth *recommends 1.0* (emphasized) —
  one-line flip in the Modelfile; the top_p/top_k/min_p are already set for that case.
- **num_ctx 131072** (128K; KV in RAM in offload mode). Server already sets
  `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0`.

## Staged
- **`~/models/qwen3-coder-next/Modelfile`** — ready. `FROM hf.co/unsloth/Qwen3-Coder-Next-GGUF:UD-Q4_K_XL`,
  mirrors the 30B's `RENDERER qwen3-coder` + `PARSER qwen3-coder`, temp 0, num_ctx 128K, stops.
- Sources saved: `~/Documents/Qwen3-Coder-Next_ How to Run Locally _ Unsloth Documentation.pdf`,
  `~/Documents/Qwen3 Coder Next … r_LocalLLaMA.pdf`.

## Resume steps
1. **Download + register** (on decent wifi) — **THE ONLY BLOCKER LEFT**:
   `ollama pull hf.co/unsloth/Qwen3-Coder-Next-GGUF:UD-Q4_K_XL` (validated; ref resolves; resumable),
   then `ollama create qwen3-coder-next -f ~/models/qwen3-coder-next/Modelfile`.
2. ✅ **DONE** — `qwen.sh:27` is now `export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-qwen3-coder:30b}"`
   (backward-compatible; plain `qwen` still runs the 30B).
3. ✅ **DONE** — `qwen-next.sh` created (shellcheck+shfmt clean), symlinked at `~/bin/qwen-next`. It
   overrides `ANTHROPIC_MODEL=qwen3-coder-next`, `CLAUDE_CONFIG_DIR=…/.claude-next`, keeps the fast
   slot on the 30B, and execs `qwen.sh`. Verified with a stubbed `claude`.
4. **Smoke test (after step 1):** `ollama run qwen3-coder-next "write a bash one-liner …"` — watch for
   **looping** (see caveat), then `qwen-next` in a repo and confirm tool calls work via the shim.

## Caveats / notes
- **llama.cpp version matters.** Unsloth: a Qwen3-Next looping bug (`key_gdiff`) was fixed Feb 4 and
  tool-call parsing Feb 19 — use updated GGUFs + recent llama.cpp/Ollama. **Ollama 0.31.2 is current
  (post-Feb), so it should be fine — but verify no loops** on the smoke test; if it loops, update Ollama.
- **Download is the only blocker.** HF throttles ~1.16 MB/s single-stream (~12 h for 49.6 GB); ~2.24
  MB/s across 3 parallel streams → throttle is roughly per-connection, so `aria2c -x16` (not installed:
  `sudo apt install aria2`) or `hf_transfer` will help on real wifi. Hotel uplink caps total anyway.
- **The shim needs no change.** `oracle-claude-shim.py` (:11435) is stateless, passes the model name
  through, and salvages leaked tool calls — so it covers `qwen-next` for free. "Separate history" =
  `CLAUDE_CONFIG_DIR`, nothing more.
