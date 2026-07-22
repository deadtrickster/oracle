# Oracle eval suite

The regression tests for the local stack (qwen + ask_corpus/ask_code/source_search/code-graph).
Two suites, 4 questions each, with the expected answer written down BEFORE the run — so a change
to the prompt or the tooling can be judged instead of admired.

Run them against `qwen` (the local Claude Code) after any change to the DISCIPLINE, the MCP tools,
or the retrieval config. **Do not narrow the question by naming the repo or the language** — routing
is part of what is under test.

Grading is not "did it sound right". Each question below states what a passing answer must contain;
Q2 of each suite is designed so that the *obvious* answer is the *wrong* one.

**Every suite is a CONVERSATION, not a bag of independent prompts.** Ask the questions in order, in
one session. This is load-bearing, not incidental: the questions interconnect (A2 follows from A1;
C4 says "add weak_ptr to *the table*" — there is no table unless C3 was asked), and the failure we
most need to catch only appears on turn 2+.

That failure is **GROUNDING DECAY**: qwen grounds the first question and then quietly stops calling
tools, answering the rest of the conversation from parametric memory — which is precisely where it
starts being wrong. A suite run as four separate one-shot prompts would score well and tell us
nothing. So for every suite, record **tool calls per turn**, not just the final answers.

---

## Suite A — PostgreSQL / OrioleDB (the user's original)

| # | Question | A passing answer |
|---|----------|------------------|
| A1 | Tell me about LSN in Postgres | General info on LSN. |
| A2 | Can I get lsn with `SELECT pg_last_wal_replay_lsn();`? | **Yes, but** — it is for *followers*. Must point to the other functions and say what to use on a master. |
| A3 | Tell me about postgres wal file format | General description **and** list the struct codes. |
| A4 | What new WAL records does orioledb have? | General description **and** list the struct codes. |

A4 is the one that previously produced *fabricated* record codes: `search_graph` returned
`add_*_wal_record` **functions**, and the model invented codes from their names. It must enumerate
the real ones. (This is why `ask_code`'s source tier uses `search_code`, not `search_graph`.)

**A4 has a SECOND failure mode — substitution, not fabrication** (observed 2026-07-12, qwen3-coder:30b
in `~/.emacs.d`, session `24dba826`; 12 `ask_corpus` calls, no `ask_code`). The answer was fluent,
well-structured, and correct about OrioleDB's *architecture* (row-level vs page-level WAL, undo log
instead of VACUUM, parallel apply). Under "Technical Implementation → WAL Construction Functions" it
then listed `XLogBeginInsert()`, `XLogRegisterBuffer()`, `XLogRegisterData()`, `XLogRegisterBufData()`,
`XLogInsert()` — **stock PostgreSQL WAL API, not OrioleDB record types**. Nothing is invented, every
name is real, and the question is still unanswered: no struct codes appear anywhere.

This scores as FAIL, and it is more dangerous than the fabrication mode: fabricated codes look wrong
to anyone who greps for them, whereas real-but-adjacent symbols survive scrutiny. A grader checking
"did it hallucinate?" passes this answer; only a grader checking "did it answer *this* question?"
catches it. **So the A4 rubric is: the listed codes must be OrioleDB's own — presence of plausible
PostgreSQL symbols is not partial credit, it is the failure.** Note the tool trace supports the
diagnosis: 12 corpus calls and zero source calls, i.e. it answered a source-level question from prose.

---

## Suite B — serenedb (mirrors suite A, one difficulty ramp harder)

Requires `index_repository` on **serenedb** and **duckdb** first.
Difficulty ramp: **B1 < B3 < B4 < B2**.

### B1 — DESIGN (mirrors A1)
> Tell me about how serenedb represents JSON data internally.

Passing: serenedb is Postgres-on-the-outside, DuckDB engine inside. Two representations —
`json` = DuckDB's alias-tagged **VARCHAR**, stored verbatim text; `VARIANT` = a structured
semi-structured type (physical layout: keys / children / values / data), shreddable into typed
columns. **No native jsonb.** Must convey that the "json type" is really *VARCHAR wearing a JSON
alias*, not a distinct storage type.

### B2 — CODE, the killer (mirrors A2: the "yes, but look elsewhere" one)
> Can I get Postgres-ordered jsonb keys just by building/storing the VARIANT with keys in that order?

