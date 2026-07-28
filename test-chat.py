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
import json
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


def fake_chat(messages, tools, out, **kw):
    """Chat goes through the TOOL-aware stream now; retrieval is a tool the model may call, not
    something every turn does unconditionally."""
    seen["msgs"] = messages
    seen["tools"] = [t["function"]["name"] for t in tools]
    out["text"] = "ok"
    out["tool_calls"] = []
    yield "ok"


rcv._chat_stream_tools = fake_chat
ch.append(H, "user", "earlier question")
ch.append(H, "assistant", "earlier answer")
evs = list(rcv.chat_stream("why did p99 spike?", "https://docs.stroppy.io/x", "Docs",
                           None, True, {"around": "AROUND TEXT", "headings": "PostgreSQL"}, H))
msgs = seen.get("msgs", [])
# system, then the task+page block, then the transcript. The task block is only sent on the first
# step of a loop; the transcript that follows is append-only, which is what keeps the prefix warm.
check("system, task, then the transcript",
      [m["role"] for m in msgs] == ["system", "user", "user", "assistant", "user"],
      str([m["role"] for m in msgs]))
check("prior turns are verbatim", msgs[2]["content"] == "earlier question")
check("the new question is last", msgs[-1]["content"] == "why did p99 spike?")
task = msgs[1]["content"]
check("page context is present", "AROUND TEXT" in task)
check("page context is marked non-evidence", "must not be used as evidence" in task)
check("the task tells it to look rather than assume", "answered by looking" in task)
check("retrieval is offered as a TOOL, not applied unconditionally",
      "search_corpus" in seen.get("tools", []), str(seen.get("tools")))
sysmsg = msgs[0]["content"]
# The site pack lives in the CACHED SYSTEM PREFIX, not the turn — identical every turn, so
# repeating it per turn would waste the prefix cache and re-state 2.7k tokens inside a conversation
# that is already growing. See test-prefix.py.
check("site pack is in the cached prefix", "virtual user" in sysmsg.lower())
check("and NOT repeated in the turn",
      not any("virtual user" in m["content"].lower() for m in msgs[1:]))
check("the system prompt separates the kinds of material",
      "CORPUS EXCERPTS" in sysmsg and "SITE REFERENCE MATERIAL" in sysmsg
      and "PAGE CONTEXT" in sysmsg and "THE CONVERSATION" in sysmsg)
check("and keeps the offline rule", "cannot check you" in sysmsg)
check("the exchange was recorded", [t["content"] for t in ch.history(H)][-2:] == ["why did p99 spike?", "ok"])

print("\n7b. a region sent to chat becomes TEXT in the transcript")
rcv.oracle_vision.describe = lambda url, question="", label="": "The panel shows p99 at 412ms."
rcv.oracle_vision.cached = lambda k: None
rcv.oracle_vision.remember = lambda *a, **k: None
ch.reset(H)
evs2 = list(rcv.chat_stream("what is wrong here?", "https://docs.stroppy.io/x", "Docs", None, True,
                            None, H, image="iVBORw0KGgo=", image_mime="image/png"))
u2 = "\n".join(m["content"] for m in seen["msgs"])
check("the reading is in the prompt", "p99 at 412ms" in u2)
check("labelled as a model's reading, not as the image",
      "read by qwen3-vl" in u2 and "not as ground truth" in u2)
stored = ch.history(H)[0]["content"]
check("and the READING, not the pixels, is what persists",
      "p99 at 412ms" in stored and "iVBORw0KGgo" not in stored)
check("so a later turn can still refer back to it", "read by qwen3-vl" in stored)

ch.reset(H)
list(rcv.chat_stream("", "https://docs.stroppy.io/x", "Docs", None, True, None, H,
                     image="iVBORw0KGgo="))
allmsgs = "\n".join(m["content"] for m in seen["msgs"])
check("a region with NO question still carries the reading",
      "p99 at 412ms" in allmsgs)
check("and the transcript holds it, not the pixels",
      "iVBORw0KGgo" not in "".join(t.get("content", "") for t in ch.history(H)))

print("\n7c. sessions: quick queries do not land in the chat you type in")
ch.reset(H)
ch.reset(H, "quick")
list(rcv.chat_stream("typed into the panel", "https://docs.stroppy.io/x", "D", None, False, None, H))
list(rcv.chat_stream("explain this selection", "https://docs.stroppy.io/x", "D", None, False, None, H,
                     session="quick"))
main_said = [t["content"] for t in ch.history(H)]
quick_said = [t["content"] for t in ch.history(H, "quick")]
check("the panel's conversation has only its own turn", "typed into the panel" in main_said[0])
check("and does not have the quick one", not any("explain this selection" in c for c in main_said))
check("the quick session has its own", "explain this selection" in quick_said[0])
check("they are separate files",
      ch._path(H, "quick") != ch._path(H) and ch._path(H, "quick").exists())
check("main keeps the legacy filename", ch._path(H).name == f"{ch._safe(H)}.json")
names = {(s["host"], s["session"]) for s in ch.sessions(H)}
check("both are listed for this host", names == {(H, "main"), (H, "quick")}, str(names))
check("and listing is host-scoped", all(s["host"] == H for s in ch.sessions(H)))
ch.delete(H, "quick")
check("deleting one leaves the other", ch.history(H) and not ch.history(H, "quick"))

print("\n7d. an image rides with the turn for the UI, never into the prompt")
ch.reset(H)
ch.append(H, "user", "look", image="data:image/png;base64,AAAA")
t = ch.history(H)[0]
check("the transcript keeps it", t.get("image", "").startswith("data:image/png"))
check("but the model's messages do not",
      "AAAA" not in json.dumps(ch.to_messages(ch.history(H)), ensure_ascii=False))

print("\n8. the turn completes")
done = [d for k, d in evs if k == "done"]
check("done carries the epoch", done and "epoch" in done[0], str(done))

print()
if fails:
    print(f"FAILED: {len(fails)} -> {', '.join(fails)}")
    sys.exit(1)
print("all chat tests passed")
