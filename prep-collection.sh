#!/usr/bin/env bash
# Prepare your PERSONAL collection into clean markdown for RAG. Run ONLINE.
# Inputs you provide:
#   corpus/links/bookmarks.html   (browser export)  OR  corpus/links/urls.txt
#   corpus/papers_raw/*.pdf        (two-column scientific PDFs)
#   corpus/books_raw/*.{epub,pdf}  (books)
# Output: clean .md/.txt under corpus/{links,papers,books} → upload into RAGFlow.
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p corpus/{links,papers,papers_raw,books,books_raw}

echo "== tool check =="
command -v marker      >/dev/null || echo "  missing: uv tool install marker-pdf        (GPU PDF→md, columns/math)"
command -v trafilatura >/dev/null || echo "  missing: uv tool install trafilatura        (URL→clean md)"
command -v pandoc      >/dev/null || echo "  missing: sudo apt install pandoc poppler-utils"

# 1) bookmarks / links ---------------------------------------------------
if [ -f corpus/links/bookmarks.html ] && [ ! -s corpus/links/urls.txt ]; then
  grep -oiP 'href="\Khttps?://[^"]+' corpus/links/bookmarks.html | sort -u > corpus/links/urls.txt
  echo "  extracted $(wc -l < corpus/links/urls.txt) urls from bookmarks.html"
fi
if [ -s corpus/links/urls.txt ] && command -v trafilatura >/dev/null; then
  echo "== fetching + cleaning links (trafilatura) =="
  trafilatura --input-file corpus/links/urls.txt --output-dir corpus/links --markdown 2>/dev/null \
    && echo "  ✓ corpus/links/*.md"
fi

# 2) two-column scientific PDFs (marker, GPU) ---------------------------
if ls corpus/papers_raw/*.pdf >/dev/null 2>&1 && command -v marker >/dev/null; then
  echo "== converting scientific PDFs (marker, GPU) =="
  marker corpus/papers_raw --output_dir corpus/papers 2>/dev/null && echo "  ✓ corpus/papers/"
fi

# 3) books --------------------------------------------------------------
for f in corpus/books_raw/*.epub; do [ -e "$f" ] || continue
  pandoc "$f" -o "corpus/books/$(basename "${f%.epub}").md" 2>/dev/null && echo "  ✓ $(basename "$f")"
done
for f in corpus/books_raw/*.pdf; do [ -e "$f" ] || continue
  if command -v marker_single >/dev/null; then marker_single "$f" --output_dir corpus/books 2>/dev/null
  else pdftotext -layout "$f" "corpus/books/$(basename "${f%.pdf}").txt" 2>/dev/null; fi
  echo "  ✓ $(basename "$f")"
done

echo; echo "DONE. Review, then upload corpus/{links,papers,books}/* into RAGFlow KBs (Step 4)."
du -sh corpus/{links,papers,books} 2>/dev/null | sed 's/^/  /'