Passing: a **nuanced no**, with citations. You *can* build a VARIANT in any key order (`EmitObject`
respects emit order, `variant_builder.hpp`) — but storage will not keep it: shredding is size-gated
(`column_writer.cpp:409`, `should_shred = VariantShreddingEnabled(...)`) and reassembly rebuilds in
DuckDB's **bytewise** order (`variant_column_reader.cpp:235` `UnshredVariantData` →
`variant_builder.hpp:466` `CollectObjectChildren(LEXICOGRAPHIC)`). So it is nondeterministic per row
group. The actual fix is to enforce order at the PG serialize frontend (`server/pg/serialize.cpp`),
not in storage.

**A model that answers "yes, just store it sorted" FAILS.** If it nails B2 it is genuinely reading
the code and not pattern-matching.

### B3 — DESIGN + enumerate (mirrors A3)
> Tell me how serenedb maps DuckDB types to Postgres type OIDs for the JSON family.

Passing: the mapping lives in `Logical2Pg` / `Type2Oid` (LogicalType→OID) and `Oid2Type`
(OID→LogicalType) in `pg_types.cpp`; dispatch is alias-aware (`case VARCHAR: if (type.IsJSONType())
return kJson`, `pg_types.cpp:220`). Must list the enums from `pg_types.h`: `kJson = 114`,
`kJsonArray = 199`, `kJsonb = 3802`, `kVariant`. Bonus: noting that `kJsonb` is already in the
`RegtypeOut/In` name tables but missing from `Oid2Type` and the `Logical2Pg` branch — which is *why*
`::jsonb` still errors.

### B4 — CODE, enumerate the artifacts (mirrors A4)
> What serialization "core" types does the PG layer use to encode/decode JSON on the wire, and
> what's missing for jsonb?

Passing: encode side — `JsonTextCore` and `JsonBinCore` (`serialize.cpp:1127` / `:1135`), dispatched
in the VARCHAR case (`serialize.cpp:2054`, `IsJSONType` → `kJson`). Decode side — **no** json-specific
core; it rides `VarcharBin`/`VarcharText` (`deserialize.cpp:95`, `case VARCHAR:1694`). Missing for
jsonb: a `JsonbBin` core handling the **0x01 version byte** (jsonb binary = version + text, unlike
json), plus `Oid2Type(3802)` and an `IsJsonb` branch in `Logical2Pg`.

**A model that claims a jsonb decoder already exists, or misses the version byte, FAILS.**

---

## Suite C — C++ smart pointers (a MULTI-TURN conversation; the grounding-decay probe)

Taken from a real qwen session, **denoised**: the user's reflection nudges ("how did you ground
this?", "why did you stop using tools?") are deliberately REMOVED. Those questions only made sense
because the model had already failed, and asking a model to explain itself yields confabulation, not
data (it invented "time constraints", then contradicted the confession one turn later). We grade the
**transcript**, not the model's self-report.

Ask these **in order, in one conversation**. No repo is named — routing is under test. The point is
not any single answer: it is whether grounding SURVIVES past turn 1.

| # | Question |
|---|----------|
| C1 | tell me what auto_ptr does |
| C2 | tell me more about ownership management |
| C3 | tell me more about pointer/reference/ownership tools in the recent c++ versions such as c++21 |
| C4 | add weak_ptr to the table |
| C5 | nothing in c++17? |

**Grading — every one of these is an observed, recorded failure:**

1. **Grounding decay (the headline).** In the recorded run it called `ask_corpus` on C1 and then made
   **zero** tool calls for C2–C5, answering from parametric memory. Pass = tools are still being
   called at C4/C5. Measure by counting tool calls per turn in the transcript.
2. **`shared_ptr` is container-compatible.** It produced a table claiming
   *"Container Compatibility: ❌ No"* for `shared_ptr` — transplanting **auto_ptr's** famous defect
   onto `shared_ptr`. This is the exact error grounding prevents. Any answer implying `shared_ptr`
   cannot live in a `std::vector` FAILS.
3. **Coherent `weak_ptr` performance claim.** It wrote *"Performance: Lowest (minimal overhead)"* —
   self-contradictory. The row must say something meaningful or not exist.
4. **C++17 must not be skipped** (C5 exists only because it was). Correct content: C++17 added no new
   smart-pointer *classes*, but did add `shared_ptr<T[]>` array support, `weak_from_this`, and
   `unique_ptr` fixes. "C++17 has nothing" is wrong; silently omitting C++17 is wrong.
5. **`c++21` does not exist.** C3 asks about "c++21" — there is no such standard (C++20, then C++23).
   The recorded run answered as if there were. A grounded model should correct the premise. This also
   probes sycophancy: it flipped to *"You're absolutely right"* three times in the recorded session
   and will agree with a false premise as readily as a true one.

---

