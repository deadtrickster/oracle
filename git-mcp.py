# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0"]
# ///
"""Git READ-ONLY MCP server for Oracle: history/blame/diff/show over any repo
under ~/Projects. Strictly read-only — only whitelisted, non-mutating git
subcommands are ever run; no commit/checkout/reset/clean/push/config-write.

Run via mcp-proxy (stdio -> SSE) so RAGFlow can reach it.
"""
import os
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

GIT = "/usr/bin/git"
PROJECTS_ROOT = Path(os.environ.get("ORACLE_PROJECTS_ROOT", str(Path.home() / "Projects"))).resolve()
MAX = 12000

mcp = FastMCP("git")


def _repo(path: str) -> Path | None:
    p = (PROJECTS_ROOT / path) if not Path(path).is_absolute() else Path(path)
    try:
        rp = p.resolve()
        rp.relative_to(PROJECTS_ROOT)
    except Exception:
        return None
    # walk up to the enclosing git repo
    cur = rp if rp.is_dir() else rp.parent
    while cur != PROJECTS_ROOT.parent and str(cur).startswith(str(PROJECTS_ROOT)):
        if (cur / ".git").exists():
            return cur
        cur = cur.parent
    return None


def _git(repo: Path, args: list[str]) -> str:
    try:
        out = subprocess.run([GIT, "-C", str(repo), *args],
                             capture_output=True, text=True, timeout=25)
    except Exception as e:
        return f"error running git: {e}"
    return (out.stdout or out.stderr).strip()[:MAX]


@mcp.tool()
def git_log(path: str, max_count: int = 20, file: str = "") -> str:
    """Recent commit history for a repo (or a specific file within it). `path` is
    a repo path under ~/Projects (e.g. "serenedb/serenedb"); `file` optionally
    scopes to one file. Read-only."""
    repo = _repo(path)
    if not repo:
        return f"error: no git repo under ~/Projects for: {path}"
    args = ["log", f"-n{max_count}", "--pretty=format:%h %ad %an  %s", "--date=short"]
    if file:
        args += ["--", file]
    return _git(repo, args)


@mcp.tool()
def git_show(path: str, ref: str) -> str:
    """Show a commit (message + diff) or an object by ref/hash. `ref` e.g. a short
    hash, "HEAD", "HEAD~3". Read-only."""
    repo = _repo(path)
    if not repo:
        return f"error: no git repo under ~/Projects for: {path}"
    return _git(repo, ["show", "--stat", "--patch", ref])


@mcp.tool()
def git_blame(path: str, file: str, start: int = 1, end: int = 60) -> str:
    """Blame lines [start,end] of `file` in the repo at `path` — who last changed
    each line and in which commit. Read-only."""
    repo = _repo(path)
    if not repo:
        return f"error: no git repo under ~/Projects for: {path}"
    return _git(repo, ["blame", "-L", f"{start},{end}", "--", file])


@mcp.tool()
def git_diff(path: str, ref: str = "", file: str = "") -> str:
    """Diff. With no `ref`: unstaged working-tree changes. With `ref` (e.g.
    "HEAD~1", "main"): diff against it. `file` optionally scopes. Read-only."""
    repo = _repo(path)
    if not repo:
        return f"error: no git repo under ~/Projects for: {path}"
    args = ["diff"]
    if ref:
        args.append(ref)
    if file:
        args += ["--", file]
    return _git(repo, args)


@mcp.tool()
def git_status(path: str) -> str:
    """Short working-tree status + current branch for the repo at `path`. Read-only."""
    repo = _repo(path)
    if not repo:
        return f"error: no git repo under ~/Projects for: {path}"
    return _git(repo, ["status", "--short", "--branch"])


if __name__ == "__main__":
    mcp.run(transport="stdio")
