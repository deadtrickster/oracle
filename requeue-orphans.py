#!/usr/bin/env python3
"""Re-queue RAGFlow parse tasks orphaned by a restart.

When the box reboots (or the RAGFlow containers are killed) mid-parse, the docs stay marked
RUNNING in the DB but their workers are gone. RAGFlow never retries them — the task executor
skips anything already RUNNING — so they sit stuck forever. This finds them and re-triggers
parsing (POST /datasets/{id}/chunks), which is non-destructive: it re-queues the doc, it does
NOT delete existing chunks.

  ./requeue-orphans.py            # re-queue every RUNNING doc
  ./requeue-orphans.py --dry-run  # just show what would be re-queued

RUN THIS ONLY AFTER A RESTART. Mid-session, a RUNNING doc is probably genuinely being parsed
by a live worker, and re-queuing it would duplicate work. Right after a boot, nothing survived,
so every RUNNING doc is by definition orphaned.
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("ORACLE_RAGFLOW_URL", "http://localhost:9380")
KEY = os.environ.get("ORACLE_RAGFLOW_KEY",
                     "ragflow-smywlJs3drgGxfKztifTmD3iNJ2lP6Uvq2-suiLQTGM")
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
DRY = "--dry-run" in sys.argv


def get(path):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(BASE + path, headers=HDR), timeout=60))


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers=HDR, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=180))


def main():
    try:
        datasets = get("/api/v1/datasets?page_size=100")["data"]
    except Exception as e:  # noqa: BLE001
        print(f"error reaching RAGFlow at {BASE}: {e}")
        return 1
    total = 0
    for d in datasets:
        stuck, page = [], 1
        while True:
            docs = get(f"/api/v1/datasets/{d['id']}/documents?page={page}&page_size=100")["data"]["docs"]
            stuck += [x["id"] for x in docs if x.get("run") == "RUNNING"]
            if len(docs) < 100:
                break
            page += 1
        if not stuck:
            continue
        total += len(stuck)
        if DRY:
            print(f"  {d['name']}: would re-queue {len(stuck)} orphaned docs")
            continue
        try:
            r = post(f"/api/v1/datasets/{d['id']}/chunks", {"document_ids": stuck})
            # code 102 is a warning ("already queued"), the re-queue still lands
            print(f"  {d['name']}: re-queued {len(stuck)} orphaned docs (code={r.get('code')})")
        except Exception as e:  # noqa: BLE001
            print(f"  {d['name']}: FAILED {e}")
    print(f"\n{'would re-queue' if DRY else 're-queued'} {total} orphaned docs"
          if total else "\nno orphaned RUNNING docs — nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
