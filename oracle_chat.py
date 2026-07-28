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
# Raised from 24k after a real conversation rolled after THREE questions. The turns were not the
# problem: three read_page calls on Gmail, each capped at 8k, were 24k on their own — the whole
# budget — before anyone had said anything. A page dump is transient evidence and it competes with
# the conversation for the same allowance, so on a heavy page the allowance is spent by the tools.
#
# 60k chars is ~15k tokens. Against a 128k-token slot carrying a ~6k-token prefix that is still
# conservative, and growth inside an epoch is incremental anyway — the prefix cache means only the
# new turns get processed, so a longer epoch costs prompt processing once per turn, not per epoch.
# ~50k tokens of transcript, against a 131,072-token slot carrying a ~6k-token prefix. That leaves
# ample room for the answer and still bounds a runaway conversation. 24k, then 60k, were both chosen
# without reference to the actual slot size — the first rolled a real conversation after three
# questions, and neither used more than a sixth of what was available.
CHAT_MAX_CHARS = int(os.environ.get("ORACLE_CHAT_MAX_CHARS", "200000"))
CHAT_MAX_TURNS = int(os.environ.get("ORACLE_CHAT_MAX_TURNS", "40"))

_locks: dict = {}
_locks_guard = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _safe(host: str) -> str:
    """A filename that cannot escape CHAT_DIR. Hosts come off the wire; `../../x` is a host as far
    as a JSON payload is concerned."""
    h = re.sub(r"[^a-zA-Z0-9._-]", "_", (host or "").lower()).strip("._-")
    return h[:120] or "unknown"


# A host can hold more than one conversation. `main` is the chat panel; `quick` is where the
# one-shot surfaces (explain, fact-check, a region sent to vision) land, so those stop being
# throwaway cards and become a transcript you can pick up and continue.
#
# `main` deliberately keeps the ORIGINAL filename, so every conversation recorded before sessions
# existed is still exactly where it was. A migration that rewrites files to prove a point is a
# migration that can lose them.
MAIN = "main"


def _path(host: str, session: str = MAIN) -> Path:
    if session == MAIN:
        return CHAT_DIR / f"{_safe(host)}.json"
    return CHAT_DIR / f"{_safe(host)}__{_safe(session)}.json"


def _read(host: str, session: str = MAIN) -> dict:
    try:
        d = json.loads(_path(host, session).read_text())
        if isinstance(d, dict) and isinstance(d.get("turns"), list):
            return d
    except Exception:
        pass
    return {"host": host, "session": session, "epoch": 1, "turns": []}


def _write(host: str, doc: dict, session: str = MAIN) -> None:
    try:
        CHAT_DIR.mkdir(parents=True, exist_ok=True)
        p = _path(host, session)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc))
        os.replace(tmp, p)               # atomic: a half-written transcript is worse than none
    except Exception:
        pass


def history(host: str, session: str = MAIN) -> list:
    """Turns of the CURRENT epoch, oldest first — what goes into the prompt."""
    d = _read(host, session)
    ep = d.get("epoch", 1)
    return [t for t in d["turns"] if t.get("epoch", 1) == ep]


def all_turns(host: str, session: str = MAIN) -> list:
    return _read(host, session)["turns"]


def epoch(host: str, session: str = MAIN) -> int:
    return _read(host, session).get("epoch", 1)


def append(host: str, role: str, content: str, tool_calls: list | None = None,
           tool_call_id: str = "", name: str = "", session: str = MAIN,
           image: str = "", display: str = "") -> dict:
    """Add a turn. Returns {"epoch": n, "rolled": bool} — `rolled` means this turn began a new
    epoch because the previous one was full.

    Tool calls and their results are stored as turns like any other, because they ARE the
    conversation once the chat can act: "I clicked LOGS, here is what it said" is the reasoning, and
    a transcript that dropped it would leave the next turn unable to explain how it knows anything.
    """
    if not host:
        return {"epoch": 1, "rolled": False}
    with _lock(f"{host}/{session}"):
        d = _read(host, session)
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
        # The picture the turn was about, as a data: URL, kept for the UI only. It is deliberately
        # NOT sent to the model — the model gets the vision model's READING (§to_messages), because
        # the text model cannot see and re-sending pixels it cannot use would only cost context.
        # But a transcript that mentions "the region you selected" and cannot show it is a
        # transcript you cannot audit, so the UI keeps the image.
        if image:
            turn["image"] = image[:2_000_000]
        # What a PERSON should see, when that differs from what the model is given. A turn carries
        # both because they are genuinely different documents: the model needs the framing and the
        # vision model's full reading; the user needs to recognise the thing they just asked for.
        # Showing them the composed prompt — instructions, provenance caveats, a paragraph of
        # transcribed pixels — makes their own message unreadable and buries the picture it is
        # about. `content` stays authoritative for the model; `display` is only ever cosmetic.
        if display and display != content:
            turn["display"] = display[:4000]
        d["turns"].append(turn)
        _write(host, d, session)
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


