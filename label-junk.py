#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "requests"]
# ///
"""Bootstrap weak labels for the junk classifier (TODO G3.8).

The classifier needs labels across the WHOLE taxonomy — ToC, index/glossary, exercises,
bibliography, figure-OCR garbage, OCR-damaged code, layout debris, boilerplate, and (mostly) clean.
Nobody hand-labels 300k chunks, so this is the §4.2 cascade again, widened: recall-oriented RULE
NOMINATORS pick candidates per class from the feature matrix (build-junk-features.py), a RANDOM
sample supplies judge-confirmed negatives, and the GPU qwen judge assigns each sampled chunk a
taxonomy class. Every verdict lands in a JSONL audit trail a human can spot-check; the corrected
file becomes the training set.

Runs fine mid-ingest: the judge is GPU (Ollama 30b), DeepDoc owns the CPU — they don't contend.

    ./label-junk.py --features coll.npz --out labels.jsonl --per-class 40 --random 120
    ./label-junk.py --features coll.npz --nominate-only        # no judge, just show the samples
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import importlib.util

import numpy as np
import requests

_spec_db = importlib.util.spec_from_file_location("label_db", Path(__file__).parent / "label-db.py")
_db = importlib.util.module_from_spec(_spec_db)
_spec_db.loader.exec_module(_db)

ES_URL = os.environ.get("ORACLE_ES_URL", "http://localhost:1200")
ES_AUTH = tuple(os.environ.get("ORACLE_ES_AUTH", "elastic:infini_rag_flow").split(":", 1))
OLLAMA = os.environ.get("ORACLE_JUDGE_URL", "http://localhost:11434/api/chat")
MODEL = os.environ.get("ORACLE_JUDGE_MODEL", "qwen3-coder:30b")

CLASSES = ["CLEAN", "TOC", "INDEX", "EXERCISE", "BIBLIOGRAPHY", "FIGURE_GARBAGE",
           "OCR_DAMAGED_CODE", "DEBRIS", "BOILERPLATE"]

# The rubric is a VERSIONED DOCUMENT (RUBRIC.md), injected verbatim — the same fixed text every
# grader applies (qwen here, Claude as blind auditor, the human as adjudicator). Never inline class
# definitions here: two copies of a definition is how graders drift apart.
RUBRIC = (Path(__file__).parent / "RUBRIC.md").read_text(encoding="utf-8")

PROMPT = """You label chunks from a technical-book retrieval corpus. Apply the rubric below EXACTLY —
its definitions, global rules, and worked examples override your own judgment of what junk is.

=== RUBRIC (fixed, versioned) ===
""" + RUBRIC + """
=== END RUBRIC ===

Classify the chunk into exactly one class: """ + ", ".join(CLASSES) + """.
Reply with ONE short sentence of reasoning, then the verdict as [[CLASS]] on its own line.

