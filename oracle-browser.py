#!/usr/bin/env python3
"""Oracle corpus browser — search the corpus, then OPEN THE SOURCE PDF at the cited page.

The point (a plane must-have): a grounded answer is only trustworthy if you can VERIFY it against the
original. ask_corpus/search_corpus give you the passage; this gives you the passage *and* a one-click
jump into the real PDF at the exact page, offline. The page comes from DeepDoc's stored positions
(PDF-parsed KBs) or the [[p.N]] markers we carry through pdftotext/OCR (text KBs).

    uv run --with fastapi --with uvicorn --with requests python oracle-browser.py   # -> http://localhost:9765
"""
import html
import os
import re
import subprocess
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

RAGFLOW = os.environ.get("ORACLE_RAGFLOW_URL", "http://localhost:9380")
KEY = os.environ.get("ORACLE_RAGFLOW_KEY", "ragflow-smywlJs3drgGxfKztifTmD3iNJ2lP6Uvq2-suiLQTGM")
RERANK_ID = "gte-multilingual-reranker-base@local-gte-rerank@Jina"
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
CORPUS = (Path(__file__).parent / "corpus").resolve()
BOOKS = [Path.home() / "Documents" / "Books", Path.home() / "Documents" / "Books" / "bio"]
PORT = int(os.environ.get("ORACLE_BROWSER_PORT", "9765"))

app = FastAPI(title="oracle-browser")


def _kb_ids():
    r = requests.get(f"{RAGFLOW}/api/v1/datasets?page_size=100", headers=HDR, timeout=30)
    return {d["name"]: d["id"] for d in r.json()["data"] if d.get("chunk_count", 0) > 0}


def _resolve_pdf(docname: str) -> Path | None:
    """RAGFlow doc name -> the original PDF on disk.

    Names are `<subdir>__<file>` (corpus '/' encoded as '__'). A `*.pdf` doc maps straight to
    corpus/<subdir>/<file> (bio_raw/books_raw symlink to ~/Documents/Books). A `*.txt` doc (Cyrillic
    books we ran through pdftotext) has its PDF under the same basename in ~/Documents/Books[/bio]."""
    rel = docname.replace("__", "/")
    direct = (CORPUS / rel).resolve()
    if direct.suffix.lower() == ".pdf" and direct.exists():
        return direct
    stem = Path(rel).stem  # e.g. Teylor_Grin_Staut_3_Tom_2013
    for root in [CORPUS] + BOOKS:
        for cand in (root.rglob(f"{stem}.pdf") if root == CORPUS else root.glob(f"{stem}.pdf")):
            if cand.exists():
                return cand.resolve()
    return None


def _resolve_txt(docname: str) -> Path | None:
    """The source .txt for a text-parsed (naive) doc, so we can locate a chunk's page."""
    rel = docname.replace("__", "/")
    p = (CORPUS / rel).resolve()
    return p if p.suffix.lower() == ".txt" and p.exists() else None


# cache: docname -> list of (char_offset, page) from [[p.N]] markers, and the raw text
_TXT_CACHE: dict[str, tuple[str, list[tuple[int, int]]]] = {}


def _txt_pagemap(docname: str):
    if docname in _TXT_CACHE:
        return _TXT_CACHE[docname]
    txt = _resolve_txt(docname)
    if not txt:
        _TXT_CACHE[docname] = ("", [])
        return _TXT_CACHE[docname]
    body = txt.read_text(encoding="utf-8", errors="replace")
    marks = [(m.start(), int(m.group(1))) for m in re.finditer(r"\[\[p\.(\d+)\]\]", body)]
    _TXT_CACHE[docname] = (body, marks)
    return _TXT_CACHE[docname]


