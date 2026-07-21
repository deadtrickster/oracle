#!/usr/bin/env python3
"""VL transcription lane for scanned Russian books (TODO G5) — all-in-one, resumable.

Qwen3-VL (local, :18081) reads each page IMAGE and transcribes it: prose verbatim, code
character-exact, display formulas as LaTeX. The pilot showed it beats both the embedded djvu text
layer (which mangles math: `2/[i-2]?`) and marker (which poisons Cyrillic prose with hallucinated
CJK and dropped `+ Fib(n-2)`). ~10-20 s/page, GPU-only — runs while DeepDoc owns the CPU.

Self-contained and crash-safe:
  - ensures the VL llama-server is up (starts it if not — survives reboots)
  - per-page output files: a rerun skips finished pages (power loss = resume, not restart)
  - page PNGs are rendered on demand (niced) and deleted after use
  - when a book completes, pages assemble into corpus/ml/<slug>.txt with [[p.N]] markers
  - finally, a seeded random 20-page audit sample is exported for the blind agreement review
    (RUBRIC discipline: no lane is trusted without a second grader on a sample)

    ./transcribe-scans.py            # run/resume everything
    ./transcribe-scans.py --status   # progress only
"""
import base64
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

VL_URL = os.environ.get("ORACLE_VL_URL", "http://127.0.0.1:18081")
VL_MODEL_DIR = Path.home() / "models/qwen3-vl"
BOOKS_DIR = Path.home() / "Documents/Books/ml"
REPO = Path(__file__).parent
PAGES_DIR = REPO / "corpus/ml/vl-pages"      # per-page transcripts (the resume state)
OUT_DIR = REPO / "corpus/ml"                 # assembled per-book .txt
AUDIT_DIR = REPO / "corpus/ml/vl-audit"      # 20-page sample for the blind review
DPI = 150

BOOKS = [  # (filename in BOOKS_DIR, slug)
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

PROMPT = (
    "Это страница русской технической книги (скан). Транскрибируй её СОДЕРЖИМОЕ точно:\n"
    "- прозу — дословно, ничего не пересказывай и не сокращай;\n"
    "- код — символ в символ, в блоке ```;\n"
    "- выключные (отдельно стоящие) формулы — как LaTeX в $$...$$, степени и индексы точно;\n"
    "- таблицы — построчно текстом;\n"
    "- если на странице рисунок/диаграмма — вставь краткую пометку вида [Рис.: что изображено];\n"
    "- НЕ включай колонтитулы и номер страницы.\n"
    "Выведи только транскрипцию, без комментариев."
)


def server_healthy() -> bool:
    try:
        return requests.get(f"{VL_URL}/health", timeout=5).status_code == 200
    except requests.RequestException:
        return False


def ensure_server():
    """Start the VL llama-server if it's not up (e.g. after a reboot). Never kills anything."""
    if server_healthy():
        return
    print("[server] not healthy — starting qwen3-vl llama-server...")
    env = {**os.environ,
           "LD_LIBRARY_PATH": "/usr/local/lib/ollama/cuda_v13:/usr/local/lib/ollama",
           "GGML_BACKEND_PATH": "/usr/local/lib/ollama/cuda_v13/libggml-cuda.so"}
    log = open(PAGES_DIR / "vl-server.log", "ab")
    subprocess.Popen(
        ["/usr/local/lib/ollama/llama-server",
         "--model", str(VL_MODEL_DIR / "Qwen3-VL-30B-A3B-Instruct-UD-Q4_K_XL.gguf"),
         "--mmproj", str(VL_MODEL_DIR / "mmproj-F16.gguf"),
         "--host", "127.0.0.1", "--port", "18081", "--alias", "qwen3-vl",
         "-ngl", "99", "-c", "16384", "--flash-attn", "on", "--jinja", "--no-webui",
         "--temp", "0.7", "--top-p", "0.8", "--top-k", "20", "--min-p", "0.0",
         "--presence-penalty", "1.5"],
        env=env, stdout=log, stderr=log, start_new_session=True)
    for _ in range(60):
        if server_healthy():
            print("[server] up")
            return
        time.sleep(5)
    sys.exit("[server] failed to become healthy — check vl-pages/vl-server.log")


def n_pages(pdf: Path) -> int:
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout
    return int(re.search(r"^Pages:\s+(\d+)", out, re.M).group(1))


def render_page(pdf: Path, page: int, dest: Path) -> Path:
    """pdftoppm one page, niced (the CPU belongs to the ingest)."""
    subprocess.run(["nice", "-n", "19", "pdftoppm", "-png", "-r", str(DPI),
                    "-f", str(page), "-l", str(page), str(pdf), str(dest / "pg")],
                   check=True, capture_output=True)
    pngs = sorted(dest.glob("pg-*.png")) or sorted(dest.glob("pg*.png"))
    return pngs[0]


def transcribe(png: Path) -> str:
    img = base64.b64encode(png.read_bytes()).decode()
    body = {"model": "qwen3-vl", "max_tokens": 2200,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}},
                {"type": "text", "text": PROMPT}]}]}
    for attempt in range(4):
        try:
            r = requests.post(f"{VL_URL}/v1/chat/completions", json=body, timeout=420)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except (requests.RequestException, KeyError) as e:
            print(f"    [retry {attempt + 1}] {str(e)[:80]}")
            ensure_server()
            time.sleep(5)
    raise RuntimeError(f"transcription failed after retries: {png}")


