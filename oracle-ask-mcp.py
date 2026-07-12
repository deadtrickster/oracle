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


def _script(s: str) -> str:
    """Rough script of a text: 'cyr' if it has a run of Cyrillic, else 'lat'."""
    return "cyr" if re.search(r"[а-яА-ЯёЁ]{4,}", s or "") else "lat"


def _diversify(question: str, chunks: list, main: int = 8, cross: int = 4) -> list:
    """Keep the top `main` reranked chunks, then reserve up to `cross` slots for chunks in a
    DIFFERENT script than the query. Same-language sources tend to win the top ranks, so a query
    in one language can crowd out relevant content in another (an English PG question burying the
    Russian PG books at ranks 9-20). This guarantees cross-language content a seat when it's in
    the reranked pool — language-agnostic, no per-topic hardcoding."""
    q = _script(question)
    top = chunks[:main]
    other = [c for c in chunks[main:]
             if _script(c.get("content_with_weight") or c.get("content", "")) != q]
    return top + other[:cross]


def _retrieve(question: str, kb_ids: list[str], top_n: int = 20):
    # retrieve a larger reranked POOL (cheap: rerank cost is set by top_k, not page_size) so
    # _diversify can pull cross-language chunks that rank just below the top few.
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
    chunks = _diversify(question, chunks)  # top few + reserved cross-language slots
    answer = _synthesize(question, chunks)
    sources = sorted({c.get("document_keyword", "?") for c in chunks})
    tag = "reranked" if reranked else "embedding-order (reranker busy)"
    return f"{answer}\n\n---\nGrounded in [{tag}]: {', '.join(sources)}"


# --------------------------------------------------------------- ask_code

