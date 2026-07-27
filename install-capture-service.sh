#!/usr/bin/env bash
# Install the oracle-capture receiver as a systemd USER service.
#
# Why a drop-in instead of hardcoding the synth URL: every other Oracle service takes its serving
# backend from ~/.config/oracle/backend.env, which `oracle-backend 30b|next` flips. Baking
# ORACLE_OLLAMA_URL into this unit would silently pin the receiver to one backend while the rest of
# the stack moved — so /ask would answer from a different model than ask_corpus, or from a model
# that isn't even resident. Same file, same switch, one source of truth.
#
# Note the receiver deliberately does NOT take ORACLE_EMBED_URL from backend.env: embeddings must
# come from Ollama proper (:11434), because the llama.cpp synth server has no /api/embed. The
# receiver's built-in default already points there, so leaving it unset is correct.
set -euo pipefail

REPO="$HOME/Projects/oracle"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/oracle-capture.service"
DROPIN_DIR="$UNIT_DIR/oracle-capture.service.d"
BACKEND_ENV="$HOME/.config/oracle/backend.env"

[ -r "$REPO/oracle-capture-receiver.py" ] || {
	echo "receiver not found at $REPO/oracle-capture-receiver.py" >&2
	exit 1
}

mkdir -p "$UNIT_DIR" "$DROPIN_DIR"
chmod +x "$REPO/oracle-capture-receiver.py"

cat >"$UNIT" <<EOF
[Unit]
Description=Oracle capture receiver (Chrome extension endpoint)
After=network.target

[Service]
ExecStart=$REPO/oracle-capture-receiver.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

if [ -r "$BACKEND_ENV" ]; then
	cat >"$DROPIN_DIR/backend.conf" <<'EOF'
# Serving backend for /ask, /explain, /factcheck synthesis — selected by
# ~/.config/oracle/backend.env (flipped by `oracle-backend 30b|next`), exactly as
# oracle-ask-bridge does. Do NOT hand-edit to switch backends.
[Service]
EnvironmentFile=%h/.config/oracle/backend.env
EOF
	echo "  drop-in: backend.env wired (synth follows \`oracle-backend\`)"
else
	echo "  WARNING: $BACKEND_ENV missing — receiver will use its built-in defaults" >&2
fi

systemctl --user daemon-reload
systemctl --user enable --now oracle-capture

echo "--- status ---"
systemctl --user is-active oracle-capture || true
sleep 2
echo "--- health ---"
curl -sf --max-time 5 http://127.0.0.1:8788/health && echo
echo "--- effective config ---"
curl -sf --max-time 20 http://127.0.0.1:8788/status |
	python3 -c 'import json,sys; d=json.load(sys.stdin); print(" ragflow:",d["ragflow"]," synth:",d["synth"]," vision:",d["vision"]); print(" dataset:",d["dataset"]," captures:",d["captures_dir"])'
