#!/usr/bin/env python3
"""Oracle corpus browser — search the corpus, then OPEN THE SOURCE PDF at the cited page.

The point (a plane must-have): a grounded answer is only trustworthy if you can VERIFY it against the
original. ask_corpus/search_corpus give you the passage; this gives you the passage *and* a one-click
jump into the real PDF at the exact page, offline. The page comes from DeepDoc's stored positions
(PDF-parsed KBs) or the [[p.N]] markers we carry through pdftotext/OCR (text KBs).

    uv run --with fastapi --with uvicorn --with requests python oracle-browser.py   # -> http://localhost:9765
"""
import html
import json
import os
import re
import subprocess
import urllib.parse
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


def _resolve_md(docname: str) -> Path | None:
    """The source .md on disk (page-less docs render as markdown, not a PDF page)."""
    rel = docname.replace("__", "/")
    p = (CORPUS / rel).resolve()
    return p if p.suffix.lower() == ".md" and p.exists() and str(p).startswith(str(CORPUS)) else None


def _md_title(docname: str, md: Path | None = None) -> str:
    """A HUMAN name for a markdown doc — the ingested slug (…___index.md) is meaningless. Prefer the
    front-matter `title:`, then the first H1, then a humanized breadcrumb of the path."""
    md = md or _resolve_md(docname)
    if md:
        head = md.read_text(encoding="utf-8", errors="replace")[:2000]
        m = (re.search(r'(?mi)^title:\s*["\']?(.+?)["\']?\s*$', head)
             or re.search(r"(?m)^#{1,2}\s+(.+?)\s*$", head))
        if m and m.group(1).strip():
            title = m.group(1).strip()
            section = docname.replace("__", "/").split("/")[0]   # top corpus folder, e.g. kubernetes
            return f"{title} · {section}" if section and section.lower() not in title.lower() else title
    rel = docname.replace("__", "/")
    for suf in ("/_index.md", "/index.md", ".md"):
        if rel.endswith(suf):
            rel = rel[: -len(suf)]
            break
    return " › ".join(part.replace("-", " ") for part in rel.split("/") if part)


# ---- corpus tree browsing (folds in the miniserve folder view) ---------------------------------
def _safe_rel(relpath: str) -> Path | None:
    """Resolve a corpus-relative path, refusing anything that escapes CORPUS."""
    p = (CORPUS / relpath).resolve()
    return p if str(p) == str(CORPUS) or str(p).startswith(str(CORPUS) + os.sep) else None


def _slug(relpath: str) -> str:
    return relpath.replace("/", "__")


def _dir_index_doc(reldir: str) -> str | None:
    """A directory's landing markdown (_index.md / index.md), as a doc slug, if present."""
    d = _safe_rel(reldir)
    if d and d.is_dir():
        for cand in ("_index.md", "index.md", "README.md"):
            if (d / cand).exists():
                return _slug(f"{reldir}/{cand}".lstrip("/"))
    return None


def _open_url(relpath: str, q: str = "") -> str:
    """The right viewer URL for a corpus file: PDF page, GH-markdown, or raw."""
    qq = f"?q={urllib.parse.quote(q)}" if q else ""
    ext = Path(relpath).suffix.lower()
    slug = urllib.parse.quote(_slug(relpath))
    if ext == ".pdf":
        return f"/view/{slug}{qq}"
    if ext == ".md":
        return f"/md/{slug}{qq}"
    if ext == ".txt" and _resolve_pdf(_slug(relpath)):   # scanned book: open its PDF
        return f"/view/{slug}{qq}"
    return f"/raw/{urllib.parse.quote(relpath)}"


def _children(reldir: str):
    """(subdirs, files) names under a corpus dir, dirs and files each sorted, hidden ones skipped."""
    d = _safe_rel(reldir)
    if not d or not d.is_dir():
        return [], []
    dirs, files = [], []
    for e in sorted(d.iterdir(), key=lambda p: p.name.lower()):
        if e.name.startswith("."):
            continue
        (dirs if e.is_dir() else files).append(e.name)
    return dirs, files


