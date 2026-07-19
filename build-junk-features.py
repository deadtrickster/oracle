#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "requests"]
# ///
"""Build the feature matrix for the CPU junk classifier (TODO G3.8) — READ-ONLY.

Every chunk already carries a bge-m3 embedding in Elasticsearch (q_1024_vec, 1024-dim), so the
semantic half of the features is free. This script scrolls the RAGFlow chunk index, pulls that vector
plus the raw content, computes cheap surface features (glyph density, script mix, word-likeness,
numeric ratio, repeat runs, HTML-table flag, token stats), and writes everything to an .npz so that
once the CPU frees up (the ingest is CPU-bound), only labelling + .fit() remain.

Nothing is written back to ES or RAGFlow. Safe to run anytime, including mid-ingest.

    ./build-junk-features.py --out features.npz            # whole corpus
    ./build-junk-features.py --kb collection --out c.npz   # one KB
    ./build-junk-features.py --sample 2000 --out probe.npz # quick probe
"""
import argparse
import importlib.util
import os
import re
import sys
from pathlib import Path

import numpy as np
import requests

ES_URL = os.environ.get("ORACLE_ES_URL", "http://localhost:1200")
ES_AUTH = tuple(os.environ.get("ORACLE_ES_AUTH", "elastic:infini_rag_flow").split(":", 1))

# reuse the detector's exact "weird glyph" definition so features and candidate-gen agree
_spec = importlib.util.spec_from_file_location("chunk_judge", Path(__file__).parent / "chunk_judge.py")
_cj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cj)

_CYR = re.compile(r"[А-Яа-яЁё]")
_LAT = re.compile(r"[A-Za-z]")
_CJK = re.compile(r"[぀-鿿　-〿]")
_DIG = re.compile(r"\d")
_VOWEL = re.compile(r"[aeiouyаеёиоуыэюя]", re.I)
_WORDLIKE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'\-]{1,19}$")
_REPEAT = re.compile(r"(.)\1\1\1")
_PUNCT = re.compile(r"[^\w\s]")
_HTML = re.compile(r"<(table|td|th|tr|thead|tbody)\b")
_NUMEND = re.compile(r"\d\s*$")
_DEFDASH = re.compile(r"\S\s[—–-]\s\S")          # "term — definition" (glossary)
_URL = re.compile(r"https?://|doi:|www\.")

# The classifier is ONE model over every junk class (TODO G3.8), so the features must carry a signal
# for each: figure garbage (glyphs), ToC (dotted leaders), index/glossary (short lines, page-number
# lines, definition dashes), exercises (MC options, numbered questions, '?'), bibliography (cite keys,
# urls). We reuse chunk_judge's trusted regexes as NUMERIC features rather than boolean gates — the
# model learns the thresholds and combinations the rules had to hardcode.
FEATURE_NAMES = [
    # surface / script
    "weird_density", "cyr_ratio", "lat_ratio", "cjk_ratio", "digit_ratio",
    "wordlike_ratio", "numeric_tok_ratio", "repeat_density", "punct_ratio",
    "avg_tok_len", "n_tokens", "content_len", "html_table",
    # apparatus signals (ToC / index / glossary / exercise / bibliography)
    "toc_leader_per_line", "citekey_per_line", "mc_option_per_line",
    "numbered_q_per_line", "question_per_line", "short_line_ratio",
    "num_end_line_ratio", "def_dash_line_ratio", "url_per_line", "n_lines",
]


