#!/usr/bin/env python3
"""Version-controlled RAGFlow prompts — dump/apply the `oracle` chat + agent instructions.

RAGFlow's DB is disposable (rebuildable from the corpus); its prompts are not stored anywhere
else, so this captures them as plain-text files under ragflow-agents/ and restores them after a
rebuild. Idempotent.

  ./sync.py --dump    # RAGFlow -> ragflow-agents/*.txt  (snapshot current prompts)
  ./sync.py --apply   # ragflow-agents/*.txt -> RAGFlow  (restore after a rebuild)

Chat prompt = prompt_config.system; agent instruction = the `Agent:<Name>` component's
obj.params.sys_prompt (see the ragflow-agents-and-api memory for the full API shape).
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

BASE = os.environ.get("ORACLE_RAGFLOW_URL", "http://localhost:9380")
KEY = os.environ.get("ORACLE_RAGFLOW_KEY", "ragflow-smywlJs3drgGxfKztifTmD3iNJ2lP6Uvq2-suiLQTGM")
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
HERE = Path(__file__).parent

# what to sync: a chat assistant, plus canvas agents (matched by title)
CHATS = ["oracle"]
AGENTS = ["oracle-omni", "code-graph", "oracle-grounded", "ingestor"]


def _get(path):
    return json.load(urllib.request.urlopen(urllib.request.Request(BASE + path, headers=HDR), timeout=30))


def _put(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers=HDR, method="PUT")
    return json.load(urllib.request.urlopen(req, timeout=30))


def _chats():
    return {c["name"]: c for c in _get("/api/v1/chats?page_size=50")["data"]["chats"]}


def _agent_ids():
    return {a["title"]: a["id"] for a in _get("/api/v1/agents?page_size=50")["data"]["canvas"]}


def _agent_component(dsl):
    """Return (component_id, params) of the Agent node holding sys_prompt."""
    for cid, c in dsl["components"].items():
        params = c.get("obj", {}).get("params", {})
        if params.get("sys_prompt") is not None and cid.startswith("Agent:"):
            return cid, params
    raise SystemExit("no Agent component with sys_prompt found")


def dump():
    chats = _chats()
    for name in CHATS:
        sysp = chats[name]["prompt_config"]["system"]
        (HERE / f"chat.{name}.txt").write_text(sysp)
        print(f"  dumped chat {name} ({len(sysp)} chars)")
    ids = _agent_ids()
    for title in AGENTS:
        d = _get(f"/api/v1/agents/{ids[title]}")["data"]
        _cid, params = _agent_component(d["dsl"])
        (HERE / f"agent.{title}.txt").write_text(params["sys_prompt"])
        print(f"  dumped agent {title} ({len(params['sys_prompt'])} chars)")


def apply():
    chats = _chats()
    for name in CHATS:
        f = HERE / f"chat.{name}.txt"
        if not f.exists():
            continue
        c = chats[name]
        pc = dict(c["prompt_config"])
        pc["system"] = f.read_text()
        r = _put(f"/api/v1/chats/{c['id']}", {"prompt_config": pc})
        print(f"  applied chat {name}: code={r.get('code')}")
    ids = _agent_ids()
    for title in AGENTS:
        f = HERE / f"agent.{title}.txt"
        if not f.exists():
            continue
        d = _get(f"/api/v1/agents/{ids[title]}")["data"]
        cid, _ = _agent_component(d["dsl"])
        d["dsl"]["components"][cid]["obj"]["params"]["sys_prompt"] = f.read_text()
        r = _put(f"/api/v1/agents/{ids[title]}", {"title": title, "dsl": d["dsl"]})
        print(f"  applied agent {title}: code={r.get('code')}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--dump":
        dump()
    elif mode == "--apply":
        apply()
    else:
        print(__doc__)
        raise SystemExit(2)
