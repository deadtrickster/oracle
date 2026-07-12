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
import os
import pathlib
import subprocess
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

mcp = FastMCP("oracle-lsp")
_servers: dict[str, SyncLanguageServer] = {}  # cache: repo_root -> started server


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


def _server(root: Path, lang: str) -> SyncLanguageServer:
    key = str(root)
    if key not in _servers:
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
