#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Oracle capture receiver — the local endpoint the "Oracle Capture" Chrome extension talks to.

Two jobs, one tiny always-on HTTP server (stdlib only — no requests/mcp, so it runs anywhere and
survives on a bare laptop mid-flight):

  POST /capture   {url,title,html,pdf_base64,captured_at}
        Turn the LIVE, authenticated DOM the browser captured into clean Markdown (the SAME
        trafilatura the fetch_url MCP tool uses, so captures match the rest of the corpus), write
        the .md (+ the archived .pdf) into corpus/inbox/captures/ IMMEDIATELY, and record a
        `pending` ingest job. A background drainer uploads pending jobs to the RAGFlow `links` KB
        whenever RAGFlow is reachable. => capturing works with the backend off; ingest catches up.

  POST /explain   {selection,url,title}
        Grounded "explain this" from the corpus, mirroring ask_corpus: retrieve (bge-m3) -> rerank
        (gte-multilingual, graceful fallback) -> synthesize with qwen, answering ONLY from the
        retrieved excerpts or saying the corpus doesn't cover it. Returns {answer, sources}.

  GET  /status    queue depth (pending/done/failed) + whether RAGFlow/synth are reachable.
  POST /drain     force a drain pass now.
  GET  /health     liveness.

Config (env, shared with the ask/ingest MCP tools so behaviour is consistent):
  ORACLE_RAGFLOW_URL   (http://localhost:9380)   ORACLE_RAGFLOW_KEY  (ragflow key)
  ORACLE_OLLAMA_URL    (http://localhost:11434)  ORACLE_SYNTH_MODEL  (qwen3-coder:30b)
  ORACLE_RERANK_ID/…                             ORACLE_CORPUS       (~/Projects/oracle/corpus)
  ORACLE_CAPTURE_PORT  (8788)                    ORACLE_CAPTURE_DATASET (links)

Binds to 127.0.0.1 only — never exposed to the LAN.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RAGFLOW = os.environ.get("ORACLE_RAGFLOW_URL", "http://localhost:9380").rstrip("/")
KEY = os.environ.get("ORACLE_RAGFLOW_KEY", "ragflow-smywlJs3drgGxfKztifTmD3iNJ2lP6Uvq2-suiLQTGM")
OLLAMA = os.environ.get("ORACLE_OLLAMA_URL", "http://localhost:11434").rstrip("/")
# Embeddings (bge-m3) always come from OLLAMA proper (:11434) — NOT the synth URL, which in
# production points at the llama.cpp qwen-next server (:18080) that has no /api/embed.
EMBED = os.environ.get("ORACLE_EMBED_URL", "http://localhost:11434").rstrip("/")
# Vision model + the ONE big GPU slot. Ownership lives in oracle_vram, shared with the Claude-Code
# shim, which now runs the same swap when a chat references an image. Two processes each holding
# their own private swap lock is not a lock: one would stop the unit the other had just started.
# oracle_vram serialises across processes and keeps availability PROBED rather than configured — a
# flag would have to be flipped in lockstep with every swap and would lie whenever the two drifted.
import oracle_vram
# Per-domain context: a hardcoded pack for our own sites, otherwise the site's own /AGENTS.md as
# fetched by the extension. Fenced and labelled at the point of use — it is text written by the
# site being examined.
import oracle_sitectx

VL_URL = oracle_vram.VL_URL
VL_MODEL = os.environ.get("ORACLE_VL_MODEL", "qwen3-vl")
VL_DISABLED_MSG = oracle_vram.VL_DISABLED_MSG
vl_available = oracle_vram.vl_available
text_available = oracle_vram.text_available
ensure_model = oracle_vram.ensure
AUTOSWAP = oracle_vram.AUTOSWAP
SYNTH_MODEL = os.environ.get("ORACLE_SYNTH_MODEL", "qwen3-coder:30b")
RERANK_ID = os.environ.get("ORACLE_RERANK_ID", "gte-multilingual-reranker-base@local-gte-rerank@Jina")
# Corpus browser (oracle-browser) — renders the ORIGINAL page a citation came from, so a footnote
# can be followed to the source instead of merely naming it (G3: "can I check it?").
BROWSER = os.environ.get("ORACLE_BROWSER_URL", "http://localhost:9765").rstrip("/")
HOME = Path.home()
CORPUS = Path(os.environ.get("ORACLE_CORPUS", str(HOME / "Projects/oracle/corpus"))).resolve()
CAPTURES = CORPUS / "inbox" / "captures"
PORT = int(os.environ.get("ORACLE_CAPTURE_PORT", "8788"))
DATASET = os.environ.get("ORACLE_CAPTURE_DATASET", "links")
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

_drain_now = threading.Event()
_ds_lock = threading.Lock()  # serialise dataset lookup/create so two captures don't double-create
_drain_lock = threading.Lock()  # one drain at a time (loop vs POST /drain) — no double uploads


# ------------------------------------------------------------------ http helpers (stdlib only)

def _req(method, url, data=None, headers=None, timeout=60):
    body = None
    hdrs = dict(headers or {})
    if data is not None and not isinstance(data, (bytes, bytearray)):
        body = json.dumps(data).encode()
        hdrs.setdefault("Content-Type", "application/json")
    elif isinstance(data, (bytes, bytearray)):
        body = data
    r = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def _ragflow(method, path, data=None, timeout=120):
    return _req(method, f"{RAGFLOW}/api/v1{path}", data=data, headers=HDR, timeout=timeout)


# ------------------------------------------------------------------ html -> markdown

_TRAFILATURA = shutil.which("trafilatura")


def _fallback_markdown(html: str) -> str:
    """Minimal HTML->text when trafilatura is absent — good enough to keep a capture, never the
    primary path on the laptop (which has trafilatura, matching fetch_url's output)."""
    h = re.sub(r"(?is)<head[^>]*>.*?</head>", " ", html)
    h = re.sub(r"(?is)<(script|style|noscript|template|svg)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?i)<(h[1-6])[^>]*>", r"\n\n## ", h)
    h = re.sub(r"(?i)<li[^>]*>", "\n- ", h)
    h = re.sub(r"(?i)<(br|/p|/div|/tr|/h[1-6])\s*/?>", "\n", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    h = unescape(h)
    h = re.sub(r"[ \t]+", " ", h)
    h = re.sub(r"\n[ \t]+", "\n", h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    return h.strip()


def html_to_markdown(html: str) -> str:
    if _TRAFILATURA:
        try:
            r = subprocess.run([_TRAFILATURA, "--markdown"], input=html,
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0 and len(r.stdout.strip()) > 80:
                return r.stdout.strip()
        except Exception:
            pass
    return _fallback_markdown(html)


def _slug(s: str, n: int = 60) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").strip()).strip("-").lower()
    return (s[:n].strip("-") or "page")


# ------------------------------------------------------------------ capture

def save_capture(payload: dict) -> dict:
    """Write .md (+ optional .pdf) and a `pending` job record. Never touches RAGFlow — that's the
    drainer's job — so this returns fast and works with the backend down."""
    CAPTURES.mkdir(parents=True, exist_ok=True)
    url = (payload.get("url") or "").strip()
    title = (payload.get("title") or "").strip()
    html = payload.get("html") or ""
    note = (payload.get("note") or "").strip()          # user's "why I kept this"
    partial = bool(payload.get("partial"))              # selection-only capture
    captured_at = payload.get("captured_at") or datetime.now(timezone.utc).isoformat()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stem = f"{ts}-{_slug(title or url)}"

    md_body = html_to_markdown(html) if html else ""
    header = f"# {title}\n\n> source: {url}\n> captured: {captured_at}\n"
    if partial:
        header += "> capture: selection only\n"
    if note:
        header += f"> note: {note}\n"
    header += "\n---\n\n"
    md_path = CAPTURES / f"{stem}.md"
    md_path.write_text(header + md_body, encoding="utf-8")

    pdf_path = None
    if payload.get("pdf_base64"):
        try:
            pdf_path = CAPTURES / f"{stem}.pdf"
            pdf_path.write_bytes(base64.b64decode(payload["pdf_base64"]))
        except Exception:
            pdf_path = None

    record = {
        "stem": stem, "url": url, "title": title, "captured_at": captured_at,
        "md": str(md_path), "pdf": str(pdf_path) if pdf_path else None,
        "note": note, "partial": partial,
        "dataset": DATASET, "chunk_method": "naive",
        "status": "pending", "attempts": 0, "doc_id": None, "error": None,
    }
    (CAPTURES / f"{stem}.capture.json").write_text(json.dumps(record, indent=2))
    _drain_now.set()
    return {"ok": True, "stem": stem, "md": str(md_path),
            "pdf": bool(pdf_path), "md_chars": len(md_body)}


def capture_job(stem: str) -> dict:
    """Ingest-confirmation for one capture: the local job status, and — once uploaded — the RAGFlow
    parse state + chunk count, so the extension can show 'parsed ✓ (N chunks)' instead of trusting
    a fire-and-forget POST (Axiom 2: close the loop)."""
    j = CAPTURES / f"{stem}.capture.json"
    if not j.exists():
        return {"error": "no such capture", "stem": stem}
    rec = json.loads(j.read_text())
    out = {"stem": stem, "status": rec.get("status"), "doc_id": rec.get("doc_id"),
           "error": rec.get("error")}
    if rec.get("status") in ("done", "duplicate") and rec.get("doc_id"):
        try:
            dsid = _dataset_id(rec["dataset"], rec["chunk_method"])
            page = 1
            while True:
                docs = _ragflow("GET", f"/datasets/{dsid}/documents?page={page}&page_size=100",
                                timeout=30)["data"]["docs"]
                hit = next((d for d in docs if d["id"] == rec["doc_id"]), None)
                if hit:
                    out["parse"] = hit.get("run")          # UNSTART/RUNNING/DONE/FAIL
                    out["chunks"] = hit.get("chunk_count", 0)
                    out["progress"] = hit.get("progress")
                    break
                if len(docs) < 100:
                    break
                page += 1
        except Exception as e:
            out["parse_error"] = str(e)[:200]
    return out


# ------------------------------------------------------------------ drainer (ingest pending -> RAGFlow)

def _dataset_id(name: str, chunk_method: str) -> str:
    with _ds_lock:
        existing = {d["name"]: d for d in _ragflow("GET", "/datasets?page_size=100", timeout=30)["data"]}
        if name in existing:
            return existing[name]["id"]
        ds = _ragflow("POST", "/datasets", data={
            "name": name, "chunk_method": chunk_method,
            "parser_config": {"raptor": {"use_raptor": False}, "graphrag": {"use_graphrag": False}},
        })["data"]
        return ds["id"]


def _find_doc_by_name(dsid: str, name: str) -> str | None:
    """Look up a document id by filename. Used to recover the id when an upload comes back empty
    because RAGFlow already holds that name — the harness knows the answer, so it must say it
    rather than hand back None and let the caller record a success that never happened."""
    page = 1
    while True:
        docs = _ragflow("GET", f"/datasets/{dsid}/documents?page={page}&page_size=100",
                        timeout=30)["data"]["docs"]
        hit = next((d for d in docs if d.get("name") == name), None)
        if hit:
            return hit["id"]
        if len(docs) < 100:
            return None
        page += 1


def _upload(md_path: Path, dsid: str) -> tuple[str | None, bool]:
    """Multipart upload of one .md, then kick off parsing.

    Returns `(doc_id, duplicate)`. RAGFlow answers an already-known filename with an EMPTY data
    list — indistinguishable from success unless you look. Previously that returned None and the
    caller stored status=done/doc_id=None: the capture was never ingested and the UI said it was.
    Now the duplicate is named as such, and we recover the existing id so /job can still report the
    real parse state."""
    boundary = "----oracle" + md_path.stem
    fname = md_path.name
    data = md_path.read_bytes()
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fname}\"\r\n"
        f"Content-Type: text/markdown\r\n\r\n".encode() + data + f"\r\n--{boundary}--\r\n".encode()
    )
    resp = _req("POST", f"{RAGFLOW}/api/v1/datasets/{dsid}/documents", data=body,
                headers={"Authorization": f"Bearer {KEY}",
                         "Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=300)
    docs = resp.get("data", [])
    if not docs:
        return _find_doc_by_name(dsid, fname), True
    doc_id = docs[0]["id"]
    _ragflow("POST", f"/datasets/{dsid}/chunks", data={"document_ids": [doc_id]})
    return doc_id, False


def _drain_once() -> dict:
    """Upload every `pending` capture. Serialised by `_drain_lock`: the background loop and an
    explicit POST /drain would otherwise walk the same job files concurrently and upload a capture
    twice (the second landing as a 'duplicate' — a self-inflicted version of the 28-copies-of-one-
    man-page ratchet)."""
    with _drain_lock:
        jobs = sorted(CAPTURES.glob("*.capture.json")) if CAPTURES.exists() else []
        done = dup = failed = 0
        for j in jobs:
            try:
                rec = json.loads(j.read_text())
            except Exception:
                continue
            if rec.get("status") != "pending":
                continue
            try:
                dsid = _dataset_id(rec["dataset"], rec["chunk_method"])
                doc_id, duplicate = _upload(Path(rec["md"]), dsid)
                rec["doc_id"] = doc_id
                if duplicate:
                    # Already in the KB under this name. NOT a fresh ingest — say so, so the popup
                    # can't show "parsed ✓" for something this run never actually ingested.
                    rec["status"] = "duplicate"
                    rec["error"] = None if doc_id else "duplicate name, existing doc not found"
                    dup += 1
                else:
                    rec["status"] = "done"
                    done += 1
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                # RAGFlow unreachable (backend off / mid-flight) — leave pending, retry next pass
                return {"done": done, "duplicate": dup, "failed": failed, "ragflow": False}
            except Exception as e:
                rec["attempts"] = rec.get("attempts", 0) + 1
                rec["error"] = str(e)[:300]
                if rec["attempts"] >= 5:
                    rec["status"] = "failed"
                    failed += 1
            j.write_text(json.dumps(rec, indent=2))
        return {"done": done, "duplicate": dup, "failed": failed, "ragflow": True}


def _drainer_loop():
    while True:
        try:
            _drain_once()
        except Exception:
            pass
        _drain_now.wait(timeout=20)
        _drain_now.clear()


# ------------------------------------------------------------------ explain (grounded, mirrors ask_corpus)

def _kb_ids() -> list[str]:
    data = _ragflow("GET", "/datasets?page_size=100", timeout=30)["data"]
    return [d["id"] for d in data if d.get("chunk_count", 0) > 0]


def _retrieve(query: str, kb_ids: list[str]):
    body = {"question": query[:500], "dataset_ids": kb_ids, "page_size": 64,
            "top_k": 64, "similarity_threshold": 0.15}
    for use_rerank in (True, False):
        b = dict(body)
        if use_rerank:
            b["rerank_id"] = RERANK_ID
        try:
            j = _ragflow("POST", "/retrieval", data=b, timeout=90)
            if j.get("code") == 0:
                return j["data"].get("chunks", []), use_rerank
        except Exception:
            pass
    return [], False


def _script(s: str) -> str:
    return "cyr" if re.search(r"[а-яА-ЯёЁ]{4,}", s or "") else "lat"


def _pretty_doc(docname: str) -> str:
    """RAGFlow doc name -> something readable in a footnote.

    Names are path-encoded (`collection_raw__Databases__elasticsearch__Elasticsearch_ The
    Definitive Guide-...pdf`), which is unreadable as a citation. Keep the last path segment and
    drop the extension; that is the actual title in every naming scheme the corpus uses."""
    base = (docname or "?").split("__")[-1]
    for ext in (".pdf", ".txt", ".md", ".epub"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    return base.strip() or (docname or "?")


def _chunk_page(chunk: dict) -> int | None:
    """Page number of a chunk, from RAGFlow's `positions` ([[page, x0, x1, y0, y1], ...])."""
    pos = chunk.get("positions") or []
    try:
        return int(pos[0][0])
    except Exception:
        return None


_FRONTMATTER = re.compile(r"\A\s*(?:---|\+\+\+)\r?\n.*?\r?\n(?:---|\+\+\+)\r?\n", re.S)


def _cite_lead(text: str) -> str:
    """The line of a chunk most likely to be findable in the source markdown.

    Two traps, both hit on real k8s docs. The chunk's stored text often BEGINS with the YAML
    front-matter (`--- reviewers: … ---`), but the browser strips front-matter before searching, so
    anchoring on it always fails silently. And Hugo shortcodes / heading markup (`{{< … >}}`, `##`)
    are rewritten during rendering, so they are unreliable probes.

    Among the remaining prose lines, take the LONGEST rather than the first. Measured over 20 real
    markdown citations: longest 60% located vs first-line 40% — a chunk's opening line is
    disproportionately a heading or a reformatted fragment, while its longest line is ordinary prose
    that survives rendering unchanged, and offers the locator more words to probe with.
    When it still misses there is no #hit and the browser falls back to scrolling to the first
    highlighted query term, which is why this is an optimisation and not a correctness dependency."""
    body = _FRONTMATTER.sub("", text or "")
    best = ""
    for line in body.splitlines():
        s = line.strip().lstrip("#>-*| ").strip()
        if not s or s.startswith("{{") or s.startswith("<!--") or s.startswith("|"):
            continue
        if len(re.findall(r"[^\W\d_]{3,}", s)) >= 3 and len(s) > len(best):
            best = s
    return " ".join(best.split()[:12])[:160]


def _cite_url(docname: str, page: int | None, query: str, snippet: str = "") -> str:
    """Deep link into the corpus browser at the exact passage a citation came from.

    Two routes, because two kinds of source: PDF/txt docs render page-accurately via /view (?p=),
    markdown docs have no pages and render via /md. ?q= highlights the query terms on arrival.

    For markdown we can do better than "highlight the query somewhere on the page": we know the
    chunk's own text, and the browser can anchor on it (`?find=` locates the block, `#hit` scrolls
    to it). Without that, a long doc with 39 query-term matches drops you at the FIRST one, which is
    often a heading rather than the sentence the claim came from. Passing the chunk's opening words
    makes the footnote land on the cited passage itself — the whole point of a citation you can
    follow."""
    doc = urllib.parse.quote(docname or "", safe="")
    q = urllib.parse.quote((query or "")[:200], safe="")
    if (docname or "").lower().endswith(".md"):
        url = f"{BROWSER}/md/{doc}?q={q}"
        # _locate_in_md probes with the first ~6 letter-runs, so a short prose lead is enough
        lead = _cite_lead(snippet)
        if lead:
            url += f"&find={urllib.parse.quote(lead, safe='')}#hit"
        return url
    p = page if isinstance(page, int) and page > 0 else 1
    return f"{BROWSER}/view/{doc}?p={p}&q={q}"


def _citations(chunks: list, query: str) -> list[dict]:
    """One entry per excerpt, INDEX-ALIGNED with the [n] numbering the model is shown.

    This is the mapping the UI needs and previously never got: the sources event emitted a sorted
    SET of document names, so a "[2]" in the answer could not be resolved to anything — the numbers
    were decoration. Emitting the ordered list makes each [n] a real, followable reference."""
    out = []
    for i, c in enumerate(chunks):
        doc = c.get("document_keyword", "?")
        page = _chunk_page(c)
        text = c.get("content_with_weight") or c.get("content", "")
        out.append({
            "n": i + 1,
            "doc": _pretty_doc(doc),
            "docname": doc,
            "page": page,
            "chunk_id": c.get("id"),
            "url": _cite_url(doc, page, query, text),
        })
    return out


def _diversify(query: str, chunks: list, main: int = 18, cross: int = 4) -> list:
    q = _script(query)
    top = chunks[:main]
    other = [c for c in chunks[main:]
             if _script(c.get("content_with_weight") or c.get("content", "")) != q]
    return top + other[:cross]


def _chat_stream(messages, url: str = None, model: str = None, timeout: int = 300, max_tokens: int = 2048):
    """Yield chat text deltas from an OpenAI-compatible /v1/chat/completions (Ollama, llama.cpp, or the
    qwen3-vl server). Streaming so the glued popup fills token-by-token instead of waiting."""
    url = url or OLLAMA
    model = model or SYNTH_MODEL
    body = json.dumps({"model": model, "stream": True, "messages": messages,
                       "temperature": 0.1, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(f"{url}/v1/chat/completions", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:  # iterates SSE lines as the socket delivers them
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0].get("delta", {}).get("content")
            except Exception:
                continue
            if delta:
                yield delta


_GROUND_RULES = (
    "using ONLY the provided documentation excerpts. Every specific claim (names, flags, sizes, "
    "semantics, versions) must come from an excerpt, never your own knowledge; cite the excerpt "
    "number/source for key claims. Write in the SAME language as the input; never switch languages "
    "mid-answer. Tag code fences by language.")

_EXPLAIN_SYSTEM = (
    "You explain a term or passage the user selected while reading, " + _GROUND_RULES +
    " If the excerpts do not explain it, reply exactly: 'The corpus doesn't cover this.' Be concise "
    "(a few sentences).")

_ASK_SYSTEM = (
    "You answer a documentation/API/concept question " + _GROUND_RULES +
    " If the excerpts do not contain the answer, reply exactly: 'The corpus doesn't cover this.' "
    "Be concise and direct.")

_FACTCHECK_SYSTEM = (
    "You fact-check a claim the user is reading against the corpus, " + _GROUND_RULES +
    " START your reply with exactly one verdict tag on its own — [SUPPORTED], [CONTRADICTED], "
    "[PARTIAL], or [NOT COVERED] — then a brief justification quoting the decisive excerpt. "
    "[SUPPORTED]/[CONTRADICTED]/[PARTIAL] require excerpts that actually address the claim; if none "
    "do, you MUST use [NOT COVERED] (the corpus is silent — never guess from your own knowledge).")



# ------------------------------------------------------------------ debug channel
# "I'm not sure it ever injects page context" is a fair thing to wonder, and it should not need a
# code read to answer. When a request asks for it, the receiver reports what it actually composed —
# the sections, their sizes, and the verbatim text — as `debug` SSE events the widget shows in its
# own tab. Off by default: it echoes the whole prompt back, which is large and, on a logged-in page,
# is the page's own content.
def _dbg(enabled: bool, stage: str, **fields):
    """A ('debug', {...}) event, or nothing when debugging is off."""
    if not enabled:
        return []
    return [("debug", {"stage": stage, **fields})]


def _sections(text: str) -> list:
    """Split a composed context into labelled sections with sizes, so the debug tab can show what
    went in without the reader having to diff two 6 KB blobs by eye."""
    out, cur, buf = [], "preamble", []
    for line in (text or "").splitlines():
        m = re.match(r"^(Page title:|Page URL:|About [^ ]+ —|What [^ ]+ publishes|How the page itself|"
                     r"Text on the page|Text rendered INSIDE|Text visible on the page|"
                     r"What this page describes)", line)
        if m:
            if buf:
                out.append({"section": cur, "chars": len("\n".join(buf))})
            cur, buf = m.group(1).rstrip(" —"), [line]
        else:
            buf.append(line)
    if buf:
        out.append({"section": cur, "chars": len("\n".join(buf))})
    return out


def _grounded_stream(retrieval_query: str, framing: str, system: str, site: str = "",
                     debug: bool = False):
    """Shared retrieve→rerank→stream path behind /explain, /ask, /factcheck. Emits SSE (event, data)
    pairs: ('sources',{sources,reranked}) once, then ('delta',{text})*, then ('done',{}) | ('error',…).
    `retrieval_query` drives retrieval; `framing` is the task-specific instruction wrapping the input.
    """
    try:
        kb_ids = _kb_ids()
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        yield ("error", {"error": "Corpus backend (RAGFlow) is unreachable."})
        return
    except Exception as e:
        yield ("error", {"error": f"corpus error: {e}"})
        return
    if not kb_ids:
        yield ("sources", {"sources": [], "reranked": False})
        yield ("delta", {"text": "The corpus has no parsed content yet."})
        yield ("done", {})
        return
    chunks, reranked = _retrieve(retrieval_query, kb_ids)
    if not chunks:
        yield ("sources", {"sources": [], "reranked": reranked})
        yield ("delta", {"text": "The corpus doesn't cover this (no relevant passages retrieved)."})
        yield ("done", {})
        return
    chunks = _diversify(retrieval_query, chunks)
    yield ("sources", {"sources": sorted({c.get("document_keyword", "?") for c in chunks}),
                       "citations": _citations(chunks, retrieval_query),
                       "reranked": reranked})
    context = "\n\n".join(
        f"[{i+1}] (source: {c.get('document_keyword','?')})\n"
        f"{c.get('content_with_weight') or c.get('content','')}"
        for i, c in enumerate(chunks))
    # Site context sits between the task and the excerpts: close enough to disambiguate the page's
    # vocabulary, and visibly separate from the excerpts, which are the only citable material.
    user = f"{framing}\n\n{site}\n\nExcerpts:\n{context}" if site else \
        f"{framing}\n\nExcerpts:\n{context}"
    yield from _dbg(debug, "prompt sent to the text model", chars=len(user),
                    site_context_chars=len(site), excerpt_count=len(chunks),
                    reranked=reranked, system=system, text=user)
    # Retrieval is done (CPU + embeddings); only NOW is the text model needed, so a swap — if the
    # vision model is currently resident — is paid for as late as possible.
    try:
        for note in ensure_model("text"):
            yield ("status", {"text": note})
    except RuntimeError as e:
        yield ("error", {"error": str(e)})
        return
    try:
        for delta in _chat_stream([{"role": "system", "content": system},
                                   {"role": "user", "content": user}]):
            yield ("delta", {"text": delta})
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        yield ("error", {"error": "Synthesis model is unreachable."})
        return
    except Exception as e:
        yield ("error", {"error": f"synthesis error: {e}"})
        return
    yield ("done", {})


def explain_stream(selection: str, url: str = "", title: str = "", agents_md: str | None = None,
                   debug: bool = False):
    where = f" (seen on: {title or url})" if (title or url) else ""
    return _grounded_stream(
        selection, f'Explain this selection{where}:\n\n"""\n{selection[:2000]}\n"""', _EXPLAIN_SYSTEM,
        oracle_sitectx.block(url, agents_md), debug)


def ask_stream(question: str):
    return _grounded_stream(question, f"Question: {question[:1000]}", _ASK_SYSTEM)


def factcheck_stream(claim: str, url: str = "", title: str = "", agents_md: str | None = None,
                     debug: bool = False):
    where = f" (from: {title or url})" if (title or url) else ""
    return _grounded_stream(
        claim, f'Claim to check{where}:\n\n"""\n{claim[:2000]}\n"""', _FACTCHECK_SYSTEM,
        oracle_sitectx.block(url, agents_md), debug)


VL_CTX_CHARS = int(os.environ.get("ORACLE_VL_CTX_CHARS", "4000"))
# Above this many characters, a raw page dump is worth compressing before it is handed to the
# vision model — a long dashboard page is mostly nav, filters and repeated legends.
VL_SUMMARIZE_OVER = int(os.environ.get("ORACLE_VL_SUMMARIZE_OVER", "2500"))

# Deliberately describes the JOB, not a list of things to find. An earlier version enumerated
# dashboard nouns (panel titles, metric names, units, time range) because the first use case was
# Grafana — and an enumeration in a prompt does not read as "for example", it reads as the whole
# task, so the model would have gone hunting for axes on a page that holds a code diff or a figure.
# Same bug as the hardcoded project names and the stale corpus inventory: the fix is to state the
# goal and let the model decide what satisfies it, for any kind of page.
_VL_BRIEF_SYSTEM = (
    "You prepare a short briefing that will be given to a VISION model along with a screenshot "
    "cropped from this page. Ask yourself: what would a person need to have read on this page in "
    "order to correctly interpret that image — to know what it depicts, what its labels and numbers "
    "mean, and what it is part of? Write down exactly that, and nothing else. Keep proper nouns, "
    "identifiers, units and figures verbatim; they are the parts an image cannot supply. Leave out "
    "navigation, controls, boilerplate, and anything that describes nothing visible. No preamble, "
    "no conclusion. If the page offers nothing that would help interpret an image from it, reply "
    "exactly: NO USEFUL CONTEXT."
)


def _summarize_for_vl(page_text: str, url: str, title: str) -> str:
    """Compress the page into a briefing for the vision model — using the TEXT model.

    Ordering is the point: this runs BEFORE the swap to vision, while the text model is still
    resident, so it is free. Doing it the obvious way (swap to VL, discover the context is huge,
    swap back to summarise, swap forward again) would cost two extra ~40 s swaps to save tokens.

    If the text model is NOT currently resident we skip it rather than swapping in to summarise:
    that would trade a cheap truncation for a minute of loading. Truncation is the fallback, so
    this is an optimisation that can always decline."""
    t = " ".join((page_text or "").split())
    if len(t) < VL_SUMMARIZE_OVER or not text_available():
        return ""
    user = (f"Page title: {title}\nPage URL: {url}\n\nPage text:\n{t[:12000]}")
    try:
        out = "".join(_chat_stream(
            [{"role": "system", "content": _VL_BRIEF_SYSTEM}, {"role": "user", "content": user}],
            timeout=120, max_tokens=400))
    except Exception:
        return ""
    out = out.strip()
    return "" if (not out or "NO USEFUL CONTEXT" in out.upper()) else out


def _vl_context(url: str, title: str, page_text: str, summarized: bool = False,
                crop_text: str = "", img: dict | None = None, source: str = "region",
                agents_md: str | None = None) -> str:
    """Text context to put in FRONT of the screenshot.

    A cropped region carries almost no self-description: a Grafana panel is a line and some axis
    ticks, and a model given only pixels will invent a plausible system, metric and time range —
    the failure this project exists to prevent, in a new modality. The page it came from usually
    states all three (dashboard name, panel titles, legend, units), so send that first.

    Truncation is head+tail rather than head-only: the page's identity is at the top, but the
    numbers, legends and panel labels a dashboard renders often sit lower down."""
    parts = []
    if title:
        parts.append(f"Page title: {title.strip()}")
    if url:
        parts.append(f"Page URL: {url.strip()}")
    site = oracle_sitectx.block(url, agents_md, citable=True)
    if site:
        parts.append(site)
    # The image's OWN markup outranks everything else on the page: alt text, title and a
    # <figcaption> are written specifically to say what this picture is. Without them a model
    # re-derives — or invents — what the author already stated.
    im = img or {}
    described = [(k, " ".join((im.get(k) or "").split()))
                 for k in ("image_alt", "image_title", "image_caption")]
    described = [(k.replace("image_", ""), v) for k, v in described if v]
    if described:
        parts.append("How the page itself describes this image (authoritative — it was written to "
                     "describe exactly this picture):\n"
                     + "\n".join(f"  {k}: {v[:600]}" for k, v in described))

    # Then the LOCAL text. On a page of twelve panels the whole-page text names twelve metrics and
    # cannot say which one was cropped — the model then picks plausibly, which is guessing with
    # better inputs. Local text answers "which". What "local" means differs by entry point, and the
    # label must say which one it is: text inside a dragged rectangle IS the image's own text, while
    # prose around an <img> merely sits next to it. Labelling the second as the first is the same
    # overclaim this file exists to prevent — one modality over.
    crop = " ".join((crop_text or "").split())
    if crop:
        label = ("Text on the page immediately AROUND this image (context, not necessarily part of "
                 "the picture)" if source == "image" else
                 "Text rendered INSIDE the cropped region (this describes the image itself, prefer "
                 "it over the rest of the page)")
        parts.append(f"{label}:\n{crop[:VL_CTX_CHARS]}")
    t = " ".join((page_text or "").split())
    if t:
        if summarized:
            parts.append(f"What this page describes (summarised from the page):\n{t}")
        else:
            if len(t) > VL_CTX_CHARS:
                head, tail = VL_CTX_CHARS * 2 // 3, VL_CTX_CHARS // 3
                t = t[:head] + " …[middle elided]… " + t[-tail:]
            parts.append(f"Text visible on the page (may be truncated):\n{t}")
    if not parts:
        return ""
    came_from = {"image": "it was taken from this page",
                 "page": "it is a screenshot of this page's entire visible area",
                 # Say "scrolled and stitched" rather than letting the model assume one screenful.
                 # A stitched capture can contain content that was never on screen together, and a
                 # model told it is looking at "the visible area" would reason about layout and
                 # adjacency that never existed.
                 "fullpage": ("it is a FULL-PAGE screenshot: the page was scrolled from the top and "
                              "the screenfuls stitched vertically into one tall image, so content "
                              "far apart in it was never visible at the same time (and a very long "
                              "page may be cut off at the bottom)"),
                 }.get(source, "the region was screenshotted from this page")
    return (f"Context for the image below — {came_from}. Use it to identify what is being shown "
            "(system, dashboard, metric names, units, time range) instead of guessing, but "
            "describe only what the IMAGE actually shows.\n\n"
            + "\n".join(parts))


def vision_stream(image_data_url: str, prompt: str = "", url: str = "", title: str = "",
                  page_text: str = "", crop_text: str = "", img: dict | None = None,
                  source: str = "region", agents_md: str | None = None, debug: bool = False):
    """Stream the qwen3-vl answer for a screenshotted region. NOT grounded — a direct look at the
    pixels (read this diagram / transcribe this / what is this). Emits ('delta',…)* then ('done',{}).

    STUB while VL_ENABLED is false: fails fast with the reason instead of timing out on :18081.
    The wire shape is identical either way, so the extension's card/⚓-ground flow needs no change
    when the broker lands — only the env flag flips."""
    # STEP 1, before any swap: if the page is long and the text model is still resident, have it
    # write the briefing now. This is the only moment it is free.
    brief, summarized = "", False
    yield from _dbg(debug, "input", source=source, url=url, title=title,
                    page_text_chars=len(page_text or ""), crop_text_chars=len(crop_text or ""),
                    image_alt=(img or {}).get("image_alt", ""),
                    agents_md_chars=len(agents_md or "") if agents_md is not None else None,
                    summarise_threshold=VL_SUMMARIZE_OVER)
    if page_text and len(page_text) >= VL_SUMMARIZE_OVER and text_available():
        yield ("status", {"text": "summarising the page with the text model…"})
        brief = _summarize_for_vl(page_text, url, title)
        summarized = bool(brief)
        yield from _dbg(debug, "summarised", ok=summarized, in_chars=len(page_text),
                        out_chars=len(brief), text=brief)
    else:
        yield from _dbg(debug, "summarise skipped",
                        why=("no page text" if not page_text else
                             f"{len(page_text)} < {VL_SUMMARIZE_OVER} chars" if len(page_text) < VL_SUMMARIZE_OVER
                             else "text model not resident (it would have to be swapped in)"))

    # STEP 2: now swap to vision.
    try:
        for note in ensure_model("vl"):
            yield ("status", {"text": note})
    except RuntimeError as e:
        yield ("error", {"error": f"{e}\n\n{VL_DISABLED_MSG}", "reason": "vision_unavailable"})
        return
    prompt = (prompt or "").strip() or "Describe and explain what is shown in this image. Be concise."
    ctx = _vl_context(url, title, brief or page_text, summarized, crop_text, img, source, agents_md)
    # Order matters: context, then the image, then the question. The text frames what the pixels
    # are before the model looks at them; the question stays last so it is the most recent thing.
    yield from _dbg(debug, "context sent to qwen3-vl", chars=len(ctx),
                    sections=_sections(ctx), prompt=prompt, text=ctx)
    content = ([{"type": "text", "text": ctx}] if ctx else []) + [
        {"type": "image_url", "image_url": {"url": image_data_url}},
        {"type": "text", "text": prompt}]
    msgs = [{"role": "user", "content": content}]
    try:
        for delta in _chat_stream(msgs, url=VL_URL, model=VL_MODEL, timeout=420, max_tokens=1500):
            yield ("delta", {"text": delta})
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        yield ("error", {"error": "Vision model (qwen3-vl) is unreachable."})
        return
    except Exception as e:
        yield ("error", {"error": f"vision error: {e}"})
        return
    yield ("done", {})


# ------------------------------------------------------------------ oracle-context: fading-slot memory (H17 SENSOR)
# NOTE: this is the SENSOR + inspectable memory only. It records what I'm exploring; it does NOT yet
# feed reranking — that blend stays parked behind the gold-query eval gate (DESIGN §5.4 / TODO H17).

CTX_PATH = CORPUS / "inbox" / "context-memory.json"
CTX_K = int(os.environ.get("ORACLE_CTX_SLOTS", "12"))            # fixed slot count (bounded memory)
CTX_TAU = float(os.environ.get("ORACLE_CTX_TAU_HOURS", "12")) * 3600.0   # decay half-life-ish (s)
CTX_MERGE = float(os.environ.get("ORACLE_CTX_MERGE", "0.60"))   # cosine >= this -> same slot
# Weight ceiling. Merging accumulated w0+w1 without bound, and decay is multiplicative, so a topic
# hit often enough became effectively immortal: it always outranked fresher slots at eviction and
# took days to fade. That defeats the point of a *fading* memory — a bounded budget only stays
# meaningful if new information can evict staler information (DESIGN §5.4: forgetting is the
# mechanism, not a limitation). Saturating keeps "revisited often" strong but still mortal.
CTX_WMAX = float(os.environ.get("ORACLE_CTX_WMAX", "8"))
_ctx_lock = threading.Lock()


def _embed(text: str):
    j = _req("POST", f"{EMBED}/api/embed", data={"model": "bge-m3", "input": text[:2000]}, timeout=60)
    v = j.get("embeddings") or j.get("embedding")
    return v[0] if v and isinstance(v[0], list) else v


def _cos(a, b) -> float:
    s = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return s / (na * nb) if na and nb else 0.0


def _ctx_load() -> dict:
    if CTX_PATH.exists():
        try:
            return json.loads(CTX_PATH.read_text())
        except Exception:
            pass
    return {"slots": [], "exclusions": []}


def _ctx_save(mem: dict):
    CTX_PATH.parent.mkdir(parents=True, exist_ok=True)
    CTX_PATH.write_text(json.dumps(mem, indent=2))


def _ctx_decay(mem: dict, now: float):
    for s in mem["slots"]:
        if s.get("pinned"):
            continue
        dt = max(0.0, now - s.get("last_seen", now))
        s["weight"] = s.get("weight", 1.0) * (2.718281828 ** (-dt / CTX_TAU))


def _excluded(mem: dict, url: str, title: str) -> bool:
    hay = f"{url} {title}".lower()
    return any(x and x.lower() in hay for x in mem.get("exclusions", []))


def observe(text: str, weight: float = 1.0, url: str = "", title: str = "") -> dict:
    """Fold a browsed/captured/explained topic into the fading-slot memory. Bounded (K slots),
    decaying (exp), associative (similar topics merge instead of each burning a slot)."""
    text = (text or "").strip()
    if not text:
        return {"skipped": "empty"}
    with _ctx_lock:
        mem = _ctx_load()
        if _excluded(mem, url, title):
            return {"skipped": "excluded"}
        try:
            emb = _embed(f"{title}\n{text}")
        except Exception as e:
            return {"skipped": f"embed-failed: {str(e)[:120]}"}
        if not emb:
            return {"skipped": "no-embedding"}
        now = time.time()
        _ctx_decay(mem, now)
        label = (title.strip() or " ".join(text.split()[:6]))[:60]
        best, best_sim = None, -1.0
        for s in mem["slots"]:
            sim = _cos(emb, s["centroid"])
            if sim > best_sim:
                best, best_sim = s, sim
        if best is not None and best_sim >= CTX_MERGE:
            # EMA merge (weight-blended) — the associative step
            w0, w1 = best["weight"], weight
            a = w1 / (w0 + w1) if (w0 + w1) else 0.5
            best["centroid"] = [(1 - a) * c + a * e for c, e in zip(best["centroid"], emb)]
            best["weight"] = min(w0 + w1, CTX_WMAX)
            best["last_seen"] = now
            best["hits"] = best.get("hits", 1) + 1
            action = "merged"
        else:
            if len(mem["slots"]) >= CTX_K:
                victim = min((s for s in mem["slots"] if not s.get("pinned")),
                             key=lambda s: s["weight"], default=None)
                if victim is not None:
                    mem["slots"].remove(victim)
            mem["slots"].append({"label": label, "centroid": emb, "weight": weight,
                                 "last_seen": now, "hits": 1, "pinned": False})
            action = "new"
        _ctx_save(mem)
        return {"action": action, "sim": round(best_sim, 3), "slots": len(mem["slots"])}


def ctx_slots() -> dict:
    with _ctx_lock:
        mem = _ctx_load()
        _ctx_decay(mem, time.time())
        _ctx_save(mem)
        slots = sorted(mem["slots"], key=lambda s: s["weight"], reverse=True)
        return {"slots": [{"label": s["label"], "weight": round(s["weight"], 3),
                           "hits": s.get("hits", 1), "pinned": s.get("pinned", False)} for s in slots],
                "exclusions": mem.get("exclusions", []),
                "params": {"K": CTX_K, "tau_hours": CTX_TAU / 3600.0, "merge": CTX_MERGE,
                           "wmax": CTX_WMAX}}


def ctx_exclude(action: str, value: str) -> dict:
    with _ctx_lock:
        mem = _ctx_load()
        ex = mem.setdefault("exclusions", [])
        value = (value or "").strip().lower()
        if action == "add" and value and value not in ex:
            ex.append(value)
        elif action == "remove" and value in ex:
            ex.remove(value)
        _ctx_save(mem)
        return {"exclusions": ex}


def ctx_forget(label: str) -> dict:
    with _ctx_lock:
        mem = _ctx_load()
        before = len(mem["slots"])
        mem["slots"] = [s for s in mem["slots"] if s["label"] != label]
        _ctx_save(mem)
        return {"removed": before - len(mem["slots"])}


def ctx_pin(label: str, pinned: bool) -> dict:
    with _ctx_lock:
        mem = _ctx_load()
        for s in mem["slots"]:
            if s["label"] == label:
                s["pinned"] = pinned
        _ctx_save(mem)
        return {"ok": True}


# ------------------------------------------------------------------ status

def status() -> dict:
    counts = {"pending": 0, "done": 0, "duplicate": 0, "failed": 0}
    if CAPTURES.exists():
        for j in CAPTURES.glob("*.capture.json"):
            try:
                st = json.loads(j.read_text()).get("status", "pending")
                counts[st] = counts.get(st, 0) + 1
            except Exception:
                pass
    ragflow_ok = synth_ok = False
    try:
        _ragflow("GET", "/datasets?page_size=1", timeout=5)
        ragflow_ok = True
    except Exception:
        pass
    try:
        urllib.request.urlopen(f"{OLLAMA}/v1/models", timeout=5)
        synth_ok = True
    except Exception:
        pass
    _vis = vl_available()
    try:
        topics = len(_ctx_load().get("slots", []))
    except Exception:
        topics = 0
    return {"queue": counts, "ragflow": ragflow_ok, "synth": synth_ok,
            "vision": _vis, "vision_note": None if _vis else VL_DISABLED_MSG,
            "dataset": DATASET, "captures_dir": str(CAPTURES), "port": PORT, "topics": topics}


# ------------------------------------------------------------------ http server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _origin_ok(self) -> bool:
        """Only the extension (or a local CLI) may drive this server.

        Binding 127.0.0.1 keeps the LAN out, but it does NOT keep out a page you happen to be
        visiting: an `http://` page can fetch `http://localhost:8788` (only `https://` is stopped by
        mixed-content), and the old handler echoed whatever Origin it was given — so any such page
        could POST /capture and write into the corpus, or read /slots and learn what you've been
        reading. Captures become RAG answers, so an injection here is a corpus-poisoning vector,
        which is the one class of bug this project exists to prevent.

        Allowed: `chrome-extension://…` (the background worker) and requests with NO Origin at all
        (curl, health checks) — a browser always sends Origin on cross-origin fetch, so permitting
        its absence does not reopen the page vector."""
        origin = self.headers.get("Origin")
        return (not origin) or origin.startswith("chrome-extension://")

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin and origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, events):
        """Stream (event, data) pairs as Server-Sent Events. Connection: close (no Content-Length),
        so the fetch() ReadableStream in the extension reads deltas until the socket closes."""
        self.close_connection = True
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for ev, data in events:
                self.wfile.write(f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError):
            pass  # client closed the popup — stop streaming

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _query(self, key, default=""):
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(self.path).query).get(key, [default])[0]

    def do_GET(self):
        if not self._origin_ok():
            self._send({"error": "forbidden origin"}, 403)
            return
        if self.path.startswith("/status"):
            self._send(status())
        elif self.path.startswith("/health"):
            self._send({"ok": True})
        elif self.path.startswith("/slots"):
            self._send(ctx_slots())
        elif self.path.startswith("/job"):
            self._send(capture_job(self._query("stem")))
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        if not self._origin_ok():
            self._send({"error": "forbidden origin"}, 403)
            return
        try:
            p = self._read_json()
            if self.path.startswith("/capture"):
                self._send(save_capture(p))
            elif self.path.startswith("/explain"):
                sel = (p.get("selection") or "").strip()
                if not sel:
                    self._send({"error": "empty selection"}, 400)
                    return
                self._send_sse(explain_stream(sel, p.get("url", ""), p.get("title", ""),
                                              p.get("agents_md"), bool(p.get("debug"))))
            elif self.path.startswith("/ask"):
                q = (p.get("question") or "").strip()
                if not q:
                    self._send({"error": "empty question"}, 400)
                    return
                self._send_sse(ask_stream(q))
            elif self.path.startswith("/factcheck"):
                claim = (p.get("claim") or p.get("selection") or "").strip()
                if not claim:
                    self._send({"error": "empty claim"}, 400)
                    return
                self._send_sse(factcheck_stream(claim, p.get("url", ""), p.get("title", ""),
                                                p.get("agents_md"), bool(p.get("debug"))))
            elif self.path.startswith("/vision"):
                img = p.get("image") or ""
                if not img:
                    self._send({"error": "no image"}, 400)
                    return
                if not img.startswith("data:"):
                    img = f"data:{p.get('mime', 'image/png')};base64,{img}"
                self._send_sse(vision_stream(img, p.get("prompt", ""), p.get("url", ""),
                                             p.get("title", ""), p.get("page_text", ""),
                                             p.get("crop_text", ""),
                                             {k: p.get(k, "") for k in
                                              ("image_alt", "image_title", "image_caption")},
                                             p.get("source", "region"), p.get("agents_md"),
                                             bool(p.get("debug"))))
            elif self.path.startswith("/observe"):
                self._send(observe(p.get("text", ""), float(p.get("weight", 1.0)),
                                   p.get("url", ""), p.get("title", "")))
            elif self.path.startswith("/exclude"):
                self._send(ctx_exclude(p.get("action", "add"), p.get("value", "")))
            elif self.path.startswith("/forget"):
                self._send(ctx_forget(p.get("label", "")))
            elif self.path.startswith("/pin"):
                self._send(ctx_pin(p.get("label", ""), bool(p.get("pinned", True))))
            elif self.path.startswith("/drain"):
                _drain_now.set()
                self._send(_drain_once())
            else:
                self._send({"error": "not found"}, 404)
        except Exception as e:
            self._send({"error": str(e)}, 500)

    def log_message(self, fmt, *args):  # quiet
        pass


def main():
    CAPTURES.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_drainer_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"oracle-capture-receiver on http://127.0.0.1:{PORT}  "
          f"(captures -> {CAPTURES}, KB '{DATASET}', trafilatura={'yes' if _TRAFILATURA else 'NO (fallback)'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
