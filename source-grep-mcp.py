# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0"]
# ///
"""Source-grep MCP server for Oracle: list_projects + ripgrep + read_lines over
ALL repositories under ~/Projects. Project-agnostic.

Fills the codebase-memory gap where a symbol (esp. a struct/typedef in a header)
is known by file:line but has no graph node, AND works on any project whether or
not it has been indexed into the code graph.

Run via mcp-proxy (stdio -> SSE) so RAGFlow can reach it. Reads are confined to
PROJECTS_ROOT; every path is realpath-resolved and rejected if it escapes.
"""
import os
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

RG = "/usr/bin/rg"
PROJECTS_ROOT = Path(os.environ.get("ORACLE_PROJECTS_ROOT", str(Path.home() / "Projects"))).resolve()

# Non-source / asset / generated files ripgrep treats as text but which are noise for a
# code search — e.g. an .svg diagram whose base64 payload matches "LSN.*=" and floods the
# caller's context. Excluded by default; an explicit `glob` include still overrides intent.
_NOISE_GLOBS = ["*.svg", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.webp", "*.bmp",
                "*.pdf", "*.min.js", "*.min.css", "*.map", "*.lock", "Cargo.lock",
                "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "*.snap",
                # i18n / generated: a search for a type name matches every translated
                # msgstr in a .po and floods the caller (seen: XLogRecPtr in de.po).
                "*.po", "*.pot", "*.mo", "**/po/**"]

mcp = FastMCP("source-grep")


def _safe(p: Path) -> Path | None:
    try:
        rp = p.resolve()
    except Exception:
        return None
    try:
        rp.relative_to(PROJECTS_ROOT)
        return rp
    except ValueError:
        return None


@mcp.tool()
def list_projects() -> str:
    """List the source repositories available under ~/Projects (git repos, plus
    notable top-level dirs). Use this to discover what code you can search/read,
    and to get the exact directory path to pass to source_search's `path` filter
    or to the code-graph tools. The code-graph project name for a repo is its path
    with slashes turned to dashes, e.g. /home/dead/Projects/foo/bar ->
    home-dead-Projects-foo-bar."""
    repos = set()
    try:
        out = subprocess.run(
            ["find", str(PROJECTS_ROOT), "-maxdepth", "4", "-name", ".git", "-type", "d"],
            capture_output=True, text=True, timeout=20)
        for line in out.stdout.splitlines():
            repos.add(str(Path(line).parent))
    except Exception as e:
        return f"error listing projects: {e}"
    rows = []
    for r in sorted(repos):
        rel = Path(r).relative_to(PROJECTS_ROOT)
        # codebase-memory project name = absolute path, leading slash dropped, slashes->dashes
        proj = str(Path(r)).lstrip("/").replace("/", "-")
        rows.append(f"{rel}\t(graph project: {proj})")
    return f"Repositories under {PROJECTS_ROOT}:\n" + "\n".join(rows)


