#!/usr/bin/env python3
"""Offload a cheap/bulk task to local qwen — to save Claude Code's context + output tokens.

The point: Claude's context is the scarce resource; local qwen is abundant/free. For work
that's bulk, parallel, or low-stakes (digest N files, first-pass triage, classify, draft),
hand it to qwen and get back a compact result instead of pulling everything into Claude's
context. Great as a BACKGROUND job — fire it, keep working, read the digest when it lands.

  qwen.py "summarize the key structs + what each is for" --files a.c b.h
  qwen.py "which of these look security-relevant? list file:line" --glob 'src/**/*.rs'
  echo "classify each error by root cause" | qwen.py --files build.log
  qwen.py "one-paragraph digest of this API" --files corpus/cpp/md/cpp/container/vector.md

Reads the prompt from argv or stdin; --files / --glob add file contents (truncated). Prints
qwen's answer to stdout. Degrades with a clear message if Ollama is down.
"""
import argparse
import glob as globmod
import json
import sys
import urllib.request
from pathlib import Path

OLLAMA = "http://localhost:11434"
MODEL = "qwen3-coder:30b"
PER_FILE_CAP = 24000   # chars per file fed to qwen
TOTAL_CAP = 120000     # total context chars


def gather(files, globs):
    paths = [Path(f) for f in files]
    for g in globs:
        paths += [Path(p) for p in globmod.glob(g, recursive=True)]
    out, used = [], 0
    for p in paths:
        if not p.is_file() or used > TOTAL_CAP:
            continue
        try:
            txt = p.read_text(errors="replace")[:PER_FILE_CAP]
        except Exception:
            continue
        block = f"===== {p} =====\n{txt}\n"
        out.append(block)
        used += len(block)
    return "".join(out)


def ask(prompt, context):
    system = ("You are a fast local assistant doing an offloaded sub-task for a stronger agent. "
              "Be concise, concrete, and grounded ONLY in the provided material. Prefer a tight "
              "structured digest (bullets, file:line, tables) over prose. If asked to find/triage, "
              "list exact locations. Do not invent; if something isn't in the material, say so.")
    user = prompt if not context else f"{prompt}\n\n--- material ---\n{context}"
    body = {"model": MODEL, "stream": False, "options": {"temperature": 0.1},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["message"]["content"].strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("prompt", nargs="?", default="", help="task for qwen (or pipe via stdin)")
    ap.add_argument("--files", nargs="*", default=[], help="files to include")
    ap.add_argument("--glob", dest="globs", nargs="*", default=[], help="globs to include (recursive)")
    a = ap.parse_args()
    prompt = a.prompt or sys.stdin.read().strip()
    if not prompt:
        ap.error("no prompt (arg or stdin)")
    context = gather(a.files, a.globs)
    try:
        print(ask(prompt, context))
    except Exception as e:  # noqa: BLE001
        print(f"[qwen offload unavailable: {e}]", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
