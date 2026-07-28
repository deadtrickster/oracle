"""Who gets the GPU next — batching requests by the model they need, ACROSS PROCESSES.

`oracle_vram` answers "make this model resident, one swapper at a time" and enforces it with an
flock, so two processes can never swap simultaneously. It does not answer the question that appears
the moment several requests are in flight: if a vision request and three text requests arrive
together, the naive order is swap-swap-swap-swap — four model loads at 20-40s each to serve four
requests whose actual work is shorter than the swapping.

So requests queue for the GPU and it is handed out in BATCHES:

  * an arrival for the model already loaded JOINS the running batch, rather than waiting behind a
    swap that would only have to swap back;
  * when a batch drains, VISION goes next if anyone is waiting. A vision request is the expensive,
    user-visible one — a screenshot someone is staring at — while text work is faster and more
    often a background step;
  * a batch is bounded in count AND time, because an unbounded batch is a livelock with good
    manners.

## Why this is a file and not an object

The first version held its state in a Python object, which meant each PROCESS batched its own work
and the browser receiver and the Claude-Code shim could still make the GPU ping-pong between them —
the exact behaviour the broker exists to prevent, just one level up. Correctness was never at risk
(oracle_vram's flock still serialises the swap itself); throughput was.

State therefore lives in one small JSON file under the runtime directory, guarded by an flock for
atomic read-modify-write. Every Oracle process sees the same queue.

## Crash safety is the hard part of doing it this way

An in-process lock is released when the holder dies. A file is not: a process killed mid-lease
would hold the GPU forever, and a broker that deadlocks the machine when something crashes is worse
than no broker. So a lease records its PID, and every acquire REAPS entries whose process is gone.
That, not politeness, is what makes it safe to put this state on disk.
"""
import fcntl
import json
import os
import random
import time
from contextlib import contextmanager
from pathlib import Path

BATCH_MAX = int(os.environ.get("ORACLE_BATCH_MAX", "8"))
BATCH_MAX_SECONDS = float(os.environ.get("ORACLE_BATCH_MAX_SECONDS", "180"))
# A lease older than this is presumed abandoned even if its PID still exists — a wedged process
# should not hold the card indefinitely. Generous, because a cold vision read genuinely can run for
# minutes.
LEASE_MAX_SECONDS = float(os.environ.get("ORACLE_LEASE_MAX_SECONDS", "1800"))
POLL_SECONDS = float(os.environ.get("ORACLE_BROKER_POLL", "0.15"))

_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
STATE_PATH = Path(os.environ.get("ORACLE_BROKER_STATE", str(_DIR / "oracle-broker.json")))
LOCK_PATH = Path(str(STATE_PATH) + ".lock")

_token = f"{os.getpid()}-{random.randint(0, 1 << 30)}"


def _alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}").exists()
    except Exception:
        return True          # cannot tell — assume alive rather than steal a live lease


