#!/usr/bin/env python3
"""Measure the RETRIEVAL stage on its own, against qrels.toml.

EVAL.md grades the final answer, which conflates two very different failures: "search never found
the passage" and "the model had the passage and fumbled it". This script measures only the first.

The number that matters is RECALL@k of the FIRST STAGE. Per Jurafsky & Martin (SLP3 §11.3), the
pipeline is a cascade — cheap search, then expensive rerank of the top N — from which follows the
rule we learned the hard way:

    THE FIRST STAGE SETS THE CEILING. Reranking can only reorder what search already returned.

If recall@64 is 0, every reranking experiment is tuning the order of a list that does not contain
the answer. (This is precisely what happened on 2026-07-13.)

    ./eval-retrieval.py                 # recall@k for the first stage, + rerank effect on rank
    ./eval-retrieval.py --k 8,64,256
"""
import argparse
import os
import re
import sys
import tomllib
from pathlib import Path

import requests

RAGFLOW = os.environ.get("ORACLE_RAGFLOW_URL", "http://localhost:9380")
KEY = os.environ.get("ORACLE_RAGFLOW_KEY",
                     "ragflow-smywlJs3drgGxfKztifTmD3iNJ2lP6Uvq2-suiLQTGM")
RERANK_ID = "gte-multilingual-reranker-base@local-gte-rerank@Jina"
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def datasets():
    r = requests.get(f"{RAGFLOW}/api/v1/datasets?page_size=100", headers=HDR, timeout=30)
    r.raise_for_status()
    return {d["name"]: d["id"] for d in r.json()["data"]}


PAGE = 30          # RAGFlow returns data:null above ~100 per page — paginate instead


def retrieve(question, kb_ids, top_k, rerank):
    """Top-k chunks, paginated (the API caps page_size, and silently returns data:null over it)."""
    out = []
    for page in range(1, (top_k + PAGE - 1) // PAGE + 1):
        body = {"question": question, "dataset_ids": kb_ids,
                "page": page, "page_size": PAGE, "top_k": top_k,
                "similarity_threshold": 0.0, "vector_similarity_weight": 0.3}
        if rerank:
            body["rerank_id"] = RERANK_ID
        r = requests.post(f"{RAGFLOW}/api/v1/retrieval", headers=HDR, json=body, timeout=300)
        r.raise_for_status()
        data = r.json().get("data") or {}
        chunks = data.get("chunks") or []
        if not chunks:
            break
        out.extend(chunks)
        if len(out) >= top_k:
            break
    return out[:top_k]


def rank_of_gold(chunks, gold):
    """1-based rank of the first chunk matching ALL gold predicates, else None."""
    for i, c in enumerate(chunks, 1):
        text = c.get("content", "")
        if all(re.search(g, text) for g in gold):
            return i
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--qrels", type=Path, default=Path(__file__).parent / "qrels.toml")
    ap.add_argument("--k", default="8,64,256", help="cut-offs to report (default 8,64,256)")
    args = ap.parse_args()

    ks = [int(x) for x in args.k.split(",")]
    kmax = max(ks)
    qs = tomllib.loads(args.qrels.read_text(encoding="utf-8"))["q"]
    ds = datasets()

    hdr = f"{'question':22} " + " ".join(f"{'R@'+str(k):>7}" for k in ks) + f" {'rank':>6} {'reranked':>9}"
    print(hdr)
    print("-" * len(hdr))

    hits = {k: 0 for k in ks}
    positives = 0
    abstain_ok = abstain_tot = 0

    for q in qs:
        kb_ids = [ds[n] for n in q["kbs"] if n in ds]
        if not kb_ids:
            print(f"{q['id'][:20]:22} (KB missing: {q['kbs']})")
            continue
        chunks = retrieve(q["question"], kb_ids, kmax, rerank=False)
        gold = q["gold"]

        if not gold:                       # TRUE NEGATIVE: nothing should be found
            abstain_tot += 1
            # we cannot verify "no gold exists" from retrieval alone; report top score as a hint
            top = chunks[0]["similarity"] if chunks else 0.0
            print(f"{q['id'][:20]:22} " + " ".join(f"{'—':>7}" for _ in ks) +
                  f" {'n/a':>6} {'':>9}   (true negative; top score {top:.3f})")
            abstain_ok += 1
            continue

        positives += 1
        r = rank_of_gold(chunks, gold)
        cells = []
        for k in ks:
            ok = r is not None and r <= k
            hits[k] += ok
            cells.append(f"{'HIT' if ok else 'miss':>7}")
        # what does the reranker do to it?
        rr = rank_of_gold(retrieve(q["question"], kb_ids, 64, rerank=True), gold)
        print(f"{q['id'][:20]:22} " + " ".join(cells) +
              f" {str(r) if r else '—':>6} {str(rr) if rr else '—':>9}")

    print("-" * len(hdr))
    for k in ks:
        print(f"  recall@{k:<4} = {hits[k]}/{positives}"
              f"  ({100*hits[k]/max(positives,1):.0f}%)")
    print(f"  true negatives (should abstain): {abstain_tot}")
    print("\n  'rank'     = position of the gold passage in the FIRST STAGE (the ceiling)")
    print("  'reranked' = its position after the cross-encoder — CANNOT rescue a miss")
    return 0


if __name__ == "__main__":
    sys.exit(main())
