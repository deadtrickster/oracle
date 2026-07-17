#!/usr/bin/env python3
"""Drive the EVAL.md suites (A/B/C/D) against the LOCAL qwen agent, headless, each as ONE multi-turn
conversation; AUTO-GRADE against a rubric; VERIFY expected facts against real source; and, in
tournament mode, A/B-test DISCIPLINE variants and rank them.

Self-contained so it runs UNATTENDED — one command does run + grade + source-check + report:

  ./eval-agent.py A                          # run+grade one suite
  ./eval-agent.py --all --label baseline     # run+grade A B C D
  ./eval-agent.py --all --extra discipline/v1.txt --label v1   # with an appended DISCIPLINE variant
  ./eval-agent.py --tournament               # baseline + every variant in discipline/, across A-D, ranked
  ./eval-agent.py --grade-json <summary.json>   # re-grade an existing run, no inference

CRITICAL (memory `eval-drive-through-qwen-wrapper`): drives through ~/bin/qwen (-> qwen.sh), NEVER
bare `claude`, so the PRODUCTION config is injected — DISCIPLINE prompt, --mcp-config +
--strict-mcp-config, --exclude-dynamic-system-prompt-sections, tool trim. A discipline VARIANT is an
`--extra` file appended via qwen.sh's ORACLE_DISCIPLINE_EXTRA hook — the production prompt still rides
underneath. Injection is verified per turn from qwen.sh's banner.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

QWEN = Path.home() / "bin" / "qwen"
CWD = Path.home() / "Projects" / "oracle"
TRANSCRIPTS = Path.home() / ".claude-local" / "projects" / "-home-dead-Projects-oracle"
REPORTS = CWD / "eval-reports"
DISCIPLINE_DIR = CWD / "discipline"
INJECTION_BANNER = "schema-discipline prompt appended"
REPOS = {"orioledb": Path.home() / "Projects" / "orioledb",
         "serenedb": Path.home() / "Projects" / "serenedb"}

# Questions verbatim from EVAL.md, asked IN ORDER in one session (the interconnection is the test).
SUITES = {
    "A": [("A1", "Tell me about LSN in Postgres"),
          ("A2", "Can I get lsn with `SELECT pg_last_wal_replay_lsn();`?"),
          ("A3", "Tell me about postgres wal file format"),
          ("A4", "What new WAL records does orioledb have?")],
    "B": [("B1", "Tell me about how serenedb represents JSON data internally."),
          ("B2", "Can I get Postgres-ordered jsonb keys just by building/storing the VARIANT with "
                 "keys in that order?"),
          ("B3", "Tell me how serenedb maps DuckDB types to Postgres type OIDs for the JSON family."),
          ("B4", "What serialization \"core\" types does the PG layer use to encode/decode JSON on "
                 "the wire, and what's missing for jsonb?")],
    "C": [("C1", "tell me what auto_ptr does"),
          ("C2", "tell me more about ownership management"),
          ("C3", "tell me more about pointer/reference/ownership tools in the recent c++ versions "
                 "such as c++21"),
          ("C4", "add weak_ptr to the table"),
          ("C5", "nothing in c++17?")],
    "D": [("D1", "расскажи, зачем обезьянам хвосты"),
          ("D2", "какие виды мышей ты знаешь"),
          ("D3", "reply the same in english")],
}

# The EVAL.md answer key, encoded. must: ALL regexes required. trap: ANY == the cardinal-sin answer.
# grounded: this turn MUST make >=1 tool call (grounding-decay probe). read_source: MUST use
# source_search/read_lines (a specific-codebase question). enumerate: model set vs real source set.
# facts: (repo, file, pattern, label) ground-truth so the report can say "findable, yet missed".
RUBRICS = {
    "A1": {"must": [r"LSN", r"XLogRecPtr|64[- ]?bit"]},
    "A2": {"must": [r"standby|follower|replica|recovery", r"pg_current_wal_lsn|primary|master"]},
    "A3": {"must": [r"WAL", r"record"]},
    "A4": {"must": [r"WAL_REC_"], "grounded": True, "read_source": True,
           "enumerate": {"pattern": r"WAL_REC_[A-Z_]+", "repo": "orioledb", "file": "wal_record.h"}},
    "B1": {"must": [r"VARIANT", r"VARCHAR", r"DuckDB"],
           "trap": [r"BSON|MongoDB|document[- ]oriented|document store"], "grounded": True,
           "read_source": True,
           "facts": [("serenedb", "pg_types.cpp", r"IsJSONType", "json == VARCHAR/JSON alias")]},
    "B2": {"must": [r"VARIANT", r"shred", r"order|reassembl"],
           "trap": [r"just store it sorted", r"\byes\b(?![^.]*\b(no|but|not|cannot)\b)"],
           "grounded": True, "read_source": True,
           "facts": [("serenedb", "variant_builder.hpp", r"EmitObject", "build respects emit order"),
                     ("serenedb", "variant_utils.hpp", r"UnshredVariantData", "reassembly rebuilds")]},
    "B3": {"must": [r"kJson\b", r"\b114\b", r"kJsonb", r"\b3802\b"],
           "trap": [r"cannot find|could not find|unable to find"], "grounded": True,
           "read_source": True,
           "facts": [("serenedb", "pg_types.h", r"kJson\s*=\s*114", "kJson = 114 (findable)"),
                     ("serenedb", "pg_types.h", r"kJsonb\s*=\s*3802", "kJsonb = 3802 (findable)")]},
    "B4": {"must": [r"JsonTextCore", r"JsonBinCore", r"version byte|0x01"],
           "trap": [r"jsonb\s+(decoder|core)[^.]*\b(exists|already)"], "grounded": True,
           "read_source": True,
           "facts": [("serenedb", "serialize.cpp", r"JsonBinCore", "JsonBinCore (findable)")]},
    "C1": {"must": [r"auto_ptr", r"deprecat|remov|C\+\+1[17]|ownership"]},
    "C2": {"must": [r"unique_ptr|shared_ptr|RAII|ownership"], "grounded": True},
    "C3": {"must": [r"C\+\+20|C\+\+23"], "trap": [r"in c\+\+21|c\+\+21 (introduc|add|brought|bring)"],
           "grounded": True},
    "C4": {"must": [r"weak_ptr"],
           "trap": [r"shared_ptr[^.]{0,70}(❌|\bno\b|cannot|can't)[^.]{0,30}(container|vector)"],
           "grounded": True},
    "C5": {"must": [r"C\+\+17"],
           "trap": [r"c\+\+17[^.]{0,40}(nothing|no new|added nothing|has nothing|doesn)"],
           "grounded": True},
    "D1": {"must": [r"баланс|равновес|хват|prehensile|balance|grasp|сигнал|climb|лаз"],
           "trap": [r"не связан с программирован|coding assistant|programming (model|assistant)|"
                    r"не могу предостав|out.?of.?scope|только.{0,15}программирован"], "grounded": True},
    "D2": {"must": [r"мыш"],
           "trap": [r"(Muridae|мышины[хе])[^.]{0,300}(белк|бобр|сурок|дикобраз|суслик)"],
           "grounded": True},
    "D3": {"must": [r"marmot|muskrat"], "trap": [r"weasel|otter|gopher"]},
}


def run_turn(qtext, sid, first, extra=None, timeout=1200):
    args = [str(QWEN), "-p", qtext, "--output-format", "json"]
    args += ["--session-id", sid] if first else ["--resume", sid]
    env = dict(os.environ)
    if extra:
        env["ORACLE_DISCIPLINE_EXTRA"] = str(extra)
    t0 = time.time()
    try:
        r = subprocess.run(args, cwd=str(CWD), capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, round(time.time() - t0, 1), (INJECTION_BANNER in (r.stdout or "")), \
            (r.stderr or "")[-300:]
    except subprocess.TimeoutExpired:
        return -1, timeout, False, "TIMEOUT"


def analyze(sid):
    f = TRANSCRIPTS / f"{sid}.jsonl"
    if not f.exists():
        return []
    entries = [json.loads(ln) for ln in f.read_text(encoding="utf-8", errors="replace").splitlines()
               if ln.strip()]
    turns, cur = [], None
    for e in entries:
        msg = e.get("message", e)
        role, content = msg.get("role"), msg.get("content")
        if role == "user":
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
    return turns


def _source_set(repo, filename, pattern):
    out = set()
    for p in REPOS.get(repo, Path("/nonexistent")).rglob(filename):
        try:
            out |= set(re.findall(pattern, p.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            pass
    return out


def _source_has(repo, filename, pattern):
    for p in REPOS.get(repo, Path("/nonexistent")).rglob(filename):
        try:
            if re.search(pattern, p.read_text(encoding="utf-8", errors="replace")):
                return True
        except OSError:
            pass
    return False


def grade(tag, turn):
    ans, tools = turn.get("answer", "") or "", turn.get("tools", [])
    read = any("source_search" in t or "read_lines" in t for t in tools)
    rub = RUBRICS.get(tag, {})
    verdict, reasons = "PASS", []

    if rub.get("grounded") and not tools:
        verdict = "FAIL"
        reasons.append("GROUNDING DECAY (0 tool calls this turn)")
    missing = [m for m in rub.get("must", []) if not re.search(m, ans, re.I)]
    if missing:
        verdict = "FAIL"
        reasons.append("missing: " + ", ".join(missing))
    hit = [t for t in rub.get("trap", []) if re.search(t, ans, re.I)]
    if hit:
        verdict = "FAIL"
        reasons.append("TRAP")
    if rub.get("read_source") and not read:
        verdict = "FAIL"
        reasons.append("did NOT read source (synthesis only)")

    enum = rub.get("enumerate")
    if enum:
        model_set = set(re.findall(enum["pattern"], ans))
        real = _source_set(enum["repo"], enum["file"], enum["pattern"])
        fab = model_set - real
        if fab:
            verdict = "FAIL"
            reasons.append(f"FABRICATED: {sorted(fab)}")
        if real:
            frac = len(model_set & real) / len(real)
            reasons.append(f"{len(model_set & real)}/{len(real)} real ({frac:.0%})")
            if frac < 0.8 and verdict == "PASS":
                verdict = "PARTIAL"

    for repo, fname, pat, label in rub.get("facts", []):
        if _source_has(repo, fname, pat) and not re.search(pat, ans, re.I):
            reasons.append(f"findable-but-missed: {label}")

    if verdict == "PASS" and not reasons:
        reasons.append("ok" + (", read source" if read else ""))
    return verdict, reasons


def report(label, suite, sid, injected, turns):
    tags = [t for t, _ in SUITES[suite]]
    graded = [(tag, *grade(tag, t), len(t["tools"])) for tag, t in zip(tags, turns)]
    passes = sum(1 for _, v, _, _ in graded if v == "PASS")
    decay = any(len(t["tools"]) == 0 for t in turns)
    lines = [f"# Suite {suite} — {label}", "",
             f"- session `{sid}` · injected: **{'YES' if injected else 'NO ❌ INVALID'}**",
             f"- **{passes}/{len(tags)} PASS** · grounding-decay: {'YES ❌' if decay else 'none ✅'}"
             f" · tools/turn {[len(t['tools']) for t in turns]}", "",
             "| turn | verdict | tools | notes |", "|---|---|---|---|"]
    for tag, v, reasons, ntools in graded:
        lines.append(f"| {tag} | **{v}** | {ntools} | {'; '.join(reasons)} |")
    lines.append("\n## Answers")
    for tag, t in zip(tags, turns):
        lines += [f"\n### {tag}", "```", (t["answer"] or "(empty)")[:1100], "```"]
    return "\n".join(lines), passes, decay


def run_suite(suite, label, extra=None):
    REPORTS.mkdir(exist_ok=True)
    sid = str(uuid.uuid4())
    tag = f"{label}/{suite}"
    print(f"== {tag}  session={sid}  extra={extra}", flush=True)
    inj = []
    for i, (t, q) in enumerate(SUITES[suite]):
        print(f"   -> {t} ...", flush=True)
        rc, secs, injected, err = run_turn(q, sid, first=(i == 0), extra=extra)
        inj.append(injected)
        print(f"      rc={rc} {secs}s injected={injected}" + (f" ERR:{err.strip()}" if rc else ""),
              flush=True)
        if rc != 0:
            print("      ABORT suite (turn failed)", flush=True)
            break
    turns = analyze(sid)
    injected = bool(inj) and all(inj) and len(inj) == len(SUITES[suite])
    rep, passes, decay = report(label, suite, sid, injected, turns)
    print("\n" + rep, flush=True)
    base = REPORTS / f"{label}-{suite}-{sid[:8]}"
    base.with_suffix(".md").write_text(rep)
    base.with_suffix(".json").write_text(json.dumps(
        {"suite": suite, "label": label, "sid": sid, "injected": injected, "extra": str(extra),
         "passes": passes, "total": len(SUITES[suite]), "decay": decay, "turns": turns},
        ensure_ascii=False, indent=2))
    return {"suite": suite, "label": label, "passes": passes, "total": len(SUITES[suite]),
            "decay": decay, "injected": injected}


def tournament(suites):
    """Baseline + every discipline/*.txt variant, across the given suites. Ranked. This is the ONE
    unattended entry point: approve `python3 eval-agent.py --tournament` and it runs the whole thing."""
    variants = [("baseline", None)]
    if DISCIPLINE_DIR.is_dir():
        variants += [(p.stem, p) for p in sorted(DISCIPLINE_DIR.glob("*.txt"))]
    print(f"### TOURNAMENT: {len(variants)} variants x {len(suites)} suites "
          f"({[n for n, _ in variants]})\n", flush=True)
    grid = {}
    for name, extra in variants:
        for s in suites:
            try:
                r = run_suite(s, name, extra=extra)
            except Exception as e:  # one failure must not kill the tournament
                print(f"!! {name}/{s} crashed: {e}", flush=True)
                r = {"passes": -1, "total": len(SUITES[s]), "decay": True, "injected": False}
            grid[(name, s)] = r
    # ranking
    print("\n" + "=" * 60 + "\n### RANKING\n" + "=" * 60, flush=True)
    header = "variant".ljust(16) + "".join(s.center(7) for s in suites) + "  total"
    print(header, flush=True)
    ranked = []
    for name, _ in variants:
        cells, tot, ttot = "", 0, 0
        for s in suites:
            r = grid[(name, s)]
            cells += f"{r['passes']}/{r['total']}".center(7)
            tot += max(0, r["passes"])
            ttot += r["total"]
        ranked.append((tot, ttot, name, cells))
    for tot, ttot, name, cells in sorted(ranked, reverse=True):
        print(name.ljust(16) + cells + f"  {tot}/{ttot}", flush=True)
    (REPORTS / "tournament-ranking.txt").write_text(
        header + "\n" + "\n".join(name.ljust(16) + cells + f"  {tot}/{ttot}"
                                  for tot, ttot, name, cells in sorted(ranked, reverse=True)))
    print(f"\nsaved ranking + per-run reports under {REPORTS}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("suite", nargs="?", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--tournament", action="store_true")
    ap.add_argument("--extra", type=Path, help="a discipline variant file to append")
    ap.add_argument("--label", default="run")
    ap.add_argument("--suites", default="ABCD", help="which suites for --tournament (default ABCD)")
    ap.add_argument("--grade-json", type=Path)
    args = ap.parse_args()

    if args.grade_json:
        d = json.loads(args.grade_json.read_text())
        print(report(d.get("label", "regrade"), d["suite"], d["sid"], d["injected"], d["turns"])[0])
        return 0
    if args.tournament:
        tournament([c for c in args.suites.upper() if c in SUITES])
        return 0
    suites = list(SUITES) if args.all else [args.suite.upper()] if args.suite else []
    if not suites:
        ap.error("give a suite (A/B/C/D), --all, or --tournament")
    for s in suites:
        if s not in SUITES:
            print(f"no such suite: {s}", file=sys.stderr)
            return 1
        run_suite(s, args.label, extra=args.extra)
    return 0


if __name__ == "__main__":
    sys.exit(main())
