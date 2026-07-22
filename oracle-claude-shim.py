# /// script
# requires-python = ">=3.10"
# dependencies = ["fastapi", "uvicorn", "httpx"]
# ///
"""Oracle Claude shim — Anthropic Messages API  <->  Ollama OpenAI-compat API.

Why this exists: Ollama's *Anthropic* endpoint mangles ~33% of qwen3-coder's
streaming tool calls under load (leaks the raw `<function=...>` text instead of a
tool_use block). Ollama's *OpenAI* endpoint parses the identical model output
100% cleanly. Claude Code only speaks streaming-Anthropic, so this shim sits in
between: it accepts Claude Code's Anthropic request, calls Ollama's OpenAI
`/v1/chat/completions` (the robust path) WITH real streaming, and translates the
OpenAI SSE stream back into Anthropic SSE events on the fly. All localhost/offline.

  Claude Code --Anthropic /v1/messages--> [shim :11435] --OpenAI--> Ollama :11434

Point Claude Code at it:  ANTHROPIC_BASE_URL=http://localhost:11435
"""
import json
import os
import re
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

OLLAMA = os.environ.get("ORACLE_OLLAMA_URL", "http://localhost:11434").rstrip("/")
OAI_URL = f"{OLLAMA}/v1/chat/completions"

app = FastAPI()

_STOP_MAP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens",
             "content_filter": "end_turn", "function_call": "tool_use", None: "end_turn"}

# ---- salvage: recover tool calls qwen3-coder leaked into text as its native XML
# (`<function=NAME><parameter=P>V</parameter></function>`) or a `<tool_call>{json}</tool_call>`
# that neither Ollama endpoint parsed. Even the OpenAI endpoint leaks ~5% under load; this
# takes the residual to ~0 regardless of which endpoint the model slipped on.
_MARKERS = ("<function=", "<tool_call")
_FUNC_RE = re.compile(r"<function=([^>\s]+)\s*>(.*?)</function>", re.DOTALL)
_PARAM_RE = re.compile(r"<parameter=([^>\s]+)\s*>(.*?)</parameter>", re.DOTALL)
_JSONCALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def _coerce(v: str):
    """Best-effort scalar coercion — leaked params are all strings, but schemas often
    want int/bool (e.g. lsp_hover line/col). Leave anything ambiguous as a string."""
    s = v.strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d*\.\d+", s):
        return float(s)
    return s


def parse_leaked_tools(text: str):
    """(clean_text, [(name, input_dict), ...]) — salvage leaked tool calls from text."""
    calls = []
    for m in _FUNC_RE.finditer(text):
        params = {p.group(1).strip(): _coerce(p.group(2)) for p in _PARAM_RE.finditer(m.group(2))}
        calls.append((m.group(1).strip(), params))
    if not calls:
        for m in _JSONCALL_RE.finditer(text):
            try:
                obj = json.loads(m.group(1))
            except Exception:
                continue
            if obj.get("name"):
                calls.append((obj["name"], obj.get("arguments") or obj.get("input") or {}))
    if not calls:
        return text, []
    clean = _JSONCALL_RE.sub("", _FUNC_RE.sub("", text))
    clean = clean.replace("<tool_call>", "").replace("</tool_call>", "").strip()
    return clean, calls


def _hold_split(buf: str):
    """(emit_now, keep) — keep is the longest suffix of buf that is a prefix of a tool
    marker, so we never stream half of a `<function=` to the client before salvaging."""
    maxm = max(len(m) for m in _MARKERS)
    keep = 0
    for k in range(1, min(len(buf), maxm) + 1):
        if any(m.startswith(buf[-k:]) for m in _MARKERS):
            keep = k
    return buf[:len(buf) - keep], buf[len(buf) - keep:]


def _text_of(content) -> str:
    """Flatten an Anthropic content value (str | list of blocks) to plain text."""
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text", ""))
        elif isinstance(b, str):
            out.append(b)
    return "\n".join(out)


