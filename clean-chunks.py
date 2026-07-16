#!/usr/bin/env python3
"""Filter CHUNKS after parsing — the one place both ingestion paths converge.

WHY THIS EXISTS (and why clean-corpus.py is not enough):

We have two ingestion paths and only one of them was filtered.

    Cyrillic / scanned PDFs  ->  pdftotext / OCR  ->  .txt  ->  clean-corpus.py  ->  RAGFlow
    Latin PDFs               ->  DeepDoc (page+bbox positions, figures)  --------->  RAGFlow
                                                                          ^
                                                              never filtered at all

So the English textbooks — whose "Review Questions" sections are the same retrieval poison, in a
language our rule is blind to — went in completely unfiltered. That is an architectural mistake:
we filtered the FORMAT (text files) instead of the THING (chunks).

Chunks are what actually gets embedded and retrieved. Filter there and it works for every parser.

Two operations, deliberately different in severity:

  DELETE  a chunk that is entirely exercise material (a quiz section) — it competes with, and beats,
          the chapter that answers the query, because a question-shaped chunk matches a
          question-shaped query. See FINDINGS.md.

  PATCH   a chunk that carries per-page BOILERPLATE (a watermark, a running header). Lambert's RLHF
          book repeats "Licensed to Iliia Khaprov" on all 310 pages, so it rides along inside nearly
          every retrieved chunk. It barely moves the ranking (low IDF) but it is pure context
          occupation — repeated in every chunk we hand the model. Strip the line, keep the chunk.

Boilerplate is detected STATISTICALLY, not by a pattern: a short line that appears in most of a
document's chunks is furniture, not content. That is the one rule here that cannot misfire on
technical prose — a real sentence does not appear on 90% of a book's pages.

    ./clean-chunks.py books --dry-run
    ./clean-chunks.py bio-books
"""
import argparse
import importlib.util
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import requests

RAGFLOW = os.environ.get("ORACLE_RAGFLOW_URL", "http://localhost:9380")
KEY = os.environ.get("ORACLE_RAGFLOW_KEY",
                     "ragflow-smywlJs3drgGxfKztifTmD3iNJ2lP6Uvq2-suiLQTGM")
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

# reuse the SAME question-run rule as the text path — one definition of "exercise material"
_spec = importlib.util.spec_from_file_location("clean_corpus", Path(__file__).parent / "clean-corpus.py")
_cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cc)

_spec2 = importlib.util.spec_from_file_location("chunk_judge", Path(__file__).parent / "chunk_judge.py")
_judge = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(_judge)

BOILERPLATE_MIN_SHARE = 0.60      # a line in >=60% of a document's chunks is furniture
BOILERPLATE_MAX_LEN = 90          # ...and furniture is short
MIN_CHUNKS_FOR_STATS = 20         # don't infer "appears everywhere" from a handful of chunks


def api(method, path, **kw):
    r = requests.request(method, f"{RAGFLOW}/api/v1{path}", headers=HDR, timeout=120, **kw)
    r.raise_for_status()
    return r.json()


def all_docs(dsid):
    """Every document — PAGINATED.

    This was a silent bug: a single `?page_size=100` call looks like a complete list and quietly
    truncates. `postgres` has 219 documents, so 119 of them — including every Postgres Pro book —
    were never judged, and the run reported "0 chunks deleted" as if that were a finding. A cap that
    masquerades as a result is the same failure mode as RAGFlow's 128 MB parser limit reporting
    `DONE, progress=1.0` with zero chunks.
    """
    out, page = [], 1
    while True:
        docs = ((api("GET", f"/datasets/{dsid}/documents?page={page}&page_size=100")
                 .get("data") or {}).get("docs")) or []
        if not docs:
            return out
        out.extend(docs)
        page += 1


def all_chunks(dsid, docid):
    out, page = [], 1
    while True:
        data = api("GET", f"/datasets/{dsid}/documents/{docid}/chunks"
                          f"?page={page}&page_size=100").get("data") or {}
        chunks = data.get("chunks") or []
        if not chunks:
            return out
        out.extend(chunks)
        page += 1


def boilerplate_lines(chunks) -> set[str]:
    """Lines that appear in most chunks of this document — watermarks, running headers."""
    if len(chunks) < MIN_CHUNKS_FOR_STATS:
        return set()
    seen = Counter()
    for c in chunks:
        lines = {ln.strip() for ln in c["content"].splitlines() if ln.strip()}
        for ln in lines:
            if len(ln) <= BOILERPLATE_MAX_LEN:
                seen[ln] += 1
    threshold = len(chunks) * BOILERPLATE_MIN_SHARE
    return {ln for ln, n in seen.items() if n >= threshold}


