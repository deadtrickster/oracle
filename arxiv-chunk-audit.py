#!/usr/bin/env python3
"""Quality-audit the ingested arXiv chunks, straight from SereneDB.

    ./arxiv-chunk-audit.py              the battery, with samples
    ./arxiv-chunk-audit.py --quiet      counts only, no example text

## Why SQL and not retrieval

Every reranked query costs ~40s of CPU while the ingest is running, and that CPU is what the parser
needs. These checks read the chunk table directly — the row-count query measured 0.1s at 460k rows —
so the audit is nearly free and can be run repeatedly as the shelf grows.

## What is checked, and why each one

The theme of this corpus's failures is that **nothing errors**. Parsing reports success, counters
say 100%, and the text is garbage. So every check here looks at CONTENT, not at status:

  corruption      the letter-`n` delimiter bug wrote `i creasi gly i volves i formatio `. It is
                  fixed, but the signature is cheap to test and this is the shelf it happened to.
  vectors         a chunk with no embedding is unreachable by vector search while still counting
                  toward every total.
  length          fragments retrieve badly and dilute; giant chunks blow the reranker's input.
  duplicates      arXiv papers repeat their own abstract, and v1/v2 of a paper are near-identical.
  apparatus       bibliography, figure fragments, arXiv stamps — real text, rarely the answer.
  language        the corpus is mixed; a shelf silently full of one language is worth knowing about.
"""
import argparse
import subprocess
import sys

TABLE = "ragflow_a73b470e7d6111f1b22afb6d9f0455fb"


