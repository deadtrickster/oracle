#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "requests"]
# ///
"""Opus junk-labeling fleet — PLANNER / IMPORTER (agents are spawned by Claude, like opus-fleet.py).

The OCR fleet's protocol, applied to labeling: subagents NEVER touch the database. The planner
exports batch files (chunk text + ids) into an in-tree directory; each Opus agent Reads ONE batch
plus RUBRIC.md and Writes ONE output file of labels; this script is the ONLY DB writer, importing
outputs sequentially. That answers the SQLite-concurrency question by construction — many agents,
one writer — and gives disk-truth resume: a batch is done iff its output file exists, so any agent
can die (wifi, usage limits) and be respawned with zero loss.

  corpus/labels/batches/b-NNNN.json    planner-exported work units (25 chunks each)
  corpus/labels/out/b-NNNN.jsonl       agent-written labels, one JSON object per chunk
  labels.db                            single-writer import target (labeler='opus', certainty 0..1)

Queue policy mirrors label-ui.py: nominator-driven candidates per class (recall-oriented, false
positives welcome — the labeler decides) + a random slice so the set isn't only what heuristics
already suspect. Chunk text is fetched from ES at export time and STORED IN THE BATCH FILE, so
agents need no network and label exactly what the classifier will train on.

    ./label-fleet.py --plan 4              # export the next 4 batches (needs features.npz)
    ./label-fleet.py --status
    ./label-fleet.py --import              # validate + ingest all un-imported outputs
    ./label-fleet.py --queue-size          # how many candidates the current queue holds
"""
import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import numpy as np
import requests

REPO = Path(__file__).parent
BATCHES = REPO / "corpus/labels/batches"
OUT = REPO / "corpus/labels/out"
FEATURES = REPO / "features.npz"
BATCH = 25
# Queue target raised 2026-07-22 (his call: "3666 is small, lets do 10%"): 10% of the corpus.
# Nominator candidates are capped per class; the RANDOM slice then FILLS to the target — the
# nominators are measurably noisy (~76% of their picks label CLEAN), so the random mass both
# de-biases the set and gives the classifier an honest prior over the real class distribution.
TARGET = 24766         # 10% of 247,665
PER_CLASS = 1000       # nominator candidates per class in the queue
SEED = 20260722

ES_URL = os.environ.get("ORACLE_ES_URL", "http://localhost:1200")
ES_AUTH = tuple(os.environ.get("ORACLE_ES_AUTH", "elastic:infini_rag_flow").split(":", 1))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_db = _load("label-db")
_lj = _load("label-junk")


def es_index() -> str:
    r = requests.get(f"{ES_URL}/_cat/indices?h=index&format=json", auth=ES_AUTH, timeout=30)
    r.raise_for_status()
    return next(x["index"] for x in r.json() if re.fullmatch(r"ragflow_[0-9a-f]{32}", x["index"]))


def fetch_chunks(idx: str, ids: list[str]) -> dict[str, dict]:
    # _source filtering must ride per-doc entries — ES 400s a top-level _source with "ids".
    r = requests.post(f"{ES_URL}/{idx}/_mget", auth=ES_AUTH, timeout=120,
                      json={"docs": [{"_id": i, "_source": ["content_with_weight", "kb_id",
                                                            "doc_id", "docnm_kwd"]} for i in ids]})
    r.raise_for_status()
    out = {}
    for d in r.json()["docs"]:
        if d.get("found"):
            s = d["_source"]
            out[d["_id"]] = {"text": s.get("content_with_weight", ""), "kb_id": s.get("kb_id", ""),
                             "doc_id": s.get("doc_id", ""), "docnm": s.get("docnm_kwd", "")}
    return out


def build_queue() -> list[tuple[str, str]]:
    """[(chunk_id, nominated_class)], deterministic under SEED — same recipe as label-ui.py."""
    if not FEATURES.is_file():
        sys.exit(f"missing {FEATURES} — run: uv run build-junk-features.py --out features.npz")
    d = np.load(FEATURES, allow_pickle=True)
    ids = [str(x) for x in d["ids"]]
    fn = [str(x) for x in d["feat_names"]]
    masks = _lj.nominators(d["surf"], fn)
    rng = np.random.default_rng(SEED)
    picked: dict[int, str] = {}
    for cls, mask in masks.items():
        rows = [r for r in np.where(mask)[0] if r not in picked]
        for r in rng.permutation(rows)[:PER_CLASS]:
            picked[int(r)] = cls
    pool = [r for r in range(len(ids)) if r not in picked]
    n_random = max(0, TARGET - len(picked))
    for r in rng.permutation(pool)[:n_random]:
        picked[int(r)] = "RANDOM"
    order = rng.permutation(sorted(picked)).tolist()
    return [(ids[r], picked[r]) for r in order]


