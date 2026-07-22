#!/usr/bin/env python3
"""Assemble the Opus gold-OCR per-page transcripts into per-book corpus files.

Replaces the VL-draft assemblies in corpus/ml/<slug>.txt with the fleet's gold pages
(corpus/ml/opus-gold/<slug>/p-NNNN.txt), keeping the exact [[p.N]] page-marker format the
browser deep-links against. The VL drafts stay rebuildable from corpus/ml/vl-pages/.

Safety: refuses to assemble a book with missing/empty pages (disk truth, same rule as
opus-fleet.py done()); prints a per-book report and writes nothing on a gap.

    ./assemble-gold.py            # report what would be written (dry run)
    ./assemble-gold.py --write    # actually write corpus/ml/<slug>.txt
"""
import argparse
import sys
from pathlib import Path

REPO = Path(__file__).parent
GOLD = REPO / "corpus/ml/opus-gold"
OUT = REPO / "corpus/ml"

# (slug, book page count) — counts must match opus-fleet.py's n_pages() totals
BOOKS = [
    ("neurocomp-kn01-galushkin-teoriya", 417),
    ("neurocomp-kn02-omatu-neuroupravlenie", 273),
    ("neurocomp-kn03-galushkin-neurocomputery", 526),
    ("neurocomp-kn04-golovko-obuchenie", 257),
    ("neurocomp-kn05-istoriya-teorii", 840),
    ("okulov-pestov-dinamicheskoe-programmirovanie", 301),
]


def assemble(slug: str, n: int, write: bool) -> bool:
    src = GOLD / slug
    missing = [p for p in range(1, n + 1)
               if not ((src / f"p-{p:04}.txt").is_file() and (src / f"p-{p:04}.txt").stat().st_size > 0)]
    if missing:
        print(f"  {slug}: REFUSED — {len(missing)} missing/empty pages, e.g. {missing[:5]}")
        return False
    parts = []
    for p in range(1, n + 1):
        text = (src / f"p-{p:04}.txt").read_text(encoding="utf-8").strip()
        parts.append(f"[[p.{p}]]\n{text}")
    body = "\n\n".join(parts) + "\n"
    out = OUT / f"{slug}.txt"
    if write:
        out.write_text(body, encoding="utf-8")
    print(f"  {slug}: {n} pages, {len(body):,} chars -> {out.name}"
          + ("" if write else "  (dry run)"))
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="write the files (default: dry run)")
    args = ap.parse_args()
    ok = all([assemble(slug, n, args.write) for slug, n in BOOKS])
    if not ok:
        print("INCOMPLETE — nothing partial was written for refused books")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
