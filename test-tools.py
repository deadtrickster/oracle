#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Test the chat tool harness: the loop, the read/act gate, and the browser hand-off.

The model is stubbed. What is under test is the HARNESS — which tools are offered, what happens to
a call it cannot run itself, what the transcript ends up containing, and whether an unanswered tool
call can ever reach the model. Those are the parts that must be right regardless of which model is
behind them, and the parts that a live test would only exercise by luck.

  ./test-tools.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
_tmp = Path(tempfile.mkdtemp(prefix="oracle-tools-test-"))
os.environ["ORACLE_CHAT_DIR"] = str(_tmp / "chat")
os.environ["ORACLE_SITE_CTX_CACHE"] = str(_tmp / "site.json")
sys.path.insert(0, str(HERE))

import oracle_chat as ch                                  # noqa: E402
import oracle_tools as tl                                 # noqa: E402

_spec = importlib.util.spec_from_file_location("rcv", HERE / "oracle-capture-receiver.py")
rcv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rcv)

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(name)


# Point agent mode at a private file BEFORE anything reads it. Without this the test inherits the
# live runtime state, and since HOST below is a real host that may be in agent mode on this machine,
# the suite blocks waiting for a human to answer a question nobody asked.
os.environ["ORACLE_AGENT_STATE"] = os.path.join(tempfile.mkdtemp(), "agent.json")

HOST = "stage.cloud.stroppy.io"
URL = f"https://{HOST}/t/default/runs/abc"

rcv.ensure_model = lambda kind, host="": iter(())
rcv._kb_ids = lambda: ["kb"]
rcv._retrieve = lambda q, kb, top_n=64: ([{"document_keyword": "pg.pdf",
                                           "content_with_weight": f"corpus text about {q}"}], True)
rcv._diversify = lambda q, c, main=18, cross=4: c
rcv._citations = lambda c, q: [{"n": 1, "doc": "pg.pdf"}]

# --- a scripted model: each entry is one turn's reply -------------------------------------------
SCRIPT = []
SEEN = []


def fake_stream(msgs, tools, out, **kw):
    SEEN.append({"msgs": msgs, "tools": [t["function"]["name"] for t in tools]})
    reply = SCRIPT.pop(0) if SCRIPT else {"text": "done."}
    out["text"] = reply.get("text", "")
    out["tool_calls"] = reply.get("tool_calls", [])
    if out["text"]:
        yield out["text"]


rcv._chat_stream_tools = fake_stream


