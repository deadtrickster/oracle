# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0", "requests"]
# ///
"""Oracle `ask_corpus` MCP tool — grounded doc Q&A as a single call.

Any caller (local Claude Code / qwen, gptel, the RAGFlow chat) invokes
ask_corpus(question) and gets back a GROUNDED answer with citations. The whole
anti-hallucination pipeline runs INSIDE the tool, deterministically, so a weak
caller cannot skip retrieval or hallucinate over raw chunks:

  1. retrieve top_k from all doc KBs (bge-m3 embeddings)
  2. rerank with gte-multilingual (falls back to embedding order if the CPU is
     busy / reranker times out — graceful during background indexing)
  3. extract-then-answer synthesis by qwen: answer ONLY from the retrieved text,
     quote facts, cite sources, or say the corpus doesn't cover it.

This is design-grounded-agent.md (#2) packaged as a callable primitive.
"""
import json
import os

import requests
from mcp.server.fastmcp import FastMCP

import re
import subprocess
from pathlib import Path

RAGFLOW = os.environ.get("ORACLE_RAGFLOW_URL", "http://localhost:9380")
KEY = os.environ.get("ORACLE_RAGFLOW_KEY", "ragflow-smywlJs3drgGxfKztifTmD3iNJ2lP6Uvq2-suiLQTGM")
OLLAMA = os.environ.get("ORACLE_OLLAMA_URL", "http://localhost:11434")
SYNTH_MODEL = os.environ.get("ORACLE_SYNTH_MODEL", "qwen3-coder:30b")
RERANK_ID = "gte-multilingual-reranker-base@local-gte-rerank@Jina"
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
RG = "/usr/bin/rg"
PROJECTS = Path(os.environ.get("ORACLE_PROJECTS_ROOT", str(Path.home() / "Projects"))).resolve()

mcp = FastMCP("oracle-ask")


def _doc_kb_ids() -> list[str]:
    r = requests.get(f"{RAGFLOW}/api/v1/datasets?page_size=100", headers=HDR, timeout=30)
    r.raise_for_status()
    # every KB with content; skip the meta KB from answers unless nothing else
    return [d["id"] for d in r.json()["data"] if d.get("chunk_count", 0) > 0]


def _retrieve(question: str, kb_ids: list[str], top_n: int = 8):
    body = {"question": question, "dataset_ids": kb_ids, "page_size": top_n,
            "top_k": 64, "similarity_threshold": 0.15}
    # try with reranker; on any failure (e.g. CPU busy -> 30s timeout) fall back
    for use_rerank in (True, False):
        b = dict(body)
        if use_rerank:
            b["rerank_id"] = RERANK_ID
        try:
            r = requests.post(f"{RAGFLOW}/api/v1/retrieval", headers=HDR, json=b, timeout=90)
            j = r.json()
            if j.get("code") == 0:
                return j["data"].get("chunks", []), use_rerank
        except Exception:
            pass
    return [], False