def is_all_exercise_by_rule(content: str) -> bool:
    blocks = [b for b in re.split(r"\n\s*\n", content) if b.strip()]
    if not blocks:
        return False
    return all(_cc.question_block_mask(blocks))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kb", help="dataset name (e.g. books, bio-books)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-strip-questions", action="store_true",
                    help="only strip boilerplate; leave exercise chunks alone (right for reference "
                         "manuals — see clean-corpus.py)")
    ap.add_argument("--audit", type=Path, default=None,
                    help="write every judge verdict (passage + reasoning) as JSONL, so a human can check what the model decided to delete")
    ap.add_argument("--judge", action="store_true",
                    help="use the LLM judge (qwen) on rule-flagged candidates instead of trusting "
                         "the rule. STRONGLY RECOMMENDED: the rule is blind to multiple-choice "
                         "questions and once deleted a jsonb operator table. Validated 7/7 against "
                         "tests/corpus-filter — run tests/test-judge.py before trusting it.")
    args = ap.parse_args()

    ds = {d["name"]: d["id"] for d in api("GET", "/datasets?page_size=100")["data"]}
    if args.kb not in ds:
        print(f"no such dataset: {args.kb}", file=sys.stderr)
        return 1
    dsid = ds[args.kb]
    audit = args.audit.open('w', encoding='utf-8') if args.audit else None

    docs = all_docs(dsid)
    print(f"{len(docs)} documents in {args.kb}")
    total_del = total_strip = 0

    for d in docs:
        chunks = all_chunks(dsid, d["id"])
        if not chunks:
            continue
        boiler = boilerplate_lines(chunks)

        # THE CASCADE: a cheap recall-oriented rule flags candidates; the judge decides.
        # Rules cannot score 283k chunks *well*; qwen cannot score 283k chunks *fast*.
        to_delete, to_patch, judged, kept_by_judge = [], [], 0, 0
        for c in chunks:
            if not args.no_strip_questions:
                if args.judge:
                    # A pure table of contents is unambiguous — drop it without a judge call.
                    if _judge.is_obvious_toc(c["content"]):
                        if audit:
                            audit.write(json.dumps({
                                "doc": d["name"], "chunk_id": c["id"], "verdict": "DROP",
                                "why": "obvious TOC (>=4 dotted-leader lines) — no judge call",
                                "text": " ".join(c["content"].split())[:400],
                            }, ensure_ascii=False) + "\n")
                            audit.flush()
                        to_delete.append(c["id"])
                        continue
                    if _judge.is_candidate(c["content"]):
                        judged += 1
                        verdict, why = _judge.judge(c["content"])
                        # Every verdict is logged. A model that DELETES corpus content must leave an
                        # audit trail a human can read — otherwise we have swapped a rule we could
                        # inspect for a model we cannot.
                        if audit:
                            audit.write(json.dumps({
                                "doc": d["name"], "chunk_id": c["id"], "verdict": verdict,
                                "why": why, "text": " ".join(c["content"].split())[:400],
                            }, ensure_ascii=False) + "\n")
                            audit.flush()
                        if verdict == "DROP":
                            to_delete.append(c["id"])
                            continue
                        kept_by_judge += 1
                elif is_all_exercise_by_rule(c["content"]):
                    to_delete.append(c["id"])
                    continue
            if boiler:
                kept = [ln for ln in c["content"].splitlines() if ln.strip() not in boiler]
                new = "\n".join(kept)
                if new != c["content"] and new.strip():
                    to_patch.append((c["id"], new))

        extra = f" judged={judged} judge-kept={kept_by_judge}" if args.judge else ""
        print(f"{d['name'][:44]:46} chunks={len(chunks):5} "
              f"delete={len(to_delete):4} strip={len(to_patch):5}{extra}"
              + (f"  boilerplate={sorted(boiler)[:1]}" if boiler else ""))

        if not args.dry_run:
            if to_delete:
                api("DELETE", f"/datasets/{dsid}/documents/{d['id']}/chunks",
                    json={"chunk_ids": to_delete})
            for cid, new in to_patch:
                api("PATCH", f"/datasets/{dsid}/documents/{d['id']}/chunks/{cid}",
                    json={"content": new})
        total_del += len(to_delete)
        total_strip += len(to_patch)

    if audit:
        audit.close()
    print(f"\n{'DRY RUN — ' if args.dry_run else ''}"
          f"{total_del} chunks deleted, {total_strip} chunks stripped of boilerplate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
