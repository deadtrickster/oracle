#!/usr/bin/env python3
"""Strip non-answer material from book text before it reaches RAGFlow.

WHY (measured 2026-07-13, see EVAL.md / DESIGN.md "Corpus hygiene"):

A textbook is not all answers. It also contains exercise questions ("Вопросы для повторения",
"Review Questions"), answer keys, indexes, bibliographies and publisher front-matter (УДК/ББК,
the editorial board). We were embedding ALL of it.

That matters more than it sounds, because of an asymmetry: a user's query is a QUESTION, and a
chunk of exercise questions is also QUESTIONS — so they embed close together. The textbook's own
question lists out-compete the passages that actually answer the query. Measured in the `bio` KB:
bogdanova is 13.6% question-list chunks, and 6 of the top 30 hits for "что такое фотосинтез" were
exercise questions displacing real content.

HONEST SCOPE: this is corpus hygiene, not a retrieval silver bullet. It removes noise that is
competing unfairly; it does not make a weakly-embedded passage win. (The "какие виды мышей" case
stays broken after cleaning — that chunk loses to a legitimately-similar one about летучие мыши,
"flying mice". Different problem.)

Two mechanisms, deliberately:

  1. QUESTION-BLOCK STRIPPING (automatic, generalises to any book/language). Blocks whose sentences
     are mostly interrogative get dropped. No curation, cannot drift out of sync with the corpus.

  2. PAGE-RANGE EXCLUSION (manual scalpel, per book, in books.toml). For what a heuristic cannot
     see: answer keys, indexes, front-matter. Possible only because pdf2txt.sh / ocr-pdf.sh now
     emit `[[p.N]]` markers — see DESIGN.md.

Usage:
    ./clean-corpus.py corpus/bio                 # clean in place (writes .orig backups)
    ./clean-corpus.py corpus/bio --dry-run       # report what WOULD be dropped
"""
import argparse
import re
import sys
import tomllib
from pathlib import Path

PAGE_RE = re.compile(r"^\[\[p\.(\d+)\]\]$")

# Exercise questions are laid out ONE PER PARAGRAPH, bullet-prefixed:
#
#     ® Вчем заключается процесс синтеза ДНК?
#
#     ® Как называют участок молекулы...?
#
# so no single paragraph is "full of questions" — the signal is a RUN of consecutive interrogative
# paragraphs. That is also what separates an exercise section from a rhetorical question inside real
# prose, which we must NOT eat. A lone question survives; three in a row do not.
MIN_RUN = 3                 # consecutive interrogative paragraphs before we call it an exercise list
MAX_Q_LEN = 400             # an exercise question is short; a long paragraph ending in "?" is prose

# NOTE — a rule we tried and DELETED: "a paragraph containing >=3 question marks is a question list".
# It produced false positives on every technical book and no true positives the run-rule missed:
#   * it ate the WAL chapter of pg_monitoring (prose that poses questions and then answers them);
#   * it ate a PostgreSQL OPERATOR TABLE — because `?`, `?|`, `?&` ARE jsonb operators, so a table of
#     them looks exactly like a list of questions.
# Counting '?' is not a signal. STRUCTURE is: a run of short, standalone, interrogative paragraphs.


def _is_interrogative(block: str) -> bool:
    """A single exercise-style question: short, standalone, ends in '?'."""
    t = " ".join(block.split())
    return t.endswith("?") and len(t) <= MAX_Q_LEN


def question_block_mask(blocks: list[str]) -> list[bool]:
    """True where a block is exercise material and should be dropped.

    ONLY runs of >= MIN_RUN consecutive standalone questions. A lone rhetorical question inside
    prose survives, and so does any paragraph that merely *contains* question marks.
    """
    drop = [False] * len(blocks)
    i = 0
    while i < len(blocks):
        if _is_interrogative(blocks[i]):
            j = i
            while j < len(blocks) and _is_interrogative(blocks[j]):
                j += 1
            if j - i >= MIN_RUN:
                for k in range(i, j):
                    drop[k] = True
            i = j
        else:
            i += 1
    return drop


def parse_ranges(spec: str) -> set[int]:
    """'1-20, 640, 700-817' -> {1..20, 640, 700..817}"""
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def clean(path: Path, drop_pages: set[int], strip_questions: bool, dry: bool) -> tuple[int, int, int]:
    """Returns (pages_dropped, question_blocks_dropped, bytes_saved)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    out, page = [], 0
    dropped_pages = q_blocks = 0
    kept_bytes = 0

    # split into page sections so page-range exclusion is exact
    for section in re.split(r"(?m)^(?=\[\[p\.\d+\]\]$)", text):
        if not section.strip():
            continue
        m = PAGE_RE.match(section.splitlines()[0].strip()) if section.splitlines() else None
        page = int(m.group(1)) if m else page

        if page in drop_pages:
            dropped_pages += 1
            continue

        header, _, body = section.partition("\n")
        blocks = re.split(r"\n\s*\n", body)
        drop = question_block_mask(blocks) if strip_questions else [False] * len(blocks)
        q_blocks += sum(drop)
        kept = [b for b, d in zip(blocks, drop) if not d]
        body_out = "\n\n".join(b for b in kept if b.strip())
        if body_out.strip():
            out.append(f"{header}\n{body_out}")
            kept_bytes += len(body_out)

    new = "\n".join(out) + "\n"
    saved = len(text) - len(new)
    if not dry:
        backup = path.with_suffix(path.suffix + ".orig")
        if not backup.exists():          # keep the FIRST original, never overwrite a backup
            backup.write_text(text, encoding="utf-8")
        path.write_text(new, encoding="utf-8")
    return dropped_pages, q_blocks, saved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", type=Path, help="corpus dir holding *.txt and (optionally) books.toml")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--strip-questions", action="store_true",
                    help="drop exercise-question runs (OPT-IN: right for textbooks, WRONG for "
                         "reference manuals — see the comment in main()). Can also be set per "
                         "corpus/book in books.toml.")
    args = ap.parse_args()

    cfg_path = args.dir / "books.toml"
    cfg = {}
    if cfg_path.exists():
        cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))

    files = sorted(f for f in args.dir.glob("*.txt") if not f.name.startswith("!"))
    if not files:
        print(f"no .txt under {args.dir}", file=sys.stderr)
        return 1

    # Question-stripping is OPT-IN, per corpus. It is right for an exam-prep TEXTBOOK (whose quiz
    # sections out-compete its own chapters) and WRONG for a reference manual: on the Postgres Pro
    # books an earlier, looser filter ate the WAL chapter and a jsonb operator table (`?` is a
    # PostgreSQL operator). Default off — turn it on deliberately, per directory, in books.toml.
    defaults = cfg.get("defaults") or {}
    default_strip = bool(defaults.get("strip_questions", False)) or args.strip_questions

    print(f"{'book':46} {'pages cut':>10} {'q-blocks':>9} {'saved':>10}")
    for f in files:
        book = cfg.get(f.stem) or {}
        spec = book.get("exclude_pages", "")
        drop = parse_ranges(spec) if spec else set()
        strip = bool(book.get("strip_questions", default_strip))
        pages, qb, saved = clean(f, drop, strip, args.dry_run)
        print(f"{f.name[:44]:46} {pages:10} {qb:9} {saved/1024:9.0f}K")
    if args.dry_run:
        print("\n(dry run — nothing written)")
    else:
        print("\noriginals kept as *.txt.orig; re-run ingest-corpus.py after deleting the old docs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
