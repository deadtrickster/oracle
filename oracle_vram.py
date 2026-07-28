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

# WHERE THE TEXT MODEL LIVES, for the purposes of residency — which is NOT the same question as
# "where do completions go". This used to piggyback on ORACLE_OLLAMA_URL, whose default is Ollama's
# :11434, while oracle-vram.sh starts and health-gates llama.cpp on :18080. Any process that had not
# inherited the receiver's environment therefore probed the wrong port, concluded the text model was
# down immediately after the swap script reported it ready, and raised "GPU swap to text failed" on
# a swap that had in fact succeeded. Probe the port the script actually manages.
TEXT_URL = os.environ.get("ORACLE_TEXT_URL", "http://127.0.0.1:18080").rstrip("/")
OLLAMA = TEXT_URL          # kept for callers that still refer to it by the old name
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
    return _probe(TEXT_URL, _text_state, _text_lock, force)


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


# ---- the tier that actually costs: model WEIGHTS, not caches ------------------------------------
#
# Three tiers exist here, and it is worth being precise about which one is expensive:
#
#   VRAM   llama.cpp's own slot KV cache. Free, and dies with the server.
#   RAM    the model FILES in page cache. This is the one that decides whether a swap takes
#          20 seconds or four minutes.
#   DISK   ~11 KB of prompt-prefix text, replayed after a restart (oracle_kv). Tiny.
#
# The middle tier is the whole game. Loading is disk-bound: 49.6 GB of text model, 17.7 GB of
# qwen3-vl, and `--no-mmap` copies the weights into process memory rather than mapping them — so a
# swap re-reads the entire file unless the kernel still has it cached. Twelve loads in six hours on
# a normal working day makes that the single largest latency in the system, larger than every
# prompt-processing saving in this repo put together.
#
# This box has 125 GB. Both models together are 67 GB, so both can stay cached at once. After a
# swap, pull the OTHER model's file into page cache in the background — fadvise first because it is
# free, then a real sequential read, because fadvise alone is a hint the kernel caps far below
# 50 GB and would have given a warming step that reported success and warmed almost nothing. The
# data lands in the kernel's cache, not in this process, and the kernel may drop it under pressure.
# Gated on MemAvailable, because a cache tier that causes swapping has made things worse, not
# better.
MODEL_FILES = {
    "text": [p for p in [os.environ.get("ORACLE_TEXT_MODEL_FILE",
             "/usr/share/ollama/.ollama/models/blobs/"
             "sha256-4bb93f0a0221ef4ff963ca9094df629c8dfdfabc3b4fdd85c1a2e4c0624fce36")] if p],
    "vl": [p for p in [
        os.environ.get("ORACLE_VL_MODEL_FILE",
                       str(Path.home() / "models/qwen3-vl/Qwen3-VL-30B-A3B-Instruct-UD-Q4_K_XL.gguf")),
        os.environ.get("ORACLE_VL_MMPROJ_FILE",
                       str(Path.home() / "models/qwen3-vl/mmproj-F16.gguf"))] if p],
}
# Leave this much RAM unclaimed, so warming never pushes the machine into reclaim.
WARM_HEADROOM = int(os.environ.get("ORACLE_WARM_HEADROOM_GB", "12")) * 1024**3
WARM_WEIGHTS = os.environ.get("ORACLE_WARM_WEIGHTS", "1").lower() in ("1", "true", "yes", "on")


def _mem_available() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


