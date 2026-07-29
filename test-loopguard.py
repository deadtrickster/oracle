#!/usr/bin/env python3
"""The two fixes for the Gmail loop: telling the model it cannot act, and stopping repeats.

Reproduced from the real transcript (mail.google.com__quick.json, 2026-07-28): asked to open an
email on a host where acting is disabled, the model spent six steps announcing clicks it could not
perform and re-screenshotting the same inbox.
"""
import importlib.util
import sys
from pathlib import Path

# Agent mode must never be read from LIVE runtime state in a test: these hosts are real, and
# one of them being in agent mode blocks the suite waiting for a human who was never asked.
import os as _os
_os.environ.setdefault("ORACLE_AGENT_STATE", f"/tmp/oracle-agent-test-{_os.getpid()}.json")
sys.path.insert(0, str(Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location(
    "recv", Path(__file__).parent / "oracle-capture-receiver.py")
recv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recv)

FAIL = []


def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


def call(name, args, cid="x"):
    return {"id": cid, "function": {"name": name, "arguments": args}}


# --- the acting notice --------------------------------------------------------------
note = recv._acting_note(False)
check("a host without actions is told so", "CANNOT ACT" in note)
check("it names the tools that are off",
      all(w in note for w in ("Click", "typing", "navigating")) or "click" in note.lower())
check("it forbids the exact workaround that was observed",
      "screenshot" in note and "waiting" in note, note[:200])
check("it gives the user a remedy", "extension" in note and "enable" in note)
check("a host WITH actions gets no notice at all", recv._acting_note(True) == "")

# --- the repeat detector ------------------------------------------------------------
LOOK = '{"full_page":false,"question":"Find the DHL email"}'
turns = [
    {"role": "user", "content": "open the newest unread email"},
    {"role": "assistant", "tool_calls": [call("read_page", "{}")]},
    {"role": "tool", "content": "…inbox text…"},
    {"role": "assistant", "tool_calls": [call("look_at_page", LOOK)]},
    {"role": "tool", "content": "…screenshot…"},
    {"role": "assistant", "tool_calls": [call("look_at_page", LOOK)]},
    {"role": "tool", "content": "…the same screenshot…"},
]
sigs = recv._call_signatures(turns)
check("counts repeats within the exchange", sigs.count(("look_at_page", LOOK)) == 2, str(sigs))
check("a first call is not a repeat", sigs.count(("read_page", "{}")) == 1)

# The guard fires at >=2 priors, i.e. on the THIRD identical call — the point where the transcript
# shows it had stopped learning anything, not the first honest retry.
check("a second identical call is still allowed", sigs.count(("read_page", "{}")) < 2)
check("a third identical call is intercepted", sigs.count(("look_at_page", LOOK)) >= 2)

# A repeat from BEFORE the user spoke again is a different question, not a loop.
across = [
    {"role": "assistant", "tool_calls": [call("look_at_page", LOOK)]},
    {"role": "tool", "content": "…"},
    {"role": "assistant", "tool_calls": [call("look_at_page", LOOK)]},
    {"role": "tool", "content": "…"},
    {"role": "user", "content": "now what about this page?"},
    {"role": "assistant", "tool_calls": [call("look_at_page", LOOK)]},
    {"role": "tool", "content": "…"},
]
check("earlier topics do not count toward the limit",
      recv._call_signatures(across).count(("look_at_page", LOOK)) == 1,
      str(recv._call_signatures(across)))

# Different arguments are progress, however similar they look.
varied = [
    {"role": "user", "content": "look at the tabs"},
    {"role": "assistant", "tool_calls": [call("click", '{"text":"WORKLOAD"}')]},
    {"role": "tool", "content": "…"},
    {"role": "assistant", "tool_calls": [call("click", '{"text":"SYSTEM"}')]},
    {"role": "tool", "content": "…"},
    {"role": "assistant", "tool_calls": [call("click", '{"text":"ORIOLEDB"}')]},
    {"role": "tool", "content": "…"},
]
s = recv._call_signatures(varied)
check("clicking through different tabs is never blocked",
      all(s.count(x) < 2 for x in s), str(s))

# --- the regression that broke a real turn ------------------------------------------
# From stage.cloud.stroppy.io, 2026-07-28: the model opened the new-run wizard, went to the presets
# library to look something up, then navigated BACK to the wizard — obviously correct. The guard
# refused it as a repeat and told it "the result did not change", when it was on a different page
# by then. It retried once, was refused again, and gave up with a summary instead of a run.
GOTO_NEW = '{"fn":"goto","args":{"path": "/t/default/runs/new"}}'
detour = [
    {"role": "user", "content": "i want to start a new orioledb tpcc test"},
    {"role": "assistant", "tool_calls": [call("site_call", GOTO_NEW)]},
    {"role": "tool", "content": "routed"},
    {"role": "assistant", "tool_calls": [call("site_call", '{"fn":"form_state"}')]},
    {"role": "tool", "content": "…"},
    # the detour
    {"role": "assistant",
     "tool_calls": [call("site_call", '{"fn":"goto","args":{"path": "/t/default/presets/workload"}}')]},
    {"role": "tool", "content": "routed"},
    {"role": "assistant", "tool_calls": [call("read_page", "{}")]},
    {"role": "tool", "content": "…presets…"},
]
sigs = recv._call_signatures(detour)
check("going BACK after a detour is not a repeat", sigs.count(("site_call", GOTO_NEW)) == 0,
      f"{sigs} — the earlier goto is on the far side of a navigation")

# ...but re-reading the SAME page with nothing in between still counts.
stuck = [
    {"role": "user", "content": "what does this say?"},
    {"role": "assistant", "tool_calls": [call("read_page", "{}")]},
    {"role": "tool", "content": "…"},
    {"role": "assistant", "tool_calls": [call("read_page", "{}")]},
    {"role": "tool", "content": "…identical…"},
]
check("re-reading an unchanged page is still caught",
      recv._call_signatures(stuck).count(("read_page", "{}")) >= 2,
      str(recv._call_signatures(stuck)))

check("goto counts as a state change", recv._changes_state("site_call", GOTO_NEW))
check("form_state does NOT count as a state change",
      not recv._changes_state("site_call", '{"fn":"form_state"}'))
check("click counts as a state change", recv._changes_state("click", '{"text":"X"}'))
check("read_page does not", not recv._changes_state("read_page", "{}"))

# --- truncation must announce itself ------------------------------------------------
short = recv._clip_tool_result("x" * 100, "design_context")
check("a result under the cap is untouched", short == "x" * 100)
long = recv._clip_tool_result("y" * (recv.TOOL_RESULT_CHARS + 5000), "design_context")
check("an over-long result is marked as CUT", "[CUT:" in long, long[-120:])
check("the mark names the tool so a retry is not tempting", "design_context" in long)
check("and says retrying returns the same thing", "same thing" in long)

print()
print(f"{len(FAIL)} failed" if FAIL else "all passed")
sys.exit(1 if FAIL else 0)