def anthropic_to_openai(body: dict) -> dict:
    """Translate an Anthropic Messages request into an OpenAI chat-completions one."""
    msgs = []
    system = body.get("system")
    if system:
        msgs.append({"role": "system", "content": _text_of(system)})

    for m in body.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        # content is a list of blocks
        text_parts, tool_calls, tool_results = [], [], []
        for b in content or []:
            t = b.get("type")
            if t == "text":
                text_parts.append(b.get("text", ""))
            elif t == "tool_use":  # assistant asked to call a tool
                tool_calls.append({
                    "id": b.get("id"), "type": "function",
                    "function": {"name": b.get("name"),
                                 "arguments": json.dumps(b.get("input", {}))}})
            elif t == "tool_result":  # user returns a tool's output
                tool_results.append({
                    "role": "tool", "tool_call_id": b.get("tool_use_id"),
                    "content": _text_of(b.get("content", ""))})
            elif t == "image":
                text_parts.append("[image omitted — model is text-only]")
        if role == "assistant":
            am = {"role": "assistant", "content": "\n".join(text_parts)}
            if tool_calls:
                am["tool_calls"] = tool_calls
            msgs.append(am)
        else:  # user
            # tool_result blocks become their own role:tool messages (must follow the
            # assistant tool_calls); any plain text becomes a user message.
            msgs.extend(tool_results)
            if text_parts:
                msgs.append({"role": "user", "content": "\n".join(text_parts)})

    out = {"model": body.get("model"), "messages": msgs,
           "stream": bool(body.get("stream")),
           "stream_options": {"include_usage": True}}
    if body.get("max_tokens"):
        out["max_tokens"] = body["max_tokens"]
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    if body.get("tools"):
        out["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object", "properties": {}})}}
            for t in body["tools"]]
    tc = body.get("tool_choice")
    if tc:
        kind = tc.get("type")
        if kind == "auto":
            out["tool_choice"] = "auto"
        elif kind == "any":
            out["tool_choice"] = "required"
        elif kind == "tool" and tc.get("name"):
            out["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
    return out


def backend_error_text(status: int | None, detail: str) -> str:
    """A VISIBLE explanation of a backend failure.

    Why this exists (2026-07-22): while llama-server was down (the VL model still held the VRAM,
    so qwen-next OOMed and then spent a minute loading), every request came back as an assistant
    message with an EMPTY content list. Structurally valid, semantically nothing — so the CLI
    printed "[Your previous response had no visible output]" and the user saw a brain-dead agent
    six turns in a row, with no hint that the model simply was not up. The shim HAD the 503 and
    threw it away. Axiom 2: the harness must surface what it knows instead of failing silently.
    """
    if status == 503 or "loading" in detail.lower():
        hint = ("the local model is still LOADING into VRAM — wait for it and retry "
                "(`curl -s localhost:18080/health`)")
    elif status is None:
        hint = ("the local model server is NOT REACHABLE — check "
                "`systemctl --user status oracle-qwen-next` and whether another process "
                "(e.g. a VL server) is holding the GPU: `nvidia-smi`")
    else:
        hint = "the local model server returned an error"
    return f"[shim] {hint}.\nbackend status={status} detail={detail[:300]}"


def error_message(model: str, text: str) -> dict:
    """A well-formed Anthropic message whose content is the error — never an empty turn."""
    return {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant",
            "model": model, "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def stream_translate(oai_req: dict, model: str):
    """Call Ollama's OpenAI streaming endpoint and yield Anthropic SSE events, holding
    back ambiguous text so a leaked `<function=...>` can be salvaged into a tool_use."""
    yield _sse("message_start", {"type": "message_start", "message": {
        "id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant",
        "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0}}})
    yield _sse("ping", {"type": "ping"})

    cur = None            # None | "text" | ("tool", key)
    next_idx = 0
    block_idx = {}        # key -> anthropic block index ("text" or a tool key)
    stop_reason = "end_turn"
    out_tokens = 0
    text_hold = ""        # ambiguous trailing text not yet emitted
    salvage = None        # None, or accumulated text once a tool marker is seen

    def close_cur():
        nonlocal cur
        if cur is None:
            return []
        idx = block_idx["text"] if cur == "text" else block_idx[cur[1]]
        cur = None
        return [_sse("content_block_stop", {"type": "content_block_stop", "index": idx})]

    def emit_text(s):
        nonlocal cur, next_idx
        outs = []
        if cur != "text":
            outs += close_cur()
            block_idx["text"] = next_idx
            next_idx += 1
            cur = "text"
            outs.append(_sse("content_block_start", {
                "type": "content_block_start", "index": block_idx["text"],
                "content_block": {"type": "text", "text": ""}}))
        outs.append(_sse("content_block_delta", {
            "type": "content_block_delta", "index": block_idx["text"],
            "delta": {"type": "text_delta", "text": s}}))
        return outs

    def open_tool(key, tid, name):
        nonlocal cur, next_idx
        outs = close_cur()
        block_idx[key] = next_idx
        next_idx += 1
        cur = ("tool", key)
        outs.append(_sse("content_block_start", {
            "type": "content_block_start", "index": block_idx[key],
            "content_block": {"type": "tool_use", "id": tid, "name": name, "input": {}}}))
        return outs

    def tool_args(key, frag):
        return [_sse("content_block_delta", {
            "type": "content_block_delta", "index": block_idx[key],
            "delta": {"type": "input_json_delta", "partial_json": frag}})]

    def fail_events(status, detail):
        """Terminate the SSE stream with a VISIBLE error instead of an empty turn."""
        evs = list(emit_text(backend_error_text(status, detail))) + list(close_cur())
        evs.append(_sse("message_delta", {"type": "message_delta",
                                          "delta": {"stop_reason": "end_turn",
                                                    "stop_sequence": None},
                                          "usage": {"output_tokens": 0}}))
        evs.append(_sse("message_stop", {"type": "message_stop"}))
        return evs

    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream("POST", OAI_URL, json=oai_req) as resp:
                if resp.status_code != 200:
                    # An error body is NOT SSE, so the loop below would yield nothing at all and
                    # the user would get an empty turn. Surface it as text.
                    body = (await resp.aread()).decode("utf-8", "replace")
                    for ev in fail_events(resp.status_code, body):
                        yield ev
                    return
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except Exception:
                        continue
                    if chunk.get("usage"):
                        out_tokens = chunk["usage"].get("completion_tokens", out_tokens)
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    fin = choices[0].get("finish_reason")

                    txt = delta.get("content")
                    if txt:
                        if salvage is not None:
                            salvage += txt
                        else:
                            text_hold += txt
                            hit = [text_hold.find(m) for m in _MARKERS if m in text_hold]
                            if hit:  # a tool marker leaked into text -> start salvaging
                                pos = min(hit)
                                if text_hold[:pos]:
                                    for s in emit_text(text_hold[:pos]):
                                        yield s
                                salvage = text_hold[pos:]
                                text_hold = ""
                            else:
                                emit, text_hold = _hold_split(text_hold)
                                if emit:
                                    for s in emit_text(emit):
                                        yield s

                    for tc in delta.get("tool_calls") or []:
                        oi = ("oai", tc.get("index", 0))
                        fn = tc.get("function") or {}
                        if oi not in block_idx:
                            for s in open_tool(oi, tc.get("id") or ("toolu_" + uuid.uuid4().hex[:24]),
                                               fn.get("name") or ""):
                                yield s
                        else:
                            cur = ("tool", oi)
                        if fn.get("arguments"):
                            for s in tool_args(oi, fn["arguments"]):
                                yield s

                    if fin:
                        stop_reason = _STOP_MAP.get(fin, "end_turn")

        except Exception as e:   # backend not listening / dropped mid-stream
            for ev in fail_events(None, repr(e)):
                yield ev
            return

    # end of stream: salvage a leaked call, or flush any held text
    if salvage is not None:
        clean, calls = parse_leaked_tools(salvage)
        if clean:
            for s in emit_text(clean):
                yield s
        for i, (name, inp) in enumerate(calls):
            key = ("salv", i)
            for s in open_tool(key, "toolu_" + uuid.uuid4().hex[:24], name):
                yield s
            for s in tool_args(key, json.dumps(inp)):
                yield s
            stop_reason = "tool_use"
    elif text_hold:
        for s in emit_text(text_hold):
            yield s

    for s in close_cur():
        yield s
    yield _sse("message_delta", {"type": "message_delta",
                                 "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                                 "usage": {"output_tokens": out_tokens}})
    yield _sse("message_stop", {"type": "message_stop"})


def openai_to_anthropic_full(oai: dict, model: str) -> dict:
    """Translate a NON-streaming OpenAI completion into an Anthropic Messages response."""
    choice = (oai.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = []
    tool_calls = msg.get("tool_calls") or []
    text = msg.get("content") or ""
    salvaged = []
    if text and not tool_calls and any(m in text for m in _MARKERS):
        text, salvaged = parse_leaked_tools(text)  # recover a leaked call from text
    if text:
        content.append({"type": "text", "text": text})
    for tc in tool_calls:
        fn = tc.get("function") or {}
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except Exception:
            inp = {}
        content.append({"type": "tool_use", "id": tc.get("id") or ("toolu_" + uuid.uuid4().hex[:24]),
                        "name": fn.get("name"), "input": inp})
    for name, inp in salvaged:
        content.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:24],
                        "name": name, "input": inp})
    usage = oai.get("usage") or {}
    stop = "tool_use" if salvaged else _STOP_MAP.get(choice.get("finish_reason"), "end_turn")
    return {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant",
            "model": model, "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                      "output_tokens": usage.get("completion_tokens", 0)}}


@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    model = body.get("model", "qwen3-coder:30b")
    oai_req = anthropic_to_openai(body)
    if body.get("stream"):
        return StreamingResponse(stream_translate(oai_req, model),
                                 media_type="text/event-stream")
    oai_req["stream"] = False
    oai_req.pop("stream_options", None)
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            r = await client.post(OAI_URL, json=oai_req)
        except Exception as e:
            return JSONResponse(error_message(model, backend_error_text(None, repr(e))))
        try:
            payload = r.json()
        except Exception:
            payload = {}
        # A 503 ("Loading model") or any error body has no `choices`, which would translate into
        # an assistant message with an EMPTY content list — a silently dead turn. Say it instead.
        if r.status_code != 200 or not payload.get("choices"):
            return JSONResponse(error_message(
                model, backend_error_text(r.status_code, json.dumps(payload)[:400] or r.text)))
        return JSONResponse(openai_to_anthropic_full(payload, model))


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Claude Code may probe this; give a cheap char/4 estimate (offline-safe)."""
    body = await request.json()
    chars = len(_text_of(body.get("system", "")))
    for m in body.get("messages", []):
        chars += len(_text_of(m.get("content", "")))
    return JSONResponse({"input_tokens": max(1, chars // 4)})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ORACLE_SHIM_PORT", "11435")))
