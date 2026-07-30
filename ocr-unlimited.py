#!/usr/bin/env python3
"""Re-read papers whose PDF text layer is broken, using baidu/Unlimited-OCR.

    ./ocr-unlimited.py --dry-run              show what would be processed
    ./ocr-unlimited.py --limit 3              try three papers first
    ./ocr-unlimited.py                        all of them, into staging
    ./ocr-unlimited.py --promote              copy accepted results into corpus/arxiv/

Runs under the model's own venv:
    /mnt/data/models/unlimited-ocr/venv/bin/python ocr-unlimited.py ...

## What this is for

51 arXiv papers have a damaged CID font map, so pdftotext returned NUL bytes and, around them,
characters that are not what the page says. Corpus policy is that such a page must be re-read, never
stripped and patched up: stripping keeps the garbage that surrounded the NUL and indexes it as if it
were the page, which is confident nonsense that retrieval will happily cite.

## Why it stages instead of overwriting

The existing .txt is bad, but "bad" is known. An OCR result that is worse, truncated, or hallucinated
would replace a known problem with a hidden one. So output goes to corpus/arxiv-ocr-staging/, gets
compared against the old text, and only moves into place with --promote. Nothing here overwrites a
corpus file on its own.

## Acceptance

A result is only worth promoting if it is plausibly the paper. Checked, in order of how cheaply they
catch a disaster:

  foreign script  CJK, kana, hangul, Georgian, Arabic, Devanagari. Checked by codepoint block, not
                  by "non-ASCII", since legitimate maths and accented names are non-ASCII too.
  no NUL          the entire point; a NUL in the output means we achieved nothing.
  length          OCR output far shorter than the broken extraction usually means it stopped early.
  alpha ratio     letters over total characters. Real prose sits high. Garbled output does not.
  repetition      the failure mode of long-horizon decoding is looping. Measured as the share of
                  the output taken by the single most common 40-character block.

These are reported per paper and never auto-applied beyond gating --promote, because a threshold
that silently drops a paper is the same amnesia problem in a different coat.

## Result on this model (2026-07-31): REJECTED, do not promote

First three papers, all three rejected. 12,533 CJK characters in one English paper, in two distinct
failure modes:

  language switching + loop   "...more expensive than the \\(\\ell_0\\)-space-based NMS,但该函数是
                              \\(\\ell_0\\) 的连续形式,故该函数是 \\(\\ell_0\\) 的连续形式,故..."
  memorised invention         "(1) 2017年1月1日，公司召开2017年第一次临时股东大会，并通知全体董事。"
                              Chinese corporate-governance boilerplate, in a vector-search paper.

The foreign-script check was added *because of* this run. The original gate passed all three as
"ok": NUL-free, longer than the broken text, high letter ratio, no 40-char repetition. Fluent,
confident, and not what the page says - the same class that disqualified marker in DESIGN 4.4.

The broken pdftotext text is worse as text and better as a corpus citizen: it fails visibly and is
marked FAILED. This would have been indexed as clean prose.
"""
import argparse
import collections
import os
import re
import shutil
import sys
from pathlib import Path

ORACLE = Path(__file__).resolve().parent
LIST = ORACLE / "corpus" / "arxiv-needs-ocr.txt"
CORPUS = ORACLE / "corpus" / "arxiv"
STAGING = ORACLE / "corpus" / "arxiv-ocr-staging"
PDF_ROOT = Path(os.environ.get("ARXIV_PDF_ROOT", "/mnt/data/arxiv/pdf"))
WEIGHTS = os.environ.get("OCR_WEIGHTS", "/mnt/data/models/unlimited-ocr/weights")


def wanted(path: Path):
    """(id, nul_bytes) for each entry. The file is generated; comments and blanks are skipped."""
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if parts and re.match(r"^\d{4}\.\d{4,5}$", parts[0]):
            out.append((parts[0], int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0))
    return out


def find_pdf(paper_id: str):
    """arXiv lays out /pdf/YYMM/ID<version>.pdf. Take the highest version present."""
    d = PDF_ROOT / paper_id.split(".")[0]
    if not d.is_dir():
        return None
    cands = sorted(d.glob(f"{paper_id}v*.pdf")) or sorted(d.glob(f"{paper_id}.pdf"))
    return cands[-1] if cands else None


