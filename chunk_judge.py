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

_VERDICT_RE = re.compile(r"\[\[(DROP|KEEP)\]\]")

# Recall-oriented. False positives are FINE — the judge removes them. A false NEGATIVE here is
# invisible and permanent, because the judge never sees the chunk.
_MC_OPTION = re.compile(r"(?m)^\s*[a-eA-E][.)]\s+\S")
_MC_STEM = re.compile(r"_{3,}")
_NUMBERED_Q = re.compile(r"(?m)^\s*\d+[.)]\s+\S")


_TOC_LEADER = re.compile(r"\.{4,}\s*\d+")                    # "Chapter 5 ........ 123"
_CITEKEY = re.compile(r"\[[A-Z][A-Za-z]+\d{2,4}\]")          # "[RYSTSOV16]" bibliography key


def _numeric_ratio(text: str) -> float:
    toks = text.split()
    if len(toks) < 20:
        return 0.0
    nums = sum(1 for t in toks if any(c.isdigit() for c in t) and
               sum(c.isdigit() for c in t) >= len(t.strip(".,;:()")) / 2)
    return nums / len(toks)


def is_candidate(text: str) -> bool:
    """Cheap, recall-oriented pre-filter: might this be droppable apparatus? Err towards YES."""
    if text.count("?") >= 2:
        return True
    if len(_MC_OPTION.findall(text)) >= 3:          # a. b. c. d.  — multiple choice
        return True
    if _MC_STEM.search(text) and _NUMBERED_Q.search(text):   # "1. ... ________"
        return True
    # apparatus signatures: index/TOC/bibliography are keyword-dense with page numbers / years /
    # citation keys, and they out-rank real content on keyword queries.
    if _numeric_ratio(text) >= 0.12:                # index / bibliography: many page/ref numbers
        return True
    if len(_TOC_LEADER.findall(text)) >= 2:         # table of contents: dotted leaders
        return True
    if len(_CITEKEY.findall(text)) >= 2:            # bibliography: [Author16] keys
        return True
    return False


SYSTEM = """You are an impartial judge curating a retrieval corpus built from textbooks and technical manuals.

Decide whether the passage below is DROP (it should NOT be a search result) or KEEP (real content).

DROP means the passage carries no information a reader could be answered WITH — it exists to test,
navigate, or reference, not to inform:
- APPARATUS: a book's INDEX (term followed by page numbers: "locks and leader election, 330"),
  TABLE OF CONTENTS (headings with page numbers / dotted leaders), LIST OF FIGURES/TABLES,
  BIBLIOGRAPHY / REFERENCES (citation entries: "[RYSTSOV16] Rystsov, D. 2016. ..."), running heads,
  copyright/front-matter.
- EXERCISE: review/quiz/self-check questions, multiple-choice items (stem + lettered options, blanks
  "________"), answer keys, "critical thinking questions".

KEEP means the passage teaches, explains, defines, describes, or documents something. KEEP these even
though they can look like the above:
- explanatory prose that raises a question and then answers it;
- a GLOSSARY that DEFINES terms (term + a real definition sentence is content, not an index);
- reference/data TABLES and syntax/OPERATOR listings with real values (in PostgreSQL `?`, `?|`, `?&`
  are JSONB operators — a table of them is content). A table of DATA is content; a list of
  term→page-number is not;
- worked examples; any passage whose numbers are DATA (measurements, code, results), not page/ref
  numbers.

The key test for apparatus: are the numbers PAGE/REFERENCE numbers pointing elsewhere (DROP), or are
they DATA that answers something (KEEP)? An index says "Raft, 349" — it points you away; it cannot
answer "what is Raft".

Rules:
- Judge PURPOSE, not surface. Punctuation and length prove nothing.
- Any language (Russian, English, ...); judge the same way. Text may be OCR/PDF-noisy — see through it.
- If genuinely unclear, KEEP. Deleting real knowledge is worse than keeping a stray apparatus chunk.

First give a ONE-SENTENCE explanation. Then output your verdict on its own line, strictly as
[[DROP]] or [[KEEP]]."""


def judge(text: str, timeout: int = 120) -> tuple[str, str]:
    """Returns (verdict, explanation). Verdict is DROP or KEEP; defaults to KEEP."""
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
        return "KEEP", f"judge unavailable ({e}) — keeping"

    m = _VERDICT_RE.search(out)
    verdict = m.group(1) if m else "KEEP"
    explanation = " ".join(out.split())[:200]
    return verdict, explanation
