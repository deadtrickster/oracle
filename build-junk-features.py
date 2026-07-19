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
_SENTEND = re.compile(r"[.!?:;…]\s*$")           # prose lines end in punctuation; ToC/index end in digits
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")        # bibliography: (2019), 1972, ...
_BIBLIO = re.compile(r"\bet al\.?|\bpp\.\s?\d|\bISBN\b|\bVol\.\s?\d", re.I)
_ANSKEY = re.compile(r"\b\d{1,3}[.)]\s*[a-eа-дA-EА-Д]\b")   # answer keys: "1. b  2. a  3. d"
_CODE = re.compile(r"::|->|=>|\(\)|[{};]|__|[a-z][A-Z]")     # operator tables / code — the jsonb scar:
                                                              # '?' counting once deleted an operator table
_PMARK = re.compile(r"\[\[p\.\d+\]\]")           # our own page markers (text-path docs)
# minimal stoplists — prose speaks in function words; apparatus (ToC lines, index entries,
# axis labels) does not. The single strongest cheap prose-vs-junk signal.
_STOP = {
    "the", "of", "and", "to", "in", "a", "is", "that", "for", "it", "as", "with", "on", "be",
    "are", "this", "by", "an", "or", "not", "from", "at", "which", "but", "can", "we", "when",
    "и", "в", "не", "на", "что", "с", "по", "это", "как", "к", "из", "у", "за", "от", "для",
    "то", "же", "или", "при", "так", "его", "но", "они", "она", "он", "мы", "вы", "а",
}

# ONE model over the WHOLE junk taxonomy (TODO G3.8) — every feature earns its place as a signal for
# a specific class, most of them paid for by a documented scar (DESIGN §4.1–4.3, §5.1b):
#   figure-OCR garbage → weird_density, repeat_density, uniq_tok_ratio (gibberish never recurs)
#   ToC               → toc_leader, num_end lines, low stopword/sentence-end, page_rel≈0 (train-time)
#   index/glossary    → short lines, alpha_sorted lines, def_dash, page_rel≈1 (train-time)
#   exercises/keys    → mc_option, numbered_q, '?' density, anskey pairs
#   bibliography      → citekey, url, year, biblio patterns
#   layout debris     → n_tokens tiny, stopword≈0, sentence_end≈0 (the 47-char §5.1b class)
#   running heads     → title_overlap with the doc name, uniq_tok_ratio low
#   operator tables / code (must be KEPT) → code_ratio, html_table
# The trusted regexes become NUMERIC features; the model learns thresholds the rules hardcoded.
FEATURE_NAMES = [
    # surface / script
    "weird_density", "cyr_ratio", "lat_ratio", "cjk_ratio", "digit_ratio",
    "wordlike_ratio", "numeric_tok_ratio", "repeat_density", "punct_ratio",
    "avg_tok_len", "n_tokens", "content_len", "html_table",
    # prose-ness
    "stopword_ratio", "sentence_end_line_ratio", "uniq_tok_ratio", "code_ratio",
    # apparatus signals
    "toc_leader_per_line", "citekey_per_line", "mc_option_per_line",
    "numbered_q_per_line", "question_per_line", "short_line_ratio",
    "num_end_line_ratio", "def_dash_line_ratio", "url_per_line", "n_lines",
    "year_per_line", "biblio_per_line", "anskey_per_line", "alpha_sorted_ratio",
    "title_overlap", "pmark_per_line",
    # provenance / position (page_rel & doc-centroid distance are derived at TRAIN time
    # from page_first + doc_id + emb, which are all stored alongside)
    "is_pdf", "is_md", "is_txt", "has_img", "page_first",
]


def surface_features(text: str, docnm: str, has_img: bool, page_first: float) -> list[float]:
    toks = text.split()
    low = [t.strip(".,;:!?()[]«»\"'").lower() for t in toks]
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
    sent_end = sum(1 for ln in lines if _SENTEND.search(ln))
    stop = sum(1 for t in low if t in _STOP)
    code = sum(1 for t in toks if _CODE.search(t))
    # index pages are alphabetized: fraction of adjacent line pairs whose first letters are ordered
    firsts = [ln.strip()[0].lower() for ln in lines if ln.strip() and ln.strip()[0].isalpha()]
    alpha_sorted = (sum(1 for a, b in zip(firsts, firsts[1:]) if b >= a) / (len(firsts) - 1)
                    if len(firsts) > 2 else 0.0)
    # running heads repeat the doc title inside the chunk
    title_toks = {w for w in re.split(r"[^a-zа-яё0-9]+", docnm.lower()) if len(w) > 3}
    overlap = (sum(1 for t in set(low) if t in title_toks) / len(title_toks)) if title_toks else 0.0
    ext = docnm.rsplit(".", 1)[-1].lower() if "." in docnm else ""
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
        stop / n,
        sent_end / nl,
        len(set(low)) / n,
        code / n,
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
        len(_YEAR.findall(text)) / nl,
        len(_BIBLIO.findall(text)) / nl,
        len(_ANSKEY.findall(text)) / nl,
        alpha_sorted,
        overlap,
        len(_PMARK.findall(text)) / nl,
        float(ext == "pdf"),
        float(ext == "md"),
        float(ext == "txt"),
        float(has_img),
        page_first,
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
            "_source": ["content_with_weight", "q_1024_vec", "doc_id", "kb_id", "docnm_kwd",
                        "page_num_int", "img_id"]}
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
        docnm = s.get("docnm_kwd", "")
        pages = s.get("page_num_int") or []
        page_first = float(pages[0]) if pages else -1.0   # -1 = no position (naive-parsed)
        ids.append(cid)
        kbs.append(s.get("kb_id", ""))
        docs.append(s.get("doc_id", ""))
        names.append(docnm)
        surf.append(surface_features(content, docnm, bool(s.get("img_id")), page_first))
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