def clean(raw) -> str:
    """Turn one infer_multi result into plain prose.

    Two things have to happen here and both were got wrong first time round.

    infer_multi returns `(text, output_tokens)`, not a string. Calling str() on the tuple stored a
    Python repr in the corpus: the whole document on one line, wrapped in `('...')`, with every
    newline as a literal backslash-n.

    And the multi-page prompt is a GROUNDING prompt, so the text carries layout markup:
    `<|det|>title [105, 64, 892, 120]<|/det|>` before each block, and `<PAGE>` between pages. That is
    genuinely useful information - it is a layout analysis for free - but it is not what belongs in a
    retrieval chunk, where it would tokenise into noise and dilute every embedding.
    """
    text = raw[0] if isinstance(raw, (tuple, list)) and raw else raw
    if not isinstance(text, str):
        text = str(text)
    # Drop the bounding-box annotations, keep the text they annotate.
    text = re.sub(r"<\|det\|>.*?<\|/det\|>", "", text, flags=re.S)
    text = re.sub(r"<\|/?[a-z]+\|>", "", text)
    # Page breaks become blank lines, so the page boundary survives as structure without a token.
    text = text.replace("<PAGE>", "\n\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def quality(text: str) -> dict:
    n = len(text)
    if n == 0:
        return {"chars": 0, "nul": 0, "alpha": 0.0, "ascii": 0.0, "foreign": 0, "repeat": 1.0}
    blocks = [text[i:i + 40] for i in range(0, n - 40, 40)] or [text]
    top = collections.Counter(blocks).most_common(1)[0][1] if blocks else 1
    # Codepoint blocks that cannot legitimately appear in these English papers. Checked by block
    # rather than "non-ASCII", because legitimate maths and accented names are non-ASCII too.
    foreign = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af"
                             r"\u10a0-\u10ff\u0600-\u06ff\u0900-\u097f]", text))
    return {
        "chars": n,
        "nul": text.count("\x00"),
        "alpha": sum(c.isalpha() for c in text) / n,
        "ascii": sum(c.isascii() for c in text) / n,
        "foreign": foreign,
        "repeat": top * 40 / n,
    }