def _md_sidebar(docname: str, q: str) -> str:
    """A left nav for the reading view: breadcrumb of ancestors + the current directory's contents,
    so you can keep reading across the doc tree. Scoped to one directory (the trees run to thousands
    of files — a full dump would be unusable); folders link deeper."""
    rel = docname.replace("__", "/")
    reldir = str(Path(rel).parent) if "/" in rel else ""
    reldir = "" if reldir == "." else reldir
    qq = f"?q={urllib.parse.quote(q)}" if q else ""

    # breadcrumb: corpus / a / b  (each ancestor -> its index doc if any, else /browse)
    crumbs = ['<a href="/browse">corpus</a>']
    acc = ""
    for part in [p for p in reldir.split("/") if p]:
        acc = f"{acc}/{part}".lstrip("/")
        idoc = _dir_index_doc(acc)
        href = f"/md/{urllib.parse.quote(idoc)}{qq}" if idoc else f"/browse/{urllib.parse.quote(acc)}"
        crumbs.append(f'<a href="{href}">{html.escape(part)}</a>')
    bc = '<div class=crumb>' + ' › '.join(crumbs) + '</div>'

    dirs, files = _children(reldir)
    items = []
    for sub in dirs:
        subrel = f"{reldir}/{sub}".lstrip("/")
        idoc = _dir_index_doc(subrel)
        href = f"/md/{urllib.parse.quote(idoc)}{qq}" if idoc else f"/browse/{urllib.parse.quote(subrel)}"
        items.append(f'<a class=dir href="{href}">📁 {html.escape(sub)}</a>')
    for f in files:
        if not f.lower().endswith(".md"):
            continue
        frel = f"{reldir}/{f}".lstrip("/")
        cur = "cur" if _slug(frel) == docname else ""
        label = _md_title(_slug(frel)) if f in ("_index.md", "index.md") else f[:-3].replace("-", " ")
        items.append(f'<a class="doc {cur}" href="/md/{urllib.parse.quote(_slug(frel))}{qq}">'
                     f'{html.escape(label)}</a>')
    return bc + '<div class=items>' + "".join(items) + '</div>'


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
        # Anchor words are LETTER runs (Cyrillic+Latin); the separator between them must match any
        # run of NON-letters — spaces, punctuation AND digits. The old `\W+` didn't span digits, so
        # a chunk like "кластера.\n 4. Перед тем…" never matched its source and fell back to text.
        letters = re.findall(r"[^\W\d_]{3,}", content, re.UNICODE)
        sep = r"[\W\d_]+"
        idx = -1
        # Try a couple of windows: the head, then a mid-chunk window (list numbers / running heads
        # cluster at the start, so a later window is often more distinctive).
        for window in (letters[:6], letters[4:10], letters[8:14]):
            if len(window) >= 3:
                m = re.search(sep.join(re.escape(w) for w in window), body)
                if m:
                    idx = m.start()
                    break
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


# ---- query-term highlighting on the rendered page ----------------------------------------------
# We highlight the ANCHOR NOUNS of the query on the page image, so a reader's eye lands where the
# match is. "какие виды мышей ты знаешь" must light up вид- and мыш- on the page — which means (a)
# dropping the conversational scaffolding (какие/ты/знаешь) and (b) matching by STEM, because the
# query says "мышей" but the page says "мышь"/"мыши". We stem to an invariant prefix and match any
# page word that starts with it. Cyrillic-aware, deliberately loose: over-highlighting a related
# word is fine; the point is to guide the eye, not to be a search engine.
_STOP = {
    # RU scaffolding: interrogatives, pronouns, the "do you know" frame, prepositions/conjunctions
    "какой", "какая", "какое", "какие", "каких", "что", "кто", "это", "этот", "эта", "как", "где",
    "ты", "вы", "мне", "меня", "мы", "они", "он", "она", "оно", "знаешь", "знаете", "знать", "есть",
    "бывают", "бывает", "такое", "такие", "и", "в", "во", "на", "по", "за", "из", "от", "для", "или",
    "про", "об", "о", "с", "со", "у", "к", "не", "ли", "же", "бы", "но", "а", "все", "всех",
    # EN scaffolding
    "what", "which", "who", "how", "the", "and", "for", "are", "is", "of", "to", "in", "on", "do",
    "does", "you", "know", "list", "kinds", "types", "there", "any",
}
_RU_SUF = ("иями", "ями", "ами", "ыми", "ими", "ого", "его", "ому", "ему", "ов", "ев", "ей", "ам",
           "ям", "ах", "ях", "ые", "ий", "ый", "ой", "ая", "яя", "ое", "ее", "ю", "я", "ы", "и",
           "а", "у", "е", "ь", "о")
