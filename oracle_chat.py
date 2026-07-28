"""Per-host conversations: one continued chat per site you are reading.

Scoped to the HOST, not the tab or the page. Reading a benchmark report, then its docs, then a run
page is one line of thought about one system, and it is the host that says so — you should be able
to ask "so what changed since that first run?" three pages later without repeating yourself.

## Append-only, with epochs

The store never rewrites a turn. That is not tidiness, it is the prerequisite for the KV prefix
cache: llama.cpp reuses a slot by longest common prefix, so editing history — summarising old
turns, dropping the middle — invalidates everything from the edit onward and costs a full
re-process of the conversation. So when a conversation outgrows its budget the store does not
compact it; it starts a new EPOCH. The old turns stay on disk, readable, and the new epoch begins a
clean prefix.

That makes the cost model honest: growth inside an epoch is incremental (only the new turns get
processed), and the one expensive moment is a visible, deliberate boundary rather than a mystery
slowdown halfway through a conversation.

`reset()` is the same mechanism the user can reach: "new topic" is a new epoch, not a delete.
Nothing is destroyed by talking to it.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

CHAT_DIR = Path(os.environ.get(
    "ORACLE_CHAT_DIR",
    str(Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "oracle" / "chat")))
# Budget for ONE epoch, in characters of transcript. Crossing it starts a new epoch rather than
# rewriting history (see above).
CHAT_MAX_CHARS = int(os.environ.get("ORACLE_CHAT_MAX_CHARS", "24000"))
CHAT_MAX_TURNS = int(os.environ.get("ORACLE_CHAT_MAX_TURNS", "40"))

_locks: dict = {}
_locks_guard = threading.Lock()


def _lock(host: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(host, threading.Lock())


def _safe(host: str) -> str:
    """A filename that cannot escape CHAT_DIR. Hosts come off the wire; `../../x` is a host as far
    as a JSON payload is concerned."""
    h = re.sub(r"[^a-zA-Z0-9._-]", "_", (host or "").lower()).strip("._-")
    return h[:120] or "unknown"


def _path(host: str) -> Path:
    return CHAT_DIR / f"{_safe(host)}.json"


def _read(host: str) -> dict:
    try:
        d = json.loads(_path(host).read_text())
        if isinstance(d, dict) and isinstance(d.get("turns"), list):
            return d
    except Exception:
        pass
    return {"host": host, "epoch": 1, "turns": []}


def _write(host: str, doc: dict) -> None:
    try:
        CHAT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _path(host).with_suffix(".tmp")
        tmp.write_text(json.dumps(doc))
        os.replace(tmp, _path(host))     # atomic: a half-written transcript is worse than none
    except Exception:
        pass


def history(host: str) -> list:
    """Turns of the CURRENT epoch, oldest first — what goes into the prompt."""
    d = _read(host)
    ep = d.get("epoch", 1)
    return [t for t in d["turns"] if t.get("epoch", 1) == ep]


def all_turns(host: str) -> list:
    return _read(host)["turns"]


def epoch(host: str) -> int:
    return _read(host).get("epoch", 1)


def append(host: str, role: str, content: str, tool_calls: list | None = None,
           tool_call_id: str = "", name: str = "") -> dict:
    """Add a turn. Returns {"epoch": n, "rolled": bool} — `rolled` means this turn began a new
    epoch because the previous one was full.

    Tool calls and their results are stored as turns like any other, because they ARE the
    conversation once the chat can act: "I clicked LOGS, here is what it said" is the reasoning, and
    a transcript that dropped it would leave the next turn unable to explain how it knows anything.
    """
    if not host:
        return {"epoch": 1, "rolled": False}
    with _lock(host):
        d = _read(host)
        ep = d.get("epoch", 1)
        cur = [t for t in d["turns"] if t.get("epoch", 1) == ep]
        rolled = False
        # Roll BEFORE appending a user turn, never mid-exchange: an epoch that begins with an
        # assistant reply to a question it cannot see reads as a non-sequitur.
        if role == "user" and cur and (
                sum(len(t.get("content", "")) for t in cur) + len(content) > CHAT_MAX_CHARS
                or len(cur) >= CHAT_MAX_TURNS):
            ep += 1
            d["epoch"] = ep
            rolled = True
        turn = {"role": role, "content": content, "at": time.time(), "epoch": ep}
        if tool_calls:
            turn["tool_calls"] = tool_calls
        if tool_call_id:
            turn["tool_call_id"] = tool_call_id
        if name:
            turn["name"] = name
        d["turns"].append(turn)
        _write(host, d)
        return {"epoch": ep, "rolled": rolled}


def to_messages(turns: list) -> list:
    """Transcript turns -> OpenAI chat messages, tool calls included and ALWAYS well formed.

    Every assistant tool_call must be followed by a result: the chat template requires it, and a
    call left dangling produces a malformed prompt rather than a clear error. They do get left
    dangling — the loop runs in an MV3 service worker, which Chrome may terminate mid-flight, and
    then nobody ever posts the result. The old transcript is not wrong about what happened; it is
    just incomplete, and completing it is the harness's job, not the model's problem.

    So a missing result is synthesised, and says what it is. The model can then reason about the
    gap ("that step did not finish, let me try again") instead of being handed a prompt the
    template cannot render."""
    out, i = [], 0
    while i < len(turns):
        t = turns[i]
        m = {"role": t["role"], "content": t.get("content", "")}
        if t.get("tool_calls"):
            m["tool_calls"] = t["tool_calls"]
        if t.get("tool_call_id"):
            m["tool_call_id"] = t["tool_call_id"]
        if t.get("name"):
            m["name"] = t["name"]
        out.append(m)
        if t["role"] == "assistant" and t.get("tool_calls"):
            answered = set()
            j = i + 1
            while j < len(turns) and turns[j]["role"] == "tool":
                answered.add(turns[j].get("tool_call_id"))
                j += 1
            for c in t["tool_calls"]:
                if c.get("id") not in answered:
                    out.append({"role": "tool", "tool_call_id": c.get("id"),
                                "name": (c.get("function") or {}).get("name", ""),
                                "content": "error: this step never completed (the browser was "
                                           "closed or the extension was reloaded). Nothing was "
                                           "done; call it again if you still need it."})
        i += 1
    return out


def pending_tools(host: str) -> list:
    """Tool calls the model asked for and nobody has answered yet — the last assistant turn's calls
    with no matching tool result after them. Survives a browser restart mid-loop, which a purely
    in-memory pending list would not."""
    turns = history(host)
    for i in range(len(turns) - 1, -1, -1):
        t = turns[i]
        if t.get("tool_calls"):
            answered = {r.get("tool_call_id") for r in turns[i + 1:] if r["role"] == "tool"}
            return [c for c in t["tool_calls"] if c.get("id") not in answered]
        if t["role"] == "user":
            return []
    return []


def reset(host: str) -> int:
    """Start a new epoch — "new topic". Keeps everything; only the prompt window moves."""
    with _lock(host):
        d = _read(host)
        if not [t for t in d["turns"] if t.get("epoch", 1) == d.get("epoch", 1)]:
            return d.get("epoch", 1)          # already empty, nothing to roll
        d["epoch"] = d.get("epoch", 1) + 1
        _write(host, d)
        return d["epoch"]


_ALLOW = CHAT_DIR / "allow-actions.json"


def actions_allowed(host: str) -> bool:
    """May the chat ACT on this host (click, type)? Off until the user says otherwise, per host.

    Per host rather than global because trust is not a property of the assistant, it is a property
    of what a mistake would cost — clicking around a benchmark UI you own is not the same as
    clicking around a bank."""
    try:
        return bool(json.loads(_ALLOW.read_text()).get(host))
    except Exception:
        return False


def set_actions(host: str, allowed: bool) -> bool:
    try:
        d = json.loads(_ALLOW.read_text())
    except Exception:
        d = {}
    d[host] = bool(allowed)
    try:
        CHAT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _ALLOW.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, _ALLOW)
    except Exception:
        pass
    return bool(allowed)


def hosts() -> list:
    """Every host with a stored conversation, most recently used first."""
    out = []
    try:
        for f in CHAT_DIR.glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            turns = d.get("turns") or []
            if turns:
                out.append({"host": d.get("host") or f.stem, "turns": len(turns),
                            "epoch": d.get("epoch", 1), "at": turns[-1].get("at", 0)})
    except Exception:
        pass
    return sorted(out, key=lambda x: x["at"], reverse=True)
