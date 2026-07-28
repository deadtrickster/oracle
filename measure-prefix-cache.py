#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Measure whether llama.cpp actually reuses the shared prompt prefix.

The restructure is worth nothing unless the server SKIPS the cached tokens, and you cannot tell
from the answers — they are identical either way. The first version of this script compared tokens
PROCESSED between requests and proved nothing, because a request that processes fewer tokens might
simply have had a smaller prompt. The number that matters is the ratio:

    reused = total prompt tokens − tokens the server actually processed

`total` comes from the server's own /tokenize (not an estimate), and `processed` from the
`prompt eval time = … / N tokens` line it logs. Four requests, chosen so each isolates one thing:

    A  cold                     nothing cached          reused ≈ 0
    B  IDENTICAL to A           everything cached       reused ≈ total   <- proves the mechanism
    C  same host, new question  prefix cached           reused ≈ prefix
    D  different host           different prefix        reused ≈ preamble only

B is the control. If B does not show near-total reuse, prefix caching is not working at all and C
and D are noise.

  ./measure-prefix-cache.py
"""
import json
import re
import subprocess
import time
import urllib.request

RECEIVER = "http://127.0.0.1:8788"
MODEL = "http://127.0.0.1:18080"
UNIT = "oracle-qwen-next"
STROPPY = "https://docs.stroppy.io/guide"
OTHER = "https://unknown.example/page"


def ntok(text: str) -> int:
    if not text:
        return 0
    req = urllib.request.Request(f"{MODEL}/tokenize", data=json.dumps({"content": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return len(json.loads(r.read())["tokens"])


def processed_since(since: str) -> int:
    out = subprocess.run(["journalctl", "--user", "-u", UNIT, "--since", since,
                          "--no-pager", "-o", "cat"], capture_output=True, text=True).stdout
    return sum(int(t) for t in re.findall(r"prompt eval time =\s*[\d.]+ ms /\s*(\d+) tokens", out))


def explain(selection: str, url: str):
    """Returns (wall_seconds, system_text, user_text) — the prompt via the debug channel, so the
    token count is of what was ACTUALLY sent rather than of a reconstruction."""
    body = {"selection": selection, "url": url, "title": "measurement", "debug": True,
            "where": {"around": "surrounding prose held constant for the measurement",
                      "headings": "Locks"}}
    req = urllib.request.Request(f"{RECEIVER}/explain", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    sysmsg = usermsg = ""
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=1800) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if not line.startswith("data: "):
                continue
            try:
                d = json.loads(line[6:])
            except Exception:
                continue
            if d.get("stage") == "prompt sent to the text model":
                sysmsg, usermsg = d.get("system", ""), d.get("text", "")
    return time.time() - t0, sysmsg, usermsg


RUNS = [("A cold", "what is a VU", STROPPY),
        ("B identical to A", "what is a VU", STROPPY),
        ("C same host, new q", "what is a scenario", STROPPY),
        ("D different host", "what is a scenario", OTHER)]

print(f"{'request':<22} {'wall':>6} {'total':>7} {'processed':>10} {'reused':>7} {'prefix':>7}")
rows = []
for label, sel, url in RUNS:
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    time.sleep(1.1)                       # journald timestamps have one-second granularity
    wall, sysmsg, usermsg = explain(sel, url)
    time.sleep(1.5)                       # let the server flush its timing line
    proc = processed_since(since)
    # +~10 for the chat template's role markers; small and constant, and it never changes the story
    total = ntok(sysmsg) + ntok(usermsg) + 10
    prefix = ntok(sysmsg)
    rows.append((label, wall, total, proc, total - proc, prefix))
    print(f"{label:<22} {wall:>5.0f}s {total:>7} {proc:>10} {total - proc:>7} {prefix:>7}")

b = rows[1]
print(f"\nB reused {b[4]}/{b[2]} tokens ({100 * b[4] / max(1, b[2]):.0f}%) — "
      + ("prefix caching IS working." if b[4] > b[2] * 0.8 else
         "NOT working; the answers are still correct, only slower."))
c, d = rows[2], rows[3]
print(f"C (same host) reused {c[4]}, its cached prefix is {c[5]} tokens.")
print(f"D (other host) reused {d[4]}, its prefix is only {d[5]} tokens — no site pack to share.")
