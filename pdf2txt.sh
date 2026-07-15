#!/usr/bin/env bash
# PDF -> text WITH PAGE MARKERS, for corpus ingestion.
#
# Why not just `pdftotext`: RAGFlow's "naive" (text) parser stores no page positions —
# every chunk gets the stub `positions=[[2,1,1,1,1]]`. Only the DeepDoc PDF parsers
# (chunk_method book/paper) record real page+bbox. But DeepDoc garbles Cyrillic CID fonts
# (Новиков -> HOBMKOB), so Russian books MUST go through pdftotext.
#
# So we carry the page number in the TEXT itself: pdftotext already separates pages with a
# form feed (\f); we turn each break into a literal `[[p.N]]` line. The marker survives
# chunking, so every chunk knows its page — which gives the corpus browser a deep link into
# the original PDF, and lets ask_corpus cite "Chebyshev, p. 412" instead of just "Chebyshev".
#
#   ./pdf2txt.sh <input.pdf> <output.txt>
set -euo pipefail
PDF="${1:?usage: pdf2txt.sh <input.pdf> <output.txt>}"
OUT="${2:?usage: pdf2txt.sh <input.pdf> <output.txt>}"

# RS="\f" splits on the page break pdftotext emits; NR is then the 1-based page number.
pdftotext -layout "$PDF" - \
  | awk 'BEGIN { RS = "\f" } { printf "[[p.%d]]\n%s", NR, $0 }' > "$OUT"

pages=$(grep -c '^\[\[p\.' "$OUT" || true)
echo "[$(date +%H:%M:%S)] $(basename "$PDF") -> $OUT ($pages pages, $(wc -c < "$OUT") bytes)"
