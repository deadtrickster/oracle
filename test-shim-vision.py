#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["fastapi", "uvicorn", "httpx"]
# ///
"""Test the shim's vision detour WITHOUT touching the GPU.

The expensive part of this feature (swap, read, swap back) is exactly the part you cannot iterate
on: each round trip is minutes. So the decisions around it — which images trigger a read, which
come from cache, which are left alone, and what the text model finally sees — are tested here with
the vision call and the swap stubbed out. What is under test is the PLAN, which is where the bugs
that cost real minutes live.

  ./test-shim-vision.py
"""
import asyncio
import base64
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# A cache of its own, so running the tests never pollutes (or reads) the real one.
_tmp = tempfile.mkdtemp(prefix="oracle-vision-test-")
os.environ["ORACLE_VISION_CACHE"] = str(Path(_tmp) / "cache.json")

sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("shim", HERE / "oracle-claude-shim.py")
shim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shim)
import oracle_vision  # noqa: E402  (after the env var above, so the cache path takes effect)

PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001                    ".replace(" ", ""))).decode()

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(name)


def img_block(data=PNG, mime="image/png"):
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}


def texts(body):
    """Every text the model will actually see, flattened."""
    out = []
    for m in body["messages"]:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
            continue
        for b in c:
            if b.get("type") == "text":
                out.append(b["text"])
            elif b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                out += [x.get("text", "") for x in b["content"]]
    return "\n".join(out)


print("\n1. an image in the last user turn is queued for the vision model")
body = {"messages": [{"role": "user", "content": [
    {"type": "text", "text": "what is wrong in this graph?"}, img_block()]}]}
pending = shim.plan_vision(body)
check("one image pending", len(pending) == 1, f"got {len(pending)}")
check("question is picked up from the same turn",
      shim.last_user_text(body) == "what is wrong in this graph?", shim.last_user_text(body))

print("\n2. the detour injects the reading where the image was")
calls = {"n": 0, "swaps": []}
oracle_vision.describe = lambda url, question="", label="": (
    calls.__setitem__("n", calls["n"] + 1) or f"READING[q={question!r} label={label!r}]")
shim.oracle_vram.ensure = lambda kind: iter([f"swapped to {kind}"]) if calls["swaps"].append(kind) is None else None


async def run(pending, body):
    return [n async for n in shim.vision_detour(pending, shim.last_user_text(body))]


notes = asyncio.run(run(pending, body))
seen = texts(body)
check("vision model called once", calls["n"] == 1, str(calls["n"]))
check("swapped to vl then back to text", calls["swaps"] == ["vl", "text"], str(calls["swaps"]))
check("reading is in the context", "READING[" in seen, seen[:120])
check("reading is labelled as a model's reading, not as the image",
      "read by qwen3-vl" in seen and "not as ground truth" in seen)
check("the user's question reached the vision model", "what is wrong in this graph?" in seen)
check("progress was reported", any("qwen3-vl" in n for n in notes), str(notes))

print("\n3. the SAME image on the next turn costs nothing (cache)")
calls["n"] = 0
calls["swaps"] = []
body2 = {"messages": [
    {"role": "user", "content": [{"type": "text", "text": "and now?"}, img_block()]}]}
pending2 = shim.plan_vision(body2)
check("nothing pending — served from cache", pending2 == [], str(pending2))
check("no swap, no vision call", calls["swaps"] == [] and calls["n"] == 0)
check("the reading is still in context", "READING[" in texts(body2))

print("\n4. an OLD uncached image never triggers a swap")
other = base64.b64encode(b"a-different-image-entirely").decode()
body3 = {"messages": [
    {"role": "user", "content": [img_block(data=other)]},
    {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    {"role": "user", "content": [{"type": "text", "text": "unrelated follow-up"}]}]}
pending3 = shim.plan_vision(body3)
check("history is not re-read", pending3 == [], str(pending3))
check("and it says so instead of pretending", "image not read" in texts(body3))

print("\n5. an image returned by the Read tool is found and named")
body4 = {"messages": [
    {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Read",
         "input": {"file_path": "/home/dead/shots/grafana.png"}}]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1",
         "content": [img_block(data=base64.b64encode(b"third-image").decode())]}]}]}
pending4 = shim.plan_vision(body4)
check("nested tool_result image found", len(pending4) == 1, str(len(pending4)))
check("labelled with the file it came from",
      pending4 and pending4[0]["label"] == "/home/dead/shots/grafana.png",
      pending4[0]["label"] if pending4 else "")

print("\n6. a failed read degrades to a message, it does not sink the turn")


def boom(url, question="", label=""):
    raise RuntimeError("vl server exploded")


oracle_vision.describe = boom
calls["swaps"] = []
notes4 = asyncio.run(run(pending4, body4))
check("swapped back to text anyway", calls["swaps"] == ["vl", "text"], str(calls["swaps"]))
check("failure is stated in context", "could not be read" in texts(body4), texts(body4)[:160])

print("\n7. a URL image source is not silently fetched")
body5 = {"messages": [{"role": "user", "content": [
    {"type": "image", "source": {"type": "url", "url": "https://example.org/x.png"}}]}]}
check("no pending work for a URL source", shim.plan_vision(body5) == [])
check("says it was omitted", "omitted" in texts(body5), texts(body5))

print("\n8. translation still produces a clean OpenAI request")
oai = shim.anthropic_to_openai(body2)
check("no image blocks survive into the backend request",
      "image" not in json.dumps(oai["messages"]).lower() or "READING[" in json.dumps(oai))
check("roles are intact", [m["role"] for m in oai["messages"]] == ["user"],
      str([m["role"] for m in oai["messages"]]))

print()
if fails:
    print(f"FAILED: {len(fails)} -> {', '.join(fails)}")
    sys.exit(1)
print("all vision-detour tests passed")