def _pull_into_cache(path: str) -> None:
    """Read a file so the kernel actually caches it.

    POSIX_FADV_WILLNEED is issued first because it is free and lets the kernel start early, but it
    is only a HINT and readahead is capped far below 50 GB — relying on it alone would give a
    warming step that reports success and warms almost nothing, which is the failure shape this repo
    keeps finding. So follow it with a real sequential read. If the file is already cached this
    costs a few seconds at memory speed; if it is not, this is the point.

    Reads into a fixed buffer and discards: the data lands in page cache, not in this process."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_WILLNEED)
        except (AttributeError, OSError):
            pass
        while True:
            if not os.read(fd, 16 * 1024 * 1024):
                break
    except OSError:
        pass
    finally:
        os.close(fd)


def warm_weights(kind: str) -> dict:
    """Pull `kind`'s weights into page cache, in the background. Returns what it decided and why —
    a cache that silently declines to cache is worse than one that is simply off."""
    if not WARM_WEIGHTS:
        return {"warmed": [], "why": "disabled"}
    avail = _mem_available()
    done, skipped = [], []
    for path in MODEL_FILES.get(kind, []):
        try:
            size = os.path.getsize(path)
        except OSError:
            skipped.append((path, "missing"))
            continue
        if avail - size < WARM_HEADROOM:
            skipped.append((path, f"only {avail // 1024**3} GB available"))
            continue
        # Background, because the caller is finishing a swap and the user is waiting on it. The
        # read is pure I/O; the next swap is what collects the benefit.
        threading.Thread(target=_pull_into_cache, args=(path,), daemon=True).start()
        done.append((path, size))
        avail -= size              # budget as if it were already resident, so two files cannot
        #                            both claim the same headroom
    return {"warmed": done, "skipped": skipped}


def cached_fraction(path: str) -> float:
    """How much of a file is resident in page cache, 0..1 — the only way to tell whether warming
    did anything. mincore(2) via mmap; no external tools."""
    import ctypes
    import mmap
    try:
        size = os.path.getsize(path)
        if not size:
            return 0.0
        with open(path, "rb") as f:
            # ACCESS_COPY, not PROT_READ: ctypes.from_buffer needs a WRITABLE buffer to hand back
            # the mapping's address, and a read-only mmap makes it raise. Copy-on-write costs
            # nothing here because nothing is ever written, and mincore still reports the residency
            # of the underlying page cache.
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_COPY)
            try:
                page = os.sysconf("SC_PAGE_SIZE")
                pages = (size + page - 1) // page
                vec = (ctypes.c_ubyte * pages)()
                libc = ctypes.CDLL("libc.so.6", use_errno=True)
                buf = (ctypes.c_char * 1).from_buffer(mm, 0)   # the mapping's base address
                try:
                    rc = libc.mincore(ctypes.c_void_p(ctypes.addressof(buf)),
                                      ctypes.c_size_t(size), vec)
                finally:
                    del buf                                    # release before mm.close()
                if rc != 0:
                    return -1.0
                return sum(1 for b in vec if b & 1) / pages
            finally:
                mm.close()
    except Exception:
        return -1.0


def _warm_other(kind: str):
    """After a swap, keep the model we just evicted warm — it is the one we will want next."""
    other = "vl" if kind == "text" else "text"
    try:
        res = warm_weights(other)
        gb = sum(s for _, s in res.get("warmed", [])) / 1024**3
        if gb:
            return f"keeping the {other} model's {gb:.0f} GB warm in RAM for the next swap"
        why = res.get("skipped") or res.get("why")
        return f"not pre-warming the {other} model ({why})" if why else ""
    except Exception:
        return ""


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

    # Now keep the model we just EVICTED warm in page cache. It is the one we will want next — a
    # swap is, by definition, a promise to swap back — and re-reading 50 GB from disk is the single
    # largest latency in this system, worth more than every prompt-processing saving combined.
    note = _warm_other(kind)
    if note:
        yield note

    # A restart empties every slot, so the model that just came back is fast at generating and slow
    # at reading. Replay the recent prompt prefixes now, while the swap is still finishing, rather
    # than charging the ~6 s of prompt processing to whoever asks the next question.
    if kind == "text":
        try:
            import oracle_kv
            yield from oracle_kv.warm_all()
        except Exception:
            pass                                     # a cold cache is slow, never wrong
