"""Who owns the one big GPU slot — shared by every process that needs a model resident.

The card is 24 GB. The text model (~20.6 GB) and qwen3-vl (~17 GB) are mutually exclusive by
arithmetic, so exactly one is resident and `oracle-vram.sh` swaps them. That was fine while the
capture receiver was the only swapper. It no longer is: the Claude-Code shim now runs the same
detour when a chat references an image, and two processes calling `oracle-vram.sh` concurrently
means one stops the unit the other just started — a swap that reports success and leaves the box
with no model, which is precisely the "did less than it claimed and said nothing" failure this
repo exists to hunt.

So ownership lives in ONE place, with a cross-process lock:

  * `text_available()` / `vl_available()` — PROBED, never configured. A flag would have to be
    flipped in lockstep with every swap and would lie whenever the two drifted. Ask what is
    actually listening; cache the answer for a few seconds so a burst of requests pays one socket.
  * `ensure(kind)` — a generator that makes `kind` resident and yields human-readable progress.
    Serialised by an flock, so N processes cause ONE swap and the losers re-probe instead of
    swapping again.

Progress is yielded rather than logged because loading is DISK-bound: with --no-mmap and MoE
expert offload a cold start reads tens of GB, and a message followed by silence is
indistinguishable from a hang (it was reported as exactly that). Callers forward these strings to
whatever UI they have.
"""
import fcntl
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

OLLAMA = os.environ.get("ORACLE_OLLAMA_URL", "http://localhost:11434").rstrip("/")
VL_URL = os.environ.get("ORACLE_VL_URL", "http://localhost:18081").rstrip("/")
VRAM_SH = str(Path(__file__).resolve().parent / "oracle-vram.sh")
AUTOSWAP = os.environ.get("ORACLE_VRAM_AUTOSWAP", "1").lower() in ("1", "true", "yes", "on")
# Must exceed oracle-vram.sh's own WAIT_S, or this kills a swap the script would have completed.
SWAP_TIMEOUT = int(os.environ.get("ORACLE_VRAM_SWAP_TIMEOUT", "1200"))
PROBE_TTL = 5.0

_LOCK_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
LOCK_PATH = _LOCK_DIR / "oracle-vram.lock"

VL_DISABLED_MSG = (
    "Vision is not loaded. The GPU (24 GB) fits the text model (~20.6 GB) or qwen3-vl (~17 GB), "
    "not both, so only one is resident at a time. Switch with:  ./oracle-vram.sh vl   "
    "(and back with  ./oracle-vram.sh text). Capture still works; ask/explain/fact-check need the "
    "text model."
)

_vl_state = {"ok": False, "at": 0.0}
_text_state = {"ok": False, "at": 0.0}
_vl_lock = threading.Lock()
_text_lock = threading.Lock()
_swap_lock = threading.Lock()          # in-process; the flock covers other processes


def _probe(url: str, state: dict, lock: threading.Lock, force: bool = False) -> bool:
    now = time.time()
    with lock:
        if not force and now - state["at"] < PROBE_TTL:
            return state["ok"]
    ok = False
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
            ok = r.status == 200
    except Exception:
        ok = False
    with lock:
        state.update(ok=ok, at=now)
    return ok


def vl_available(force: bool = False) -> bool:
    """Is the vision server up right now? Cached for PROBE_TTL seconds."""
    return _probe(VL_URL, _vl_state, _vl_lock, force)


def text_available(force: bool = False) -> bool:
    """Is the synthesis server up right now?

    llama.cpp exposes /health; Ollama does not and answers 404 — which this reads as "not up".
    That is deliberate: autoswap is only meaningful against the llama.cpp backend, and callers
    additionally gate on vl_available() so a plain-Ollama setup never triggers a swap."""
    return _probe(OLLAMA, _text_state, _text_lock, force)


def resident() -> str:
    """"text" | "vl" | "none" — what is actually listening, not what someone configured."""
    if text_available():
        return "text"
    return "vl" if vl_available() else "none"


def ensure(kind: str):
    """Make `kind` ("text"|"vl") resident, swapping if needed. Yields progress strings.

    Raises RuntimeError if the model could not be made resident. Yielding nothing means it was
    already there — the common case, and it costs one cached probe."""
    if kind not in ("text", "vl"):
        raise ValueError(f"kind must be text|vl, got {kind!r}")
    probe = vl_available if kind == "vl" else text_available
    if probe():
        return
    if not AUTOSWAP:
        raise RuntimeError(f"{kind} model is not loaded and autoswap is off — run "
                           f"./oracle-vram.sh {kind}")

    with _swap_lock:
        if probe():                       # another thread swapped it in while we waited
            return
        with open(LOCK_PATH, "w") as lf:
            waiting = False
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                waiting = True
                yield "another Oracle process is already swapping the GPU — waiting for it…"
                fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                # Re-probe with the lock held and the cache bypassed: whoever we queued behind may
                # have loaded exactly what we wanted, and a stale 5-second cache would send us
                # through a second, pointless swap.
                if probe(force=True):
                    if waiting:
                        yield f"the other process loaded {kind} — reusing it"
                    return
                yield from _swap(kind)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


def _swap(kind: str):
    other = "text model" if kind == "vl" else "vision model"
    want = "qwen3-vl" if kind == "vl" else "the text model"
    yield f"swapping GPU: unloading the {other}, loading {want}…"

    # Run it asynchronously and HEARTBEAT. A single message followed by minutes of silence is
    # indistinguishable from a hang; emitting elapsed time also keeps a long SSE connection warm.
    proc = subprocess.Popen([VRAM_SH, kind], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    started = time.time()
    while proc.poll() is None:
        if time.time() - started > SWAP_TIMEOUT:
            proc.kill()
            raise RuntimeError(f"GPU swap to {kind} timed out after {SWAP_TIMEOUT}s")
        time.sleep(2)
        waited = int(time.time() - started)
        if waited and waited % 10 < 2:
            yield f"loading {want}… {waited}s (reading weights from disk)"
    out = proc.stdout.read() if proc.stdout else ""

    # HEALTH decides, not the exit code: llama-server accepts the socket long before the weights
    # are loaded, and the script has its own rollback path whose exit status says nothing about
    # what ended up resident. Ask the port.
    probe = vl_available if kind == "vl" else text_available
    if not probe(force=True):
        raise RuntimeError(f"GPU swap to {kind} failed: {out.strip()[:300]}")
    yield f"{want} is resident"

    # A restart empties every slot, so the model that just came back is fast at generating and slow
    # at reading. Replay the recent prompt prefixes now, while the swap is still finishing, rather
    # than charging the ~6 s of prompt processing to whoever asks the next question.
    if kind == "text":
        try:
            import oracle_kv
            yield from oracle_kv.warm_all()
        except Exception:
            pass                                     # a cold cache is slow, never wrong
