#!/usr/bin/env python3
"""Compare Unlimited-OCR against the incumbent extraction, on the pages that actually broke.

    ./ocr-compare.py --book neurocomp-kn01-galushkin-teoriya --pages 8
    ./ocr-compare.py --arxiv --limit 5
    ./ocr-compare.py --list-books

Runs under the model's venv:
    /mnt/data/models/unlimited-ocr/venv/bin/python ocr-compare.py ...

## Why these two corpora and not a benchmark score

DESIGN 4.4 settled the extraction question by measuring three candidates on the same pages of the
same book, across prose / code / math. marker lost for one specific reason: it hallucinated CJK and
Georgian *into Russian prose*, which is the same weird-glyph class the curation classifier hunts, so
ingesting it would have manufactured junk at scale. qwen3-vl:30b won and became the lane.

A new model's benchmark number says nothing about that failure. OmniDocBench is largely EN/ZH, and
Unlimited-OCR's own paper does not claim multilingual strength. So the test is the incumbent's home
turf: the scanned Cyrillic books, page for page against qwen3-vl's stored transcripts.

The arXiv mode is the easier case - English papers whose CID font map is broken, where the incumbent
is pdftotext output containing NUL bytes. Anything readable beats that, so it tests capability rather
than judgement.

## What is measured

  foreign      characters from scripts that have no business in a Russian technical book: CJK, kana,
               Hangul, Georgian, Arabic, Devanagari, Hebrew, Thai. This is the metric that decided
               the last bake-off. Non-zero is disqualifying, not a demerit.
  cyrillic     share of letters that are Cyrillic. Collapse means the model transliterated or gave up.
  agreement    difflib ratio against the incumbent's text for the same page. High agreement means
               both read the same page the same way. LOW agreement is not automatically a loss - it
               means a human has to look - so it is reported, never scored.
  math/code    counts of LaTeX and fence markers, because those are where the last bake-off's
               candidates actually differed.

Every page's two texts are written side by side under corpus/ocr-compare/ for reading. The numbers
narrow down which pages to read; they do not decide.
"""
import argparse
import difflib
import os
import random
import re
import sys
import unicodedata
from pathlib import Path

ORACLE = Path(__file__).resolve().parent
BOOKS_DIR = Path.home() / "Documents/Books/ml"
VL_PAGES = ORACLE / "corpus" / "ml" / "vl-pages"
OUT = ORACLE / "corpus" / "ocr-compare"
STAGING = ORACLE / "corpus" / "arxiv-ocr-staging"
ARXIV_TXT = ORACLE / "corpus" / "arxiv"
WEIGHTS = os.environ.get("OCR_WEIGHTS", "/mnt/data/models/unlimited-ocr/weights")

# Scripts that cannot legitimately appear in these books. marker's failure was producing exactly
# these inside Russian prose, so they are checked by codepoint block rather than by heuristic.
FOREIGN = [
    (0x4E00, 0x9FFF),   # CJK unified ideographs
    (0x3040, 0x30FF),   # hiragana + katakana
    (0xAC00, 0xD7AF),   # hangul
    (0x10A0, 0x10FF),   # georgian
    (0x0600, 0x06FF),   # arabic
    (0x0900, 0x097F),   # devanagari
    (0x0590, 0x05FF),   # hebrew
    (0x0E00, 0x0E7F),   # thai
]


def foreign_chars(text: str):
    hits = {}
    for ch in text:
        cp = ord(ch)
        for lo, hi in FOREIGN:
            if lo <= cp <= hi:
                hits.setdefault(unicodedata.name(ch, "?").split()[0], 0)
                hits[unicodedata.name(ch, "?").split()[0]] += 1
                break
    return hits


def stats(text: str) -> dict:
    letters = [c for c in text if c.isalpha()]
    cyr = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
    f = foreign_chars(text)
    return {
        "chars": len(text),
        "cyr": (cyr / len(letters)) if letters else 0.0,
        "foreign": sum(f.values()),
        "foreign_kinds": f,
        "math": len(re.findall(r"\$|\\\(|\\\[|\\begin\{", text)),
        "code": text.count("```"),
    }


def load_model():
    import torch
    from transformers import AutoModel, AutoTokenizer
    free = torch.cuda.mem_get_info()[0] / 2**30 if torch.cuda.is_available() else 0
    if free < 8:
        print(f"only {free:.1f} GiB VRAM free. Evict the big model:")
        print("  systemctl --user stop oracle-qwen-next.service")
        sys.exit(1)
    tok = AutoTokenizer.from_pretrained(WEIGHTS, trust_remote_code=True)
    model = AutoModel.from_pretrained(WEIGHTS, trust_remote_code=True,
                                      torch_dtype=torch.bfloat16).eval().cuda()
    return tok, model


