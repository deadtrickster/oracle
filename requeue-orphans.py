#!/usr/bin/env python3
"""Un-stick RAGFlow documents whose parse task died without ever reporting it.

    ./requeue-orphans.py --dry-run          show what is stuck and what would happen
    ./requeue-orphans.py                    fail the dead tasks, then re-queue them
    ./requeue-orphans.py --stale-minutes 30 be more conservative about "dead"

## The failure this repairs

A task executor that is SIGKILLed — `docker compose up` recreating the container, a reboot, an OOM
kill — cannot write a terminal status on the way out. No `try/except` catches SIGKILL. The document
is left `run=RUNNING` at whatever progress it had reached, and RAGFlow will never touch it again:
the re-parse endpoint refuses anything already RUNNING (`chunk_api.py`: "Can't parse document that is
currently being processed"). That guard is right — a live worker owns its document — but it cannot
tell a live worker from a dead one, so the row is stuck forever.

Observed: the arXiv queue drained 635 -> 160 and then stopped dead at exactly 45, twice in one night,
with the executor idle at ~1% CPU. The 45 were the documents in flight when the container was
recreated. They sat at `progress 0.8` — past chunking, inside embed+insert.

**A document stuck RUNNING is worse than a failed one.** `ingest-status.py` reads it as work in
progress, so it is a failure that never surfaces — the exact amnesia the corpus policy exists to
prevent. So this does not quietly retry: it records FAIL with a reason first, and only then re-queues.

## Order matters: tasks first, then the document, then the re-queue

`document.run` is a **projection of the task rows, not a fact.** The API server runs a progress-sync
thread that rewrites each document from its tasks every ~6 seconds. So marking a document FAIL while
a stale task row still exists is undone almost immediately — measured: 36 of 45 documents were back
to RUNNING twelve seconds after being marked FAIL, and the re-parse endpoint refused all 45 again.

Hence: **delete the dead task rows first.** That is not an improvisation; it is what RAGFlow's own
re-parse endpoint does (`TaskService.filter_delete([Task.doc_id == id])`) before enqueuing fresh work.
With nothing left to project from, `run=FAIL` sticks, the row becomes honest and visible, and the
public endpoint accepts the re-parse. It also means `ingest-corpus.py` would pick these up on its own
next cycle even if this script were never run again — it re-queues FAIL documents by design.

## What "dead" means here — and why it reads the TASK row, not the document row

There is no liveness API, so the signal is staleness. But it must be measured on the right row.

`document.update_time` is useless for this. It advances every ~6 seconds whether or not anything is
happening, because the API server runs a progress-sync thread that rewrites document rows from their
tasks. Measured directly on a document stuck at 0.8: `update_time` moved +50.8s over a 50s window
while `progress` did not move at all. Any staleness test against that column reports every stuck
document as freshly alive — which is exactly the trap that hid these 45 for hours.

`task.update_time` is only written by the executor that owns the task, and the separation is stark:

    live tasks   0-4 minutes stale
    dead tasks   43 and 161 minutes stale

So: a RUNNING document whose newest task has not been touched for `--stale-minutes` (default 15), or
which has no task row at all. The window is generous on purpose — being wrong in this direction
duplicates work, being wrong in the other direction loses a document forever.

Never run this while deliberately parsing something enormous with long silent stretches. It reports
what it is about to touch, and `--dry-run` shows it without acting.

## Re-parse DELETES the document's existing chunks

`chunk_api.py` calls `docStoreConn.delete({"doc_id": id})` before re-queuing. For a document stuck
mid-embed that is correct — its chunks are a partial write — but it is not a no-op, and an earlier
version of this file claimed the opposite in its docstring. Do not point this at a DONE document
expecting an incremental top-up.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request

BASE = os.environ.get("ORACLE_RAGFLOW_URL", "http://localhost:9380")
KEY = os.environ.get("ORACLE_RAGFLOW_KEY",
                     "ragflow-smywlJs3drgGxfKztifTmD3iNJ2lP6Uvq2-suiLQTGM")
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

MYSQL = ["docker", "exec", "docker-mysql-1", "mysql", "-uroot", "-pinfini_rag_flow", "-N", "-e"]


def sql(query: str) -> str:
    r = subprocess.run(MYSQL + [query], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"mysql failed: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def get(path):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(BASE + path, headers=HDR), timeout=60))


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers=HDR, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=300))


def stale_docs(minutes: int):
    """RUNNING documents whose newest TASK has not been touched for `minutes` (see module docstring
    for why this cannot be measured on document.update_time), longest stall first."""
    rows = sql(
        "select d.id, d.kb_id, k.name, d.name, round(d.progress,3), "
        "  coalesce(timestampdiff(minute, from_unixtime(max(t.update_time)/1000), now()), 9999) s "
        "from rag_flow.document d "
        "  join rag_flow.knowledgebase k on k.id = d.kb_id "
        "  left join rag_flow.task t on t.doc_id = d.id "
        "where d.run = '1' "
        "group by d.id, d.kb_id, k.name, d.name, d.progress "
        # No task row at all is also an orphan: the document was marked RUNNING and nothing was ever
        # enqueued for it, so waiting will not help. coalesce puts those first at 9999.
        f"having s >= {minutes} "
        "order by s desc")
    out = []
    for line in rows.splitlines():
        parts = line.split("\t")
        if len(parts) == 6:
            out.append(dict(zip(("id", "kb_id", "kb", "name", "progress", "stale_min"), parts)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("--stale-minutes", type=int, default=15,
                    help="how long RUNNING with no progress counts as dead (default 15)")
    a = ap.parse_args()

    try:
        docs = stale_docs(a.stale_minutes)
    except Exception as e:  # noqa: BLE001
        print(f"could not read the document table: {e}", file=sys.stderr)
        return 1

    if not docs:
        print(f"no documents stuck RUNNING for more than {a.stale_minutes}m — nothing to do")
        return 0

    by_kb: dict[str, list[dict]] = {}
    for d in docs:
        by_kb.setdefault(d["kb"], []).append(d)

    print(f"{len(docs)} document(s) stuck RUNNING with no progress for >{a.stale_minutes}m:\n")
    for kb, group in by_kb.items():
        print(f"  {kb}  ({len(group)})")
        for d in group[:4]:
            print(f"    {d['name'][:46]:46} progress={d['progress']:>6}  stalled {d['stale_min']}m")
        if len(group) > 4:
            print(f"    … and {len(group) - 4} more")
    print()

    if a.dry_run:
        print("--dry-run: would mark these FAIL, then re-queue them (which deletes their partial "
              "chunks). Nothing changed.")
        return 0

    ids = "','".join(d["id"] for d in docs)

    # 1. Delete the dead TASK rows FIRST. This is the step whose absence made the whole repair a
    #    no-op, and the reason is that `document.run` is a PROJECTION of the task rows, not a fact.
    #    The API server's progress-sync thread rewrites the document from its tasks every ~6s, so
    #    setting run=FAIL while a stale task still exists is undone within seconds: measured 36 of 45
    #    documents back to RUNNING twelve seconds after being marked FAIL.
    #
    #    Deleting them is not an improvisation — it is exactly what RAGFlow's own re-parse endpoint
    #    does (`TaskService.filter_delete([Task.doc_id == id])`) before enqueuing fresh work. These
    #    tasks describe an executor that no longer exists.
    deleted = sql(f"delete from rag_flow.task where doc_id in ('{ids}'); select row_count();")
    print(f"deleted {deleted.strip()} dead task row(s) — stops the progress-sync thread "
          f"resurrecting RUNNING")

    # 2. Now make the document rows honest, and it sticks, because there is nothing left to project
    #    from. CONVERT(0x0A USING utf8mb4) rather than a bare CHAR(10): the latter is a BINARY value
    #    in this MySQL and lands in the column as `base64:type15:Cg==` instead of a newline.
    note = ("task abandoned without a terminal status (executor killed mid-parse); "
            "marked FAIL by requeue-orphans.py")
    sql(f"update rag_flow.document set run='4', progress=-1, "
        f"progress_msg=concat(coalesce(progress_msg,''), convert(0x0A using utf8mb4), '{note}') "
        f"where id in ('{ids}')")
    print(f"marked {len(docs)} document(s) FAIL — visible in ingest-status.py")

    # 3. Now re-queue. Per dataset, and CHECK THE RESPONSE: the previous version of this script
    #    assumed code 102 was a harmless "already queued" warning and printed success for 45
    #    rejections in a row. 102 is a refusal.
    requeued = failed = 0
    for kb, group in by_kb.items():
        kb_id = group[0]["kb_id"]
        try:
            r = post(f"/api/v1/datasets/{kb_id}/chunks",
                     {"document_ids": [d["id"] for d in group]})
        except Exception as e:  # noqa: BLE001
            print(f"  {kb}: re-queue call failed: {e}")
            failed += len(group)
            continue
        if r.get("code") == 0:
            print(f"  {kb}: re-queued {len(group)}")
            requeued += len(group)
        else:
            print(f"  {kb}: REFUSED code={r.get('code')} {r.get('message', '')!r}")
            failed += len(group)

    # 4. Verify by re-reading, not by trusting step 3. An accepted POST means the task was enqueued;
    #    it does not mean the document left FAIL. This is the check whose absence hid the original bug.
    still = sql(f"select count(*) from rag_flow.document where id in ('{ids}') and run='4'")
    print(f"\nre-queued {requeued}, failed {failed}; {still.strip()} of {len(docs)} still FAIL "
          f"(expect 0 once the executor picks them up — re-run --dry-run in a few minutes)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
