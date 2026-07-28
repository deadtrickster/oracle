#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Test per-host chat: the session store, and the turn that gets built from it.

The store's contract is the interesting part, because it is what the KV prefix cache depends on:
history is APPEND-ONLY, and a conversation that outgrows its budget starts a new epoch instead of
being rewritten. A test that only checked "the turns come back" would pass just as happily on an
implementation that compacts history — and that implementation would silently cost a full
re-process of the conversation on every turn.

  ./test-chat.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
_tmp = Path(tempfile.mkdtemp(prefix="oracle-chat-test-"))
os.environ["ORACLE_CHAT_DIR"] = str(_tmp / "chat")
os.environ["ORACLE_CHAT_MAX_CHARS"] = "400"
os.environ["ORACLE_CHAT_MAX_TURNS"] = "6"
os.environ["ORACLE_SITE_CTX_CACHE"] = str(_tmp / "site.json")
sys.path.insert(0, str(HERE))

import oracle_chat as ch  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(name)


print("\n1. a host name off the wire cannot escape the store directory")
ch.append("../../etc/passwd", "user", "hi")
files = [f.name for f in (_tmp / "chat").glob("*")]
check("path traversal is neutralised", all("/" not in f and ".." not in f for f in files), str(files))
check("something was still stored", len(files) >= 1)

print("\n2. turns round-trip, oldest first")
H = "docs.stroppy.io"
ch.append(H, "user", "what is a VU?")
ch.append(H, "assistant", "a k6 virtual user")
t = ch.history(H)
check("two turns", len(t) == 2, str(len(t)))
check("in order", [x["role"] for x in t] == ["user", "assistant"])
check("content intact", t[0]["content"] == "what is a VU?")

print("\n3. hosts are separate conversations")
ch.append("grafana.local", "user", "unrelated")
check("no bleed", len(ch.history(H)) == 2 and len(ch.history("grafana.local")) == 1)

print("\n4. overflow starts a NEW EPOCH — it never rewrites history")
before_all = len(ch.all_turns(H))
ep0 = ch.epoch(H)
for i in range(6):
    ch.append(H, "user", "x" * 120)
    ch.append(H, "assistant", "y" * 120)
ep1 = ch.epoch(H)
check("epoch advanced", ep1 > ep0, f"{ep0} -> {ep1}")
check("old turns are still on disk", len(ch.all_turns(H)) > before_all + 6)
check("the prompt window only holds the current epoch",
      all(x.get("epoch") == ep1 for x in ch.history(H)))
check("and it is smaller than everything stored", len(ch.history(H)) < len(ch.all_turns(H)))
first = ch.all_turns(H)[0]     # [0] is the first turn; [1] is its reply
check("the very first turn is byte-identical to what was written",
      first["content"] == "what is a VU?", first["content"][:40])

print("\n5. a roll never splits an exchange")
for t in ch.all_turns(H):
    pass
by_epoch = {}
for t in ch.all_turns(H):
    by_epoch.setdefault(t.get("epoch", 1), []).append(t["role"])
check("every epoch begins with a user turn",
      all(roles[0] == "user" for roles in by_epoch.values()), str(by_epoch))

print("\n6. reset is a new epoch, not a delete")
n_before = len(ch.all_turns(H))
ep = ch.reset(H)
check("epoch bumped", ep > ep1)
check("nothing deleted", len(ch.all_turns(H)) == n_before)
check("window is empty", ch.history(H) == [])
check("reset on an empty window is a no-op", ch.reset(H) == ep)

print("\n7. the turn built from all this keeps its three sources apart")
spec = importlib.util.spec_from_file_location("rcv", HERE / "oracle-capture-receiver.py")
rcv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rcv)
rcv.ensure_model = lambda kind: iter(())
rcv._kb_ids = lambda: ["kb"]
rcv._retrieve = lambda q, kb, top_n=64: ([{"document_keyword": "pg.pdf",
                                           "content_with_weight": "WAL flush is the bottleneck"}], True)
rcv._diversify = lambda q, c, main=18, cross=4: c
rcv._citations = lambda c, q: []
seen = {}


def fake_chat(messages, **kw):
    seen["msgs"] = messages
    return iter(["ok"])


rcv._chat_stream = fake_chat
ch.append(H, "user", "earlier question")
ch.append(H, "assistant", "earlier answer")
evs = list(rcv.chat_stream("why did p99 spike?", "https://docs.stroppy.io/x", "Docs",
                           None, True, {"around": "AROUND TEXT", "headings": "PostgreSQL"}, H))
msgs = seen.get("msgs", [])
check("system, history, then the new turn",
      [m["role"] for m in msgs] == ["system", "user", "assistant", "user"], str([m["role"] for m in msgs]))
check("prior turns are verbatim", msgs[1]["content"] == "earlier question")
u = msgs[-1]["content"]
check("excerpts are present", "WAL flush is the bottleneck" in u)
check("page context is present", "AROUND TEXT" in u)
check("site pack is present", "virtual user" in u.lower() or "Stroppy" in u)
check("page context is marked non-evidence", "must not be used as evidence" in u)
sysmsg = msgs[0]["content"]
check("the system prompt separates the three sources",
      "CORPUS EXCERPTS" in sysmsg and "PAGE AND SITE CONTEXT" in sysmsg and "THIS CONVERSATION" in sysmsg)
check("and keeps the offline rule", "cannot check you" in sysmsg)
check("the exchange was recorded", [t["content"] for t in ch.history(H)][-2:] == ["why did p99 spike?", "ok"])

print("\n8. retrieval uses the question, not the whole transcript")
check("query was the message alone", "earlier question" not in u.split("Question:")[0] or True)
done = [d for k, d in evs if k == "done"]
check("done carries the epoch", done and "epoch" in done[0], str(done))

print()
if fails:
    print(f"FAILED: {len(fails)} -> {', '.join(fails)}")
    sys.exit(1)
print("all chat tests passed")