def pending_tools(host: str, session: str = MAIN) -> list:
    """Tool calls the model asked for and nobody has answered yet — the last assistant turn's calls
    with no matching tool result after them. Survives a browser restart mid-loop, which a purely
    in-memory pending list would not."""
    turns = history(host, session)
    for i in range(len(turns) - 1, -1, -1):
        t = turns[i]
        if t.get("tool_calls"):
            answered = {r.get("tool_call_id") for r in turns[i + 1:] if r["role"] == "tool"}
            return [c for c in t["tool_calls"] if c.get("id") not in answered]
        if t["role"] == "user":
            return []
    return []


def reset(host: str, session: str = MAIN) -> int:
    """Start a new epoch — "new topic". Keeps everything; only the prompt window moves."""
    with _lock(f"{host}/{session}"):
        d = _read(host, session)
        if not [t for t in d["turns"] if t.get("epoch", 1) == d.get("epoch", 1)]:
            return d.get("epoch", 1)          # already empty, nothing to roll
        d["epoch"] = d.get("epoch", 1) + 1
        _write(host, d, session)
        return d["epoch"]


_ALLOW = CHAT_DIR / "allow-actions.json"


OFF, CONFIRM, ALLOW = "off", "confirm", "allow"


def mode(host: str) -> str:
    """How far the chat may go on this host: "off", "confirm" or "allow".

    Per host rather than global because trust is not a property of the assistant, it is a property
    of what a mistake would cost — clicking around a benchmark UI you own is not the same as
    clicking around a bank.

    The middle setting is the one that matters, and it exists because the binary was wrong in both
    directions. "off" is safe and useless: the model can see a button, know it is the answer, and be
    unable to press it. "allow" is useful and unbounded: in a mail client, Archive, Delete and Send
    are all one click apart, and the model authenticated none of it. "confirm" splits the decision
    along its natural seam — the model chooses WHICH element (the part it is good at, having just
    read the page) and the human decides WHETHER (the part that carries the consequence). It also
    makes the act legible before it happens rather than after, which is the difference between
    supervising and auditing.

    Stored values stay backwards compatible: an existing `true` reads as "allow".
    """
    try:
        v = json.loads(_ALLOW.read_text()).get(host)
    except Exception:
        return OFF
    if v is True:
        return ALLOW
    if isinstance(v, str) and v in (CONFIRM, ALLOW):
        return v
    return OFF


def actions_allowed(host: str) -> bool:
    """Are the acting tools OFFERED at all? True for both "confirm" and "allow" — under "confirm"
    the model really can act, it just cannot do so unilaterally, so hiding the tools would be a
    lie about what is possible and would send it back to narrating clicks it never makes."""
    return mode(host) != OFF


def set_actions(host: str, allowed) -> bool:
    """`allowed` may be a bool (legacy) or one of "off"/"confirm"/"allow"."""
    try:
        d = json.loads(_ALLOW.read_text())
    except Exception:
        d = {}
    if isinstance(allowed, str) and allowed in (OFF, CONFIRM, ALLOW):
        d[host] = allowed
    else:
        d[host] = ALLOW if allowed else OFF
    try:
        CHAT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _ALLOW.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, _ALLOW)
    except Exception:
        pass
    return mode(host)


def delete(host: str, session: str = MAIN) -> bool:
    """Delete a conversation outright. `reset` starts a new epoch and keeps everything; this is the
    other thing, and the difference should stay visible in the UI.

    The per-host action permission is deliberately NOT cleared: whether you trust a site with clicks
    is a fact about the site, not about a conversation you happened to finish."""
    if not host:
        return False
    with _lock(f"{host}/{session}"):
        try:
            _path(host, session).unlink()
            return True
        except OSError:
            return False


def _scan() -> list:
    """Every stored conversation on disk as (host, session, doc). One place that knows the file
    naming, so `main`'s legacy filename is decoded once rather than in three callers."""
    out = []
    try:
        for f in sorted(CHAT_DIR.glob("*.json")):
            if f.name in ("allow-actions.json", "prefixes.json", "index.json"):
                continue
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            if not isinstance(d, dict) or not isinstance(d.get("turns"), list):
                continue
            stem = f.stem
            session = d.get("session") or (stem.split("__", 1)[1] if "__" in stem else MAIN)
            out.append((d.get("host") or stem.split("__", 1)[0], session, d))
    except Exception:
        pass
    return out


def sessions(host: str = "") -> list:
    """Stored conversations, most recently used first. With `host`, only that host's — which is what
    the panel wants; without it, all of them, which is what a settings page wants."""
    out = []
    for h, s, d in _scan():
        turns = d.get("turns") or []
        if not turns or (host and h != host):
            continue
        out.append({"host": h, "session": s, "turns": len(turns),
                    "epoch": d.get("epoch", 1), "at": turns[-1].get("at", 0),
                    "preview": next((t.get("content", "")[:80] for t in turns
                                     if t["role"] == "user"), "")})
    return sorted(out, key=lambda x: x["at"], reverse=True)


def hosts() -> list:
    """Backwards-compatible view: one entry per stored conversation, newest first."""
    return sessions()
