#!/usr/bin/env bash
# Ollama install + service config (PLAN.md Step 1). Needs sudo — run interactively.
# After this, run: bash pull-models.sh   (no sudo needed)
set -euo pipefail

if ! command -v ollama >/dev/null 2>&1; then
  echo "== installing ollama =="
  curl -fsSL https://ollama.com/install.sh | sh
else
  echo "== ollama already installed: $(ollama --version 2>/dev/null) =="
fi

echo "== systemd override (Docker reachability + KV cache + context) =="
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_HOST=0.0.0.0"            # reachable from Docker (mind untrusted networks/firewall)
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"      # halves KV cache -> more context in 24 GB
Environment="OLLAMA_FLASH_ATTENTION=1"       # required for quantized KV cache
Environment="OLLAMA_CONTEXT_LENGTH=32768"    # server-side default ctx
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now ollama
sudo systemctl restart ollama

sleep 2
echo "== sanity =="
curl -s http://localhost:11434/api/version && echo " <- ollama answers"
echo "OK. Now run: bash pull-models.sh"
