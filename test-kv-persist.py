#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Does the prompt prefix come back warm after a model restart? Restart it and find out.

Our own vision feature restarts the text server (one 24 GB card, two models) and a restart empties
every slot. This checks, end to end against the real server, that the prefix is warm again before
the next question — with a CONTROL, because "the second request was fast" proves nothing alone.

    restart, then ask                      -> cold, reuse ≈ 0   (control)
    restart, replay the prefix, then ask   -> reuse ≈ the prefix

The measure is `timings.cache_n` from the server itself: tokens it did not have to process.

Restarts the text model twice; takes a few minutes and briefly makes it unavailable.

  ./test-kv-persist.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("ORACLE_OLLAMA_URL", "http://localhost:18080")
os.environ.setdefault("ORACLE_SYNTH_MODEL", "qwen3-coder-next")
import oracle_kv as kv                                    # noqa: E402
import oracle_sitectx as sc                               # noqa: E402

_spec = importlib.util.spec_from_file_location("rcv", HERE / "oracle-capture-receiver.py")
rcv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rcv)

MODEL = os.environ["ORACLE_OLLAMA_URL"].rstrip("/")
UNIT = "oracle-qwen-next"
HOST = "docs.stroppy.io"
PREFIX = rcv._system_for(sc.block(f"https://{HOST}/guide"), HOST)

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(name)


def ask(user="Reply with the single word: ok"):
    body = {"model": "qwen3-coder-next", "max_tokens": 1,
            "messages": [{"role": "system", "content": PREFIX}, {"role": "user", "content": user}]}
    r = urllib.request.Request(f"{MODEL}/v1/chat/completions", data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=900) as resp:
        return json.loads(resp.read()).get("timings", {})


def restart():
    subprocess.run(["systemctl", "--user", "restart", UNIT], check=True)
    # Same patience as oracle-vram.sh, and for the same reason: loading is DISK-bound (--no-mmap,
    # MoE expert offload, ~48 GB), so a cold page cache turns a 20 s start into minutes. A 300 s
    # limit reported failure here while the model was still loading — the exact mistake that script
    # already documents.
    for _ in range(900):
        try:
            with urllib.request.urlopen(f"{MODEL}/health", timeout=2) as h:
                if h.status == 200:
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("model did not come back")


print(f"\nprefix: {len(PREFIX)} chars\n")

print("1. the prefix is recorded just by being used")
check("host is known to the warmer", HOST in kv.known(), str(kv.known()))

print("\n2. control: restart, ask immediately — must be cold")
restart()
t = ask()
check("nothing reused after a restart", t.get("cache_n", 0) < 100, f"cache_n={t.get('cache_n')}")
cold_n = t.get("prompt_n", 0)
print(f"   cold: processed {cold_n}, reused {t.get('cache_n')}")

print("\n3. restart, replay the prefix, then ask")
restart()
warmed = list(kv.warm_all())
check("the warmer ran and said so", bool(warmed), str(warmed))
t = ask("Reply with the single word: fine")
check("the prefix is reused", t.get("cache_n", 0) > 500, f"cache_n={t.get('cache_n')}")
check("so almost nothing is processed", t.get("prompt_n", 99999) < cold_n / 4,
      f"{t.get('prompt_n')} vs cold {cold_n}")
print(f"   warm: processed {t.get('prompt_n')}, reused {t.get('cache_n')}")

print()
if fails:
    print(f"FAILED: {len(fails)} -> {', '.join(fails)}")
    sys.exit(1)
print("the prefix survives a restart")
