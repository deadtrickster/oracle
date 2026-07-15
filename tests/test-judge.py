#!/usr/bin/env python3
"""Validate the LLM judge against the labelled fixtures BEFORE trusting it to delete anything.

A judge that deletes corpus content is only as good as its measured accuracy on cases we have
labelled by hand. So: run it on tests/corpus-filter/*, where the expected verdict is fixed in the
filename, and report per-case results.

The case that matters most is `gap_*` — OpenStax multiple-choice review questions, which the RULE
is structurally blind to (they don't end in "?"). If the judge catches those, it has earned its
place. If it also keeps every `keep_*` (the jsonb operator table, the WAL prose), it is safe to use.

    ./tests/test-judge.py
"""
import importlib.util
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("chunk_judge", ROOT / "chunk_judge.py")
cj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cj)

# what the judge SHOULD say, per filename prefix
EXPECTED = {"drop": "EXERCISE", "gap": "EXERCISE", "keep": "CONTENT"}


def main() -> int:
    files = sorted((HERE / "corpus-filter").glob("*.txt"))
    wrong = []
    print(f"judge = {cj.MODEL}\n")
    for f in files:
        kind = f.name.split("_", 1)[0]
        want = EXPECTED[kind]
        text = f.read_text(encoding="utf-8")
        cand = cj.is_candidate(text)

        t = time.time()
        got, why = cj.judge(text)
        dt = time.time() - t

        ok = got == want
        # a chunk the pre-filter never flags is never judged — that is a silent miss
        reachable = cand or want == "CONTENT"
        flag = "ok  " if ok else "WRONG"
        print(f"  [{flag}] {f.stem:36} want={want:8} got={got:8} ({dt:4.1f}s) "
              f"prefilter={'flagged' if cand else 'not-flagged'}")
        if not ok:
            wrong.append((f.stem, want, got, why))
        if not reachable:
            wrong.append((f.stem, "REACHABLE", "pre-filter never flags it — judge never sees it", ""))

    print()
    if wrong:
        print(f"{len(wrong)} PROBLEM(S):")
        for name, want, got, why in wrong:
            print(f"  - {name}: wanted {want}, got {got}")
            if why:
                print(f"      judge said: {why}")
        return 1
    print(f"judge agrees with all {len(files)} labelled fixtures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
