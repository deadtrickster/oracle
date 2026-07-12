# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0", "multilspy", "requests"]
# ///
"""Oracle LSP MCP — wire the local model to real language servers (rust-analyzer,
clangd, gopls, pyright) for COMPILER-ACCURATE code facts, plus LLM actions grounded
in them. This is the "LSP for truth, LLM for intent" layer: exact hover types/enum
values, definitions, references, symbols (deterministic, from the compiler) and
explain/propose-improvement (qwen, grounded in the LSP-resolved code).

Fixes the class of error where a model miscopies a value table — hover asks the
compiler, which KNOWS `WAL_REC_REINSERT == 15`.

Tools:
  lsp_hover(file, line, col)            compiler type/value/doc at a position
  lsp_definition(file, line, col)       where a symbol is defined
  lsp_references(file, line, col)       all references (semantic, not grep)
  lsp_symbols(file)                     document symbols (functions/structs/enums…)
  lsp_code_actions(file, start, end)    the language server's OWN refactorings for a
                                        region (rust-analyzer/clangd/gopls assists:
                                        'Extract into function', 'Inline variable', …)
  suggest_refactor(file, start, end)    NEW: qwen reasons over the LSP's real action
                                        menu + the source and recommends what to do
  explain_code(file, start, end)        qwen explains the region, grounded in the source
  propose_improvement(file, start, end) qwen proposes improvements for the region

The "actions" model: the language server already offers deterministic, compiler-accurate
refactorings (lsp_code_actions). We do NOT replace those — we ADD new, LLM-backed actions
(explain_code / propose_improvement / suggest_refactor) over the same "do something to a
code region" idea. LSP for the safe mechanics; the LLM for intent the compiler can't judge.

file paths are absolute or ~/Projects-relative; line/col are 1-indexed (LSP is 0-indexed
internally). Language + project root are inferred from the path.
"""
import asyncio
import json
import os
import pathlib
import subprocess
import threading
import time
from pathlib import Path

import requests
from mcp.server.fastmcp import FastMCP
from multilspy import SyncLanguageServer
from multilspy.multilspy_config import MultilspyConfig
from multilspy.multilspy_logger import MultilspyLogger

PROJECTS = Path(os.environ.get("ORACLE_PROJECTS_ROOT", str(Path.home() / "Projects"))).resolve()
OLLAMA = os.environ.get("ORACLE_OLLAMA_URL", "http://localhost:11434")
SYNTH_MODEL = os.environ.get("ORACLE_SYNTH_MODEL", "qwen3-coder:30b")
EXT_LANG = {".rs": "rust", ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp",
            ".hpp": "cpp", ".go": "go", ".py": "python"}

CLANGD = os.environ.get("ORACLE_CLANGD", "clangd")
mcp = FastMCP("oracle-lsp")
_servers: dict = {}  # cache: (repo_root, lang) -> started server (multilspy or clangd client)


