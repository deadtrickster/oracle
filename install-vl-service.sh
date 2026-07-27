#!/usr/bin/env bash
# Install qwen3-vl as a systemd USER service, mirroring oracle-qwen-next.
#
# Until now the vision server was started by transcribe-scans.py with a bare Popen that
# "never kills anything" — correct when it was the only claimant, but it cannot be stopped by
# anything else, does not survive a reboot, and is invisible to the tooling that manages every other
# service here. The VRAM switch has to be able to STOP it, so it needs to be a unit.
#
# Flags are copied from transcribe-scans.py's launcher (the Unsloth-recommended sampler settings,
# incl. presence-penalty 1.5 as OCR anti-repetition), so the model behaves identically whether it is
# driven by the transcription lane or by the browser extension.
set -euo pipefail

MODEL_DIR="$HOME/models/qwen3-vl"
UNIT="$HOME/.config/systemd/user/oracle-qwen-vl.service"
LLAMA="/usr/local/lib/ollama/llama-server"

[ -r "$MODEL_DIR/Qwen3-VL-30B-A3B-Instruct-UD-Q4_K_XL.gguf" ] || {
	echo "VL model not found under $MODEL_DIR" >&2
	exit 1
}
[ -x "$LLAMA" ] || {
	echo "llama-server not found at $LLAMA" >&2
	exit 1
}

mkdir -p "$(dirname "$UNIT")"
cat >"$UNIT" <<EOF
[Unit]
Description=Oracle qwen3-vl vision server (:18081)
After=network.target

[Service]
Environment=LD_LIBRARY_PATH=/usr/local/lib/ollama/cuda_v13:/usr/local/lib/ollama
Environment=GGML_BACKEND_PATH=/usr/local/lib/ollama/cuda_v13/libggml-cuda.so
ExecStart=$LLAMA \\
    --model $MODEL_DIR/Qwen3-VL-30B-A3B-Instruct-UD-Q4_K_XL.gguf \\
    --mmproj $MODEL_DIR/mmproj-F16.gguf \\
    --host 127.0.0.1 --port 18081 --alias qwen3-vl \\
    -ngl 99 -c 16384 --flash-attn on --jinja --no-webui \\
    --temp 0.7 --top-p 0.8 --top-k 20 --min-p 0.0 --presence-penalty 1.5
Restart=no

[Install]
WantedBy=default.target
EOF

# Deliberately NOT enabled: the VRAM switch decides which model is resident. An enabled unit would
# start at login and fight the text model for the one big slot.
systemctl --user daemon-reload
echo "installed $UNIT (not enabled — oracle-vram.sh starts/stops it on demand)"
