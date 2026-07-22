#!/usr/bin/env python3
"""Positional citation-accuracy probe (EVAL.md, failure class 3 — misattribution).

QUESTION: when a model cites `file:line`, does accuracy depend on HOW the content reached it?

  Arm A  bulk `Read` — one call returns up to 2,000 numbered lines; every later citation is a
         long-range recall over a block ingested thousands of tokens earlier.
  Arm B  `source_search` + `read_lines` — many small windows, each fetched while writing about
         that region. BOTH arms see line numbers (read_lines prints `NNN<TAB>text` too), so the
         variable under test is WINDOW SIZE AND RECENCY, not the presence of an address.

Same model serves both arms (qwen-next via the wrapper), same file, same prompt — so a difference
is a HARNESS result, not a model result (Axiom 2).

GROUND TRUTH IS MECHANICAL — no judge, no rubric. Each suggestion must name a SYMBOL that literally
occurs inside the line range it cites; we verify against the pinned git blob. Line-number truth
rots when a file changes, so the target is pinned by commit and checked before running.

    ./probe-citations.py --arm A          # run one arm
    ./probe-citations.py --arm B
    ./probe-citations.py --score out/A.txt out/B.txt
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path("/home/dead/Projects/oracle/ragflow")
TARGET = "rag/nlp/__init__.py"
PIN = "cb93883"                     # commit that last touched TARGET; tree must be clean
ABS = REPO / TARGET
WRAPPER = "/home/dead/bin/qwen-next"
OUT = Path("/home/dead/Projects/oracle/probe-out")
N = 8

PROMPT = f"""Review the file {ABS} and propose exactly {N} concrete improvements.

Output ONLY {N} lines, each EXACTLY in this format and nothing else:

CITE <start>-<end> | <SYMBOL> | <suggestion>

  <start>-<end>  a line range in that file (1-indexed) that your suggestion is about
  <SYMBOL>       an identifier or literal string that ACTUALLY APPEARS on some line inside
                 <start>-<end> — it will be checked automatically against the file
  <suggestion>   one sentence

Spread the {N} suggestions across the WHOLE file, not just the beginning. No preamble, no summary,
no markdown, no code fences — only the {N} CITE lines."""

# Base trim from qwen.sh, plus the per-arm forcing. Arm A must not reach the source tools; Arm B
# must not reach Read (nor ask_code, which would do the reading for it).
BASE = ("mcp__codebase-memory__index_repository mcp__codebase-memory__index_status "
        "mcp__codebase-memory__detect_changes mcp__codebase-memory__ingest_traces "
        "mcp__codebase-memory__manage_adr mcp__codebase-memory__delete_project "
        "mcp__codebase-memory__get_graph_schema WebSearch WebFetch Agent")
ARMS = {
    "A": BASE + " mcp__source-grep__read_lines mcp__source-grep__source_search "
                "mcp__oracle-ask__ask_code mcp__codebase-memory__get_code_snippet",
    "B": BASE + " Read mcp__oracle-ask__ask_code",
}

CITE_RE = re.compile(r"CITE\s*(\d+)\s*-\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*(.*)")


def pinned_lines() -> list[str]:
    """The file as of PIN — the ONLY ground truth we score against."""
    blob = subprocess.run(["git", "-C", str(REPO), "show", f"{PIN}:{TARGET}"],
                          capture_output=True, text=True, check=True).stdout
    return blob.splitlines()


def check_pin():
    dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", TARGET],
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        sys.exit(f"REFUSING: {TARGET} is modified — the working copy would not match {PIN}")


def run_arm(arm: str, timeout: int):
    check_pin()
    OUT.mkdir(exist_ok=True)
    env = dict(os.environ, CLAUDE_LOCAL_DISALLOW=ARMS[arm])
    print(f"-- arm {arm}: disallowing {'source tools' if arm == 'A' else 'Read'}")
    r = subprocess.run([WRAPPER, "-p", PROMPT], env=env, capture_output=True,
                       text=True, timeout=timeout)
    dest = OUT / f"{arm}.txt"
    dest.write_text(r.stdout + ("\n[STDERR]\n" + r.stderr if r.stderr.strip() else ""),
                    encoding="utf-8")
    print(f"   wrote {dest} ({len(r.stdout)} chars, rc={r.returncode})")


def score(paths):
    lines = pinned_lines()
    n_file = len(lines)
    print(f"target {TARGET} @ {PIN} — {n_file} lines\n")
    for p in paths:
        text = Path(p).read_text(encoding="utf-8")
        rows = []
        for m in CITE_RE.finditer(text):
            s, e, sym = int(m.group(1)), int(m.group(2)), m.group(3).strip().strip("`'\"")
            s, e = max(1, s), min(n_file, e)
            window = "\n".join(lines[s - 1:e]) if s <= e else ""
            rows.append((s, e, sym, sym in window))
        if not rows:
            print(f"{p}: no parseable CITE lines\n")
            continue
        ok = sum(1 for *_, good in rows if good)
        print(f"{p}: {ok}/{len(rows)} citations verified")
        for s, e, sym, good in rows:
            pos = 100 * s / n_file
            print(f"   {'OK  ' if good else 'MISS'} {s:>5}-{e:<5} ({pos:4.0f}% into file)  {sym[:40]}")
        # positional split — the hypothesis is that accuracy decays with depth
        early = [g for s, _, _, g in rows if s <= n_file / 2]
        late = [g for s, _, _, g in rows if s > n_file / 2]
        f = lambda b: f"{sum(b)}/{len(b)}" if b else "n/a"  # noqa: E731
        print(f"   first half: {f(early)}   second half: {f(late)}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", choices=["A", "B"])
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--score", nargs="*", metavar="FILE")
    a = ap.parse_args()
    if a.arm:
        run_arm(a.arm, a.timeout)
    if a.score is not None:
        score(a.score or sorted(str(p) for p in OUT.glob("*.txt")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
