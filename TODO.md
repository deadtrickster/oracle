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
      qwen judge + glyph detector; output drives keep/delete/excise/strip. Feature extractor is BUILT:
      `build-junk-features.py` (read-only, `q_1024_vec` + 38 surface features → .npz; probed OK; incl. stopword/sentence-end prose-ness, alphabetized-index order, answer-key/bibliography patterns, code-ratio [the jsonb `?` scar], title-overlap, page position via page_num_int).
      Raw-collection probe (2026-07-19) validated the signals (flagged ToC = real ToC; index-ish at
      alpha-sorted 0.90 / stopwords 0.08 vs prose 0.28) and grew the taxonomy: **ocr-damaged-code** —
      numbered code lines OCR'd from PDF page images (`GridPa e`, `R0UND`, fullwidth `，`). Keep-vs-drop
      hinges on code_ratio × is_pdf × weird_density — a three-way interaction rules can't weigh; model
      territory. **Sequencing:** assemble the labeled set anytime (read-only ES scan; judge
      calls at a quiet moment — they share the 30B with coding); train/score only after the collection
      ingest drains (CPU contention). DESIGN §4.3.
- [ ] **G3.7** — **RETURN AND PATCH DEEPDOC (deferred, like the word-boundary fix).** Root cause: the
      onnx layout model (`deepdoc/parser/pdf_parser.py`, `_layouts_rec`) **mislabels a diagram as
      `text`**, so its OCR is flattened inline instead of being pulled out as a `figure`
      (`_extract_table_figure`, `rag/app/{paper,book}.py:54`). The box-level garbled-text filter at
      **`pdf_parser.py` ~810** already has Strategy 1 (PUA/CID) + Strategy 2 (font-encoding) that clear
      a box → OCR fallback; add a **Strategy 3** (box-drawing/replacement-glyph density + long repeat
      runs → drop the box *before* chunk assembly), so no mixed chunk is ever formed. Change is inside
      the RAGFlow container against pinned **v0.26.4** — do it when we next touch the parser, not now.

---

# H. PARKED (good ideas, deliberately not being built)

Written down so they cost nothing to leave alone. **Do not start these.**

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
