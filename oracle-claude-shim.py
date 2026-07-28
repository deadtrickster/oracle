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
import asyncio
import base64
import json
import os
import re
import threading
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# Vision detour. Optional on purpose: the shim's job is translating tool calls, and it must keep
# doing that on a machine where these modules or the vision unit are absent.
try:
    import oracle_vision
    import oracle_vram
    VISION = os.environ.get("ORACLE_SHIM_VISION", "1").lower() in ("1", "true", "yes", "on")
except Exception:                                    # pragma: no cover - degraded but functional
    oracle_vision = oracle_vram = None
    VISION = False

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


# ---- shared context: give the text model the pixels it cannot see -------------------------------
#
# Claude Code sends images as content blocks — pasted screenshots, and whatever `Read` returns for
# a .png. This shim used to replace every one of them with "[image omitted — model is text-only]",
# which is true of the text model and useless to the user: the picture they were asking about was
# dropped on the floor and the model answered anyway, from the filename and the surrounding chat.
#
# It is not text-only, though. The same GPU runs qwen3-vl; it just cannot run both at once. So the
# shim takes the detour: swap to vision, have it READ the image, swap back, and put the reading in
# the prompt. The conversation continues with the image described in context — which is what the
# text model reprocessing its whole transcript amounts to anyway.
#
# Two things keep this affordable and honest:
#   * only images in the LAST user turn can trigger a swap. Older ones come from the cache or stay
#     stubs. Otherwise a long session would re-read its whole history on every turn.
#   * the reading is content-addressed and cached (oracle_vision), because Claude Code re-sends the
#     entire transcript every turn — an uncached read would swap the GPU twice per turn, forever,
#     for an image that has not changed.
IMG_STUB = "[image omitted — model is text-only]"
IMG_UNREAD = ("[image not read — the vision model was not run for this one (only the most recent "
              "message's images are read). Ask again referring to it if you need it.]")


def _blocks(msg) -> list:
    c = msg.get("content")
    return c if isinstance(c, list) else []


def _image_slots(body: dict):
    """Yield (msg_index, container_list, position, block, tool_use_id) for every image block,
    including images nested inside a tool_result (which is how `Read` hands a .png back). The
    tool_use_id travels with the slot so naming it later is a dict lookup, not a scan comparing
    multi-megabyte base64 blocks for equality."""
    for mi, m in enumerate(body.get("messages") or []):
        for i, b in enumerate(_blocks(m)):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "image":
                yield mi, _blocks(m), i, b, None
            elif b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                for j, ib in enumerate(b["content"]):
                    if isinstance(ib, dict) and ib.get("type") == "image":
                        yield mi, b["content"], j, ib, b.get("tool_use_id")


def _image_bytes(block: dict):
    """(data_url, sha) for a base64 image block, or (None, None). URL sources are skipped: this box
    is offline by design, and silently fetching one would be both a network call and a lie."""
    src = block.get("source") or {}
    if src.get("type") != "base64" or not src.get("data"):
        return None, None
    mime = src.get("media_type") or "image/png"
    try:
        raw = base64.b64decode(src["data"], validate=False)
    except Exception:
        return None, None
    if not raw:
        return None, None
    return f"data:{mime};base64,{src['data']}", oracle_vision.sha(raw)


def _tool_labels(body: dict) -> dict:
    """tool_use_id -> file path, so an image that came back from `Read` can be named in context.
    A description headed "screenshot.png" is worth more to the next turn than an unlabelled one."""
    out = {}
    for m in body.get("messages") or []:
        for b in _blocks(m):
            if isinstance(b, dict) and b.get("type") == "tool_use":
                inp = b.get("input") or {}
                p = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
                if p and b.get("id"):
                    out[b["id"]] = str(p)
    return out


def last_user_text(body: dict) -> str:
    msgs = body.get("messages") or []
    for m in reversed(msgs):
        if m.get("role") == "user":
            t = _text_of(m.get("content")).strip()
            return t
    return ""


def plan_vision(body: dict) -> list:
    """Rewrite image blocks in place: cached readings become text, and everything in the last user
    turn that has no reading yet is returned as work to do. Mutates `body`."""
    if not VISION or oracle_vision is None:
        return []
    msgs = body.get("messages") or []
    last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1)
    labels = _tool_labels(body)
    pending = []
    for mi, container, pos, b, tuid in list(_image_slots(body)):
        data_url, key = _image_bytes(b)
        if not key:
            container[pos] = {"type": "text", "text": IMG_STUB}
            continue
        label = labels.get(tuid, "") if tuid else ""    # name it after the file `Read` opened
        hit = oracle_vision.cached(key)
        if hit:
            container[pos] = {"type": "text", "text": oracle_vision.block(hit, label)}
        elif mi == last_user:
            pending.append({"container": container, "pos": pos, "key": key,
                            "data_url": data_url, "label": label})
        else:
            container[pos] = {"type": "text", "text": IMG_UNREAD}
    return pending