def strip_markup(raw) -> str:
    # Refuse to stringify a non-string. infer() returns None unless eval_mode=True - its only
    # `return outputs` sits behind `if '<image>' in ... and eval_mode`, and the text otherwise
    # reaches stdout through the streamer and nowhere else. Calling str() on that produced the
    # 4-character string "None", which then scored as a real result: eight pages of "the model
    # returned almost nothing" that were entirely this bug.
    if raw is None:
        raise ValueError("model returned None - pass eval_mode=True to infer()")
    text = raw[0] if isinstance(raw, (tuple, list)) and raw else raw
    if not isinstance(text, str):
        raise TypeError(f"expected str from the model, got {type(text).__name__}")
    text = re.sub(r"<\|det\|>.*?<\|/det\|>", "", text, flags=re.S)
    text = re.sub(r"<\|/?[a-z]+\|>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text.replace("<PAGE>", "\n\n")).strip()


def book_file(slug: str):
    """Map a slug back to its PDF by reading transcribe-scans.py's own BOOKS table, so the two
    cannot drift apart."""
    src = (ORACLE / "transcribe-scans.py").read_text(encoding="utf-8")
    block = re.search(r"BOOKS = \[(.*?)\n\]", src, re.S)
    if not block:
        return None
    for fn, sl in re.findall(r'\("([^"]+)",\s*\n?\s*"([^"]+)"\)', block.group(1)):
        if sl == slug:
            p = BOOKS_DIR / fn
            return p if p.exists() else None
    return None


def cmp_books(a):
    import fitz
    slug = a.book
    pdf = book_file(slug)
    if pdf is None:
        print(f"no PDF for slug {slug}")
        return 1
    pages_dir = VL_PAGES / slug
    have = sorted(int(p.stem.split("-")[1]) for p in pages_dir.glob("p-*.txt"))
    if not have:
        print(f"no incumbent transcripts in {pages_dir}")
        return 1

    random.seed(a.seed)
    sample = sorted(random.sample(have, min(a.pages, len(have))))
    print(f"{slug}: {len(have)} incumbent pages, sampling {len(sample)} (seed {a.seed})\n")

    tok, model = load_model()
    dest = OUT / slug
    dest.mkdir(parents=True, exist_ok=True)
    scratch = dest / ".png"
    scratch.mkdir(exist_ok=True)

    print(f"{'page':>6}  {'model':<10}{'chars':>7}{'cyr':>7}{'foreign':>9}{'math':>6}{'code':>6}"
          f"{'agree':>7}")
    totals = {"foreign_new": 0, "foreign_old": 0}
    with fitz.open(pdf) as doc:
        for pno in sample:
            old = (pages_dir / f"p-{pno:04}.txt").read_text(encoding="utf-8", errors="replace")
            if pno - 1 >= len(doc):
                continue
            img = scratch / f"p{pno:04}.png"
            doc[pno - 1].get_pixmap(dpi=a.dpi).save(img)
            try:
                raw = model.infer(tok, prompt="<image>\nFree OCR. ", image_file=str(img),
                                  output_path=str(scratch), base_size=1024, image_size=640,
                                  crop_mode=True, no_repeat_ngram_size=30, ngram_window=180,
                                  # Required: infer() returns None without it.
                                  eval_mode=True)
            except Exception as e:  # noqa: BLE001
                print(f"{pno:>6}  infer failed: {e}")
                continue
            new = strip_markup(raw)
            (dest / f"p-{pno:04}.new.txt").write_text(new, encoding="utf-8")
            (dest / f"p-{pno:04}.old.txt").write_text(old, encoding="utf-8")

            so, sn = stats(old), stats(new)
            agree = difflib.SequenceMatcher(None, old, new).ratio()
            totals["foreign_old"] += so["foreign"]
            totals["foreign_new"] += sn["foreign"]
            print(f"{pno:>6}  {'qwen3-vl':<10}{so['chars']:>7}{so['cyr']:>7.2f}"
                  f"{so['foreign']:>9}{so['math']:>6}{so['code']:>6}{'':>7}")
            print(f"{'':>6}  {'unlimited':<10}{sn['chars']:>7}{sn['cyr']:>7.2f}"
                  f"{sn['foreign']:>9}{sn['math']:>6}{sn['code']:>6}{agree:>7.2f}")
            if sn["foreign_kinds"]:
                print(f"{'':>6}  foreign scripts in NEW: {sn['foreign_kinds']}")
            img.unlink(missing_ok=True)

    print(f"\nforeign-script chars  incumbent {totals['foreign_old']}  "
          f"unlimited {totals['foreign_new']}")
    print(f"side-by-side texts in {dest}")
    return 0


def cmp_arxiv(a):
    """Broken pdftotext vs staged OCR. No model needed - both texts already exist."""
    staged = sorted(STAGING.glob("*.txt"))[:a.limit or None]
    if not staged:
        print(f"nothing staged in {STAGING}; run ocr-unlimited.py first")
        return 1
    print(f"{'paper':<14}{'source':<12}{'chars':>9}{'nul':>7}{'foreign':>9}{'math':>6}")
    for s in staged:
        new = s.read_text(encoding="utf-8", errors="replace")
        oldp = ARXIV_TXT / s.name
        old = oldp.read_text(encoding="utf-8", errors="replace") if oldp.exists() else ""
        for label, t in (("pdftotext", old), ("unlimited", new)):
            st = stats(t)
            print(f"{s.stem if label=='pdftotext' else '':<14}{label:<12}{st['chars']:>9}"
                  f"{t.count(chr(0)):>7}{st['foreign']:>9}{st['math']:>6}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--book")
    ap.add_argument("--arxiv", action="store_true")
    ap.add_argument("--list-books", action="store_true")
    ap.add_argument("--pages", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20260721,
                    help="fixed so the sample is reproducible, per the audit discipline in DESIGN 4.4")
    a = ap.parse_args()

    if a.list_books:
        for d in sorted(VL_PAGES.glob("*/")):
            n = len(list(d.glob("p-*.txt")))
            print(f"  {d.name:<46} {n:>5} pages  "
                  f"{'pdf ok' if book_file(d.name) else 'PDF MISSING'}")
        return 0
    if a.arxiv:
        return cmp_arxiv(a)
    if a.book:
        return cmp_books(a)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
