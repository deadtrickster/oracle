#!/usr/bin/env python3
"""Run one query with and without the excluded knowledge bases, and diff what comes back.

    ./compare-retrieval.py "how does WAL flushing work in postgres"
    ./compare-retrieval.py --top 8 "learned index structures"

## Why this exists

arXiv is excluded from retrieval by default and opted into per call. That decision was made from
arithmetic — ~1.1M papers at ~52 chunks each against a corpus of ~424k chunks total — but arithmetic
predicts dilution, it does not measure it. This measures it: same query, same reranker, one run with
the excluded shelves and one without, showing which documents each returns and how far the shared
ones move.

What to look for:

  * A SOFTWARE question ("how does X work") should barely change, or should get worse when arXiv is
    added — papers reuse the vocabulary of the systems they study, so they match the words without
    answering the question.
  * A RESEARCH question ("what does the literature say", "recent approaches to Y") should improve,
    and the arXiv results should displace weaker curated ones rather than sitting below them.

If arXiv displaces curated sources on software questions, the exclusion is earning its place. If it
never appears even on research questions, `research=true` is not worth having.
"""
import argparse
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_receiver():
    spec = importlib.util.spec_from_file_location("rcv", HERE / "oracle-capture-receiver.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def top_docs(chunks, n):
    """[(rank, document, first words)] — the ranking is what we are comparing, not the prose."""
    out = []
    for i, c in enumerate(chunks[:n], 1):
        doc = c.get("document_keyword", "?")
        text = " ".join((c.get("content_with_weight") or c.get("content", "")).split())[:90]
        out.append((i, doc, text))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query")
    ap.add_argument("--top", type=int, default=6)
    a = ap.parse_args()

    rcv = load_receiver()
    excluded = rcv.KB_EXCLUDE
    print(f"query   : {a.query}")
    print(f"excluded: {', '.join(sorted(excluded)) or '(nothing)'}\n")

    runs = {}
    for label, include in (("default", None), ("research", excluded)):
        kb_ids = rcv._kb_ids(include=include)
        try:
            chunks, reranked = rcv._retrieve(a.query, kb_ids)
        except Exception as e:  # noqa: BLE001
            print(f"{label}: retrieval failed ({e})")
            return 1
        runs[label] = chunks
        print(f"── {label.upper()}  ({len(kb_ids)} KBs, {len(chunks)} hits, "
              f"reranked={reranked}) " + "─" * 20)
        for i, doc, text in top_docs(chunks, a.top):
            mark = "  «arxiv" if doc.startswith("arxiv__") else ""
            print(f"  {i}. {doc[:44]:44} {text[:70]}{mark}")
        print()

    # The interesting number is not overlap but DISPLACEMENT: how much of the curated top-N the
    # papers pushed out. A research question should displace; a software question should not.
    d = [c.get("document_keyword", "?") for c in runs["default"][:a.top]]
    r = [c.get("document_keyword", "?") for c in runs["research"][:a.top]]
    arxiv_in_top = sum(1 for x in r if x.startswith("arxiv__"))
    displaced = [x for x in d if x not in r]
    print(f"arXiv took {arxiv_in_top}/{a.top} of the top slots when included")
    if displaced:
        print("displaced from the curated top:")
        for x in displaced:
            print(f"  - {x[:70]}")
    else:
        print("nothing displaced — the curated results held their positions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
