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
BROWSER = {"read_page", "look_at_page", "click", "type_text"}

# Tools that change the user's session. Only offered for hosts the user has enabled.
ACTING = {"click", "type_text"}


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
        "the page says. Cheap and instant.",
        {"selector": {"type": "string", "description": "Optional CSS selector to read only part of "
                                                       "the page. Omit for the whole page."}}),
    _fn("look_at_page",
        "Take a screenshot and have the vision model read it. Use for charts, diagrams, colours, "
        "layout, or anything read_page cannot express as text — a dashboard's numbers are usually "
        "in the pixels, so for 'what do these metrics show' this is the right tool. Costs about a "
        "minute (the GPU swaps to the vision model and back), so prefer read_page when the answer "
        "is text.",
        {"full_page": {"type": "boolean", "description": "true scrolls and stitches the whole page; "
                                                         "false (default) captures the viewport."},
         "question": {"type": "string", "description": "What to look for, so the reading is focused "
                                                       "on it."}}),
]

ACT_TOOLS = [
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


def for_host(actions_allowed: bool) -> list:
    """The tool list to offer. Acting tools are ABSENT, not merely discouraged, when the host is not
    enabled — a tool the model cannot see is a tool it cannot mis-call."""
    return READ_TOOLS + (ACT_TOOLS if actions_allowed else [])


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
    if name == "look_at_page":
        return ("looked at the whole page" if a.get("full_page") else "looked at the screen") + \
               (f" — {a['question']}" if a.get("question") else "")
    if name == "click":
        return f"clicked “{a.get('text') or a.get('selector', '?')}”"
    if name == "type_text":
        return f"typed into {a.get('selector', '?')}"
    return f"{name}({a})"
