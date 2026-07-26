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
import urllib.request
from datetime import datetime, timezone
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RAGFLOW = os.environ.get("ORACLE_RAGFLOW_URL", "http://localhost:9380").rstrip("/")
KEY = os.environ.get("ORACLE_RAGFLOW_KEY", "ragflow-smywlJs3drgGxfKztifTmD3iNJ2lP6Uvq2-suiLQTGM")
OLLAMA = os.environ.get("ORACLE_OLLAMA_URL", "http://localhost:11434").rstrip("/")
SYNTH_MODEL = os.environ.get("ORACLE_SYNTH_MODEL", "qwen3-coder:30b")
RERANK_ID = os.environ.get("ORACLE_RERANK_ID", "gte-multilingual-reranker-base@local-gte-rerank@Jina")
HOME = Path.home()
CORPUS = Path(os.environ.get("ORACLE_CORPUS", str(HOME / "Projects/oracle/corpus"))).resolve()
CAPTURES = CORPUS / "inbox" / "captures"
PORT = int(os.environ.get("ORACLE_CAPTURE_PORT", "8788"))
DATASET = os.environ.get("ORACLE_CAPTURE_DATASET", "links")
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

_drain_now = threading.Event()
_ds_lock = threading.Lock()  # serialise dataset lookup/create so two captures don't double-create


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
    captured_at = payload.get("captured_at") or datetime.now(timezone.utc).isoformat()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stem = f"{ts}-{_slug(title or url)}"

    md_body = html_to_markdown(html) if html else ""
    header = f"# {title}\n\n> source: {url}\n> captured: {captured_at}\n\n---\n\n"
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
        "dataset": DATASET, "chunk_method": "naive",
        "status": "pending", "attempts": 0, "doc_id": None, "error": None,
    }
    (CAPTURES / f"{stem}.capture.json").write_text(json.dumps(record, indent=2))
    _drain_now.set()
    return {"ok": True, "stem": stem, "md": str(md_path),
            "pdf": bool(pdf_path), "md_chars": len(md_body)}


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


def _upload(md_path: Path, dsid: str) -> str | None:
    """Multipart upload of one .md, then kick off parsing. Returns doc_id, or None on duplicate."""
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
        return None  # duplicate name already present
    doc_id = docs[0]["id"]
    _ragflow("POST", f"/datasets/{dsid}/chunks", data={"document_ids": [doc_id]})
    return doc_id


def _drain_once() -> dict:
    jobs = sorted(CAPTURES.glob("*.capture.json")) if CAPTURES.exists() else []
    done = failed = 0
    for j in jobs:
        try:
            rec = json.loads(j.read_text())
        except Exception:
            continue
        if rec.get("status") != "pending":
            continue
        try:
            dsid = _dataset_id(rec["dataset"], rec["chunk_method"])
            doc_id = _upload(Path(rec["md"]), dsid)
            rec["status"], rec["doc_id"] = "done", doc_id
            done += 1
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            # RAGFlow unreachable (backend off / mid-flight) — leave pending, retry next pass
            return {"done": done, "failed": failed, "ragflow": False}
        except Exception as e:
            rec["attempts"] = rec.get("attempts", 0) + 1
            rec["error"] = str(e)[:300]
            if rec["attempts"] >= 5:
                rec["status"] = "failed"
                failed += 1
        j.write_text(json.dumps(rec, indent=2))
    return {"done": done, "failed": failed, "ragflow": True}


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


def _diversify(query: str, chunks: list, main: int = 18, cross: int = 4) -> list:
    q = _script(query)
    top = chunks[:main]
    other = [c for c in chunks[main:]
             if _script(c.get("content_with_weight") or c.get("content", "")) != q]
    return top + other[:cross]


def _chat(messages, timeout: int = 300) -> str:
    j = _req("POST", f"{OLLAMA}/v1/chat/completions", data={
        "model": SYNTH_MODEL, "stream": False, "messages": messages,
        "temperature": 0.1, "max_tokens": 2048}, timeout=timeout)
    return j["choices"][0]["message"]["content"]


def explain(selection: str, url: str = "", title: str = "") -> dict:
    kb_ids = _kb_ids()
    if not kb_ids:
        return {"answer": "The corpus has no parsed content yet.", "sources": []}
    chunks, reranked = _retrieve(selection, kb_ids)
    if not chunks:
        return {"answer": "The corpus doesn't cover this (no relevant passages retrieved).",
                "sources": [], "reranked": reranked}
    chunks = _diversify(selection, chunks)
    context = "\n\n".join(
        f"[{i+1}] (source: {c.get('document_keyword','?')})\n"
        f"{c.get('content_with_weight') or c.get('content','')}"
        for i, c in enumerate(chunks))
    system = (
        "You explain a term or passage the user selected while reading, using ONLY the provided "
        "documentation excerpts. Protocol: (1) note which excerpts are relevant; (2) explain the "
        "selection using ONLY facts present in them — every specific claim (names, flags, sizes, "
        "semantics, versions) must come from an excerpt, never your own knowledge; (3) cite the "
        "excerpt number/source for key claims; (4) if the excerpts do not explain it, reply exactly: "
        "'The corpus doesn't cover this.' Be concise (a few sentences). Tag code fences by language. "
        "Write your entire answer in the SAME language as the selection; never switch languages "
        "mid-answer.")
    where = f" (seen on: {title or url})" if (title or url) else ""
    user = f"Explain this selection{where}:\n\n\"\"\"\n{selection[:2000]}\n\"\"\"\n\nExcerpts:\n{context}"
    answer = _chat([{"role": "system", "content": system}, {"role": "user", "content": user}])
    sources = sorted({c.get("document_keyword", "?") for c in chunks})
    return {"answer": answer, "sources": sources, "reranked": reranked}


# ------------------------------------------------------------------ status

def status() -> dict:
    counts = {"pending": 0, "done": 0, "failed": 0}
    if CAPTURES.exists():
        for j in CAPTURES.glob("*.capture.json"):
            try:
                counts[json.loads(j.read_text()).get("status", "pending")] += 1
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
    return {"queue": counts, "ragflow": ragflow_ok, "synth": synth_ok,
            "dataset": DATASET, "captures_dir": str(CAPTURES), "port": PORT}


# ------------------------------------------------------------------ http server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
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

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/status"):
            self._send(status())
        elif self.path.startswith("/health"):
            self._send({"ok": True})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        try:
            if self.path.startswith("/capture"):
                self._send(save_capture(self._read_json()))
            elif self.path.startswith("/explain"):
                p = self._read_json()
                sel = (p.get("selection") or "").strip()
                if not sel:
                    self._send({"error": "empty selection"}, 400)
                    return
                self._send(explain(sel, p.get("url", ""), p.get("title", "")))
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
