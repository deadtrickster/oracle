#!/usr/bin/env python3
"""Rebuild the needs-OCR list by scanning the extracted arXiv text for NUL bytes.

    ./arxiv-scan-nul.py            rewrite corpus/arxiv-needs-ocr.txt from what is on disk
    ./arxiv-scan-nul.py --count    just say how many, change nothing

## Why this is derived, not accumulated

The list used to be appended to as extraction ran, which made it an ever-growing record that could
only drift from reality: papers get re-extracted, files get deleted, batches get redone, and an
append-only log remembers all of it forever. Scanning the files is cheap (a `\\x00 in bytes` test
over a few thousand files) and gives an answer that is true right now — so the list is regenerated
rather than maintained, and truncating it costs nothing.

## What a NUL means here

`pdftotext` emits U+0000 where a PDF's CID→Unicode font map is broken. Measured across 1,446 papers:
51 affected (3.5%), median 724 NULs per affected file, none under 10 — systematically damaged
extraction, not a stray glyph.

These are NOT stripped and NOT deleted. Stripping keeps whatever surrounded the NUL, which came from
the same broken extraction, and indexes it as though it were the paper. Deleting makes the paper
vanish from the system of record. So they stay on disk, get ingested, and fail visibly at insert —
a work queue rather than amnesia. This list is what that queue looks like.
"""
import os
import sys
from pathlib import Path

TEXT = Path(os.environ.get("ARXIV_TEXT_OUT",
                           str(Path(__file__).resolve().parent / "corpus" / "arxiv")))
LIST = TEXT.parent / "arxiv-needs-ocr.txt"
REASON = "pdftotext emitted NUL (broken CID font)"


def scan() -> list:
    """[(paper_id, nul_count, size)] for every extracted file containing a NUL."""
    out = []
    for f in sorted(TEXT.glob("*.txt")):
        try:
            b = f.read_bytes()
        except OSError:
            continue
        n = b.count(0)
        if n:
            out.append((f.stem, n, len(b)))
    return out


def main() -> int:
    if not TEXT.is_dir():
        print(f"no extracted text at {TEXT}", file=sys.stderr)
        return 1
    hits = scan()
    total = len(list(TEXT.glob("*.txt")))

    if "--count" in sys.argv:
        pct = 100 * len(hits) / total if total else 0
        print(f"{len(hits)} of {total} extracted files contain NUL ({pct:.1f}%)")
        return 0

    with open(LIST, "w") as fh:
        fh.write("# Papers whose text layer is broken — pdftotext emitted NUL bytes.\n")
        fh.write("# DERIVED: regenerate with ./arxiv-scan-nul.py, do not hand-edit.\n")
        fh.write("# id\tnul_bytes\tfile_bytes\treason\n")
        for pid, n, size in hits:
            fh.write(f"{pid}\t{n}\t{size}\t{REASON}\n")

    pct = 100 * len(hits) / total if total else 0
    print(f"{len(hits)} of {total} extracted files contain NUL ({pct:.1f}%) -> {LIST}")
    if hits:
        worst = max(hits, key=lambda h: h[1])
        print(f"worst: {worst[0]} with {worst[1]:,} NULs in {worst[2]:,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
