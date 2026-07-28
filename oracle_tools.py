"""The tools the chat can call — and the line between looking and acting.

The ceiling this removes: asked "what do you think about this page?", a chat turn could only do the
one thing every turn did unconditionally — retrieve from the corpus — and the corpus has nothing to
say about a page it has never seen. The honest answer requires LOOKING, so the model has to be able
to act rather than only be fed.

## Two kinds of tool, and why the split is structural

  READ  search_corpus, read_page, look_at_page   — cannot change anything
  ACT   click, type_text                          — operate the user's logged-in session

A wrong READ is a wrong answer: annoying, visible, recoverable. A wrong ACT is a wrong DEED —
a deleted run, a triggered rerun, a submitted form — in a session the model did not authenticate
and cannot undo. The page this was built against has Delete, Rerun and New Run within a few hundred
pixels of each other.

So acting tools are gated PER HOST, and the gate lives in the harness: when a host is not enabled,
the acting tools are not described to the model at all. Not "described and discouraged" — absent.
A prompt asking a model to be careful is exactly the workaround this repo keeps refusing to write
(Axiom 2): if the harness can make an action impossible, it must, rather than asking nicely.

## Where each tool runs

`search_corpus` runs on the receiver, which has retrieval. Everything else needs a DOM, which the
receiver does not have and never will — so those are handed back to the extension, which executes
them and posts the result as the next turn. The model decides "move the right hand"; the component
that owns the hand does the moving, and reports what actually happened.
"""

# Tools the RECEIVER executes itself.
LOCAL = {"search_corpus"}

# Tools that need a browser. The extension executes these and posts the result back.
BROWSER = {"read_page", "look_at_page", "click", "type_text", "wait", "navigate", "site_call"}

# Tools that change the user's session. Only offered for hosts the user has enabled.
ACTING = {"click", "type_text", "navigate"}


def _fn(name, description, properties, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(required)}}}


READ_TOOLS = [
    _fn("search_corpus",
        "Search the user's offline corpus of documentation, books and papers. Use for questions "
        "about how something works in general, not about what is on the screen. Returns numbered "
        "excerpts you must cite as [n].",
        {"query": {"type": "string", "description": "What to look for. A focused phrase beats a "
                                                    "sentence."}},
        ["query"]),
    _fn("read_page",
        "Read the rendered text of the page the user is looking at. Use this before guessing what "
        "the page says. Cheap and instant. It CANNOT see inside an iframe — an embedded dashboard "
        "reads as 'Loading…' no matter how long you wait, so use look_at_page for those: the "
        "screenshot shows what is actually rendered.",
        {"selector": {"type": "string", "description": "Optional CSS selector to read only part of "
                                                       "the page. Omit for the whole page."}}),
    _fn("wait",
        "Pause before looking again, for content that loads asynchronously. Use after a click that "
        "starts a load, instead of reading the same half-rendered page twice.",
        {"seconds": {"type": "number", "description": "1-15."}},
        ["seconds"]),
    _fn("look_at_page",
        "Take a screenshot and have the vision model read it. Use for charts, diagrams, colours, "
        "layout, or anything read_page cannot express as text — a dashboard's numbers are usually "
        "in the pixels, so for 'what do these metrics show' this is the right tool.\n"
        "COST, precisely, because it changes what is worth doing: the GPU holds one model at a "
        "time, so a look costs a swap to vision and a swap back — about a minute. BUT tool calls "
        "you request TOGETHER in one reply run back-to-back without the text model in between, so "
        "several looks in one reply cost ONE swap, not one each. If you need to survey three views, "
        "ask for click, look, click, look, click, look in a SINGLE reply rather than one per turn. "
        "Prefer read_page when the answer is text.",
        {"full_page": {"type": "boolean", "description": "true scrolls and stitches the whole page; "
                                                         "false (default) captures the viewport."},
         "question": {"type": "string", "description": "What to look for, so the reading is focused "
                                                       "on it."}}),
]

ACT_TOOLS = [
    _fn("navigate",
        "Go to a URL in the tab you are working in. Only within the SAME SITE — this is for using a "
        "site's own URL grammar (filters, tabs, ids) when its reference material documents one, "
        "which is faster and more reliable than clicking through to the same place. It changes what "
        "the user is looking at, so say why first.",
        {"url": {"type": "string", "description": "Absolute URL on the current site."}},
        ["url"]),
    _fn("click",
        "Click an element on the page. This ACTS on the user's logged-in session and cannot be "
        "undone by you — a click may delete, rerun, submit or navigate. Identify the target by its "
        "visible text where possible, and say what you are about to do and why before calling it.",
        {"text": {"type": "string", "description": "The element's visible text, exactly as shown."},
         "selector": {"type": "string", "description": "CSS selector, if text is ambiguous or the "
                                                       "element has no text."}}),
    _fn("type_text",
        "Type into a field on the page. ACTS on the user's session. Does not submit unless you also "
        "click something.",
        {"selector": {"type": "string", "description": "CSS selector of the input."},
         "text": {"type": "string", "description": "What to type."},
         "clear": {"type": "boolean", "description": "Clear the field first (default true)."}},
        ["selector", "text"]),
]


