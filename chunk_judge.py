"""LLM-as-a-judge for corpus curation: is this chunk EXERCISE material or CONTENT?

WHY A JUDGE AND NOT A RULE
--------------------------
Rules kept failing at this, in both directions:
  * the run-of-interrogatives rule is blind to OpenStax's MULTIPLE-CHOICE review questions, because
    they do not end in "?" ("3. The smallest unit of biological structure is the ________. a. organ");
  * the "obvious" fix (count question marks) DELETED A POSTGRESQL OPERATOR TABLE, because `?`, `?|`
    and `?&` ARE jsonb operators, and it ate a chapter of technical prose that poses questions and
    then answers them.
"Is this block exercise material or explanatory content?" is a JUDGMENT, not a pattern — and it has
to work across languages, which a rule cannot.

THE CASCADE (the same shape SLP3 gives for retrieval)
-----------------------------------------------------
Rules cannot score 283k chunks *well*; qwen cannot score 283k chunks *fast*. So:

    cheap recall-oriented pre-filter  ->  candidates  ->  expensive judge on the survivors

The pre-filter (`is_candidate`) is tuned for RECALL: it flags anything questionish and tolerates
false positives, because the judge will throw those out. The judge decides.

PROMPT DESIGN (adapted from MT-Bench via Lambert, "RLHF", §5.7 — in our own corpus)
-----------------------------------------------------------------------------------
Its judge prompt gives explicit criteria, demands a short explanation BEFORE a strictly-formatted
verdict, and explicitly warns the judge not to be swayed by response length. We keep all three, and
add the two counterexamples that burned us as explicit negative cases — a judge that does not know
about the jsonb `?` operator will make exactly the mistake the rule made.
"""
import re

import requests

OLLAMA = "http://localhost:11434/api/chat"
MODEL = "qwen3-coder:30b"

_VERDICT_RE = re.compile(r"\[\[(EXERCISE|CONTENT)\]\]")

# Recall-oriented. False positives are FINE — the judge removes them. A false NEGATIVE here is
# invisible and permanent, because the judge never sees the chunk.
_MC_OPTION = re.compile(r"(?m)^\s*[a-eA-E][.)]\s+\S")
_MC_STEM = re.compile(r"_{3,}")
_NUMBERED_Q = re.compile(r"(?m)^\s*\d+[.)]\s+\S")


def is_candidate(text: str) -> bool:
    """Cheap pre-filter: might this be exercise material? Err towards YES."""
    if text.count("?") >= 2:
        return True
    if len(_MC_OPTION.findall(text)) >= 3:          # a. b. c. d.  — multiple choice
        return True
    if _MC_STEM.search(text) and _NUMBERED_Q.search(text):   # "1. ... ________"
        return True
    return False


SYSTEM = """You are an impartial judge curating a retrieval corpus built from textbooks and technical manuals.

Decide whether the passage below is EXERCISE material or CONTENT.

EXERCISE means the passage exists to TEST the reader rather than to inform them:
- review questions, quiz items, self-check questions, chapter problems;
- multiple-choice items (a stem plus lettered options, often with a blank "________");
- answer keys, solutions, "critical thinking questions".

CONTENT means the passage teaches, explains, defines, describes, or documents something —
INCLUDING these cases, which are CONTENT and must never be called EXERCISE:
- explanatory prose that raises a question and then answers it ("What happens if the buffer
  fills? The server then...");
- a lone rhetorical question inside an explanation;
- reference tables, syntax listings and OPERATOR TABLES. Note carefully: in PostgreSQL `?`, `?|`
  and `?&` are JSONB OPERATORS, so a table of them is full of question marks and is still CONTENT;
- worked examples that show and explain a solution.

Rules for judging:
- Judge the passage's PURPOSE, not its punctuation. Question marks prove nothing either way.
- Do NOT let the length of the passage influence you. Short is not exercise; long is not content.
- Passages may be in any language (Russian, English, ...). Judge the same way in all of them.
- Text may be noisy from OCR or PDF extraction. Judge the intent through the noise.
- If it is genuinely unclear, answer CONTENT. Deleting real knowledge is far worse than keeping a
  stray quiz item.

First give a ONE-SENTENCE explanation. Then output your verdict on its own line, strictly as
[[EXERCISE]] or [[CONTENT]]."""


def judge(text: str, timeout: int = 120) -> tuple[str, str]:
    """Returns (verdict, explanation). Verdict is EXERCISE or CONTENT; defaults to CONTENT."""
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"PASSAGE:\n\n{text[:4000]}"},
        ],
        "stream": False,
        "options": {"temperature": 0, "num_predict": 200},
    }
    try:
        r = requests.post(OLLAMA, json=body, timeout=timeout)
        r.raise_for_status()
        out = r.json()["message"]["content"]
    except Exception as e:
        # The judge is unavailable -> keep the chunk. NEVER delete on an error: a failed judge must
        # not become a silent deleter. (The reranker's silent fallback taught us this.)
        return "CONTENT", f"judge unavailable ({e}) — keeping"

    m = _VERDICT_RE.search(out)
    verdict = m.group(1) if m else "CONTENT"
    explanation = " ".join(out.split())[:200]
    return verdict, explanation
