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
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

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


def _page_of(chunk: dict) -> int | None:
    pos = chunk.get("positions") or []
    if pos and pos[0] and pos[0][0] and pos[0][0] > 1:  # DeepDoc: [page, x0, x1, top, bottom]
        return int(pos[0][0])
    m = re.search(r"\[\[p\.(\d+)\]\]", chunk.get("content_with_weight") or chunk.get("content", ""))
    return int(m.group(1)) if m else None                 # text KBs: [[p.N]] marker


PAGE = """<!doctype html><meta charset=utf-8><title>Oracle corpus</title>
<style>body{{font:15px/1.5 system-ui;max-width:900px;margin:2rem auto;padding:0 1rem;background:#faf9f7}}
form{{display:flex;gap:.5rem}}input[name=q]{{flex:1;padding:.5rem}}button{{padding:.5rem 1rem}}
.hit{{border:1px solid #ddd;border-radius:6px;padding:.7rem 1rem;margin:.8rem 0;background:#fff}}
.src{{color:#666;font-size:.85em}}.src a{{color:#06c;text-decoration:none}}.src a:hover{{text-decoration:underline}}
.body{{white-space:pre-wrap;margin-top:.4rem}}mark{{background:#fe9}}.tag{{color:#999;font-size:.8em}}</style>
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
        page = _page_of(c)
        text = html.escape((c.get("content_with_weight") or c.get("content", "")).strip()[:1200])
        link = ""
        if _resolve_pdf(doc):
            anchor = f"#page={page}" if page else ""
            link = f' — <a href="/pdf/{html.escape(doc)}{anchor}" target=_blank>open PDF{f" p.{page}" if page else ""} ↗</a>'
        out.append(f'<div class=hit><div class=src>{html.escape(doc)}'
                   f'{f" · p.{page}" if page else ""} · score {c.get("similarity",0):.2f}{link}</div>'
                   f'<div class=body>{text}</div></div>')
    return PAGE.format(q=html.escape(q), body="".join(out))


@app.get("/pdf/{docname}")
def pdf(docname: str):
    p = _resolve_pdf(docname)
    if not p:
        return PlainTextResponse(f"no source PDF found for {docname}", status_code=404)
    return FileResponse(p, media_type="application/pdf", filename=p.name)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
