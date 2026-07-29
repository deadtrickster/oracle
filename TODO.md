# Oracle — TODO

Last updated **2026-07-16**.

This file is the **durable state of the work**. **§G is the only checklist** — the work that is
actually happening. §A–D describe what each item *is* (reference, cited by number from §G). §F logs
what was done and what it measured. §H parks the good ideas we are deliberately not building.
Written to survive a compaction, a reboot, and a week away from the machine.

---

## 🧊 FEATURE FREEZE — declared 2026-07-14

**No new items.** The list went 4 → 22 in two days because every fix surfaced two more bugs, and
each one was genuinely interesting. That is how a project dies: perpetually one fascinating detour
from being useful.

### What this is actually for

`orioledb-waldump` was the **forcing function**, not the goal — a concrete task that would prove the
thing worked. We are well past it. Look at what two days of work actually produced: none of it was
about WAL records. It was a stemmer that made the one informative word match *nothing*; a parser that
reported `DONE` on zero output; a reranker that silently stopped reranking under load; a model that
fabricated a taxonomy **and attached a citation to it**.

So the real subject is:

> **An assistant whose answers you can TRUST when there is no network to check them against.**

Offline is what makes that hard, and interesting. With a network, a wrong answer is an inconvenience —
you notice, you Google, you move on. Offline, a confident wrong answer *is the output*. Every failure
we found has the same shape: **the system did less than it claimed and said nothing.**

### The test an item must pass

*Does this make a grounded answer more trustworthy — or make an untrustworthy one visible?*

That is the whole criterion. It admits three kinds of work, and §G is exactly those three:

- **G1 — can it FIND the answer?** (recall@8 = 40% — the passage is missing 60% of the time, and that
  failure *reads as* "the model hallucinated")
- **G2 — can it USE its tools?** (a wrong-repo search costs 4 calls, a defocused context, and once,
  fabricated WAL codes)
- **G3 — can I CHECK it, and will it be there?** (read the raw source myself; open the cited page;
  and the stack must come up from cold at 30,000 feet)

**§H — PARKED.** Good ideas, deliberately not being built. Written down so they cost nothing to
leave alone. New ideas go here **without discussion** — the freeze is the feature. Anything already
in flight finishes; nothing new starts.

---

## Working protocol

1. **Work §G, top to bottom.** It encodes real dependencies, not preferences — some items make
   things *worse* if done out of order (bumping the retrieval pool before fixing the reranker's
   silent timeout degrades quality *invisibly*).
2. **Measure before and after, with the instruments in §D.** No fix is good because it sounds good:
   on 2026-07-13 three diagnoses sounded excellent and were all wrong. If the number doesn't move,
   say so plainly rather than quietly banking the change.
