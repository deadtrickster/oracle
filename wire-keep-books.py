# /// script
# requires-python = ">=3.10"
# ///
"""Wire the deduped KEEP book list into ingest-corpus.py's corpus/keep_raw/ lane.

Reads ~/Documents/Books/keep-plan.json (the G5.1 dedup output: {"keep": [<abs paths>]}),
drops the books already covered by existing KBs, symlinks the remaining PDFs into
corpus/keep_raw/ with collision-safe flat names, and reports the EPUB/DJVU files that
need conversion first (RAGFlow's `book` parser takes PDF/DOCX/TXT only).

Already-ingested source repos to skip (their whole repo is a *_raw symlink already
parsed into a KB):
  awesome-book-collection -> corpus/collection_raw  -> `collection` KB
  ml                      -> corpus/ml_raw           -> `ml` KB
  bio                     -> corpus/bio_raw           -> `bio` KB

Idempotent: a correct existing symlink is left untouched; re-runs are safe.
Run:  uv run wire-keep-books.py            (report + create symlinks)
      uv run wire-keep-books.py --dry-run  (report only)
"""
import argparse
import json
import os
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent
KEEP_PLAN = Path.home() / "Documents" / "Books" / "keep-plan.json"
DEST = ROOT / "corpus" / "keep_raw"

# Source-repo path segments whose books are already ingested under another KB.
ALREADY_INGESTED_REPOS = {"awesome-book-collection", "ml", "bio"}


def repo_of(path: str) -> str:
    """First path segment under Documents/Books/ (the source collection)."""
    tail = path.split("Documents/Books/", 1)[-1]
    return tail.split("/", 1)[0]


def flat_name(path: Path, taken: set[str]) -> str:
    """Collision-safe flat filename for the symlink under keep_raw/.

    Start from the basename; on collision, prepend the parent-dir slug, then a
    numeric suffix as a last resort. Keeps names human-readable in the KB."""
    base = path.name
    if base not in taken:
        return base
    cand = f"{path.parent.name}__{base}"
    if cand not in taken:
        return cand
    stem, ext = os.path.splitext(base)
    i = 2
    while f"{stem}-{i}{ext}" in taken:
        i += 1
    return f"{stem}-{i}{ext}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only, create no symlinks")
    args = ap.parse_args()

    keep = json.loads(KEEP_PLAN.read_text())["keep"]

    new, skipped = [], []
    for p in keep:
        (skipped if repo_of(p) in ALREADY_INGESTED_REPOS else new).append(p)

    pdfs = [p for p in new if p.lower().endswith(".pdf")]
    other = [p for p in new if not p.lower().endswith(".pdf")]  # epub / djvu / etc.

    print(f"KEEP total ...................... {len(keep)}")
    print(f"  skipped (already in a KB) ..... {len(skipped)}  "
          f"{dict(Counter(repo_of(p) for p in skipped))}")
    print(f"  NEW to ingest ................. {len(new)}")
    print(f"    PDFs (book parser) .......... {len(pdfs)}")
    print(f"    needs conversion first ...... {len(other)}  "
          f"{dict(Counter(os.path.splitext(p)[1].lower() for p in other))}")
    print()

    if not args.dry_run:
        DEST.mkdir(parents=True, exist_ok=True)
    taken: set[str] = set()
    linked = existing = 0
    for p in sorted(pdfs):
        src = Path(p)
        name = flat_name(src, taken)
        taken.add(name)
        link = DEST / name
        if link.is_symlink() and link.resolve() == src.resolve():
            existing += 1
            continue
        if args.dry_run:
            linked += 1
            continue
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(src)
        linked += 1

    verb = "would link" if args.dry_run else "linked"
    print(f"corpus/keep_raw/: {verb} {linked} new PDF symlink(s), {existing} already correct")
    if other:
        report = ROOT / "corpus" / "keep_raw-needs-conversion.txt"
        if not args.dry_run:
            report.write_text("\n".join(sorted(other)) + "\n")
            print(f"wrote {len(other)} non-PDF paths needing conversion -> {report}")
        else:
            print(f"({len(other)} non-PDF paths would be written to {report})")
    print()
    print("Next: add  (\"keep-books\", \"book\", [\"keep_raw/*.pdf\"])  to ingest-corpus.py KBS,")
    print("then:  uv run ingest-corpus.py --api-key <KEY> --only keep-books")


if __name__ == "__main__":
    main()