def verdict(new: dict, old_chars: int) -> str:
    # Foreign script first, because it is the failure that matters and the one every other metric
    # here missed. Measured on the first three papers: 12,533 CJK characters in an English paper,
    # including degenerate Chinese looping mid-sentence and a memorised Chinese corporate-governance
    # sentence invented whole. All three passed nul/length/alpha/repeat as "ok".
    #
    # This is the same class that disqualified marker in the 2026-07-21 bake-off (DESIGN 4.4):
    # fluent, confident text that is not what the page says. A NUL is honest damage; this is not.
    if new["foreign"] > 0:
        return f"REJECT {new['foreign']} foreign-script chars (hallucinated)"
    if new["nul"]:
        return "REJECT nul in output"
    if new["chars"] < 500:
        return "REJECT too short"
    if old_chars and new["chars"] < old_chars * 0.4:
        return "REJECT much shorter than the broken text"
    if new["alpha"] < 0.5:
        return "REJECT low letter ratio"
    if new["repeat"] > 0.2:
        return "REJECT repetition loop"
    return "ok"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", default=str(LIST))
    ap.add_argument("--limit", type=int, default=0, help="process only the first N")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--promote", action="store_true",
                    help="copy accepted staged results into corpus/arxiv/")
    ap.add_argument("--reclean", action="store_true",
                    help="re-run the cleaner over already-staged files, no GPU. Recovers output "
                         "written before clean() existed, when each pass was stored as a Python "
                         "repr of the (text, tokens) tuple.")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--pages-per-pass", type=int, default=8,
                    help="images per infer_multi call; the model's 32K budget covers the prefill "
                         "for all of them, so this trades VRAM against round trips")
    a = ap.parse_args()

    items = wanted(Path(a.list))
    if a.limit:
        items = items[:a.limit]
    if not items:
        print("nothing to do")
        return 0

    STAGING.mkdir(parents=True, exist_ok=True)

    if a.promote:
        moved = 0
        for pid, _ in items:
            src = STAGING / f"{pid}.txt"
            if not src.exists():
                continue
            old = CORPUS / f"{pid}.txt"
            q = quality(src.read_text(encoding="utf-8", errors="replace"))
            v = verdict(q, len(old.read_text(encoding="utf-8", errors="replace")) if old.exists() else 0)
            if v != "ok":
                print(f"  skip {pid}: {v}")
                continue
            shutil.copy2(src, old)
            moved += 1
        print(f"\npromoted {moved} of {len(items)}")
        print("re-ingest with: python3 ingest-corpus.py --api-key <KEY> --only arxiv")
        return 0

    if a.reclean:
        import ast
        for pid, _ in items:
            src = STAGING / f"{pid}.txt"
            if not src.exists():
                continue
            parts = []
            for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("('") or line.startswith('("'):
                    try:
                        parts.append(clean(ast.literal_eval(line)))
                        continue
                    except (ValueError, SyntaxError):
                        pass
                parts.append(clean(line))
            src.write_text("\n\n".join(p for p in parts if p), encoding="utf-8")
            q = quality(src.read_text(encoding="utf-8"))
            print(f"  {pid}: {q['chars']} chars, alpha {q['alpha']:.2f}, repeat {q['repeat']:.2f}")
        return 0

    missing = [p for p, _ in items if find_pdf(p) is None]
    print(f"{len(items)} papers, {len(missing)} with no PDF in {PDF_ROOT}")
    if a.dry_run:
        for pid, nul in items[:10]:
            pdf = find_pdf(pid)
            old = CORPUS / f"{pid}.txt"
            print(f"  {pid}  nul={nul:<6} pdf={'yes' if pdf else 'NO':<4} "
                  f"old_chars={len(old.read_text(encoding='utf-8', errors='replace')) if old.exists() else 0}")
        if len(items) > 10:
            print(f"  ... and {len(items)-10} more")
        return 0

    import fitz  # pymupdf
    import torch
    from transformers import AutoModel, AutoTokenizer

    free = torch.cuda.mem_get_info()[0] / 2**30 if torch.cuda.is_available() else 0
    print(f"CUDA available={torch.cuda.is_available()} free={free:.1f} GiB")
    if torch.cuda.is_available() and free < 8:
        print("under 8 GiB free. Evict the big model first:")
        print("  systemctl --user stop oracle-qwen-next.service")
        return 1

    print(f"loading {WEIGHTS}")
    tok = AutoTokenizer.from_pretrained(WEIGHTS, trust_remote_code=True)
    model = AutoModel.from_pretrained(WEIGHTS, trust_remote_code=True,
                                      torch_dtype=torch.bfloat16).eval().cuda()

    scratch = STAGING / ".pages"
    scratch.mkdir(exist_ok=True)
    print(f"\n{'paper':<14}{'pages':>6}{'chars':>9}{'alpha':>7}{'rep':>6}  verdict")

    for pid, _ in items:
        pdf = find_pdf(pid)
        if pdf is None:
            print(f"{pid:<14}{'-':>6}{'-':>9}{'-':>7}{'-':>6}  no PDF")
            continue

        # Render once, reuse for every pass over this paper.
        imgs = []
        try:
            with fitz.open(pdf) as doc:
                for i, page in enumerate(doc):
                    p = scratch / f"{pid}_{i:03d}.png"
                    page.get_pixmap(dpi=a.dpi).save(p)
                    imgs.append(str(p))
        except Exception as e:  # noqa: BLE001
            print(f"{pid:<14}{'-':>6}{'-':>9}{'-':>7}{'-':>6}  render failed: {e}")
            continue

        parts = []
        try:
            for i in range(0, len(imgs), a.pages_per_pass):
                group = imgs[i:i + a.pages_per_pass]
                out = model.infer_multi(
                    tok, prompt="<image>Multi page parsing.", image_files=group,
                    output_path=str(scratch), image_size=640,
                    # The documented failure mode of long-horizon decoding is looping. The model
                    # exposes an n-gram block for exactly this; use it rather than post-filtering.
                    no_repeat_ngram_size=30, ngram_window=180,
                )
                parts.append(clean(out))
        except Exception as e:  # noqa: BLE001
            print(f"{pid:<14}{len(imgs):>6}{'-':>9}{'-':>7}{'-':>6}  infer failed: {e}")
            for p in imgs:
                Path(p).unlink(missing_ok=True)
            continue

        text = "\n\n".join(parts)
        (STAGING / f"{pid}.txt").write_text(text, encoding="utf-8")
        for p in imgs:
            Path(p).unlink(missing_ok=True)

        old = CORPUS / f"{pid}.txt"
        old_chars = len(old.read_text(encoding="utf-8", errors="replace")) if old.exists() else 0
        q = quality(text)
        print(f"{pid:<14}{len(imgs):>6}{q['chars']:>9}{q['alpha']:>7.2f}{q['repeat']:>6.2f}  "
              f"{verdict(q, old_chars)}")

    print(f"\nstaged in {STAGING}")
    print("review a few, then: ./ocr-unlimited.py --promote")
    return 0


if __name__ == "__main__":
    sys.exit(main())
