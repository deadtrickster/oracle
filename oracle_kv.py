"""Get the prompt prefix warm again after a model restart — by replaying the prompt, not the KV.

Our own vision feature restarts the text server (one 24 GB card, two models), and a restart empties
every slot. The prefix is ~2,500 tokens, which at 300-500 tok/s is ~6 seconds of prompt processing
charged to whoever asks the next question.

## What did not work, and why it is written down

The obvious mechanism is llama.cpp's `--slot-save-path`: dump a slot's KV to a file, load it back
after the restart. It was built, measured, and dropped, because it does not do the job on this
build:

    dump size   ≈ 79 MB fixed + 13 KB/token   (q8_0 KV)  — 112 MB for the prefix
    save/restore ≈ 30 ms                                  — vs ~6 s to re-process
    restore     reports n_restored = 6033 tokens          — and then:
    next request                                             cache_n = 0

The restore reports success and the following request reuses NOTHING. Pinning the request to the
restored slot (`id_slot`) made it worse, not better. An earlier probe appeared to show reuse of
2,538 tokens, but that was a slot left warm by a previous run — the confound this file exists to
warn about. The controlled sequence (restart -> restore -> ask, nothing else in between) is
unambiguous: restored KV is not visible to prefix matching here.

## What works instead: replay the prompt

Keep the ~11 KB of prefix TEXT, and after a restart send it as a one-token request. The model
rebuilds the KV itself, through the same path that warms it normally, so there is nothing to be
subtly wrong about. It costs ~6 s of GPU time — but that time is spent while the swap is finishing,
in the background, instead of inside the user's next question.

Storing 11 KB of text instead of 112 MB of tensors, to recover the same state, is also just a
better trade. The `--slot-save-path` flag stays on the unit: it is harmless, and a future llama.cpp
may make the fast path work.
"""
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

MODEL_URL = os.environ.get("ORACLE_OLLAMA_URL", "http://localhost:11434").rstrip("/")
MODEL_NAME = os.environ.get("ORACLE_SYNTH_MODEL", "qwen3-coder-next")
WARM_DIR = Path(os.environ.get("ORACLE_KV_DIR", str(Path.home() / ".cache" / "oracle" / "kv")))
WARM_MAX = int(os.environ.get("ORACLE_WARM_HOSTS", "2"))     # --parallel 2: two slots, two prefixes
WARM_MIN_CHARS = int(os.environ.get("ORACLE_WARM_MIN_CHARS", "2000"))
ENABLED = os.environ.get("ORACLE_KV_PERSIST", "1").lower() in ("1", "true", "yes", "on")

_lock = threading.Lock()
_path = WARM_DIR / "prefixes.json"


def _load() -> dict:
    try:
        return json.loads(_path.read_text())
    except Exception:
        return {}


def remember(host: str, prefix: str) -> None:
    """Record this host's prompt prefix so it can be replayed after a restart.

    Only worth it for a prefix with real bulk in it: the bare preamble is ~200 tokens, which is
    under half a second and not worth a background request."""
    if not ENABLED or not host or len(prefix or "") < WARM_MIN_CHARS:
        return
    with _lock:
        d = _load()
        if d.get(host, {}).get("prefix") == prefix:
            d[host]["at"] = time.time()               # unchanged; just refresh recency
        else:
            d[host] = {"prefix": prefix, "at": time.time()}
        for host_, _ in sorted(d.items(), key=lambda kv: kv[1].get("at", 0))[:-WARM_MAX * 2]:
            d.pop(host_, None)                        # keep a little history, not all of it
        try:
            WARM_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, _path)
        except Exception:
            pass


def known() -> list:
    """Hosts with a stored prefix, most recently used first."""
    return [h for h, _ in sorted(_load().items(), key=lambda kv: kv[1].get("at", 0), reverse=True)]


def warm(host: str, timeout: int = 300) -> dict:
    """Replay `host`'s prefix as a one-token request. Returns {"tokens": processed, "reused": n}."""
    if not ENABLED:
        return {}
    entry = _load().get(host)
    if not entry:
        return {}
    body = {"model": MODEL_NAME, "max_tokens": 1,
            "messages": [{"role": "system", "content": entry["prefix"]},
                         {"role": "user", "content": "ok"}]}
    req = urllib.request.Request(f"{MODEL_URL}/v1/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            t = json.loads(r.read()).get("timings", {})
        return {"tokens": t.get("prompt_n", 0), "reused": t.get("cache_n", 0)}
    except Exception:
        return {}


def warm_all(limit: int = WARM_MAX):
    """Re-warm the most recent hosts, one slot each. Yields progress strings."""
    for host in known()[:limit]:
        got = warm(host)
        if got.get("tokens"):
            yield f"re-warmed {got['tokens']} prompt tokens for {host}"