def _page_of(chunk: dict, docname: str) -> int | None:
    pos = chunk.get("positions") or []
    # Trust positions ONLY when it's a real DeepDoc bbox: [page, x0, x1, top, bottom] with a
    # non-degenerate box. Naive/text docs store a bogus counter here ([[619,618,618,618,618]]),
    # which is NOT a page — that put the viewer past the end of the PDF.
    if pos and pos[0] and len(pos[0]) >= 5:
        page, x0, x1, top, bot = pos[0][:5]
        if x1 > x0 and bot > top and page >= 1:
            return int(page)
    content = (chunk.get("content_with_weight") or chunk.get("content", "")).strip()
    # Text doc: locate this chunk in the source .txt by a DISTINCTIVE multi-word phrase (first word
    # alone matched the ToC entry — "Raft" appears there and links landed on p.9 instead of p.321),
    # then count [[p.N]] markers before that offset. Whitespace-tolerant, since the indexed chunk is
    # re-wrapped differently from the raw .txt.
    body, marks = _txt_pagemap(docname)
    if body and marks:
        words = [w for w in re.findall(r"\w{2,}", content) if not w.isdigit()][:6]
        idx = -1
        if len(words) >= 3:
            probe = re.compile(r"\W+".join(re.escape(w) for w in words))
            m = probe.search(body)
            idx = m.start() if m else -1
        if idx >= 0:
            page = None
            for off, pg in marks:
                if off <= idx:
                    page = pg
                else:
                    break
            if page:
                return page
    # marker literally inside the chunk: the chunk starts on the page BEFORE its first marker
    # (there is content preceding it), which is a good approximation when the phrase probe misses.
    m = re.search(r"\[\[p\.(\d+)\]\]", content)
    if m:
        n = int(m.group(1))
        return max(1, n - 1) if content.find(m.group(0)) > 40 else n
    return None


PAGE = """<!doctype html><meta charset=utf-8><title>Oracle corpus</title>
<style>body{{font:15px/1.5 system-ui;max-width:760px;margin:2rem auto;padding:0 1rem;background:#faf9f7}}
form{{display:flex;gap:.5rem}}input[name=q]{{flex:1;padding:.5rem}}button{{padding:.5rem 1rem}}
.hit{{border:1px solid #ddd;border-radius:6px;padding:.7rem 1rem;margin:.8rem 0;background:#fff}}
.src{{color:#666;font-size:.85em;margin-bottom:.5rem}}.src a{{color:#06c;text-decoration:none}}.src a:hover{{text-decoration:underline}}
.body{{white-space:pre-wrap;margin-top:.4rem}}img.pg{{width:100%;border:1px solid #eee;box-shadow:0 1px 6px #0002;display:block}}
mark{{background:#fe9}}.tag{{color:#999;font-size:.8em}}</style>
<h2>🔮 Oracle corpus browser</h2>
<form action=/search><input name=q value="{q}" placeholder="search the corpus…" autofocus>
<button>search</button></form>{body}"""


@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE.format(q="", body="<p class=tag>Search grounded passages, then open the source PDF at the cited page.</p>")


@app.get("/search", response_class=HTMLResponse)
def search(q: str = "", k: int = 12):
    if not q.strip():
        return home()
    ids = list(_kb_ids().values())
    body = {"question": q, "dataset_ids": ids, "page_size": k, "top_k": 64,
            "similarity_threshold": 0.1, "rerank_id": RERANK_ID}
    try:
        chunks = (requests.post(f"{RAGFLOW}/api/v1/retrieval", headers=HDR, json=body, timeout=120)
                  .json().get("data") or {}).get("chunks", [])
    except Exception as e:
        return PAGE.format(q=html.escape(q), body=f"<p>error: {html.escape(str(e))}</p>")
    if not chunks:
        return PAGE.format(q=html.escape(q), body="<p class=tag>No passages found.</p>")
    out = []
    for c in chunks:
        doc = c.get("document_keyword", "?")
        page = _page_of(c, doc)
        d = html.escape(doc)
        # Prefer the RENDERED PDF PAGE over the reconstructed chunk text (pdftotext output is ugly:
        # re-wrapped, page markers, diagram fragments). Fall back to text only when there's no PDF.
        if _resolve_pdf(doc) and page:
            head = (f'<div class=src>{d} · p.{page} · score {c.get("similarity",0):.2f} — '
                    f'<a href="/view/{d}?p={page}" target=_blank>open ↗</a></div>')
            media = (f'<a href="/view/{d}?p={page}" target=_blank>'
                     f'<img class=pg src="/pageimg/{d}?p={page}" loading=lazy alt="p.{page}"></a>')
            out.append(f'<div class=hit>{head}{media}</div>')
        else:
            text = html.escape((c.get("content_with_weight") or c.get("content", "")).strip()[:1200])
            link = f' — <a href="/pdf/{d}" target=_blank>open PDF ↗</a>' if _resolve_pdf(doc) else ""
            out.append(f'<div class=hit><div class=src>{d}'
                       f'{f" · p.{page}" if page else ""} · score {c.get("similarity",0):.2f}{link}</div>'
                       f'<div class=body>{text}</div></div>')
    return PAGE.format(q=html.escape(q), body="".join(out))