@mcp.tool()
def source_search(pattern: str, path: str = "", glob: str = "", max_count: int = 40, context: int = 3) -> str:
    """Ripgrep source code under ~/Projects for `pattern` (a regex).

    Works on ANY project, indexed or not. Use this to find EXACT definitions the
    code graph misses — struct/typedef bodies, macros, enum members, constants —
    and their file:line.
    - `path`: optional sub-path under ~/Projects to scope the search (e.g.
      "serenedb/serenedb" or "rocksdb"); empty = all projects.
    - `glob`: optional file filter (e.g. "*.h", "*.rs", "*.go").
    - `context`: lines of context around each match.

    To find a DEFINITION, ANCHOR the pattern rather than searching a bare identifier
    (a bare type/function name matches thousands of usages). The name is the last token
    of its declaration, so anchor its END: 'typedef.*XLogRecPtr;' (trailing ';' lands on
    `typedef uint64 XLogRecPtr;`, not the many function-pointer typedefs that merely take
    it as a param), '} FooBar;' for a struct, 'enum Kind', '#define MAX', 'FooBar\\s*='.
    If a search is too broad, this returns a per-file match-count summary (not a usage
    dump) so you can narrow. Follow up with read_lines for full definition bodies.
    """
    target = PROJECTS_ROOT
    if path:
        t = _safe(PROJECTS_ROOT / path)
        if t is None or not t.exists():
            return f"error: path not under ~/Projects or missing: {path}"
        target = t
    noise = []
    for g in _NOISE_GLOBS:
        noise += ["--glob", f"!{g}"]
    inc = ["--glob", glob] if glob else []

    # Broadness guard: a bare common identifier (e.g. "XLogRecPtr") matches thousands of
    # USAGES across the tree; the char cap then returns only the alphabetically-first
    # usages and buries the DEFINITION. Probe per-file counts first; if the search is too
    # broad, return a compact summary that steers toward the definition instead of a
    # firehose of usages.
    try:
        probe = subprocess.run([RG, "-c", "--color", "never", *noise, *inc,
                                "--regexp", pattern, str(target)],
                               capture_output=True, text=True, timeout=30)
    except Exception as e:
        return f"error running ripgrep: {e}"
    counts = []
    for ln in probe.stdout.splitlines():
        p, _, n = ln.rpartition(":")
        if n.isdigit():
            counts.append((int(n), p.replace(str(PROJECTS_ROOT) + "/", "")))
    total = sum(n for n, _ in counts)
    if total == 0:
        return "no matches"
    if total > 150 or len(counts) > 30:
        counts.sort(reverse=True)
        top = "\n".join(f"  {n:>5}  {p}" for n, p in counts[:20])
        return (f"Broad match: {total} matches in {len(counts)} files — refine rather than "
                f"dump usages. To find a DEFINITION, anchor the NAME where it is declared "
                f"(the identifier is the last token, so anchor its end): e.g. "
                f"'typedef.*{pattern};' (note trailing ';'), '}} {pattern};' for a struct, "
                f"'enum {pattern}', '#define {pattern}', or '{pattern}\\s*='. "
                f"You can also add a `glob` (e.g. \"*.h\") or scope `path` to a subdir. "
                f"Files with the most matches:\n{top}")

    # Focused enough — return the actual snippets. --max-columns truncates a single very
    # long line (base64/minified) to a preview so one line can't dominate the excerpt.
    cmd = [RG, "--line-number", "--no-heading", "--color", "never",
           "--max-count", str(max_count), "-C", str(context),
           "--max-columns", "300", "--max-columns-preview", *noise, *inc,
           "--regexp", pattern, str(target)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        return f"error running ripgrep: {e}"
    text = out.stdout.strip()
    if not text:
        return "no matches"
    text = text.replace(str(PROJECTS_ROOT) + "/", "")
    return text[:12000]


@mcp.tool()
def read_lines(path: str, start: int, end: int) -> str:
    """Read lines [start, end] (1-indexed, inclusive) from a source file under
    ~/Projects. `path` may be absolute or root-relative (e.g.
    "serenedb/serenedb/src/foo.cpp"). Use after source_search / search_code
    reports a file:line for a definition you need verbatim.
    """
    p = Path(path)
    cand = p if p.is_absolute() else (PROJECTS_ROOT / path)
    rp = _safe(cand)
    if rp is None or not rp.is_file():
        return f"error: path not under ~/Projects or not a file: {path}"
    try:
        lines = rp.read_text(errors="replace").splitlines()
    except Exception as e:
        return f"error reading file: {e}"
    start = max(1, start)
    end = min(len(lines), max(start, end))
    width = len(str(end))
    rel = rp.relative_to(PROJECTS_ROOT)
    body = "\n".join(f"{i:>{width}}\t{lines[i-1]}" for i in range(start, end + 1))
    return f"{rel} lines {start}-{end}:\n{body}"[:12000]


if __name__ == "__main__":
    mcp.run(transport="stdio")