@contextmanager
def _locked():
    """The state file, exclusively, with dead entries already reaped."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            try:
                st = json.loads(STATE_PATH.read_text())
            except Exception:
                st = {}
            st.setdefault("holder", None)
            st.setdefault("active", [])
            st.setdefault("waiting", [])
            st.setdefault("served", 0)
            st.setdefault("since", 0.0)

            now = time.time()
            st["active"] = [a for a in st["active"]
                            if _alive(a.get("pid", 0)) and now - a.get("at", 0) < LEASE_MAX_SECONDS]
            # A waiter that died, or one that has been waiting longer than any request could
            # reasonably live, must not go on influencing priority.
            st["waiting"] = [w for w in st["waiting"]
                             if _alive(w.get("pid", 0)) and now - w.get("at", 0) < LEASE_MAX_SECONDS]
            if not st["active"]:
                st["holder"] = None

            box = {"st": st}
            yield box
            try:
                tmp = STATE_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(box["st"]))
                os.replace(tmp, STATE_PATH)
            except OSError:
                pass
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _waiting(st: dict, kind: str) -> int:
    return sum(1 for w in st["waiting"] if w.get("kind") == kind)


def _may_start(st: dict, kind: str, me: str) -> bool:
    others = [w for w in st["waiting"] if w.get("token") != me]
    other_kind = "text" if kind == "vl" else "vl"
    n_other = sum(1 for w in others if w.get("kind") == other_kind)
    if not st["active"]:
        # Free. Vision has priority: text yields if any vl request is waiting.
        return kind == "vl" or n_other == 0
    if st["holder"] != kind:
        return False                       # different model — wait for the batch to drain
    if n_other and (st["served"] >= BATCH_MAX
                    or time.time() - st["since"] > BATCH_MAX_SECONDS):
        return False                       # this batch has had its turn
    return True


def _new_token() -> str:
    return f"{_token}-{time.time()}-{random.randint(0, 1 << 20)}"


def acquire(kind: str, timeout: float = 900.0) -> tuple:
    """(token, note). Prefer `lease`, which cannot forget to release."""
    me = _new_token()
    return me, _acquire_as(kind, me, timeout)


def _drop_waiter(me: str) -> None:
    with _locked() as box:
        box["st"]["waiting"] = [w for w in box["st"]["waiting"] if w.get("token") != me]


def release(me: str) -> None:
    with _locked() as box:
        st = box["st"]
        st["active"] = [a for a in st["active"] if a.get("token") != me]
        if not st["active"]:
            st["holder"] = None


class lease:
    """`with oracle_broker.lease("vl") as note:` — `note` is a human-readable wait message, or "".

    A context manager so the release is a `finally` the caller cannot forget. Every path that
    touches a model goes through this; one that does not is a request that can swap the GPU out
    from under a batch."""

    def __init__(self, kind: str, timeout: float = 900.0):
        self.kind = kind
        self.timeout = timeout
        self.note = ""
        self._me = ""

    def __enter__(self):
        self._me = _new_token()
        self.note = _acquire_as(self.kind, self._me, self.timeout)
        return self.note

    def __exit__(self, *exc):
        release(self._me)
        return False


def _acquire_as(kind: str, me: str, timeout: float) -> str:
    """acquire(), but with a caller-supplied token so the lease can release exactly its own entry."""
    started = time.time()
    blocked_by = None
    registered = False
    try:
        while True:
            with _locked() as box:
                st = box["st"]
                if not registered:
                    st["waiting"].append({"token": me, "pid": os.getpid(), "kind": kind,
                                          "at": time.time()})
                    registered = True
                if _may_start(st, kind, me):
                    st["waiting"] = [w for w in st["waiting"] if w.get("token") != me]
                    if st["holder"] != kind:
                        st["holder"], st["served"], st["since"] = kind, 0, time.time()
                    st["active"].append({"token": me, "pid": os.getpid(), "kind": kind,
                                         "at": time.time()})
                    st["served"] += 1
                    registered = False
                    break
                blocked_by = blocked_by or st.get("holder")
            if time.time() - started > timeout:
                _drop_waiter(me)
                raise TimeoutError(f"waited {timeout:.0f}s for the {kind} model")
            time.sleep(POLL_SECONDS * (0.5 + random.random()))
    except BaseException:
        if registered:
            _drop_waiter(me)
        raise
    secs = time.time() - started
    if secs < 0.5:
        return ""
    return (f"waited {secs:.0f}s for the GPU (another {blocked_by} request was using it)"
            if blocked_by else f"waited {secs:.0f}s for the GPU")


def status() -> dict:
    with _locked() as box:
        st = box["st"]
        return {"holder": st["holder"], "active": len(st["active"]), "served": st["served"],
                "waiting": {"text": _waiting(st, "text"), "vl": _waiting(st, "vl")},
                "pids": sorted({a.get("pid") for a in st["active"]})}