class ClangdClient:
    """Minimal raw LSP client over stdio for clangd — multilspy has no C/C++ server, but
    clangd is exactly what the user's Emacs (eglot) drives. Synchronous request/response
    via a background reader thread. Exposes the same request_* method names as multilspy's
    SyncLanguageServer so the tool bodies don't care which backend answers."""

    def __init__(self, root: Path):
        self.root = root
        args = [CLANGD, "--background-index", "--log=error", "-j=4"]
        if (root / "compile_commands.json").exists():
            args.append(f"--compile-commands-dir={root}")
        self.proc = subprocess.Popen(args, cwd=str(root), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self._id = 0
        self._wlock = threading.Lock()
        self._pending: dict = {}
        self._opened: set = set()
        threading.Thread(target=self._reader, daemon=True).start()
        self._request("initialize", {
            "processId": os.getpid(), "rootUri": root.as_uri(),
            "capabilities": {"textDocument": {"hover": {"contentFormat": ["markdown", "plaintext"]},
                                              "definition": {}, "references": {},
                                              "documentSymbol": {"hierarchicalDocumentSymbolSupport": False}},
                             "workspace": {"symbol": {}}}}, timeout=60)
        self._notify("initialized", {})

    def _frame(self, obj):
        data = json.dumps(obj).encode()
        with self._wlock:
            self.proc.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
            self.proc.stdin.flush()

    def _reader(self):
        f = self.proc.stdout
        while True:
            line = f.readline()
            if not line:
                return
            n = 0
            while line not in (b"\r\n", b"\n", b""):
                if b":" in line:
                    k, _, v = line.partition(b":")
                    if k.strip().lower() == b"content-length":
                        n = int(v.strip())
                line = f.readline()
            body = f.read(n) if n else b""
            try:
                msg = json.loads(body)
            except Exception:
                continue
            mid = msg.get("id")
            if mid is not None and mid in self._pending and "method" not in msg:
                ev, box = self._pending[mid]
                box.append(msg.get("result"))
                ev.set()
            elif mid is not None and "method" in msg:  # server->client request: reply null
                self._frame({"jsonrpc": "2.0", "id": mid, "result": None})

    def _request(self, method, params, timeout=30):
        with self._wlock:
            self._id += 1
            mid = self._id
        ev = threading.Event()
        box: list = []
        self._pending[mid] = (ev, box)
        self._frame({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        ok = ev.wait(timeout)
        self._pending.pop(mid, None)
        return box[0] if (ok and box) else None

    def _notify(self, method, params):
        self._frame({"jsonrpc": "2.0", "method": method, "params": params})

    def _open(self, rel):
        if rel in self._opened:
            return
        p = self.root / rel
        lid = "cpp" if p.suffix.lower() in (".cpp", ".cc", ".cxx", ".hpp") else "c"
        self._notify("textDocument/didOpen", {"textDocument": {
            "uri": p.as_uri(), "languageId": lid, "version": 1,
            "text": p.read_text(errors="replace")}})
        self._opened.add(rel)

    def _uri(self, rel):
        return (self.root / rel).as_uri()

    def request_hover(self, rel, line, col):
        self._open(rel)
        return self._request("textDocument/hover", {
            "textDocument": {"uri": self._uri(rel)}, "position": {"line": line, "character": col}})

    def request_definition(self, rel, line, col):
        self._open(rel)
        r = self._request("textDocument/definition", {
            "textDocument": {"uri": self._uri(rel)}, "position": {"line": line, "character": col}})
        return r if isinstance(r, list) else ([r] if r else [])

    def request_references(self, rel, line, col):
        self._open(rel)
        r = self._request("textDocument/references", {
            "textDocument": {"uri": self._uri(rel)}, "position": {"line": line, "character": col},
            "context": {"includeDeclaration": True}})
        return r or []

    def request_document_symbols(self, rel):
        self._open(rel)
        return self._request("textDocument/documentSymbol", {"textDocument": {"uri": self._uri(rel)}}) or []

    def request_workspace_symbol(self, query):
        # clangd serves this from its background index, which may still be warming on a
        # cold start — retry a couple times before giving up.
        for _ in range(3):
            r = self._request("workspace/symbol", {"query": query}, timeout=45)
            if r:
                return r
            time.sleep(2)
        return r or []

    def code_action(self, rel, s_line, s_col, e_line, e_col):
        self._open(rel)
        return self._request("textDocument/codeAction", {
            "textDocument": {"uri": self._uri(rel)},
            "range": {"start": {"line": s_line, "character": s_col},
                      "end": {"line": e_line, "character": e_col}},
            "context": {"diagnostics": []}}) or []


def _resolve(file: str) -> Path | None:
    p = Path(file)
    cand = p if p.is_absolute() else (PROJECTS / file)
    try:
        rp = cand.resolve()
        rp.relative_to(PROJECTS)
        return rp if rp.is_file() else None
    except Exception:
        return None


def _repo_root(f: Path, lang: str) -> Path:
    markers = {"rust": "Cargo.toml", "go": "go.mod", "python": "pyproject.toml"}
    marker = markers.get(lang)
    cur = f.parent
    while cur != PROJECTS.parent and str(cur).startswith(str(PROJECTS)):
        if marker and (cur / marker).exists():
            return cur
        if (cur / "compile_commands.json").exists() or (cur / ".git").exists():
            return cur
        cur = cur.parent
    return f.parent


def _server(root: Path, lang: str):
    key = (str(root), lang)
    if key not in _servers:
        if lang in ("c", "cpp"):
            _servers[key] = ClangdClient(root)  # multilspy has no C/C++; drive clangd directly
        else:
            cfg = MultilspyConfig.from_dict({"code_language": lang})
            lsp = SyncLanguageServer.create(cfg, MultilspyLogger(), str(root))
            lsp.start_server().__enter__()  # keep alive for the process lifetime
            _servers[key] = lsp
    return _servers[key]


def _prep(file: str):
    f = _resolve(file)
    if f is None:
        return None, None, None, f"error: not a file under ~/Projects: {file}"
    lang = EXT_LANG.get(f.suffix.lower())
    if not lang:
        return None, None, None, f"error: unsupported language for {f.suffix}"
    root = _repo_root(f, lang)
    rel = str(f.relative_to(root))
    return _server(root, lang), rel, f, None


def _qwen(system: str, user: str) -> str:
    r = requests.post(f"{OLLAMA}/api/chat", timeout=300, json={
        "model": SYNTH_MODEL, "stream": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": 0.1}})
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


def _lines(f: Path, start: int, end: int) -> str:
    ls = f.read_text(errors="replace").splitlines()
    start, end = max(1, start), min(len(ls), end)
    w = len(str(end))
    return "\n".join(f"{i:>{w}}\t{ls[i-1]}" for i in range(start, end + 1))


def _raw_code_actions(lsp: SyncLanguageServer, rel: str, f: Path,
                      start_line: int, end_line: int) -> list[dict]:
    """Send a raw textDocument/codeAction over the async bridge (multilspy doesn't
    wrap it). Range is the full span of the given 1-indexed lines. Returns the LSP's
    action list ({title, kind, …}); empty if none / server not ready."""
    src = f.read_text(errors="replace").splitlines()
    end_line = max(start_line, end_line)
    s0 = max(0, start_line - 1)
    e0 = min(len(src) - 1, end_line - 1) if src else 0
    e_col = len(src[e0]) if src and e0 < len(src) else 0
    if isinstance(lsp, ClangdClient):  # clangd backend: raw client already speaks codeAction
        return lsp.code_action(rel, s0, 0, e0, e_col)
    a = lsp.language_server
    uri = f.as_uri()

    async def _call():
        with a.open_file(rel):
            params = {
                "textDocument": {"uri": uri},
                "range": {"start": {"line": s0, "character": 0},
                          "end": {"line": e0, "character": e_col}},
                "context": {"diagnostics": []},
            }
            return await a.server.send.code_action(params)

    res = asyncio.run_coroutine_threadsafe(_call(), lsp.loop).result(timeout=60)
    return res or []


@mcp.tool()
def lsp_hover(file: str, line: int, col: int = 1) -> str:
    """Compiler-accurate type/value/documentation for the symbol at file:line:col
    (1-indexed). Use for EXACT facts a text search can't guarantee — a resolved type,
    an enum member's value, a function signature. This is ground truth from the
    language server, not a guess."""
    lsp, rel, f, err = _prep(file)
    if err:
        return err
    try:
        h = lsp.request_hover(rel, line - 1, col - 1)
    except Exception as e:
        return f"lsp error: {e}"
    if not h:
        return "no hover info at that position (check line/col; col is on the symbol)"
    contents = h.get("contents", h)
    if isinstance(contents, dict):
        contents = contents.get("value", str(contents))
    return str(contents)[:4000]


@mcp.tool()
def lsp_definition(file: str, line: int, col: int = 1) -> str:
    """Where the symbol at file:line:col is defined (semantic go-to-definition)."""
    lsp, rel, f, err = _prep(file)
    if err:
        return err
    try:
        d = lsp.request_definition(rel, line - 1, col - 1)
    except Exception as e:
        return f"lsp error: {e}"
    if not d:
        return "no definition found at that position"
    out = []
    for loc in d:
        r = loc.get("range", {}).get("start", {})
        out.append(f"{loc.get('relativePath', loc.get('uri',''))}:{r.get('line',0)+1}:{r.get('character',0)+1}")
    return "\n".join(out)


@mcp.tool()
def lsp_references(file: str, line: int, col: int = 1) -> str:
    """All references to the symbol at file:line:col (semantic — real uses, no
    false positives in strings/comments). Essential before a rename/refactor."""
    lsp, rel, f, err = _prep(file)
    if err:
        return err
    try:
        refs = lsp.request_references(rel, line - 1, col - 1)
    except Exception as e:
        return f"lsp error: {e}"
    if not refs:
        return "no references found"
    out = [f"{r.get('relativePath', r.get('uri',''))}:{r.get('range',{}).get('start',{}).get('line',0)+1}"
           for r in refs]
    return f"{len(out)} references:\n" + "\n".join(out[:80])


def _detect_lang(root: Path) -> str | None:
    if (root / "Cargo.toml").exists():
        return "rust"
    if (root / "go.mod").exists():
        return "go"
    if (root / "compile_commands.json").exists():
        cpp = sum(1 for _ in root.rglob("*.cpp"))
        return "cpp" if cpp > sum(1 for _ in root.rglob("*.c")) else "c"
    counts = {}
    for ext, lang in EXT_LANG.items():
        counts[lang] = counts.get(lang, 0) + sum(1 for _ in root.glob(f"**/*{ext}"))
    return max(counts, key=counts.get) if counts and max(counts.values()) else None


def _resolve_proj(project: str) -> Path | None:
    cand = (PROJECTS / project).resolve()
    if str(cand).startswith(str(PROJECTS)) and cand.is_dir():
        return cand
    prefix = str(PROJECTS).lstrip("/").replace("/", "-") + "-"
    rem = project[len(prefix):] if project.startswith(prefix) else project
    parts = rem.split("-")
    base = PROJECTS
    while parts:
        for k in range(len(parts), 0, -1):
            child = base / "-".join(parts[:k])
            if child.is_dir():
                base, parts = child, parts[k:]
                break
        else:
            return None
    return base if base != PROJECTS else None


@mcp.tool()
def lsp_workspace_symbol(query: str, project: str) -> str:
    """Find a symbol by NAME across a whole project, semantically (LSP
    workspace/symbol) — the compiler's index, not grep. Use this FIRST when looking
    for where a function/struct/type/enum is DEFINED: it returns real definitions
    (file:line + kind), self-limiting (no usage firehose). `project` is a path under
    ~/Projects (or a code-graph slug). NOTE: works only where the project is indexable
    (Cargo.toml, go.mod, or compile_commands.json) and finds only REAL symbols — macro-
    generated members (X-macro tables like WAL_REC_*/PG_RMGR) won't appear; fall back to
    source_search/ask_code for those. Empty result => degrade to grep."""
    root = _resolve_proj(project)
    if root is None:
        return f"error: project not found under ~/Projects: {project}"
    lang = _detect_lang(root)
    if not lang:
        return f"error: could not detect a language for {project}"
    if not any((root / m).exists() for m in ("Cargo.toml", "go.mod", "compile_commands.json")):
        return (f"note: {project} has no Cargo.toml/go.mod/compile_commands.json — LSP index will "
                f"be unreliable here; use source_search/ask_code (grep) instead. lang guess={lang}")
    try:
        lsp = _server(root, lang)
        syms = lsp.request_workspace_symbol(query)
    except Exception as e:
        return f"lsp error (workspace/symbol {lang}): {e}"
    if not syms:
        return f"no workspace symbols for '{query}' (degrade to source_search/ask_code)"
    out = []
    for s in syms[:60]:
        loc = s.get("location", {})
        rng = loc.get("range", {}).get("start", {})
        uri = loc.get("uri", "") or loc.get("relativePath", "")
        out.append(f"{s.get('name','?')}\t{s.get('kind','')}\t{uri.replace('file://','')}:{rng.get('line',0)+1}")
    return f"{len(out)} symbol(s) for '{query}':\n" + "\n".join(out)


@mcp.tool()
def lsp_symbols(file: str) -> str:
    """List the symbols defined in a file (functions, structs, enums, …) with their
    lines — a semantic outline. Use to navigate before hovering/explaining."""
    lsp, rel, f, err = _prep(file)
    if err:
        return err
    try:
        syms = lsp.request_document_symbols(rel)
    except Exception as e:
        return f"lsp error: {e}"
    flat = syms[0] if isinstance(syms, tuple) else syms
    if not flat:
        return "no symbols"
    out = []
    for s in flat:
        rng = s.get("range", s.get("location", {}).get("range", {})).get("start", {})
        out.append(f"{rng.get('line',0)+1}\t{s.get('kind','')}\t{s.get('name','')}")
    return "\n".join(out[:200])


@mcp.tool()
def lsp_code_actions(file: str, start_line: int, end_line: int = 0) -> str:
    """List the language server's OWN code actions / refactorings available for a code
    region — the deterministic, compiler-accurate assists your editor offers here
    (rust-analyzer: 'Extract into function', 'Inline variable', 'Replace let with if
    let', quick-fixes; clangd/gopls likewise). Read-only: returns the menu (title +
    kind), it does NOT apply them. Use this to SEE what safe mechanical refactors exist
    before deciding; pair with suggest_refactor to have the model recommend one."""
    lsp, rel, f, err = _prep(file)
    if err:
        return err
    if not end_line:
        end_line = start_line
    try:
        actions = _raw_code_actions(lsp, rel, f, start_line, end_line)
    except Exception as e:
        return f"lsp error: {e}"
    if not actions:
        return ("no code actions offered here (try a range that spans a full "
                "statement/expression; the server may still be indexing on a cold start)")
    out = []
    for it in actions:
        title = it.get("title", "?")
        kind = it.get("kind", it.get("command", {}).get("command", "") if isinstance(it.get("command"), dict) else "")
        out.append(f"- {title}" + (f"  [{kind}]" if kind else ""))
    return (f"{len(out)} code action(s) from the language server for "
            f"{rel} lines {start_line}-{end_line}:\n" + "\n".join(out))


@mcp.tool()
def suggest_refactor(file: str, start_line: int, end_line: int) -> str:
    """RECOMMEND a refactoring for a region, combining the language server's real code
    actions (the deterministic refactorings rust-analyzer/clangd/gopls actually offer
    here) with the model's judgment about intent. Returns: (1) the LSP's available
    actions verbatim, then (2) qwen's pick of which to apply and WHY, plus any extra
    source-grounded improvements the LSP can't propose. This is the "LSP for the safe
    mechanics, LLM for the intent" action: the model reasons over the SERVER's actual
    menu, not an imagined one. Suggestions only — it does not edit."""
    lsp, rel, f, err = _prep(file)
    if err:
        return err
    try:
        actions = _raw_code_actions(lsp, rel, f, start_line, end_line)
    except Exception as e:
        return f"lsp error: {e}"
    menu = [it.get("title", "?") for it in actions]
    menu_txt = "\n".join(f"- {m}" for m in menu) or "(the language server offered none for this range)"
    code = _lines(f, start_line, end_line)
    ans = _qwen(
        "You recommend a refactoring. You are given the code region AND the exact list of "
        "code actions the language server offers for it (these are safe, compiler-verified "
        "mechanical refactors). Protocol: (1) if one of the LISTED server actions best fits "
        "the intent, recommend it BY ITS EXACT TITLE and explain what it does and why it "
        "helps here; prefer these over hand edits because they are compiler-safe. (2) Then "
        "add any improvements the server can NOT offer (naming, structure, error handling, "
        "correctness) grounded ONLY in the shown lines. Be concrete and concise. If the code "
        "is already clean, say so.",
        f"File: {rel} lines {start_line}-{end_line}\n\nCode:\n{code}\n\n"
        f"Language-server code actions available here:\n{menu_txt}")
    return (f"Language-server actions for {rel} lines {start_line}-{end_line}:\n{menu_txt}\n\n"
            f"--- recommendation (qwen, grounded in the above) ---\n{ans}")


@mcp.tool()
def explain_code(file: str, start_line: int, end_line: int) -> str:
    """Explain what a region of code does. qwen explains, grounded in the ACTUAL
    source lines (read from disk) — not from memory. Good for 'what does this do'."""
    lsp, rel, f, err = _prep(file)
    if err:
        return err
    code = _lines(f, start_line, end_line)
    ans = _qwen(
        "Explain what this code does, concisely and precisely, grounded ONLY in the given lines. "
        "Reference exact identifiers. Do not invent behavior not visible in the code.",
        f"File: {rel} lines {start_line}-{end_line}\n{code}")
    return ans


@mcp.tool()
def propose_improvement(file: str, start_line: int, end_line: int) -> str:
    """Propose improvements/refactorings for a region (readability, safety, perf,
    idiom). qwen proposes, grounded in the actual source. Suggestions only — it does
    not edit. Complements the language server's mechanical refactors: for a specific
    compiler-safe refactor (extract/inline/rename) see lsp_code_actions, or use
    suggest_refactor to have the model choose from the server's actual menu; use
    lsp_references first to see the full impact of a change."""
    lsp, rel, f, err = _prep(file)
    if err:
        return err
    code = _lines(f, start_line, end_line)
    ans = _qwen(
        "Propose concrete improvements to this code — correctness, safety, readability, idiom, "
        "performance — grounded ONLY in the given lines. For each: what and why, with a short "
        "before/after if useful. Do not invent context you can't see; if it looks fine, say so.",
        f"File: {rel} lines {start_line}-{end_line}\n{code}")
    return ans


if __name__ == "__main__":
    mcp.run(transport="stdio")