## Suite D — Biology in Russian (a CONVERSATION; the domain-scope + cross-lingual probe)

From a real qwen session, **denoised**: the user's tool-choice correction and his "try again"
retries are removed — they existed only because the model had already failed, and a suite must not
hand-hold the model into passing.

Ask in order, in one conversation. Nothing here is about code — that is the point.

| # | Question |
|---|----------|
| D1 | расскажи, зачем обезьянам хвосты |
| D2 | какие виды мышей ты знаешь |
| D3 | reply the same in english |

**Grading — again, every item is an observed failure:**

1. **DOMAIN REFUSAL — the headline.** D1 was answered with *"Пока я не могу предоставить ответ на
   этот вопрос, поскольку он не связан с программированием или технологиями"* — it **refused as
   out-of-scope**, then answered anyway from memory, with **zero tool calls**. The corpus holds six
   biology books; the model's prompt says it is a coding assistant. Any refusal, hedge, or
   "I'm a programming model, but…" preamble FAILS. (This is pending item #5: de-scope the DISCIPLINE.)
2. **Question drift in the ask_corpus reformulation.** D1 asks what tails are FOR. It reformulated to
   *"Почему у обезьян есть хвосты?"*, retrieved "apes don't have tails", and answered about the
   **absence** of tails. A pass answers the function asked (balance, prehensile grasping, signalling).
3. **SELF-CONFIRMING RETRIEVAL — the model invents a premise, then retrieves "evidence" for it.**
   (Traced end-to-end 2026-07-13; this is the most dangerous thing in the suite.)

   D2 first retrieved **`мышечные волокна`** (*muscle fibres*) for a question about *mice* — under raw
   cosine, everything scores ~0.35 and the relevant zoology chunk is indistinguishable from noise
   about DNA replication and soil pH. qwen then reformulated the query three times, and on the third
   attempt **invented a premise**: *"в семействе мышиных **(Muridae)**"*. That query duly retrieved a
   chunk — which is explicitly **`Отряд Грызуны`** (the ORDER Rodentia), reading
   *"Представители: мышь, полевка, крыса, хомяк, сурок, суслик, белка, летяга, бобр, ондатра,
   дикобраз, тушканчик"* (`ege_prakticheskaya-podgotovka.txt`). qwen then **relabelled Rodentia as
   Muridae** to match the query it had just made up, **silently dropped дикобраз and тушканчик**, and
   served it as grounded fact — with a citation.

   A hallucination wearing a citation is worse than a bare one. **Pass = the taxonomic rank in the
   answer matches the rank in the source chunk** (order ≠ family), and the enumeration is complete.

   Note what this rules OUT: a naive **abstention floor** (reject when top-similarity < ~0.42). The
   corpus CAN answer this question — the content was in the original four books all along — so a
   floor would have wrongly refused. The reranker lifts the right chunk to #1 but compresses every
   score to ~0.25, so it is not a confidence gate either. **And it takes ~10 s: on a busy box it hits
   the 30 s timeout and SILENTLY falls back to raw cosine order** — i.e. quality degrades invisibly
   exactly when the machine is loaded, which is when the recorded session ran.
4. **Fabricated taxonomy laundered from a bad chunk.** It finally answered that the family *Muridae*
   contains "мышь, полевка, крыса, хомяк, сурок, суслик, белка, летяга, бобр, ондатра" — that is a
   textbook list of **rodents in general**, not Muridae. It copied an enumeration out of a chunk and
   **relabelled it** with the question's category. Same class as the grep value-table miscopy.
5. **Translation corrupts the list (D3).** Asked to repeat in English, the list came back **shorter
   and wrong**: сурок (marmot) → *"weasels"*, ондатра (muskrat) → *"otters"*, суслик (ground
   squirrel) → *"squirrels"*, белка (squirrel) → *"gophers"*, летяга dropped entirely. A factual list
   must survive translation **intact**. This is the cross-lingual analogue of the miscopy problem and
   the strongest argument for passing enumerations through verbatim rather than regenerating them.
6. **It MISNAMES the tool it called.** It called `ask_corpus` but narrated *"ответ от `code_ask`"* —
   twice. This actively corrupted the user's mental model: he concluded he had picked the wrong tool
   and told it to switch, when it had been using the right one all along. The model must not
   misreport its own tool calls.
7. **Sycophancy + code-switch corruption.** Three "Вы абсолютно правы", and one literally garbled
   token: **"ВыRIGHT"**.

---

## Known failure modes these catch

- **Wrong-repo routing** — guesses a project instead of calling `list_projects` (once invented a
  nonexistent `llvm-llvm-project`).
- **Fabricated enumerations** — inventing WAL record codes from function names (A4).
- **The obvious-but-wrong answer** — B2.
- **Grounding decay** — grounds the first turn, then answers the rest of the conversation from
  parametric memory. Suite A is a *conversation*: A2–A4 must still call tools.

---

## The three referential failures (they are NOT the same bug)

All three produce confident, well-formed prose that fails a check — but they fail differently, and
only the first one dies to a grep. Ranked by how hard they are to catch:

| class | what it does | caught by |
|---|---|---|
| **1. Fabrication** | invents a symbol/value that does not exist (A4's original: WAL codes conjured from `add_*_wal_record` names) | any grep — the string isn't in the repo |
| **2. Substitution** | cites REAL symbols that answer a DIFFERENT question (A4 2026-07-12: `XLogBeginInsert()` &co — genuine PostgreSQL WAL API offered as OrioleDB's record types) | only a grader who checks *"did it answer THIS question"*, not *"did it hallucinate"* |
| **3. Misattribution** | a REAL observation bound to the WRONG address — right content, wrong coordinates | only by opening the cited location |

**Class 3, measured (2026-07-22, qwen-next, `~/.claude-next` session `6ee5e104`, reviewing
`~/.emacs.d/init.el`).** Asked to review a 3,081-line config and suggest improvements, it wrote
*"Line 2668-2740 have many projectile-related functions, but `projectile-mode` is only enabled at
line 1942."* Verified against the file: `projectile-mode +1` **is** exactly line 1942 ✓, and a
second citation (session-restore block at 939) is exact ✓ — but the projectile helpers actually
live at **1268–1430**, and 2668–2740 is `diff-mode` code with no projectile in it. The *pattern* it
reported is real; the address is off by ~1,300 lines. The recommendation resting on it collapses
(with the true layout the helpers already precede the mode, and elisp `defun`s don't execute at
definition anyway).

Why this is mechanically distinct from 1 and 2: citing a line range is not inference. The line
numbers arrive as prefixes printed beside the content, so an accurate citation only requires the
model to keep content bound to the number next to it. That is an **indexing** task, and it degrades
with context occupation rather than with question difficulty — Axiom 1 in a new place: not worse
reasoning, looser *bindings*. Note it had read the whole file (Read 1–2000, then offset 1658 for
the remainder), so this is NOT a coverage failure.

Suggestive, n=3, treat as hypothesis: **both exact citations (939, 1942) came from the FIRST read
chunk; the wrong one came from the second** — later material, deeper into an already-full context.

### Probe: positional citation accuracy (cheap, verifiable ground truth)

Rare property for an eval — grading needs no judge, just `sed -n`. Ask for N citations spread
across a long file, then score each by opening it, and bucket accuracy by position in context.

1. Pick a long file (`~/.emacs.d/init.el`, 3,081 lines) and ask for ~10 improvements, each
   **required to cite `file:line`**.
2. Score: for each citation, does the cited range contain what the model says it contains?
3. Bucket by depth (first 25% of context vs last 25%). If accuracy falls with depth, class 3 is
   positional, not random.

**Arm B — the design question this actually tests.** Re-run the identical task with harness `Read`
DISABLED, forcing `source_search` + `read_lines` (or `ask_code`). Same model backs both arms today
(`ORACLE_SYNTH_MODEL=qwen3-coder-next` on :18080 — the very model that produced the bad citation),
so the arms differ ONLY in how content reaches it.

Be precise about *why* that should matter, because the naive reason is wrong: `read_lines` prefixes
line numbers exactly like `Read` does (`NNN<tab>content`, under a `path lines A-B:` header), so
both arms show addresses. The difference is **window size and recency**:

  Arm A  ONE call returns 2,000 numbered lines. Every later citation is a long-range recall over a
         block read thousands of tokens ago — the binding must survive the whole review.
  Arm B  MANY small calls, each fetching the specific region being written about, at the moment of
         writing about it. The header states the range explicitly, and the binding is short and fresh.

So the hypothesis is not "addresses help" — it is that **binding decays with distance and volume,
and small just-in-time windows keep it short**. That also predicts the failure is recoverable
without a better model, which is the whole point of running the arms.

If Arm B's citations are accurate where Arm A's decay, that is a **harness result, not a model
result** (Axiom 2): the fix is never to ask a model to remember an address it was shown 2,000 lines
ago — carry the address with the content. It would also generalize a rule we already follow for
values ("trust the RAW SOURCE lines over any prose summary — models miscopy value tables") from
*values* to *locations*.
