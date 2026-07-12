# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0"]
# ///
"""Emacs READ-ONLY MCP server for Oracle.

Lets the assistant SEE what you are working on in your running Emacs — list
buffers, read a buffer's text, read around the cursor. It NEVER writes, evaluates
model-supplied elisp, or changes Emacs state: every tool builds a fixed, read-only
elisp expression internally and only interpolates a safely-escaped buffer name.

Run via mcp-proxy (stdio -> SSE) so RAGFlow can reach it.
"""
import subprocess
from mcp.server.fastmcp import FastMCP

EMACSCLIENT = "/usr/local/bin/emacsclient"
MAX_CHARS = 60000

mcp = FastMCP("emacs")


def _elisp_string(s: str) -> str:
    """Escape a Python str into an elisp string literal — prevents any injection
    through a buffer name."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _eval(elisp: str, timeout: int = 8) -> str:
    """Run a FIXED read-only elisp expression against the user's Emacs."""
    try:
        out = subprocess.run([EMACSCLIENT, "--eval", elisp],
                             capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return "error: emacsclient not found"
    except subprocess.TimeoutExpired:
        return "error: emacs did not respond (is the server running?)"
    if out.returncode != 0:
        return f"error: {out.stderr.strip() or 'emacsclient failed'}"
    return out.stdout


def _unquote(s: str) -> str:
    """emacsclient prints elisp return values; strings come back quoted with
    escapes. Decode the common cases."""
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return s.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')


@mcp.tool()
def emacs_list_buffers() -> str:
    """List the user's current Emacs buffers: name, associated file (if any), and
    major mode. Use this to see what they're working on before reading a buffer."""
    elisp = (
        '(mapconcat (lambda (b) (with-current-buffer b '
        '(format "%s\\t%s\\t%s" (buffer-name) '
        '(or (buffer-file-name) "-") major-mode))) '
        '(seq-remove (lambda (b) (string-prefix-p " " (buffer-name b))) (buffer-list)) "\\n")')
    return _unquote(_eval(elisp))


@mcp.tool()
def emacs_current_buffer() -> str:
    """Return the name of the buffer the user is currently focused on (the
    selected window's buffer). Read it with emacs_read_buffer."""
    return _unquote(_eval('(buffer-name (window-buffer (selected-window)))'))


@mcp.tool()
def emacs_read_buffer(name: str) -> str:
    """Return the full text of the named Emacs buffer (READ ONLY). Get valid names
    from emacs_list_buffers or emacs_current_buffer. Large buffers are truncated."""
    nm = _elisp_string(name)
    elisp = (f'(let ((b (get-buffer {nm}))) (if b (with-current-buffer b '
             f'(buffer-substring-no-properties (point-min) '
             f'(min (point-max) (+ (point-min) {MAX_CHARS})))) '
             f'(format "no such buffer: %s" {nm})))')
    return _unquote(_eval(elisp))


@mcp.tool()
def emacs_around_point(name: str = "", lines: int = 40) -> str:
    """Return the region around the cursor (point) in the named buffer, or the
    current buffer if name is empty — `lines` lines before and after, with a
    >>> marker on the point line. Use to see exactly what the user is looking at."""
    nm = _elisp_string(name) if name else "(buffer-name (window-buffer (selected-window)))"
    elisp = (
        f'(let ((b (get-buffer {nm}))) (if b (with-current-buffer b '
        f'(save-excursion (let* ((cur (line-number-at-pos)) '
        f'(beg (progn (forward-line (- {lines})) (point))) '
        f'(dummy (goto-char (point-min))) '
        f'(_ (forward-line (1- cur)))) '
        f'(goto-char (point-min)) (forward-line (max 0 (- cur {lines} 1))) '
        f'(let ((s (point))) (forward-line (+ (* 2 {lines}) 1)) '
        f'(buffer-substring-no-properties s (point))))) '
        f'(format "no such buffer"))))')
    # Simpler, robust version: just return N lines around point with a marker.
    elisp = (
        f'(let ((b (get-buffer {nm}))) (if (not b) "no such buffer" '
        f'(with-current-buffer b (let* ((cur (line-number-at-pos (point))) '
        f'(lo (max 1 (- cur {lines}))) (hi (+ cur {lines})) (out "")) '
        f'(save-excursion (goto-char (point-min)) (forward-line (1- lo)) '
        f'(let ((n lo)) (while (and (<= n hi) (not (eobp))) '
        f'(setq out (concat out (format "%s%d\\t%s\\n" (if (= n cur) ">>> " "    ") n '
        f'(buffer-substring-no-properties (line-beginning-position) (line-end-position))))) '
        f'(forward-line 1) (setq n (1+ n))))) out))))')
    return _unquote(_eval(elisp))


if __name__ == "__main__":
    mcp.run(transport="stdio")