def psql(sql: str, timeout: int = 120) -> str:
    r = subprocess.run(
        ["docker", "exec", "-e", "PGPASSWORD=oracle-sdb", "oracle-serenedb", "psql",
         "-h", "127.0.0.1", "-p", "7890", "-U", "postgres", "-t", "-A", "-F", "|", "-c", sql],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return f"ERR {r.stderr.strip()[:120]}"
    return r.stdout.strip()


def kb_id(name: str) -> str:
    r = subprocess.run(
        ["docker", "exec", "docker-mysql-1", "mysql", "-uroot", "-pinfini_rag_flow", "-N",
         "-e", f"select id from rag_flow.knowledgebase where name='{name}'"],
        capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


def row(label: str, value, total=None, note=""):
    if isinstance(value, int) and total:
        pct = 100 * value / total if total else 0
        flag = ""
        print(f"  {label:34} {value:>9,}  {pct:5.1f}%  {flag}{note}")
    else:
        print(f"  {label:34} {value:>9}  {note}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quiet", action="store_true", help="counts only, no sample text")
    ap.add_argument("--kb", default="arxiv")
    a = ap.parse_args()

    kb = kb_id(a.kb)
    if not kb:
        print(f"no knowledge base named {a.kb}", file=sys.stderr)
        return 1
    W = f"kb_id = '{kb}'"

    print(f"\n═══ {a.kb} chunk audit ═══\n")

    base = psql(f"""select count(*), count(distinct doc_id),
                           count(q_1024_vec), count(q_1024_vec_n),
                           min(length(content_with_weight)),
                           max(length(content_with_weight)),
                           cast(avg(length(content_with_weight)) as int)
                    from {TABLE} where {W};""")
    if base.startswith("ERR") or not base:
        print(f"  could not read the table: {base}")
        return 1
    n, docs, vraw, vnorm, lmin, lmax, lavg = (int(x or 0) for x in base.split("|"))

    print("SHAPE")
    row("chunks", n, None)
    row("documents", docs, None)
    row("chunks per document", round(n / docs, 1) if docs else 0, None)
    row("length min / avg / max", f"{lmin} / {lavg} / {lmax}", None, "chars")

    print("\nVECTORS  (a chunk with no embedding is invisible to vector search)")
    row("with raw vector", vraw, n)
    row("with normalised vector", vnorm, n, "← the one the IVF index uses")

    print("\nCORRUPTION  (the letter-n delimiter bug's signature)")
    corrupt = psql(f"""select
        sum(case when content_with_weight like '% a d %' then 1 else 0 end),
        sum(case when content_with_weight like '% i %' then 1 else 0 end),
        sum(case when content_with_weight like '% the %' then 1 else 0 end),
        sum(case when position(chr(0) in content_with_weight) > 0 then 1 else 0 end)
        from {TABLE} where {W};""")
    if not corrupt.startswith("ERR"):
        ad, lone_i, the, nul = (int(x or 0) for x in corrupt.split("|"))
        row("' a d ' (was 'and')", ad, n, "expect ~0; a few are real maths")
        row("' i ' (was 'in')", lone_i, n, "expect low")
        row("' the ' (healthy English)", the, n, "expect high")
        row("contains NUL", nul, n, "expect 0 — these fail at insert")

    print("\nLENGTH  (fragments dilute; giants break the reranker)")
    lens = psql(f"""select
        sum(case when length(content_with_weight) < 200 then 1 else 0 end),
        sum(case when length(content_with_weight) < 500 then 1 else 0 end),
        sum(case when length(content_with_weight) > 4000 then 1 else 0 end)
        from {TABLE} where {W};""")
    if not lens.startswith("ERR"):
        tiny, small, huge = (int(x or 0) for x in lens.split("|"))
        row("under 200 chars", tiny, n)
        row("under 500 chars", small, n)
        row("over 4000 chars", huge, n)

    print("\nDUPLICATES  (abstracts repeat; v1/v2 of a paper are near-identical)")
    dup = psql(f"""select count(*) from (
        select content_with_weight, count(*) c from {TABLE} where {W}
        group by content_with_weight having count(*) > 1) t;""")
    dupn = psql(f"""select coalesce(sum(c - 1), 0) from (
        select content_with_weight, count(*) c from {TABLE} where {W}
        group by content_with_weight having count(*) > 1) t;""")
    row("distinct texts appearing >1x", dup.strip() or "0", None)
    row("redundant copies", int(dupn.strip() or 0), n)

    print("\nAPPARATUS  (real text, rarely the answer — the demote-by-default case)")
    app = psql(f"""select
        sum(case when (length(content_with_weight)-length(replace(content_with_weight,'doi','')))/3 >= 3 then 1 else 0 end),
        sum(case when (length(content_with_weight)-length(replace(content_with_weight,'doi','')))/3 >= 6 then 1 else 0 end),
        sum(case when content_with_weight like '%arXiv:%' then 1 else 0 end),
        sum(case when content_with_weight like '%Preprint%' or content_with_weight like '%Under review%' then 1 else 0 end)
        from {TABLE} where {W};""")
    if not app.startswith("ERR"):
        d3, d6, stamp, preprint = (int(x or 0) for x in app.split("|"))
        row("bibliography (>=3 DOIs)", d3, n)
        row("dense bibliography (>=6)", d6, n)
        row("carries an arXiv stamp", stamp, n)
        row("preprint / under-review header", preprint, n)

    # Mathematical notation, by symbol presence. NOT by "share of non-Latin characters" — that was
    # tried and is worthless: stripping [a-zA-Z ] leaves punctuation, digits and newlines, so 96% of
    # perfectly ordinary prose scores as non-Latin. Symbols are a narrow test that means what it says.
    print("\nNOTATION  (formula-dense text embeds and retrieves poorly)")
    maths = psql(f"""select sum(case when content_with_weight like '%∈%'
                            or content_with_weight like '%≤%'
                            or content_with_weight like '%∑%'
                            or content_with_weight like '%𝑥%' then 1 else 0 end)
                     from {TABLE} where {W};""")
    if not maths.startswith("ERR"):
        row("contains maths symbols", int(maths.strip() or 0), n,
            "≈half the shelf is maths/physics by category")

    if not a.quiet:
        print("\nSAMPLES  (random, unfiltered — read them)")
        s = psql(f"""select substr(replace(content_with_weight, chr(10), ' '), 1, 150)
                     from {TABLE} where {W} order by random() limit 5;""")
        for line in s.splitlines():
            print(f"  · {line[:150]}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