def queued_or_done() -> set[str]:
    """Chunk ids already exported to any batch file — the planner's dedup set (disk truth)."""
    seen = set()
    for f in BATCHES.glob("b-*.json"):
        seen.update(c["chunk_id"] for c in json.loads(f.read_text(encoding="utf-8"))["chunks"])
    return seen


def next_batch_no() -> int:
    nums = [int(f.stem.split("-")[1]) for f in BATCHES.glob("b-*.json")]
    return max(nums, default=0) + 1


def plan(k: int):
    BATCHES.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    conn = _db.connect()
    opus_done = _db.labeled_ids(conn, "opus")
    seen = queued_or_done()
    queue = [(cid, nom) for cid, nom in build_queue()
             if cid not in seen and cid not in opus_done]
    idx = es_index()
    emitted = 0
    while emitted < k and queue:
        take, queue = queue[:BATCH], queue[BATCH:]
        chunks = fetch_chunks(idx, [c for c, _ in take])
        rows = [{"chunk_id": cid, "nominated": nom, **chunks[cid]}
                for cid, nom in take if cid in chunks and chunks[cid]["text"].strip()]
        if not rows:
            continue
        n = next_batch_no()
        path = BATCHES / f"b-{n:04}.json"
        path.write_text(json.dumps({"batch": n, "chunks": rows}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        print(json.dumps({"batch": n, "file": str(path), "out": str(OUT / f"b-{n:04}.jsonl"),
                          "n_chunks": len(rows)}))
        emitted += 1
    if emitted == 0:
        print("QUEUE DRAINED — nothing left to export")


def status():
    batches = sorted(BATCHES.glob("b-*.json"))
    outs = {f.stem for f in OUT.glob("b-*.jsonl")}
    done = sum(1 for b in batches if b.stem in outs)
    conn = _db.connect()
    n_opus = len(_db.labeled_ids(conn, "opus"))
    n_human = len(_db.labeled_ids(conn, "human"))
    print(f"  batches exported : {len(batches)}")
    print(f"  batches labeled  : {done}  (output file present)")
    print(f"  db rows          : opus={n_opus}  human={n_human}")


def do_import():
    """Sequential, validating, idempotent: an output file is ingested once (tracked in the DB),
    and every row must carry a known class and a sane certainty before anything is written."""
    conn = _db.connect()
    conn.execute("CREATE TABLE IF NOT EXISTS imported_files (fname TEXT PRIMARY KEY, "
                 "ts TEXT NOT NULL DEFAULT (datetime('now', 'localtime')))")
    conn.commit()
    already = {r[0] for r in conn.execute("SELECT fname FROM imported_files")}
    batches = {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in BATCHES.glob("b-*.json")}
    total = 0
    for f in sorted(OUT.glob("b-*.jsonl")):
        if f.name in already:
            continue
        meta = {c["chunk_id"]: c for c in batches.get(f.stem, {}).get("chunks", [])}
        rows, bad = [], []
        for ln, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError as e:
                bad.append(f"line {ln}: not JSON ({e})")
                continue
            cid, label = r.get("chunk_id"), r.get("label")
            cert = r.get("certainty")
            if cid not in meta:
                bad.append(f"line {ln}: chunk {cid!r} not in batch {f.stem}")
            elif label not in _db.CLASSES:
                bad.append(f"line {ln}: unknown label {label!r}")
            elif not isinstance(cert, (int, float)) or not (0.0 <= float(cert) <= 1.0):
                bad.append(f"line {ln}: bad certainty {cert!r}")
            else:
                rows.append(r)
        if bad:
            print(f"  {f.name}: REFUSED ({len(bad)} problems; nothing imported from this file)")
            for b in bad[:6]:
                print(f"      {b}")
            continue
        for r in rows:
            m = meta[r["chunk_id"]]
            _db.add_label(conn, chunk_id=r["chunk_id"], label=r["label"], labeler="opus",
                          kb_id=m["kb_id"], doc_id=m["doc_id"], docnm=m["docnm"],
                          note=r.get("note", ""), nominated=m.get("nominated", ""),
                          text=m["text"], certainty=float(r["certainty"]),
                          spans=r.get("spans") or None)
        conn.execute("INSERT INTO imported_files (fname) VALUES (?)", (f.name,))
        conn.commit()
        total += len(rows)
        print(f"  {f.name}: imported {len(rows)} labels")
    print(f"TOTAL imported this run: {total}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", type=int, metavar="N", help="export next N batches")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--import", dest="do_import", action="store_true")
    ap.add_argument("--queue-size", action="store_true")
    a = ap.parse_args()
    if a.plan:
        plan(a.plan)
    elif a.do_import:
        do_import()
    elif a.queue_size:
        q = build_queue()
        seen = queued_or_done()
        print(f"queue: {len(q)} candidates, {len([1 for c, _ in q if c not in seen])} unexported")
    else:
        status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
