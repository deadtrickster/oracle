#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Generate a site's read-only API allowlist from its OpenAPI spec.

    ./scripts/gen-readonly-api.py <openapi.yaml> --out site-packs/<domain>.readonly.json \
                                  [--verify <dir with generated *.connect.go>]

## Who decides what is safe

Not me reading endpoint names and guessing, and not the model at request time — the API's own
authors, in the spec, via `x-idempotency-level: NO_SIDE_EFFECTS`. That marker is a machine-readable
statement by the people who wrote the handlers, which beats anyone's judgement about what a name
implies. `get-`/`list-` prefixes are a naming convention, not a guarantee, and are never sufficient
on their own.

Three independent signals must agree before an operation is offered to a model:

  1. the spec DECLARES it side-effect-free,
  2. it is a GET,
  3. the Connect procedure it maps to EXISTS in the generated code (`--verify`).

Everything dropped is printed. A short list that silently looks complete is the failure worth
avoiding.

Rule 2 is not redundant, and the reason is a bug this script already had. A path item holds several
operations, and `/api/v1/system/settings` carries BOTH `get: getSystemSettings [NO_SIDE_EFFECTS]`
and `put: updateSystemSettings [IDEMPOTENT]`. The first version of this script scanned the file with
regexes and attributed the marker to the LAST method it had seen on that path — so it read one
operation's safety property off another operation. Parsing the document as a document, and requiring
method and marker to come from the same operation, is what makes the marker mean anything. Rule 2
now costs nothing and fails closed if that ever regresses.

## Why the key is the Connect procedure, not the REST path

The REST paths here are not callable from a browser at all: ogen decodes a JSON body on GET, and
both `fetch` and `XMLHttpRequest` drop bodies on GET by specification. The same handlers are
reachable over Connect (POST + JSON) — which is exactly what the site's own web app speaks. So the
spec supplies the safety property, the generated code supplies the callable address, and the
allowlist is keyed by the string that actually goes on the wire. An allowlist keyed by something
adjacent to the call is one you can satisfy while calling something else.

Request schemas are emitted too, so the model can discover how to call an operation instead of being
hand-fed prose that goes stale the moment a field is added.
"""
import json
import re
import sys
from pathlib import Path

import yaml

MARKER, LEVEL = "x-idempotency-level", "NO_SIDE_EFFECTS"


def _schema_fields(spec: dict, ref: str, depth: int = 0) -> dict:
    """{field: "type — doc"} for a $ref'd request schema. One level of nesting, then it stops:
    the point is to tell a model what to send, not to reproduce the whole type system in a prompt."""
    name = (ref or "").rsplit("/", 1)[-1]
    sch = ((spec.get("components") or {}).get("schemas") or {}).get(name) or {}
    out = {}
    for field, f in (sch.get("properties") or {}).items():
        kind = f.get("type") or ""
        if "$ref" in f:
            kind = f["$ref"].rsplit("/", 1)[-1]
            if depth < 1:
                inner = _schema_fields(spec, f["$ref"], depth + 1)
                if inner:
                    kind += " {" + ", ".join(inner) + "}"
        elif kind == "array":
            items = f.get("items") or {}
            kind = f"array of {items.get('type') or items.get('$ref', '').rsplit('/', 1)[-1]}"
        doc = " ".join((f.get("description") or "").split())[:120]
        out[field] = f"{kind} — {doc}" if doc else kind
    return out


def collect(spec: dict) -> tuple[dict, list]:
    """(operations keyed by Connect procedure, list of human-readable drop reasons)."""
    ops, dropped = {}, []
    for path, item in (spec.get("paths") or {}).items():
        for method, op in (item or {}).items():
            if not isinstance(op, dict) or op.get(MARKER) != LEVEL:
                continue
            opid, group = op.get("operationId") or "", op.get("x-ogen-operation-group") or ""
            if method != "get":
                dropped.append(f"declared read-only but {method.upper()}, not GET: {path}")
                continue
            if not opid or not group:
                dropped.append(f"no derivable procedure (missing operationId/group): {path}")
                continue
            proc = f"/cloud.v1.api.{group}Service/{opid[0].upper()}{opid[1:]}"
            ref = (((op.get("requestBody") or {}).get("content") or {})
                   .get("application/json") or {}).get("schema", {}).get("$ref", "")
            ops[proc] = {"rest_path": path,
                         "summary": " ".join((op.get("summary") or "").split()).strip("'")[:200],
                         "request": _schema_fields(spec, ref)}
    return ops, dropped


def verify(ops: dict, root: Path) -> list:
    """Drop any procedure that the generated Connect code does not actually define."""
    known = set()
    for f in root.rglob("*.connect.go"):
        known |= set(re.findall(r'"(/[A-Za-z0-9_.]+/[A-Za-z0-9_]+)"', f.read_text(errors="replace")))
    if not known:
        print(f"verify: no procedures found under {root} — refusing to emit an unverified list",
              file=sys.stderr)
        sys.exit(1)
    missing = sorted(p for p in ops if p not in known)
    for p in missing:
        ops.pop(p)
    return missing


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print(__doc__.strip())
        return 2
    spec_path = Path(argv[0]).expanduser()
    spec = yaml.safe_load(spec_path.read_text(errors="replace"))

    ops, dropped = collect(spec)
    if not ops:
        print(f"no {LEVEL} GET operations in {spec_path} — right spec?", file=sys.stderr)
        return 1
    if "--verify" in argv:
        missing = verify(ops, Path(argv[argv.index("--verify") + 1]).expanduser())
        dropped += [f"no such Connect procedure: {p}" for p in missing]
        print(f"verify: {len(ops)} confirmed against generated Connect code", file=sys.stderr)
    for d in dropped:
        print(f"dropped ({d})", file=sys.stderr)

    doc = {"source": str(spec_path), "generated_from": f"{MARKER}: {LEVEL} ∩ GET ∩ exists-in-code",
           "operations": dict(sorted(ops.items()))}
    text = json.dumps(doc, indent=1, ensure_ascii=False)
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1])
        out.write_text(text + "\n")
        print(f"{len(ops)} read-only operations -> {out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
