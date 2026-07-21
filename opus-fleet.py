#!/usr/bin/env python3
"""Opus gold-OCR fleet — PLANNER/RENDERER (the spawning is done by Claude via subagents).

Why this shape: headless `claude -p` is NOT covered by the Max plan, so the fleet runs as
session subagents (Agent tool, model=opus). This script owns everything that must survive
crashes, compaction, and usage-limit waits:

  DISK-TRUTH RESUME   a page is done iff its gold .txt AND .diff.json exist non-empty in
                      corpus/ml/opus-gold/<slug>/. `--plan` recomputes the remaining work from
                      disk every time — any orchestrator, any session, any day can continue.
  RENDERING           `--plan` pre-renders the batch pages in-tree (approve-once paths).
  STATUS              `--status` shows per-book progress.

Orchestrator loop (Claude): run `--plan N` -> spawn one opus subagent per emitted batch with the
v3 closed contract (Read/Write only, per-page outputs) -> on completion notifications, repeat.
On usage-limit failures: stop spawning, wait for the window, rerun `--plan` and continue.

    ./opus-fleet.py --status
    ./opus-fleet.py --plan 4      # render + print the next 4 batches as JSON lines
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent
VL_PAGES = REPO / "corpus/ml/vl-pages"
GOLD = REPO / "corpus/ml/opus-gold"
BOOKS_DIR = Path.home() / "Documents/Books/ml"
BATCH = 15

BOOKS = [  # (pdf filename, slug) — must match transcribe-scans.py
    ("Нейрокомпьютеры и их применение. Книга 01. _Галушкин А.И._ Теория нейронных сетей.(2000).pdf",
     "neurocomp-kn01-galushkin-teoriya"),
    ("Нейрокомпьютеры и их применение. Книга 02. _Сигеру Омату, Марзуки Халид, Рубия Юсоф_ Нейроуправление и его приложения.(2000).pdf",
     "neurocomp-kn02-omatu-neuroupravlenie"),
    ("Нейрокомпьютеры и их применение. Книга 03. Галушкин А.И._Нейрокомпьютеры.(2000).pdf",
     "neurocomp-kn03-galushkin-neurocomputery"),
    ("Нейрокомпьютеры и их применение. Книга 04. _Головко В.А._ Нейронные сети - обучение, организация и применение.(2001).pdf",
     "neurocomp-kn04-golovko-obuchenie"),
    ("Нейрокомпьютеры и их применение. Книга 05. Нейронные сети - история развития теории.(2001).pdf",
     "neurocomp-kn05-istoriya-teorii"),
    ("Окулов С.М., Пестов О.А._Динамическое программирование.(2012).pdf",
     "okulov-pestov-dinamicheskoe-programmirovanie"),
]


def n_pages(slug: str) -> int:
    return len(list((VL_PAGES / slug).glob("p-*.txt")))


def done(slug: str, p: int) -> bool:
    # Protocol v4 (2026-07-21): pure-OCR agents write only the gold .txt — the draft-diff record
    # was dropped once the VL error profile was banked (255+ pages). The gold transcript alone is
    # the completion criterion; v3-era pages additionally have p-NNNN.diff.json, which is fine.
    t = GOLD / slug / f"p-{p:04}.txt"
    return t.is_file() and t.stat().st_size > 0


def missing_pages(slug: str) -> list[int]:
    return [p for p in range(1, n_pages(slug) + 1) if not done(slug, p)]


def render(slug: str, pdf: Path, pages: list[int]):
    rd = GOLD / slug / "render"
    rd.mkdir(parents=True, exist_ok=True)
    for p in pages:
        if (rd / f"p-{p:03}.png").exists() or (rd / f"p-{p:04}.png").exists():
            continue
        subprocess.run(["nice", "-n", "19", "pdftoppm", "-png", "-r", "150",
                        "-f", str(p), "-l", str(p), str(pdf), str(rd / "p")],
                       check=True, capture_output=True)


def status():
    total = donec = 0
    for _, slug in BOOKS:
        n = n_pages(slug)
        d = n - len(missing_pages(slug))
        total += n
        donec += d
        flag = " ✓" if d == n and n else ""
        print(f"  {slug:46} {d:4}/{n}{flag}")
    print(f"  TOTAL {donec}/{total}")


def plan(k: int):
    emitted = 0
    for fname, slug in BOOKS:
        if emitted >= k:
            break
        pdf = BOOKS_DIR / fname
        miss = missing_pages(slug)
        for i in range(0, len(miss), BATCH):
            if emitted >= k:
                break
            pages = miss[i:i + BATCH]
            render(slug, pdf, pages)
            rd = GOLD / slug / "render"
            pad = 3 if any(rd.glob(f"p-{pages[0]:03}.png")) else 4
            print(json.dumps({
                "slug": slug,
                "book": "okulov" if slug.startswith("okulov") else "neurocomp",
                "workdir": str(GOLD / slug),
                "vl_dir": str(VL_PAGES / slug),
                "render_pad": pad,
                "pages": pages,
            }, ensure_ascii=False))
            emitted += 1
    if emitted == 0:
        print("FLEET COMPLETE — no missing pages")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--plan", type=int, metavar="N", help="render + emit next N batches as JSON")
    args = ap.parse_args()
    if args.status:
        status()
    elif args.plan:
        plan(args.plan)
    else:
        status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
