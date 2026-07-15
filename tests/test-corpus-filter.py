#!/usr/bin/env python3
"""Regression tests for clean-corpus.py's question-stripping filter.

The filter is DESTRUCTIVE — it deletes text before it is ever indexed — so a false positive is
silent and permanent. The first version of it ate the WAL chapter of a Postgres book and a jsonb
operator table (because `?`, `?|`, `?&` ARE PostgreSQL operators, so a table of them looks exactly
like a list of questions). Nothing caught that but a human reading the diff.

Hence this: real excerpts from the real books, with the expected verdict fixed in the filename.

    keep_*   the filter MUST NOT touch these  (false positive = silently deleted knowledge)
    drop_*   the filter MUST strip these      (false negative = quiz sections poison retrieval)
    gap_*    the filter CURRENTLY misses these — a KNOWN, DOCUMENTED limitation. Asserted so that
             the day someone fixes it, this test says so out loud instead of changing behaviour
             silently. A gap you have written down is a decision; one you have not is a bug.

    ./tests/test-corpus-filter.py
"""
import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent

spec = importlib.util.spec_from_file_location("clean_corpus", ROOT / "clean-corpus.py")
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)


def verdicts(text: str) -> list[bool]:
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    return cc.question_block_mask(blocks)


def main() -> int:
    failures = []
    for f in sorted((HERE / "corpus-filter").glob("*.txt")):
        kind = f.name.split("_", 1)[0]
        dropped = verdicts(f.read_text(encoding="utf-8"))
        n, total = sum(dropped), len(dropped)

        if kind == "keep":
            ok = n == 0
            want = "drop nothing"
        elif kind == "drop":
            ok = n == total and total > 0
            want = "drop everything"
        elif kind == "gap":
            ok = n == 0          # currently NOT caught — see docstring
            want = "known gap: not caught (yet)"
        else:
            failures.append(f"{f.name}: unknown prefix {kind!r}")
            continue

        status = "ok  " if ok else "FAIL"
        print(f"  [{status}] {f.stem:36} dropped {n}/{total:<3} ({want})")
        if not ok:
            failures.append(f"{f.stem}: dropped {n}/{total}, expected to {want}")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for x in failures:
            print(f"  - {x}")
        return 1
    print("all corpus-filter fixtures pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