def status():
    total = done = 0
    for fname, slug in BOOKS:
        pdf = BOOKS_DIR / fname
        if not pdf.exists():
            print(f"  MISSING {fname}")
            continue
        n = n_pages(pdf)
        d = len(list((PAGES_DIR / slug).glob("p-*.txt"))) if (PAGES_DIR / slug).exists() else 0
        total += n
        done += d
        print(f"  {slug:44} {d:4}/{n}")
    print(f"  TOTAL {done}/{total}")


def main() -> int:
    if "--status" in sys.argv:
        status()
        return 0
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    ensure_server()

    for fname, slug in BOOKS:
        pdf = BOOKS_DIR / fname
        if not pdf.exists():
            print(f"SKIP missing: {fname}")
            continue
        n = n_pages(pdf)
        book_dir = PAGES_DIR / slug
        book_dir.mkdir(exist_ok=True)
        todo = [p for p in range(1, n + 1)
                if not (book_dir / f"p-{p:04}.txt").is_file()
                or not (book_dir / f"p-{p:04}.txt").stat().st_size]
        print(f"== {slug}: {n} pages, {len(todo)} to do")
        t0 = time.time()
        for i, p in enumerate(todo, 1):
            tmp = book_dir / "render"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir()
            png = render_page(pdf, p, tmp)
            text = transcribe(png)
            (book_dir / f"p-{p:04}.txt").write_text(text, encoding="utf-8")
            shutil.rmtree(tmp, ignore_errors=True)
            if i % 20 == 0:
                rate = (time.time() - t0) / i
                print(f"   {slug}: {i}/{len(todo)} ({rate:.1f}s/page, "
                      f"~{(len(todo) - i) * rate / 60:.0f} min left)", flush=True)
        # assemble the book when complete
        if all((book_dir / f"p-{p:04}.txt").is_file() for p in range(1, n + 1)):
            out = OUT_DIR / f"{slug}.txt"
            with out.open("w", encoding="utf-8") as f:
                for p in range(1, n + 1):
                    f.write(f"\n[[p.{p}]]\n")
                    f.write((book_dir / f"p-{p:04}.txt").read_text(encoding="utf-8"))
                    f.write("\n")
            print(f"== ASSEMBLED {out.name} ({out.stat().st_size // 1024} KB)")

    # audit sample: seeded, reproducible; re-render the PNGs so the blind reviewer sees the source
    AUDIT_DIR.mkdir(exist_ok=True)
    rng = random.Random(7)
    pool = []
    for fname, slug in BOOKS:
        pdf = BOOKS_DIR / fname
        if pdf.exists():
            pool += [(pdf, slug, p) for p in range(1, n_pages(pdf) + 1)
                     if (PAGES_DIR / slug / f"p-{p:04}.txt").is_file()]
    for pdf, slug, p in rng.sample(pool, min(20, len(pool))):
        base = AUDIT_DIR / f"{slug}-p{p:04}"
        if not base.with_suffix(".png").exists():
            tmp = AUDIT_DIR / "render"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir()
            png = render_page(pdf, p, tmp)
            shutil.move(str(png), base.with_suffix(".png"))
            shutil.rmtree(tmp, ignore_errors=True)
        shutil.copy(PAGES_DIR / slug / f"p-{p:04}.txt", base.with_suffix(".txt"))
    print(f"AUDIT SAMPLE ready in {AUDIT_DIR} (20 pages) — blind-review before ingest")
    print("ALL DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
