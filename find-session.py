#!/usr/bin/env python3
"""Find and summarize Claude Code / local-qwen session transcripts.

Sessions live in ~/.claude/projects/<mangled-cwd>/<uuid>.jsonl and are big (tens of MB) — reading
one into an agent's context is a compaction event, so this script answers the questions you
actually have WITHOUT dumping the file: which session was that, what model ran it, what did it
call, what failed, and what were the last few turns.

Why it exists: locating "the qwen session in .emacs.d" by hand is three greps and a date sort every
single time, and the naive `grep model` gets it wrong (a Claude session that merely *visited*
~/.emacs.d looks identical to a qwen one until you count whose model field dominates).

    ./find-session.py                          # recent sessions, newest last
    ./find-session.py --project emacs          # only projects whose path matches
    ./find-session.py --qwen --since 1d        # only local-model sessions from the last day
    ./find-session.py --show <uuid-prefix>     # tools + failures + last turns of one session
    ./find-session.py --show <uuid> --turns 12 --chars 1200
"""
import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path.home() / ".claude/projects"


def parse_since(s: str) -> float:
    m = re.fullmatch(r"(\d+)([hdm])", s or "")
    if not m:
        return 0.0
    n, unit = int(m.group(1)), m.group(2)
    return time.time() - n * {"m": 60, "h": 3600, "d": 86400}[unit]


def scan(path: Path) -> dict:
    """One pass over a transcript: models, tools, failures, cwds — never keeping message bodies."""
    models, tools, cwds = Counter(), Counter(), Counter()
    errors, turns, n = [], 0, 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            n += 1
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if c := r.get("cwd"):
                cwds[c] += 1
            msg = r.get("message") or {}
            if mm := msg.get("model"):
                models[mm] += 1
            content = msg.get("content")
            if msg.get("role") == "user" and isinstance(content, str):
                turns += 1
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    tools[b.get("name", "?")] += 1
                elif b.get("type") == "tool_result" and b.get("is_error"):
                    t = b.get("content")
                    if isinstance(t, list):
                        t = " ".join(str(x.get("text", "")) for x in t if isinstance(x, dict))
                    errors.append(" ".join(str(t).split())[:200])
    real = Counter({k: v for k, v in models.items() if k != "<synthetic>"})
    return {"path": path, "lines": n, "models": real, "tools": tools, "errors": errors,
            "cwds": cwds, "turns": turns, "mtime": path.stat().st_mtime,
            "size": path.stat().st_size}


def is_local(models: Counter) -> bool:
    top = models.most_common(1)[0][0] if models else ""
    return not top.startswith("claude")


def listing(args):
    rows = []
    for f in ROOT.glob("*/*.jsonl"):
        if args.project and args.project.lower() not in f.parent.name.lower():
            continue
        if f.stat().st_mtime < parse_since(args.since):
            continue
        rows.append(f)
    rows.sort(key=lambda p: p.stat().st_mtime)
    rows = rows[-args.limit:]
    print(f"{'when':16} {'size':>6} {'id':10} {'model':22} {'turns':>5}  project / cwd")
    for f in rows:
        s = scan(f)
        if args.qwen and not is_local(s["models"]):
            continue
        if args.claude and is_local(s["models"]):
            continue
        model = s["models"].most_common(1)[0][0] if s["models"] else "-"
        cwd = s["cwds"].most_common(1)[0][0] if s["cwds"] else f.parent.name
        when = time.strftime("%m-%d %H:%M", time.localtime(s["mtime"]))
        print(f"{when:16} {s['size'] / 1e6:5.1f}M {f.stem[:8]:10} {model[:22]:22} "
              f"{s['turns']:>5}  {cwd}")


def show(args):
    matches = [f for f in ROOT.glob("*/*.jsonl") if f.stem.startswith(args.show)]
    if not matches:
        print(f"no session id starting with {args.show!r}")
        return 1
    f = matches[0]
    s = scan(f)
    print(f"session {f.stem}\n  file    {f}")
    print(f"  when    {time.strftime('%Y-%m-%d %H:%M', time.localtime(s['mtime']))}  "
          f"{s['size'] / 1e6:.1f} MB, {s['lines']} records, {s['turns']} user turns")
    print(f"  models  {dict(s['models'])}")
    print(f"  cwds    {[c for c, _ in s['cwds'].most_common(3)]}")
    print(f"  tools   {dict(s['tools'].most_common(12))}")
    if s["errors"]:
        print(f"  {len(s['errors'])} failed tool results:")
        for e, c in Counter(e[:90] for e in s["errors"]).most_common(8):
            print(f"    [{c}x] {e}")
    print(f"\n--- last {args.turns} exchanges (truncated to {args.chars} chars) ---")
    keep = []
    with f.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = r.get("message") or {}
            role, content = msg.get("role"), msg.get("content")
            if role not in ("user", "assistant"):
                continue
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(b.get("text", "") for b in content
                                if isinstance(b, dict) and b.get("type") == "text")
            else:
                text = ""
            text = text.strip()
            if not text or text.startswith("<"):
                continue
            keep.append((role, text))
    for role, text in keep[-args.turns:]:
        body = text if len(text) <= args.chars else text[:args.chars] + " …[cut]"
        print(f"\n[{role}] {body}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="substring of the project dir (e.g. 'emacs', 'oracle')")
    ap.add_argument("--since", default="7d", help="age window: 30m / 6h / 2d (default 7d)")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--qwen", action="store_true", help="only local-model sessions")
    ap.add_argument("--claude", action="store_true", help="only Claude sessions")
    ap.add_argument("--show", metavar="ID", help="summarize one session (id prefix)")
    ap.add_argument("--turns", type=int, default=8, help="--show: how many exchanges to print")
    ap.add_argument("--chars", type=int, default=800, help="--show: truncate each to N chars")
    args = ap.parse_args()
    return show(args) if args.show else (listing(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