def _synthesize(question: str, chunks: list) -> str:
    context = "\n\n".join(
        f"[{i+1}] (source: {c.get('document_keyword','?')})\n{c.get('content_with_weight') or c.get('content','')}"
        for i, c in enumerate(chunks))
    system = (
        "You answer strictly from provided documentation excerpts. Protocol: (1) internally note "
        "the excerpts relevant to the question; (2) answer using ONLY facts present in them — every "
        "specific claim (names, flags, byte sizes, semantics, versions) must come from an excerpt, "
        "never from your own knowledge; (3) cite the excerpt number/source for key claims; (4) if "
        "the excerpts do not contain the answer, reply exactly: 'The corpus doesn't cover this.' "
        "Be concise. Tag code fences by language.")
    user = f"Question: {question}\n\nExcerpts:\n{context}"
    r = requests.post(f"{OLLAMA}/api/chat", timeout=300, json={
        "model": SYNTH_MODEL, "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "options": {"temperature": 0.1}})
    r.raise_for_status()
    return r.json()["message"]["content"]


@mcp.tool()
def ask_corpus(question: str) -> str:
    """Answer a documentation/API/concept question GROUNDED in the offline corpus
    (Rust, io_uring, Linux, Go, PostgreSQL/OrioleDB, Emacs, git/bash/glibc docs +
    books + papers). Use this for ANY factual/how-does-X-work question instead of
    answering from your own knowledge — it retrieves, reranks, and synthesizes an
    answer strictly from the corpus, with citations, or says the corpus doesn't
    cover it. Returns the grounded answer plus the sources it used."""
    try:
        kb_ids = _doc_kb_ids()
    except Exception as e:
        return f"error reaching corpus: {e}"
    if not kb_ids:
        return "The corpus has no parsed content yet."
    chunks, reranked = _retrieve(question, kb_ids)
    if not chunks:
        return "The corpus doesn't cover this (no relevant chunks retrieved)."
    answer = _synthesize(question, chunks)
    sources = sorted({c.get("document_keyword", "?") for c in chunks})
    tag = "reranked" if reranked else "embedding-order (reranker busy)"
    return f"{answer}\n\n---\nGrounded in [{tag}]: {', '.join(sources)}"


# --------------------------------------------------------------- ask_code

def _qwen(system: str, user: str) -> str:
    r = requests.post(f"{OLLAMA}/api/chat", timeout=300, json={
        "model": SYNTH_MODEL, "stream": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": 0.1}})
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


_NOISE_GLOBS = ["*.svg", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.webp", "*.bmp",
                "*.pdf", "*.min.js", "*.min.css", "*.map", "*.lock", "Cargo.lock",
                "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "*.snap"]


def _rg(pattern: str, root: Path, max_count: int = 8, context: int = 3) -> str:
    try:
        # --sort=path so include/ headers (where enums/macros/structs are DEFINED) come
        # before src/ usages — critical for "list the types" style questions. Exclude asset/
        # generated files (an .svg diagram's base64 matches "LSN.*=" and floods context) and
        # cap line width so a single minified/base64 line can't dominate the excerpt.
        cmd = [RG, "--line-number", "--no-heading", "--color", "never",
               "-i", "--sort", "path", "--max-count", str(max_count), "-C", str(context),
               "--max-columns", "300", "--max-columns-preview"]
        for g in _NOISE_GLOBS:
            cmd += ["--glob", f"!{g}"]
        cmd += ["--regexp", pattern, str(root)]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        return out.stdout
    except Exception:
        return ""


def _descend(base: Path, parts: list[str]) -> Path | None:
    """Match dash-split slug parts to nested dir names that may themselves contain
    dashes, with backtracking (longest child name first)."""
    if not parts:
        return base if base.is_dir() else None
    for k in range(len(parts), 0, -1):
        child = base / "-".join(parts[:k])
        if child.is_dir():
            hit = _descend(child, parts[k:])
            if hit:
                return hit
    return None


def _resolve_project(project: str) -> Path | None:
    """Accept either a path relative to ~/Projects ('orioledb/orioledb-postgres') OR a
    codebase-memory project slug ('home-dead-Projects-orioledb-orioledb-postgres', i.e.
    the absolute path with '/'->'-'). Returns the real dir under ~/Projects, or None."""
    # 1. direct relative path
    cand = (PROJECTS / project).resolve()
    if str(cand).startswith(str(PROJECTS)) and cand.exists():
        return cand
    # 2. codebase-memory slug: <abspath>.lstrip('/').replace('/','-')
    prefix = str(PROJECTS).lstrip("/").replace("/", "-") + "-"
    rem = project[len(prefix):] if project.startswith(prefix) else project
    return _descend(PROJECTS, rem.split("-")) if rem else None


@mcp.tool()
def ask_code(question: str, project: str = "") -> str:
    """Answer a SOURCE-CODE question GROUNDED in the actual code under ~/Projects
    (e.g. "what WAL record types does orioledb have", "how is struct X laid out",
    "where is Y implemented"). Use this for questions about a codebase's OWN source —
    exact structs/enums/macros/functions — which the documentation corpus (ask_corpus)
    does NOT contain. It derives search patterns, greps the source, reads the matches,
    and synthesizes an answer with file:line citations, or says it's not in the source.
    `project` optionally scopes to one repo under ~/Projects — either a relative path
    ("orioledb/orioledb-postgres") OR a codebase-memory project id
    ("home-dead-Projects-orioledb-orioledb-postgres"); empty = search all projects."""
    root = PROJECTS
    if project:
        cand = _resolve_project(project)
        if cand is None:
            return f"error: project not found under ~/Projects (tried path + code-graph id): {project}"
        root = cand
    # 1. derive grep patterns from the natural-language question
    try:
        raw = _qwen(
            "You turn a code question into ripgrep search patterns. Output ONLY 1-4 patterns, "
            "one per line, no prose. Prefer exact identifiers/symbols/macros likely in the source "
            "(e.g. WAL_REC, XLogInsert, struct FooBar). Patterns are case-insensitive regex.",
            f"Question: {question}")
        patterns = [p.strip() for p in raw.splitlines() if p.strip() and len(p.strip()) < 80][:4]
    except Exception as e:
        return f"error deriving search terms: {e}"
    if not patterns:
        patterns = [re.sub(r"[^\w]+", ".*", question)[:60]]
    # 2. grep the source
    hits, seen = [], 0
    for p in patterns:
        h = _rg(p, root)
        if h:
            hits.append(f"# matches for /{p}/:\n{h}")
            seen += h.count("\n")
        if seen > 700:
            break
    blob = "\n\n".join(hits)[:16000]
    if not blob.strip():
        return f"Not found in the source under {root.name} (patterns tried: {patterns})."
    # 3. synthesize grounded answer
    ans = _qwen(
        "Answer the question using ONLY these source-code excerpts (file:line prefixed). Quote "
        "exact identifiers/values/struct fields VERBATIM and cite file:line. CRITICAL: read every "
        "numeric value LITERALLY from the source — in a table like X(NAME, 7, ...) or NAME = 7, the "
        "code is exactly that number (the literal argument/RHS), NEVER a sequential position; do "
        "not renumber, reorder, infer, or skip entries. If the question asks for an enumeration "
        "(types, codes, fields, flags), find the DEFINITION (enum / #define / X-macro table / "
        "struct) and reproduce EVERY entry exactly as written. If the excerpts don't answer it, say "
        "'Not found in the source.' Never invent.",
        f"Question: {question}\n\nSource excerpts:\n{blob}")
    # Attach the raw definition-ish lines so the caller can verify qwen's synthesis (qwen can
    # misread value tables even when grounded). Prefer header lines with a literal number.
    raw_lines = [ln for ln in blob.splitlines()
                 if re.search(r"\.h[:-]\d+[:-].*(=\s*\d+|,\s*\d+\s*,|#define)", ln)][:40]
    verify = ("\n\nRAW SOURCE (verify against this — authoritative over the summary):\n"
              + "\n".join(raw_lines)) if raw_lines else ""
    return (f"{ans}{verify}\n\n---\nGrounded in source under: "
            f"{root.relative_to(PROJECTS.parent)} (patterns: {', '.join(patterns)})")


if __name__ == "__main__":
    mcp.run(transport="stdio")