CHUNK ({docnm}):
---
{text}
---"""

_VERDICT = re.compile(r"\[\[(" + "|".join(CLASSES) + r")\]\]")


def nominators(surf: np.ndarray, fn: list[str]) -> dict[str, np.ndarray]:
    """Recall-oriented per-class candidate masks. False positives are FINE — the judge decides."""
    c = {f: surf[:, i] for i, f in enumerate(fn)}
    return {
        "TOC": c["toc_leader_per_line"] > 0.1,
        "INDEX": ((c["alpha_sorted_ratio"] > 0.8) & (c["short_line_ratio"] > 0.6)
                  & (c["n_lines"] >= 8)) | (c["def_dash_line_ratio"] > 0.5),
        "EXERCISE": (c["mc_option_per_line"] > 0.15) | (c["anskey_per_line"] > 0.05)
                    | ((c["numbered_q_per_line"] > 0.3) & (c["question_per_line"] > 0.2)),
        "BIBLIOGRAPHY": (c["citekey_per_line"] > 0.05)
                        | ((c["year_per_line"] > 0.5) & (c["biblio_per_line"] > 0.1)),
        "FIGURE_GARBAGE": c["weird_density"] > 0.001,
        "OCR_DAMAGED_CODE": (c["code_ratio"] > 0.08) & (c["is_pdf"] > 0)
                            & ((c["weird_density"] > 0) | (c["wordlike_ratio"] < 0.35)),
        "DEBRIS": (c["n_tokens"] < 25) & (c["stopword_ratio"] < 0.08),
        "BOILERPLATE": (c["title_overlap"] > 0.5) & (c["n_tokens"] < 60),
    }


def fetch_content(ids: list[str]) -> dict[str, str]:
    r = requests.get(f"{ES_URL}/_cat/indices?h=index&format=json", auth=ES_AUTH, timeout=30)
    idx = next(x["index"] for x in r.json() if re.fullmatch(r"ragflow_[0-9a-f]{32}", x["index"]))
    out = {}
    for i in range(0, len(ids), 200):
        r = requests.post(f"{ES_URL}/{idx}/_mget?_source=content_with_weight", auth=ES_AUTH,
                          timeout=60, json={"ids": ids[i:i + 200]})
        r.raise_for_status()
        for d in r.json()["docs"]:
            if d.get("found"):
                out[d["_id"]] = d["_source"].get("content_with_weight", "")
    return out


def judge(docnm: str, text: str, timeout: int = 120) -> tuple[str, str]:
    body = {"model": MODEL, "stream": False,
            "messages": [{"role": "user",
                          "content": PROMPT.format(docnm=docnm[:80], text=text[:2400])}],
            "options": {"temperature": 0.0, "num_predict": 160}}
    try:
        r = requests.post(OLLAMA, json=body, timeout=timeout)
        r.raise_for_status()
        reply = r.json()["message"]["content"]
    except Exception as e:  # noqa: BLE001 — a failed judge must never silently label junk
        return "JUDGE_ERROR", str(e)[:120]
    m = _VERDICT.search(reply)
    why = " ".join(reply.split())[:200]
    return (m.group(1) if m else "CLEAN"), why


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--features", type=Path, required=True, help=".npz from build-junk-features.py")
    ap.add_argument("--db", type=Path, default=None, help="labels SQLite DB (default: repo labels.db)")
    ap.add_argument("--per-class", type=int, default=40)
    ap.add_argument("--random", type=int, default=120, dest="n_random",
                    help="random sample for judge-confirmed negatives")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--nominate-only", action="store_true", help="no judge calls; print counts + samples")
    args = ap.parse_args()


    d = np.load(args.features, allow_pickle=True)
    fn = list(d["feat_names"])
    surf, ids, docnm = d["surf"], d["ids"], d["docnm"]
    masks = nominators(surf, fn)
    rng = np.random.default_rng(args.seed)

    picked: dict[int, str] = {}          # chunk row -> nominating class (first wins)
    for cls, mask in masks.items():
        rows = [r for r in np.where(mask)[0] if r not in picked]
        take = rng.permutation(rows)[:args.per_class]
        for r in take:
            picked[int(r)] = cls
    pool = [r for r in range(len(ids)) if r not in picked]
    for r in rng.permutation(pool)[:args.n_random]:
        picked[int(r)] = "RANDOM"

    counts: dict[str, int] = {}
    for cls in list(masks) + ["RANDOM"]:
        counts[cls] = sum(1 for v in picked.values() if v == cls)
    print("nominated:", json.dumps(counts))
    if args.nominate_only:
        for r, cls in list(picked.items())[:12]:
            print(f"  [{cls:16}] {docnm[r][:44]}")
        return 0

    conn = _db.connect(args.db) if args.db else _db.connect()
    already = _db.labeled_ids(conn, "qwen")
    todo = {r: nom for r, nom in picked.items() if str(ids[r]) not in already}
    print(f"judging {len(todo)} (skipping {len(picked) - len(todo)} already qwen-labeled; "
          f"rubric v{_db.rubric_version()})")
    content = fetch_content([str(ids[r]) for r in todo])
    done = errs = 0
    for r, nominated in todo.items():
        cid = str(ids[r])
        text = content.get(cid, "")
        if not text:
            continue
        verdict, why = judge(str(docnm[r]), text)
        if verdict == "JUDGE_ERROR":
            errs += 1     # a failed judge must never silently become a label
            continue
        _db.add_label(conn, chunk_id=cid, label=verdict, labeler="qwen",
                      kb_id=str(d["kb"][r]), doc_id=str(d["doc"][r]), docnm=str(docnm[r]),
                      note=why, nominated=nominated, text=" ".join(text.split())[:400])
        done += 1
        if done % 25 == 0:
            print(f"  judged {done}/{len(todo)} (errors {errs})", flush=True)
    print(f"stored {done} qwen labels ({errs} judge errors) in the labels DB")

    # agreement matrix: nominator vs judge — the honest picture of rule precision
    from collections import Counter
    rows = conn.execute("SELECT nominated, label FROM latest WHERE labeler='qwen'").fetchall()
    mat = Counter((r["nominated"], r["label"]) for r in rows)
    noms = sorted({r["nominated"] for r in rows})
    print("\nnominator -> judge (all qwen labels in DB):")
    for nom in noms:
        tally = {j: n for (a, j), n in mat.items() if a == nom}
        top = ", ".join(f"{j}:{n}" for j, n in sorted(tally.items(), key=lambda x: -x[1])[:4])
        print(f"  {nom:18} {top}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