_EN_SUF = ("ies", "ing", "es", "ed", "s")


def _stem(w: str) -> str:
    """Strip one inflectional suffix to an invariant prefix (>=3 chars). Crude but language-robust."""
    for suf in _RU_SUF + _EN_SUF:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def _anchor_terms(query: str) -> list[str]:
    """Content stems of the query — scaffolding and short words removed."""
    out = []
    for w in re.findall(r"[^\W\d_]{3,}", query.lower(), re.UNICODE):
        if w in _STOP:
            continue
        s = _stem(w)
        if len(s) >= 3 and s not in out:
            out.append(s)
    return out


_BBOX_CACHE: dict[tuple[str, int], tuple[float, float, list]] = {}


def _bbox_words(pdf: Path, page: int):
    """(page_w, page_h, [(xMin,yMin,xMax,yMax,word)]) for one page, via `pdftotext -bbox`.

    Coordinates are PDF points; pdftoppm renders at N dpi, so a box as a FRACTION of page size
    (xMin/page_w …) maps directly onto the width:100% <img> with no dpi bookkeeping."""
    key = (str(pdf), page)
    if key in _BBOX_CACHE:
        return _BBOX_CACHE[key]
    try:
        xml = subprocess.run(["pdftotext", "-bbox", "-f", str(page), "-l", str(page), str(pdf), "-"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        xml = ""
    pm = re.search(r'<page width="([\d.]+)" height="([\d.]+)"', xml)
    words = []
    if pm:
        for m in re.finditer(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">'
                             r'([^<]*)</word>', xml):
            x0, y0, x1, y1 = (float(m.group(i)) for i in range(1, 5))
            words.append((x0, y0, x1, y1, html.unescape(m.group(5)).lower()))
        res = (float(pm.group(1)), float(pm.group(2)), words)
    else:
        res = (0.0, 0.0, [])
    _BBOX_CACHE[key] = res
    return res


def _hl_boxes(pdf: Path, page: int, terms: list[str]):
    """Highlight rectangles as (left%, top%, width%, height%) for words matching any anchor stem."""
    if not terms:
        return []
    pw, ph, words = _bbox_words(pdf, page)
    if not pw or not ph:
        return []
    out = []
    for x0, y0, x1, y1, w in words:
        if any(w.startswith(t) for t in terms):
            out.append((100 * x0 / pw, 100 * y0 / ph, 100 * (x1 - x0) / pw, 100 * (y1 - y0) / ph))
    return out


def _overlays(pdf: Path | None, page: int | None, query: str) -> str:
    if not pdf or not page:
        return ""
    spans = ""
    for left, top, w, h in _hl_boxes(pdf, page, _anchor_terms(query)):
        spans += (f'<span class=hl style="left:{left:.2f}%;top:{top:.2f}%;'
                  f'width:{w:.2f}%;height:{h:.2f}%"></span>')
    return spans


_TAG_OR_ENT = re.compile(r"<[^>]+>|&[#\w]+;")


def _mark_terms(fragment: str, terms: list[str]) -> str:
    """Wrap anchor-stem matches in <mark>, in the TEXT of an HTML fragment only — never inside a
    <tag> or an &entity; (so a link's href and escaped chars stay intact). This gives the text/
    markdown results the same eye-guide the rendered PDF pages get from the overlay boxes."""
    if not terms:
        return fragment

    def mark_words(seg: str) -> str:
        return re.sub(r"[^\W\d_]{3,}",
                      lambda m: (f"<mark>{m.group(0)}</mark>"
                                 if any(m.group(0).lower().startswith(t) for t in terms)
                                 else m.group(0)),
                      seg, flags=re.UNICODE)

    out, last = [], 0
    for m in _TAG_OR_ENT.finditer(fragment):
        out.append(mark_words(fragment[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(mark_words(fragment[last:]))
    return "".join(out)


_MD_EXT = ["fenced_code", "tables", "sane_lists", "toc", "attr_list"]


def _locate_in_md(raw: str, find: str) -> int:
    """Char offset in raw markdown where a chunk's text starts (letter-run probe), or -1."""
    words = re.findall(r"[^\W\d_]{3,}", find, re.UNICODE)[:6]
    if len(words) >= 3:
        m = re.search(r"[\W\d_]+".join(re.escape(w) for w in words), raw)
        if m:
            return m.start()
    return -1


def _strip_frontmatter(raw: str) -> str:
    """Drop a leading Hugo/Jekyll front-matter block (--- YAML --- or +++ TOML +++) — it is metadata,
    not prose, and markdown renderers dump it verbatim at the top of the page."""
    return re.sub(r"\A(?:---|\+\+\+)\r?\n.*?\r?\n(?:---|\+\+\+)\r?\n", "", raw, count=1, flags=re.S)


def _render_md(raw: str, query: str, find: str = "") -> str:
    """Raw markdown -> GitHub-flavored HTML, query terms highlighted, with a scroll anchor injected
    at the block that matches `find` (so /md?...#hit lands on the retrieved passage)."""
    import markdown
    raw = _strip_frontmatter(raw)
    if find:
        idx = _locate_in_md(raw, find)
        if idx >= 0:
            ls = raw.rfind("\n", 0, idx) + 1          # start of the line the hit is on
            raw = raw[:ls] + '\n<a id="hit"></a>\n\n' + raw[ls:]   # block-level HTML passes through
    rendered = markdown.markdown(raw, extensions=_MD_EXT)
    return _mark_terms(rendered, _anchor_terms(query))


PAGE = """<!doctype html><meta charset=utf-8><title>Oracle corpus</title>
<style>body{{font:15px/1.5 system-ui;max-width:760px;margin:2rem auto;padding:0 1rem;background:#faf9f7}}
form{{display:flex;gap:.5rem}}input[name=q]{{flex:1;padding:.5rem}}button{{padding:.5rem 1rem}}
.hit{{border:1px solid #ddd;border-radius:6px;padding:.7rem 1rem;margin:.8rem 0;background:#fff}}
.src{{color:#666;font-size:.85em;margin-bottom:.5rem}}.src a{{color:#06c;text-decoration:none}}.src a:hover{{text-decoration:underline}}
.body{{white-space:pre-wrap;margin-top:.4rem}}img.pg{{width:100%;border:1px solid #eee;box-shadow:0 1px 6px #0002;display:block}}
.pgwrap{{position:relative;display:block}}
.hl{{position:absolute;background:rgba(255,214,0,.38);mix-blend-mode:multiply;border-radius:2px;pointer-events:none}}
/* Make a markdown result READ LIKE A TYPESET PAGE so it sits beside the PDF page images without
   clashing: same white frame/border/shadow, a book serif, page-like margins. */
.md{{margin-top:.3rem;padding:1.7rem 2rem;background:#fff;border:1px solid #eee;box-shadow:0 1px 6px #0002;
     border-radius:2px;font-family:Georgia,"Times New Roman",serif;font-size:1.02em;line-height:1.65;color:#222}}
.md>*:first-child{{margin-top:0}}.md>*:last-child{{margin-bottom:0}}
.md h1,.md h2,.md h3,.md h4{{margin:1.1em 0 .4em;line-height:1.25;font-weight:600;font-family:system-ui,sans-serif}}
.md h1{{font-size:1.4em}}.md h2{{font-size:1.2em}}.md h3{{font-size:1.07em}}
.md p{{margin:.6em 0}}.md ul,.md ol{{margin:.6em 0;padding-left:1.5em}}.md li{{margin:.3em 0}}
.md blockquote{{margin:.6em 0;padding:.1em 1em;border-left:3px solid #ddd;color:#555}}
.md pre{{background:#f6f5f2;padding:.7rem;border-radius:4px;overflow-x:auto;line-height:1.45;font-size:.88em}}
.md code{{background:#f0efec;padding:.05rem .3rem;border-radius:3px;font-size:.9em;font-family:ui-monospace,monospace}}
.md pre code{{background:none;padding:0}}.md table{{border-collapse:collapse;margin:.6em 0}}
.md td,.md th{{border:1px solid #ddd;padding:.25rem .6rem}}.md a{{color:#06c}}.md img{{max-width:100%}}
.md hr{{border:0;border-top:1px solid #e5e5e5;margin:1em 0}}
mark{{background:#fe9}}.tag{{color:#999;font-size:.8em}}</style>
<h2>🔮 Oracle corpus browser · <a href="/browse" style="font-size:.6em;color:#06c;text-decoration:none">▤ browse tree</a></h2>
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
        d = urllib.parse.quote(doc)
        pdf = _resolve_pdf(doc)
        sim = c.get("similarity", 0)
        # Prefer the RENDERED PDF PAGE over the reconstructed chunk text (pdftotext output is ugly:
        # re-wrapped, page markers, diagram fragments). Fall back to text only when there's no PDF.
        if pdf and page:
            # Header shows the REAL source PDF name, not the ingested `<subdir>__<file>.txt` slug.
            name = html.escape(pdf.name)
            qq = urllib.parse.quote(q)
            view = f"/view/{d}?p={page}&q={qq}"
            head = (f'<div class=src><b>{name}</b> · p.{page} · score {sim:.2f} — '
                    f'<a href="{view}" target=_blank>open ↗</a></div>')
            media = (f'<a class=pgwrap href="{view}" target=_blank>'
                     f'<img class=pg src="/pageimg/{d}?p={page}" loading=lazy alt="p.{page}">'
                     f'{_overlays(pdf, page, q)}</a>')
            out.append(f'<div class=hit>{head}{media}</div>')
        else:
            raw = (c.get("content_with_weight") or c.get("content", "")).strip()
            terms = _anchor_terms(q)
            qq = urllib.parse.quote(q)
            # Page-less docs (markdown) have no PDF to render — show the content, but for .md render
            # it AS markdown instead of dumping the raw source with its link syntax and fences.
            if doc.endswith(".md"):
                body_html = f'<div class=md>{_render_md(raw[:6000], q)}</div>'
                # link to the FULL doc, GitHub-rendered + scrolled to this passage (#hit)
                find = urllib.parse.quote(" ".join(re.findall(r"[^\W\d_]{3,}", raw, re.UNICODE)[:6]))
                link = f' — <a href="/md/{d}?q={qq}&find={find}#hit" target=_blank>open ↗</a>'
                name = html.escape(_md_title(doc))
            else:
                body_html = f'<div class=body>{_mark_terms(html.escape(raw[:1200]), terms)}</div>'
                name = html.escape(pdf.name) if pdf else html.escape(doc)
                link = f' — <a href="/pdf/{d}" target=_blank>open PDF ↗</a>' if pdf else ""
            out.append(f'<div class=hit><div class=src><b>{name}</b>'
                       f'{f" · p.{page}" if page else ""} · score {sim:.2f}{link}</div>'
                       f'{body_html}</div>')
    return PAGE.format(q=html.escape(q), body="".join(out))


def _render_page(pdf: Path, page: int, dpi: int = 200) -> bytes | None:
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


# The viewer is a small client app: arrow keys / buttons swap the page IMAGE in place (no full
# reload → no flash), and neighbouring pages are PRECACHED so the swap is instant. Highlight boxes
# come from /pagemeta as JSON and are redrawn per page. `/*CFG*/` is filled with the page's config
# (kept out of the JS braces so we don't have to escape the whole script for str.format).
VIEW = """<!doctype html><meta charset=utf-8><title>corpus viewer</title>
<style>body{font:14px system-ui;margin:0;background:#333;color:#eee;text-align:center}
.bar{position:sticky;top:0;z-index:5;background:#222;padding:.5rem;display:flex;gap:1rem;justify-content:center;align-items:center}
.bar a,.bar button{color:#8cf;background:none;font:inherit;cursor:pointer;text-decoration:none;padding:.2rem .6rem;border:1px solid #555;border-radius:4px}
.bar button.off{opacity:.3;pointer-events:none}.bar a{color:#9c9}
.pgwrap{position:relative;display:inline-block;margin:1rem auto;min-height:60vh}
.pgwrap img{max-width:100%;display:block;background:#fff;box-shadow:0 2px 12px #0008}
.hl{position:absolute;background:rgba(255,214,0,.42);mix-blend-mode:multiply;border-radius:2px;pointer-events:none}</style>
<div class=bar>
<button id=prev>← prev</button><b id=lbl></b><button id=next>next →</button>
<a id=dl target=_blank>full PDF ↧</a>
</div>
<div class=pgwrap id=wrap><img id=pg alt=page></div>
<script>
const C=/*CFG*/;
const img=document.getElementById('pg'),wrap=document.getElementById('wrap'),lbl=document.getElementById('lbl'),
      prev=document.getElementById('prev'),next=document.getElementById('next');
document.getElementById('dl').href='/pdf/'+C.doc;
const imgUrl=p=>`/pageimg/${C.doc}?p=${p}`, metaUrl=p=>`/pagemeta/${C.doc}?p=${p}&q=${C.q}`;
const metaCache={};
const getMeta=async p=>metaCache[p]??(metaCache[p]=fetch(metaUrl(p)).then(r=>r.json()).catch(()=>({boxes:[]})));
function draw(boxes){wrap.querySelectorAll('.hl').forEach(e=>e.remove());
  for(const b of boxes||[]){const s=document.createElement('span');s.className='hl';
    s.style.cssText=`left:${b[0]}%;top:${b[1]}%;width:${b[2]}%;height:${b[3]}%`;wrap.appendChild(s);}}
function preload(p){if(p>=1&&p<=C.total){const i=new Image();i.src=imgUrl(p);getMeta(p);}}
let cur=0;
async function show(p){
  p=Math.max(1,Math.min(C.total,p));cur=p;
  const pre=new Image();pre.src=imgUrl(p);try{await pre.decode()}catch(e){}   // decode before swap → no flash
  if(cur!==p)return;                                                          // a newer nav superseded us
  img.src=imgUrl(p);draw((await getMeta(p)).boxes);
  lbl.textContent=`${C.name} — page ${p} / ${C.total}`;document.title=`${C.name} p.${p}`;
  prev.classList.toggle('off',p<=1);next.classList.toggle('off',p>=C.total);
  history.replaceState(null,'',`/view/${C.doc}?p=${p}&q=${C.q}`);
  [1,-1,2,-2,3].forEach(d=>preload(p+d));
}
prev.onclick=()=>show(cur-1);next.onclick=()=>show(cur+1);
addEventListener('keydown',e=>{if(e.key==='ArrowLeft'){e.preventDefault();show(cur-1)}
  else if(e.key==='ArrowRight'){e.preventDefault();show(cur+1)}});
show(C.start);
</script>"""


@app.get("/view/{docname}", response_class=HTMLResponse)
def view(docname: str, p: int = 1, q: str = ""):
    pdf = _resolve_pdf(docname)
    if not pdf:
        return HTMLResponse(f"no source PDF for {html.escape(docname)}", status_code=404)
    total = _pdf_pages(pdf) or p
    cfg = json.dumps({"doc": urllib.parse.quote(docname), "name": pdf.name, "total": total,
                      "start": max(1, min(p, total)), "q": urllib.parse.quote(q)})
    return HTMLResponse(VIEW.replace("/*CFG*/", cfg))


@app.get("/pagemeta/{docname}")
def pagemeta(docname: str, p: int = 1, q: str = ""):
    """Highlight boxes for one page as JSON, so the client can redraw overlays on an in-place page
    swap without a round-trip to re-render HTML."""
    pdf = _resolve_pdf(docname)
    if not pdf:
        return Response(json.dumps({"boxes": []}), media_type="application/json")
    total = _pdf_pages(pdf) or p
    boxes = [[round(l, 2), round(t, 2), round(w, 2), round(h, 2)]
             for l, t, w, h in _hl_boxes(pdf, max(1, min(p, total)), _anchor_terms(q))]
    return Response(json.dumps({"p": p, "total": total, "boxes": boxes}),
                    media_type="application/json")


MDPAGE = """<!doctype html><meta charset=utf-8><title>{name}</title>
<style>body{{margin:0;background:#f0efec;font:14px system-ui}}
.bar{{position:sticky;top:0;z-index:5;background:#222;color:#eee;padding:.5rem 1rem;display:flex;gap:1rem;align-items:center}}
.bar a{{color:#8cf;text-decoration:none}}.bar b{{font-weight:600}}
.layout{{display:flex;align-items:flex-start}}
.tree{{position:sticky;top:41px;width:280px;flex:none;max-height:calc(100vh - 41px);overflow:auto;
padding:1rem .8rem;background:#f7f6f3;border-right:1px solid #e2e2e2;font-size:13px}}
.tree .crumb{{color:#888;margin-bottom:.6rem;font-size:12px}}.tree .crumb a{{color:#478;text-decoration:none}}
.tree .items{{display:flex;flex-direction:column}}.tree a{{display:block;padding:.22rem .4rem;border-radius:4px;
text-decoration:none;color:#245;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.tree a:hover{{background:#eceae4}}.tree a.dir{{color:#555}}.tree a.cur{{background:#ffe9a8;font-weight:600;color:#000}}
.main{{flex:1;min-width:0;display:flex;justify-content:center}}
.doc{{max-width:820px;width:100%;margin:1.5rem;padding:2.5rem 3rem;background:#fff;border:1px solid #e2e2e2;box-sizing:border-box;
box-shadow:0 2px 14px #00000014;border-radius:2px;font:16px/1.65 Georgia,"Times New Roman",serif;color:#1a1a1a}}
.doc h1,.doc h2,.doc h3,.doc h4{{font-family:system-ui,sans-serif;line-height:1.25;margin:1.3em 0 .5em;font-weight:600}}
.doc h1{{font-size:1.7em;border-bottom:1px solid #eaeaea;padding-bottom:.25em}}
.doc h2{{font-size:1.35em;border-bottom:1px solid #eaeaea;padding-bottom:.2em}}.doc h3{{font-size:1.15em}}
.doc p{{margin:.7em 0}}.doc ul,.doc ol{{padding-left:1.6em}}.doc li{{margin:.3em 0}}
.doc a{{color:#06c}}.doc code{{background:#f0efec;padding:.1rem .35rem;border-radius:3px;font:.88em ui-monospace,monospace}}
.doc pre{{background:#f6f5f2;padding:.8rem 1rem;border-radius:5px;overflow-x:auto}}.doc pre code{{background:none;padding:0}}
.doc blockquote{{margin:.8em 0;padding:.2em 1em;border-left:4px solid #ddd;color:#555}}
.doc table{{border-collapse:collapse;margin:.8em 0}}.doc td,.doc th{{border:1px solid #ddd;padding:.3rem .7rem}}
.doc img{{max-width:100%}}.doc hr{{border:0;border-top:1px solid #e5e5e5;margin:1.5em 0}}
#hit{{scroll-margin-top:70px}}mark{{background:#fe9}}
:target+*,#hit+*{{animation:flash 2.5s ease-out}}@keyframes flash{{from{{background:#fff6cf}}to{{background:transparent}}}}</style>
<div class=bar><a href="/search?q={q}">← results</a><a href="/browse">▤ browse</a><b>{name}</b></div>
<div class=layout><nav class=tree>{tree}</nav><div class=main><div class=doc>{body}</div></div></div>
<script>if(location.hash==='#hit'){{document.getElementById('hit')?.scrollIntoView()}}
document.querySelector('.tree a.cur')?.scrollIntoView({{block:'center'}});</script>"""


@app.get("/md/{docname}", response_class=HTMLResponse)
def mdview(docname: str, q: str = "", find: str = ""):
    md = _resolve_md(docname)
    if not md:
        return HTMLResponse(f"no source markdown for {html.escape(docname)}", status_code=404)
    raw = md.read_text(encoding="utf-8", errors="replace")
    return MDPAGE.format(name=html.escape(_md_title(docname, md)), q=urllib.parse.quote(q),
                         tree=_md_sidebar(docname, q), body=_render_md(raw, q, find))


BROWSE = """<!doctype html><meta charset=utf-8><title>corpus: /{rel}</title>
<style>body{{font:15px/1.5 system-ui;max-width:900px;margin:2rem auto;padding:0 1rem;background:#faf9f7}}
.crumb{{color:#888;margin-bottom:1rem}}.crumb a{{color:#06c;text-decoration:none}}
a.row{{display:flex;gap:.6rem;padding:.35rem .5rem;border-radius:5px;text-decoration:none;color:#234}}
a.row:hover{{background:#efeee9}}.sz{{margin-left:auto;color:#aaa;font-size:.85em}}
form{{margin:.5rem 0 1.5rem}}input{{padding:.45rem;width:60%}}button{{padding:.45rem .9rem}}</style>
<form action=/search><input name=q placeholder="search the corpus…"><button>search</button></form>
<div class=crumb>{crumb}</div>{rows}"""


@app.get("/browse", response_class=HTMLResponse)
@app.get("/browse/{relpath:path}", response_class=HTMLResponse)
def browse(relpath: str = "", q: str = ""):
    d = _safe_rel(relpath)
    if not d or not d.is_dir():
        return HTMLResponse(f"no such corpus dir: {html.escape(relpath)}", status_code=404)
    # breadcrumb
    crumbs, acc = ['<a href="/browse">corpus</a>'], ""
    for part in [p for p in relpath.split("/") if p]:
        acc = f"{acc}/{part}".lstrip("/")
        crumbs.append(f'<a href="/browse/{urllib.parse.quote(acc)}">{html.escape(part)}</a>')
    dirs, files = _children(relpath)
    rows = []
    if relpath:
        parent = str(Path(relpath).parent)
        parent = "" if parent == "." else parent
        rows.append(f'<a class=row href="/browse/{urllib.parse.quote(parent)}">⬆️ ..</a>')
    for sub in dirs:
        subrel = f"{relpath}/{sub}".lstrip("/")
        rows.append(f'<a class=row href="/browse/{urllib.parse.quote(subrel)}">📁 {html.escape(sub)}/</a>')
    for f in files:
        frel = f"{relpath}/{f}".lstrip("/")
        p = _safe_rel(frel)
        sz = f'<span class=sz>{p.stat().st_size // 1024} KB</span>' if p else ""
        icon = {".pdf": "📕", ".md": "📄", ".txt": "📃"}.get(Path(f).suffix.lower(), "📎")
        rows.append(f'<a class=row href="{_open_url(frel, q)}" target=_blank>{icon} {html.escape(f)}{sz}</a>')
    return BROWSE.format(rel=html.escape(relpath), crumb=" › ".join(crumbs), rows="".join(rows))


@app.get("/raw/{relpath:path}")
def raw(relpath: str):
    p = _safe_rel(relpath)
    if not p or not p.is_file():
        return PlainTextResponse(f"no such file: {relpath}", status_code=404)
    return FileResponse(p, filename=p.name)


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