def site_tool(helpers: dict, actions_allowed: bool):
    """One `site_call` tool describing the named functions this site's helper file provides.

    Why a single tool with an `fn` enum, rather than one tool per function: the tool list is part of
    the CACHED PREFIX. A site with a dozen helpers would push a dozen schemas in front of every
    request on every host, and the prefix is the thing we spent real effort keeping stable. One tool
    whose enum varies per host keeps the shape constant.

    Helper functions that change something are marked `"acts": true` in the manifest and obey the
    same per-host gate as click/type_text — a site helper is not a way around the gate, and if it
    became one the gate would be theatre."""
    fns = {n: s for n, s in (helpers.get("functions") or {}).items()
           if actions_allowed or not s.get("acts")}
    if not fns:
        return None
    lines = []
    for name, spec in sorted(fns.items()):
        params = ", ".join(f"{p}: {d}" for p, d in (spec.get("params") or {}).items())
        lines.append(f"  {name}({params}) — {spec.get('description', '')}"
                     + ("  [CHANGES THINGS]" if spec.get("acts") else ""))
    return _fn("site_call",
               "Call one of this site's own functions. These talk to the site's API directly and "
               "return exact JSON — no screenshot, no reading numbers off pixels, no model swap. "
               "PREFER THIS over look_at_page whenever a function below answers the question: a "
               "number you read from a chart is a transcription, a number from here is the value. "
               "Available on this site:\n" + "\n".join(lines),
               {"fn": {"type": "string", "enum": sorted(fns), "description": "Which function."},
                "args": {"type": "object", "description": "Its arguments, as named above."}},
               ["fn"])


def for_host(actions_allowed: bool, helpers: dict | None = None) -> list:
    """The tool list to offer. Acting tools are ABSENT, not merely discouraged, when the host is not
    enabled — a tool the model cannot see is a tool it cannot mis-call."""
    tools = READ_TOOLS + (ACT_TOOLS if actions_allowed else [])
    site = site_tool(helpers or {}, actions_allowed)
    return tools + ([site] if site else [])


def check_site_call(helpers: dict, args: dict) -> str | None:
    """Reject a site_call the manifest does not permit; None means allowed.

    The allowlist check lives here — in the harness, before the call leaves the receiver — because
    the alternative is checking inside the page, where the code doing the checking is the code being
    asked to run. A gate is only a gate if it sits upstream of the thing it gates."""
    fn = (args or {}).get("fn")
    spec = (helpers.get("functions") or {}).get(fn)
    if not spec:
        return f"{fn!r} is not a function this site provides."
    gate = (helpers.get("allowlists") or {}).get(fn)
    if not gate:
        return None
    val = ((args or {}).get("args") or {}).get(gate["param"])
    if val not in gate["values"]:
        return (f"{gate['param']}={val!r} is not permitted. This site's API allowlist is generated "
                f"from its OpenAPI spec and contains only operations the spec marks as having no "
                f"side effects. Call list_operations to see them.")
    return None


def is_browser(name: str) -> bool:
    return name in BROWSER


def is_acting(name: str) -> bool:
    return name in ACTING


def describe(name: str, args: dict) -> str:
    """A one-line, human-readable account of a call, for the panel and the transcript. The user
    should be able to see what was done without reading JSON."""
    a = args or {}
    if name == "search_corpus":
        return f"searched the corpus for “{a.get('query', '')}”"
    if name == "read_page":
        return "read the page" + (f" ({a['selector']})" if a.get("selector") else "")
    if name == "wait":
        return f"waited {a.get('seconds', 0)}s for the page to load"
    if name == "look_at_page":
        return ("looked at the whole page" if a.get("full_page") else "looked at the screen") + \
               (f" — {a['question']}" if a.get("question") else "")
    if name == "click":
        return f"clicked “{a.get('text') or a.get('selector', '?')}”"
    if name == "type_text":
        return f"typed into {a.get('selector', '?')}"
    if name == "navigate":
        return f"went to {a.get('url', '?')}"
    if name == "site_call":
        inner = a.get("args") or {}
        detail = inner.get("path") or inner.get("id") or ""
        return f"asked the site's API: {a.get('fn', '?')}" + (f" ({detail})" if detail else "")
    return f"{name}({a})"
