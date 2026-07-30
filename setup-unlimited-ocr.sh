#!/usr/bin/env bash
# setup-unlimited-ocr.sh - install baidu/Unlimited-OCR into its own venv, weights on /mnt/data.
#
#   ./setup-unlimited-ocr.sh            create venv, install deps, fetch weights
#   ./setup-unlimited-ocr.sh --check    report what is present, install nothing
#
# ## Why a separate venv
#
# The model needs transformers 4.57.1 and torch 2.10. The reranker service is pinned to transformers
# 4.48.3 because v5 removed create_position_ids_from_input_ids and that breaks GTE/jina RoPE. Those
# cannot share an environment. System python is 3.14 and the model wants 3.12, so uv pins that too.
#
# ## Why /mnt/data
#
# Weights are ~6 GB and the venv with CUDA wheels is another ~5 GB. /mnt/data has the room and is
# where the rest of the corpus lives. Home stays clean.
#
# ## What it is for
#
# 51 arXiv papers have a broken text layer: pdftotext emitted NUL bytes because the CID font map is
# damaged, so the characters it returned are not what the page says. Per corpus policy those are kept
# and failed visibly rather than stripped, and they wait in corpus/arxiv-needs-ocr.txt for a real OCR
# pass. This is that pass.
set -uo pipefail

ROOT="${OCR_ROOT:-/mnt/data/models/unlimited-ocr}"
VENV="$ROOT/venv"
WEIGHTS="$ROOT/weights"
MODEL="baidu/Unlimited-OCR"
PY="${OCR_PYTHON:-3.12}"

CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

say() { printf '%s\n' "$*"; }

report() {
	say "venv      $([ -x "$VENV/bin/python" ] && echo "present  $("$VENV/bin/python" --version 2>&1)" || echo MISSING)"
	if [ -x "$VENV/bin/python" ]; then
		say "torch     $("$VENV/bin/python" -c 'import torch;print(torch.__version__, "cuda", torch.version.cuda, "avail", torch.cuda.is_available())' 2>&1 | head -1)"
		say "transformers $("$VENV/bin/python" -c 'import transformers;print(transformers.__version__)' 2>&1 | head -1)"
	fi
	if [ -d "$WEIGHTS" ]; then
		say "weights   $(du -sh "$WEIGHTS" 2>/dev/null | cut -f1) in $WEIGHTS"
	else
		say "weights   MISSING"
	fi
}

if [ "$CHECK" -eq 1 ]; then
	report
	exit 0
fi

command -v uv >/dev/null || {
	say "uv not on PATH"
	exit 1
}

mkdir -p "$ROOT"

if [ ! -x "$VENV/bin/python" ]; then
	say "creating venv (python $PY) at $VENV"
	uv venv --python "$PY" "$VENV" || exit 1
fi

# torch first and on its own. It pulls the CUDA runtime wheels, and letting the resolver consider it
# alongside everything else turns a 3 GB download into a long backtracking search.
say "installing torch"
VIRTUAL_ENV="$VENV" uv pip install --quiet torch torchvision || exit 1

say "installing transformers and friends"
# pymupdf renders PDF pages to images: the source documents here are PDFs, and the model takes
# images. addict/easydict are required by the model's trust_remote_code path.
VIRTUAL_ENV="$VENV" uv pip install --quiet \
	"transformers==4.57.1" accelerate safetensors pillow pymupdf einops \
	tokenizers huggingface_hub addict easydict matplotlib || exit 1

if [ ! -d "$WEIGHTS" ] || [ -z "$(ls -A "$WEIGHTS" 2>/dev/null)" ]; then
	say "fetching $MODEL weights to $WEIGHTS (~6 GB)"
	mkdir -p "$WEIGHTS"
	VIRTUAL_ENV="$VENV" "$VENV/bin/python" -c "
from huggingface_hub import snapshot_download
snapshot_download('$MODEL', local_dir='$WEIGHTS', max_workers=4)
" || exit 1
fi

say
report
say
say "next: ./ocr-unlimited.py --list corpus/arxiv-needs-ocr.txt --dry-run"