def _render_page(pdf: Path, page: int, dpi: int = 130) -> bytes | None:
    """Render one PDF page to PNG (pdftoppm). Reliable landing, no browser-viewer dependency."""
    try:
        # No output prefix -> single-page PNG streamed to stdout. (`-singlefile`, and an explicit
        # "-" arg, both break stdout in this poppler build: the former yields 0 bytes, the latter
        # writes a file literally named "-".)
        r = subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", str(dpi),
                            "-png", str(pdf)],
                           capture_output=True, timeout=30)
        return r.stdout if r.returncode == 0 and r.stdout else None
    except Exception:
        return None


def _pdf_pages(pdf: Path) -> int:
    try:
        out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=15).stdout
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


VIEW = """<!doctype html><meta charset=utf-8><title>{doc} p.{p}</title>
<style>body{{font:14px system-ui;margin:0;background:#333;color:#eee;text-align:center}}
.bar{{position:sticky;top:0;background:#222;padding:.5rem;display:flex;gap:1rem;justify-content:center;align-items:center}}
.bar a{{color:#8cf;text-decoration:none;padding:.2rem .6rem;border:1px solid #555;border-radius:4px}}
.bar a.off{{opacity:.3;pointer-events:none}}img{{max-width:100%;margin:1rem auto;display:block;background:#fff;box-shadow:0 2px 12px #0008}}
.dl{{color:#9c9}}</style>
<div class=bar>
<a class="{prevoff}" href="/view/{doc}?p={prev}">← p.{prev}</a>
<b>{doc} — page {p} / {total}</b>
<a class="{nextoff}" href="/view/{doc}?p={next}">p.{next} →</a>
<a class=dl href="/pdf/{doc}" target=_blank>full PDF ↧</a>
</div>
<img src="/pageimg/{doc}?p={p}" alt="page {p}">"""


@app.get("/view/{docname}", response_class=HTMLResponse)
def view(docname: str, p: int = 1):
    pdf = _resolve_pdf(docname)
    if not pdf:
        return HTMLResponse(f"no source PDF for {html.escape(docname)}", status_code=404)
    total = _pdf_pages(pdf) or p
    p = max(1, min(p, total))
    return VIEW.format(doc=html.escape(docname), p=p, total=total,
                       prev=max(1, p - 1), next=min(total, p + 1),
                       prevoff="off" if p <= 1 else "", nextoff="off" if p >= total else "")


@app.get("/pageimg/{docname}")
def pageimg(docname: str, p: int = 1):
    pdf = _resolve_pdf(docname)
    if not pdf:
        return PlainTextResponse("no pdf", status_code=404)
    total = _pdf_pages(pdf) or p
    png = _render_page(pdf, max(1, min(p, total)))
    if not png:
        return PlainTextResponse(f"could not render page {p} (pdf has {total} pages)", status_code=404)
    return Response(png, media_type="image/png")


@app.get("/pdf/{docname}")
def pdf(docname: str):
    p = _resolve_pdf(docname)
    if not p:
        return PlainTextResponse(f"no source PDF found for {docname}", status_code=404)
    return FileResponse(p, media_type="application/pdf", filename=p.name)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
