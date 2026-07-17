#!/usr/bin/env python3
"""Drive an EVAL.md suite against the LOCAL qwen agent, headless, as ONE multi-turn
conversation, and report tool-calls-per-turn — the grounding-decay metric EVAL.md cares about.

CRITICAL (see memory `eval-drive-through-qwen-wrapper`): this drives through ~/bin/qwen
(-> qwen.sh), NEVER bare `claude`. qwen.sh injects the PRODUCTION config that is the thing
under test — the DISCIPLINE system prompt (--append-system-prompt), --mcp-config +
--strict-mcp-config, --exclude-dynamic-system-prompt-sections, and the tool trim. Our
-p/--session-id/--resume flags just ride through qwen.sh's "$@". Injection is VERIFIED from
the saved transcript before any result is trusted.

  ./eval-agent.py A            # run suite A, print the report
"""
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

QWEN = Path.home() / "bin" / "qwen"
CWD = Path.home() / "Projects" / "oracle"
TRANSCRIPTS = Path.home() / ".claude-local" / "projects" / "-home-dead-Projects-oracle"
# Claude Code does NOT store --append-system-prompt in the transcript, so we can't grep the JSONL to
# prove injection. The reliable signal is qwen.sh's own banner, printed at the moment it builds the
# exec line with `--append-system-prompt "$DISCIPLINE"` and the tool trim. Its presence == injected.
INJECTION_BANNER = "schema-discipline prompt appended"

# Questions verbatim from EVAL.md. Asked IN ORDER, one session — the interconnection is the test.
SUITES = {
    "A": [
        ("A1", "Tell me about LSN in Postgres"),
        ("A2", "Can I get lsn with `SELECT pg_last_wal_replay_lsn();`?"),
        ("A3", "Tell me about postgres wal file format"),
        ("A4", "What new WAL records does orioledb have?"),
    ],
    # Suite B — serenedb (mirrors A, one ramp harder). Prereq: serenedb + duckdb indexed.
    # Difficulty ramp B1 < B3 < B4 < B2; B2 is the killer ("yes just store it sorted" == FAIL).
    "B": [
        ("B1", "Tell me about how serenedb represents JSON data internally."),
        ("B2", "Can I get Postgres-ordered jsonb keys just by building/storing the VARIANT "
                "with keys in that order?"),
        ("B3", "Tell me how serenedb maps DuckDB types to Postgres type OIDs for the JSON family."),
        ("B4", "What serialization \"core\" types does the PG layer use to encode/decode JSON on "
                "the wire, and what's missing for jsonb?"),
    ],
}


def run_turn(qtext: str, sid: str, first: bool, timeout: int = 1200):
    args = [str(QWEN), "-p", qtext, "--output-format", "json"]
    args += ["--session-id", sid] if first else ["--resume", sid]
    t0 = time.time()
    try:
        r = subprocess.run(args, cwd=str(CWD), capture_output=True, text=True, timeout=timeout)
        # qwen.sh prints the injection banner to stdout before exec; capture it as proof-of-injection.
        injected = INJECTION_BANNER in (r.stdout or "")
        return r.returncode, round(time.time() - t0, 1), injected, (r.stderr or "")[-400:]
    except subprocess.TimeoutExpired:
        return -1, timeout, False, "TIMEOUT"


def analyze(sid: str):
    f = TRANSCRIPTS / f"{sid}.jsonl"
    if not f.exists():
        return None, []
    raw = f.read_text(encoding="utf-8", errors="replace")
    entries = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
    turns, cur = [], None
    for e in entries:
        msg = e.get("message", e)
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            # a tool_result also arrives as role=user; only a plain user message opens a new turn
            is_tr = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
            if not is_tr:
                if cur:
                    turns.append(cur)
                txt = content if isinstance(content, str) else " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
                cur = {"q": txt.strip()[:80], "tools": [], "answer": ""}
        elif role == "assistant" and cur is not None:
            for b in (content or []):
                if isinstance(b, dict):
                    if b.get("type") == "tool_use":
                        cur["tools"].append(b.get("name", "?"))
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        cur["answer"] = b["text"].strip()
    if cur:
        turns.append(cur)
    return f, turns


def main() -> int:
    suite = (sys.argv[1] if len(sys.argv) > 1 else "A").upper()
    if suite not in SUITES:
        print(f"no such suite: {suite} (have {list(SUITES)})", file=sys.stderr)
        return 1
    sid = str(uuid.uuid4())
    print(f"== Suite {suite}  session={sid}  (driving through {QWEN})")
    injected_all = []
    for i, (tag, q) in enumerate(SUITES[suite]):
        print(f"  -> {tag}: {q!r} ...", flush=True)
        rc, secs, injected, err = run_turn(q, sid, first=(i == 0))
        injected_all.append(injected)
        print(f"     rc={rc} in {secs}s  injected={injected}"
              + (f"  stderr: {err.strip()}" if rc != 0 else ""), flush=True)
        if rc != 0:
            print("     ABORTING suite — a turn failed; the conversation can't continue.", flush=True)
            break

    f, turns = analyze(sid)
    injected = all(injected_all) and len(injected_all) == len(SUITES[suite])
    print("\n" + "=" * 72)
    print(f"TRANSCRIPT: {f}")
    print(f"DISCIPLINE PROMPT INJECTED (qwen.sh banner, every turn): "
          f"{'YES ✅' if injected else 'NO ❌  (INVALID RUN)'}  {injected_all}")
    print("=" * 72)
    print(f"{'turn':6}{'tool calls':>11}   tools used")
    for (tag, _), t in zip(SUITES[suite], turns):
        print(f"{tag:6}{len(t['tools']):>11}   {', '.join(t['tools']) or '— none (parametric!)'}")
    print("\n---- answers (first-pass, for human grading vs EVAL.md) ----")
    for (tag, _), t in zip(SUITES[suite], turns):
        print(f"\n### {tag}\n{t['answer'][:1400]}")
    # machine-readable dump for the before/after comparison later
    out = TRANSCRIPTS.parent / f"eval-{suite}-{sid[:8]}.json"
    out.write_text(json.dumps({"suite": suite, "sid": sid, "injected": injected, "turns": turns},
                              ensure_ascii=False, indent=2))
    print(f"\nsaved summary: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
