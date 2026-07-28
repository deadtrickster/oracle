#!/usr/bin/env python3
"""The site_call chain, checked without a browser or a GPU.

What is worth testing here is not "does JSON parse" but the two claims this feature makes:

  1. the model is offered ONLY functions the site's manifest declares, and acting helpers obey the
     same per-host gate as click/type_text;
  2. an operation the API's spec did not mark side-effect-free CANNOT be called, and the refusal
     happens on the receiver — before anything reaches the page.

Claim 2 is the one that would be embarrassing to get wrong, so it is tested from the direction an
attacker-ish model would take it: a plausible-looking procedure that simply is not on the list.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

FAIL = []


def check(name, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


def main():
    import oracle_sitectx
    import oracle_tools

    h = oracle_sitectx.helpers_for("cloud.stroppy.io")
    check("helper discovered for the host", bool(h), repr(h)[:200])
    if not h:
        return 1
    check("manifest functions parsed", set(h["functions"]) >= {"api", "list_operations"},
          str(sorted(h["functions"])))
    check("helper source travels with it", "__oracle_stroppy" in h.get("code", ""))
    check("namespace read from the manifest", h.get("namespace") == "__oracle_stroppy",
          h.get("namespace"))

    # Suffix match: subdomains inherit, unrelated hosts must not.
    check("subdomain inherits the helper", bool(oracle_sitectx.helpers_for("stage.cloud.stroppy.io")))
    check("unrelated host gets nothing", not oracle_sitectx.helpers_for("evilcloud.stroppy.io.attacker.com"))
    check("parent domain gets nothing", not oracle_sitectx.helpers_for("stroppy.io"))

    gate = h["allowlists"]["api"]
    check("allowlist loaded from the generated file", len(gate["values"]) > 50, len(gate["values"]))

    # --- the tool as the model sees it -------------------------------------------------
    tools = oracle_tools.for_host(False, h)
    names = [t["function"]["name"] for t in tools]
    check("site_call is offered", "site_call" in names, str(names))
    check("acting tools still absent when the host is not enabled",
          "click" not in names and "navigate" not in names, str(names))
    site = [t for t in tools if t["function"]["name"] == "site_call"][0]
    enum = site["function"]["parameters"]["properties"]["fn"]["enum"]
    check("enum lists exactly the manifest's functions", set(enum) == set(h["functions"]),
          f"{sorted(enum)} vs {sorted(h['functions'])}")

    # --- the gate ----------------------------------------------------------------------
    allowed = "/cloud.v1.api.TestRunService/ListTestRuns"
    check("a declared read-only op is permitted",
          oracle_tools.check_site_call(h, {"fn": "api", "args": {"procedure": allowed}}) is None)

    for bad, why in [
        ("/cloud.v1.api.TestRunService/DeleteTestRun", "a real mutating procedure"),
        ("/cloud.v1.api.TestRunService/CreateTestRun", "the one that would start a run"),
        ("/cloud.v1.api.SystemSettingsService/UpdateSystemSettings", "shares a REST path with a read"),
        ("/cloud.v1.api.TestRunService/ListTestRuns/../DeleteTestRun", "path trickery"),
        ("", "empty"),
        (None, "missing"),
    ]:
        check(f"refused: {why}",
              oracle_tools.check_site_call(h, {"fn": "api", "args": {"procedure": bad}}) is not None,
              f"{bad!r} was ALLOWED")

    check("unknown function refused",
          oracle_tools.check_site_call(h, {"fn": "definitely_not_a_helper"}) is not None)

    # The UpdateSystemSettings case is the regression that matters: it shares `/api/v1/system/settings`
    # with a genuine read, and the first version of the generator read the marker off the wrong
    # operation on multi-method paths.
    ops = json.loads((Path(__file__).parent / "site-packs" /
                      "cloud.stroppy.io.readonly.json").read_text())["operations"]
    # Anchored at the START of the method name, not a substring: `GetSystemSettings` contains "Set"
    # and an unanchored match calls a read a write, which is a test that cries wolf until someone
    # stops reading it.
    muties = [p for p in ops if p.split("/")[-1].startswith(
        ("Create", "Update", "Delete", "Start", "Stop", "Cancel", "Rerun", "Archive", "Restore",
         "Set", "Import", "Upload", "Add", "Remove", "Revoke", "Invite"))]
    check("no mutating-looking procedure survived generation", not muties, str(muties))
    check("every entry carries a request schema",
          all("request" in v for v in ops.values()))

    # --- the local branch: list_operations must not need a browser ---------------------
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "recv", Path(__file__).parent / "oracle-capture-receiver.py")
    recv = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(recv)
    except Exception as e:
        check("receiver imports", False, str(e))
        return 1
    check("list_operations routes LOCALLY (no browser hand-off)",
          not recv._site_call_is_browser("site_call", {"fn": "list_operations"}, h))
    check("api routes to the BROWSER", recv._site_call_is_browser("site_call", {"fn": "api"}, h))
    check("read_page still routes to the browser",
          recv._site_call_is_browser("read_page", {}, h))

    text = recv._run_local_tool("site_call", {"fn": "list_operations", "args": {"match": "wizard"}},
                                False, h)["text"]
    check("list_operations returns real operations with their fields",
          "ProbeCatalog" in text and "request:" in text, text[:300])
    check("list_operations filter actually filters", "ListTestRuns" not in text, text[:200])

    empty = recv._run_local_tool("site_call", {"fn": "list_operations", "args": {"match": "zzzz"}},
                                 False, h)["text"]
    check("an empty filter result says so instead of looking complete",
          "No operation matches" in empty, empty[:160])

    # A host with no helper file must behave exactly as before this feature existed.
    with tempfile.TemporaryDirectory() as d:
        oracle_sitectx._PACK_DIR = Path(d)
        check("a site with no helper offers no site_call",
              "site_call" not in [t["function"]["name"]
                                  for t in oracle_tools.for_host(True, oracle_sitectx.helpers_for("x.com"))])

    print()
    print(f"{len(FAIL)} failed" if FAIL else "all passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