def surface_features(text: str) -> list[float]:
    toks = text.split()
    n = max(1, len(toks))
    chars = max(1, len(text))
    lines = [ln for ln in text.splitlines() if ln.strip()]
    nl = max(1, len(lines))
    weird = len(_cj._WEIRD.findall(text))
    wordlike = sum(1 for t in toks if _WORDLIKE.match(t) and _VOWEL.search(t))
    numeric = sum(1 for t in toks if any(c.isdigit() for c in t))
    short = sum(1 for ln in lines if len(ln.strip()) < 40)
    num_end = sum(1 for ln in lines if _NUMEND.search(ln))
    def_dash = sum(1 for ln in lines if _DEFDASH.search(ln))
    return [
        weird / chars,
        len(_CYR.findall(text)) / chars,
        len(_LAT.findall(text)) / chars,
        len(_CJK.findall(text)) / chars,
        len(_DIG.findall(text)) / chars,
        wordlike / n,
        numeric / n,
        len(_REPEAT.findall(text)) / chars,
        len(_PUNCT.findall(text)) / chars,
        chars / n,
        float(len(toks)),
        float(len(text)),
        float(bool(_HTML.search(text))),
        len(_cj._TOC_LEADER.findall(text)) / nl,
        len(_cj._CITEKEY.findall(text)) / nl,
        len(_cj._MC_OPTION.findall(text)) / nl,
        len(_cj._NUMBERED_Q.findall(text)) / nl,
        text.count("?") / nl,
        short / nl,
        num_end / nl,
        def_dash / nl,
        len(_URL.findall(text)) / nl,
        float(len(lines)),
    ]


def _index() -> str:
    r = requests.get(f"{ES_URL}/_cat/indices?h=index&format=json", auth=ES_AUTH, timeout=30)
    r.raise_for_status()
    idx = [x["index"] for x in r.json() if re.fullmatch(r"ragflow_[0-9a-f]{32}", x["index"])]
    if not idx:
        sys.exit(f"no ragflow chunk index found at {ES_URL}")
    return idx[0]


def scan(kb_id: str | None, limit: int | None):
    idx = _index()
    q = {"term": {"kb_id": kb_id}} if kb_id else {"match_all": {}}
    body = {"size": 1000, "query": q,
            "_source": ["content_with_weight", "q_1024_vec", "doc_id", "kb_id", "docnm_kwd"]}
    j = requests.post(f"{ES_URL}/{idx}/_search?scroll=5m", auth=ES_AUTH, json=body, timeout=120).json()
    sid, seen = j.get("_scroll_id"), 0
    try:
        while j["hits"]["hits"]:
            for h in j["hits"]["hits"]:
                s = h["_source"]
                yield h["_id"], s
                seen += 1
                if limit and seen >= limit:
                    return
            j = requests.post(f"{ES_URL}/_search/scroll", auth=ES_AUTH,
                              json={"scroll": "5m", "scroll_id": sid}, timeout=120).json()
            sid = j.get("_scroll_id")
    finally:
        if sid:
            requests.delete(f"{ES_URL}/_search/scroll", auth=ES_AUTH,
                            json={"scroll_id": sid}, timeout=30)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True, help="output .npz")
    ap.add_argument("--kb-id", default=None, help="limit to one kb_id (dataset id, not name)")
    ap.add_argument("--sample", type=int, default=None, help="stop after N chunks (quick probe)")
    args = ap.parse_args()

    ids, kbs, docs, names, surf, emb = [], [], [], [], [], []
    skipped = 0
    for cid, s in scan(args.kb_id, args.sample):
        vec = s.get("q_1024_vec")
        content = s.get("content_with_weight") or ""
        if not vec or not content:
            skipped += 1
            continue
        ids.append(cid)
        kbs.append(s.get("kb_id", ""))
        docs.append(s.get("doc_id", ""))
        names.append(s.get("docnm_kwd", ""))
        surf.append(surface_features(content))
        emb.append(np.asarray(vec, dtype=np.float16))
        if len(ids) % 20000 == 0:
            print(f"  {len(ids)} chunks...", flush=True)

    if not ids:
        sys.exit("no chunks collected")
    np.savez_compressed(
        args.out,
        ids=np.array(ids, dtype=object),
        kb=np.array(kbs, dtype=object),
        doc=np.array(docs, dtype=object),
        docnm=np.array(names, dtype=object),
        surf=np.asarray(surf, dtype=np.float32),
        emb=np.asarray(emb, dtype=np.float16),
        feat_names=np.array(FEATURE_NAMES, dtype=object),
    )
    print(f"wrote {len(ids)} chunks ({skipped} skipped) -> {args.out}")
    print(f"  surf: {np.asarray(surf).shape}  emb: {np.asarray(emb).shape} (float16)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
