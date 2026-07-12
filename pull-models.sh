#!/usr/bin/env bash
# Pull the models (PLAN.md Step 1). ~32 GB total. No sudo needed.
set -euo pipefail
ollama pull qwen3-coder:30b   # ~19 GB - PRIMARY
ollama pull codestral         # ~13 GB - lighter/battery (optional but plan lists it)
echo "== smoke test (loads on GPU) =="
ollama run qwen3-coder:30b "Say OK and nothing else."
nvidia-smi --query-gpu=memory.used --format=csv