3. **On finishing an item:** update `DESIGN.md` / `BLOG.md` *if the item changed the design or is
   worth telling* — but **always update this file**: tick the box, and add an entry to §F recording
   **the approach taken and the result measured** (including the ones that didn't work).
4. **Ask before destroying index state.** Present the reasons and the numbers first.
5. **New idea? It goes to §H.** Not into §G, not "while I'm here". The freeze is the feature.

---

## A. Tool use & context hygiene  *(reference — the checklist is §G)*

Items 1–4 were agreed in principle; 5–8 came out of the auto_ptr transcript.

- **1. Routing — remove the prompt bias.** `qwen.sh`'s DISCIPLINE hardcodes two orioledb project
      names, so every `project` argument gravitates to them (it searched a C database fork for a C++
      stdlib class, and once *invented* a nonexistent `llvm-llvm-project`). **Delete the named
      examples**; tell it to call `list_projects`. Do NOT add a "language vs repo" rule — removing
      prompt bias beats adding prompt rules.
- **2. `ask_code` empty-result redirect.** It dead-ends with "Not found under X" while
      `source_search` already redirects. Same auto-broaden ⇒ a wrong project guess self-corrects in
      one call instead of four.
- **3. Rerank grep hits** with the idle CPU reranker (:9760). Today matches are truncated by
      *path order* (alphabetical, i.e. meaningless) — the auto_ptr definition lost to kiwisolver
      comments. Keep top-k VERBATIM. Do NOT summarise grep through qwen (it miscopies value tables).
- **4. Compact code-graph output.** `search_graph`'s `fp`/`sp`/`bt` blobs are pure context noise.
- **5. De-scope the DISCIPLINE.** Asked about mice, qwen refused — *"не связан с
      программированием"* — but the corpus now holds biology. Same bug class as the hardcoded project
      names: the PROMPT over-narrows.
- **6. `source_search` must accept the graph-slug it prints itself.** `list_projects` outputs
      `algo/go (graph project: home-dead-Projects-algo-go)`; feeding that slug back is rejected, while
      `ask_code` accepts it. Our own tools disagree on what an identifier is.
- **7. Grep anchoring gives false negatives.** The broadness guard says "anchor the definition";
      the model wrote `class auto_ptr` → *no matches*, because libcxx writes
      `class _LIBCPP_TEMPLATE_VIS auto_ptr`. On empty result, auto-relax (drop leading keywords, keep
      the trailing identifier) and report what the relaxed search found.
- **8. `Read` cannot consume `source_search` output.** It emits repo-relative paths; `Read` needs
      absolute → "File does not exist" (twice). Emit absolute paths.

> 1, 2, 6, 7 and 8 are all the same shape: **the harness knows the right answer and returns an error
> instead of saying it.** That is the closed-loop failure — the model asked to move the right hand,
> and the harness silently lost the step.

---

## B. Retrieval quality  *(reference — the checklist is §G)*

- ✅ **9. Cyrillic stemmer — SHIPPED (2026-07-13).**
      `ragflow/rag/nlp/rag_tokenizer.py` (bind-mounted): Snowball over Cyrillic tokens, applied to the
      indexer and the query builder alike. Upstream stems English (Porter) and leaves Russian
      untouched, so `мышей` could never match the indexed `мышь` — the only informative, high-IDF term
      in the query matched **nothing**, while `виды` matched everywhere. *IDF cannot rescue a term
      that never matches.* **Measured: gold rank 101 → 32.** Required re-parsing all Cyrillic docs.

- **10. Bump the retrieval pool 64 → 256.** *(needs 12 first)*
      **recall@64 = 80%, recall@256 = 100%** — everything is findable, the pool is just too small.
      **Blocked:** reranking 64 chunks takes ~10 s, so 256 takes ~40 s — past the **30 s timeout**,
      which silently falls back to raw cosine. Bumping the pool naively makes things *worse,
      invisibly*. ⇒ First parallelise the reranker across the idle cores (24 available;
      `reranker-service.py`, :9760).

- **11. Widen the final slice (currently 8).**
      **recall@8 = 40%** — we hand the model 8 chunks and the answering passage is absent **60% of the
      time**. Every "the model hallucinated" report needs re-reading in that light: often it never had
      the answer. After reranking from a 256 pool the mice gold lands at rank 13; a top-8 cut drops it.

- **12. The reranker's silent fallback is a CORRECTNESS bug.**
      On timeout it quietly returns embedding order, so quality degrades invisibly exactly when the
      box is loaded. **Make it visible.** (Graceful degradation nobody can observe is a bug with good
      PR.)

- **13. Query normalization — cheapest big win on the board.**
      Stripping conversational filler moved the gold passage **rank 30 → 3**:

      | query | gold rank |
      |---|---|
      | `какие виды мышей ты знаешь` | 30 |
      | `виды мышей` | **3** |
      | `виды мышей мышь` | **2** → 1 after rerank |
      | `виды мышей семейство мышиные Muridae` *(qwen's own rewrite)* | 8 → **25** |

      Note the last row: **the model's query rewriting makes retrieval strictly worse**, and it then
      fabricates to justify what came back. Query rewriting by the weak model is not a neutral act.

- **14. Pin the output language.** `ask_corpus` answered a Russian question half in **Chinese**
      (*"…включает около 900 видов，其中包括一些能够飞行的动物…"*) — same root as the garbled `ВыRIGHT`
      token. Our synthesis prompt never says what language to answer in. Applies to **any** model in a
      bilingual corpus: Claude leaked Russian into English prose the same evening.

- **15. `search_corpus` — raw chunks, no synthesis.** `ask_corpus` runs synthesis through
      **qwen**: lossy compression by the weakest component in the pipeline. Asked about corpus
      cleaning it offered, as "evidence", **a Python function it had invented**. Reading the raw chunks
      instead yielded the two facts that mattered, verbatim from Jurafsky & Martin.
      **Never put a weak model between a strong model and the source.**

---

## C. Corpus hygiene  *(reference — the checklist is §G)*

- ✅ Page markers `[[p.N]]` carried through pdftotext **and** OCR
- ✅ Question-stripping, **opt-in per corpus** (`clean-corpus.py` + `books.toml`)
- ✅ English PDFs via **DeepDoc** — real page+bbox positions **and** extracted figures
- ✅ Ghostscript downsampling under RAGFlow's silent **128 MB parser cap** (455 MB → 56 MB, page
      count preserved 1:1)
- ✅ Byte-capped upload batches (1.3 GB in one multipart → HTTP 413)

- ✅ **16. LLM-as-a-judge replaces the rule — BUILT & VALIDATED (2026-07-14).**
      `chunk_judge.py` + `clean-chunks.py --judge`, prompt adapted from MT-Bench via Lambert's RLHF
      §5.7 (in our own corpus). **Validated 7/7 on `tests/corpus-filter` (`tests/test-judge.py`)** —
      including the two cases rules could never get right: it CATCHES multiple-choice review
      questions and KEEPS the jsonb operator table.

      **The cascade** (same shape SLP3 gives for retrieval): a cheap recall-oriented rule flags
      candidates → the judge decides. Measured **1.7% of chunks are candidates → 26 min instead of
      26 h**. Rules cannot score 283k chunks *well*; qwen cannot score them *fast*.

      Safety: on any judge error → verdict CONTENT (a failed judge must never become a silent
      deleter — cf. the reranker's invisible fallback). "If unclear → CONTENT." Every verdict is
      written to a JSONL **audit trail**: swapping a rule we can inspect for a model we cannot would
      be a bad trade.

      **Finding that reframes the whole item:** the rule was blind to **Russian** multiple-choice too
      (`A11. Корнеплод — это... 1) ... 2) ...` — no question mark anywhere). I had labelled the gap as
      an *English* problem. It never was. **"Ends with ?" is simply the wrong definition of a
      question** — the same mistake as "`?` means question" (it's a jsonb operator) and "`мышей` ≠
      `мышь`". Three times in one day my rules encoded the SURFACE FORM instead of the thing.
      ⇒ Fix the `gap_en_*` fixture label; add a `gap_ru_multiple_choice` fixture.

- ✅ **17. Per-page boilerplate — handled by `clean-chunks.py`.** Lambert's RLHF book repeats
      *"Licensed to Iliia Khaprov"* on all 310 pages, riding along inside nearly every retrieved
      chunk (low-IDF, so it barely moves ranking — but it is pure context occupation, Axiom 1).
      Detected **statistically, not by pattern**: a short line present in >60% of a document's chunks
      is furniture. That rule cannot misfire on prose — a real sentence does not appear on 90% of a
      book's pages. Stripped via PATCH; the chunk survives.

- ✅ **18. 22,406 GARBLED DUPLICATE CHUNKS DELETED (2026-07-14).** All 7 Postgres Pro Russian books
      were ingested **twice**: as clean pdftotext `.txt` in `postgres`, AND as DeepDoc-parsed PDFs in
      `books` — where DeepDoc had **stripped the spaces** (`Вкнигерассматриваетсявнутреннееустройство`).
      **29% of the `books` KB** was word-boundary-less garbage: unmatchable lexically, yet still
      carrying embeddings that competed for top-k slots against the clean copies of themselves.
      Near-duplicate crowding in its most stupid possible form. Deleted; PDFs removed from
      `corpus/books_raw/` (originals safe in `~/Documents/Books/`).

- **21. TRAIN A CLASSIFIER — distil the judge into a reward model. (His idea, 2026-07-14: "maybe
      teach a small model on it instead of rules?" I built the LLM judge instead; the literature says
      he was right.)**

      **Evidence from Lambert, *RLHF* §5.7 (our corpus):** *"generative reward models … on RM
      evaluations, they tend to be behind existing RMs, showing that reward modeling is an important
      technique."* **LLM-as-a-judge UNDERPERFORMS a trained reward model.** We chose the judge only
      because it needed no training data. **That constraint is now gone.**

      **The audit trail IS the training set.** `verdicts-*.jsonl` already holds 1,000+ chunks labelled
      by qwen, with reasoning, in two languages. The judge stops being the *filter* and becomes the
      *labeller*.

      A distilled classifier wins on every axis:
      - **Deterministic.** The judge is NOT stable even at temperature 0 (dry run said 225, the real
        run deleted 221 — ollama's batching is non-deterministic). §5.7's tip ("use temperature 0")
        reduces variance; it does not remove it.
      - **Fast enough to score EVERY chunk** — not just the 1.7% the cheap pre-filter flags. **This
        eliminates the pre-filter's blind spots entirely**, which is the failure that has bitten us
        repeatedly (the rule cannot see multiple-choice, so the judge never gets shown it).
      - **Probably more accurate**, per the passage above.
      - Cheap on CPU — the same class of model as the cross-encoder we already run.

      **TARGET ARCHITECTURE (his call, 2026-07-14): KEEP THE JUDGE — but demote it to LAST-RESORT
      TIE-BREAKER.**

      ```
        every chunk ──► CLASSIFIER (CPU, deterministic, scores ALL 283k)
                          ├── confident EXERCISE  ──► delete
                          ├── confident CONTENT   ──► keep
                          └── UNCERTAIN BAND      ──► LLM judge decides   ◄── the judge lives HERE
      ```

      Why this is better than either alone:
      - **The rule disappears.** No hand-written pre-filter ⇒ **no blind spot** ⇒ nothing is silently
        never-looked-at. That single change kills the failure mode that has recurred all week.
      - **Determinism where it is cheap, judgment where it is needed.** The classifier is stable and
        exhaustive; the judge is expensive and slightly unstable — so spend it only on the cases that
        are genuinely ambiguous, where its variance costs least and its reasoning is worth most.
      - It is the SAME cascade as retrieval (cheap-and-exhaustive → expensive-and-smart), and the same
        cascade as the current rule→judge — just with the weak first stage replaced by one that can
        actually see everything.
      - Calibrate the uncertain band against `tests/corpus-filter`; widen it until the judge is only
        being asked the questions we would want a human to look at.

      Validate against `tests/corpus-filter` (and grow that fixture set) exactly as the judge was.

- **22. RAG-RewardBench** — a reward-model benchmark **specifically for RAG** (Lambert §5.8,
      ref [30]; alongside M-RewardBench for multilingual, RewardBench2, RM-Bench). Read it before
      building item 21: it is the closest published evaluation to what we are actually doing, and it
      would be daft to invent our own metric without looking at theirs first. Also note **ReWordBench**
      (typos/noise) — directly relevant, since half our corpus is OCR'd.

- **19. CONSOLIDATE THE TWO FILTERS INTO ONE.** We now have question-stripping in *two* places:
      `clean-corpus.py` (text path, weak rule) and `clean-chunks.py --judge` (chunk level, both
      paths, validated). That is exactly the duplicate-path smell. **Retire question-stripping from
      `clean-corpus.py`**; leave it owning only **page-range exclusion** (which must happen before
      parsing). One definition of "exercise material", one place, judged by a model.

- **20. Run the judge over the whole corpus** (`bio`, `bio-books`, `books`, `postgres`) once the
      dry-run totals are reviewed. ~26 min. Re-measure `eval-retrieval.py` afterwards — the whole
      point is whether removing question-shaped chunks lifts **recall@8**.

---

## D. Instruments — built 2026-07-13, USE THEM

Before these existed we could not distinguish a retrieval failure from a model failure, and that
ambiguity produced three confident wrong diagnoses in one evening.

- ✅ **`EVAL.md`** — 4 suites (PG/LSN · serenedb · auto_ptr/C++ · biology-in-Russian), each a real
      **conversation**, each with the expected answer written down BEFORE the run. Grounding decay
      only shows on turn 2+, so single-shot prompts would score well and tell us nothing.
- ✅ **`qrels.toml` + `eval-retrieval.py`** — **recall@k of the first stage.** The first stage sets
      the ceiling: rerank can only reorder what search already found. Includes a true-negative (no
      passage in the corpus explains what tails are *for*) where abstention is the only right answer.
- ✅ **`tests/test-corpus-filter.py`** — 7 fixtures of real book text. `keep_*` must never be touched
      (a false positive silently deletes knowledge — it ate the WAL chapter once), `drop_*` must be
      stripped, `gap_*` asserts a KNOWN limitation so fixing it cannot happen silently.

- **Re-run all four EVAL suites** once §A lands — before *and* after, transcripts compared.
- **Re-run `eval-retrieval.py`** after 10–13; target **recall@8 ≥ 80%** (today: 40%).

**Which instrument judges which section:** §B is judged by `eval-retrieval.py` (recall@k). §A is
judged by the four `EVAL.md` conversations. §C feeds both. Do not land §B and §A changes together —
you will not be able to attribute either.

---


# F. LOG — what was done, and what it measured

Newest last. Record the approach AND the result, including failures — the failures were the most
useful part of 2026-07-13.

### 2026-07-13 — the mice investigation (`FINDINGS.md`)
- **Three wrong diagnoses, all nearly shipped:** "corpus lacks it" (it didn't); "add an abstention
  floor at 0.42" (would have refused answerable questions); "BM25 is dead" (I had passed a parameter
  name that does not exist and diagnosed a system bug from my own typo).
- **Root cause was never retrieval.** `ask_corpus` honestly abstained; **qwen fabricated on top of the
  abstention** — invented "Muridae" in its own query, retrieved a passage labelled *Отряд Грызуны*
  (order Rodentia), relabelled it to match its invented premise, and cited it.
- **Built the instruments that were missing** (§D). Without them we could not tell a retrieval failure
  from a model failure — which is precisely how three wrong diagnoses survived.

### 2026-07-13 — Cyrillic stemmer (item 9) ✅
- **Approach:** Snowball over Cyrillic tokens in `rag_tokenizer.py`, applied to indexer *and* query
  builder (one `tokenize()` serves both — symmetric by construction). Verified `мыш` ∌ `мышц`.
- **Result:** gold passage rank **101 → 32**. Real, but *partial* — it did not put the passage in the
  top 8. Required re-parsing all Cyrillic docs.
- **Lesson:** IDF was working perfectly; the high-IDF term simply matched **nothing**. *Term weighting
  and stemming are two halves of one idea and I had shipped one.*

### 2026-07-14 — corpus filtering: rules → LLM judge (items 16, 17) ✅
- **Approach:** filter at the **chunk** level (`clean-chunks.py`), where both parsers converge — the
  text-level filter had left every DeepDoc PDF completely unfiltered. Judgment by **qwen**
  (`chunk_judge.py`), prompt adapted from MT-Bench via Lambert RLHF §5.7. Cascade: a cheap
  recall-oriented rule flags candidates → the judge decides.
- **Result:** judge **7/7** on labelled fixtures. Candidates are **1.7%** of chunks ⇒ **26 min, not
  26 h**. On `bio`: 225 EXERCISE / 76 CONTENT out of 301 candidates — **the judge rescued 25% of what
  the rule flagged**, and found **57 exercise chunks in bogdanova that the text-level rule had already
  walked past**.
- **Lesson (the day's theme, three times over):** my rules kept encoding the **surface form** instead
  of the thing. `?` is not a question (it is a jsonb operator). A question need not contain `?`
  (`A11. Корнеплод — это... 1) ... 2) ...`). And `мышей` is not `мышь` unless something makes them one
  token. *Purpose cannot be compiled into a regex.*
- **Safety:** judge error ⇒ verdict CONTENT (a failed judge must never become a silent deleter);
  "unclear ⇒ CONTENT"; every verdict written to a JSONL audit trail.

### 2026-07-14 — E1.1/E1.3: judge applied to `bio`; the poisoning thesis FAILED its own test ⚠️
- **Approach:** deleted the 221 chunks the judge called EXERCISE (dry run had said 225 — *the judge is
  not perfectly reproducible even at temperature 0; an LLM judge has variance a rule does not*).
  Then re-measured retrieval, per E1.3.
- **RESULT — NEGATIVE, and it must be said plainly:** the mice query's gold passage moved from rank
  **32 → 31**. Deleting 221 exercise chunks did **nothing** for the case that started the entire
  investigation. **The corpus-poisoning thesis is NOT supported by this measurement.**
- **Why, in hindsight (I had already found this and half-forgot it):** the passage that beats the
  rodent list is not a quiz — it is **Рукокрылые** (bats = *летучие мыши*, "flying mice", cos 0.762
  vs the gold's 0.471). A legitimately similar passage. Removing quizzes cannot touch it. Only a
  cross-encoder over a bigger pool can — which is exactly Phase 2 (mice reaches rank **13** when the
  reranker gets a 256 pool).
- **MY MEASUREMENT FAILURE:** photosynthesis went **16 → 1**, which looks like a triumph — but it is
  **confounded**. I landed the stemmer AND the judge and only took an intermediate reading for mice.
  I wrote "do not land two changes together — you will not be able to attribute either" into the
  protocol *the same afternoon*, and then did it. **Take the intermediate measurement.**
- **What survives:** the corpus IS cleaner (221 exercise + 22,406 garbled chunks gone; boilerplate
  handled), which is defensible on context-hygiene grounds (Axiom 1) — but **it bought no measured
  retrieval win**, and claiming otherwise would be exactly the self-congratulation this log exists to
  prevent.

### 2026-07-15 — reflection / self-critique: FAILED to fix, but REVEALED the mechanism
- **Tried (his idea):** instead of constraining scope up front, let qwen list freely then reflect —
  (A) single pass with an "⚠️ Уверенность" self-flag section; (B) two-pass, a separate skeptical
  critic re-checking each item against the excerpts.
- **Neither fixed the answer.** Both still listed the full rodent order + bats as "виды мышей".
- **BUT the critic wrote the most diagnostic line of the whole investigation:**
  *"Сурок → **KEEP** — упоминается как грызун, **не является мышью**, но родствен."* — it explicitly
  stated a marmot is NOT a mouse and kept it in the list of mice anyway.
- **Re-diagnosis: NOT a knowledge ceiling.** qwen HAS the fact and articulates it on demand. What it
  lacks is the willingness to ACT on it — to delete from its own draft. Strong **KEEP-bias /
  sycophancy** (same "actively pushes back" tendency seen day 1). Reasoning present, enforcement
  absent.
- **⇒ Concrete structural fix (closed-loop, NOT a prompt workaround):** make the critic emit, per
  item, a STRUCTURED verdict + reason; then the HARNESS (code, not the model) drops any item whose
  reason says "не является"/"not a". Model does the thinking ("is this a mouse?" — it can answer);
  harness does the acting (remove it). Parked in §H — needs the reranker/pool work first, and belongs
  next to the taxonomic-rank-verification idea.

### 2026-07-15 — taxonomic-scope prompt constraint: FAILED (negative result, don't re-try)
- **Context:** after the clean re-ingest, `ask_corpus("какие виды мышей")` still mislabels the whole
  RODENT list (крыса, хомяк, суслик…) and the BATS (летучие мыши) as "виды мышей". Retrieval is fine;
  qwen conflates мышь (genus) / грызуны (order) / летучие мыши (different order). The *desired*
  behaviour is explicit abstention: "no mouse-species list; only домовая + лесная мышь in passing".
- **Tried:** added a CRITICAL constraint to the synthesis prompt — *"only include an item if an
  excerpt identifies it as a member of the EXACT category asked; do not substitute a broader/adjacent
  category (an order that contains X, or an animal merely named like X)."* Tested head-to-head.
- **Result: NO IMPROVEMENT, slightly worse.** qwen STILL listed the full rodent order as "виды мышей"
  (same chunk, same mislabel), and on "грызунов" degraded into dumping the excerpt verbatim. The
  instruction was in the prompt; the model couldn't apply it.
- **Conclusion — empirical confirmation of Axiom 2 ([[harness-not-prompt-workarounds]]):** the failure
  is a **capability ceiling** (30B can't reliably hold "mouse ⊂ rodent"), not a missing instruction.
  Piling prompt text on a model reasoning failure did nothing but add context occupation (Axiom 1).
  **NOT committed. Do not re-attempt via prompt.** The real fix, if any, is structural (verify the
  answer's taxonomic rank against the chunk's own labels, or accept that scope-boundary questions are
  outside a small grounded model's reach and mark them unanswerable).

### 2026-07-14 — "DeepDoc garbles Cyrillic" was FOLKLORE. Three upstream bugs found. 🔴
*(He asked: "why can't we extend deepdoc to handle russian?" — I had been repeating the folklore for
days without ever checking it.)*

- **The folklore was wrong.** pdfplumber extracts the Cyrillic **perfectly** — 1327 Cyrillic chars,
  **0 PUA/unmapped** on the page tested. No CID garbling. The characters were always correct.
- **Bug 1 — no space glyphs.** The PDFs encode **no spaces at all**; words are separated by
  positioning. pdfTeX writes `[(Summary)-250(of)-250(Contents)] TJ` — the `-250` **is** the space
  (in TeX, interword space is *glue*, not a character). DeepDoc only emits a space for a literal `" "`
  char, so the text welds: `Вкнигерассматривается`, `2.9•MINIMUMEDITDISTANCE33substitutions`.
  **NOT a Russian problem — he spotted that immediately.** 8 of our 16 books are affected (every
  TeX-family PDF: pdfTeX, LuaTeX, xdvipdfmx — plus one from iText). Includes **SLP3, Sutton & Barto,
  Dive into Deep Learning**. English is *rescued by an OCR fallback*, which is why nobody noticed —
  we have been OCR-ing books whose text layer was already perfect (and OCR is the ingest bottleneck:
  CPU 94%, GPU 0%).
- **Bug 2 — the OCR fallback destroys scripts it cannot spell.** `ocr.res` (the recognition alphabet)
  is **6270 CJK / 52 Latin / 6 Cyrillic**. Coverage of the extracted text: **English 99.0%, Russian
  19.8%**. So the fallback throws away a good text layer for a model that can spell one character in
  five. *This* is the real origin of "DeepDoc garbles Cyrillic".
  Guard added (**his idea — "can it detect the language first?"**, sharpened): don't ask what language
  it is, ask whether **the OCR model's own alphabet covers it**. Cheaper than language ID, no language
  list, and self-corrects if `ocr.res` is ever swapped for a multilingual model.
- **Bug 3 — the Go port made it worse.** `internal/deepdoc/parser/pdf/layout/chars_boxes.go` *does*
  implement gap-based spacing — but gated on `asciiWordPattern = ^[0-9a-zA-Z,.:;!%]+$`. Python's own
  space regex two lines from the bug **already includes Cyrillic** (`[0-9a-zA-Zа-яА-Я,.?;:!%]`); the
  Go port copied that class and **dropped the `а-яА-Я`**. And its threshold (`gap >= min(width)/2`)
  under-inserts even for English.
- **Threshold validated against `pdftotext` as ground truth** (RU 253 words / EN 442 on the sampled
  pages): ragflow's rule recovers **150/253** Russian words; ours (`0.25 × mean char width`) lands
  within **2%** on both scripts.
- **Upstream status:** `main` (51 commits ahead of our pin) still has all of it. **No issue or PR
  mentions the space bug — novel.** But **issue #12109 is OPEN and is our `chunk_token_num` bug**
  (their symptom is the mirror image: chunks too BIG, breaking a reranker's 2048 limit, via
  `paper.py`). PR drafted: `ragflow-pr-space-inference.md`.
- **Consequence for us:** with these three fixes, Russian PDFs can go through **DeepDoc** — which
  means Rogov's *PG18 Internals* could finally have **figures and page positions** instead of
  text-only. That is a G3 (can-I-check-it) win. Not done: needs a re-parse; decide after the
  in-flight one lands.

### 2026-07-14 — the `book` parser was shredding every English book (G1.0) 🔴
- **Found by:** he asked why 4 English books produced ~60k chunks while 6 Russian books produced 6.6k.
  It was not a difference of language or length — it was **two parsers disagreeing by 20×**.
- **Measured:** median chunk — `naive` **1168 chars** / `book` **47 chars**. SLP3's distribution:
  **256 of 500 chunks under 50 characters**, none over 1000. Sample chunk, in full:
  `"133 The nature of preferences10 reward functions 138"` — a table-of-contents line.
- **Root cause:** `rag/app/book.py` takes `hierarchical_merge` whenever a bullet/heading pattern is
  detected (every textbook). That function **never reads `chunk_token_num`** — it accumulates against
  a **hardcoded 218-token** limit *and only merges singleton groups*; anything the bullet detector
  groups is emitted as-is, however small. `naive_merge` — the one path that honours the setting — was
  effectively dead code for real books.
- **Impact:** ~126k of ~300k chunks are layout debris, concentrated in our BEST sources (SLP3, DDIA,
  Sutton & Barto, CLRS). A 50-char chunk's embedding is near-noise — and noise is what wins when
  everything scores ~0.35. **An independent second cause of recall@8 = 40%**: eight slots of rubble.
- **Fix:** patch `book.py` to take `naive_merge` when `chunk_token_num` is set. **Positions survive** —
  page+bbox are assigned afterwards by `tokenize_chunks(..., pdf_parser)`, which matches chunk text
  back to the layout. So we keep DeepDoc's page mapping and figures *and* get 1200-char chunks; the
  trade-off I thought we faced was imaginary.
- **A SECOND BUG on the same branch** (found only because I tested a claim I had already written into
  DESIGN.md as fact): the `naive_merge` branch **destroys the page positions**. DeepDoc's tag is
  `@@page\t...##` — a **double** at-sign — and the code splits on a **single** `@`, yielding 3 parts
  instead of 2, so the `len(pr) == 2` check fails and the tag is dropped. Split on `"@@"` and
  `naive_merge`'s `add_chunk()` re-appends it, `pdf_parser.crop()` recovers page+bbox.
  **This is WHY nobody noticed the branch was broken: it was already dead code.** No real book ever
  reached it, so both bugs sat there undisturbed.
- **VERIFIED on one book before re-parsing all 19** (`lbdl.pdf`):

  | | unpatched | patched |
  |---|---|---|
  | chunks | 637 | **66** |
  | median chars | 47 | **2302** |
  | < 50 chars | 51% | **0%** |
  | with page positions | 0/66 | **66/66** |

  So: sane chunks **and** the page mapping. The trade-off I feared (good chunks *or* a corpus browser)
  never existed.
- **Lesson (the week's theme, again):** the setting was accepted, stored, displayed by the API — and
  silently ignored by the code path that actually ran. **Silence read as success.** And I never looked,
  because chunk size is *boring*.
- **Lesson 2 — verify the claim you already wrote down.** I had asserted "positions survive" in
  DESIGN.md *before* testing it. Had I not gone back to check, I would have shipped a corpus with no
  page positions and a design doc confidently explaining why it had them.
- **Lesson 3 — a bind-mount edit is not live until you prove it.** `docker compose up -d` saw no spec
  change and did not restart the container, so the first fix was tested against the OLD code and I
  nearly concluded positions were unrecoverable. **Always `inspect.getsource()` in the container.**

### 2026-07-14 — E1.2: judge run corpus-wide (partial), + a silent bug in my own script ⚠️
- **Result (the parts that ran):** `books` **258 deleted**, `bio-books` **286 deleted**.
  - **OpenStax Microbiology 200, Biology 2e 81** — precisely the MULTIPLE-CHOICE "Review Questions"
    the rule is structurally blind to, in the books that never passed through `clean-corpus.py` at
    all. **The gap is closed in practice, not just in a fixture.**
  - Textbooks with exercises got cut (Dive into DL 105, CLRS 79, Sutton & Barto 33, SLP3 24);
    **reference manuals got ZERO** (Rust Patterns 43 judged/43 kept, Database Internals 37/37,
    Latency 14/14). The judge tells a textbook from a manual with nobody telling it which is which.
- **BUG IN `clean-chunks.py` (mine):** it fetched `documents?page_size=100` and never paginated, so
  **`postgres` (219 docs) had 119 documents silently skipped — including all 7 Postgres Pro books** —
  and the run printed "0 chunks deleted" as if that were a finding. **A cap that masquerades as a
  result** — the same failure mode as RAGFlow's 128 MB parser limit reporting `DONE, progress=1.0`
  with zero chunks. Fixed (`all_docs()` paginates).
- **Second bug:** the queued runner's wait-loop grepped a status script that can time out, read the
  empty output as "nothing is parsing", and judged **half-parsed KBs**. Rewritten to ask the API
  directly, and to treat an API error as *still busy* — never as *done*.
- **Lesson:** both bugs are the day's theme again — **silence read as success**. Re-queued; results
  pending.

### 2026-07-14 — 22,406 garbled duplicate chunks deleted (item 18) ✅
- **Found:** all 7 Postgres Pro Russian books were ingested **twice** — clean pdftotext in `postgres`,
  and DeepDoc-parsed PDFs in `books` where **the spaces had been stripped**
  (`Вкнигерассматриваетсявнутреннееустройство`). **29% of the `books` KB.**
- **Result:** deleted; PDFs removed from `corpus/books_raw/` (originals safe in `~/Documents/Books/`).
- **Lesson:** unmatchable lexically, yet still carrying embeddings that competed for top-k against the
  clean copies of themselves. Near-duplicate crowding in its most stupid possible form — and it had
  been silently degrading every PostgreSQL query in the corpus.

### 2026-07-18 — qwen-next KV prefix cache: per-slot, and how we keep sessions warm ✅/🅿️
- **Behavior:** llama-server caches each slot's KV and routes a new request to the slot whose cached
  tokens best match the prompt **prefix** (`selected slot by LCP similarity, sim_best = 0.994`). First
  turn = full PP over the whole ~90 K context (minutes); every turn after reuses the stable prefix and
  PP's only the delta → the "gets faster after the first prompt" effect. Cache is **per slot**;
  `kv_unified` only shares the KV *memory pool*, not the cached content. (Full writeup in DESIGN §2.)
- **Done:** `--parallel 1 → 2` for warm session-switching. The 0.2 tok/s collapse is from two requests
  generating **at the same instant**, not from having 2 slots — so `--parallel 2` used *sequentially*
  is free. Hard ceiling is VRAM: the shared ~256 K pool holds two ~120 K sessions but not two huge ones.
- **🅿️ Parked (post-freeze), no fork needed:** llama-server has `--slot-save-path` + `POST
  /slots/{id}?action=save|restore`. Teach **the shim** to dump the active slot's KV to a tmpfs on
  idle (keyed by session) and restore before the next session's first request → unlimited warm
  sessions in the 125 GB RAM, surviving restarts. Caveat: a dump is void once the prefix changes
  (DISCIPLINE edit, **context compaction rewriting history**, model/quant/flag change).

### 2026-07-19 — diagram-OCR garbage found in retrieval; ask_code redirect fixed 🔴/✅
- **Found (from a qwen session's grep):** the *code* grep was clean, but raw `search_corpus` on the
  ClickHouse huge-pages paper surfaced two chunks where DeepDoc flattened a **diagram into the text
  stream** — box-drawing glyphs `口□□□`, repeat runs `DDDDDD`, shredded words — **interleaved with
  legit prose in the same chunk**. A whole-chunk delete would drop the good text; the split is *inside*
  the chunk. Plan = G3.6 (remove+reingest post-pass) + G3.7 (deferred DeepDoc Strategy-3). DESIGN §4.3.
- **Fixed ✅ (commit 0e913a4):** `ask_code`'s scoped-miss redirect emitted the bare 2nd path segment
  (`orioledb-postgres`) as a project id, which `_resolve_project` then rejected — the tool contradicted
  itself and the model dead-ended. Now it emits only ids that resolve (`orioledb/orioledb-postgres`).
- **Non-issue:** `search_corpus`/`ask_corpus` return `-32602` from the **main Opus session** but work
  fine for qwen (session 74166c2a used ask_corpus post-restart; raw retrieval API returns 30 chunks).
  It's a main-session tool-call quirk, not a server break. Russian answers to English PG queries are
  EXPECTED — Rogov's PG books are the unmatched source.

---

### 2026-07-19 — power loss mid-ingest: the reboot test ran itself ⚡
- **Recovered alone:** docker stack, Ollama, ES/MySQL/MinIO/Redis, user services — and the parse queue
  (it lives in Redis, which persists), so ingestion partially resumed before anyone touched it.
- **Needed hands:** (1) qwen-next resurrected via its *enabled* unit while `backend.env` said 30b —
  21.6 GB VRAM held mid-ingest; stopped, disabled, and `oracle-backend` now switches with
  `enable --now`/`disable --now` so the choice survives reboots (commit 81eb199). (2) The 129 docs
  `RUNNING` at crash time: their in-flight page tasks were popped from Redis and lost — without action
  they'd finish with **silent page gaps**; `requeue-orphans.py` re-triggered all 129 (code=102 =
  deduped against what Redis resumed). Cost: they restart from page 1 (~3,900 page tasks re-queued).
- **Open verification:** crash-partial docs might duplicate chunks if re-parse doesn't clear the
  partial set — spot-check one when it completes; exact-content dedupe over the KB if needed.
- **Metric lesson:** +10 chunks/min "looked stalled" — but chunks are now ~2,100 chars (46× the old
  debris) and the queue tail is the OCR-heavy half of the collection. Watch pages/queue depth, not
  chunks/min. DESIGN §7.1 has the full recovery doctrine.

### 2026-07-21 — the OCR bake-off: local qwen3-vl beat the specialist stack ✅
- **Measured on the same Okulov pages:** embedded djvu layer (clean prose, math *wrong as written* —
  `а13 = (((ааа)2)2)а`), marker (perfect LaTeX, but hallucinates CJK/Georgian into Russian prose and
  DROPPED `+ Fib(n-2)` from code), **qwen3-vl:30b UD-Q4_K_XL: all three axes right** at ~10 s/page.
  Marker's failure is literally our weird-glyph junk class — ingesting it would manufacture garbage.
- **Lane shipped:** `transcribe-scans.py` (all-in-one, per-page resume, ensures its own server,
  `[[p.N]]` assembly, seeded 20-page audit export). 2,614 pages running on the GPU while DeepDoc
  owns the CPU. Ingest GATED on the blind audit (three-defense discipline).
- **Serving:** Unsloth UD quant + mmproj on the bundled `llama-server --mmproj` (:18081), fully
  GPU-resident, Unsloth Instruct samplers (presence_penalty 1.5 = OCR anti-repetition).
- **The war story** (BLOG Act 19): marker's downloader hung on a dead socket (no timeout), my
  salvage skipped a dotfile (glob), the CDN changed the model mid-resume (Franken-file caught by
  safetensors header math) → the fetch doctrine is now: resumable + stall-abort + **sha256 from the
  API**, existence checks are not integrity checks.

### 2026-07-21 — both DeepDoc fixes UPSTREAMED; #16958 MERGED ✅
- [ragflow#16958](https://github.com/infiniflow/ragflow/pull/16958) (word boundaries for non-Latin
  scripts + skip-OCR-fallback-the-recogniser-can't-serve) — **merged**.
- [ragflow#16959](https://github.com/infiniflow/ragflow/pull/16959) (honour `chunk_token_num` in
  book/paper chunkers, keep page positions; closes upstream #12109) — open.
- This is the precedent G3.7 ("return and patch DeepDoc") relies on: our container patches have a
  path upstream, so Strategy-3 can go the same route when built.

### 2026-07-21 — the duplicate ratchet: 28 copies of one man page 🔴→✅
- His catch ("ingest status says there are new items"): every idempotent ingest run had been
  re-uploading `io_uring_check_version.3.txt` — RAGFlow's internal name registry stores it as
  `name(N).txt`, our dedup checked the PLAIN name, never matched, re-uploaded: +1 duplicate per run,
  28 copies (27 parsed) quietly competing in retrieval. Same near-dup crowding class as the
  twice-ingested Russian books, in miniature — and invisible until someone read the status line.
- Fixed: `have` now registers suffix-stripped names too (ratchet can't turn); 27 copies deleted;
  verified `0 new` on a fresh pass.

### 2026-07-21 (evening) — early VL audit (8 blind pages) + SereneDB Phase 0 load started ⚙️
- **Early audit** (Кн.01/02, seeded random, image-first blind): prose ~98% verbatim, ZERO
  hallucinated scripts; 13/15 formulas exact; 3/3 real figures stubbed correctly. Errors: 1
  near-synonym substitution (безошибочным→безопасным — the predicted paraphrase class), 1 dropped
  `⁻¹` (×2 on one page), 2 hallucinated figure stubs on figure-less pages (one echoes the prompt
  placeholder), running heads on 3/8 pages, page-edge hyphen stubs dropped. Recommendation
  standing: **pass-with-cleanups** (stub + running-head strips at assembly, deterministic).
  Adjudication pending; full-sample audit when the run finishes.
- **SereneDB Phase 0** (G4.1, in the `serenedb-phase0` worktree): container up (Postgres wire,
  IPv4-only, trust-from-container / password-from-host), `serenedb-load.py` written; smoke 1,000
  chunks in 6 s; **full load COMPLETE: 243,900 chunks in 23 min (173/s), exact ES mirror**
  (books 5,531 = 5,531 verified). Indexes deliberately NOT built yet — k-means waits for the
  Russian lane per the G4.1 prediction note.
- **🔴 Load exposed: the corpus was NEVER ~365K chunks.** RAGFlow's per-doc `chunk_count` is
  STALE after re-parse — `books` metadata says 74,258 while ES holds 5,531 (the pre-fix debris
  count survived the reparse in metadata only). `ingest-status.py` sums those fields, so it
  overstates the corpus by ~120K; **real corpus = 243,900 (ES ground truth)**. Our own status
  tool reporting more than the store holds — the house failure-shape, in the house tooling.
  PROPOSED (his go pending): teach ingest-status.py to count from ES per kb_id instead.

### 2026-07-26 — browse-time capture extension + receiver; associative-reranking DESIGNED, not built ✅/📐

Built **`chrome-capture`** (MV3) + **`oracle-capture-receiver.py`** (stdlib-only, binds 127.0.0.1:8788):
- **Capture** the *live, authenticated* tab — what server-side `fetch_url` can't reach
  (login/paywall/JS) — as clean Markdown via the **same trafilatura** `fetch_url` uses, plus an
  archived print-to-PDF (DevTools `Page.printToPDF`). Lands in `corpus/inbox/captures/`, ingested into
  the `links` KB. Markdown is the retrieval source (feeding captured PDFs back through the lossy
  PDF→text path is the road this repo keeps fighting); the PDF is kept for visual reference only.
- **Explain this** (right-click a selection) → grounded answer **streamed token-by-token (SSE)** into a
  popup glued to the selection, mirroring `ask_corpus` (retrieve → gte rerank → qwen, or "corpus
  doesn't cover this"). All receiver traffic runs from the extension's background worker, because an
  https page can't `fetch('http://localhost')` (mixed content).
- **Two-layer offline buffering** (works mid-flight): the extension queues captures when the receiver
  is down; the receiver writes files + a `pending` job immediately and drains to RAGFlow when it
  returns. **Verified** here against mock RAGFlow/synth: offline path (files written, job stays
  `pending`, not `failed`), recovery (multipart upload → `done`, doc_id), and the explain SSE stream
  (`sources → delta* → done`). Icons are pure-Python-generated PNGs (no PIL in this container).
- Registered `oracle-capture` in `oracle-ctl.sh`; `corpus/` is gitignored so captures never commit.

Also **spec'd — deliberately did NOT build — associative reranking** (his ask): DESIGN §5.4, parked as
**H17**. A bounded decaying "fixed-slot" memory of what I'm currently reading, feeding a *capped,
additive* rerank stage; capture/explain full-weight, dwell fractional. Parked behind the gold-query A/B
gate: recency bias is this repo's cardinal poisoning failure wearing a mask, and weighted-RRF already
backfired once (G4.1). The capture extension is the sensor it will need; the ranking blend waits for
the number. (Protocol §5: a new idea goes to §H, not §G — the freeze is the feature.)

### 2026-07-27 — Opus label fleet hit the 10% target: 24,832/24,766 ✅ (G3.8-LABELS)
- **Approach:** resumed the parked fleet (13,307) and ran the single-writer loop to completion —
  1,001 batches, two lanes concurrent, retiring each lane agent at ~130–160k tokens and
  standing up a fresh one so no lane degraded. `--import` is the only DB writer; agents Read the
  batch + rubric and Write one JSONL file; disk-truth resume by file presence.
- **Result:** **24,832 opus labels** (target 24,766 = 10% of 247,665). Class mix 88.6% CLEAN; the
  junk taxonomy is populated across all 8 non-clean classes (DEBRIS 759 … FIGURE_GARBAGE 119).
  726 excision spans. This is now the G3.8 classifier's training set — and the low-confidence
  classes (FIGURE_GARBAGE 0.58, BOILERPLATE 0.60, OCR_DAMAGED_CODE 0.67) map the uncertain band
  where the classifier should defer to the judge.
- **The harness caught every data-quality slip without a permission prompt** — the whole point of
  the `--import` SHORT/REFUSED reporting added this run. Agents dropped `certainty` on confident
  CLEAN rows (null) and miscounted 24-for-25; validation flagged each, I bounced the owning agent to
  append the gap, re-imported (SHORT files aren't marked done, so no double-insert). One lane looped
  forever on a monster single-line chunk; `TaskStop` + reassign to a fresh lane finished it in ~2 min.
- **Lesson (the week's theme, again):** the failure modes were all *silent by default* — a missing
  field, a short file, a stuck agent that keeps re-reading. Making `--import` say "SHORT 24/25 —
  missing X" out loud is the same move as making the reranker's timeout visible (item 12): a
  degradation nobody can observe is a bug with good PR.

### 2026-07-27 — "the corpus has nothing on Kubernetes": a prompt EXAMPLE LIST read as INVENTORY 🔴→✅
- **Symptom (his catch):** qwen answered a question about Kubernetes coverage *without calling a
  tool*, asserting the corpus holds Rust/C++/etc but nothing on k8s. It holds the entire official
  Kubernetes doc tree, *Kubernetes Patterns* (O'Reilly 2023) and *Cloud Native DevOps with
  Kubernetes*; 443 labelled chunks mention kubernetes/kubectl, and `search_corpus` reranks
  pod-security passages at **0.73/0.62/0.59**. The model had not looked — it recited our own text.
- **Two stale lists, both written by us, both months out of date:**
  1. `qwen.sh` DISCIPLINE route (A): *"(Rust std, io_uring semantics, PostgreSQL concepts, Go,
     Linux, general knowledge)"* — meant as examples of question *kind*, read as library *contents*.
  2. **`ask_corpus`'s docstring** — i.e. the MCP **tool description**, injected into every model's
     context on every call: *"(Rust, io_uring, Linux, Go, PostgreSQL/OrioleDB, Emacs, git/bash/glibc
     docs + books + papers)"*.
- **Fix — subtraction, per A.1 ("removing prompt bias beats adding prompt rules"):** both lists
  deleted. DISCIPLINE now states that corpus contents are an EMPIRICAL question — never claim a
  topic is absent, query it, and only "ask_corpus returned nothing for X" licenses saying so, phrased
  as a query result rather than a known fact. Tool description now says the corpus is large, general,
  unlisted and *changing*. `shellcheck`/`shfmt`/`bash -n` clean; `oracle-ask-bridge` restarted so the
  new description is actually published (descriptions are read at server start — editing the file is
  not enough). Verified `~/bin/qwen` and `~/bin/qwen-next` both resolve to the fixed `qwen.sh`.
- **The generalisable bit (DESIGN §9.0, third instance of this bug class):** we had been treating
  DISCIPLINE as "the prompt" and docstrings as documentation — but **a tool description IS prompt**,
  biases identically, and never gets audited because it doesn't live in the prompt file. Rule:
  *a prompt may describe how to use a tool; it must never describe what the data contains.*
- **Same rot found one layer up:** README claimed "Elasticsearch (243,900 chunks)" while production
  is `DOC_ENGINE=serenedb` and ES holds **247,665** — a stale factual claim about our own
  architecture, in the document that introduces it. Corrected.

### 2026-07-28 — the text-only chat can see: one GPU slot, one owner, one cache ✅ (DESIGN §6.1)
- **PR #1 merged** (`48a4da7`): the capture extension + receiver land on `main` — capture, ask,
  explain, fact-check with linked citations, region vision, the H17 recency sensor.
- **Two extension bugs, both mine, both user-caught.** (1) The region crosshair stopped appearing:
  `screenshotRegion()` pre-flighted `/status` and refused when `vision:false`. Correct until
  auto-swap landed — after it, `vision:false` means "not resident *at this instant*", so the guard
  aborted before injecting the selector and refused the very request that would have swapped the
  model in. (2) The prompt toolbar clamped against a hardcoded `330px` that is not its width, so
  near the right edge the arithmetic said "fits" while Send was off-screen. It now appears centred
  under the cursor, measured, flipping above when there is no room below.
- **Three new ways in for vision:** right-click an **image** (its `alt`/`title`/`<figcaption>` go in
  first, marked authoritative — they say what the picture *means*, which pixels cannot), **explain
  this page** (whole viewport: text summarised first, then the screenshot), and an image pasted into
  a **local Claude Code chat**.
- **His catch: the region route never summarised while the image route did.** Same page, different
  behaviour, no visible reason. Cause: the region route derived `page_text` from the located-text
  walk, whose dedup and 400-node cap compress a dashboard below the receiver's 2500-char summarise
  threshold. The walk's job is *location* (which panel did you crop); whole-page context is
  `innerText`. One extractor now, so the extractor no longer decides whether the page gets
  summarised.
- **The shim's `[image omitted — model is text-only]` was the real hole.** Every pasted screenshot
  and every `Read` of a `.png` was discarded there, and the model answered anyway from the filename.
  The box is not text-only — it just cannot be both at once. `oracle-claude-shim.py` now swaps to
  qwen3-vl, reads the image, swaps back, and injects the reading. **Measured live:** 296 s
  end-to-end on the Grafana screenshot; the text model quoted 17.6.0 / 4.19 GB / 40.9 MB — values
  present only in the pixels — *and* volunteered that it had not seen the image, because the
  injected block is labelled as another model's reading rather than as the image itself.
- **`oracle_vram.py` — one owner for the 24 GB slot,** shared by the receiver and the shim, with an
  `flock` across processes. Two services each holding a private in-process lock is not a lock: one
  stops the unit the other just started, the swap reports success, and the box ends up with no model
  — this repo's signature failure shape in a new place. `oracle_vision.py` holds the read cache,
  content-addressed by `sha256(bytes)`: **not an optimisation** — Claude Code re-sends the whole
  transcript every turn, so uncached this would swap the GPU twice per turn forever to re-read an
  unchanged picture. Only the newest turn may trigger a swap; older images say they were not read.
- `test-shim-vision.py` covers the plan (what triggers a read, what comes from cache, what is left
  alone, what the model finally sees) with the GPU stubbed — the expensive part is exactly the part
  you cannot iterate on.

### 2026-07-28 (later) — prompt prefix shared and cached: 2,533 tokens/request ✅ (H16, DESIGN §6.2)
- **Nothing shared a prefix** because each feature's system message WAS its task instruction —
  explain, fact-check and chat differed at token zero, so the site pack was re-processed every
  request at 300-500 tok/s. Restructured: preamble + site pack in the system message, task
  instruction moved to the front of the user message. The three-source discipline moved into the
  preamble, where every feature needed it anyway (chat had been carrying a private copy).
- **Measured** (`measure-prefix-cache.py`): identical request → 4 tokens processed of 11,860 (33s →
  15s); same host, new question → 2,535 reused; different host → 199. **My first measurement was
  wrong** and proved nothing: it compared tokens PROCESSED between requests, but a request that
  processes fewer tokens may simply have had a smaller prompt. The number is `total − processed`,
  with total from the server's own `/tokenize`.
- **🔴 NEGATIVE RESULT — `--slot-save-path` does not work here.** Dumps are ~79 MB + 13 KB/token and
  save in ~30 ms, and restore reports `n_restored=6033` — after which the next request reuses
  **zero**. Pinning `id_slot` made it worse. An earlier probe seemed to show 2,538 reused; that was
  a slot left warm by the previous run, which is exactly why the controlled sequence (restart →
  restore → ask, nothing between) is the only evidence that counts. The unit flag was reverted
  rather than left as dead config.
- **What ships instead:** keep the ~11 KB of prefix TEXT and replay it as a one-token request after
  a swap (`oracle_kv.warm_all()`, called from `oracle_vram` once text is resident). Control: ask
  after a restart → processed 2,553, reused 0. Warmed → processed **15**, reused **2,538**. Costs
  ~6 s of GPU in the background instead of inside the user's next question.
- **The fragility is deliberate and tested** (`test-prefix.py`): editing the preamble invalidates
  every warm slot, editing a pack invalidates that host's, and anything request-specific leaking
  into the preamble moves the boundary to token zero — which breaks caching **while still producing
  correct answers**, visible only as latency.

### 2026-07-28 (evening) — per-host chat, site packs, and four self-inflicted regressions 🔴→✅
- **Per-host chat** (`oracle_chat.py`, `chrome-capture/chat.js`, `POST /chat`): one continued
  conversation per HOST, append-only with epochs, transcript on the receiver so it survives the tab
  and a restart. Selection and region can be sent into it; a region is read by qwen3-vl and the
  **reading** enters the transcript, not the pixels. Verified live: turn 2 answered "what did I just
  ask you about?" from turn 1.
- **Per-domain context** (`oracle_sitectx.py`): curated packs for hosts we own (stroppy.io + every
  subdomain — vocabulary, the PostgreSQL/OrioleDB engine notes taken verbatim from stroppy-mcp, and
  a "how to answer about this site" section), otherwise the site's own `/AGENTS.md` fetched by the
  extension. Untrusted files are fenced, non-citable and explicitly never instructions; curated
  packs may instruct, because they are ours.
- **Debug tab**: a checkbox turns on `debug` SSE events from BOTH sides — what the extension
  captured and sent, what the receiver composed, section by section, verbatim. A SKIPPED step
  reports itself with the reason, because a debug view that only shows what happened reads as "the
  other thing never happens".
- **Full-page screenshots**: scroll + stitch, capped (6 slices / 12000 CSS px / 9000 device px),
  sticky chrome hidden after the first slice, scroll restored, and labelled to the model as
  *stitched* — content far apart in that image was never on screen together.
- **🔴 Four regressions, three of them mine, all found by him testing:**
  1. the shared preamble lumped curated packs in with untrusted AGENTS.md → every stroppy question
     answered "The corpus doesn't cover this" while the material that covered it sat in the prompt;
  2. an empty retrieval short-circuited before the model ran, so a pack could never rescue it;
  3. the page-context block ended with "say so even when this page appears to" — written pre-packs,
     now sitting above "Excerpts: NONE" as an order to refuse;
  4. no timeout on the `/AGENTS.md` fetch → any unresponsive host hung every request *before the
     receiver was contacted* ("stuck on Consulting the corpus…"). 2.5 s deadline now.
  Plus: a dead SSE stream (my ~15 receiver restarts during his testing) left the card frozen on a
  half-answer, indistinguishable from thinking. Streams that end without done/error now say so.
- **Vision prompt iterated against a REAL screenshot** (his stroppy-cloud run page). Root cause of
  "it explains Grafana to me": the vision path had **no system message at all**, so the pack's
  answering rules read as background prose. Three further fixes, each from an actual error: `pool=400`
  called "400 VUs"; "well below the peak of 50K ops/s" (that is the axis maximum, not capacity);
  PostgreSQL `checkpoint_timeout` advice for an OrioleDB run. Final answer opens with the run
  identity, flags the 11x p50→p99 spread, and verifies the transaction mix against the TPC-C spec.

### 2026-07-28 (night) — the harness works: it clicks through tabs by itself ✅ (G6, DESIGN §6.3/6.4)
- **Built and observed working.** Asked to explain a run, it clicked `METRICS`, clicked `GRAFANA`,
  guessed a selector that did not exist, was told so, and recovered with a screenshot. The
  before/after URLs in the tool results prove the navigation instead of the model asserting it.
- **Read/act split, gated per host.** Acting tools are ABSENT from the tool list when a host is not
  enabled — a tool the model cannot see cannot be mis-called, and asking a model to be careful is
  the workaround this repo refuses to write. The page it was built against has Delete, Rerun and
  New Run within a few hundred pixels of each other.
- **The extension drives the loop.** The receiver has no DOM, so a turn ends with a `tool_request`
  and the extension re-enters by posting the result. Every tool reports its OUTCOME: a click returns
  the page's url/title/text ~900 ms later, a miss returns the clickable labels that DO exist, a
  `read_page` miss says the selector was a guess and returns the whole page.
- **Sessions per host** (`main` = the panel, `quick` = explain/fact-check/regions), append-only with
  epochs, listed per host in the panel and globally in the popup. `main` keeps the legacy filename,
  so nothing recorded before sessions moved.
- **`oracle_broker`**: batches GPU work by model, vision first when both wait, bounded so text
  cannot starve it. Tests assert the ORDER, because order is the feature.
- **RAM tier**: keep the evicted model's file in page cache after a swap (fadvise + a real read;
  fadvise alone silently warms almost nothing). Both models measured at 100% resident.
- **🔴 Four self-inflicted faults, all his catches:** (1) `node --check` PASSED on a file with two
  adjacent string literals, so a syntax error killed the service worker while my gate said fine —
  `chrome-capture/check.sh` now parses each file the way Chrome does; (2) the keepalive existed but
  only covered tool execution, not the first model call, so Chrome killed the worker mid-turn and
  the panel span for 7 minutes; (3) a stuck turn froze the session switcher, so you could not even
  go and look at the other conversation; (4) `look_at_page` photographed Oracle's own panel, so the
  model read its own last answer as part of the page.
- **Two honesty fixes worth keeping in mind:** the swap progress line claimed "reading weights from
  disk" unconditionally (it now measures — both models are page-cached, and the 20-30 s is the
  `--no-mmap` copy plus the PCIe push, which RAM cannot remove); and the panel said "consulting the
  corpus…" before the model had decided to do anything of the kind.
- **Still open:** a `p95` in a vision reading of a dashboard whose legend shows p50/p90/p99 —
  probably a relabelled p99, unverified, and it is the number feeding the tail-latency conclusion.

### G6 — THE CHAT AS A HARNESS: tools, not just context ✅ (DESIGN §6.3)
- **The prompt that exposes the ceiling (his):** *"what do you think about this page?"* goes to
  corpus retrieval, because retrieval is the only thing a turn knows how to do — and the corpus has
  nothing to say about a page it has never seen. The honest answer requires LOOKING, so the model
  needs to act, not just be fed.
- **Tools:** `look_at_page` (screenshot → qwen3-vl → reading as the tool result), `read_page`,
  `search_corpus` (what every turn does unconditionally today, made explicit and optional),
  then `click` / `type`.
- **The extension drives the loop.** The receiver cannot execute a browser action, only ask for one:
  a turn ends with a `tool_request`, the extension performs it and posts the result back as the next
  turn. Same closed-loop rule as Axiom 2 — the model decides "move the right hand", the harness must
  actually move it, verified.
- **Acting is not retrieval.** `click`/`type` run inside a logged-in session where a wrong action is
  a wrong DEED, not a wrong answer — a deleted run, a triggered rerun, a submitted form. Reading
  tools can be automatic; acting tools need a gate, and the gate belongs in the harness, not in a
  prompt asking the model to be careful.
- **Retire retrieval-by-default:** retrieve when the question is about the corpus, look when it is
  about the page. The model is what knows which.

### G6.1 — SITE TOOLS: the website's own API as a tool surface ✅ (DESIGN §6.3.1–6.3.3, 2026-07-29)

His framing: *"we are free to look at panel code and instruct the model to use javascript and
internal routings"*, and later *"site-pack does all the dirty tricks here while we work with my
local qwen"* — the panel will host the chat itself once the model moves to Claude, so per-site
scripting is the product, not overspecialisation.

- **`site-packs/<domain>.js`** — named functions, run in the page's MAIN world (where the app's
  credentials and client live), called BY NAME. The model never writes JavaScript. Source ships with
  each tool request from the receiver, so editing a helper needs no extension reload.
- **Generated allowlist** — `scripts/gen-readonly-api.py`: `NO_SIDE_EFFECTS` ∩ GET ∩ exists-in-
  generated-Connect-code. 60 of 143 methods; `StartTestRun`/`DeleteTestRun`/`OpenShell` excluded.
  Enforced on the RECEIVER, before the call leaves. A standing test asserts every procedure the
  helper source calls is on the list.
- **PromQL** — `/public/metrics/` scopes every query server-side to one run. The model controls the
  question, never the scope. This is the fix for numbers read off pixels (2.36K/841/1.8K tx/s for
  one run; a "p95" read off a legend showing p50/p90/p99).
- **Safe click (his idea)** — per-host acting is now `off | confirm | allow`. Under confirm the model
  picks the element, the page outlines it, the human presses Enter; declining is reported as a
  result, not an error.
- **Harness bugs this surfaced**, each of which had produced a wrong answer that looked right:
  `type_text` wrote `el.value`, which React's value tracker swallows — the field showed the text,
  React state stayed empty, Start did nothing, and the tool reported success by reading back the
  property it had just assigned; a disabled button accepted a silent click; a repeat-guard blocked
  a legitimate navigate-back; a 20,000-char cut landed mid-JSON with no marker; context caps were
  set at ~16% of a 131,072-token slot; injected functions cannot see module scope (now checked by
  `chrome-capture/check-injected.mjs`, wired into `check.sh`).

### G8 — arXiv: mirror -> corpus, and two silent-corruption bugs (2026-07-29)

The mirror at `/mnt/data/arxiv` (separate long-running sync, ~230k of a ~1.1M target, 3.1M papers
catalogued in `state.db`) now feeds the corpus. `arxiv-select.py` picks a slice and extracts text;
`arxiv-tail.sh` + `oracle-arxiv-ingest.service` follow the download continuously.

**`naive` over pdftotext, not DeepDoc over the PDFs.** Measured here: DeepDoc is ~415s per 13-page
range (table analysis ~47s, OCR ~31s), so the 8,273 systems-category papers already downloaded would
be six to eleven WEEKS of CPU. Via pdftotext it is ~0.11s each — 50 papers in 5.3s. arXiv PDFs are
born-digital, so OCR and table-structure recognition solve a problem these files do not have.

**Chunk volume is the real constraint, not CPU.** Measured 52 chunks/paper: the systems slice alone
would be 2.8M chunks against a corpus of 424k. Selection is mandatory and the script says so.

- [x] **The delimiter that ate every letter `n`.** Chunks stored as `i creasi gly i volves i
      formatio `. Creating a dataset through RAGFlow's API without an explicit delimiter stores the
      default double-escaped (`\\n`), which `unicode_escape` turns into backslash + a literal `n`;
      the txt parser splits on each character of that set and consumes it. Parsing reported success
      throughout. Eight KBs were affected (arxiv, bio, books, collection, keep-books, ml, papers,
      tpc) — invisible on the PDF-parsed ones and on the Cyrillic one, which has no Latin `n` to
      lose. Fixed by setting `delimiter` explicitly in ingest-corpus.py + repairing every KB AND
      DOCUMENT row. Upstream bug, reproduced on `main`, written up in
      `~/Projects/ragflow/stories/delimiter-eats-the-letter-n.md`.

      **`parser_config` is copied forward THREE times** — knowledgebase → document (at upload) →
      task (at queue time). The parser reads the last copy. Fixing the KB row alone changed nothing
      and the re-parse produced byte-identical garbage; 16,477 document rows needed correcting too.
      Remember this for any parser_config change, not only this bug.

      Two MySQL traps while repairing: `JSON_SET(..., CHAR(10))` stores a BINARY value that reads
      back as `base64:type15:Cg==` — worse than the original; use `CONVERT(0x0A USING utf8mb4)`.
      And storing the literal 2-char `\n` means fighting backslash escaping through MySQL + JSON +
      whatever reads it back, where every layer adds or removes one. A real newline sidesteps the
      argument: it is not an escape sequence, so `unicode_escape` passes it through unchanged.
      Verify by decoding exactly as the parser does, never by reading `JSON_EXTRACT` output (it
      re-escapes and misleads).

      **There IS a cancel API** (his correction, and he was right):
      `POST /api/v1/datasets/{id}/documents/stop` with `{"document_ids": [...]}`. A queued task
      holds a config snapshot and cannot be corrected in place, so cancelling the 515 in-flight
      tasks beat waiting an hour for them to finish writing chunks already known to be wrong.
      Re-queueing then deletes each document's prior chunks (`filter_delete` +
      `docStoreConn.delete(pre_chunk_ids)`) — verified: arXiv's chunk count in SereneDB dropped to
      0 before rebuilding, so corrupt rows are replaced rather than accumulated.
- [x] **NUL from pdftotext**, 3.5% of papers (51 of 1,446; median 724 NULs per affected file, none
      under 10 — systematically broken CID font maps, not stray glyphs). Extrapolates to ~38k over
      the mirror. Per G4.4 policy these are KEPT and allowed to fail visibly rather than dropped:
      a failure that vanishes is not a decision, it is amnesia. Ids accumulate in
      `corpus/arxiv-needs-ocr.txt` for a deliberate OCR pass.
- [ ] Run DeepDoc OCR over the `arxiv-needs-ocr.txt` queue.
- [ ] Decide how wide to go. cs.DB alone is ~72k chunks; the systems slice is ~430k, which would
      double the corpus. Measure retrieval before widening.

### G7 — TPC specifications ingested (2026-07-29)

`fetch-tpc-specs.sh` → `~/Documents/Books/TPC` → `corpus/tpc_raw` → KB `tpc` (book parser). 13
specs: the 12 current ones plus TPC-B, because **pgbench is a TPC-B implementation** (his call) and
is therefore the reference for a workload still run daily. The index page has a second, obsolete
table whose marker is a section HEADING — grepping the page for `.pdf` silently returns both halves,
so the split is positional. Governance documents (bylaws, CLA, membership, policies, procedures,
fair use) excluded: they answer no technical question and only dilute retrieval.

# G. THE WORK (the only checklist)

Test for inclusion: **does this make a grounded answer more trustworthy — or make an untrustworthy
one visible?**

Everything below is one of: *can it find the answer* (G1), *can it use its tools* (G2), *can I check
it, and will it be there* (G3).

### G1 — Can it FIND the answer?  *(the single biggest defect in the system)*
**recall@8 = 40%.** The model is handed 8 chunks and the answering passage is missing **60% of the
time**. With no network to fall back on, that is fatal — and worse, it is *invisible*: it reads as
"the model hallucinated", so you blame the model and never look upstream. We did, for weeks.

- [x] **G1.0**  ✅ book.py fixed + re-parsed (median 47→~2400, positions 100%) — **THE `book` PARSER IGNORES `chunk_token_num`. Median chunk = 47 chars.** (Found
      2026-07-14.) Upstream takes `hierarchical_merge` for any document with headings — every textbook
      — and that path never reads `chunk_token_num`: it accumulates against a **hardcoded 218-token**
      limit and only merges *singleton* groups. DeepDoc calls TOC lines and running heads "sections",
      so each becomes its own chunk.
      **Half of SLP3's chunks are under 50 characters.** ~126k of ~300k chunks in the corpus are
      layout debris (page numbers, headers), and they are in `books` — SLP3, DDIA, Sutton & Barto,
      CLRS, Database Internals. **A top-8 retrieval hands the model ~500 chars of rubble.** This is an
      INDEPENDENT SECOND CAUSE of recall@8 = 40%, and it degrades every English book we own.
      *Patched* (`rag/app/book.py`, bind-mounted): take `naive_merge` when `chunk_token_num` is set.
      Positions are NOT lost — page+bbox are assigned later by `tokenize_chunks(..., pdf_parser)`, so
      we keep DeepDoc's figures and page mapping AND get sane chunks. **Requires re-parsing `books`
      and `bio-books`.** Verify on one book before re-parsing all 19.

- [x] **G1.1**  ✅ already visible — [reranked] vs [embedding-order (reranker busy)] tag = item **12** — make the reranker's silent timeout fallback **visible**. Prerequisite:
      without it, G1.3 degrades quality invisibly under load.
- [ ] **G1.2** — parallelise the reranker across the 24 idle cores. Unblocks G1.3.
- [ ] **G1.3** = item **10** — pool 64 → 256. **recall@64 = 80% → recall@256 = 100%.**
- [x] **G1.4**  ✅ slice widened main 8→18 (the recall-64 bump) = item **11** — widen the final slice past 8.
- [x] **G1.5**  ✅ query normalization shipped (strips filler; 'какие…ты знаешь'→'виды грызунов') = item **13** — query normalization. Cheapest win on the board (gold rank 30 → 3).
- [x] **G1.6**  ✅ re-measured: recall@8 40→60%, recall@64 80→100% — re-measure. **Target: recall@8 ≥ 80%.**

### G2 — Can it USE its tools?
Every wasted tool call costs four more calls and a defocused context (Axiom 1) — and once, a
wrong-repo result became **fabricated `WAL_REC_*` codes**. A tool that dead-ends instead of
redirecting doesn't just cost time; it produces a confident wrong answer.

- [ ] **G2.1** — run the four `EVAL.md` suites BEFORE any change; keep the transcripts.
- [x] **G2.2** = items **1, 2, 6, 7, 8** — the closed-loop fixes. All one bug: *the harness knows the
      right answer and returns an error instead of saying it.*
- [x] **G2.3** = items **3, 4** — rerank grep hits; compact the code-graph blobs. Context hygiene.
- [x] **G2.4** = item **5** — de-scope the DISCIPLINE (it refuses non-coding questions).
- [ ] **G2.5** — re-run the four suites. Compare transcripts, **counting tool calls per turn**.

### G3 — Can I CHECK it, and will it be there?
- [x] **G3.1**  ✅ corpus-wide judge run finished (2026-07-16) — ~1,687 apparatus/exercise chunks
      dropped across 17 KBs, book KBs came back **0** (already clean = no false positives).
- [x] **G3.2**  ✅ output language pinned in synthesis prompt (stops Chinese leak) = item **14** — pin the output language (half the corpus is Russian; qwen leaks Chinese).
- [x] **G3.3** = item **15** — `search_corpus` (raw chunks). *I* need this on the plane: never put the
      weak model between me and the source.
- [ ] **G3.4** — **`./oracle-ctl.sh resume` must work from cold.** Verify once, end to end. If the
      stack doesn't come up over the Atlantic, none of the rest matters.
      *Partial involuntary test 2026-07-19 (power loss):* docker stack + Ollama + user services + the
      Redis parse queue all came back on their own — but two recovery gaps surfaced (qwen-next's
      enabled-unit vs backend.env mismatch, fixed in `oracle-backend`; in-flight parse tasks lost →
      `requeue-orphans.py`). Still NOT a validation of `oracle-ctl.sh resume` itself — nobody ran it;
      the deliberate end-to-end check remains open. Details: §F 2026-07-19, DESIGN §7.1.
- [x] **G3.5** — **the corpus browser** (he called it a must-have). Offline, open the actual PDF at the
      cited page. The `[[p.N]]` markers exist for exactly this.
- [ ] **G3.6** — **diagram-OCR garbage: retrieval-side remove+reingest pass.** `clean-chunks.py` gains a
      three-way classify (ToC/index → delete; **junk** = diagram OCR mixed with prose → excise the
      garbage span, offload `(chunk_id, doc_id, cleaned_text, snippet)` to a worklist file; clean →
      keep), then a loop that DELETEs the old chunk and ADDs the cleaned text as a NEW chunk so it
      re-embeds (PATCH updates text but not the vector → stale garbage embedding). See DESIGN §4.3.
- [ ] **G3.8** — **train the CPU junk classifier** (he approved; *training time is not a constraint*).
      The deterministic detector proved SAFE (token-diff: removes only stray glyphs, never words) but
      flags 9–15% of chunks and can't tell a garbage `●` from a chart-legend `●` — precision ceiling.
      Demote it to candidate generator; verdict comes from a classifier on the CPU tier. Features:
      the **already-stored** `q_1024_vec` bge-m3 embeddings (free, in ES) + surface stats. Model: GBT/
      MLP first, small multilingual encoder fine-tune if needed (hours on CPU are fine). Labels:
      bootstrap via rules→qwen-judge weak labels→human spot-check (the §4.2 audit pattern). It is ONE
      model over the WHOLE junk taxonomy — ToC, index, glossary, exercises, bibliography, figure-OCR
      garbage, boilerplate — replacing the is_obvious_toc rule + statistical strip + recall rules + GPU
      qwen judge + glyph detector. **Output contract (his call 2026-07-22): DEMOTE BY DEFAULT** —
      classifier writes `junk_class`/`junk_score` chunk metadata to ES; apparatus classes (ToC, index,
      glossary, bibliography, exercises) get score-demoted in the ask_corpus rerank layer (per-class
      weights, config not code); ONLY irrecoverable classes (figure-OCR garbage, ocr-damaged-code)
      still delete/excise. Full book stays in RAG; navigational queries can later invert the weights.
      Eval bar: content-query recall must not regress, navigational should improve. DESIGN §4.3
      policy-change block. Feature extractor is BUILT:
      `build-junk-features.py` (read-only, `q_1024_vec` + 38 surface features → .npz; probed OK; incl. stopword/sentence-end prose-ness, alphabetized-index order, answer-key/bibliography patterns, code-ratio [the jsonb `?` scar], title-overlap, page position via page_num_int).
      Raw-collection probe (2026-07-19) validated the signals (flagged ToC = real ToC; index-ish at
      alpha-sorted 0.90 / stopwords 0.08 vs prose 0.28) and grew the taxonomy: **ocr-damaged-code** —
      numbered code lines OCR'd from PDF page images (`GridPa e`, `R0UND`, fullwidth `，`). Keep-vs-drop
      hinges on code_ratio × is_pdf × weird_density — a three-way interaction rules can't weigh; model
      territory. **Sequencing:** assemble the labeled set anytime (read-only ES scan; judge
      calls at a quiet moment — they share the 30B with coding); train/score only after the collection
      ingest drains (CPU contention). DESIGN §4.3.
- [x] **G3.8-LABELS — Opus label fleet reached the 10% target: 24,832/24,766 on 2026-07-27** ✅
      (parked at 13,307 on 2026-07-24 for the weekly limit; resumed and finished this session, 1,001
      batches, incl. 2 spillover batches past the target for a round number). The labeled set is now
      the G3.8 classifier's training corpus. Final state:
      - **Class mix (24,832):** CLEAN **21,996 (88.6%)**, DEBRIS 759, OCR_DAMAGED_CODE 630,
        EXERCISE 392, BIBLIOGRAPHY 374, INDEX 312, TOC 126, BOILERPLATE 124, FIGURE_GARBAGE 119.
        726 chunks carry excision spans. `python3 label-db.py fleet-stats` for the live histogram.
      - **Confidence:** 70% of rows ≥0.8 (10,169 at ≥0.9). Lowest-confidence classes are exactly the
        boundary cases the agents kept flagging: **FIGURE_GARBAGE 0.58, BOILERPLATE 0.60,
        OCR_DAMAGED_CODE 0.67** — two-column-merge, license-text, and prose-vs-damaged-code borders.
        These are where the classifier's uncertain band (→ judge tie-break, per G3.8) should sit.
      - **Rubric lives at `./RUBRIC.md` (repo root), NOT `corpus/labels/RUBRIC.md`** — the old path
        in earlier prompts was wrong; agents only worked because they had it internalized.
      - Agent protocol: Read+Write only, judge every chunk, ALL N rows or the importer refuses; ignore
        the batch's `nominated` hints (unreliable — mostly wrong this run).
      - **Operational learnings from the resume run (fed back into label-fleet.py / the dispatch
        prompt):**
        - `--import` now reports **SHORT** batches (agent wrote 24/25, or missed chunk_ids) and
          **REFUSED** batches (null-certainty rows) inline, so the operator never shells out an ad-hoc
          `python3 -c` to find the gap (that tripped permission prompts). Bounce the owning agent to
          *append* the missing row(s); SHORT files are NOT marked done, so re-import is safe (no
          double-insert of the good rows).
        - Two recurring agent failure modes: **dropping `certainty` on confident CLEAN rows** (reads
          as null) and **miscounting 24-for-25**. Both are caught by import validation; the dispatch
          prompt now says "explicit numeric certainty on EVERY row incl. confident CLEAN" and "count
          the chunks, output exactly N — verify before finishing."
        - Retire a lane agent at ~130–160k tokens and spawn a fresh one; past that they stall/re-read.
          One lane looped forever on a single multi-K-token line (the `clip()` head+tail export exists
          for exactly this) — `TaskStop` it and reassign the batch to a fresh lane.
- [ ] **G3.9 — RE-OCR REPAIR for OCR_DAMAGED_CODE (his call 2026-07-22: "reocr is the way").**
      Damaged code (dropped glyphs `GridPa e`, digit swaps `R0UND`/`Fib(n-l)`, fullwidth `，`,
      column-interleaving) is the one junk class that is wrong IN THE PAYLOAD — and often the only
      copy of a book's listing, so deletion wastes real value. Plan, three tiers, his pick is 3:
      1. RUBRIC v1.2 (pending his wording): mixed chunks take the DOMINANT class + mandatory spans
         over the damaged code; pure damaged-code chunks stay OCR_DAMAGED_CODE.
      2. Excise actuator: spans -> §4.3 remove+reingest (machinery exists; prose survives).
      3. **REPAIR, not delete**: the classifier finds damaged-code chunks corpus-wide; each carries
         page provenance (page_first/doc_id) -> re-render the source page -> re-transcribe with the
         VL lane (qwen3-vl now; gold-tuned VL after H12; Opus for high-value listings) -> replace
         the damaged region. **Acceptance is MECHANICAL, not judged**: the repaired snippet must
         (a) parse under the target language's grammar and (b) stay within small edit distance of
         the damaged original — the parser is the hard stop (soft-limits/hard-stops, DESIGN §9.0);
         anything failing falls back to tier-2 excision. Sequencing: AFTER the classifier trains —
         detection at corpus scale is precisely what the model is for. Note the virtuous loop: the
         Opus gold pages already give the VL lane its fine-tune set (H12), and a better VL directly
         raises tier-3 repair yield.
- [ ] **G3.7** — **RETURN AND PATCH DEEPDOC (deferred, like the word-boundary fix).** Root cause: the
      onnx layout model (`deepdoc/parser/pdf_parser.py`, `_layouts_rec`) **mislabels a diagram as
      `text`**, so its OCR is flattened inline instead of being pulled out as a `figure`
      (`_extract_table_figure`, `rag/app/{paper,book}.py:54`). The box-level garbled-text filter at
      **`pdf_parser.py` ~810** already has Strategy 1 (PUA/CID) + Strategy 2 (font-encoding) that clear
      a box → OCR fallback; add a **Strategy 3** (box-drawing/replacement-glyph density + long repeat
      runs → drop the box *before* chunk assembly), so no mixed chunk is ever formed. Change is inside
      the RAGFlow container against pinned **v0.26.4** — do it when we next touch the parser, not now.

### G4 — The SereneDB experiment (his call, 2026-07-20 — runs PARALLEL to G3.8 junk training)

Replace Elasticsearch as RAGFlow's chunk store with **SereneDB** (serenedb.com — per him the public
pitch is dated: today it leans on **DuckDB for execution + IResearch for storage** rather than the
historical RocksDB/Velox framing; Postgres wire protocol, single-node Apache 2.0,
`docker run serenedb/serenedb`). Capability map checks out on paper: `BM25()` + `@@` over inverted
indexes, IVF vector index with `metric='cosine'` + `<=>`, hybrid lexical+vector in ONE SQL query,
plain SQL for the mget/scroll/filter/delete surface. **They ship a dedicated ES-migration guide**
(docs.serenedb.com/sql/indexes/inverted/migrating-from-elasticsearch) that mechanically maps the
whole surface RAGFlow uses: bool/match → `&&`/`||`/`@@`, boosts (`^`), kNN → `<=>`, hybrid with
**native RRF** (or exact-weight replication via scorer arithmetic `BM25(...)*w`), `ts_highlight()`
for RAGFlow's chunk-highlight UI, `optimize_top_k` (WAND) for the top-64 pool, aggregations → SQL;
its stated limitations (more_like_this, phrase suggester) touch nothing we use. Two facts that
matter: (a) **the Cyrillic
stemmer survives** — RAGFlow tokenizes/stems BEFORE storage, so the store needs only
whitespace+lowercase (`case='lower', stemming=false` dictionary); (b) ES is our heaviest resident
(4.3 GB index + JVM) — real footprint prize if RocksDB+columnar is leaner.

- [ ] **G4.1 — Phase 0: side-by-side, zero RAGFlow changes.** Export all ~350K chunks from ES (scroll
      machinery exists), bulk-insert over Postgres wire, index, replicate RAGFlow's hybrid query in
      SQL. Measure vs ES: ingest time, index size/RAM, query latency, and **recall parity on the
      gold-query retrieval eval** (the instrument exists; ES sets the bar at recall@64 = 100%).
      Insider guidance (from him, 2026-07-20):
      - **Analyzer: do NOT use the `text` template** (native ICU — most capable, slowest). Our
        `content_ltks` is pre-tokenized/pre-stemmed space-separated lowercase, so the cheapest
        pipeline wins: `delimiter(' ')` (+ `norm` at most). Templates compose via `pipeline`/
        `segmentation` dictionaries (docs: create_text_search_dictionary/{pipeline,segmentation}).
      - **Vector: hierarchical IVF** (built for fast build, small memory, S3) — expect tuning to
        reach ES-recall, with knobs UNLIKE ES: **quantization at index time** (`sq8` the sane
        default; NOTE quantization applies only to `l2`/`ip` — `cosine` stays unquantized. bge-m3
        embeddings are typically L2-normalized ⇒ cosine ≡ `ip`; VERIFY stored `q_1024_vec` norms
        ≈1.0, then use `metric='ip'` + sq8) and **`sdb_nprobe`** (default 8; with nlist ≈
        2·√350K ≈ 1.2K clusters that probes <1% — expect to raise it) + **`sdb_rerank_factor`**
        (exact-distance rerank recovers quantization loss) at query time
        (docs: sql/indexes/inverted/vector-search). Tune the (quantization × nprobe ×
        rerank_factor) matrix against the gold-eval recall bar, not against feel.
      - **Prediction to check:** our corpus is clustered BY CONSTRUCTION (18 KBs of distinct
        topics), which is IVF-friendly — queries deep inside one topic should recall fine at low
        nprobe. The stress case is **cross-domain queries landing near cluster borders** (e.g.
        "huge pages in Postgres" straddling Linux-memory and Postgres-tuning clusters — the exact
        queries `_diversify` exists for). If recall drops anywhere vs ES's HNSW, expect it there;
        measure those separately, don't let single-topic queries average the failure away. Also:
        k-means centroids are trained on the data present at build time — **build the index AFTER
        the collection ingest drains**, and expect periodic rebuilds as the corpus grows.
      **MEASURED 2026-07-22 — Phase 0 done, PARITY REACHED. Scripts: `serenedb-load.py` (mirror),
      `serenedb-index.py` (normalize+build), `serenedb-eval.py` (recall), `serenedb-repro-planner.py`
      (the limitation, minimal).**
      - **Mirror**: 247,665 chunks (ES scroll → Postgres wire, ~181 rows/s, idempotent top-up).
      - **The norms prediction was HALF WRONG, and it mattered.** Guidance said bge-m3 is
        L2-normalized ⇒ cosine ≡ ip ⇒ ip+sq8 quantization is free. Measured: stored `q_1024_vec`
        norms are **0.935–0.976 (mean 0.950)**, NOT 1.0. On the raw column `ip` would silently
        boost longer vectors ~4% and rank differently from ES's cosine. Fix: materialize a
        normalized column `embn` (one set-wise DuckDB `list_transform` statement, 807s for 247K —
        NOT 247K client round trips) and index that with `metric='ip', quant='sq8'`. On unit
        vectors ip IS cosine, so we keep ES semantics AND unlock quantization. Raw `emb` stays an
        untouched mirror. Post-check: norms exactly 1.000000.
      - **Index build: 4.7s** for 247K×1024 (inverted + IVF in ONE index, incl.
        `optimize_top_k='bm25(1.2,0.75)'`). Verified real, not lazy: self-retrieval returns the
        query's own chunk at rank 1, and `<#>` is negative-ip (ascending = most similar).
      - **The delimiter analyzer choice is validated by the stemmer surviving**: `мыш` matches 103
        chunks via `@@` vs ~983 by naive substring — because `мышц` (muscle) is correctly a
        DIFFERENT token. Our Snowball patch reaches SereneDB intact, and the query side reproduces
        it (rag_tokenizer loads standalone by stubbing `common.settings.DOC_ENGINE_INFINITY`).
      - **RECALL@64: 5/5 = ES parity, 24.7 ms mean** (weighted fusion, nprobe=8). Better ranks than
        RRF on three queries (photosynthesis @3→@1, lsn-general @2→@1).
      - **The IVF-border prediction did NOT reproduce; the real risk was FUSION.** nprobe 8→512
        changed recall not at all (flat 4/5 on RRF) and only cost latency (16→48 ms) — so the miss
        was never the index probing too few clusters. Cause: `@@` over a tokenized question is OR
        semantics (the 5-token mice query matches **7,901** chunks), so equal-weight RRF lets
        stopword hits flood the pool and evict a gold chunk sitting at **vector rank 50**.
        Weighted fusion (0.7*bm25_norm + 0.3*cosine, RAGFlow's own weights) recovers it to @40.
        **Lesson: for engine-swap parity, replicate the incumbent's FUSION, don't accept the new
        store's default.** RRF is not wrong in general — it is wrong as a parity claim.
      - **LIMITATION FOUND (worth reporting upstream — he knows the team).** Certain query shapes
        lose the inverted-index access path and fail at PLAN time with
        `UndefinedFunction: Table Function with name iresearch_scan does not exist! Did you mean
        "iceberg_scan"?`. Narrowed by the reproducer (which corrected our first guess — joining two
        index branches is FINE): the breaking construct is a **scalar subquery over an
        index-scanning CTE**, i.e. `(SELECT max(s) FROM lex)` — exactly the natural way to
        normalize BM25 before weighting it against cosine. So the fusion that wins recall is the
        one pure SQL cannot express here; we fuse client-side instead (+~7 ms, identical math).
        This is a Phase-1 constraint too: `serenedb_conn.py` will hit it.
      **TUNING PASS 2026-07-22 (after parity) — what moved the needle and what backfired.**
      recall@64 is SATURATED (ES and SereneDB both 5/5), so it cannot show improvement; the eval now
      reports **recall@8, MRR and mean rank of gold**, which have headroom (the mice query was
      "recalled" at rank 40 — present, but far outside the window a synthesizer reads).
      | config | r@64 | r@8 | MRR | mean rank | ms |
      |---|---|---|---|---|---|
      | weighted fusion (parity config) | 5/5 | 4 | 0.81 | 8.8 | 36 |
      | plain RRF | 4/5 | 4 | 0.57 | 1.8* | 19 |
      | **weighted RRF (`wrrf`)** | **2/5** | 0 | **0.02** | 24.0 | 21 |
      | weighted + cross-encoder rerank | 5/5 | 4 | 0.81 | **3.8** | 10393 |
      | RRF + cross-encoder rerank | 4/5 | 4 | 0.80 | 1.0* | 10494 |
      *mean rank is computed over FOUND queries only, so a config that MISSES the hard query
      flatters its own average — MRR is the honest summary. Noted because it nearly fooled me.
      - **`wrrf` was my idea and it FAILED — instructively.** The plan: weight the reciprocal ranks
        instead of the scores, so no `max(BM25)` is needed and the whole fusion fits in ONE query
        the planner can serve (killing the client-side second round trip). Result 2/5, MRR 0.02.
        Why: weighting ranks ≠ weighting scores. In score space a weak lexical match contributes a
        small number; in rank space the top lexical hit contributes 1/61 **however bad it is**, so
        the 0.7 lexical weight AMPLIFIED the 7,901-match stopword flood instead of emphasizing good
        lexical evidence. Rank-based fusion discards the "how good is this match" signal — which is
        precisely the signal the hard query needs. The planner limitation therefore still costs us
        a round trip; it is not routable around this way.
      - **The real quality lever was a MISSING STAGE, not a knob.** RAGFlow's pipeline is
        retrieve→RERANK; our SereneDB path was retrieval-only, so the comparison had been unfair to
        BOTH sides. Adding the same GTE cross-encoder (:9760): mean rank **8.8 → 3.8**, and the
        mice query **@40 → @15** — the bi-encoder scores resemblance (bats = "летучие мыши" beat the
        rodent passage 0.762 vs 0.471), a cross-encoder reads query+passage together and scores
        answerability. Cost: **10.4 s** on CPU for 64 docs. Note this is PARITY, not advantage —
        ES pays the identical stage.
      - **STILL MISSING for any "better than ES" claim: the ES latency baseline.** We measured
        SereneDB at 19–36 ms retrieval-only and never timed the ES hybrid on the same queries, so
        "faster" is currently unmeasured. Do that before tuning further.
      **RERUN 2026-07-23 — BM25 root cause found upstream; lexical branch now LIVE.** The other
      session's trace (see ~/Projects/serenedb/SERENEDB-FINDINGS-HANDOFF.md, retest block): the
      zero scores were a dictionary-config gap, not a scorer bug — the delimiter dictionary needs
      `frequency = true` (else BM25::PrepareScorer returns a constant-0 scorer, SILENTLY) and
      `norm = true` (else the `b` length term is inert). serenedb-index.py now builds `chunks_idx`
      against `ltks_scored` (delimiter + frequency/position/norm; rebuild 5.6 s). Same gold eval,
      same fusion math, lexical branch finally contributing:
      | config | r@64 | r@8 | MRR | mean rank | ms |
      |---|---|---|---|---|---|
      | ES hybrid (baseline) | 5/5 | 4 | 0.82 | 2.8 | 180 |
      | weighted fusion, lexical inert (07-22) | 5/5 | 4 | 0.81 | 8.8 | 36 |
      | **weighted fusion, lexical live** | 5/5 | 4 | **0.82** | **3.0** | **35** |
      | weighted + GTE rerank stage | 5/5 | **5** | **0.83** | **2.0** | 10515* |
      *NOT cold-start (first guess, wrong): three runs at 10,393/10,566/10,515 ms = steady-state
      CPU inference — qwen-next holds 20.6 GB of the card, so GTE runs CPU. A GPU-resident rerank
      timing requires evicting qwen-next; either way the stage is engine-independent (ES pays the
      identical rerank), so it cancels in the engine comparison — 35 ms vs 180 ms is the delta.
      Headline: **ES-parity MRR at ~5× lower latency, and with the (shared) rerank stage SereneDB
      beats the ES baseline on every quality metric** — mice-species gold @11 fused, @6 reranked.
      Also from the retest: the `iresearch_scan` plan-copy bug fires on ANY ≥2 references to an
      index-scanning CTE, and `s / MAX(s) OVER ()` (window fn, single reference) is a pure-SQL
      workaround — so the weighted fusion IS expressible in one query after all; the wrrf detour
      above is obsolete. G4.2 is no longer gated: config fix + workaround unblock serenedb_conn.py.
- [ ] **G4.2 — Phase 1 (only on G4.1 parity): `serenedb_conn.py` for RAGFlow.** RAGFlow abstracts the
      store behind `DOC_ENGINE` (`rag/utils/{es,infinity,opensearch}_conn.py`) — implement the same
      interface, bind-mount into pinned v0.26.4 like our other patches. Side benefit: every ES-direct
      scan in clean-chunks/build-junk-features becomes plain SQL.
      **2026-07-23: conn WRITTEN and smoke-green** (worktree c70470d). ob_conn-shaped (one table
      per tenant, ES field names verbatim); every engine choice probe-verified on 26.07.3 first
      (serenedb-conn-probe.py): multi-column inverted index (OR-match SUMS per-column BM25 — free
      field boosting), list_contains/unnest arrays, single-query window-norm fusion, ~1s async
      index refresh (ES refresh_interval semantics), native wire decode. NEW ENGINE FOOTGUN:
      vector-op predicate inside an ANN scan's WHERE silently EMPTIES the result
      (serenedb-ivf-probe.py B2/B4) — thresholds must wrap the scan; added to the handoff.
      test-serenedb-conn.py runs the full lifecycle standalone: ALL PASS.
      REMAINING to run RAGFlow on it: settings.py `serenedb` branch + msgStoreConn decision
      (keep ES for memory store during side-by-side), service_conf entry, docker bind-mounts,
      then in-app eval vs ES before any cutover. Contributable upstream after a port to HEAD.
      **2026-07-23: RAGFlow now RUNS on SereneDB end-to-end.** Wiring done: settings.py
      `serenedb` doc-engine branch (+ msgStore stays on ES for the trial), service_conf `serenedb`
      block, docker-compose bind-mounts (serenedb_conn.py + settings.py), serenedb joined to the
      docker_ragflow network. Full ES->SereneDB migration THROUGH conn.insert(): 247,665/247,665
      docs, 0 errors, 44 min at ~95 docs/s (serenedb-migrate-es.py). Post-migration verify
      (serenedb-verify-migration.py): row count exact, fulltext lane 'мыш'->103 (ES-era match) in
      3ms, ANN lane serves, conn.search fusion 10 hits in 35ms with distinct scores. Container
      recreated DOC_ENGINE=serenedb; boot log shows SereneDBConnection initialized; a live
      search_corpus MCP query returned 5 reranked verbatim Cyrillic passages — retrieval,
      tokenization, vector fusion, rerank all working with NO ES in the doc path.
      NEXT: run the gold eval IN-APP on both engines (not just the standalone harness), decide
      cutover, then retire ES doc store. Note: `serenedb` still logs "Unknown configuration key"
      (harmless — config_utils allowlist), worth silencing before upstreaming.
      **QUERY THROUGHPUT measured 2026-07-23 (serenedb-throughput.py, gold queries, thread pool).**
      SereneDB ~2.4x ES QPS at every concurrency, ~2x tighter tail:
      | conc | SDB QPS | SDB p99 | ES QPS | ES p99 |
      |---|---|---|---|---|
      | 1 | 49 | 27ms | 30 | 53ms |
      | 8 | 301 | 35ms | 161 | 84ms |
      | 16 | 514 | 42ms | 241 | 108ms |
      | 32 | 580 | 112ms | 245 | 232ms |
      Both saturate ~conc 16-32 (single shared box). SDB tail stays under ES median until sat.
      **SCORER SWEEP 2026-07-23 (serenedb-quality-sweep.py) — the 07-22 pass ran with BM25 inert,
      so lexical-scorer choice was never tested.** Harness validated: bm25/prune-off/nprobe-32
      reproduces the parity 0.818 exactly. Findings (5-query gold, MRR / mean-rank / recall@8):
      - PRUNE HURTS at real nprobe (bm25 0.818 -> 0.549 with prune on) — earlier "prune is THE fix"
        was measured with the lexical branch inert; retract it. Keep prune OFF.
      - **lm_dirichlet (Dirichlet-smoothed LM scorer) BEATS BM25**: MRR 0.829 / rank 2.2 /
        recall@8 **5/5** at vw~0.25, vs bm25 0.818 / rank 3.0 / 4/5. Robust across vw 0.2-0.4.
        The win is at RETRIEVAL: lm_dir gets the hard mice query into the top-8 pool; bm25 leaves
        it at ~11. WITH the GTE rerank stage both converge to 0.833 / rank 2 / 5/5 (reranker
        re-reads the pool), so the scorer only matters for un-reranked serving.
      - TFIDF's apparent early lead was a prune-on artifact; it's worse than bm25 with prune off.
      - HONEST CAVEAT: lm_dirichlet is NOT a SereneDB-only knob — Lucene/ES ship
        LMDirichletSimilarity too (per-field `similarity` mapping). Neither RAGFlow default uses
        it. So this improves retrieval on WHICHEVER engine we keep; it does not tilt ES-vs-SDB.
      TO ADOPT: swap the lexical scorer in serenedb_conn's search SQL from BM25() to
      lm_dirichlet(); it's a one-line change, same index (no rebuild — LM scorers read the same
      frequency/norm postings). Gate on the in-app eval confirming the gold-set win survives.
      **IN-APP PARITY ACHIEVED 2026-07-23 (eval-retrieval.py through RAGFlow /retrieval, both
      engines).** The in-app eval caught THREE conn bugs the standalone smoke test could not —
      each a silent 0-recall, exactly why in-app gating matters (worktree 4b7e983):
      1. get_scores() + create_doc_meta_idx() are called by the retriever but are NOT
         @abstractmethod on DocStoreConnection — a conformant subclass compiles, smoke-passes, and
         returns 0 recall. FIX: implemented both. UPSTREAM-WORTHY: add them to the ABC.
      2. lexical @@ must use MatchTextExpr.matching_text (RAGFlow's tokenized ^-weighted query),
         not extra_options['original_query'] (raw) — else tokenizer-split terms miss
         (auto_ptr stored as 'auto _ ptr').
      3. scored lexical branch matches content_ltks ALONE: ORDER BY BM25 over a multi-column @@
         OR EMPTIES at scale when 2+ columns densely match (serenedb repro
         repro-multicolumn-bm25-order-empty.py). ES field boosts (docnm^10) deferred.
      RESULT — in-app, through the real pipeline, identical gold set:
      | query | ES rank | SereneDB rank |
      |---|---|---|
      | mice-species | 32 | 28 |
      | photosynthesis | 1 | 1 |
      | lsn-general | 23 | 6 |
      | lsn-replay-fn | 3 | 3 |
      | auto-ptr | 5 | 11 |
      | recall@8 | 3/5 | 3/5 |
      | recall@64 | 5/5 | **5/5** |
      SereneDB ties ES on recall in-app and ranks 2/5 queries better (lsn-general 6 vs 23).
      Reranked, auto-ptr/photosynthesis/lsn-replay all land @1. Adapter is FUNCTIONALLY COMPLETE.
      REMAINING before retiring ES: adopt lm_dirichlet (above), silence the config-key warning,
      re-add ES field boosts via per-column BM25 summed in Python (not OR — see bug #3), forward-
      port to RAGFlow HEAD for the upstream PR.
- [ ] **G4.3 — dogfood the side stores:** labels DB + feature matrix could ride SereneDB too; daily
      use also fixes Suite B's "no corpus coverage of serenedb" gap from the inside.

### G5 — Ingest the `ml` shelf (~/Documents/Books/ml — triaged 2026-07-20, NOT yet ingested)

The shelf is clean (deduped, mojibake fixed, djvu→pdf converted, `_dupes/` holds the retired copies)
but nothing from it is in the corpus yet. Three lanes when we ingest (after the collection drains):
- [ ] **English born-digital PDFs** (Bishop PRML, ESLII 2e, MML, Shalev-Shwartz, Kochenderfer 2e,
      Deisenroth integration draft) → DeepDoc `book` lane (real text layers, positions + figures).
      New `ml` KB or fold into `books` — decide at wiring time.
- [x] **RESEQUENCED (2026-07-21 late): the Opus gold fleet REPLACES the audit gate.** Every page is
      individually verified against its image (verify-and-correct, full transcripts + diff records
      in corpus/ml/opus-gold/) — a stronger guarantee than the sampled-audit protocol. When the
      fleet completes: assemble books FROM GOLD (same [[p.N]] format), wire ("ml","naive",
      ["ml/*.txt"]), **ingest immediately, no gate**. H12 (teach the VLM) is explicitly sequenced
      AFTER G3.8 (junk classifier); its step-0 temp sweep can use any idle-GPU moment.
- [x] **Russian scans → LANE DECIDED (2026-07-21): local qwen3-vl transcription** (DESIGN §4.4).
      The bake-off: embedded layer = clean prose / broken math; marker = perfect math / CJK-poisoned
      prose + dropped code terms; **qwen3-vl:30b won all three axes** (char-exact code incl. the
      `+ Fib(n-2)` marker dropped, `$a^{13}$` superscripts intact, clean prose). Full 6-book run
      (2,614 pages, ~10 GPU-hours, resumable `transcribe-scans.py`) **RUNNING**; output
      `corpus/ml/<slug>.txt` with `[[p.N]]`. GATE before ingest: blind 20-page audit sample
      (agreement protocol — VLM OCR can silently paraphrase). The 4 papers/theses have usable text
      layers except ЭЧАЯ (CID-garbled) — run it through the VL lane too (51 pages, minutes).
- [ ] Wire the chosen lanes into `ingest-corpus.py` (idempotent, EXCLUDE convention available) and
      run — AFTER the collection ingest finishes (CPU) and ideally after the G3.8 label pass so the
      new books enter through whatever curation the classifier ships.

### G5.1 — Dedup the book library before ingest (2026-07-24)

~420 PDF/EPUB in ~/Documents/Books (local library + several fetched collections). The same book
recurs as different scans, PDF-vs-EPUB, edition variants, terse-vs-verbose names. `dedup-books.py`
resolves it (DESIGN §4.5) — a **cheap→expensive cascade**, split into two phases:
- [x] **Cascade built + hardened.** Filename (containment, not just Jaccard — catches
      `clean-code.pdf` ≡ `Clean Code_ A Handbook…`) → page count corroboration → qwen-next inside
      the file. A review of the tool's own SUSPECT list caught a false-merge class: shared domain
      phrases ("deep learning") let union-find chain 3 distinct books into one and silently drop 2;
      fixed by requiring equal page counts / pdf-epub pairing on the fuzzy path. **381 unique books**,
      12 already-ingested excluded, ~40 dup/older editions dropped.
- [x] **qwen-next re-architected as a feature extractor, not a matcher** (his call). `--extract`
      loops each PDF once → `{title,authors,edition,year}` cached to `.book-identities.json`
      (resumable, N calls, no O(n²)); match phase is pure Python over the cached titles. The
      pairwise-qwen version monopolized the GPU to a client timeout — the extract/match split fixes
      it. **Extraction running now** (background, ~380 pdfs).
- [x] **Extraction + match DONE (2026-07-24).** 355 PDFs → qwen `{title,year,pages}` extracted to
      `.book-identities.json`, matched in pure Python. Report `~/Documents/Books/dedup-report.txt`,
      machine list `keep-plan.json` (`{"keep": [<341 absolute paths>]}`). Outcome: **341 KEEP**,
      65 older editions DROPPED (newest edition of each stays), 14 already-ingested skipped, 67
      groups flagged "verify". His decisions (2026-07-24): **keep ALL distinct titles — no domain
      filter** (fiction / pop-sci / philosophy stay in; the corpus is broad, not coding-only); and
      **drop the 65 older editions** in favour of the newest already in KEEP.
- [x] **INGEST the new KEEP books — DONE uploading, PARSING 2026-07-24 (his call: "ingest now",
      scope "new-only").** Critical scoping catch: of the 341 KEEP, **165 were already in a KB**
      (awesome-book-collection=`collection` 141, `ml` 19, `bio` 5 — their whole repo is a *_raw
      symlink already parsed), so ingesting all 341 would double-hit retrieval. `wire-keep-books.py`
      drops those 165 and symlinks the **176 genuinely-new** into `corpus/keep_raw/`: **132 PDFs**
      symlinked + uploaded to the new **`keep-books` KB** (`book` parser) — all 132 parsing
      server-side (CPU/DeepDoc, hours). Full key ends `-suiLQTGM` ([[ragflow-api-key]]); ingest via
      `uv run ingest-corpus.py --api-key <KEY> --only keep-books` (added `--only` filter + the KB line).
- [ ] **Stage 2 — the 44 KEEP .epub** (book parser rejects epub) listed in
      `corpus/keep_raw-needs-conversion.txt`. Convert pandoc/calibre→md, then a `keep-books-epub`
      naive KB (chunk_method is per-dataset, can't mix book+naive in one KB).
- [ ] **Verify the 67 MERGE groups** (`dedup-report.txt`) for within-KEEP dup filenames — deferred,
      not blocking; low risk (idempotent by filename within a KB; cross-KB overlap already handled).
- [ ] **G5.2 — course-slides ingest strategy (2026-07-25, his call: pulled the 6 LTL course decks
      back out of keep-books before they parsed, added this).** Slides are a **poor RAG modality** and
      we need a general policy before ingesting any more. The reasoning (cite): (1) slides are sparse
      bullet fragments + heavy figures + little connective prose → the `book` parser manufactures
      exactly the **FIGURE_GARBAGE and fragmented-chunk** classes the G3.8 junk fleet exists to fight;
      (2) they **lose to real books at retrieval** — a bullet reading "BM25" ranks on the term but
      explains nothing, while Manning has the derivation → the "garbage shaped like the query" failure
      (BLOG); worst on the very topics where we now have the best sources; (3) several were **French**
      in a mostly-English/Russian corpus. Options to evaluate: (a) fast `naive`/pdftotext lane (skip
      the figure-OCR that generates the garbage); (b) **hold slides until G3.8 demote-by-default** is
      live so they enter score-demoted; (c) a slide-aware extraction (title + bullet-dedup + drop
      figure boxes); (d) keep slides as on-disk reference only, never in RAG. Files kept at
      `~/Documents/Books/LTL/Course-Material/` (6 merged per-course PDFs).
- [ ] **Fix the 3 scripts hardcoding the TRUNCATED key** (`oracle-ingest-mcp.py`, `ingest-status.py`,
      `scratch-membership.py` → append `-suiLQTGM`); they currently 401. [[ragflow-api-key]]
- [ ] **Rename KBs + on-disk organization — DEFERRED (his call 2026-07-24: "keep the rubrics for
      now, deal with them later").** KB/dataset names (`keep-books`, `ml`, `collection`, …) are the
      retrieval key referenced in 3 places (RAGFlow dataset + `ingest-corpus.py` KBS + code:
      oracle-ingest-mcp/oracle-browser/build-junk-features/clean-chunks). Also revisit how the book
      library is laid out on disk (~/Documents/Books repos + corpus/*_raw symlinks). Not urgent.
- Note: keep-books parse ETA measured **~28h** at full CPU (0.04 tasks/s; 4,531 page-tasks; all 24
  cores, DeepDoc+CPU-embed is the cost). Runs server-side, resumes on interrupt; `./ingest-status.py`.

### G4.4 — NUL (0x00) in chunk text breaks SereneDB insert (2026-07-25)

Production RAGFlow runs **DOC_ENGINE=serenedb** (confirmed on the container) — the keep-books
parse writes chunks straight into **SereneDB via `serenedb_conn.py`**, not ES. One keep-books doc
(a book) FAILed with DuckDB rejecting
`A string literal cannot contain NUL (0x00) characters`. Origin is NOT OCR (a vision model can't emit
U+0000) — it's DeepDoc's **pdfminer text-layer extraction**: this PDF has a broken CID→Unicode font
map, so pdfminer maps unmappable glyphs to U+0000 (its own `_is_garbled_char` docstring names this),
and those NULs ride the merged text-layer chars into the chunk in two page-ranges. (RAGFlow's error suffix says "Elasticsearch/Infinity" — that's stale boilerplate, NOT
the active engine.) One failed page-task marks the whole doc FAIL though 353 chunks landed.
Root cause (verified 2026-07-25): NUL is NOT real content — this PDF has a broken font/encoding
(`pdftotext` yields NUL on 29,720 lines too), so it's **extraction corruption**, and the fix belongs
in the PARSER, not the storage adapter (a DB-layer strip would leave the garbage in the embeddings +
displayed page). NOT a DeepDoc-specific bug — any extractor hits it on this PDF.
- [x] **POLICY (his call 2026-07-25): a broken text layer must trigger OCR FALLBACK, not a strip.**
      Stripping garbled chars *deletes* real content (leaves holes/gibberish where text should be);
      OCR *recovers* it from the page image, which is already rendered. NUL/control chars cannot be
      legitimate content, so their presence is a hard signal that pdfminer's CID→Unicode mapping
      failed for that region → re-OCR that region and use the OCR text, discarding the corrupt
      text-layer chars. Consistent with "reocr is the way" (G3.9).
- [x] **DONE 2026-07-29 (Python side).** Applied as patch 2 of `patch-ragflow.sh`, exactly as
      specified below: an absolute `any('\x00' in c['text'])` check inserted BEFORE the existing
      strategies, setting `self.page_chars[pi] = []` so the OCR path supplies the text. Absolute and
      not a ratio, for the reason recorded here — the existing strategies sample 200 chars and
      compare against 0.3, and a single NUL trips no ratio while being enough to fail the insert.
      Verified by executing the patched module in the container. **Go `internal/deepdoc` still
      needs the mirror** (`util/garbled.go`, `IsGarbledText` is ratio-based there too) before G4.3;
      written up for upstreaming in `~/Projects/ragflow/stories/oracle-container-patches.md`.
- [ ] **Mechanism in `deepdoc/parser/pdf_parser.py`:** the OCR-fallback machinery already exists —
      `_is_garbled_text` decides "this block is garbled → OCR it." The bug is the trigger is a **soft
      0.5 ratio**: a block with a few scattered NULs stays under threshold, never falls back, and the
      corrupt chars pass through. Fix = make NUL / control chars (anything `_is_garbled_char` flags as
      `cp < 0x20`, plus the PUA/CID cases) a **hard trigger** — their mere presence routes the block
      to OCR, independent of the ratio. Never let a NUL survive to the chunk. Go `internal/deepdoc`
      needs the same before G4.3.
- [ ] Sequencing: apply, then container restart to reload the parser — the restart re-queues the ~104
      docs still parsing, so **do it after the bulk parse drains**, then re-parse the FAIL(s).
      Deterministic: re-parsing WITHOUT the fix fails again on the same NUL.
      **State 2026-07-29:** patch applied, restart NOT yet done (13 TPC specs + 1 book were parsing).
      Restart, then re-parse the one remaining FAIL. NOTE: re-parse is FULL cost here — RAGFlow's
      page-level reuse is keyed on a digest over the whole chunking config, and ours no longer
      matches what was stored (`005be96cdb6f0162` vs `e11ca2f17b35f4bb`), so every page-task re-runs
      and the previous chunks are deleted first. Do not promise "only the failed pages".

### G4.3 — Switch to the native Go SereneDB engine after ingestion (his plan 2026-07-24)

**CORRECTION 2026-07-25: production is ALREADY on SereneDB (DOC_ENGINE=serenedb), so keep-books are
being written into SereneDB now — NOT ES. My earlier "ingest to ES then migrate" note was wrong.
The switch to the Go engine needs NO ES→SereneDB migration; the data is already in SereneDB. The
switch is: swap the Python `serenedb_conn.py` adapter for the Go engine, both reading the SAME
SereneDB tables — so it reduces to a schema-match check (`serenedb_conn.py` writes vs Go `schema.go`
`columnOrder`, incl. `ragflow_doc_meta_*`).**


`~/Projects/ragflow/ragflow` branch **`ik-serenedb-go-engine`** adds a full native Go SereneDB engine
(2,716 lines: `internal/engine/serenedb/{schema,search,chunk,client,metadata,…}.go`) to the RAGFlow
**Go rewrite**. Plan: switch production to this branch after the keep-books ingest finishes.
- **KEY FINDING — no re-parse needed.** `schema.go` keeps the **ES mapping field names verbatim**
  ("the read path needs no renames"): `content_ltks`, `content_with_weight`, `q_1024_vec`,
  `pagerank_fea`, `page_num_int`, … — the SAME layout as the Python `serenedb_conn.py` I built, with
  the same landmines baked in (dict `rf_scored_delim` freq/position/norm = the BM25-zero fix;
  `lexScoredCol="content_ltks"` single-column = the multi-col-OR-empties fix; `q_(\d+)_vec` vector
  col). So switching is a **data migration, not a reingest** — parsed chunks + bge-m3 embeddings are
  reused. This resolves his earlier "python vs go data layout differs — reingest?" — layouts DON'T
  differ at field level; both mirror ES.
- **Sequence:** (1) keep-books finishes parsing into ES (~28h) → (2) re-run `serenedb-migrate-es.py`
  (did 247k docs ES→SereneDB in ~44min once already this session) to sync the new ~176 books into
  SereneDB → (3) switch RAGFlow to the Go branch pointed at SereneDB.
- **Verify at cutover:** the migrate script's written column set exactly matches `schema.go`'s
  `columnOrder`/`docMetaColumnOrder` (both ES-derived, should match — but confirm, incl. the
  `ragflow_doc_meta_*` per-tenant metadata tables the Go engine expects). Relates to [[ragflow-agents-and-api]].

### G4.2/G4.1 — SereneDB: in-app parity + upgraded to 26.07.4 (2026-07-23/24)

- [x] **In-app parity reached** (through RAGFlow /retrieval on both engines): recall@64 5/5 = ES.
      Three conn bugs the in-app eval caught (not the unit smoke): missing get_scores/
      create_doc_meta_idx (silent 0-recall — worth adding to the ABC upstream), lexical @@ must use
      the tokenized matching_text not raw original_query, and single-column BM25 (multi-column OR
      empties). Adapter FUNCTIONALLY COMPLETE. Contribution handoff written to ~/Projects/ragflow.
- [x] **Upgraded production to SereneDB 26.07.4** (released 2026-07-23). Verified two of my four
      reported engine bugs FIXED against the released image: #962 multiref-CTE (iresearch_scan) and
      #964 vector-op-predicate-in-ANN-WHERE. Simplified the conn to the natural queries relying on
      those fixes (min engine version now 26.07.4); dropped the workarounds. Container upgraded in
      place (volume carried forward, index recovered); in-app recall@64 stayed 5/5, auto-ptr @1.
- [ ] Still open: adopt lm_dirichlet scorer (recall@8 5/5 vs BM25's 4/5), re-add ES field boosts via
      per-column BM25 summed in Python, silence the `serenedb` config-key warning, forward-port to
      RAGFlow HEAD for the PR. Multi-column-BM25-order engine bug: production-confirmed, not yet
      minimally reproduced (needs large matching-set scale) — retest on a future SereneDB release.

---

# H. PARKED (good ideas, deliberately not being built)

Written down so they cost nothing to leave alone. **Do not start these.**

- **H17 — ASSOCIATIVE RERANKING: recency-context as a capped, additive stage (his idea, 2026-07-26;
  DESIGN §5.4).** The reranker scores `(query, doc)` only — no channel for *what I've been reading*.
  Add a bounded **decaying "fixed-slot" memory** (streaming k-means, K≈8–16 slots: centroid + decaying
  weight + label; new topics merge by cosine or evict the lowest weight; `w *= exp(-Δt/τ)`), and blend
  it into the already-reranked pool as `score = α·rerank_norm + β·assoc`, `β` small and **capped** so
  it lifts on-theme passages a few ranks but never overrides relevance. **Tiered signals:** all
  browsing feeds it, but **capture + explain at full weight, passive dwell at a fraction** (and behind
  the exception list). Lives as a shared `oracle-context` service so `ask_corpus` (not just the
  extension's explain) becomes associative; the **capture extension is already the sensor** (built
  2026-07-26). Exception/denylist + incognito + forget + pin, managed from the popup — dwell is the
  poisoning surface, so the denylist is load-bearing. **Sibling of H13** (index-side enrichment) **and
  H14** (interpretation memory): H17 remembers *current reading* and biases *ranking* — the third
  orthogonal lever on "topical proximity ≠ answerability", the wall the reranker alone can't climb.
  **The gate (why it's parked):** recency bias IS the repo's cardinal failure (garbage shaped like the
  query) in a new mask, and weighted-RRF already backfired (G4.1). Ships only behind an off-switch and
  only if it moves the gold passage's rank UP without hurting recall@64 / precision, measured A/B on
  the gold-query eval (§D). Until that number exists: design, not code.
  **KDA connection (his pointer, 2026-07-27):** this fixed-slot memory is a coarse, inspectable
  instance of linear/delta attention — Moonshot's **KDA (Kimi Delta Attention)** family: fixed-size
  state + gated decay + delta-rule overwrite (softmax keeps everything and defocuses; linear-delta
  forgets by construction). Two write rules to A/B when it graduates: **EMA-merge** (current design)
  vs **delta-overwrite** (a colliding topic *replaces* rather than averages, so a shifted reading
  lands at the same address). K3's exact gate awaits its technical report (DESIGN §5.4 postscript).

- **H16 — KV SLOT SAVE/RESTORE: warm sessions parked in RAM (promoted from a §F footnote —
  it was never in this list; DESIGN §2 describes it).** llama-server already exposes
  `--slot-save-path` + `POST /slots/{id}?action=save|restore` (a slot's KV dumped/reloaded as a
  file — memcpy-fast vs a multi-minute re-PP). It isn't automatic because "session stop" is a
  client-side event the server never hears. Teach the **shim** to save the active slot's KV to a
  tmpfs on idle (keyed by session) and restore before the next session's first request →
  effectively unlimited warm qwen-next sessions in the 125 GB RAM, surviving server restarts.
  Caveats: a dump is void when the prefix changes (DISCIPLINE edit, compaction rewriting history,
  model/quant/flag change) and is coupled to the exact llama.cpp build. (H14's memory layer and
  this compose: one remembers *knowledge*, the other remembers *context*.)
- **H13 — WRITE-TIME CHUNK ENRICHMENT (from the A-Mem paper, NeurIPS 2025 — in the corpus).**
  A-Mem's eq. 3: embed LLM-generated keywords + context WITH the content, so the vector moves toward
  the queries that should find it. This attacks "topical proximity ≠ answerability" (the мыши case)
  from the INDEX side — the one lever the reranker can't reach (it only reorders what stage 1
  finds). Build `enrich-chunks.py` (inverse of clean-chunks: qwen writes per-chunk keywords + a
  "what questions does this answer" line at idle-GPU time; remove+reingest so it re-embeds), pilot
  on `bio` where the failure lives, judge on the gold-query eval. Their ablation credibility note:
  works across 1B-3B local models.
- **H14 — AGENT MEMORY LAYER, A-Mem-style (the sleeper).** qwen's sessions learn nothing from each
  other. A small evolving note-store of SESSION LEARNINGS ("orioledb branch names embed issue
  numbers", "serenedb IVF wants nprobe tuning") beside the corpus — storage-side agency where it
  belongs: over INTERPRETATIONS, not sources. Notes = A-Mem schema (content, keywords, context,
  links); link-gen = embedding recall + qwen judge (their ablation: link generation is the
  load-bearing module, evolution is refinement). Retrieval wired into the DISCIPLINE/MCP as a small
  "session memory" tool. **Design requirement — evolution with provenance BY CONSTRUCTION (his
  point, 2026-07-21):** qwen can't be trusted to append changelogs (that would be a prompt
  workaround, Axiom 2), so the store must make silent rewrites impossible: **append-only versions +
  a `latest` view** (the labels-DB pattern) — an "update" is an INSERT with a reason field; history
  is free; provenance is schema, not behavior. (Claude's own memory uses the discipline form of the
  same rule — [memory dir] — because a written rule suffices there.)
- **REJECTED (recorded so we don't re-litigate): A-Mem-style memory evolution applied to the
  CORPUS.** Chunks are sources, not interpretations; rewriting sources as understanding grows is
  corruption. The source-store vs experience-store distinction is the line the paper never draws.
  (A-Mem's update-with-provenance instinct was adopted for Claude's own memory discipline instead —
  not an Oracle work item.)
- **H12 — TEACH THE VLM (his idea, 2026-07-21): the transcription lane as a trainable system.**
  The early audit produced exactly the artifacts training needs: an error taxonomy (near-synonym
  substitutions, dropped `⁻¹`, hallucinated `[Рис.:]` stubs, leaked running heads), ground-truth
  corrections, and a frozen acceptance sample. Three rungs, cheapest first:
    1. **Prompt tournament** (not really parked — folds into the lane cleanup): variants scored
       against the frozen audit sample, DISCIPLINE-tournament style. Targets the two systematic
       bugs (template-echo figure stubs, running heads).
    2. **DPO from audit corrections**: every audit fix is a preference pair (page image, flawed
       transcript, corrected transcript). Audits become training data, not just gatekeeping.
    3. **RLVR/GRPO with a VERIFIABLE reward**: synthesize pages we control (Cyrillic prose + LaTeX
       + Pascal in book typography, scan-degradation augmentation) → reward = edit-distance +
       formula-exact-match against the generated source. No human labels in the loop; Unsloth
       ships Qwen3-VL RL notebooks.
  **Constraints — RESEARCHED 2026-07-21 (he can rent an H200):**
  - **QLoRA-4bit on the 30B MoE is off the table everywhere** — BitsandBytes doesn't support MoE
    quantized training (Unsloth: "not recommended right now"; explains their missing 30B BnB
    upload). So the 24 GB local box genuinely can't train the 30B — not a skill issue.
  - **LoRA-16bit SFT of 30B-A3B: ~63 GB @4K ctx (Unsloth H100 bench) → fits ONE rented H200
    (141 GB) comfortably**, vision tower included. Community recipe exists for Qwen3-VL-30B-A3B
    LoRA; caveats: DeepSpeed **ZeRO-3 breaks LoRA gradient flow on this MoE — use ZeRO-2**; leave
    the router frozen (default). Gold-SFT epoch over ~2.6K pages ≈ hours; rental cost trivial.
  - **RL (GRPO/GSPO): 8B has the beaten path** — official Unsloth VLM-RL notebooks (runs on a T4!),
    custom python reward fns (our edit-distance + formula-exact RLVR reward plugs straight in),
    vLLM rollouts via fast_inference=True. **30B-A3B GRPO is proven only at 8×H200 scale** (verl
    bench: ~252 s/step); single-H200 30B GRPO would be pioneering (BF16 weights alone ~61 GB ×
    trainer+rollout copies) — not the first move.
  - **Plan (his call: skip the 8B — train the model we actually run):** the 30B-A3B activates
    ~3B/token, so at inference it's CHEAPER than an 8B dense — there is no deployment win in the
    smaller model; its only advantage was trainability without rental, which the H200 removes.
    (1) **Gold-SFT-LoRA the 30B-A3B on one rented H200** (ZeRO-2, router frozen, ~63 GB@4K,
    hours/epoch over the 2,614-page gold set); (2) judge on the gold eval vs stock-30B;
    (3) RL only if SFT plateaus with measurable errors left — then it's multi-H200 verl GRPO on
    the 30B (the proven scale), with the synthetic-RLVR reward (edit-distance + formula-exact).
    The 8B survives only as a deep contingency, not a plan step.
  - **STEP 0, BEFORE ANY RENTAL — fix the temperature and re-baseline (his catch):** the lane ran
    Unsloth's CHAT samplers (temp 0.7) on a single-right-answer task; p.47 transcribed correctly in
    one run and wrongly in another — part of the 72%-major rate is sampling noise, not capability.
    Re-run gold-covered pages at temp 0/0.1/0.3 (keep a repetition guard — greedy makes loops
    likelier; presence_penalty exists for a reason), score mechanically against gold, and make the
    winner the lane's permanent setting. The SFT go/no-go and expected gains are measured against
    the LOW-TEMP baseline, never the chat-sampler one. Also: **hold out one full book** from
    training (train on 5, eval on the 6th) — the tune must generalize past these books' typography.
    Predicted gains by class: habits (stubs/headers/hyphens/eqno/HTML-tags) — high; domain vocab
    (acronyms, Latin/Cyrillic contamination) — medium-high; 150dpi perception limits — none (floor).
  **Open decision (when the OCR run finishes): teach the junk detector (G3.8) or the VLM first.**
  Grounding for the RL work: Lambert's RLHF book is in the corpus — ask_corpus its own handbook.

- **H1 — CONTEXT-AWARE CHUNK VALUE (his idea, 2026-07-14 — the best one on this page).**
  The judge asks *"is this chunk good?"*. The right question is **"does this chunk ADD anything?"**
  Value is **marginal, not intrinsic**: a beautiful passage that says what 40 others already say has
  near-zero marginal value; a scruffy OCR'd paragraph that is the *only* coverage of a topic is
  precious. Intrinsic quality and marginal value are nearly unrelated — and we have been optimising
  the wrong one.
    - *Redundancy:* chunks with many near-neighbours (cos > 0.95) are one chunk wearing many hats;
      they crowd each other in the top-k, so 8 retrieved slots deliver 1 passage of information.
      Semantic near-duplicate removal (the garbled PG books were the crude, string-level version).
    - *Coverage:* a chunk alone in its region of embedding space covers ground nothing else does →
      **protect from deletion, and BOOST in retrieval**.
    - **Why this is not just curation — it is a RETRIEVAL idea.** It explains the mice case exactly:
      the rodent passage is the ONLY enumeration of rodent species in the corpus (maximal marginal
      value), while the bats passage is one of many. Cosine ranks bats higher because it measures
      **resemblance**, and resemblance has no concept of *"this is the only place that says it."*
    - Read first: **MMR (maximal marginal relevance)**, coreset selection / data pruning.
- **H2 — item 21: distil the judge into a trained classifier**, with the judge demoted to
  last-resort tie-breaker on the uncertain band (his architecture). Kills the hand-written pre-filter
  and its blind spot. Blocked on nothing but discipline.
- **H3 — item 22: RAG-RewardBench / ReWordBench** (Lambert §5.8). Read before H2.
- **H4 — item 19: consolidate the two filters** (retire question-stripping from `clean-corpus.py`).
- **H5 — E1.5: fix the mislabelled fixture** (the gap was never English) + add `gap_ru_multiple_choice`.
- **H6 — RLHF book, read properly.** So far: §5.7 only.
- **H7 — NCBI Bookshelf / LibreTexts biology** (Alberts, Lodish, Cooper). Corpus is big enough.
- **H8 — `dkms install nvidia/580.159.03`** — the "differences between built and installed modules"
  warning behind the scary boot. Not blocking; do it on the ground, not the night before.
- **H9 — `paper.py` also ignores `chunk_token_num`** (checked 2026-07-14 after the `book.py` disaster —
  he asked whether other PDFs were affected). It chunks by SECTION (`title_frequency` → `sec_ids`),
  not by token count. Measured on `papers`: median **550 chars**, **13%** under 50, positions
  **434/434**. Degraded but NOT the catastrophe `book.py` was (median 47, 51% under 50, zero
  positions) — and for a 10-page paper, a section arguably *is* the right unit. ~64 junk chunks out of
  492. Not worth breaking the freeze; revisit if paper retrieval ever looks wrong.
- **H11 — CORPUS AS DESIGN CONSCIENCE, not just a search source (his ask, 2026-07-19).** His prompt,
  verbatim: *"regarding the corpus that ingested/being ingested - so far we have it as a pure search
  source, like find/synthesize facts. How to make the next step - so you and/or my qwens start to
  consider say the real sys design best practicies from the corpus when coding?"*
  **The gap:** the corpus is query-shaped; a coding task doesn't generate queries. When qwen writes a
  retry loop, DDIA's "retries need idempotency" sits unretrieved — the model doesn't know it's
  standing next to a question. A vague "consult best practices" DISCIPLINE line is the Axiom-2
  anti-pattern (prompt papering over a missing harness loop). Three mechanisms that fit the axioms:
    1. **Facet extraction at plan time** — a `consult_corpus(task)` tool: distill the task into 3–5
       design facets ("bounded vs unbounded queue", "crash consistency"), multi-query retrieve,
       return a HARD-CAPPED design brief (top principles + citations; Axiom 1 forbids an 18-chunk
       dump). Mandate it with a concrete trigger ("before writing the plan, call once") — qwen
       follows triggers, not advice.
    2. **Harness-run design critic over the DIFF (the bet).** Closed-loop, no model initiative
       needed: a script extracts facets from the produced diff, retrieves the principles, runs qwen
       AS JUDGE: "here's the diff, here are 3 cited principles — violated?" Judging against a stated
       principle is easier for a weak model than generating under unstated constraints — the same
       asymmetry as the curation judge cascade. Advisory pre-commit / `design-review.py <diff>`.
    3. **Distill a principles layer** — one offline qwen-next sweep over the design-heavy sources →
       a small derived KB of dense, citation-backed maxims ("bound every queue — DDIA/SRE"). Then 1+2
       retrieve over MAXIMS: precise, tiny in context, checkable, back-referenced to the book page.
       The retrieval unit finally matches the use.
  **Do not:** inject excerpts into every coding turn (context occupation), or add trigger-less prompt
  advice. **Validate:** eval-harness suite with a corpus-covered pitfall (unbounded queue,
  non-idempotent retry); measure catch-rate with/without. Judged, not admired.
- **H10 — ingest is DeepDoc-bound, not embedding-bound.** Measured while re-parsing: CPU 94% (task
  executor at 1298%, i.e. 13 of 24 cores), **GPU at 0%**. The 10× chunk reduction cut a stage that was
  already free. If ingest speed ever matters, the lever is DeepDoc's per-page layout pass — a lighter
  layout recognizer, or more executor parallelism — NOT chunk count.

### 2026-07-15 — CLEAN BASE re-measure (single code version, all PDFs re-parsed) ✅
First unconfounded recall@k since the 40% baseline. Corpus: books/bio-books re-parsed with the
book.py + space fixes (median chunk 47→~2400, positions 0→100%, figures 100%); Russian KBs stemmed
+ page-marked.

| metric | 2026-07-13 | now |
|---|---|---|
| recall@8  | 40% (2/5) | **60% (3/5)** |
| recall@64 | 80% (4/5) | **100% (5/5)** |
| recall@256| 100%      | 100% |

- **recall@64 = 100% is the win**: every gold passage is now within the reranker's reach. The first
  stage no longer sets a losing ceiling — Phase 2 (parallelise reranker → 256 pool → wider slice) can
  now surface all of them.
- photosynthesis rank 16→1 (chunking+stemmer); lsn-general climbed into the pool (Postgres re-parse).
- mice still miss@8 (rank 30/31): bio Russian KB unaffected by these fixes, AND it's the
  topical-similarity/bats problem, not a pool problem. Confirmed not retrieval-fixable.
- NOTE the dataset-level chunk_count in RAGFlow is STALE after delete+re-ingest (showed 74k/60k;
  real per-doc sums 5,886 / 3,808). Sanity-check the summary counter against per-doc totals.

### 2026-07-15 — BUMPED retrieval to 64 (G1.3-lite, the safe half) ✅
`oracle-ask-mcp.py`: `_retrieve` page_size 20→64 (return the full reranked pool); `_diversify`
main 8→18 (feed the top ~22 chunks to synthesis). recall@64 is 100% and the gold ranked 15-18 —
retrieved then dropped before synthesis by the old narrow slice. Rerank at 64 is ~10s, inside the
30s timeout, so this needs NO reranker parallelisation (that's only for the 256 bump).
- **Verified:** `какие виды грызунов` now answers with the full, correct rodent list (rodent-list
  chunk reaches slice position 10). The old main=8 slice dropped it (rank 15).
- **BUT the model still miscategorises:** it appended "летучие мыши относятся к отряду Грызуны" —
  bats are Chiroptera, not rodents. Retrieval is now correct; the residual error is the synthesis
  reasoning ceiling (see the 2026-07-15 reflection entry). Widening the slice cannot fix a category
  error the model makes over correct evidence.
- Context cost: ~22 chunks (~50KB) to qwen — a real Axiom-1 load, accepted for the recall. The 256
  bump (item 10) still waits on the reranker fix (items 12 + G1.2).

- **H11 — OFFLINE FACT SOURCE (Wikipedia), the answer to "the books can't do factoids".** School
  biology textbooks are CONCEPT sources, not almanacs — they discuss the giraffe's neck as an
  evolution example and its 7 cervical vertebrae, but never its length; no mouse-species list. No
  retrieval fix helps: the fact isn't in the text. ⇒ Add a **separate** offline fact layer.
  - **Shape: a `wiki_search` MCP tool over a Kiwix ZIM** — NOT a vector KB. The ZIM ships its own
    Xapian full-text index (no embedding/chunking), and full-text is BETTER for factoids ("giraffe"
    → the article → the fact is right there). ~50-line MCP wrapper (kiwix-serve HTTP or libzim).
    Parallel to ask_corpus/ask_code — retrieval method matched to source: vector=concepts,
    fulltext=factoids, grep=code.
  - **DO NOT** ingest Wikipedia into a RAGFlow vector KB — millions of articles swamp/dilute the
    technical corpus and blow up the reranker (same lesson as the bio-books diluting PG).
  - **Bias filter (his ask): keep only science trees.** Query-time category filter over the full ZIM
    (reversible): allowlist Biology/Chemistry/Physics/Math/Astronomy/Earth-sci/Tech/Animals/Plants/
    Anatomy trees; drop Politics/Government/Wars/Elections/Countries/Living-people. Fuzzy (multi-cat
    articles, cyclic graph → ~95%, pick a depth); natural+formal sciences core is the clean part.
    Alternative for pure organism facts: Wikispecies / EOL (politics-free by construction).
  - **Ladder:** wiki_search → else qwen parametric with a "(general knowledge, not corpus)" tag →
    else abstain. Ties to item 5 (de-scope DISCIPLINE): the model KNOWS giraffe≈2m; grounding forbids
    it. Route by question type — technical=strict grounding, world-knowledge=wiki/parametric.

### 2026-07-15 — §G code sweep: finished every code-fixable flight-critical item ✅
Worked §G top-to-bottom. Shipped this session (all committed):
- **G1.4/G1.5/G3.2**: retrieval slice widened to 18, query normalization (strip filler; verified
  "какие виды X ты знаешь"→"виды X"), output language pinned (stops qwen's Chinese leak).
- **G1.1/G1.6**: reranker fallback already tagged visible; recall re-measured on the clean base
  (@8 40→60%, @64 80→100%).
- **G3.3**: `search_corpus` MCP tool shipped — top-k passages verbatim, no synthesis.
- **G2.2** (items 1,2,6,7,8) + **G2.4** (item 5): the full closed-loop set — routing debias +
  de-scope (qwen.sh, passes shellcheck/shfmt), source_search accepts the graph slug, auto-relax on
  a too-strict anchor, absolute paths for Read, ask_code redirect on scoped miss.
- **G2.3**: item 3 (rerank grep hits verbatim via :9760 — the definition now outranks the comment,
  verified) + item 4 (already satisfied: ask_code extracts clean graph fields, no fp/sp/bt).
DESIGN §5.2/5.3/9.0 + BLOG Act 13 updated.

### 2026-07-16 — the corpus browser, built for real (G3.5) ✅
The must-have. A grounded answer is only trustworthy if you can VERIFY it against the original, offline
— so the browser closes that loop. Shipped (commits `e66e6c8`, `791ed55`):
- **Search → the rendered page, not the chunk.** Results embed the actual PDF page image (`pdftoppm`,
  200 dpi), because reading reconstructed `pdftotext` (re-wrapped, page-marker noise, diagram shards)
  "sucks." Page comes from DeepDoc bbox or the `[[p.N]]` markers.
- **Highlight the query on the page.** Anchor nouns are boxed on the page image (word bboxes via
  `pdftotext -bbox`, positioned as page-fraction %) and `<mark>`ed in markdown/text. Cyrillic-stemmed
  (`мышей→мыш`, `виды→вид`) with a conversational stoplist, so *"какие виды мышей ты знаешь"* lights up
  `вид`/`мыш` and nothing else.
- **Markdown reads like a page too.** GitHub-flavoured render (front-matter stripped), framed in serif
  so it sits beside the PDF renders without clashing; `/md/{doc}` opens the full doc centred, scrolled
  to the passage (`#hit`), with a **left nav tree** of its directory so you can keep reading.
- **Folded in miniserve.** `/browse` + `/raw` are the corpus folder tree, opening each file in the
  right viewer; the old miniserve on `:9800` (`oracle-docs.service`) is stopped and disabled.
- **Real names, native paging.** Headers show the source PDF filename / md front-matter title (not the
  `<subdir>__<file>.txt` slug); the viewer flips pages **in place** with ←/→ (decode-before-swap, no
  flash) and precaches ±3 neighbours.
- **A bug the browser exposed:** apparatus (index/TOC/bibliography) out-ranks real content on keyword
  queries because it is the densest possible keyword match. Extended the judge to DROP apparatus and
  swept it — plus 108 unambiguous TOC chunks (≥4 dotted-leader lines) deleted directly. `raft` no
  longer returns a table of contents.
- **Closed the ingestion loop for it.** The manual deletions were post-hoc against the live index; on
  re-ingest, apparatus comes back (RAGFlow's parser is a black box — curation is necessarily
  post-parse). So: folded the ≥4-dotted-leader TOC rule into `chunk_judge.is_obvious_toc` (a
  deterministic drop, no judge call), and gave `ingest-corpus.py` a `--curate` flag that runs the
  `clean-chunks.py --judge` sweep on every KB after parsing — curation is no longer a step to
  remember.

### 2026-07-16 — G3.1: corpus-wide judge run, finished and audited ✅
Swept all 17 KBs through `clean-chunks.py --judge` (apparatus-heavy books first). **~1,687 chunks
deleted, ~26k boilerplate lines stripped**, every DROP audit-logged.
- **The book KBs — `books`/`bio-books`/`bio`/`postgres` — deleted 0.** They were swept during the
  browser work; the judge re-ran and cut nothing. That's the load-bearing result: **no false
  positives**, no real knowledge lost on a re-run.
- **Biggest yield `emacs` (1142):** GNU Info-manual **indexes** (`* calc-date: Date Conversions.
  (line 12463)` — name→node→line pointers) and the Emacs FAQ's bare question-headers. Textbook
  apparatus — an index that points elsewhere and answers nothing.
- **`cpp` (269):** cppreference `### References` blocks (standard-document page pointers) + redlink
  stubs. **`linux` (95):** course syllabi, key-value dumps, Bash-variable indexes. `go`/`rust`
  (15/27): link-farms, RFC TODO templates.
- **Audit:** sampled the content-shaped DROP reasons by hand — all correct (cross-reference/index
  sections, not prose). The judge's *"if unclear, KEEP"* bias plus the recall-oriented pre-filter
  held; a couple of borderline narrative/exercise calls, no systematic loss.
- This also exercised the new deterministic `is_obvious_toc` path and the spaced-dot TOC fix.

**Deliberately NOT done, with reasons (not forgotten):**
- **G1.2/G1.3** (parallelise reranker → 256 pool): infra. The 64 bump already banked recall@64=100%;
  256 needs the reranker parallelised first and buys little now. Deferred, not blocking.
- **G2.1/G2.5** (run the 4 EVAL suites vs qwen, before/after): a TESTING activity needing a live
  `qwen` session with the new prompt/tools — a human-in-the-loop run, not a code edit. Do next time
  the local agent is driven.
- **G3.1** (corpus-wide judge): bio judged (221 cut) + validated; full re-run on the clean corpus is
  hygiene with NO measured retrieval benefit (2026-07-15 log) — low priority.
- **G3.4** (`oracle-ctl.sh resume` from cold): needs an actual reboot to test; `status` is clean.
- **G3.5** (corpus browser): ✅ **built** (2026-07-16 log). Search → the rendered source page with the
  query highlighted; folds in the old miniserve folder view.