def call(name, args, cid="c1"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def run(gen):
    return [(k, v) for k, v in gen]


def ev(evs, kind):
    return [v for k, v in evs if k == kind]


print("\n1. acting tools are ABSENT until the host is enabled")
ch.reset(HOST)
SCRIPT[:] = [{"text": "hello"}]
SEEN.clear()
run(rcv.chat_stream("hi", URL, "t", None, False, None, HOST))
offered = SEEN[0]["tools"]
check("read tools offered", {"search_corpus", "read_page", "look_at_page"} <= set(offered), str(offered))
check("acting tools withheld", "click" not in offered and "type_text" not in offered, str(offered))

ch.set_actions(HOST, True)
ch.reset(HOST)
SCRIPT[:] = [{"text": "hello"}]
SEEN.clear()
run(rcv.chat_stream("hi", URL, "t", None, False, None, HOST))
check("acting tools appear once enabled", "click" in SEEN[0]["tools"], str(SEEN[0]["tools"]))
check("and the gate is per host", not ch.actions_allowed("other.example"))

print("\n2. a corpus search runs on the receiver and the loop continues")
ch.reset(HOST)
SCRIPT[:] = [{"tool_calls": [call("search_corpus", {"query": "wal flush"})]},
             {"text": "WAL flush is the bottleneck [1]."}]
SEEN.clear()
evs = run(rcv.chat_stream("why is it slow?", URL, "t", None, False, None, HOST))
check("two model calls", len(SEEN) == 2, str(len(SEEN)))
check("citations were emitted", ev(evs, "sources") and ev(evs, "sources")[0]["citations"])
check("the answer streamed", "WAL flush" in "".join(d["text"] for d in ev(evs, "delta")))
roles = [t["role"] for t in ch.history(HOST)]
check("transcript records user, assistant+call, tool, assistant",
      roles == ["user", "assistant", "tool", "assistant"], str(roles))
check("the tool result is in the transcript",
      "corpus text about wal flush" in ch.history(HOST)[2]["content"])

print("\n3. a browser tool ENDS the turn and asks the extension")
ch.reset(HOST)
SCRIPT[:] = [{"text": "Let me look.", "tool_calls": [call("read_page", {}, "c9")]}]
evs = run(rcv.chat_stream("what is on this page?", URL, "t", None, False, None, HOST))
reqs = ev(evs, "tool_request")
check("a tool_request was emitted", bool(reqs), str(evs[-3:]))
check("it names the call", reqs and reqs[0]["calls"][0]["name"] == "read_page")
check("it explains itself in words", reqs and "read the page" in reqs[0]["calls"][0]["says"])
check("it marks read tools as non-acting", reqs and reqs[0]["calls"][0]["acting"] is False)
check("the turn ends pending", ev(evs, "done") and ev(evs, "done")[0].get("pending_tools"))
check("the call is remembered as pending", len(ch.pending_tools(HOST)) == 1)

print("\n4. the extension reports back and the loop resumes")
SCRIPT[:] = [{"text": "The page shows run abc, completed."}]
SEEN.clear()
evs = run(rcv.chat_tool_results(HOST, [{"id": "c9", "name": "read_page",
                                        "content": "Run abc — completed — 2h48m"}], URL, "t"))
check("nothing is pending now", ch.pending_tools(HOST) == [])
check("the model saw the tool result",
      # ensure_ascii=False, or the em-dash becomes — and this asserts nothing
      "Run abc — completed" in json.dumps(SEEN[0]["msgs"], ensure_ascii=False))
check("it answered", "completed" in "".join(d["text"] for d in ev(evs, "delta")))

print("\n5. an unanswered call can never reach the model")
ch.reset(HOST)
SCRIPT[:] = [{"tool_calls": [call("read_page", {}, "a1"), call("look_at_page", {}, "a2")]}]
run(rcv.chat_stream("look", URL, "t", None, False, None, HOST))
check("two calls pending", len(ch.pending_tools(HOST)) == 2)
SCRIPT[:] = [{"text": "ok"}]
SEEN.clear()
run(rcv.chat_tool_results(HOST, [{"id": "a1", "name": "read_page", "content": "text"}], URL, "t"))
sent = json.dumps(SEEN[0]["msgs"])
check("the skipped call was filled with an error, not dropped",
      "not executed" in sent, sent[-300:])
ids = [t.get("tool_call_id") for t in ch.history(HOST) if t["role"] == "tool"]
check("every call has a result", set(ids) == {"a1", "a2"}, str(ids))

print("\n5b. an ABANDONED call still yields a well-formed prompt")
# Chrome may kill the MV3 service worker mid-loop, so the result never arrives. The transcript is
# then correct about what happened and incomplete as a prompt — every tool_call needs a result or
# the chat template cannot render it.
ch.reset(HOST)
ch.append(HOST, "user", "look at this")
ch.append(HOST, "assistant", "looking", tool_calls=[call("look_at_page", {}, "dead1")])
ch.append(HOST, "user", "still there?")
msgs = ch.to_messages(ch.history(HOST))
roles = [m["role"] for m in msgs]
check("a synthetic result is inserted", roles == ["user", "assistant", "tool", "user"], str(roles))
check("it says the step never ran", "never completed" in msgs[2]["content"])
check("and matches the call id", msgs[2]["tool_call_id"] == "dead1")

print("\n6. a stale result from an abandoned loop is ignored")
n_before = len(ch.history(HOST))
SCRIPT[:] = [{"text": "ok"}]
run(rcv.chat_tool_results(HOST, [{"id": "ghost", "name": "read_page", "content": "x"}], URL, "t"))
check("no turn was appended for it",
      not any(t.get("tool_call_id") == "ghost" for t in ch.history(HOST)))

print("\n7. the loop cannot run away")
ch.reset(HOST)
SCRIPT[:] = [{"tool_calls": [call("search_corpus", {"query": f"q{i}"}, f"s{i}")]} for i in range(40)]
SEEN.clear()
evs = run(rcv.chat_stream("loop forever", URL, "t", None, False, None, HOST))
check(f"stopped at {rcv.CHAT_MAX_STEPS} steps", len(SEEN) == rcv.CHAT_MAX_STEPS, str(len(SEEN)))
check("and said so", "stopped after" in "".join(d["text"] for d in ev(evs, "delta")))

print("\n8. a leaked tool call is salvaged from prose")
text, calls = rcv._salvage_tool_calls(
    'sure<function=read_page><parameter=selector>.main</parameter></function>')
check("recovered", len(calls) == 1 and calls[0]["function"]["name"] == "read_page", str(calls))
check("arguments parsed", json.loads(calls[0]["function"]["arguments"]) == {"selector": ".main"})
check("prose kept", text.strip() == "sure")
_, c2 = rcv._salvage_tool_calls('<tool_call>{"name": "click", "arguments": {"text": "LOGS"}}</tool_call>')
check("json form too", len(c2) == 1 and c2[0]["function"]["name"] == "click", str(c2))

print()
if fails:
    print(f"FAILED: {len(fails)} -> {', '.join(fails)}")
    sys.exit(1)
print("all tool-harness tests passed")
