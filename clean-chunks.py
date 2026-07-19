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


# RAGFlow keeps chunks in Elasticsearch (DOC_ENGINE=elasticsearch). For a full-KB scan we read them
# DIRECT from ES (scroll, 1000/req) instead of the 100-per-page HTTP API — far faster over 161 books.
# Writes still go through the RAGFlow API so embeddings + token fields regenerate (see DESIGN §4.3).
ES_URL = os.environ.get("ORACLE_ES_URL", "http://localhost:1200")
ES_AUTH = tuple(os.environ.get("ORACLE_ES_AUTH", "elastic:infini_rag_flow").split(":", 1))


def _es_index() -> str:
    r = requests.get(f"{ES_URL}/_cat/indices?h=index&format=json", auth=ES_AUTH, timeout=30)
    r.raise_for_status()
    idx = [x["index"] for x in r.json() if re.fullmatch(r"ragflow_[0-9a-f]{32}", x["index"])]
    if not idx:
        raise RuntimeError(f"no ragflow chunk index (ragflow_<tenant>) found at {ES_URL}")
    return idx[0]


def es_chunks(kb_id: str):
    """Yield {id, doc_id, docnm, content} for every chunk of a KB, read direct from ES via scroll."""
    idx = _es_index()
    body = {"size": 1000, "query": {"term": {"kb_id": kb_id}},
            "_source": ["content_with_weight", "doc_id", "docnm_kwd"]}
    j = requests.post(f"{ES_URL}/{idx}/_search?scroll=2m", auth=ES_AUTH, json=body, timeout=90)
    j.raise_for_status()
    j = j.json()
    sid = j.get("_scroll_id")
    try:
        while j["hits"]["hits"]:
            for h in j["hits"]["hits"]:
                s = h["_source"]
                yield {"id": h["_id"], "doc_id": s.get("doc_id"),
                       "docnm": s.get("docnm_kwd"), "content": s.get("content_with_weight", "")}
            r = requests.post(f"{ES_URL}/_search/scroll", auth=ES_AUTH,
                              json={"scroll": "2m", "scroll_id": sid}, timeout=90)
            r.raise_for_status()
            j = r.json()
            sid = j.get("_scroll_id")
    finally:
        if sid:
            requests.delete(f"{ES_URL}/_search/scroll", auth=ES_AUTH,
                            json={"scroll_id": sid}, timeout=30)


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
    ap.add_argument("kb", nargs="?", help="dataset name (e.g. books, bio-books). "
                    "Not needed with --junk-apply (the worklist carries kb/doc ids).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--junk-out", type=Path, default=None,
                    help="DIAGRAM-OCR pass: scan the KB direct from ES, and for every chunk carrying "
                         "flattened-diagram garbage write (chunk_id, doc_id, cleaned_text, snippet) to "
                         "this worklist file. Read-only — nothing is deleted. Review, then --junk-apply.")
    ap.add_argument("--junk-apply", type=Path, default=None,
                    help="Consume a --junk-out worklist: for each record DELETE the old chunk and, if "
                         "cleaned text remains, ADD it back as a new chunk (which re-embeds). "
                         "PATCH-in-place would keep the garbage embedding, so we remove+reingest.")
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

    # --- APPLY worklist (remove+reingest): needs no KB scan; records carry kb/doc ids ---
    if args.junk_apply:
        recs = [json.loads(ln) for ln in args.junk_apply.read_text(encoding="utf-8").splitlines()
                if ln.strip()]
        removed = readded = del_only = 0
        for r in recs:
            if not args.dry_run:
                api("DELETE", f"/datasets/{r['kb_id']}/documents/{r['doc_id']}/chunks",
                    json={"chunk_ids": [r["chunk_id"]]})
            removed += 1
            if r["cleaned"].strip():
                if not args.dry_run:
                    api("POST", f"/datasets/{r['kb_id']}/documents/{r['doc_id']}/chunks",
                        json={"content": r["cleaned"]})
                readded += 1
            else:
                del_only += 1
        print(f"{'DRY RUN — ' if args.dry_run else ''}remove+reingest: {removed} removed, "
              f"{readded} re-added clean (re-embedded), {del_only} all-garbage deleted")
        return 0

    ds = {d["name"]: d["id"] for d in api("GET", "/datasets?page_size=100")["data"]}
    if not args.kb:
        print("kb is required (unless --junk-apply)", file=sys.stderr)
        return 1
    if args.kb not in ds:
        print(f"no such dataset: {args.kb}", file=sys.stderr)
        return 1
    dsid = ds[args.kb]

    # --- DETECT diagram-OCR garbage → worklist (read direct from ES, no mutation) ---
    if args.junk_out:
        seen = flagged = allgarbage = 0
        with args.junk_out.open("w", encoding="utf-8") as f:
            for c in es_chunks(dsid):
                seen += 1
                cleaned = _judge.find_diagram_garbage(c["content"])
                if cleaned is None:
                    continue
                flagged += 1
                allgarbage += (cleaned.strip() == "")
                f.write(json.dumps({"kb_id": dsid, "doc_id": c["doc_id"], "chunk_id": c["id"],
                                    "docnm": c["docnm"], "cleaned": cleaned, "orig": c["content"]},
                                   ensure_ascii=False) + "\n")
        print(f"scanned {seen} chunks in {args.kb}: flagged {flagged} diagram-junk "
              f"({allgarbage} all-garbage) -> {args.junk_out}")
        print(f"review it, then: clean-chunks.py --junk-apply {args.junk_out}")
        return 0

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
