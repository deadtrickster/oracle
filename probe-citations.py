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
MIN_QUOTE = 15          # chars a citation quote must have to be evidence of anything

PROMPT = f"""Review the file {ABS} and propose exactly {N} concrete improvements.

Output ONLY {N} lines, each EXACTLY in this format and nothing else:

CITE <start>-<end> | <QUOTE> | <suggestion>

  <start>-<end>  a line range in that file (1-indexed) that your suggestion is about
  <QUOTE>        at least 15 characters COPIED VERBATIM, character for character, from ONE line
                 inside <start>-<end>. Not a paraphrase, not a symbol name you remember — an exact
                 substring of the real text. It is checked automatically against the file.
                 (Do not include the '|' character in the quote.)
  <suggestion>   one sentence

Spread the {N} suggestions across the WHOLE file, not just the beginning. No preamble, no summary,
no markdown, no code fences — only the {N} CITE lines."""

# Base trim from qwen.sh, plus the per-arm forcing. Arm A must not reach the source tools; Arm B
# must not reach Read (nor ask_code, which would do the reading for it).
BASE = ("mcp__codebase-memory__index_repository mcp__codebase-memory__index_status "
        "mcp__codebase-memory__detect_changes mcp__codebase-memory__ingest_traces "
        "mcp__codebase-memory__manage_adr mcp__codebase-memory__delete_project "
        "mcp__codebase-memory__get_graph_schema WebSearch WebFetch Agent")
# Bash is disallowed in BOTH arms — the 2026-07-22 run showed Arm B ignoring read_lines and
# walking the file with `sed -n 'A,Bp'` instead (17 Bash calls, one read_lines), which tested
# windowed-vs-bulk but left the actual tooling question unanswered. Denying Bash forces each arm
# onto exactly one content path.
ARMS = {
    "A": BASE + " Bash mcp__source-grep__read_lines mcp__source-grep__source_search "
                "mcp__oracle-ask__ask_code mcp__codebase-memory__get_code_snippet",
    "B": BASE + " Bash Read mcp__oracle-ask__ask_code",
    # Arm C — the tool we may already own for exactly this. ask_code greps, reads, and synthesizes
    # in ONE call, returning file:line citations plus a RAW SOURCE block, so the address never has
    # to be remembered. If C matches B's accuracy at A's cost, the answer is "route citation tasks
    # to ask_code" and no Read-limiting hook is needed at all. Note the confound to report: its
    # synthesis runs on the SAME qwen-next, so C is that model reading its own grep output.
    "C": BASE + " Bash Read mcp__source-grep__read_lines mcp__source-grep__source_search "
                "mcp__codebase-memory__get_code_snippet mcp__codebase-memory__search_code",
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
    what = {"A": "everything but Read", "B": "Read and Bash (source tools only)",
            "C": "everything but ask_code"}[arm]
    print(f"-- arm {arm}: disallowing {what}")
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
            # Whitespace is normalized on both sides: the model retyping a quote with different
            # indentation is not a citation error, and we are scoring LOCATION accuracy.
            norm = lambda t: " ".join(t.split())  # noqa: E731
            # A too-short quote is not evidence of anything — "#" or ")" matches almost any
            # window, so it would score as a hit while proving nothing about the location.
            # The prompt demands >= MIN_QUOTE chars; enforce it here or the metric is a sieve.
            valid = len(norm(sym)) >= MIN_QUOTE
            rows.append((s, e, sym, valid and norm(sym) in norm(window), valid))
        if not rows:
            print(f"{p}: no parseable CITE lines\n")
            continue
        ok = sum(1 for r in rows if r[3])
        short = sum(1 for r in rows if not r[4])
        print(f"{p}: {ok}/{len(rows)} citations verified"
              + (f"   ({short} rejected: quote < {MIN_QUOTE} chars)" if short else ""))
        for s, e, sym, good, valid in rows:
            pos = 100 * s / n_file
            tag = "OK  " if good else ("SHRT" if not valid else "MISS")
            print(f"   {tag} {s:>5}-{e:<5} ({pos:4.0f}% into file)  {sym[:44]}")
        # positional split — the hypothesis is that accuracy decays with depth
        early = [r[3] for r in rows if r[0] <= n_file / 2]
        late = [r[3] for r in rows if r[0] > n_file / 2]
        f = lambda b: f"{sum(b)}/{len(b)}" if b else "n/a"  # noqa: E731
        print(f"   first half: {f(early)}   second half: {f(late)}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", choices=["A", "B", "C"])
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
