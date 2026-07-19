#!/usr/bin/env python3
"""Agreement between two labelers in the labels DB — defenses 2 and 3 of the labeling protocol.

Defense 2 (spot-check): before training on a batch of labels, a second grader blind-labels a
sample; this computes agreement on the overlap (latest label per chunk per labeler). >=95% ->
trust the batch; below -> stop and fix the rubric. Defense 3: every disagreement is a RUBRIC BUG,
not noise — the dump is the adjudication worklist; each resolution becomes a rule or worked example
in RUBRIC.md (version bump, relabel what the amendment touches).

    ./label-agreement.py human qwen [--db labels.db] [--dump disagreements.jsonl]
    ./label-agreement.py human claude --dump adjudicate.jsonl
"""
import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

_spec = importlib.util.spec_from_file_location("label_db", Path(__file__).parent / "label-db.py")
_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_db)


def latest(conn, labeler: str) -> dict[str, dict]:
    return {r["chunk_id"]: dict(r) for r in
            conn.execute("SELECT * FROM latest WHERE labeler = ?", (labeler,))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a", help="labeler name (human | qwen | claude)")
    ap.add_argument("b", help="labeler name")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--dump", type=Path, default=None,
                    help="write disagreements JSONL (adjudication worklist)")
    args = ap.parse_args()

    conn = _db.connect(args.db) if args.db else _db.connect()
    A, B = latest(conn, args.a), latest(conn, args.b)
    common = sorted(set(A) & set(B))
    if not common:
        print(f"no overlapping chunks between '{args.a}' ({len(A)}) and '{args.b}' ({len(B)})")
        return 1

    agree = [c for c in common if A[c]["label"] == B[c]["label"]]
    pct = 100 * len(agree) / len(common)
    print(f"overlap {len(common)}: agree {len(agree)} ({pct:.1f}%)  "
          f"[protocol threshold: >=95% to trust the batch]")

    conf = Counter((A[c]["label"], B[c]["label"]) for c in common)
    labels = sorted({l for pair in conf for l in pair})
    print(f"\nconfusion ({args.a} rows x {args.b} cols):")
    w = max(len(l) for l in labels) + 1
    print(" " * w + "".join(f"{l[:8]:>9}" for l in labels))
    for la in labels:
        print(f"{la:<{w}}" + "".join(f"{conf.get((la, lb), 0) or '':>9}" for lb in labels))

    dis = [c for c in common if A[c]["label"] != B[c]["label"]]
    if dis:
        print(f"\n{len(dis)} disagreements (rubric bugs to adjudicate):")
        for c in dis[:10]:
            txt = " ".join((A[c].get("text") or B[c].get("text") or "").split())[:100]
            print(f"  {args.a}={A[c]['label']:16} {args.b}={B[c]['label']:16} {txt}")
        if len(dis) > 10:
            print(f"  ... and {len(dis) - 10} more")
        if args.dump:
            with args.dump.open("w", encoding="utf-8") as f:
                for c in dis:
                    f.write(json.dumps({
                        "chunk_id": c, args.a: A[c]["label"], args.b: B[c]["label"],
                        f"note_{args.a}": A[c].get("note", ""), f"note_{args.b}": B[c].get("note", ""),
                        "rubric_a": A[c].get("rubric_version"), "rubric_b": B[c].get("rubric_version"),
                        "docnm": A[c].get("docnm") or B[c].get("docnm"),
                        "text": A[c].get("text") or B[c].get("text"),
                        "resolution": ""}, ensure_ascii=False) + "\n")
            print(f"\nadjudication worklist -> {args.dump} "
                  f"(fill `resolution`, then amend RUBRIC.md + bump version)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
