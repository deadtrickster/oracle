#!/usr/bin/env python3
"""How's ingestion going? — a quick, tolerant RAGFlow parse-status report.

Queries every dataset and its documents, tallies parse states (DONE/RUNNING/FAIL),
shows overall % complete, what's actively parsing, and anything that failed.
Read-only. Tolerant of RAGFlow being slow under parse load (per-request retries).

  ./ingest-status.py            # summary
  ./ingest-status.py -v         # + actively-parsing docs and failures
  ORACLE_RAGFLOW_URL=... ORACLE_RAGFLOW_KEY=... ./ingest-status.py
"""
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def expected_kbs():
    """KB names declared in ingest-corpus.py, so KBs defined but not yet created in
    RAGFlow (e.g. cpp/cpp-libs mid-ingest) still show up as pending instead of vanishing."""
    try:
        src = (Path(__file__).parent / "ingest-corpus.py").read_text()
        return re.findall(r'^\s*\(\s*"([a-z0-9_-]+)"\s*,\s*"(?:naive|book|paper)"', src, re.M)
    except Exception:
        return []

BASE = os.environ.get("ORACLE_RAGFLOW_URL", "http://localhost:9380")
KEY = os.environ.get("ORACLE_RAGFLOW_KEY",
                     "ragflow-smywlJs3drgGxfKztifTmD3iNJ2lP6Uvq2-suiLQTGM")
VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv


