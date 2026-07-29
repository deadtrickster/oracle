#!/usr/bin/env python3
"""Select arXiv papers by category from the mirror and extract their text for ingestion.

The mirror at /mnt/data/arxiv holds the PDFs and a catalogue (`state.db`: 3.1M papers with
categories, 1.7M objects with download state). This picks a slice, runs `pdftotext` over it, and
writes `corpus/arxiv/<id>.txt` ready for `ingest-corpus.py --only arxiv`.

    ./arxiv-select.py --category cs.DB --list
    ./arxiv-select.py --category cs.DB --category cs.IR --max 2000
    ./arxiv-select.py --category cs.DB --max 50 --force     # redo ones already extracted

## Why pdftotext and not DeepDoc

Measured on this box: DeepDoc costs ~415s per 13-page range (table analysis ~47s, OCR ~31s, model
inference between). A 15-page paper is 7-14 minutes, so the ~8k systems-category papers already
downloaded would be six to eleven WEEKS of continuous CPU — and the mirror is heading for ~1.1M.
Through pdftotext it is a second or two each.

Nothing is given up: arXiv PDFs are born-digital and carry a real text layer. OCR and table-
structure recognition are solving a problem these files do not have. The one thing DeepDoc would add
— figure extraction and page positions — is not worth two orders of magnitude here.

## Selection is not optional

You cannot ingest arXiv. At ~30 chunks per paper, the 54,419 systems-category papers alone would be
~1.6M chunks against a corpus that currently holds 424,515 in total. Pick a slice you would actually
read, ingest it, and see whether retrieval improves before widening.

## The category-matching trap

SQLite's LIKE is case-insensitive, so `categories LIKE '%cs.%'` cheerfully matches **physi*cs.*optics**
and quietly triples your slice. Every match here is anchored to a token boundary.
"""
import argparse
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

MIRROR = Path(os.environ.get("ARXIV_MIRROR", "/mnt/data/arxiv"))
STATE = MIRROR / "state.db"
OUT = Path(os.environ.get("ARXIV_TEXT_OUT",
                          str(Path.home() / "Projects/oracle/corpus/arxiv")))
STATE_DONE = 1          # objects.state: 0 pending, 1 downloaded, 3 superseded (latest-only policy)