def _qwen(system: str, user: str, timeout: int = 240) -> str:
    r = requests.post(f"{OLLAMA}/api/chat", timeout=timeout, json={
        "model": SYNTH_MODEL, "stream": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": 0.1}})
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


_STOP = {"what", "does", "do", "the", "list", "each", "with", "its", "and", "for", "how",
         "is", "are", "of", "define", "defines", "defined", "new", "type", "types", "record",
         "records", "code", "codes", "numeric", "struct", "fields", "format", "have", "has",
         "in", "a", "an", "on", "disk", "layout", "value", "values", "where", "which", "that"}


def _heuristic_patterns(question: str) -> list[str]:
    """Cheap pattern extraction when the LLM derivation times out — pull identifier-ish
    tokens (snake_case / CamelCase / ALLCAPS) from the question."""
    toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", question)
    ident = [t for t in toks if ("_" in t or any(c.isupper() for c in t)) and t.lower() not in _STOP]
    return (ident or [t for t in toks if t.lower() not in _STOP])[:4]


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


GRAPH_URL = os.environ.get("ORACLE_GRAPH_URL", "http://localhost:9750/sse")
_graph_projects_cache: list | None = None


def _graph_call(tool: str, args: dict, timeout: int = 45) -> str:
    """Call a codebase-memory graph tool over its SSE bridge; '' on any failure so the
    caller degrades to grep. Runs its own event loop (ask_code runs in a worker thread)."""
    import asyncio
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async def _run():
        async with sse_client(GRAPH_URL) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await asyncio.wait_for(s.call_tool(tool, args), timeout)
                return res.content[0].text if res.content else ""
    try:
        return asyncio.run(_run())
    except RuntimeError:  # unexpectedly inside a loop -> isolate in a fresh thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(1) as ex:
            return ex.submit(lambda: asyncio.run(_run())).result(timeout + 10)
    except Exception:
        return ""


def _graph_projects() -> list:
    """Cached list of (slug, root_path) for indexed graph projects."""
    global _graph_projects_cache
    if _graph_projects_cache is None:
        try:
            data = json.loads(_graph_call("list_projects", {}, timeout=20) or "{}")
            _graph_projects_cache = [(p.get("name"), p.get("root_path", "")) for p in data.get("projects", [])]
        except Exception:
            _graph_projects_cache = []
    return _graph_projects_cache


def _graph_has(slug: str) -> bool:
    return any(n == slug for n, _ in _graph_projects())


def _nested_slugs(root: Path, main_slug: str | None) -> list[str]:
    """Indexed graph projects physically UNDER `root` (e.g. git submodules) — so a query
    scoped to a parent repo also reaches an embedded engine indexed as its own project
    (serenedb → its vendored duckdb)."""
    rp = str(root).rstrip("/") + "/"
    return [n for n, p in _graph_projects() if p and p.rstrip("/").startswith(rp) and n != main_slug]


def _graph_tier(slug: str, question: str, patterns: list[str]) -> str:
    """Semantic tier: search_graph (NL discovery) → get_code_snippet (the actual BODIES of
    the top hits, so the model reads real logic not just signatures) + search_code (catches
    macro/text the node index misses). Returns source-ish context."""
    out, snippets, nodes, seen = [], [], [], set()

    def _add(res):
        for n in res or []:
            qn = n.get("qualified_name")
            if qn and qn not in seen:
                seen.add(qn)
                nodes.append(n)
    try:
        # (1) BM25 on the question + (2) semantic vector search (bridges vocabulary — needs a
        # moderate/full index; empty on a fast index) + (3) name_pattern from the derived terms
        # (catches e.g. *Shred* nodes the question doesn't name lexically).
        sq = [w for w in re.findall(r"[a-zA-Z]{4,}", question.lower()) if w not in _STOP][:6]
        g = json.loads(_graph_call("search_graph", {"project": slug, "query": question,
                                                    "semantic_query": sq, "limit": 8}) or "{}")
        _add(g.get("results"))
        _add(g.get("semantic_results"))
        if patterns:
            npat = ".*(" + "|".join(p.strip("^$ ") for p in patterns[:4] if p.strip()) + ").*"
            _add(json.loads(_graph_call("search_graph", {"project": slug, "name_pattern": npat,
                                                        "limit": 8}) or "{}").get("results"))
        nodes = nodes[:12]
        for n in nodes:
            sig = (n.get("signature") or "").replace("\n", " ")[:120]
            doc = (n.get("docstring") or "").replace("\n", " ").strip()[:90]
            out.append(f"{n.get('label','')} {n.get('name','')}{sig} -> {n.get('return_type','')}"
                       f"  [{n.get('file_path','')}]" + (f"  // {doc}" if doc else ""))
        for n in nodes[:3]:  # pull the real source of the top-ranked hits
            qn = n.get("qualified_name")
            if not qn:
                continue
            src = _graph_call("get_code_snippet", {"project": slug, "qualified_name": qn}, timeout=25)
            if src and not src.strip().startswith("{"):
                snippets.append(f"# source of {n.get('name','')} [{n.get('file_path','')}]:\n{src[:1500]}")
    except Exception:
        pass
    if patterns:
        sc = _graph_call("search_code", {"project": slug, "pattern": patterns[0],
                                         "regex": True, "mode": "compact", "limit": 8})
        if sc and not sc.strip().startswith("{"):
            out.append("# search_code:\n" + sc[:2000])
    body = "\n".join(x for x in out if x.strip())
    if snippets:
        body += "\n\n" + "\n\n".join(snippets)
    return body


@mcp.tool()
def ask_code(question: str, project: str = "") -> str:
    """Answer a SOURCE-CODE question GROUNDED in the actual code under ~/Projects
    (e.g. "what WAL record types does orioledb have", "how is struct X laid out",
    "where is Y implemented"). Use this for questions about a codebase's OWN source —
    exact structs/enums/macros/functions — which the documentation corpus (ask_corpus)
    does NOT contain. It runs a graceful-degradation search — the semantic code graph
    (when the project is indexed) PLUS anchored grep — reads the matches, and synthesizes
    with file:line citations + a RAW SOURCE block, or says it's not in the source.
    ALWAYS pass `project` when you know which repo the question is about: it enables the
    fast semantic tier and keeps grep scoped (an all-repo search is slow and may time out).
    `project` is a relative path ("orioledb/orioledb-postgres") OR a codebase-memory id
    ("home-dead-Projects-orioledb-orioledb-postgres"); empty = grep all repos (grep only)."""
    root = PROJECTS
    slug = None
    if project:
        cand = _resolve_project(project)
        if cand is None:
            return f"error: project not found under ~/Projects (tried path + code-graph id): {project}"
        root = cand
        slug = str(cand).lstrip("/").replace("/", "-")  # code-graph project id
    # 1. derive grep patterns from the natural-language question
    try:
        raw = _qwen(
            "Turn the user's code question into 2-5 case-insensitive ripgrep regex patterns that "
            "locate the relevant DEFINITION(S) in source. Output ONLY the patterns, one per line, "
            "no prose. CRITICAL: derive every pattern from THIS question's own terms — the specific "
            "types, functions, macros, or concepts it names or implies. NEVER output a generic "
            "placeholder ('Name', 'FooBar') or an example token from these instructions; always "
            "substitute the real identifier from the question. Shape guidance: for a struct/enum/"
            "typedef use 'struct <RealName>' / 'enum <RealName>'; if it asks to LIST entries likely "
            "defined by an X-macro/table, include that table's invocation '<RealMacro>\\('; otherwise "
            "use the distinctive identifier or its prefix. No line-start '^' anchor (defs are often "
            "indented).",
            f"Question: {question}", timeout=120)
        bad = {"name", "foobar", "realname", "realmacro"}
        patterns = [p.strip() for p in raw.splitlines()
                    if p.strip() and len(p.strip()) < 80
                    and not any(b in p.strip().lower() for b in bad)][:5]
    except Exception:
        patterns = []  # LLM slow/unavailable -> fall back to heuristics, never hard-fail
    if not patterns:
        patterns = _heuristic_patterns(question) or [re.sub(r"[^\w]+", ".*", question)[:60]]
    # 2. TIER 1 — code graph (semantic; works for C/C++ where LSP can't). Only when the scoped
    #    project is actually indexed; graph search needs a project, so skip it for all-repo asks.
    graph_ctx = ""
    if slug and _graph_has(slug):
        graph_ctx = _graph_tier(slug, question, patterns)
    if slug:  # also consult indexed submodules under this repo (e.g. serenedb's duckdb)
        for ns in _nested_slugs(root, slug):
            extra = _graph_tier(ns, question, patterns)
            if extra:
                graph_ctx += f"\n# nested project {ns}:\n{extra}"
    # 3. TIER 2 — anchored grep (the floor; catches X-macro tables and un-indexed repos)
    hits, seen = [], 0
    for p in patterns:
        h = _rg(p, root)
        if h:
            hits.append(f"# matches for /{p}/:\n{h}")
            seen += h.count("\n")
        if seen > 700:
            break
    blob = "\n\n".join(hits)[:16000]
    if not graph_ctx and not blob.strip():
        base = f"Not found in the source under {root.name}" if project else "Not found in the source"
        return f"{base} (patterns tried: {patterns})."
    # 4. synthesize from whichever tier(s) produced signal
    context = ""
    if graph_ctx:
        context += "# CODE GRAPH (semantic — functions/structs + their source, file:line):\n" + graph_ctx[:12000] + "\n\n"
    if blob.strip():
        context += "# GREP (raw source lines):\n" + blob
    # Stage 1 — DISTILL: a focused reduce over the raw gathered context, keeping only evidence
    # relevant to THIS question. Works around weak-model focus + context length: the retrieval
    # drags in noise (unrelated std::variant/library hits), and a single-pass answer anchors on
    # it. This pass throws the noise away and quotes the KEY code (a gate/flag, a sort/order, a
    # size check) with file:line, so the answer pass reasons over signal only.
    focused = context
    try:
        focused = _qwen(
            "You are given raw code excerpts (a semantic graph view with function SOURCE bodies, "
            "and grep hits). Extract ONLY what is directly relevant to answering the question. Quote "
            "the exact relevant lines WITH their file:line. DISCARD unrelated matches — e.g. generic "
            "std::variant/library usage that is not the feature asked about, unrelated files. When a "
            "relevant function body reveals the KEY logic (a size/threshold check, an ordering or "
            "sort, a gate/flag, an enum/table of values), quote that specific code verbatim. Output "
            "only the extracted evidence (with file:line), no conclusion yet. If truly nothing is "
            "relevant, output 'NONE'.",
            f"Question: {question}\n\n{context}", timeout=150)
        if len(focused.strip()) < 40 or focused.strip() == "NONE":
            focused = context  # distilled to nothing -> fall back to raw context
    except Exception:
        focused = context
    # Stage 2 — ANSWER from the distilled evidence only
    try:
        ans = _qwen(
            "Answer the question using ONLY this evidence (file:line prefixed). Quote exact "
            "identifiers/values/fields VERBATIM and cite file:line. CRITICAL: read every numeric "
            "value LITERALLY — in a table like X(NAME, 7, ...) or NAME = 7 the code is exactly that "
            "number (the literal argument/RHS), NEVER a sequential position; do not renumber, "
            "reorder, infer, or skip entries. For an enumeration (types/codes/fields/flags) "
            "reproduce EVERY entry exactly. If the evidence shows a nuance (e.g. order is NOT "
            "preserved, a feature is size-gated, a decoder is missing), state it plainly and cite "
            "the exact code. If the evidence doesn't answer it, say 'Not found in the source.' "
            "Never invent.",
            f"Question: {question}\n\nRelevant evidence:\n{focused}")
    except Exception:
        ans = ("(synthesis model timed out — grounded evidence below; read it literally)\n\n"
               + focused[:6000])
    # Attach the raw definition-ish lines so the caller can verify qwen's synthesis (qwen can
    # misread value tables even when grounded). Prefer header lines with a literal number.
    raw_lines = [ln for ln in blob.splitlines()
                 if re.search(r"\.h[:-]\d+[:-].*(=\s*\d+|,\s*\d+\s*,|#define)", ln)][:40]
    verify = ("\n\nRAW SOURCE (verify against this — authoritative over the summary):\n"
              + "\n".join(raw_lines)) if raw_lines else ""
    tiers = ("code-graph+grep" if graph_ctx and blob.strip()
             else "code-graph" if graph_ctx else "grep")
    return (f"{ans}{verify}\n\n---\nGrounded [{tiers}] in source under: "
            f"{root.relative_to(PROJECTS.parent)} (patterns: {', '.join(patterns)})")


if __name__ == "__main__":
    mcp.run(transport="stdio")
