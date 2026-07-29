#!/usr/bin/env python3
"""Drive an Oracle chat session by hand — the CLI side of agent mode.

Built because two panel bugs were diagnosed from transcripts and both diagnoses were wrong. A
transcript shows what the model said and what the tools returned; it does not show what the page
did. This borrows the extension's real logged-in tab so whoever is debugging can act on the actual
application instead of inferring from a relayed screenshot.

    ./agent-drive.py on   --host stage.cloud.stroppy.io [--session main]
    ./agent-drive.py poll [--host ...]            what the session is waiting for
    ./agent-drive.py say  "text" [--call NAME '{"json":"args"}'] ...
    ./agent-drive.py off  --host ...

`say` posts one assistant turn: prose, and any number of tool calls. The extension executes browser
tools exactly as it does for the local model, so the confirm gate, the transcript and the panel all
behave normally — the only thing replaced is who writes the assistant message.

Every exchange lands in a JSONL trace (see oracle_agent.TRACES): messages in, reply out, and the
REAL tool results. That is the material for teaching the local model — a record of a flow solved
correctly on the real application, which beats any amount of prose about how the flow ought to go.
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8788"


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or "{}")


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read() or "{}")


def show_pending(p: dict) -> None:
    if not p or not p.get("id"):
        print("nothing waiting" + (f" (agent sessions: {p.get('sessions')})" if p else ""))
        return
    print(f"request {p['id']}  {p['host']}/{p['session']}  "
          f"(model call #{p.get('meta', {}).get('call_no')})")
    # `tools` is the full OpenAI schema list, not names — print the names, and for site_call the
    # helper functions too, since that enum is the whole point of the tool.
    names = []
    for t in p.get("tools", []):
        fn = t.get("function", t) if isinstance(t, dict) else {}
        name = fn.get("name", "?")
        enum = ((fn.get("parameters") or {}).get("properties") or {}).get("fn", {}).get("enum")
        names.append(f"{name}({'|'.join(enum)})" if enum else name)
    print(f"tools: {', '.join(names)}")
    print("-" * 78)
    for m in p.get("messages", []):
        role = m.get("role")
        body = (m.get("content") or "").strip()
        if role == "system":
            print(f"[system, {len(body)} chars — the cached prefix; ask for it with --full]")
            continue
        if m.get("tool_calls"):
            for c in m["tool_calls"]:
                print(f"[assistant CALL] {c['function']['name']} {c['function'].get('arguments','')}")
            if body:
                print(f"[assistant] {body}")
            continue
        label = {"user": "user", "tool": f"tool:{m.get('name', '?')}"}.get(role, role)
        print(f"[{label}] {body[:1800]}")
    print("-" * 78)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["on", "off", "poll", "say"])
    ap.add_argument("text", nargs="?", default="")
    ap.add_argument("--host", default="")
    ap.add_argument("--session", default="main")
    ap.add_argument("--call", nargs=2, action="append", metavar=("NAME", "ARGS_JSON"),
                    help="a tool call to make; repeatable")
    ap.add_argument("--full", action="store_true", help="print the system prompt too")
    a = ap.parse_args()

    try:
        if a.action in ("on", "off"):
            if not a.host:
                print("--host is required", file=sys.stderr)
                return 2
            r = post("/agent/mode", {"host": a.host, "session": a.session,
                                     "on": a.action == "on"})
            print(f"agent mode {'ON' if r.get('agent') else 'OFF'} for {a.host}/{a.session}")
            print(f"sessions in agent mode: {r.get('sessions')}")
            return 0

        if a.action == "poll":
            q = urllib.parse.urlencode({"host": a.host, "session": a.session}) if a.host else ""
            p = get("/agent/poll" + (f"?{q}" if q else ""))
            if a.full and p.get("messages"):
                print(p["messages"][0].get("content", ""))
                print("=" * 78)
            show_pending(p)
            return 0

        # say
        q = urllib.parse.urlencode({"host": a.host, "session": a.session}) if a.host else ""
        p = get("/agent/poll" + (f"?{q}" if q else ""))
        if not p.get("id"):
            print("nothing is waiting for a reply", file=sys.stderr)
            return 1
        calls = []
        for i, (name, args) in enumerate(a.call or []):
            try:
                json.loads(args)
            except ValueError as e:
                print(f"--call {name}: arguments are not valid JSON ({e})", file=sys.stderr)
                return 2
            calls.append({"id": f"ext-{p['id']}-{i}", "type": "function",
                          "function": {"name": name, "arguments": args}})
        r = post("/agent/reply", {"host": p["host"], "session": p["session"], "id": p["id"],
                                  "text": a.text, "tool_calls": calls})
        if not r.get("accepted"):
            print("rejected — the request expired or another reply won the race", file=sys.stderr)
            return 1
        print(f"sent: {len(calls)} tool call(s)" + (f" + {len(a.text)} chars of prose" if a.text else ""))
        return 0
    except urllib.error.URLError as e:
        print(f"receiver unreachable at {BASE} ({e})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