def get(path, tries=4):
    """GET with retries — RAGFlow can stall for seconds while the task executor churns."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {KEY}"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def docs_of(did):
    out, page = [], 1
    while True:
        r = get(f"/api/v1/datasets/{did}/documents?page={page}&page_size=100")["data"]
        batch = r.get("docs", [])
        out += batch
        if len(batch) < 100:
            return out
        page += 1


def bar(frac, width=24):
    filled = int(round(frac * width))
    return "#" * filled + "." * (width - filled)


def doc_engine():
    """Which document store is ACTUALLY in use, and how big it is on disk.

    Worth reporting for two reasons. RAGFlow's own failure text says "please check log file and
    Elasticsearch/Infinity status!" on every insert error regardless of engine — stale boilerplate
    that names two backends this system does not use, and sends whoever reads it to the wrong place.

    And the engine can change without anyone choosing it: `.env` reads DOC_ENGINE from the invoking
    shell, so a `docker compose up` in a terminal that never exported it silently moved the whole
    corpus onto Elasticsearch. Nothing errored; this script cheerfully reported 50/50 done while the
    chunks went somewhere else. The only way to notice was to open the database. So it is printed
    here, every run, next to the numbers it qualifies.
    """
    import shutil
    import subprocess

    def sh(*cmd, timeout=8):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    if not shutil.which("docker"):
        return None
    engine = sh("docker", "exec", "docker-ragflow-cpu-1", "printenv", "DOC_ENGINE") or "unknown"
    out = {"engine": engine, "size": "", "rows": ""}

    if engine == "serenedb":
        path = sh("docker", "inspect", "oracle-serenedb", "--format",
                  "{{range .Mounts}}{{.Source}}{{end}}")
        out["size"] = sh("du", "-sh", path).split("\t")[0] if path else ""
        out["where"] = path
        n = sh("docker", "exec", "-e", "PGPASSWORD=oracle-sdb", "oracle-serenedb", "psql",
               "-h", "127.0.0.1", "-p", "7890", "-U", "postgres", "-t", "-c",
               "select count(*) from ragflow_a73b470e7d6111f1b22afb6d9f0455fb;")
        out["rows"] = n.strip()
    elif engine == "elasticsearch":
        path = sh("docker", "inspect", "docker-es01-1", "--format",
                  "{{range .Mounts}}{{.Source}}{{end}}")
        out["where"] = path
        out["size"] = sh("du", "-sh", path).split("\t")[0] if path else ""
    return out


def main():
    eng = doc_engine()
    if eng:
        line = f"document store: {eng['engine']}"
        if eng.get("rows"):
            line += f"  ·  {int(eng['rows']):,} rows" if eng["rows"].isdigit() else ""
        if eng.get("size"):
            line += f"  ·  {eng['size']} on disk"
        if eng.get("where"):
            line += f"  ·  {eng['where']}"
        print(line)
        if eng["engine"] not in ("serenedb", "elasticsearch"):
            print("  ⚠ could not determine the engine — is the ragflow container up?")

    try:
        ds = get("/api/v1/datasets?page_size=100")["data"]
    except Exception as e:  # noqa: BLE001
        print(f"error reaching RAGFlow at {BASE}: {e}")
        return 1

    tot_docs = tot_done = tot_chunks = 0
    grand = {}
    active, failed = [], []
    print(f"{'dataset':16}{'done/total':>12} {'chunks':>8}  progress")
    print("-" * 66)
    # fetch every dataset's documents concurrently — RAGFlow is slow under parse load and
    # serial pagination over thousands of docs is what makes this "hang" before printing.
    def fetch(d):
        try:
            return d, docs_of(d["id"]), None
        except Exception as e:  # noqa: BLE001
            return d, None, e
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(fetch, sorted(ds, key=lambda x: x["name"])))
    for d, docs, err in results:
        if err is not None:
            print(f"{d['name'][:16]:16}  (listing failed: {err})")
            continue
        states = {}
        for doc in docs:
            st = doc.get("run", "?")
            states[st] = states.get(st, 0) + 1
            grand[st] = grand.get(st, 0) + 1
            if st == "RUNNING" and (doc.get("progress", 0) or 0) > 0:
                active.append((d["name"], doc["name"], doc.get("progress", 0),
                               (doc.get("progress_msg", "") or "").replace("\n", " ")[-70:]))
            if st in ("FAIL", "CANCEL"):
                failed.append((d["name"], doc["name"],
                               (doc.get("progress_msg", "") or "").replace("\n", " ")[-90:]))
        done = states.get("DONE", 0)
        n = len(docs)
        # Sum the PER-DOCUMENT counts, not the dataset's `chunk_count`.
        #
        # `knowledgebase.chunk_num` is an accumulator that never decrements when chunks are deleted.
        # After a mass re-parse it read 196,782 for a KB that actually held 3,805 rows — a 50x
        # overstatement, and the number this tool had been printing all along. Per-document counts
        # are reset by the parser, so they track reality; the document store's own row count (in the
        # header) is the final arbiter, and the two should agree.
        ch = sum(doc.get("chunk_count", doc.get("chunk_num", 0)) or 0 for doc in docs)
        tot_docs += n
        tot_done += done
        tot_chunks += ch
        if n:
            frac = done / n
            extra = " ".join(f"{k}:{v}" for k, v in sorted(states.items()) if k != "DONE")
            print(f"{d['name'][:16]:16}{done:>6}/{n:<5} {ch:>8}  {bar(frac)} {frac*100:3.0f}%  {extra}")

    print("-" * 66)
    frac = tot_done / tot_docs if tot_docs else 1
    print(f"{'TOTAL':16}{tot_done:>6}/{tot_docs:<5} {tot_chunks:>8}  {bar(frac)} {frac*100:3.0f}%")
    print(f"states: {grand}")
    # surface KBs with no rows yet — the table above skips 0-doc datasets, so cpp/cpp-libs
    # mid-ingest (created-but-empty) or still-uncreated would otherwise vanish.
    present = {d["name"] for d, dd, e in results if e is None}
    empty = sorted(d["name"] for d, dd, e in results if e is None and not dd)
    pending = [k for k in expected_kbs() if k not in present]
    if empty:
        print(f"empty (created, no docs yet): {', '.join(empty)}")
    if pending:
        print(f"not created yet (defined in ingest-corpus.py): {', '.join(pending)}")
    if failed:
        # Cap the listing. arXiv alone is expected to produce tens of thousands of NUL-extraction
        # failures at ~3.5% of ~1.1M papers; printing them all turns a status line into a wall and
        # buries the one failure that is actually new. The COUNT is the signal; the examples are
        # illustration. `-v` shows more.
        cap = 20 if VERBOSE else 5
        print(f"\n⚠ {len(failed)} FAILED/CANCELLED"
              + (f" (showing {min(cap, len(failed))}; -v for more)" if len(failed) > cap else "")
              + ":")
        nul = 0
        for name, dn, msg in failed[:cap]:
            # RAGFlow appends "please check log file and Elasticsearch/Infinity status!" to every
            # insert error, naming backends this system does not run. Drop it and say what the
            # engine actually is (printed at the top), so nobody goes looking at an Infinity that
            # was never here.
            clean = msg.replace("please check log file and Elasticsearch/Infinity status!", "").strip()
            if "NUL (0x00)" in clean:
                nul += 1
            print(f"  {name:12} {dn[:36]:36} {clean[-120:]}")
        if nul:
            print(f"\n  {nul} of these are NUL-byte extraction failures — a broken CID font map, so")
            print("  the text layer is unusable and the paper needs OCR rather than a retry.")
            print("  Ids are listed in corpus/arxiv-needs-ocr.txt.")
    if VERBOSE and active:
        print(f"\nActively parsing ({len(active)}), highest progress first:")
        for name, dn, pr, msg in sorted(active, key=lambda x: -x[2])[:15]:
            print(f"  {pr:>5.0%}  {name:12} {dn[:34]:34} {msg}")
    elif active and not VERBOSE:
        print(f"({len(active)} docs actively parsing — run with -v to see them)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
