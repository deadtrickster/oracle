# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0", "requests"]
# ///
"""Oracle ingestion MCP server — lets a RAGFlow agent do what we did by hand:
point it at a folder / PDF / URL and it classifies, converts, routes to the right
knowledge base with the right chunk method, and parses.

Division of labour: these tools do the MECHANICS and surface CLASSIFICATION SIGNALS
(type, language, page count, paper-vs-book heuristics, sample text). The agent (qwen)
makes the routing JUDGMENT (which dataset, which chunk method) from those signals.

Tools:
  list_datasets()                     existing KBs + chunk methods
  list_folder(path)                   enumerate a folder (under allowed roots)
  inspect(path)                       type/lang/pages/title/sample + paper-vs-book hint
  fetch_url(url)                       URL -> clean markdown saved under corpus/inbox
  ingest_file(path, dataset, chunk_method, create_if_missing)
                                      stage into corpus + upload to RAGFlow + parse
  parse_status(dataset)               parse progress for a KB

Run via mcp-proxy (stdio -> SSE) for RAGFlow. Reads confined to allowed roots.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import requests
from mcp.server.fastmcp import FastMCP

RAGFLOW = os.environ.get("ORACLE_RAGFLOW_URL", "http://localhost:9380")
KEY = os.environ.get("ORACLE_RAGFLOW_KEY", "ragflow-smywlJs3drgGxfKztifTmD3iNJ2lP6Uvq2-suiLQTGM")
HOME = Path.home()
CORPUS = Path(os.environ.get("ORACLE_CORPUS", str(HOME / "Projects/oracle/corpus"))).resolve()
ROOTS = [HOME / "Documents", HOME / "Projects", CORPUS, Path("/tmp")]
INBOX = CORPUS / "inbox"
SUPPORTED = {".md", ".txt", ".pdf", ".html", ".htm", ".docx", ".csv", ".json", ".asc"}
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

mcp = FastMCP("oracle-ingest")


def _safe(p: Path) -> Path | None:
    try:
        rp = p.resolve()
    except Exception:
        return None
    return rp if any(str(rp).startswith(str(r.resolve())) for r in ROOTS) else None


def _api(method, path, **kw):
    r = requests.request(method, f"{RAGFLOW}/api/v1{path}", headers=HDR, timeout=120, **kw)
    r.raise_for_status()
    return r.json()


def _detect_lang(text: str) -> str:
    cyr = len(re.findall(r"[Ѐ-ӿ]", text))
    return "RU" if cyr > 30 else "EN/other"


# ---------------------------------------------------------------- tools

@mcp.tool()
def list_datasets() -> str:
    """List existing knowledge bases with their chunk method and doc/chunk counts,
    so you know where content can be routed. Common KBs: rust, rust-api, io_uring,
    linux, go, postgres, emacs, papers (Paper method), books (Book method),
    links (articles/URLs), oracle-meta."""
    ds = _api("GET", "/datasets?page_size=100")["data"]
    rows = [f"{d['name']}\tchunk_method={d['chunk_method']}\tdocs={d['document_count']}\tchunks={d['chunk_count']}"
            for d in sorted(ds, key=lambda x: x["name"])]
    return "Knowledge bases:\n" + "\n".join(rows)


@mcp.tool()
def list_folder(path: str) -> str:
    """List files in a folder under ~/Documents, ~/Projects, or the corpus.
    Use before batch-ingesting a directory."""
    p = _safe(Path(path))
    if p is None or not p.is_dir():
        return f"error: not a directory under allowed roots: {path}"
    files = [f for f in sorted(p.rglob("*")) if f.is_file()]
    out = [f"{f.relative_to(p)}\t{f.stat().st_size // 1024}KB\t{f.suffix or '-'}" for f in files[:200]]
    return f"{len(files)} files under {p}:\n" + "\n".join(out)


@mcp.tool()
def inspect(path: str) -> str:
    """Inspect a local file to classify it: kind, size, page count, LANGUAGE
    (RU vs EN/other — routing matters for the multilingual reranker), title, a text
    sample, and a paper-vs-book HINT for PDFs. Use this before deciding dataset +
    chunk_method. For URLs, call fetch_url first, then inspect the saved path."""
    p = _safe(Path(path))
    if p is None or not p.is_file():
        return f"error: not a file under allowed roots: {path}"
    ext = p.suffix.lower()
    size = p.stat().st_size
    info = [f"path: {p}", f"ext: {ext}", f"size: {size // 1024} KB"]
    if ext == ".pdf":
        try:
            meta = subprocess.run(["pdfinfo", str(p)], capture_output=True, text=True, timeout=20).stdout
            pages = re.search(r"Pages:\s*(\d+)", meta)
            title = re.search(r"Title:\s*(.+)", meta)
            npages = int(pages.group(1)) if pages else 0
            info.append(f"pages: {npages}")
            if title:
                info.append(f"title: {title.group(1).strip()}")
            sample = subprocess.run(["pdftotext", "-f", "1", "-l", "3", str(p), "-"],
                                    capture_output=True, text=True, timeout=30).stdout
            info.append(f"language: {_detect_lang(sample)}")
            low = sample.lower()
            is_paper = npages and npages < 40 and any(k in low for k in ("abstract", "references", "arxiv", "proceedings", "we present", "in this paper"))
            info.append(f"paper_vs_book_hint: {'PAPER (short + academic markers) -> papers KB, chunk_method=paper' if is_paper else 'BOOK/long -> books KB, chunk_method=book (or a topic KB with naive)'}")
            if _detect_lang(sample) == "RU":
                info.append("CYRILLIC WARNING: DeepDoc (book/paper chunk methods) GARBLES Cyrillic "
                            "CID fonts (Новиков -> HOBMKOB). For this PDF you MUST call pdf_to_text "
                            "first, then ingest_file the resulting .txt with chunk_method='naive'. "
                            "Do NOT ingest this PDF directly with book/paper method.")
            info.append("sample: " + re.sub(r"\s+", " ", sample)[:500])
        except Exception as e:
            info.append(f"pdf inspect error: {e}")
    else:
        try:
            text = p.read_text(errors="replace")[:2000]
            info.append(f"language: {_detect_lang(text)}")
            info.append("sample: " + re.sub(r"\s+", " ", text)[:500])
        except Exception as e:
            info.append(f"read error: {e}")
    return "\n".join(info)


@mcp.tool()
def fetch_url(url: str, name: str = "") -> str:
    """Fetch a URL and save it as clean markdown under corpus/inbox, returning the
    saved path (then inspect + ingest it). Handles articles/blogs (trafilatura, then
    r.jina.ai fallback for paywalled/JS pages). For a PDF URL, downloads the PDF.
    `name` optionally sets the output filename stem."""
    INBOX.mkdir(parents=True, exist_ok=True)
    stem = name or re.sub(r"[^a-zA-Z0-9._-]+", "-", url.split("//")[-1])[:80].strip("-")
    stem = re.sub(r"\.(pdf|md|html?|txt)$", "", stem, flags=re.I)  # avoid double extension
    if url.lower().split("?")[0].endswith(".pdf"):
        out = INBOX / f"{stem}.pdf"
        try:
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            out.write_bytes(r.content)
            return f"downloaded PDF -> {out} ({len(r.content)//1024} KB). Now inspect() it."
        except Exception as e:
            return f"error downloading pdf: {e}"
    out = INBOX / f"{stem}.md"
    # trafilatura first
    try:
        r = subprocess.run(["trafilatura", "-u", url, "--markdown"],
                           capture_output=True, text=True, timeout=60)
        if len(r.stdout) > 300:
            out.write_text(r.stdout)
            return f"fetched (trafilatura) -> {out} ({len(r.stdout)} chars). Now inspect() it."
    except Exception:
        pass
    # jina reader fallback
    try:
        r = requests.get(f"https://r.jina.ai/{url}", timeout=90)
        if len(r.text) > 300:
            out.write_text(r.text)
            return f"fetched (jina reader) -> {out} ({len(r.text)} chars). Now inspect() it."
    except Exception as e:
        return f"error fetching url (both methods failed): {e}"
    return "error: could not extract content from url (empty)"


@mcp.tool()
def pdf_to_text(path: str, name: str = "") -> str:
    """Extract a PDF to clean UTF-8 text (pdftotext -layout) under corpus/inbox, and
    return the .txt path. REQUIRED for Cyrillic/Russian (and other non-Latin) PDFs:
    DeepDoc's book/paper parsers garble CID-font Cyrillic, but pdftotext preserves it.
    After this, ingest the .txt with ingest_file(..., chunk_method='naive')."""
    p = _safe(Path(path))
    if p is None or not p.is_file() or p.suffix.lower() != ".pdf":
        return f"error: not a PDF under allowed roots: {path}"
    INBOX.mkdir(parents=True, exist_ok=True)
    out = INBOX / ((name or p.stem) + ".txt")
    try:
        subprocess.run(["pdftotext", "-layout", str(p), str(out)], check=True, timeout=180)
    except Exception as e:
        return f"error running pdftotext: {e}"
    n = sum(1 for _ in out.open(errors="replace"))
    return f"extracted -> {out} ({n} lines). Now ingest_file('{out}', <dataset>, chunk_method='naive')."


@mcp.tool()
def ingest_file(path: str, dataset: str, chunk_method: str = "naive", create_if_missing: bool = True) -> str:
    """Upload a file to a RAGFlow knowledge base and start parsing. Creates the KB if
    missing (with RAPTOR + GraphRAG OFF — they're slow; and the tenant default
    embedding bge-m3). chunk_method: 'naive' (general/docs/articles), 'book' (long
    books PDF/txt — NOT .md, which the Book parser rejects), 'paper' (2-column
    academic PDFs). The file is also copied into corpus/inbox for the reading browser.
    Returns the document id + parse-start status."""
    p = _safe(Path(path))
    if p is None or not p.is_file():
        return f"error: not a file under allowed roots: {path}"
    ext = p.suffix.lower()
    # Book/Paper parsers reject .md; map to .txt name on upload
    upload_name = p.name
    if ext not in SUPPORTED or (chunk_method in ("book", "paper") and ext == ".md"):
        upload_name = p.name + ".txt"

    existing = {d["name"]: d for d in _api("GET", "/datasets?page_size=100")["data"]}
    if dataset in existing:
        ds = existing[dataset]
    elif create_if_missing:
        ds = _api("POST", "/datasets", json={"name": dataset, "chunk_method": chunk_method,
                  "parser_config": {"raptor": {"use_raptor": False}, "graphrag": {"use_graphrag": False}}})["data"]
    else:
        return f"error: dataset '{dataset}' does not exist and create_if_missing=False"
    dsid = ds["id"]

    with p.open("rb") as fh:
        r = requests.post(f"{RAGFLOW}/api/v1/datasets/{dsid}/documents",
                          headers={"Authorization": f"Bearer {KEY}"},
                          files=[("file", (upload_name, fh))], timeout=300)
    r.raise_for_status()
    data = r.json().get("data", [])
    if not data:
        return f"upload returned no doc (maybe duplicate name '{upload_name}' already in '{dataset}')"
    doc_id = data[0]["id"]
    _api("POST", f"/datasets/{dsid}/chunks", json={"document_ids": [doc_id]})
    # copy into corpus/inbox for the reading browser (best-effort)
    try:
        INBOX.mkdir(parents=True, exist_ok=True)
        if _safe(p) and not str(p).startswith(str(INBOX)):
            shutil.copy2(p, INBOX / upload_name)
    except Exception:
        pass
    return f"OK: uploaded '{upload_name}' to '{dataset}' (chunk_method={ds['chunk_method']}), doc_id={doc_id}, parsing started."


@mcp.tool()
def parse_status(dataset: str) -> str:
    """Report parse progress for a KB: how many docs DONE / RUNNING / FAIL and total
    chunks. Use to confirm ingestion finished or find failures to re-queue."""
    existing = {d["name"]: d for d in _api("GET", "/datasets?page_size=100")["data"]}
    if dataset not in existing:
        return f"error: no such dataset '{dataset}'"
    ds = existing[dataset]
    from collections import Counter
    st, page = Counter(), 1
    while True:
        docs = _api("GET", f"/datasets/{ds['id']}/documents?page={page}&page_size=100")["data"]["docs"]
        st.update(d["run"] for d in docs)
        if len(docs) < 100:
            break
        page += 1
    return f"{dataset}: chunks={ds['chunk_count']} states={dict(st)}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