def safe_stem(paper_id: str) -> str:
    """The .txt filename stem for a paper id — one definition, used by both the selector and the
    extractor so 'already extracted' cannot mean two different things."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", paper_id)


def where_for(cats: list) -> tuple:
    """SQL + args matching any of `cats` at a token boundary."""
    clauses, args = [], []
    for c in cats:
        clauses.append("(p.categories = ? OR p.categories LIKE ? OR p.categories LIKE ? "
                       "OR p.categories LIKE ?)")
        args += [c, f"{c} %", f"% {c} %", f"% {c}"]
    return "(" + " OR ".join(clauses) + ")", args


def select(cats: list, limit: int, newest_first: bool = True, have: set | None = None) -> list:
    """Up to `limit` papers NOT already extracted, newest first.

    Pages through the catalogue rather than taking one `LIMIT` window, because the obvious version
    silently starves. `ORDER BY update_date DESC LIMIT 2000` returns the newest 2,000 every time; on
    the second run they are all extracted already, the filter removes them, and the result is ONE
    new paper per cycle — while 305,000 downloaded papers sit outside the window, unreachable
    forever. The tailer looked like it was drying up when it was looking at the same 2,000 rows.

    So the LIMIT has to bound NEW work, not rows examined.
    """
    if not STATE.exists():
        print(f"no catalogue at {STATE}", file=sys.stderr)
        return []
    have = have or set()
    db = sqlite3.connect(f"file:{STATE}?mode=ro", uri=True)
    # No categories = the whole mirror. The mirror is already scoped (all categories, 2022-01 ->
    # 2026-07, latest version only), so "everything downloaded" is a meaningful selection rather
    # than an unbounded one.
    w, args = where_for(cats) if cats else ("1=1", [])
    order = "p.update_date DESC" if newest_first else "p.update_date ASC"

    out, offset, page = [], 0, max(limit * 4, 2000)
    while len(out) < limit:
        rows = db.execute(
            f"""SELECT o.name, o.paper_id, p.categories, p.update_date
                FROM objects o JOIN papers p ON p.id = o.paper_id
                WHERE o.state = ? AND {w}
                ORDER BY {order} LIMIT ? OFFSET ?""",
            [STATE_DONE] + args + [page, offset]).fetchall()
        if not rows:
            break                                   # catalogue exhausted
        for r in rows:
            if safe_stem(r[1]) not in have:
                out.append(r)
                if len(out) >= limit:
                    break
        offset += len(rows)
    return out


def extract(name: str, paper_id: str, force: bool) -> str:
    """pdftotext one paper into OUT. Returns "ok" | "skip" | "missing" | "empty" | "error"."""
    # The catalogue stores GCS OBJECT names (`arxiv/arxiv/pdf/2607/x.pdf`); the mirror strips the
    # duplicated prefix when it writes them (`dest = root / name.replace("arxiv/arxiv/", "", 1)` in
    # arxiv_sync.py). Reproduce that exactly rather than inventing a second convention — the two
    # drifting apart would show up as "missing file" for everything, which is precisely what it did.
    pdf = MIRROR / name.replace("arxiv/arxiv/", "", 1)
    if not pdf.exists():
        # The catalogue is the mirror's record of intent; a file can be absent if the sync was
        # interrupted between download and rename.
        return "missing"
    dst = OUT / f"{safe_stem(paper_id)}.txt"
    if dst.exists() and dst.stat().st_size > 0 and not force:
        return "skip"
    try:
        r = subprocess.run(["pdftotext", "-q", str(pdf), str(dst)],
                           capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "error"
    if r.returncode != 0:
        return "error"
    # A NUL byte means pdftotext hit a broken CID→Unicode font map and emitted U+0000 where glyphs
    # should be. Real text never contains one. Observed on arXiv 2607.17571, which parsed fine and
    # then failed at insert with "A string literal cannot contain NUL (0x00) characters" — after the
    # CPU had already been spent.
    #
    # Never STRIP it — that is the G4.4 policy: what surrounds a NUL came from the same broken
    # extraction, so stripping keeps the garbage and indexes it as if it were the paper.
    #
    # But do not silently DROP it either. The first version deleted the file, and the paper simply
    # ceased to exist: gone from ingest-status, recorded only in a side file nobody reads. A failure
    # that vanishes is worse than a failure that sits there, because the second one is a work queue
    # and the first one is amnesia. His call, and it is the right one — failures stay failed.
    #
    # So the file is KEPT and will be ingested and fail at insert, visibly, in RAGFlow's own
    # document table.
    #
    # The needs-OCR list is NOT written here. It is derived — `./arxiv-scan-nul.py` rebuilds it by
    # scanning the extracted text. An append-only log written during extraction can only drift from
    # what is actually on disk (files get re-extracted, deleted, batches redone), and then nobody
    # trusts it. Scanning is cheap and always true.
    if dst.exists():
        try:
            if b"\x00" in dst.read_bytes():
                return "corrupt"
        except OSError:
            pass
    if not dst.exists() or dst.stat().st_size < 500:
        # Under 500 bytes means no usable text layer — a scan, or a PDF whose fonts defeated
        # extraction. Drop it rather than ingesting a stub: a document that says nothing still
        # occupies a retrieval slot, and its title will still match a query.
        dst.unlink(missing_ok=True)
        return "empty"
    return "ok"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--category", action="append", default=[],
                    help="arXiv category, e.g. cs.DB. Repeatable.")
    ap.add_argument("--all", action="store_true",
                    help="every downloaded paper, no category filter (the mirror is all categories)")
    ap.add_argument("--max", type=int, default=500)
    ap.add_argument("--oldest", action="store_true", help="oldest first (default: newest)")
    ap.add_argument("--force", action="store_true", help="re-extract even if the .txt exists")
    ap.add_argument("--list", action="store_true", help="show the selection, extract nothing")
    a = ap.parse_args()

    if not a.category and not a.all:
        print("give --category, or --all for everything downloaded", file=sys.stderr)
        return 2
    # Exclude what is already extracted INSIDE the query paging, so --max bounds new work. Doing it
    # afterwards means the newest window is re-examined every cycle and almost nothing comes back.
    OUT.mkdir(parents=True, exist_ok=True)
    have = set() if a.force else {f.stem for f in OUT.glob("*.txt")}
    rows = select(a.category, a.max, newest_first=not a.oldest, have=have)
    if not rows:
        print("nothing new to extract — the mirror has nothing downloaded that is not already done",
              file=sys.stderr)
        return 0
    print(f"{len(rows)} papers selected from {', '.join(a.category) if a.category else 'ALL categories'}")

    if a.list:
        for name, pid, cats, upd in rows[:60]:
            print(f"  {upd}  {pid:14} {cats[:60]}")
        if len(rows) > 60:
            print(f"  … and {len(rows) - 60} more")
        return 0

    if not shutil_which("pdftotext"):
        print("pdftotext not found (apt install poppler-utils)", file=sys.stderr)
        return 1

    tally = {"ok": 0, "skip": 0, "missing": 0, "empty": 0, "corrupt": 0, "error": 0}
    for i, (name, pid, _c, _u) in enumerate(rows, 1):
        tally[extract(name, pid, a.force)] += 1
        if i % 200 == 0:
            print(f"  {i}/{len(rows)}  {tally}")

    print()
    print(f"extracted {tally['ok']}, already had {tally['skip']}, "
          f"no text layer {tally['empty']}, NUL-corrupt but KEPT (will fail visibly) {tally['corrupt']}, "
          f"missing file {tally['missing']}, failed {tally['error']}")
    print(f"text in: {OUT}  ({len(list(OUT.glob('*.txt')))} files total)")
    print()
    print("next:  ./ingest-corpus.py --api-key <KEY> --only arxiv")
    return 0


def shutil_which(x):
    from shutil import which
    return which(x)


if __name__ == "__main__":
    sys.exit(main())