def _describe_sync(job: dict, question: str) -> str:
    text = oracle_vision.describe(job["data_url"], question=question, label=job["label"])
    oracle_vision.remember(job["key"], text, question=question, label=job["label"])
    return text


async def _athread(make_gen):
    """Run a BLOCKING generator on a worker thread and yield its items into the event loop.

    oracle_vram.ensure() is deliberately synchronous — it is shared with the stdlib-only capture
    receiver, which has no event loop. Rather than write a second async copy of swap logic (two
    implementations of "who owns the GPU" is how they drift), bridge it here."""
    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def run():
        try:
            for item in make_gen():
                loop.call_soon_threadsafe(q.put_nowait, ("item", item))
        except BaseException as e:                       # noqa: BLE001 - forwarded to the caller
            loop.call_soon_threadsafe(q.put_nowait, ("error", e))
            return
        loop.call_soon_threadsafe(q.put_nowait, ("done", None))

    threading.Thread(target=run, daemon=True).start()
    while True:
        kind, v = await q.get()
        if kind == "item":
            yield v
        elif kind == "error":
            raise v
        else:
            return


async def vision_detour(pending: list, question: str):
    """Swap to vision, read every pending image, swap back. Yields progress lines.

    The swap back is in a `finally`: leaving the box with vision resident would silently break
    every subsequent chat turn, and a failure in the middle of reading three images is exactly
    when that is most likely to happen."""
    n = len(pending)
    yield f"{n} image{'s' if n > 1 else ''} in this message — reading with qwen3-vl."
    try:
        async for note in _athread(lambda: oracle_vram.ensure("vl")):
            yield note
        loop = asyncio.get_running_loop()
        for i, job in enumerate(pending, 1):
            name = job["label"] or f"image {i}"
            yield f"reading {name}…"
            try:
                text = await loop.run_in_executor(None, _describe_sync, job, question)
            except Exception as e:                        # one bad image must not sink the turn
                job["container"][job["pos"]] = {
                    "type": "text", "text": f"[image could not be read by the vision model: {e}]"}
                yield f"failed to read {name}: {e}"
                continue
            job["container"][job["pos"]] = {
                "type": "text", "text": oracle_vision.block(text, job["label"])}
    finally:
        async for note in _athread(lambda: oracle_vram.ensure("text")):
            yield note


async def ensure_text_backend():
    """Bring the text model back if VISION took the card — e.g. the browser extension ran a vision
    request in another window. Gated on the vision server actually being up, so a plain-Ollama
    backend (no /health, reads as "down") never triggers a pointless swap."""
    if not VISION or oracle_vram is None:
        return
    if oracle_vram.text_available() or not oracle_vram.vl_available():
        return
    async for note in _athread(lambda: oracle_vram.ensure("text")):
        yield note


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
                # plan_vision() normally replaced this with the vision model's reading already;
                # reaching here means the detour is off or the source was not inline base64.
                text_parts.append(IMG_STUB)
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


async def stream_translate(body: dict, model: str, pending: list | None = None):
    """Call Ollama's OpenAI streaming endpoint and yield Anthropic SSE events, holding
    back ambiguous text so a leaked `<function=...>` can be salvaged into a tool_use.

    Takes the Anthropic `body` rather than a translated request because the vision prelude REWRITES
    that body (images become the vision model's reading of them) and translation has to happen
    after, not before."""
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

    # ---- prelude: put the right model on the card, and turn any images into text, BEFORE calling
    # the backend. Progress is emitted as VISIBLE assistant text. There is no status channel in the
    # Anthropic stream, and a swap can run for minutes: silence for that long is indistinguishable
    # from a hang — which is exactly how the first version of this was reported.
    emitted = False

    async def notes(agen):
        nonlocal emitted
        async for note in agen:
            emitted = True
            for s in emit_text(f"⟪oracle⟫ {note}\n"):
                yield s

    try:
        async for s in notes(vision_detour(pending, last_user_text(body)) if pending
                             else ensure_text_backend()):
            yield s
    except Exception as e:                              # noqa: BLE001 - reported, not swallowed
        for s in fail_events(None, f"vision detour failed: {e!r}"):
            yield s
        return
    if emitted:
        for s in close_cur():
            yield s

    oai_req = anthropic_to_openai(body)

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
    # Rewrites cached image readings into the body and reports what still needs the vision model.
    pending = plan_vision(body)
    if body.get("stream"):
        return StreamingResponse(stream_translate(body, model, pending),
                                 media_type="text/event-stream")
    # Non-streaming: same detour, minus the progress nobody is watching.
    try:
        if pending:
            async for _ in vision_detour(pending, last_user_text(body)):
                pass
        else:
            async for _ in ensure_text_backend():
                pass
    except Exception as e:                              # noqa: BLE001 - surfaced to the caller
        return JSONResponse(error_message(model, f"vision detour failed: {e!r}"))
    oai_req = anthropic_to_openai(body)
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
