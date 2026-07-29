#!/usr/bin/env python3
"""Sample SereneDB under the arXiv firehose: size, ingest rate, and query latency over time.

    ./serene-stress-watch.py                 one sample, printed
    ./serene-stress-watch.py --watch 300     sample every 5 minutes, append to the log
    ./serene-stress-watch.py --report        summarise the log so far

## Why this exists

The arXiv tailer is deliberately a firehose. Its purpose is not a curated shelf — it is load:

  1. stress SereneDB at a scale nothing else in this corpus reaches,
  2. find out whether retrieval DILUTES as the index grows, empirically rather than by argument,
  3. harvest junk at volume for the G3.8 classifier.

None of that is answered by watching the corpus get bigger. A stress test without measurement is
just filling a disk. So this records the numbers that would show a limit being reached, with
timestamps, so "it felt slower yesterday" becomes a series.

## What is sampled, and why each one

  rows / bytes        the obvious ones — but on their own they only prove the disk works.
  chunks per doc      drifts if parsing changes behaviour under load.
  QUERY LATENCY       the operational question. An index that ingests fine and answers slowly is
                      still broken. Sampled with a fixed query set so the numbers are comparable
                      across runs; the same queries every time, hot and cold.
  retrieval identity  the DILUTION question. For each probe we record which documents came back.
                      If a query that returned curated sources last week returns papers today, that
                      is dilution, and it is invisible in any size metric.

The probe queries are deliberately boring and stable. Two are software questions that SHOULD keep
answering from the curated shelves no matter how much arXiv arrives; two are research questions that
legitimately move. If the software probes start returning arXiv, the exclusion has failed or the
reranker has been overwhelmed.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = Path(os.environ.get("SERENE_STRESS_LOG", HERE / "corpus" / "serene-stress.jsonl"))

# Fixed probe set — never change these without starting a new log, or the series stops comparing.
PROBES = [
    ("software", "how does WAL flushing work in postgres"),
    ("software", "what does the rust borrow checker do with lifetimes"),
    ("research", "GPU approximate nearest neighbour search with quantization"),
    ("research", "learned index structures for databases"),
]


def sh(*cmd, timeout=60):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def psql(sql: str) -> str:
    return sh("docker", "exec", "-e", "PGPASSWORD=oracle-sdb", "oracle-serenedb", "psql",
              "-h", "127.0.0.1", "-p", "7890", "-U", "postgres", "-t", "-A", "-c", sql)


def sample() -> dict:
    rcv = _receiver()
    t = "ragflow_a73b470e7d6111f1b22afb6d9f0455fb"

    rows = psql(f"select count(*) from {t};") or "0"
    kbs = psql(f"select kb_id, count(*) from {t} group by kb_id;")
    per_kb = {}
    for line in kbs.splitlines():
        if "|" in line:
            k, n = line.split("|", 1)
            per_kb[k.strip()] = int(n.strip() or 0)

    out = {
        "at": time.time(),
        "rows": int(rows.strip() or 0),
        "kb_count": len(per_kb),
        "bytes": _dir_bytes("/mnt/data/oracle/serenedb"),
        "disk_free_gb": _free_gb("/mnt/data"),
        "probes": [],
    }

    # Latency + identity, per probe. Two passes: the first pays any cache cost, the second is the
    # steady-state number. Both are recorded — a widening gap between them is itself a signal.
    for kind, q in PROBES:
        row = {"kind": kind, "query": q}
        for pass_name in ("cold", "warm"):
            t0 = time.perf_counter()
            try:
                chunks, _ = rcv._retrieve(q, rcv._kb_ids())
                row[f"{pass_name}_ms"] = round((time.perf_counter() - t0) * 1000)
                if pass_name == "warm":
                    docs = [c.get("document_keyword", "?") for c in chunks[:6]]
                    row["top_docs"] = docs
                    row["arxiv_in_top"] = sum(1 for d in docs if d.startswith("arxiv__"))
            except Exception as e:  # noqa: BLE001
                row[f"{pass_name}_ms"] = None
                row["error"] = str(e)[:120]
                break
        out["probes"].append(row)
    return out


def _receiver():
    import importlib.util
    spec = importlib.util.spec_from_file_location("rcv", HERE / "oracle-capture-receiver.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dir_bytes(path: str) -> int:
    out = sh("du", "-sb", path, timeout=120)
    try:
        return int(out.split()[0])
    except Exception:  # noqa: BLE001
        return 0


def _free_gb(path: str) -> int:
    out = sh("df", "-BG", "--output=avail", path)
    try:
        return int(out.splitlines()[-1].strip().rstrip("G"))
    except Exception:  # noqa: BLE001
        return 0


def show(s: dict) -> None:
    print(f"rows {s['rows']:>10,}  |  {s['bytes'] / 1e9:6.1f} GB  |  "
          f"{s['disk_free_gb']:,} GB free  |  {s['kb_count']} KBs")
    for p in s["probes"]:
        docs = ", ".join(d[:26] for d in p.get("top_docs", [])[:3])
        print(f"  {p['kind']:8} cold {str(p.get('cold_ms')):>5}ms  warm {str(p.get('warm_ms')):>5}ms"
              f"  arxiv {p.get('arxiv_in_top', '?')}/6  {docs}")


def report() -> int:
    if not LOG.exists():
        print(f"no log at {LOG}")
        return 1
    rows = [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]
    if not rows:
        print("log is empty")
        return 1
    first, last = rows[0], rows[-1]
    hours = (last["at"] - first["at"]) / 3600 or 1e-9
    print(f"{len(rows)} samples over {hours:.1f}h")
    print(f"rows  {first['rows']:,} -> {last['rows']:,}  "
          f"({(last['rows'] - first['rows']) / hours:,.0f}/h)")
    print(f"bytes {first['bytes'] / 1e9:.1f} GB -> {last['bytes'] / 1e9:.1f} GB")
    print()
    print("warm latency by probe kind (ms):")
    for kind in ("software", "research"):
        series = [p["warm_ms"] for r in rows for p in r["probes"]
                  if p["kind"] == kind and p.get("warm_ms")]
        if series:
            print(f"  {kind:8} first {series[0]:>5}  last {series[-1]:>5}  "
                  f"min {min(series):>5}  max {max(series):>5}")
    print()
    print("dilution — arXiv share of the top 6 on SOFTWARE probes (should stay 0):")
    for r in (rows[0], rows[len(rows) // 2], rows[-1]):
        shares = [p.get("arxiv_in_top") for p in r["probes"] if p["kind"] == "software"]
        print(f"  {time.strftime('%m-%d %H:%M', time.localtime(r['at']))}  {shares}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch", type=int, metavar="SECONDS",
                    help="sample forever at this interval, appending to the log")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    if a.report:
        return report()

    LOG.parent.mkdir(parents=True, exist_ok=True)
    while True:
        s = sample()
        with open(LOG, "a") as fh:
            fh.write(json.dumps(s) + "\n")
        show(s)
        if not a.watch:
            return 0
        time.sleep(a.watch)


if __name__ == "__main__":
    sys.exit(main())
