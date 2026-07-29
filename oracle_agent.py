"""Let an EXTERNAL agent be the model for a session — and record what it did.

## Why this exists

Two panel bugs in a row were diagnosed from transcripts and both diagnoses were wrong. "The click
created a draft each time" — it did not; there was one draft and every click did nothing. Reading a
transcript tells you what the model *said* and what the tools *returned*; it does not tell you what
the page did, which is the thing that actually matters. The person who can see the page has been
relaying it, one screenshot at a time, and I have been guessing in between.

So: borrow the browser. The extension already owns a real logged-in tab and can click, type, read
and screenshot it. This module lets a stronger model sit where qwen sits — receive the same
messages, with the same tools, on the same page — and drive it directly.

## What it is NOT

Not a way to answer the user's questions with a different model. The session has to be put into
agent mode deliberately, and the panel says so while it is on. It is an instrument for working out
what a flow actually requires, on a real page, with hands.

## The traces are the point

Every exchange is appended to a JSONL trace: the messages in, the reply out, the tool calls and
their real results. That is a record of a flow being solved correctly on the real application —
which is the material for teaching the local model, whether as few-shot exemplars in a site pack, as
an eval fixture, or as fine-tuning data. A trace of a *successful* flow is worth more than any
amount of prompt advice about how the flow ought to go, because it is what happened rather than what
someone imagined would happen.

## Shape

The receiver, in agent mode, publishes the pending request and blocks. An external process polls,
decides, and posts the assistant turn back. Everything downstream — the browser hand-off, the
confirm gate, the transcript — is unchanged, because this replaces exactly one thing: who produces
the assistant message.

File-backed with flock, like `oracle_broker`, because the poller is a different process and may well
be on the other side of a `curl`.
"""
import fcntl
import json
import os
import time
from pathlib import Path

def _state() -> Path:
    """Resolved per call, not at import.

    A module-level constant reads the environment once, at import time — which meant a test that
    imports the receiver was permanently bound to the LIVE state file. `test-tools.py` happens to
    use `stage.cloud.stroppy.io` as its host, the same host that was in agent mode on this machine,
    so the suite silently blocked for fifteen minutes waiting for a human to answer a question no
    human had been asked. Resolving here lets a test point somewhere private after import, and costs
    one environment lookup on a path that already touches the filesystem."""
    return Path(os.environ.get(
        "ORACLE_AGENT_STATE",
        str(Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "oracle-agent.json")))
TRACES = Path(os.environ.get(
    "ORACLE_AGENT_TRACES",
    str(Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share"))
        / "oracle" / "traces")))

# How long a turn waits for the external agent before giving up and saying so. Generous: a human-
# paced agent reading a page and deciding is not fast, and the alternative — timing out mid-flow —
# loses the context that made the trace worth recording.
WAIT_SECONDS = float(os.environ.get("ORACLE_AGENT_WAIT", "900"))
POLL_SECONDS = 0.4


def _load() -> dict:
    try:
        return json.loads(_state().read_text())
    except Exception:
        return {"mode": {}, "pending": {}, "replies": {}}


def _save(d: dict) -> None:
    _state().parent.mkdir(parents=True, exist_ok=True)
    tmp = _state().with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    os.replace(tmp, _state())


def _with_lock(fn):
    """Serialise read-modify-write across processes."""
    _state().parent.mkdir(parents=True, exist_ok=True)
    lock = _state().with_suffix(".lock")
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            d = _load()
            out = fn(d)
            _save(d)
            return out
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def key(host: str, session: str) -> str:
    return f"{host}/{session}"


def enabled(host: str, session: str) -> bool:
    return bool(_load().get("mode", {}).get(key(host, session)))


def set_mode(host: str, session: str, on: bool) -> bool:
    def go(d):
        d.setdefault("mode", {})
        k = key(host, session)
        if on:
            d["mode"][k] = True
        else:
            d["mode"].pop(k, None)
            d.setdefault("pending", {}).pop(k, None)
            d.setdefault("replies", {}).pop(k, None)
        return on
    return _with_lock(go)


def sessions() -> list:
    return sorted(_load().get("mode", {}))


def publish(host: str, session: str, messages: list, tools: list, meta: dict) -> str:
    """Post a request for the external agent. Returns its id."""
    rid = f"{int(time.time() * 1000)}-{os.getpid()}"

    def go(d):
        d.setdefault("pending", {})[key(host, session)] = {
            "id": rid, "host": host, "session": session, "at": time.time(),
            "messages": messages, "tools": tools, "meta": meta,
        }
        d.setdefault("replies", {}).pop(key(host, session), None)
        return rid
    return _with_lock(go)


def poll(host: str = "", session: str = "") -> dict:
    """What the external agent should answer next, or {}."""
    d = _load()
    pend = d.get("pending", {})
    if host:
        return pend.get(key(host, session)) or {}
    # No host given: hand back the oldest waiting request across all agent sessions, so a poller can
    # be started without knowing which page the user is on.
    items = sorted(pend.values(), key=lambda p: p.get("at", 0))
    return items[0] if items else {}


def reply(host: str, session: str, rid: str, text: str, tool_calls: list) -> bool:
    def go(d):
        k = key(host, session)
        pend = d.get("pending", {}).get(k)
        if not pend or (rid and pend.get("id") != rid):
            return False
        d.setdefault("replies", {})[k] = {"id": pend["id"], "text": text or "",
                                          "tool_calls": tool_calls or []}
        d.get("pending", {}).pop(k, None)
        return True
    return _with_lock(go)


# How often to emit a heartbeat while waiting. The panel gives up on a turn after ~7 minutes of
# SILENCE, so a long wait must keep saying something — otherwise the user is told the connection
# died while the receiver is patiently blocking exactly as designed.
HEARTBEAT_SECONDS = 20.0


def await_reply(host: str, session: str, rid: str, timeout: float = WAIT_SECONDS):
    """Block until the external agent answers.

    Yields ("status", {...}) EVENTS, not strings: this is delegated with `yield from` straight into
    the SSE stream, where every item is unpacked as (event, data). Yielding a bare string ended the
    connection mid-turn and the panel reported "the answer was cut off" — which was true, and was
    this function's fault rather than the network's."""
    deadline = time.time() + timeout
    started = time.time()
    last_beat = 0.0
    while time.time() < deadline:
        d = _load()
        got = d.get("replies", {}).get(key(host, session))
        if got and got.get("id") == rid:
            def go(dd):
                dd.get("replies", {}).pop(key(host, session), None)
                return True
            _with_lock(go)
            return got
        now = time.time()
        if now - last_beat >= HEARTBEAT_SECONDS:
            last_beat = now
            waited = int(now - started)
            yield ("status", {"text": f"waiting for the external agent… {waited}s"})
        time.sleep(POLL_SECONDS)
    # Clear the request so a later turn is not answered by a stale reply.
    def drop(dd):
        dd.get("pending", {}).pop(key(host, session), None)
        return True
    _with_lock(drop)
    return None


def trace(host: str, session: str, event: str, payload: dict) -> None:
    """Append to this session's trace. Never raises — a lost trace must not break a turn."""
    try:
        TRACES.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in f"{host}__{session}")
        with open(TRACES / f"{safe}.jsonl", "a") as fh:
            fh.write(json.dumps({"at": time.time(), "event": event, **payload},
                                ensure_ascii=False) + "\n")
    except Exception:
        pass
