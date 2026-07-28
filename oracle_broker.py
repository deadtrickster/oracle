"""Who gets the GPU next — batching requests by the model they need, so they stop thrashing it.

`oracle_vram` answers "make this model resident, one swapper at a time". It does not answer the
question that appears the moment more than one thing is in flight: if a vision request and three
text requests all arrive together, the naive order is swap-swap-swap-swap, four model loads to serve
four requests. Each load is 20-40 seconds. The work itself is often shorter than the swapping.

So requests do not take the GPU directly; they queue for it, and the broker hands it out in BATCHES:

  * whoever holds the GPU keeps it while others want the SAME model — arrivals join the batch that
    is already running, rather than waiting behind a swap that would only have to swap back;
  * when the current batch drains, VISION goes next if anyone is waiting for it. That is his rule
    and it is the right one: a vision request is the expensive, user-visible one (a screenshot the
    person is staring at), and text work is both faster and more likely to be a background step;
  * a batch is bounded, so a steady stream of text requests cannot starve a waiting vision request
    forever. Fairness here is not politeness — an unbounded batch is a livelock with good manners.

This is deliberately NOT a queue of work items. Requests keep their own threads and their own
streaming connections; the broker only decides who may proceed. A request that dies mid-flight
releases its lease in a `finally` and the batch moves on, which a work-queue design would have to
reimplement.
"""
import os
import threading
import time
from collections import deque

# How many requests may join one model's batch before the other model is allowed in. Large enough
# that a burst of quick text turns is served without swapping; small enough that a person waiting on
# a screenshot is not held behind an endless drip of them.
BATCH_MAX = int(os.environ.get("ORACLE_BATCH_MAX", "8"))
# ...and how long a batch may hold the GPU, for the same reason expressed in time rather than count.
BATCH_MAX_SECONDS = float(os.environ.get("ORACLE_BATCH_MAX_SECONDS", "180"))


class _Broker:
    def __init__(self):
        self._cv = threading.Condition()
        self._holder = None          # "text" | "vl" | None — the model the current batch is using
        self._active = 0             # leases currently held
        self._served = 0             # leases served by the current batch
        self._since = 0.0
        self._waiting = {"text": 0, "vl": 0}
        self._log = deque(maxlen=50)

    # -- internal, called with the lock held -------------------------------------------------
    def _may_start(self, kind: str) -> bool:
        if self._holder is None:
            # Nobody holds it. Vision has priority: if a vl request is waiting, text yields.
            return kind == "vl" or self._waiting["vl"] == 0
        if self._holder != kind:
            return False                       # different model — wait for the batch to drain
        # Same model: join the batch, unless this batch has had its turn and the other model waits.
        other = "text" if kind == "vl" else "vl"
        if self._waiting[other] and (self._served >= BATCH_MAX
                                     or time.time() - self._since > BATCH_MAX_SECONDS):
            return False
        return True

    def acquire(self, kind: str, timeout: float = 900.0):
        """Wait for permission to use `kind`. Returns a note about the wait, or ""."""
        started = time.time()
        with self._cv:
            self._waiting[kind] += 1
            try:
                waited_for = None
                while not self._may_start(kind):
                    if waited_for is None:
                        waited_for = self._holder
                    if not self._cv.wait(timeout=max(0.0, timeout - (time.time() - started))):
                        raise TimeoutError(f"waited {timeout:.0f}s for the {kind} model")
            finally:
                self._waiting[kind] -= 1
            if self._holder != kind:
                self._holder, self._served, self._since = kind, 0, time.time()
            self._active += 1
            self._served += 1
            self._log.append((time.time(), kind, "start"))
        secs = time.time() - started
        if secs < 0.5:
            return ""
        return (f"waited {secs:.0f}s for the GPU (another {waited_for or 'request'} was using it)"
                if waited_for else f"waited {secs:.0f}s for the GPU")

    def release(self):
        with self._cv:
            self._active = max(0, self._active - 1)
            if self._active == 0:
                self._holder = None            # batch drained; whoever is waiting may proceed
            self._log.append((time.time(), "-", "end"))
            self._cv.notify_all()

    def status(self) -> dict:
        with self._cv:
            return {"holder": self._holder, "active": self._active, "served": self._served,
                    "waiting": dict(self._waiting)}


_broker = _Broker()


class lease:
    """`with oracle_broker.lease("vl") as note:` — `note` is a human-readable wait message, or "".

    Used as a context manager so the release is a `finally` the caller cannot forget. Every path
    that touches a model goes through this; one that does not is a request that can swap the GPU out
    from under a batch."""

    def __init__(self, kind: str, timeout: float = 900.0):
        self.kind = kind
        self.timeout = timeout
        self.note = ""

    def __enter__(self):
        self.note = _broker.acquire(self.kind, self.timeout)
        return self.note

    def __exit__(self, *exc):
        _broker.release()
        return False


def status() -> dict:
    return _broker.status()
