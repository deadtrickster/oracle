#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Dedup a pile of book PDFs/EPUBs, preferring the most recent edition, and drop anything already
ingested into the Oracle corpus.

TWO PHASES, cleanly split:

  EXTRACT (`--extract`)  loop EVERY pdf ONCE through local qwen-next: feed it the first pages and
     get back {title, authors, edition, year}. Page count comes from pdfinfo. Results are cached
     to ~/Documents/Books/.book-identities.json keyed by path+size, so extraction is a resumable
     one-pass job (N model calls, never N²) and can run in the background. qwen is a feature
     extractor here — it does NOT do any matching.

  MATCH (default)  pure Python over the cached identities: group by canonical qwen title (fuzzy),
     keep the newest edition/year within each group (tiebreak pages, then file size), and drop
     anything whose title matches a corpus/*_raw source already ingested. No model calls. Instant
     and re-runnable. Falls back to the filename title when a file has no cached identity yet.

    ./dedup-books.py --extract            # populate/refresh the identity cache via qwen-next
    ./dedup-books.py                      # match + report using the cache (+ filename fallback)
    ./dedup-books.py --json plan.json     # also write the KEEP plan
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).parent
BOOKS = HOME / "Documents/Books"
IDENTITY_CACHE = BOOKS / ".book-identities.json"
# qwen-next is served by a dedicated llama-server (OpenAI-compatible), NOT ollama.
QWEN_URL = "http://localhost:18080"
QWEN_MODEL = "qwen3-coder-next"

CANDIDATE_ROOTS = [
    BOOKS / "My-Books-Collections", BOOKS / "Book-Collection", BOOKS / "Books-Collection",
    BOOKS / "oreilly-books-collection-", BOOKS / "awesome-book-collection",
    BOOKS / "ml", BOOKS / "bio",
    BOOKS / "LTL", BOOKS / "IR-Foundations",  # 2026-07-25 IR shelf (papers/theses/tutorials)
    BOOKS,  # BOOKS itself = loose files (non-recursive)
]
INGESTED_GLOB = "corpus/*_raw/*.pdf"
EXTS = {".pdf", ".epub", ".djvu"}

ED_RX = re.compile(r"\b(\d+)(?:st|nd|rd|th)?\s*(?:ed(?:ition)?|edition)\b", re.I)
YEAR_RX = re.compile(r"(?:19|20)\d{2}")
PUB_RX = re.compile(r"\b(o'?reilly|packt|manning|apress|springer|wiley|no\s*starch|mit\s*press|"
                    r"addison|pearson|mcgraw|media|press|publications?|edition|ebook|www|com|"
                    r"libgen|li|lc|pdfdrive)\b", re.I)
STOP = {"the", "a", "an", "of", "to", "and", "for", "in", "with", "on", "by", "your", "second",
        "third", "fourth", "first", "revised", "reprint"}
HAVE_PDFINFO = shutil.which("pdfinfo") is not None
HAVE_PDFTOTEXT = shutil.which("pdftotext") is not None


def _clean(s: str) -> str:
    s = re.sub(r"\(\d+\)", " ", s)
    s = ED_RX.sub(" ", s)
    s = YEAR_RX.sub(" ", s)
    s = PUB_RX.sub(" ", s)
    s = s.replace("_", " ").replace("-", " ")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip().lower()


def sig_words(name: str) -> list[str]:
    return [w for w in _clean(name).split() if w not in STOP and len(w) > 1]


def edition_rank(name: str, edition, year) -> int:
    """Higher = newer. Prefer qwen's structured edition/year; fall back to the filename."""
    if isinstance(edition, int) and edition > 0:
        return 1000 + edition
    if isinstance(year, int) and year > 1900:
        return year
    m = ED_RX.search(name)
    if m:
        return 1000 + int(m.group(1))
    yrs = [int(y) for y in YEAR_RX.findall(name)]
    return max(yrs) if yrs else 0


def containment(a: set, b: set) -> float:
    """Overlap over the SMALLER set — robust when one title is much terser than the other."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def pdf_pages(path: Path) -> int | None:
    if not HAVE_PDFINFO or path.suffix.lower() != ".pdf":
        return None
    try:
        out = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=30)
        for line in out.stdout.splitlines():
            if line.startswith("Pages:"):
                return int(line.split()[1])
    except Exception:
        return None
    return None


def qwen_extract(path: Path) -> dict:
    """One qwen-next call: first pages -> {title, authors, edition, year}. Feature extraction only."""
    if path.suffix.lower() != ".pdf" or not HAVE_PDFTOTEXT:
        return {}
    try:
        out = subprocess.run(["pdftotext", "-f", "1", "-l", "5", str(path), "-"],
                             capture_output=True, text=True, timeout=60)
        text = out.stdout.strip()[:4000]
        if len(text) < 40:
            return {}
        prompt = ("These are the first pages of a book (may be OCR-noisy). Identify it. Reply with "
                  "ONLY compact JSON, no prose: "
                  '{"title": <string>, "authors": <string>, "edition": <int or null>, '
                  '"year": <int or null>}. Use the canonical published title.\n\n---\n' + text)
        req = urllib.request.Request(
            f"{QWEN_URL}/v1/chat/completions",
            data=json.dumps({"model": QWEN_MODEL, "temperature": 0, "stream": False,
                             "response_format": {"type": "json_object"},
                             "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            content = json.loads(r.read())["choices"][0]["message"]["content"]
        d = json.loads(content)
        return {"title": str(d.get("title") or ""), "authors": str(d.get("authors") or ""),
                "edition": d.get("edition"), "year": d.get("year")}
    except Exception as e:
        return {"error": str(e).splitlines()[0][:80]}


def scan(root: Path, recursive: bool = True) -> list[Path]:
    if not root.exists():
        return []
    it = root.rglob("*") if recursive else root.glob("*")
    return [p for p in it if p.suffix.lower() in EXTS and p.is_file()]


def gather() -> list[Path]:
    seen, out = set(), []
    for root in CANDIDATE_ROOTS:
        for p in scan(root, recursive=(root.name != "Books")):
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(p)
    return out


def cache_key(p: Path) -> str:
    try:
        return f"{p.resolve()}::{p.stat().st_size}"
    except OSError:
        return str(p.resolve())


def load_cache() -> dict:
    if IDENTITY_CACHE.exists():
        try:
            return json.loads(IDENTITY_CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def do_status() -> int:
    """One-line extraction progress — so checking it is a single allowlisted command, not an
    inline python that trips a permission prompt."""
    cache = load_cache()
    pdfs = [p for p in gather() if p.suffix.lower() == ".pdf"]
    done = sum(1 for p in pdfs if cache_key(p) in cache)
    errs = sum(1 for p in pdfs if isinstance(cache.get(cache_key(p)), dict)
               and cache[cache_key(p)].get("error"))
    total_files = len(gather())
    print(f"identity extraction: {done}/{len(pdfs)} pdfs cached ({errs} errors), "
          f"{len(pdfs) - done} remaining")
    print(f"library: {total_files} files ({len(pdfs)} pdf, {total_files - len(pdfs)} epub/djvu)")
    if done:
        last = [v.get("title", "") for k, v in cache.items()
                if isinstance(v, dict) and v.get("title")][-3:]
        for t in last:
            print(f"  recent: {t[:60]}")
    return 0


def do_extract() -> int:
    """Phase 1: loop every PDF once through qwen-next, cache {title,authors,edition,year}.
    Resumable — a file already cached (same path+size) is skipped."""
    cache = load_cache()
    files = [p for p in gather() if p.suffix.lower() == ".pdf"]
    todo = [p for p in files if cache_key(p) not in cache]
    print(f"identity cache: {len(cache)} known; {len(files)} pdfs; {len(todo)} to extract")
    for i, p in enumerate(todo, 1):
        ident = qwen_extract(p)
        cache[cache_key(p)] = ident
        IDENTITY_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
        t = ident.get("title", "") or ident.get("error", "?")
        print(f"  [{i}/{len(todo)}] {p.name[:50]:50} -> {t[:50]}")
    print(f"done; cache has {len(cache)} identities at {IDENTITY_CACHE}")
    return 0


def title_tokens(p: Path, cache: dict) -> tuple[set, str, str]:
    """Preferred title token set: qwen's canonical title if cached, else the filename. Returns
    (token_set, display_title, source)."""
    ident = cache.get(cache_key(p), {})
    qt = _clean(ident.get("title", "")) if isinstance(ident, dict) else ""
    if len(qt.split()) >= 2:
        return set(w for w in qt.split() if w not in STOP), qt, "qwen"
    fn = " ".join(sig_words(p.stem))
    return set(fn.split()), fn, "filename"


def do_match(json_out: str | None) -> int:
    cache = load_cache()
    candidates = gather()

    info = {}
    for p in candidates:
        toks, disp, src = title_tokens(p, cache)
        ident = cache.get(cache_key(p), {}) if isinstance(cache.get(cache_key(p)), dict) else {}
        info[p] = {"toks": toks, "title": disp, "src": src, "pages": pdf_pages(p),
                   "edition": ident.get("edition"), "year": ident.get("year")}

    ingested = {}
    for p in REPO.glob(INGESTED_GLOB):
        ingested.setdefault(" ".join(sig_words(p.stem)[:6]), p.name)

    # union-find grouping on the (mostly qwen) titles — matching is pure Python here.
    parent = {p: p for p in candidates}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    cand = list(candidates)
    merges = []
    for i in range(len(cand)):
        for j in range(i + 1, len(cand)):
            p, q = cand[i], cand[j]
            if find(p) == find(q):
                continue
            a, b = info[p]["toks"], info[q]["toks"]
            jw, cw = jaccard(a, b), containment(a, b)
            pg1, pg2 = info[p]["pages"], info[q]["pages"]
            same_pages = bool(pg1) and pg1 == pg2
            fmt_pair = {p.suffix.lower(), q.suffix.lower()} == {".pdf", ".epub"}
            both_qwen = info[p]["src"] == "qwen" and info[q]["src"] == "qwen"
            # qwen titles are clean, so a high overlap there is trustworthy on its own; filename
            # titles still need page/format corroboration (distinct books share domain phrases).
            if cw >= 0.85 and (both_qwen or same_pages or fmt_pair or jw >= 0.85):
                parent[find(p)] = find(q)
                merges.append((p, q, f"{'qwen' if both_qwen else 'name'} cw={cw:.2f}"
                               + (f",pg={pg1}" if same_pages else ",fmt" if fmt_pair else "")))

    groups = defaultdict(list)
    for p in candidates:
        groups[find(p)].append(p)

    keep, dup, already = [], [], []
    for grp in groups.values():
        keys = [" ".join(sorted(info[p]["toks"]))[:60] for p in grp]
        ing = next((ingested[k] for p in grp
                    for k in [" ".join(sig_words(p.stem)[:6])] if k in ingested), None)
        if ing:
            already.extend((p, ing) for p in grp)
            continue
        winner = max(grp, key=lambda p: (edition_rank(p.name, info[p]["edition"], info[p]["year"]),
                                         info[p]["pages"] or 0, p.stat().st_size))
        keep.append(winner)
        dup.extend((p, winner) for p in grp if p != winner)
        _ = keys

    def rel(p):
        try:
            return str(p.relative_to(HOME))
        except ValueError:
            return str(p)

    n_qwen = sum(1 for p in candidates if info[p]["src"] == "qwen")
    print(f"scanned {len(candidates)} files -> {len(groups)} books  "
          f"(titles: {n_qwen} from qwen, {len(candidates)-n_qwen} from filename)")
    print(f"already ingested (corpus/*_raw): {len(ingested)} titles\n")
    print(f"=== KEEP — ingest these ({len(keep)}) ===")
    for p in sorted(keep, key=lambda x: info[x]["title"]):
        pg = info[p]["pages"]
        print(f"  {rel(p)}" + (f"   [{pg}p]" if pg else "") + f"   «{info[p]['title'][:60]}»")
    print(f"\n=== DUP — older/duplicate editions dropped ({len(dup)}) ===")
    for p, w in dup:
        print(f"  {rel(p)}\n      -> superseded by {w.name}")
    print(f"\n=== MERGES — verify (grouped beyond exact title) ({len(merges)}) ===")
    for p, q, why in merges:
        print(f"  [{why}]\n      {p.name}\n      {q.name}")
    print(f"\n=== ALREADY INGESTED — skip ({len(already)}) ===")
    for p, name in already:
        print(f"  {rel(p)}\n      -> already in corpus as {name}")

    if json_out:
        Path(json_out).write_text(json.dumps({"keep": [str(p) for p in keep]},
                                             indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"\nwrote KEEP plan -> {json_out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--extract", action="store_true",
                    help="phase 1: fill the qwen-next identity cache (one model call per pdf)")
    ap.add_argument("--status", action="store_true", help="print extraction progress and exit")
    ap.add_argument("--json", metavar="FILE", help="phase 2: also write the KEEP plan as JSON")
    a = ap.parse_args()
    if a.status:
        return do_status()
    return do_extract() if a.extract else do_match(a.json)


if __name__ == "__main__":
    sys.exit(main())
