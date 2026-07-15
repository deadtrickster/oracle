#!/usr/bin/env bash
# OCR a SCANNED (image-only) PDF to text.
#
# Some PDFs have no text layer at all — 0 extractable chars, 0 embedded fonts (check with
# `pdffonts` / `pdftotext | wc -c`). pdftotext returns nothing for those; the pages are just
# images. This renders each page and runs tesseract over it.
#
#   ./ocr-pdf.sh <input.pdf> <output.txt> [lang]      lang default: rus (needs tesseract-ocr-<lang>)
#   OCR_JOBS=8 ./ocr-pdf.sh book.pdf out.txt rus      # more parallelism
#
# Deliberately modest parallelism by default (4) so it doesn't starve RAGFlow's parser.
set -uo pipefail
PDF="${1:?usage: ocr-pdf.sh <input.pdf> <output.txt> [lang]}"
OUT="${2:?usage: ocr-pdf.sh <input.pdf> <output.txt> [lang]}"
LANG="${3:-rus}"
JOBS="${OCR_JOBS:-4}"

pages=$(pdfinfo "$PDF" 2>/dev/null | awk '/^Pages/{print $2}')
[ -z "${pages:-}" ] && { echo "cannot read $PDF"; exit 1; }
tesseract --list-langs 2>/dev/null | grep -qx "$LANG" || {
  echo "tesseract lang '$LANG' missing — install tesseract-ocr-$LANG"; exit 1; }

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
echo "[$(date +%H:%M:%S)] OCR $(basename "$PDF"): $pages pages, lang=$LANG, $JOBS workers"

# render + OCR each page independently (parallel), one temp image at a time so we never
# materialise thousands of PNGs at once
seq 1 "$pages" | xargs -P "$JOBS" -I{} bash -c '
  p="$1"; pdf="$2"; tmp="$3"; lang="$4"
  pdftoppm -r 300 -gray -f "$p" -l "$p" -png "$pdf" "$tmp/img$p" 2>/dev/null
  img=$(ls "$tmp/img$p"*.png 2>/dev/null | head -1)
  [ -n "$img" ] && tesseract "$img" "$tmp/txt$p" -l "$lang" 2>/dev/null
  rm -f "$tmp/img$p"*.png
' _ {} "$PDF" "$tmp" "$LANG"

# Stitch pages back in order, tagging each with a `[[p.N]]` marker — same convention as
# pdf2txt.sh, so text-ingested books still carry page numbers (RAGFlow's naive parser stores
# no positions of its own). See pdf2txt.sh for why.
for p in $(seq 1 "$pages"); do
  echo "[[p.$p]]"
  cat "$tmp/txt$p.txt" 2>/dev/null
  echo
done > "$OUT"
echo "[$(date +%H:%M:%S)] -> $OUT ($(wc -c < "$OUT" | tr -d ' ') bytes)"
