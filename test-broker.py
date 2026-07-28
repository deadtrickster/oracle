#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Test the GPU broker: batching, vision priority, and the starvation bound.

Concurrency tests that only check "it did not crash" are theatre. These assert the ORDER things ran
in, because the order is the entire feature — a broker that serialises correctly but swaps between
every request has solved nothing.

  ./test-broker.py
"""
import os
import sys
import threading
import time
from pathlib import Path

os.environ["ORACLE_BATCH_MAX"] = "3"
os.environ["ORACLE_BATCH_MAX_SECONDS"] = "60"
# Its OWN state file. The broker's whole point is that every Oracle process shares one queue — which
# means a test run against the default path would queue behind the live receiver's real GPU work,
# and worse, make the receiver queue behind the test. Tests must not compete with production for a
# resource they are pretending to model.
import tempfile as _tf                                    # noqa: E402
os.environ["ORACLE_BROKER_STATE"] = str(Path(_tf.mkdtemp(prefix="oracle-broker-test-")) / "b.json")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import oracle_broker as br                                # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(name)


def worker(kind, hold, order, label):
    with br.lease(kind):
        order.append(("start", label))
        time.sleep(hold)
        order.append(("end", label))


print("\n1. requests for the SAME model share the GPU instead of taking turns")
order = []
ts = [threading.Thread(target=worker, args=("text", 0.25, order, f"t{i}")) for i in range(3)]
[t.start() for t in ts]
[t.join() for t in ts]
starts = [lbl for kind, lbl in order if kind == "start"]
first_end = next(i for i, (k, _) in enumerate(order) if k == "end")
check("all three started before any finished", first_end >= 3, str(order))
check("three ran", len(starts) == 3)

print("\n2. a different model waits for the batch to drain")
order = []
a = threading.Thread(target=worker, args=("text", 0.4, order, "text"))
a.start()
time.sleep(0.05)
b = threading.Thread(target=worker, args=("vl", 0.05, order, "vl"))
b.start()
a.join(); b.join()
check("vl did not start until text finished",
      order.index(("end", "text")) < order.index(("start", "vl")), str(order))

print("\n3. VISION goes first when both are waiting for a free GPU")
# The holder must be VL, so the queued text cannot simply join its batch — otherwise this measures
# batching (which is correct and desirable) rather than priority. Both waiters are then genuinely
# queued when the slot frees, which is the only moment priority means anything.
order = []
hold = threading.Thread(target=worker, args=("vl", 0.35, order, "holder-vl"))
hold.start()
time.sleep(0.05)
later_text = threading.Thread(target=worker, args=("text", 0.05, order, "text-queued"))
later_text.start()
time.sleep(0.02)
later_vl = threading.Thread(target=worker, args=("vl", 0.05, order, "vl-queued"))
later_vl.start()
time.sleep(0.02)
hold.join(); later_text.join(); later_vl.join()
check("vision took the free slot before the text that queued first",
      order.index(("start", "vl-queued")) < order.index(("start", "text-queued")), str(order))

print("\n3b. and a same-model arrival JOINS the running batch rather than queueing")
order = []
hold = threading.Thread(target=worker, args=("text", 0.3, order, "holder-text"))
hold.start()
time.sleep(0.05)
join = threading.Thread(target=worker, args=("text", 0.05, order, "joiner"))
join.start()
hold.join(); join.join()
check("it started while the holder was still running",
      order.index(("start", "joiner")) < order.index(("end", "holder-text")), str(order))

print("\n4. a batch is bounded, so a stream of text cannot starve vision")
order = []
stop = threading.Event()


def spam():
    i = 0
    while not stop.is_set() and i < 30:
        with br.lease("text"):
            order.append(("start", f"spam{i}"))
            time.sleep(0.05)
        i += 1
        time.sleep(0.01)


spammers = [threading.Thread(target=spam) for _ in range(3)]
[t.start() for t in spammers]
time.sleep(0.15)
vl_done = threading.Event()


def vlreq():
    with br.lease("vl", timeout=10):
        order.append(("start", "VL"))
        time.sleep(0.05)
    vl_done.set()


v = threading.Thread(target=vlreq)
v.start()
got = vl_done.wait(timeout=10)
stop.set()
[t.join() for t in spammers]
v.join()
check("vision got in despite continuous text load", got)
n_before = sum(1 for k, lbl in order if k == "start" and lbl.startswith("spam")
               and order.index((k, lbl)) < order.index(("start", "VL")))
check("and did not wait for all 30 of them", n_before < 30, str(n_before))

print("\n5. a lease is released even when the body raises")
try:
    with br.lease("text"):
        raise RuntimeError("boom")
except RuntimeError:
    pass
check("nothing is left holding the GPU", br.status()["active"] == 0, str(br.status()))
with br.lease("vl") as note:
    check("and the next request proceeds immediately", note == "", note)
check("released again afterwards", br.status()["active"] == 0, str(br.status()))

print("\n6. it works ACROSS PROCESSES — the whole reason it is a file")
# The in-process version batched per process, so the browser receiver and the Claude-Code shim
# could still make the GPU ping-pong between them. Real subprocesses, or this proves nothing.
import subprocess                                        # noqa: E402
import textwrap                                          # noqa: E402

HOLD = textwrap.dedent("""
    import sys, time, os
    sys.path.insert(0, os.environ["ORACLE_SRC"])
    import oracle_broker as br
    with br.lease(sys.argv[1]):
        print("START", flush=True)
        time.sleep(float(sys.argv[2]))
    print("END", flush=True)
""")
env = {**os.environ, "ORACLE_SRC": str(Path(__file__).resolve().parent)}
holder = subprocess.Popen([sys.executable, "-c", HOLD, "text", "1.2"], env=env,
                          stdout=subprocess.PIPE, text=True)
assert holder.stdout.readline().strip() == "START"
st = br.status()
check("another process's lease is visible here", st["active"] == 1 and st["holder"] == "text",
      str(st))
t0 = time.time()
with br.lease("vl"):
    waited = time.time() - t0
check("a vl lease in THIS process waits for it", waited > 0.5, f"{waited:.2f}s")
holder.wait(timeout=10)
check("and the file shows nobody holding it afterwards", br.status()["active"] == 0,
      str(br.status()))

print("\n7. a process that dies holding the GPU does not wedge it")
KILLME = textwrap.dedent("""
    import sys, time, os
    sys.path.insert(0, os.environ["ORACLE_SRC"])
    import oracle_broker as br
    tok, _ = br.acquire("vl")
    print("HELD", flush=True)
    time.sleep(60)
""")
victim = subprocess.Popen([sys.executable, "-c", KILLME], env=env,
                          stdout=subprocess.PIPE, text=True)
assert victim.stdout.readline().strip() == "HELD"
check("it holds the GPU", br.status()["holder"] == "vl", str(br.status()))
victim.kill()
victim.wait(timeout=10)
t0 = time.time()
with br.lease("text") as note:
    took = time.time() - t0
check("a dead holder is reaped rather than blocking forever", took < 3, f"{took:.2f}s")
check("nothing is left over", br.status()["active"] == 0, str(br.status()))

print()
if fails:
    print(f"FAILED: {len(fails)} -> {', '.join(fails)}")
    sys.exit(1)
print("all broker tests passed")
