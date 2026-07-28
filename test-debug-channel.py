#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Test the debug channel — the thing that answers "is page context ever actually injected?"

The whole point of this channel is that you should not have to read the source to find out what
went into the prompt. So the test asserts the reverse of the usual thing: not that the code is
correct, but that the code REPORTS what it did, truthfully, including when it decided to skip a
step. A debug view that omits the skip is worse than none — it reads as "this never happens".

  ./test-debug-channel.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["ORACLE_SITE_CTX_CACHE"] = str(Path(tempfile.mkdtemp()) / "c.json")
sys.path.insert(0, str(HERE))

spec = importlib.util.spec_from_file_location("rcv", HERE / "oracle-capture-receiver.py")
rcv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rcv)

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(name)


def events(gen):
    return [(k, v) for k, v in gen]


def dbg(evs, stage):
    for k, v in evs:
        if k == "debug" and v.get("stage") == stage:
            return v
    return None


# --- stubs: nothing here may touch the GPU or the corpus -----------------------------------------
rcv.ensure_model = lambda kind, host="": iter(())
rcv.text_available = lambda force=False: True
rcv._chat_stream = lambda msgs, **kw: iter(["answer"])
rcv._kb_ids = lambda: ["kb"]
rcv._retrieve = lambda q, kb, top_n=64: ([{"document_keyword": "d.pdf",
                                           "content_with_weight": "excerpt text"}], True)
rcv._diversify = lambda q, c, main=18, cross=4: c
rcv._citations = lambda c, q: []

PNG = "data:image/png;base64,iVBORw0KGgo="

print("\n1. debug is OFF by default — no events, no leaked prompt")
evs = events(rcv.vision_stream(PNG, "", "https://x.example/p", "T", page_text="hello page"))
check("no debug events", not any(k == "debug" for k, _ in evs))

print("\n2. vision reports the page context it was GIVEN")
evs = events(rcv.vision_stream(PNG, "what is this", "https://x.example/p", "T",
                               page_text="PAGE TEXT HERE", crop_text="CROP", debug=True))
inp = dbg(evs, "input")
check("an input event exists", inp is not None)
check("page text size is reported", inp and inp.get("page_text_chars") == len("PAGE TEXT HERE"), str(inp))
check("crop text size is reported", inp and inp.get("crop_text_chars") == 4)

print("\n3. and reports the context it actually COMPOSED, verbatim")
ctx = dbg(evs, "context sent to qwen3-vl")
check("a context event exists", ctx is not None)
check("it carries the full text", ctx and "PAGE TEXT HERE" in ctx.get("text", ""), (ctx or {}).get("text", "")[:120])
check("page text really is injected", ctx and "PAGE TEXT HERE" in ctx["text"])
check("sections are itemised", ctx and isinstance(ctx.get("sections"), list) and ctx["sections"])

print("\n4. a SKIPPED step says so, and says why")
sk = dbg(evs, "summarise skipped")
check("skip is reported", sk is not None)
check("with the reason", sk and "<" in sk.get("why", ""), str(sk))
long_page = "x" * (rcv.VL_SUMMARIZE_OVER + 10)
rcv._summarize_for_vl = lambda t, u, ti: "A BRIEF"
evs2 = events(rcv.vision_stream(PNG, "", "https://x.example/p", "T", page_text=long_page, debug=True))
su = dbg(evs2, "summarised")
check("a summary that DID run is reported", su is not None and su.get("ok") is True)
check("with its output", su and su.get("text") == "A BRIEF")
ctx2 = dbg(evs2, "context sent to qwen3-vl")
check("the summary, not the raw page, is what went in",
      ctx2 and "A BRIEF" in ctx2["text"] and "xxxxxxxxxx" not in ctx2["text"])

print("\n5. the grounded path reports its prompt too, site pack included")
evs3 = events(rcv.explain_stream("p99 at 50 VUs", "https://docs.stroppy.io/x", "T", None, True))
pr = dbg(evs3, "prompt sent to the text model")
check("a prompt event exists", pr is not None)
check("site context is measured", pr and pr.get("site_context_chars", 0) > 1000, str((pr or {}).get("site_context_chars")))
check("the excerpts are in it", pr and "excerpt text" in pr.get("text", ""))
check("the system prompt is shown", pr and "corpus" in (pr.get("system") or "").lower())

print("\n5b. page context: the selection's surroundings, marked non-evidence")
evs4 = events(rcv.explain_stream(
    "it collapses under contention", "https://docs.stroppy.io/x", "Stroppy docs", None, True,
    {"around": "TPC-B hammers one branch row.", "headings": "PostgreSQL \u203a Locks", "page": ""}))
pr2 = dbg(evs4, "prompt sent to the text model")
check("page context is measured", pr2 and pr2.get("page_context_chars", 0) > 0)
u2 = (pr2 or {}).get("text", "")
check("the heading chain is in the prompt", "PostgreSQL \u203a Locks" in u2)
check("the surrounding text is in the prompt", "TPC-B hammers one branch row." in u2)
check("marked not citable", "must not be cited" in u2)
check("and not usable as evidence", "must not be used as evidence" in u2)
check("excerpts stay LAST, closest to the answer", u2.rindex("Excerpts:") > u2.rindex("Where the user"))
evs5 = events(rcv.explain_stream("x", "https://a.example/p", "T", None, True, None))
pr3 = dbg(evs5, "prompt sent to the text model")
check("with no extension context the page is still identified",
      pr3 and "a.example" in pr3.get("text", ""))

print("\n6. a full-page screenshot is labelled as stitched, not as one screenful")
c = rcv._vl_context("https://x.example/p", "T", "", False, "", None, "fullpage")
check("says it was scrolled and stitched", "stitched" in c)
check("warns content was never on screen together", "never visible at the same time" in c)
check("warns it may be cut off", "cut off" in c)

print()
if fails:
    print(f"FAILED: {len(fails)} -> {', '.join(fails)}")
    sys.exit(1)
print("all debug-channel tests passed")
