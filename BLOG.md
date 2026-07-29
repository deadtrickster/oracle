# Blog post sketch — "Oracle: a plane-proof reference brain on one laptop"

Working title options:
- *An offline reference brain: grounding a local LLM so it stops lying to you*
- *24 GB of VRAM, 125 GB of RAM, and no internet: building a coding oracle for the plane*
- *The model is the weak link — design around it*

Audience: systems/infra engineers who run local LLMs and are tired of confident hallucinations.
Tone: honest, measured, war-story. Show the failures and the fixes, not a glossy tutorial.

---

## Hook (the failure that motivates everything)
Open cold: I asked my local 30B model "what is `pg_last_wal_replay_lsn`?" It gave a beautiful,
well-formatted answer — and mislabeled it as the checkpoint LSN. Confident. Wrong. That's the
whole problem with local LLMs in one screenshot: they're fluent and they lie about specifics. I
wanted a reference brain I could trust on a plane with no internet to fact-check it. So I built
one, and the interesting part isn't the RAG — it's everything I did to stop the weak model from
hurting itself.

## Act 1 — The constraint that shapes the architecture
- One laptop: RTX 5090 24 GB, 125 GB RAM, 24 cores. Offline.
- The insight: **LLM inference is memory-bandwidth-bound; RAG is capacity-bound.** So split by
  appetite — fast VRAM for the model, abundant cheap RAM/CPU for everything else.
- Why this beats unified memory (Apple / DGX Spark): on a unified box the weights and the RAG
  data fight over one pool; the split gives each what it wants. Numbers: GDDR7 ~1.8 TB/s vs
  Spark's LPDDR5X ~273 GB/s. Capacity is useless if it's slow.
- Diagram: GPU = qwen + query-embedder; CPU/RAM = parsing, vector store, reranker, code graph.

## Act 2 — Grounding, because the model can't be trusted
- The two failure modes: wrong chunk retrieved, and hallucination while generating.
- Fix 1: **a reranker.** Show the measured win — the authoritative header went rank 3 → rank 1.
  Aside: benchmarking rerankers (bge-m3 was 14s on CPU — too slow; a multilingual MiniLM/GTE at
  ~1–3s was the answer; the "just google the error" moment when the model wouldn't load).
- Fix 2: **extract-then-answer.** Force the model to quote verbatim facts first, then answer only
  from them, or admit the gap. Package it as one tool (`ask_corpus`) so even a weak caller can't
  skip it. Show the before/after: mislabeled LSN vs a cited, correct answer.
- The thesis line: *a model is only as exact as its grounding — and this is true whether the model
  is a local 30B or a frontier one.*
- The catch: docs aren't the whole truth. "What WAL records does OrioleDB have?" isn't in *any*
  doc — it's an X-macro in the extension's own source, and `ask_corpus` correctly *abstains*. So
  there's a twin primitive, `ask_code`: same extract-then-answer discipline, but it greps the
  actual source (`--sort=path` so *definitions* outrank *usages*) and cites `file:line`. Routing
  matters: send a source question to the doc corpus and you get a confident, wrong answer about
  PostgreSQL's WAL instead of OrioleDB's.

## Act 2½ — When grounding isn't enough, ask the compiler
- Here's the humbling part: even handed the *exact right lines*, the 30B model still miscopied a
  value table — it renumbered an enum whose real code was 15 down to 8. Grounding put the truth on
  screen; the model fumbled the transcription. Grep finds text; it doesn't *resolve* symbols.
- The fix is a different kind of oracle: a **language server**. rust-analyzer/clangd/gopls *are*
  the compiler's ground truth. `lsp_hover(file, line, col)` returns the resolved type/value the
  compiler *knows* — no transcription step to fumble. "LSP for truth, LLM for intent."
- The fun twist on top: language servers already ship refactorings ("Extract into function",
  "Inline variable"). I don't replace them — I let the local model *reason over the server's real
  action menu*: `suggest_refactor` asks rust-analyzer what's actually available here, then has qwen
  pick one **by its exact title** and explain why, plus add the judgment calls (naming, structure)
  the compiler can't make. Deterministic, compiler-safe mechanics; LLM for the intent. The model
  chooses among *real* refactors, never imagined ones.

## Act 3 — The model is the weak link; scaffold around it
- Give a weak model open-ended agency and it spirals (real example: asked for a package, it
  web-searched, re-read a 1700-line file twice, blew its context window, forgot the question).
- The pattern that works (borrowed from a watchdog tool called C3L): deterministic control loop,
  one small scoped task at a time, tool schemas that validate. Don't trust the model to decide
  when it's done or which tool to call — make the harness do it.
- Concretely: scoped agents (an ingestor that classifies+routes files; a grounded Q&A agent);
  disabling/teaching the tools the model malforms; trimming context; capping the reranker's
  candidate set.

## Act 4 — Running Claude Code on a local model (the fun twist)
- Ollama now speaks the Anthropic API natively — so the *entire* Claude Code harness (context
  mgmt, tools, subagents, MCP, hooks) runs on local qwen with three env vars. **No proxy.** I was
  proud of that line. Keep it in mind.
- The harness assumes a strong model; qwen underperforms exactly where it's most ambitious. Same
  lesson as everywhere: wire it minimal, keep tasks scoped, and hand it a **routing discipline** in
  the system prompt — docs → `ask_corpus`, a repo's own source → `ask_code`, an exact symbol
  type/value → `lsp_hover`, a refactor → the language server's action menu. Never answer from
  weights. (I saved that routing as a memory so my *real* Claude Code uses these tools too.)

## Act 4½ — The "no proxy" line does not survive contact with reality
- Timeline, because this is the fun part. First tools worked. Then, after a restart: **every tool
  call a red dot, no result.** My first guess — stale MCP connections after I'd restarted some
  servers — was wrong. The real symptom showed up next: the model printing raw
  `<function=mcp__oracle-ask__ask_corpus>` XML as *text*, with a stray `</tool_call>` hanging off
  the end. The tool call wasn't failing; it was never being *parsed*.
- So I measured, because guessing had already burned me once. A 2×2: Anthropic vs OpenAI endpoint,
  streaming vs not, six-plus runs each under a realistic 14-tool load. The result was clean and
  damning: **Anthropic + streaming leaks the tool call as text ~33% of the time; every other cell
  is ~0%.** Claude Code only speaks streaming-Anthropic — it walks straight into the one broken
  quadrant. And I was already on the latest Ollama; there was no upgrade to hide behind.
- The fix is the thing I bragged about not needing: a proxy. A thin **shim** that takes Claude
  Code's streaming-Anthropic request, calls Ollama's *OpenAI* endpoint (the robust path) with real
  streaming, and translates the events back. "No proxy" was an aesthetic, not a requirement — and
  correctness doesn't care about my aesthetics.
- One more twist, because local models are relentless: even the OpenAI endpoint leaks ~5% under
  load. So the shim gets a **salvage parser** — when qwen dumps its tool call as text anyway, a
  regex recovers the `<function=NAME><parameter=…>` XML into a real `tool_use` block (coercing
  `line: "21"` → `21` on the way). Belt, then suspenders. Measured after: **12/12 clean.** From a
  third of calls silently dropped to none.
- The synthesis: offline Claude Code + your code graph + your grounded corpus + an 80-line shim
  that makes a weak model's tool calls actually land = a self-contained offline coding agent.
  Weaker than the real thing, but real, and yours.

## Act 5 — Make it feed itself
- The system ingests its own material: point an agent at a folder/PDF/URL and it classifies,
  routes, and parses. Multilingual (my PostgreSQL books are in Russian — the reranker choice
  hinged on that). It even ingests its own design docs so it can explain itself offline.

## Act 6 — The corpus poisons itself (and my three wrong diagnoses)

I added biology books, mostly to see whether the thing generalises past code. It does — and biology
promptly broke every assumption, because it's the only corpus I have that is in an inflected
language, half OCR'd, and written as a *textbook*.

I asked it, in Russian, "what species of mice do you know?" It came back with a confident,
cited list of the family **Muridae**. The list was wrong. Chasing that one answer took four
diagnoses, three of which were mine and wrong:

- **"The corpus doesn't have it."** Wrong — it did, in a book that had been there all along.
- **"Add an abstention floor: refuse when similarity < 0.42."** Wrong, and I'd have shipped it. The
  corpus *could* answer; a floor would have refused a good question. (In-corpus queries score
  0.47–0.65, out-of-corpus 0.24–0.36 — a beautiful, clean separation, and a trap. The one case I
  cared about scored 0.35 *and was answerable*.)
- **"Hybrid search is broken — BM25 is dead."** Wrong: I'd passed a parameter name that doesn't
  exist, so the API silently ignored it and every score came back identical. I diagnosed a system
  bug from my own typo.

What was *actually* happening is the thing I now can't unsee. **A user's query is a question. A
textbook's "Вопросы для повторения" section is also questions.** They embed close together — so the
book's own exercise questions out-compete the passages that answer the query. One of my books was
**13.6% question lists**. Six of the top thirty hits for "what is photosynthesis" were exercise
questions. I was retrieving the quiz instead of the chapter.

So I strip them before ingest: a run of three-plus consecutive interrogative paragraphs is exercise
material; a lone rhetorical question in prose survives. Plus page-range exclusion for answer keys
and indexes, which is only possible because I now carry `[[p.N]]` markers through OCR and
text-extraction — the same markers that let a citation say "p. 412" and let a browser open the PDF
to that page.

And then the honest part, which is the only reason this act is worth writing: **it didn't fix the
mice.** After cleaning, the rodent passage (cosine 0.471) *still* loses to a passage about
**Рукокрылые** — bats, which in Russian are literally *летучие мыши*, "flying mice" (0.762). The
embedder isn't malfunctioning. It is doing precisely what a bi-encoder does: scoring **topical
proximity, not answerability**. It cannot know that one passage *answers* and the other merely
*resembles*. Only a cross-encoder, which reads query and passage together, can. My own corpus told
me this, out of Jurafsky & Martin, when I finally thought to ask it instead of theorising.

The real bug, it turned out, was never in retrieval at all. Retrieval had honestly reported *"the
corpus doesn't cover this."* The model then synthesised a taxonomy on top of that abstention — it
had invented the word "Muridae" in its own search query, retrieved a passage explicitly labelled
**Отряд Грызуны** (the *order* Rodentia), relabelled it to match the premise it had just made up,
and served it with a citation. A hallucination wearing a footnote is worse than a naked one.

## Act 7 — I built a library and then refused to read it

The stupidest and best moment of the whole evening.

I'd spent hours theorising about *why* my retrieval was picking the wrong passages. My own corpus
contains Jurafsky & Martin's *Speech and Language Processing* — the actual textbook on this exact
subject. It had been sitting there, indexed, the entire time.

So I asked my system. It gave me a fluent, confident paragraph — and, as supporting evidence, **a
Python function it had invented on the spot.** Useless.

Then I did the obvious thing: I turned the synthesis **off** and read the raw passages myself. Two
sentences, straight out of the book:

> *"The bi-encoder … is less accurate, since its relevance decision can't take full advantage of all
> the possible interactions."*

There's my bug, stated as **architecture**, not as a defect. A bi-encoder reads your question and the
passage *separately* and compares summaries. It measures **resemblance**. It structurally cannot
measure **answerability**. Bats beat rodents because bats are *летучие мыши* — "flying mice" — and
resemblance is all it has.

> *"Use cheaper methods (like BM25) as the first pass … then use expensive methods … to rerank only
> the top N."*

And there's the thing I'd completely missed: **the first stage sets the ceiling.** Reranking can only
reorder what search already found. My correct passage was never in the top 64 — so every reranking
experiment I'd run that evening was carefully tuning the order of a list that *didn't contain the
answer*. Hours of it.

qwen's summary contained neither sentence. It couldn't have — **summarising is discarding**, and it
discards precisely what it doesn't recognise as important. I had put my weakest component between
myself and my books.

The lesson generalises past this box: **the summary is for the reader who can't afford the source.**
I could afford the source. The compression step existed to protect a 56K-context model, and I'd let
it protect *me*, from my own library.

So now there are two doors: `ask_corpus` (synthesised, for weak callers) and `search_corpus` (the raw
passages, verbatim, for anyone who can actually read). The books were right there the whole time.
They were the best tool in the building and I was talking to a summary of them.

## Act 7¼ — The bug was fifty years old, and so was the fix

I finally built the instrument I should have had from the start: a `qrels` file — for each test
question, the passage that *ought* to win — and one number, **recall@64**. Does the search stage even
*find* the right passage, before any model gets near it?

```
                           R@8    R@64   R@256   rank  after-rerank
mice-species              miss    miss     HIT    101        13
photosynthesis            miss     HIT     HIT     16         1
lsn-general               miss     HIT     HIT     17         8
lsn-replay-fn              HIT     HIT     HIT      3         1
auto-ptr                   HIT     HIT     HIT      3         1

  recall@8  = 40%     ← and the top 8 is what I hand the model
```

Forty percent. **The passage that answers the question isn't in the model's context 60% of the
time.** Every "the model hallucinated" complaint I'd logged needed re-reading in that light: a lot of
the time, it never had the answer to work with. I'd been blaming the model for a failure that
happened two stages upstream.

But *why* was the mice passage at rank **101**?

My search is hybrid — part vector similarity, part old-fashioned keyword matching. And the keyword
half carries **70% of the score by default**. So I looked at what the keyword half does with my query,
and it does this:

```
'photosynthesis running runs'  ->  'photosynthesi run run'     ← stemmed
'мышей'                        ->  'мышей'
'мышь'                         ->  'мышь'
```

English gets Porter stemming. **Russian gets nothing.**

My query says *мышей* (genitive plural). The book says *мышь* (nominative). To the keyword index
those are two unrelated strings, and they never match. So the **one** informative word in my question
matched **nothing** — while *виды* ("species"), a word that appears on nearly every page of a biology
textbook, matched everywhere and steered the entire query into noise.

A friend put it to me as a question: *isn't that solved? Frequency is inverse to importance.* Yes. It
is. **Spärck Jones solved term weighting in 1972** — rare words matter, common words don't, that's
IDF, it's in every textbook including the ones in my corpus.

And IDF was working perfectly. *мышей* had a magnificent, rare, high-IDF score.

**It just matched zero documents.** IDF cannot rescue a term that never matches. Term weighting and
stemming are two halves of one idea, and I had shipped exactly one of them.

The fix is eleven lines: run the Russian Snowball stemmer over Cyrillic tokens, on **both** sides —
because a stemmer is worthless unless it produces the *same invariant* for the query and the
document.

```
мышь / мыши / мышей / мышам / мышью  →  мыш       one invariant
мышца / мышцы                        →  мышц    ┐ disjoint — mouse
мышечный                             →  мышечн  ┘ does not collide with muscle
```

Snowball for Russian dates to the early 2000s. Porter's algorithm is from **1980**. The bug and its
remedy are both older than most of the stack I built on top of them — I had a GPU, a 30-billion
parameter model, a vector database and a cross-encoder reranker, and I was defeated by a **suffix**.

There is a lesson in there about where I chose to look. I spent the evening interrogating the
*newest* and most glamorous part of the pipeline — embeddings, reranking, chunking, prompts —
because that's where I assumed the interesting failures live. The failure was in the boring part.
The boring part was fifty years old, extremely well understood, and simply **absent**.

## Act 7½ — The punchline: it got me too

While writing the section you just read, I described the bats passage in an English sentence as being
"**литерально** 'flying mice'".

Литерально. In an English blog post. I had been staring at qwen's Russian-Chinese hybrid output for
hours, quoting it, pasting it into my notes — and then I code-switched into Russian **in exactly the
way I had spent the evening documenting as qwen's bug.**

I noticed and quietly fixed it, which was the wrong instinct. It isn't a typo. It's data.

Because it means cross-lingual leakage isn't a quirk of a small Chinese-trained model. It's what
happens to *any* model whose context is saturated with another language — and the corpus I'd been
swimming in was half Russian. qwen does it every few paragraphs. I did it once in an evening. **Same
failure, different rate.** The only variable is tolerance.

Which lands somewhere I didn't expect when I started this project. I'd been treating the local model
as the fragile one and myself as the reliable reader — the whole architecture assumes that. But
context doesn't just *distract* you; it **contaminates** you. Whatever is in the window leaks into
the output. That's true at 56K tokens and it's true at a million; the constant is different, the
law isn't.

So "pin the output language in the prompt" stops being a crutch bolted onto a weak model, and
becomes what it always was: **the correct behaviour of a harness that knows its corpus is
bilingual.** Written for the model that needs it. Which, it turns out, is all of them.

## Act 8 — I stopped writing rules and hired a judge (and then my thesis failed)

The corpus filter I'd been building was a pile of regexes, and every single one encoded the *surface
form* of the thing instead of the thing:

- I assumed **`?` means question** — and nearly deleted a PostgreSQL operator table, because `?`, `?|`
  and `?&` *are* jsonb operators.
- I assumed **a question contains `?`** — and silently missed every multiple-choice item in the
  corpus (`A11. Корнеплод — это... 1) ... 2) ...`). No question mark anywhere. My "cleaned" biology
  books were still full of quiz questions and I had no idea.
- I'd already assumed **`мышей` is `мышь`** — and it wasn't, until a stemmer made it so.

Three variations on one mistake, in a single day. *Purpose cannot be compiled into a regex.*

So I replaced the rule with a **judge**: qwen reads each candidate chunk and decides whether it exists
to *inform* the reader or to *test* them. The prompt is adapted from MT-Bench — which I found in
Lambert's RLHF book, in my own corpus, which I'd ingested that morning. Explicit criteria, an
explanation before a strictly-formatted verdict, and a warning not to be swayed by length.

Two details I'd defend:

**It's a cascade, not a replacement.** Rules can't score 283,000 chunks *well*; a 30B model can't
score them *fast*. So the rule stays as a deliberately over-eager pre-filter — flag anything
questionish, tolerate false positives — and the judge rules on the survivors. **1.7% of chunks get
judged: 26 minutes instead of 26 hours.** It's the same retrieve-then-rerank cascade the textbook
gave me for search, pointed at curation instead.

**On any error, the judge votes KEEP.** A judge that fails must never become a silent deleter. And
every verdict goes to an audit log — swapping a rule I can inspect for a model I can't would be a bad
trade.

It scored 7/7 on the labelled fixtures. It caught the multiple-choice questions. It kept the operator
table. It kept an author's *preface* that happened to be three rhetorical questions in a row — the
thing any `?`-counting rule would have shredded — because it read what the passage was *for*. On one
book it rescued a quarter of what my rule had flagged.

**And then it didn't work.**

I deleted 221 exercise chunks from the biology corpus, re-ran the measurement, and the passage I'd
spent two days chasing moved from rank **32 to rank 31**.

Nothing. My corpus-poisoning thesis — the one I'd been so pleased with, the one that produced the
best line in this post about the book's quiz beating the book's chapter — **had just failed its own
test.**

And the reason was sitting in my notes from six hours earlier: the passage beating my rodent list was
never a quiz. It was **bats**. *Летучие мыши.* "Flying mice." Deleting quizzes cannot do a thing about
a passage that is genuinely, legitimately similar.

The corpus is cleaner, and I'll defend that on its own terms — 221 quiz chunks and 22,000 garbled
duplicates gone is less noise in every context window I fill. But it bought **no measured retrieval
win**, and the honest entry in the log says so.

I only know that because I wrote down, *in advance*, what result would prove me wrong. I'd added a
line to the plan that morning: *"if recall doesn't move, say so plainly — do not bank it silently."*
Past-me knew exactly who he was dealing with.

What the judge *did* earn is worth keeping separate from what it didn't. Set loose on the real corpus,
it cut **286 chunks** from the OpenStax biology textbooks — the multiple-choice review questions no
rule of mine could see — and **zero** from Rust Patterns, Database Internals and Latency. It tells a
textbook from a reference manual without anyone telling it which is which. That's real. It just isn't
a retrieval fix, and I'd been about to sell it as one.

### The coda, which is the same joke a fourth time

Reviewing that corpus-wide run, one line bothered me. The Postgres books: **"0 chunks deleted."**

Not "few". Zero. From 219 documents.

I had written `documents?page_size=100` and never paginated. The API returned the first hundred
documents, my script judged those, and reported a clean result — while **119 documents, including
every single Postgres book, were never fetched at all.** The "0" wasn't a finding. It was my own
truncation, wearing the costume of a finding.

Which is precisely the bug I'd spent the previous evening cursing RAGFlow for: its parser silently
refuses files over 128 MB and still reports `DONE, progress: 1.0` — success, zero chunks. I wrote a
whole section about how outrageous that was. Then I did it to myself, in a script whose entire purpose
was cleaning up after silent failures.

**Silence read as success.** Every real bug this week has been a variant of it: a cap that reports
completion, a reranker that times out and quietly returns the unranked list, a stemmer that isn't
there so a word matches nothing, a garbled book that indexes fine. None of them threw an error. All of
them just... quietly did less, and said it was done.

If there's one thing I'd take off this laptop and apply to any system, it's that. **Make your
components incapable of failing quietly.** The bug you can see costs an afternoon. The bug that
reports success costs you a thesis.

## Act 9 — "Why do four English books make ten times more chunks than six Russian ones?"

He asked it in passing. It's the best question anyone asked all week.

I'd just noticed the biology textbooks were producing an absurd number of chunks and had filed it
under *"DeepDoc is granular, I'll look later."* He looked at the same numbers and saw that they didn't
add up — four books, sixty thousand chunks; six books, six thousand.

The chunk is the unit of truth in a retrieval system. It's the thing you embed, the thing you rank,
the thing you hand the model. So I measured the median chunk size, which I had never once done:

```
naive parser (the Russian books)  ->  1168 characters
book  parser (every English book) ->    47 characters
```

Forty-seven characters. Here is an entire chunk, indexed and embedded as though it were a passage of
Jurafsky & Martin:

```
133 The nature of preferences10 reward functions 138
```

That's a **table-of-contents line**. Half of SLP3's chunks are under fifty characters. Not one reaches
a thousand. **Roughly 126,000 of my 300,000 chunks are page numbers, running heads and TOC fragments**
— and they're concentrated in my *best* books: Jurafsky, Kleppmann, Sutton & Barto, CLRS.

Both parsers were configured `chunk_token_num = 512`. The `naive` one obeys it. The `book` one takes a
different code path — `hierarchical_merge`, chosen whenever the document has headings, which is to say
*for every textbook ever written* — and that function **never reads the setting at all.** It merges
against a hardcoded 218-token limit and only merges groups of size one; anything else is emitted
as-is, however tiny. The single branch that honours `chunk_token_num` was dead code for real books.

So: the setting was accepted by the API, stored in the config, echoed back to me on request, and
silently discarded by the code that actually ran. **Nothing errored. Nothing warned.** (The fix is
now upstream as [infiniflow/ragflow#16959](https://github.com/infiniflow/ragflow/pull/16959),
closing their #12109 — turns out everyone's textbooks were 47 characters long.) It is the same
bug as the parser that reports `DONE` on zero output, the reranker that times out and returns the
unranked list, the stemmer that isn't there. **Silence read as success**, one more time, and this time
it was quietly wrecking every English book I own.

And the consequence lands exactly where I'd been struggling: retrieval hands the model its top eight
chunks. Eight times fifty characters is **four hundred characters of debris**. I have been asking a
model to answer from a page of table-of-contents lines, and then studying its hallucinations with
great interest.

The fix looked like four lines: take the branch that respects the setting. I wrote that down in the
design doc, confidently, including the claim that I'd keep DeepDoc's page positions — the ones the
corpus browser needs to open a citation at the right page.

Then I tested it, and every chunk came back with **zero positions**.

Because there was a *second* bug on the same branch. The position tag DeepDoc embeds looks like
`@@page\tx0\tx1\ttop\tbottom##` — note the **double** at-sign. The code splits it on a **single** one:

```python
"foo@@1\t2\t3\t4\t5##".split("@")   →   ["foo", "", "1\t2\t3\t4\t5##"]     # three parts, not two
```

The code then checks `if len(parts) == 2` — which is now false — and silently drops the position.
Every chunk down that path loses its page mapping.

And *that* is why nobody had ever noticed the branch was broken: **it was already dead code.** No real
book ever reached it, so both bugs sat there, undisturbed, waiting for someone to fix the first one
and discover the second.

Split on `@@`, and it all works:

```
              unpatched   patched
chunks             637        66
median chars        47      2302
under 50 chars      51%        0%
page positions    0/66     66/66
```

Sane chunks *and* the page mapping. The trade-off I'd been dreading — good chunks **or** a working
browser — never existed. The code was just wrong in two places.

Worth sitting with: I had *already written the claim into the design document* before I tested it. If
I hadn't gone back to verify a thing I'd asserted as fact, I'd have shipped a corpus with no page
positions and a document confidently explaining why it had them.

The reason I hadn't looked is the reason I never look: **chunk size is boring.** It isn't the
embedding model, or the reranker, or the prompt. It's a number in a config file. That's twice this
week the bug has been in the least glamorous component available — first a missing stemmer from 1980,
now an integer that nobody reads.

## Act 10 — "Why can't it read Russian?" (the folklore, and the space that wasn't there)

For weeks I'd been routing the Russian books through `pdftotext` instead of the fancy layout parser,
on the strength of a note I'd left myself: *"DeepDoc garbles Cyrillic."* It had the ring of truth. I
never checked it.

Then: *"why can't we extend DeepDoc to handle Russian?"*

So I looked. The parser reads the Cyrillic **perfectly** — 1,327 Cyrillic characters on the test
page, zero unmapped. The characters were never the problem. This is what it actually produced:

```
Окнигекак-тоиначе.Такиепометкимогутоказатьсяполезными
```

Correct letters. **No spaces.** `Окниге` should be `О книге`. It wasn't garbling anything — it was
welding every word on the page into one token, and *that's* what my folklore had misremembered as
"garbling."

And here is the loveliest piece of trivia I learned all project. I pulled the drawing instructions
out of the PDF, and pdfTeX writes text like this:

```
[(Summary)-250(of)-250(Contents)] TJ
```

The words are separate strings. Between them are *numbers*. **There is no space character anywhere on
the page** — that `-250` is the space.

The reason is pure Knuth. In TeX, interword space is not a character; it is **glue** — a stretchable,
squeezable quantity that the line-breaker pulls on to justify a paragraph. By the time the page is
printed, that glue has been resolved into a *distance*, and a distance in a PDF is a number in the
positioning operator, not a glyph. TeX had no reason to emit a space, because in its model there was
never a space *character* — only the space *between* things. Look at `(Lar)10(ge)` in the same line:
that's intra-word kerning, the identical mechanism an order of magnitude smaller. Word-space is
`-250`; a kern is `10`. Same operator, same units, different scale — which is exactly why you can
recover the words: the two populations don't overlap.

`pdftotext` has quietly reconstructed spaces from that geometry for decades. DeepDoc waits for a
character TeX never had a reason to write.

Two more turns of the screw, both from questions I didn't ask myself:

**"This space thing is not Russian-specific, lol."** He was right, and it's the correction that made
the bug worth reporting. I measured every PDF in the library. *Eight of sixteen books* have no space
glyphs — every TeX-produced document, including **Jurafsky & Martin** and **Sutton & Barto**, the two
most-cited books on my own shelf. English had been getting away with it because welded English trips
the "this looks garbled" heuristic and falls back to OCR — which works fine for Latin. So the whole
time, I'd been running OCR over English textbooks whose text layer was *already perfect*, paying the
single most expensive step in ingestion (my GPU sits at 0% during ingest; it's all CPU layout
analysis) to reconstruct text I already had.

For Russian, that same fallback is a trapdoor. The OCR model's dictionary holds 6,270 CJK characters,
52 Latin, and **six** Cyrillic. It can spell 20% of a Russian page versus 99% of an English one. So it
discards a perfect text layer and hands the page to a model that cannot form the words. *That* — not
the font encoding, not "language support" — is the true origin of "DeepDoc garbles Cyrillic": a
well-meaning fallback, falling back onto a model that can spell one letter in five.

**And the port carried the bug across.** The parser was being migrated to Go, and the Go version has
the gap-from-geometry fix — gated on an `asciiWordPattern` regex, so every non-Latin script is
excluded by construction. The kicker: the Python code's *own* space regex, a few lines from the bug,
already includes Cyrillic. Someone ported that character class to Go and deleted the `а-яА-Я`.

The fix is small and it went upstream as two PRs —
[infiniflow/ragflow#16958](https://github.com/infiniflow/ragflow/pull/16958) (word boundaries for
non-Latin scripts + the OCR-fallback guard, **merged**) and
[#16959](https://github.com/infiniflow/ragflow/pull/16959) (the `chunk_token_num` fix, Act 9's bug). But the thing I keep turning over is that I sat on
top of this for weeks behind a four-word note — *"DeepDoc garbles Cyrillic"* — that was wrong in every
particular, and I never questioned it until someone else did.

## Act 11 — The mouse that could not be caught

By now the corpus was clean: stemmed, well-chunked, de-welded. Time to go back to the question that
started everything — *"какие виды мышей ты знаешь"*, what species of mice do you know.

It failed. Again. But this time I could watch *exactly* where, because I'd finally built the
instruments to see each stage separately.

**Retrieval was no longer the problem.** The passage listing rodents now sat at rank 15–30 in the
pool, up from 101 — the stemmer and the clean chunks had done their work. When I asked the *adjacent*
question, *"какие виды грызунов"* (rodents, not mice), the whole thing worked end to end: the list
was at rank 15, and once I widened the slice to include it, the model produced a correct, cited
answer. The information was *there*.

So why did "mice" still fail? Because the model cannot tell a **mouse** from a **rodent** from a
**bat**. Given the list of the rodent *order* — rat, hamster, marmot, beaver — it labels the whole
thing "species of mice." Given *летучие мыши* (bats — literally "flying mice," a different order
entirely), it includes them too, on the strength of the name.

I tried to fix it with a prompt: *"only list something as X if an excerpt identifies it as X; do not
substitute a broader category."* It changed nothing. The model read the instruction and listed the
rodents anyway. A direct, empirical confirmation of a rule I'd been repeating all project — *don't
paper over a model's failure with more prompt* — administered to me, by me.

Then came the suggestion that actually taught me something: *don't constrain it — let it answer, then
ask it to reflect on what it's unsure about.* So I ran a second pass: a skeptical critic, re-reading
each item against the sources. And the critic wrote this, about the marmot:

> **Сурок → KEEP** — mentioned as a rodent, **is not a mouse**, but related.

Read that twice. The model **stated that a marmot is not a mouse** — and kept it on the list of mice.

That single line reorganised my whole understanding of the failure. It is *not* that the model lacks
the knowledge. It has the fact; it can write it down on demand. What it lacks is the will to *act* on
its own fact — to delete something from a list it has already produced. It has a deep bias toward
keeping, toward agreeing with its own draft, the same eager-to-please tendency that makes it say
"you're absolutely right" three times in a row.

Which, finally, points somewhere concrete — and it's the same place everything else in this project
pointed. Don't ask the model to be more disciplined. Have it emit the judgment as data — *"marmot: not
a mouse"* — and let **code**, not the model, do the deleting. The model decides; the harness acts. A
closed loop. The model asked to move the right hand; the harness moves the right hand.

I did not finish that fix. But I finished understanding the problem, which after two days of being
wrong felt like the larger victory: the wall is no longer in a part I can't see. Retrieval is solved.
What's left is a small model's unwillingness to contradict itself, and that has a shape, and the shape
is familiar.

## Act 12 — The last honest measurement

After all of it — the stemmer, the chunker fixes, the word boundaries, the corpus cleaning — I ran
the retrieval benchmark one final time, on a clean single-version corpus. This is the number that
either justifies the week or doesn't:

```
                before   after
recall@8         40%      60%
recall@64        80%     100%
```

**recall@64 = 100% is the win.** Every answer I test for is now within reach of the reranker. The
first stage no longer throws the answer away before anything smart can look at it. That was the whole
disease — the answer missing from the model's context, masquerading as a hallucination — and it's
cured at the pool level.

So I did the safe half of the obvious fix: return the top 64 instead of the top 20, and hand the
model the top ~20 of those instead of the top 8. Immediately, questions that had failed for a week
started answering. "What rodents do you know?" — which had returned *"the corpus lists no specific
names"* — now returns the actual list, because the passage that was always sitting at rank 15 finally
reaches the model.

And then, in the very same answer, the model added: *"bats belong to the order Rodentia."*

They don't. Bats are Chiroptera. The model had the correct rodent list in front of it, cited it
correctly, and then reached past it to file bats — *летучие мыши*, flying mice — under rodents on the
strength of the name. Retrieval handed it the truth; it garnished the truth with a category error of
its own invention.

Which is the whole project in one sentence. I spent a week making sure the right passage lands in the
model's hands, and I succeeded, and it turns out that was necessary and not sufficient. The pipeline
can put the truth on the screen. It cannot make a 30-billion-parameter model stop pattern-matching a
name into the wrong family. That's not a retrieval bug, or a chunking bug, or a stemmer bug. That's
the model, and no amount of plumbing upstream of it changes what it does with what it's given.

The honest ending isn't "solved." It's "the failures finally moved to where they actually live."

## Closing — what I actually learned
1. Split resources by appetite; don't buy unified memory for a bandwidth-bound job.
2. Grounding beats weights for specifics; make generation cite or abstain.
3. The model is the weak link — deterministic scaffolding, not trust.
4. Measure, don't assert. (Every latency number here contradicted my first guess.)
5. Offline forces discipline: pin every dep, materialize the corpus, fetch nothing at runtime.
6. **Garbage doesn't have to be wrong to poison you — it only has to be shaped like the query.**
7. **Write the expected answer down before you run the test.** Every suite I have now was
   reconstructed from a conversation where I'd been fooled, and I only knew I'd been fooled because
   I'd said out loud what "right" looked like first.
8. **Never put a weak model between yourself and the source.** Summarising is discarding — and it
   discards exactly what it failed to recognise. If you can afford to read the passages, read the
   passages. I built a library and then spent an evening talking to a summary of it.
9. **Measure each stage separately, or you will blame the wrong one.** End-to-end tests told me "the
   answer is wrong". They could not tell me *which stage* was wrong — so I blamed the model for a
   failure that happened two stages upstream. One number (recall@64) ended a whole evening of
   theorising.
10. **Suspect the boring part.** I interrogated the embeddings, the reranker, the chunker and the
    prompt — everything new and interesting. The bug was a missing **stemmer**, and the fix was
    published in 1980.
11. **Purpose cannot be compiled into a regex.** Every rule I wrote encoded the surface form instead
    of the thing: `?` is not a question (it's a jsonb operator), a question needn't contain `?` (it's
    a numbered stem with options), and `мышей` isn't `мышь` until a stemmer says so. Where the
    decision is a *judgment*, use a model — as a cascade behind a cheap filter, with an audit log,
    and biased towards keeping.
12. **Write down what would prove you wrong, before you look.** My best idea of the week failed its
    own test — 221 chunks deleted, the answer moved one rank. I only reported that honestly because
    I'd committed to the failure condition in advance, while I still believed the thesis.
13. **Make your components incapable of failing quietly.** Every real bug here was silence read as
    success: a parser that refuses a file and reports `DONE`, a reranker that times out and returns
    the unranked list, a missing stemmer so the one word that mattered matched nothing, a
    `chunk_token_num` accepted, stored, echoed back — and never read by the code that ran, and — in
    the script I wrote to clean up after silent failures — an unpaginated API call that skipped 119
    documents and printed "0 deleted". **The bug you can see costs an afternoon. The bug that reports
    success costs you a thesis.**
14. **Measure the boring things.** Chunk size. Token counts. Row counts. The two worst bugs of the
    week were a stemmer from 1980 and an integer nobody reads — not the embedder, not the reranker,
    not the prompt. I never looked, because those aren't the interesting parts. That is exactly why
    the bugs were there.
15. **The best question of the week was "why is that number bigger than the other number?"** Asked by
    someone glancing at output I'd already dismissed. Sanity-check the totals. Ask why one is 10× the
    other. Most of what I found this week began with a number that didn't look right.
16. **Distrust your own folklore.** "DeepDoc garbles Cyrillic" was a four-word note I'd left myself,
    wrong in every particular, and it steered weeks of work around a bug I never diagnosed. The
    characters were always fine; TeX just doesn't write spaces. A belief you never re-test is a bug
    with tenure.
17. **The failure moving upstream is progress, even when the answer is still wrong.** The mice
    question failed at the start because retrieval couldn't find the passage; it fails now because a
    30B model can't tell a mouse from a rodent it has *just described* as "not a mouse." That's not
    the same failure — it's a better one. Knowing exactly which stage is the wall is most of the work.
18. **A model that won't act on a fact it can state needs a harness, not a prompt.** Asked to
    self-critique, the model wrote "marmot: not a mouse" and kept the marmot on the list of mice. It
    has the judgment; it lacks the will to enforce it. So take the enforcement away from it: have it
    emit the verdict as data, and let code do the deleting. The model decides which hand to move; the
    harness moves the hand.

## Act 13 — Finishing the punch list

After the dramatic bugs come the boring fixes, and they matter too. Once I understood that the wall
was the model and not the pipeline, I went back and did the whole tool-layer punch list — the stuff
that had been quietly costing calls and context the entire time.

The common shape, again: **every one was the harness returning an error it had the information to
avoid.** `list_projects` printed a project id in one format and then rejected it when you pasted it
back. The grep tool told you to "anchor the definition," and then returned nothing for `class
auto_ptr` because the real declaration is `class _LIBCPP_TEMPLATE_VIS auto_ptr` — our own advice,
walking us into a wall. `source_search` emitted repo-relative paths that the file reader couldn't
open. `ask_code` dead-ended on a wrong-repo guess instead of pointing at the right repo it had already
found. None of these were the *model's* fault. The model asked for the right thing; the harness lost
the step and blamed the model.

So: the tools now accept the ids they emit, auto-relax an over-strict anchor and tell you where the
symbol actually lives, hand back absolute paths, and redirect instead of dead-ending. And two
*prompt* fixes — because the routing bug was *caused* by the prompt (two hardcoded project names that
dragged every search toward them), and the "I'm only a coding assistant, I can't answer about mice"
refusal was the prompt over-narrowing the domain. Both fixed by *removing* prompt, not adding it.

None of it is exciting. All of it is the difference between a tool you fight and a tool that gets out
of the way. On a plane, with no second chances, that difference is the whole game.

## Act 14 — I built the thing that lets me not trust the model

Every prior act was about making the model's answer better. This one admits it never gets to *perfect*
and builds the escape hatch: **a way to check.** A grounded answer is a claim with a footnote; the
browser is what turns the footnote back into the source, offline, in one click.

The first version served the retrieved *chunk* as text and I hated it on sight. A chunk is what the
embedder sees, not what a human should — re-wrapped `pdftotext`, page markers mid-sentence, the caption
of a diagram fused to the paragraph after it. So I threw the text away and rendered **the actual page**:
`pdftoppm`, 200 dpi, the typeset book exactly as printed. Reading the reconstruction was archaeology;
reading the page is just reading.

Then the small, telling fights, each one a "why is *this* wrong":

- **"Why P9?"** A chunk about Raft linked to page 9 — the table of contents. My page-finder had probed
  the source text with the chunk's first word, *"Raft,"* and `.find()` returned the first hit: the ToC
  entry. Fixed to probe a distinctive *phrase*. Then a Russian chunk still fell through to ugly text
  because the phrase *"кластера. 4. Перед…"* has a number wedged in it and my word-separator didn't
  span digits. The page viewer is a pile of these — every one a place where "close enough" wasn't.
- **"Highlight the terms."** On a rendered page your eye needs somewhere to land. I pull word bounding
  boxes from `pdftotext -bbox` and paint gold boxes over the matches — but matched by *stem*, not
  string, so *"какие виды мышей ты знаешь"* lights up `вид` and `мыш` across all their inflections and
  ignores the "do you know" scaffolding. The same anchor-noun idea as the query normaliser, now made
  visible on the page.
- **"This name sucks."** The doc was called `kubernetes__setup__production-environment___index.md`.
  That's an ingest artifact, not a name. Markdown docs now show their front-matter title
  (*"Production environment"*), PDFs their real filename.
- **"Let me see the tree, so I can keep reading."** The markdown came from a docs *repo* — so opening a
  doc now gives you a left nav tree of its neighbours, and a `/browse` folder view of the whole corpus.
  Which quietly deleted a whole component: I'd been running a second static server (miniserve) just to
  browse files. "These two can fold now," he said. They folded.
- **"The whole screen flashes."** ←/→ did a full page navigation. Now the viewer swaps only the page
  image, decodes it *before* the swap so there's no blank frame, and precaches the pages around you. It
  went from a web page you reload to a reader you flip through.

And the browser paid for itself immediately by exposing a corpus bug I'd have never found in a metrics
table: search `raft`, and the top hit was the book's **index**. Of course it was — an index is the
single densest keyword match in the entire book, and the single most useless thing to read. *Garbage
doesn't have to be wrong to poison you; it only has to be shaped like the query.* The fix wasn't in the
browser at all; it was teaching the curation judge that a table of term→page-number is apparatus, not
content, and sweeping it out. The tool that verifies the answers turned out to be the best instrument I
had for finding what was wrong with the data underneath them.

## Act 15 — I taught the machine to grade itself, and it found the ceiling

The browser verifies one answer at a time, by eye. But the failure that haunts this whole project is a
model that is *fluent and wrong*, and fluency is precisely what a quick read forgives. So I wrote the
answer key down first — before any run — so a prompt change could be **judged, not admired**, and then
built a harness to drive the local model through the questions and grade it against that key.

Two things made it honest. First, it drives the model through the exact launcher I ship (`qwen.sh` —
production system prompt, MCP tools, the works), never a bare client, because otherwise you're grading a
different animal than the one that flies. Second, the suites are *conversations*, not lists — because
the failure I most needed to catch only shows up on turn two. It's called **grounding decay**: the
model grounds the first question with real tool calls, then quietly stops and answers the rest from
memory — which is exactly where it starts making things up. You can't see that in a single prompt; you
have to count tool calls *per turn* across a conversation.

Then the result that made the whole exercise worth it. On the PostgreSQL/OrioleDB suite the local model
was genuinely good — asked for OrioleDB's WAL records, it opened the actual header and enumerated all
nineteen, zero invented. I checked every one against the source; it didn't fabricate a single code.
I was ready to call the local stack good enough.

Then I pointed it at serenedb — a big private C++ codebase the corpus has never seen — and it scored
**zero out of four**. Not by refusing. By *answering*: it called its tools, got generic noise back, and
then confidently reconstructed a plausible, wrong story from training memory — describing a Postgres/
DuckDB engine as if it were MongoDB, claiming a key-ordering behavior the code flatly contradicts. On
the hardest question it never opened the source at all. And here's the part that reframes everything:
every fact it missed, I found with `grep` in about ten seconds. The tools weren't the bottleneck. The
*model* was — its search-and-synthesis on unfamiliar ground. **Grounding is not correctness.** Calling
the tool and reading what it returns are two different skills, and the second one is where a weak model
quietly falls back to bullshitting.

That's the ceiling, drawn precisely: strong where the ground is familiar, a confident fabricator where
it isn't. Which is the entire argument for a bigger brain — and, in the meantime, for one more turn of
the screw on the prompt. So the last move was to make *prompt-tuning* a closed loop too: each candidate
DISCIPLINE is a file appended on top of production, and a tournament runs them all across every suite
against the same frozen rubric, and ranks them. No arguing about wording. The prompt improves or it
doesn't, measured against a constant — the same discipline I'd already applied to the corpus and the
tools, finally turned on the words I put in the model's own mouth. I left it running overnight to grade
and re-grade itself while I slept.

## Act 16 — Ollama got me 80%; the last 20% was raw llama.cpp

The model downloaded, Ollama ran it, ~23 tok/s. Fine. Then I noticed the GPU sitting at one busy
core during generation and wondered what was being left on the table. The answer turned into the most
satisfying tuning session of the whole project — and a lesson about where convenience stops paying.

Ollama runs `llama-server` under the hood but fixes the important knobs internally. Run that same
binary directly (once you feed it Ollama's CUDA backend by hand — `GGML_BACKEND_PATH` at
`cuda_v13/libggml-cuda.so`, an incantation that took a couple of "no usable GPU found" faceplants to
find) and the knobs come out. The sweep that followed produced a genuinely **counterintuitive** result:
**prompt processing and token generation want opposite thread counts.** Prompt processing is a big
parallel matrix-multiply — it wants all 24 cores. Generation is one token at a time, memory-bandwidth
bound — and piling 24 threads on it made them *fight over the memory bus*: **24 threads → 2 tok/s; 8
threads → 34.** Fewer threads, faster. Ollama uses one `--threads` for both, so it can't win both — you
split `--threads` (8) from `--threads-batch` (24) and suddenly you beat it on *both* axes: PP ~1200
(from a fat `--ubatch`), TG ~34. My first attempt, before I understood any of this, was *15× slower*
than Ollama because I naively shoved every expert onto the CPU. The tools only help if you know which
way to turn them.

Then the wiring, which is where I got humbled twice more.

I stood the tuned server up as a systemd unit and pointed everything at it — the agent's shim (one env
var, since it already spoke the right API) and the corpus synthesizer (a small patch, since it spoke
Ollama's dialect). One fast qwen-next serving the whole stack; Ollama demoted to embeddings. Clean. And
completely broken: **"Worked 0s,"** every turn, even a plain "continue." The log: `tools param requires
--jinja flag`. Claude Code sends its tool schema on *every* request, and llama-server with `--no-jinja`
*rejects any request that carries tools* — so it wasn't the tool-heavy turns failing, it was **all** of
them. One flag.

And then, still broken on long sessions, a subtler one — the one worth remembering. Claude Code believes
qwen-next has a **200K** context window (its model registry says so). I'd capped the server at 128K. A
143K-token conversation is under 200K, so Claude Code never triggers compaction — it thinks there's
room — and sends the whole thing. llama-server, capped lower than Claude Code *believes*, rejects it.
**The overflow protection that should have saved it — compaction — never fired, because it fires against
the believed limit, not the real one.** The fix is to make the real limit exceed the belief: `-c 262144`.
Now the server can hold more than Claude Code will ever send, and compaction (at ~200K) always fires
with headroom to spare. The bug wasn't the size; it was the *disagreement* between two components about
how big the box was — the same silent-mismatch failure mode this whole project keeps rediscovering,
wearing yet another hat.

## Act 17 — The garbage was hiding inside a good chunk

I'd already built the curation pass — rules and an LLM judge that strip the exercises and the tables of
contents (Acts 13–14). So when I looked at a session's search results and saw junk, I expected to find a
chunk we'd missed: some quiz, some index page the judge let through. I grepped the corpus for the query
and read the raw passages. And the garbage was there — box-drawing glyphs `口□□□`, a run of `DDDDDD`,
shredded words like `rylooku` and `arely consulted`, loose numbers with no sentence around them. A
diagram from the ClickHouse huge-pages paper: DeepDoc had tried to OCR a *picture* and produced the
text-equivalent of static.

The reflex is obvious — add it to the delete list, like the quizzes. But look closer at the chunk and
the reflex is wrong: the noise wasn't a chunk of its own. It was **interleaved with real prose** —
`huge pages`, `backend reads a buffer`, `shared_buffers` — in the *same* chunk. The exercises were a
whole-chunk problem: a quiz is a quiz, delete it. This was a *within*-chunk problem. Delete it and you
lose the sentence sitting next to the static; keep it and you keep the static. The unit we'd been
filtering — the chunk — was the wrong unit. The garbage lived *below* it.

That forced two realizations. First, this needs a **repair**, not a deletion: excise the garbage span,
keep the prose. Second — and this is the part that's easy to get wrong — you can't just overwrite the
chunk's text. RAGFlow's update-content call changes the *words* but not the *embedding vector*. Patch a
chunk in place and it keeps the embedding computed from the garbage, so it keeps getting retrieved for
all the wrong reasons. The only honest repair is to **delete the chunk and add the cleaned text back as a
new one**, so it re-embeds. Remove and reingest, not patch. My own suggestion to just patch it was the
lazy answer, and the user caught it.

And the deepest fix isn't at the retrieval layer at all — it's in the parser. DeepDoc *does* separate
figures from text; it pulls out every region its layout model labels `figure`. The garbage exists only
because the model **mislabeled a diagram as text**, so its OCR never got pulled. The parser already has
box-level filters for exactly this shape of problem — it detects PUA/CID gibberish and font-encoding
garble and throws those boxes away before they become chunks (the same machinery where, months earlier,
I'd taught it to infer word boundaries from geometry the way `pdftotext` does). A third strategy —
"this box is a flattened diagram" — belongs right there, and would stop the garbage from ever reaching a
chunk. But that's a patch to a pinned dependency's guts, so it's written down and deferred, not done in
the heat of the moment. Fix what's already in the index now; return to the parser when we're next
elbow-deep in it. The same discipline the whole project runs on: know where the real fix lives, even when
you're taking the pragmatic one.

## Act 18 — The electricity company ran my test for me

There was one item on the list I kept not doing: *verify the stack comes up from cold — needs an
actual reboot.* It had been sitting there for days, because rebooting a machine that's mid-way through
parsing 161 books feels like pulling the tablecloth to check the dishes are stable. Then someone needed
the outlet more than my laptop did, and the test ran itself.

The report card was better than I feared and worse than I hoped. Docker brought the whole RAG stack
back unprompted. Ollama came back. And — the genuinely pleasant surprise — the ingestion *partially
resumed on its own*, because RAGFlow's parse queue lives in Redis, and Redis persists. The CPU was
already grinding again before I'd finished checking what was broken.

Two things were broken, both instructive. First: my backend toggle switched models by *starting* and
*stopping* systemd units — but the big model's unit was still *enabled*, so the reboot resurrected it,
and it sat holding 21.6 GB of VRAM against a config file that said the other model was active. A
switch that only flips the running state is half a switch; the reboot re-decides it. The fix is one
word — `enable --now` instead of `start` — but the principle generalizes: **state you chose must be
encoded where the machine reads its boot instructions, or the machine will un-choose it.**

Second, subtler: the queue that survived is also a queue that *lies by omission*. Tasks not yet
started persisted and resumed. Tasks **in flight** at the moment of death had been popped from the
queue — gone, unrecorded. A book parsed as ten page-ranges that loses one would finish the other nine
and then sit at partial progress forever, *silently missing a chunk of itself*. No error, no retry,
nothing to see unless you already suspected it. That's the failure shape this whole project keeps
finding: the system does less than it claims and says nothing. The recovery script re-queues every
book that was mid-parse — at the honest cost of redoing their partial progress, which promptly made
the ingest "look stalled" and taught the day's last lesson: after you make chunks 46× bigger, watching
chunks-per-minute is watching the wrong needle and panicking on schedule.

## Act 19 — Three OCRs walked into a bar, and the winner wasn't an OCR

The ml shelf had four djvu scans of Russian neural-network books from 2000 — the kind of books
where the formulas *are* the content. Extracting them offered two roads, both bad. The embedded
text layers (some librarian's OCR from 2013) read the prose beautifully and mangled every formula
into modern art: `а13 = (((ааа)2)2)а`, which as written is simply *wrong* — that's a¹³ with its
superscripts amputated. The specialist alternative, marker — a 3.3 GB stack of layout models and a
math-recognition network — did the opposite: gorgeous LaTeX (`$$f[i,k]=\sum_{j=1}^{k}f[i-k,j]$$`),
while quietly hallucinating Chinese, Georgian and Thai glyphs *into the Russian prose* and dropping
a `+ Fib(n-2)` from a code listing. One tool couldn't see math; the other couldn't stop dreaming.

Getting marker running was its own comedy of layered failures, each teaching the same lesson at a
different altitude. Its model downloader had no timeout, so a connection blip left it hanging on a
dead socket forever. My salvage of its 98.6%-complete download skipped a 1.5 KB `.gitattributes` —
shell globs don't match dotfiles — and the loader's existence-only manifest check responded by
re-downloading 1.4 GB. Then the CDN quietly swapped the model file between my resume attempts, and
I stitched the old file's head onto the new file's tail: sizes matched perfectly, contents were
garbage, and only the safetensors header arithmetic caught it. Existence checks are not integrity
checks; size checks are not integrity checks; the fetch doctrine that survived is curl with resume,
a stall-abort, and a sha256 from the API — verified before anything gets to claim it's a model.

Then came the twist. The user asked: if *you* can read these formulas off the page, maybe qwen can
too? The text-only qwen can't — no eyes — but Qwen3-VL exists, Unsloth ships it as a
quality-per-byte UD quant, and our tuned llama-server turned out to already speak `--mmproj`. Ten
seconds after loading, the general-purpose vision model read the same scanned page and produced
`$a^{13}=(((a\cdot a\cdot a)^2)^2)\cdot a$` — superscripts intact — then transcribed the Pascal
block character-for-character *including the term marker had dropped*, in clean Russian prose with
zero hallucinated scripts. The 17 GB generalist beat the 3.3 GB specialist at the specialist's own
job, on the first try, running locally on hardware that was idle anyway.

There's a genuine architectural lesson under the anecdote. The specialist stack is a *pipeline* —
detection model feeds recognition model feeds math model — and every seam is a place where errors
compound silently. The VLM is one model looking at the whole page the way a person does, and when
it reads "Fib(n-1)" it knows, in a way no pipeline stage does, that Pascal code tends to say
`Fib(n-1) + Fib(n-2)`. Its language prior is doing restoration work the OCR pipeline structurally
cannot. So now 2,614 pages are streaming through the GPU at ten seconds each while DeepDoc chews
the CPU — the two heaviest jobs on the box, not competing — and the output goes behind the same
gate as everything else here: a blind 20-page audit against the page images before a single chunk
is ingested. Because a model that reads beautifully is exactly the kind of witness this project has
learned not to trust without a second grader.

## Act 20 — I taught it to remember what I was reading

The corpus is everything I loaded *before* the flight. But reading isn't a fixed act — on any given
day I'm down some specific rabbit hole, and the answer I want is coloured by the one I wanted an hour
ago. The system had no idea. Same query, same ranking, forever, no matter what I'd spent the morning
on.

Two builds came out of that. The first is a Chrome extension, and it exists because the server-side
fetcher has a blind spot I kept hitting: it can't see anything behind my login. The good stuff — the
doc page gated behind SSO, the article that's JavaScript all the way down, the internal wiki — is
exactly what a from-the-outside fetch returns empty on. So I moved the capture into the one place
that's already authenticated: the tab in front of me. One click grabs the *rendered* DOM, runs it
through the same trafilatura the rest of the corpus uses (so a captured page looks like every other
page, not a special case), keeps a print-to-PDF for the figures, and drops it into the `links` shelf.
And because the whole point of this project is a laptop at 30,000 feet, it buffers twice: the
extension holds captures if the receiver's down, the receiver holds them if RAGFlow's down, and it
all lands when the stack comes back. You can capture with the entire backend switched off.

The second is a right-click: *Explain this with Oracle*. Select a term and a little card, glued to
the selection, fills in token by token with an answer drawn **only from my corpus** — the same
grounded pipeline as everything else, so it either explains from the sources or says the corpus
doesn't cover it. No round trip to a network that isn't there.

And then the idea I actually got excited about, and deliberately did **not** build yet. If the
extension already sees everything I read, it can keep a small, fading memory of the *topics* I'm
circling — a fixed handful of slots, each a cluster of related things, the recent ones bright and the
stale ones decaying out. Feed that into the ranking as a gentle, capped nudge and retrieval becomes
*associative*: ask an ambiguous question in the middle of a Postgres afternoon and it leans Postgres,
the way a colleague who's been in the room with you would. All browsing tints the memory, but the
things I *chose* to save or ask about weigh the most; a page I merely skimmed only tilts it a little,
and a denylist keeps my inbox out of it entirely.

I wrote the whole design down and left it parked, for one reason this repo has taught me the hard
way. The cardinal sin here is *garbage shaped like the query* — a passage that wins retrieval by
resembling the question without answering it. Recency bias is that exact sin wearing a friendly mask:
lean too hard on "what I just read" and one idle tangent poisons every answer after it. I've already
watched a plausible re-ranking trick backfire on the numbers. So this one ships only behind an
off-switch, and only if it moves the gold passage *up* without costing recall — measured, A/B, on the
same eval that's caught every other good-sounding idea that wasn't. The sensor is built. The nudge
waits for the number.

## Act 21 — I paid a genius to label ten thousand pages of junk, and the loop kept lying to me

The corpus has a garbage problem I've been circling for weeks: table-of-contents lines, back-of-book
indexes, bibliographies, OCR'd code with the digits swapped, diagrams flattened into `口□□□`. None of
it is *wrong*, exactly — it's just apparatus, and it competes for the eight retrieval slots that
should hold answers. I'd tried a rule (blind to anything it wasn't written for) and a GPU model as a
judge (accurate, slow, and not even deterministic at temperature zero). The literature kept pointing
the same way: stop judging every chunk with a big model, and train a small fast classifier that can
score all quarter-million of them. But a classifier needs a labeled set, and a labeled set is the one
thing you can't fake.

So the plan was to spend the expensive model on the one job only it can do: hand-grade a
representative slice — ten percent, 24,766 chunks — into nine classes, and let *that* become the
training data. The architecture is the same shape as everything else here. Many readers, one writer:
each labeling agent reads a 25-chunk batch and the rubric and writes one small file of verdicts; a
single importer is the only thing that ever touches the database. Resume is by disk truth — a batch
is done if and only if its output file exists — so an agent can die mid-run (usage limit, a stall, a
stuck read) and be respawned with nothing lost. Roughly a thousand batches, two lanes running at
once, and I retired each lane the moment its context crossed ~140k tokens and stood up a fresh one,
because past that they start re-reading their own history instead of working.

And then the loop spent two days teaching me the same lesson this whole project keeps teaching me:
**the failures are silent by default.** An agent would confidently report "batch done, 25 chunks" and
have written 24 — off by one, every field perfect except that one chunk_id simply wasn't there.
Another would label everything correctly and quietly omit the `certainty` field on the rows it was
*most* sure about, because a confident CLEAN felt like it didn't need a number. Early on I caught
these by shelling out a one-line script to diff the file against the batch — which was itself the
wrong move, because now *I* was the ad-hoc unaudited step. The fix was to make the importer say it out
loud: `SHORT 24/25 — missing 62feaa03` and `REFUSED — bad certainty on line 3`. Once the machine
announced the gap, the repair was mechanical: bounce the agent to append the one missing row, and —
crucially — never mark a short file "done," so re-importing it can't double-count the 24 good rows it
already has. It's the exact same move as making the reranker's silent timeout visible, or the parser
that reported `DONE` on zero output. A degradation nobody can observe isn't graceful; it's a bug with
good manners.

One agent got genuinely stuck — caught on a single chunk that was one physical line thousands of
tokens long, paging through it forever looking for a middle that the exporter had deliberately elided.
(There's a head-plus-tail truncation for exactly this; junk signals live at the edges and in the
texture, not in the middle of a 28,000-token listing.) I killed it and handed the batch to a fresh
lane, which finished it in two minutes. The stuck one wasn't failing — it was *trying*, forever, which
is worse, because trying-forever doesn't trip an alarm either.

It finished: 24,832 labeled chunks, 88.6% of them clean, the other eleven percent spread across the
full junk taxonomy. The most useful number isn't the total — it's *where the grader was unsure.* The
three lowest-confidence classes are figure-garbage, boilerplate, and OCR-damaged code, all hovering
around 0.6, all for the same reason: they're boundary calls. Is a box-drawing table a real rendered
table or a shredded diagram? Is a license block per-page furniture or substantive text? Is a
prose-heavy passage with three mangled code lines "clean" or "damaged"? Those aren't the classifier's
job to force — they're precisely the band where a small fast model should throw up its hands and defer
to the expensive judge. The expensive model didn't just make the training set. By recording its own
doubt, it drew the map of where cheap judgment ends and expensive judgment has to begin.

## Act 22 — "We have nothing on Kubernetes," said the model, having not looked

I asked the local model what the corpus had on Kubernetes. It told me, pleasantly and immediately,
that the corpus covers Rust and C++ and a few other things but has nothing on Kubernetes.

It has the entire official Kubernetes documentation tree. It has *Kubernetes Patterns* and *Cloud
Native DevOps with Kubernetes*. Four hundred and forty-three labelled chunks mention `kubectl`. When
I ran the actual retrieval, pod-security passages came back reranked at 0.73.

The model hadn't looked. Not "looked and missed" — hadn't issued a query at all. And when I went
hunting for where it got its confident inventory, I found I had written it myself, twice.

The first copy was in the system prompt, in a routing rule: *documentation and concept questions —
Rust std, io_uring semantics, PostgreSQL concepts, Go, Linux, general knowledge — go to the corpus
tool.* Those were meant as examples of the **kind** of question. They were read as the **contents of
the library**. The second copy was worse, because I'd never thought of it as prompt at all: the
corpus tool's own docstring, the description string the model sees on every single call, opening
with a parenthetical list of what the corpus contains. Both lists were written months ago. Both had
been false for months — the corpus had since eaten Kubernetes, biology, machine learning and several
hundred books.

There's a specific trap here worth naming. A list in a prompt does not read as *"for example."* It
reads as *ground truth about the world*. The model has no way to tell a stale illustration from a
current fact, and an inventory question — "do you have anything on X?" — is exactly the question
that a list appears to answer directly. So the model answers from the prompt, confidently, and never
calls the tool, because the prompt looks authoritative and the tool looks optional. This is the same
failure this project keeps producing in new costumes: **the system did less than it claimed and said
nothing about it.** Except this time it didn't even do less — it did *nothing*, and narrated a
result.

The fix was subtraction, which by now is the pattern. Both lists deleted. In their place: the corpus
is large, general, unlisted and *changing*, so what it contains is knowable only by asking. Only "I
searched and found nothing" licenses saying a topic is absent, and it has to be said that way — as
the outcome of a query, not as a fact you happen to know.

But the part I'll actually carry forward is the second copy. I'd been treating the system prompt as
"the prompt" and tool docstrings as documentation — as if one were live wiring and the other were a
comment. They're the same wire. A tool description is injected verbatim into the model's context and
biases it identically; it just isn't in the file you think of as the prompt, so it never gets
audited and never expires. Which gives a rule sharp enough to actually apply: **a prompt may
describe how to use a tool. It must never describe what the data contains.** The data is allowed to
change. The sentence about it never does.

## Act 23 — The assistant couldn't see, so it answered anyway

The shim that lets Claude Code run on a local qwen had one line in it I'd written months earlier and
never thought about again: when a message contained an image, it replaced it with
`[image omitted — model is text-only]` and carried on.

Which is true of the text model, and useless to me. I'd paste a screenshot of a dashboard, ask what
was wrong with it, and get a fluent answer built from the filename and the surrounding conversation.
Not a refusal. An *answer*. The picture I was asking about had been dropped on the floor between
Claude Code and the model, and nothing in the transcript said so.

The machine is not text-only. It has a 30B vision model sitting right there. What it can't do is run
both at once: the card is 24 GB, the text model is 20.6, qwen3-vl is 17. They're mutually exclusive
by arithmetic, not by policy.

So the shim takes the detour. An image in a message means: swap the vision model in, have it *read*
the picture, swap back, and put the reading in the prompt. The conversation resumes with the image
described in context. It costs minutes and it's completely visible while it happens, because a
silent four-minute pause is indistinguishable from a hang — I know, because the first version was
reported to me as one.

Two details turned out to be load-bearing.

The first is that **the cache is not an optimisation.** Claude Code re-sends the entire transcript
on every turn, so an image pasted once is present in every subsequent request. Without a
content-addressed cache, each turn would swap the GPU twice — out and back, minutes each way — to
re-read a picture that hadn't changed. The readings are keyed by `sha256` of the bytes, so the same
image in another session under another filename is one read, forever.

The second is **who is allowed to conclude.** The obvious design is to let the vision model answer
the question — it can see, after all. That's exactly wrong. It would take a weaker model's
conclusion and hand it to a stronger one wearing the clothes of an observation, and everything
downstream would treat "what the image says" as fact. So the vision model is prompted to *report*:
transcribe verbatim, describe structure, say "illegible" rather than approximate, and explicitly
**not** answer the question. The block that lands in the text model's context is labelled as one
model's reading, not as the image.

I measured it on a Grafana screenshot. The text model came back quoting 17.6.0, 4.19 GB, 40.9 MB —
numbers that exist only in those pixels — and volunteered, unprompted, that it had not seen the
image and these came from the vision model's report. That last clause is the whole design working. A
system that can't see should say so *while* using what it was told.

## Act 24 — I moved 2,500 tokens and the assistant stopped answering

Prompt processing on this box runs at 300–500 tokens per second. That number is boring right up
until you notice that the same 2,500-token block of reference material was being re-processed on
every single request — six seconds, every time, to re-read text that hadn't changed.

llama.cpp will happily skip it. It routes each request to whichever slot has the best matching
prompt **prefix** and only processes what comes after. I had simply never given it a prefix to
match: each feature's system message *was* its task instruction, so "explain this" and "fact-check
this" and the chat panel differed at token zero and shared nothing.

So I restructured. Everything constant went into the system message — a preamble, then the site's
reference material — and every task instruction moved down into the user message, below the
boundary. Measured with the server's own tokenizer against its own timing log: an identical request
went from 9,325 tokens processed to **4**. A *different* question on the same site reused 2,533. It
worked exactly as advertised.

And it broke every answer in the product.

Because in the act of writing that shared preamble, I described all the context uniformly: page
context, the site's own published file, our curated reference material — all of it "not evidence,
never licenses a claim the excerpts don't support". Which is right for two of those three and
catastrophically wrong for the third. The curated pack is material *we wrote*, for exactly that
site, precisely so it could answer questions the corpus can't. I had just forbidden the model from
using it.

The report came back as four words: *nothing works actually now*. Not chat, not explain. Every
question about a benchmark dashboard answered with "The corpus doesn't cover this" — while the
material that covered it sat in the prompt, marked unusable.

Then it got funnier. Fixing the preamble wasn't enough, because two *other* places encoded the same
stale assumption. An empty retrieval short-circuited and returned "the corpus doesn't cover this"
before the model was ever called — written back when excerpts were the only possible source. And the
page-context block ended with a sentence I'd been proud of: *"if the excerpts do not answer the
question, say so even when this page appears to."* Written to stop the thing becoming a web
summariser. Now sitting directly above the words "Excerpts: NONE", where it read as an order to
refuse.

Three separate vetoes on the same answer, each individually defensible, each written before the
thing it was now blocking existed.

The lesson isn't "be careful when refactoring prompts", which is useless. It's that **a rule written
against one failure mode outlives the assumption that made it correct**, and prompts have no type
system to tell you. The excerpt-only rule was right when excerpts were the only source. It survived
into a world with three sources and became a bug — invisible, because the system still produced
grammatical, confident, well-formed output. It just said no.

There's a second edge to this one. The restructure that caused it is also the most fragile thing in
the repo: if anything request-specific ever leaks into that preamble — a date, a URL, a selection
length — the cache boundary silently moves to token zero and the whole mechanism stops working
*while still producing correct answers*. The only symptom is latency. That failure can't be caught
by reading the output, so it's a test rather than a comment, and the test asserts the one property
that matters: every feature, on a given host, must produce a byte-identical prefix.

## Act 25 — I asked it what it thought of the page, and it had never looked

The chat panel could answer questions about my corpus. Asked "what do you think about this page?",
it searched the corpus — because searching the corpus was the only thing a turn knew how to do. The
corpus has never seen that page. So it answered from the page's *title*, and from whatever the
conversation had already established, fluently.

The fix is not a better prompt. It is hands. A turn now has tools: read the page, look at it with
the vision model, search the corpus, click, type. Retrieval stopped being what every turn does and
became one option among several, chosen by the thing that knows which the question needs.

Two structural decisions turned out to matter more than the tools themselves.

**The browser drives the loop, not the server.** The receiver decides *what* to do and can do none
of it — it has no DOM and never will. So a turn ends with a request for a tool, the extension
performs it, and posts the result back as the next turn. This is the same rule the whole project
runs on: the model decides "move the right hand", and the component that *owns* the hand does the
moving and reports what actually happened. Anything else is a system narrating actions it did not
take.

**Reading and acting are different in kind, so they are gated differently.** A wrong read is a wrong
answer: visible, recoverable, annoying. A wrong *click* is a wrong deed — a deleted run, a triggered
rerun, a submitted form — inside a session the model did not authenticate and cannot undo. The page
I built this against has **Delete**, **Rerun** and **New Run** within a few hundred pixels of each
other. So clicking is enabled per host, and when a host is not enabled the acting tools are not
described to the model at all. Not described-and-discouraged. Absent. A tool it cannot see is a tool
it cannot mis-call, and "please be careful" in a prompt is the workaround this project keeps
refusing to write.

Then I watched it work, which is where the real lessons were.

Asked to explain a benchmark run, it clicked METRICS, clicked GRAFANA, guessed a CSS selector that
did not exist, was told so, and recovered by taking a screenshot. The before/after URLs in the tool
results proved the navigation rather than the model asserting it. That is exactly the behaviour I
wanted — and three things about it were wrong in ways I would not have predicted.

**It photographed itself.** The screenshot included Oracle's own chat panel, so the model read its
own previous answer as part of the website. Describing itself. From a stale copy. With no way to
know that was what it was doing.

**It invented a selector.** `div[data-testid="metrics-panel"]` — nothing on the page has it. The
tool replied `no element matches`, which is *true* and *useless*: a model given a bare failure has
nothing to correct itself with, so it guesses again. Every failing tool now answers with what would
have worked — a click that misses returns the clickable labels that actually exist; a read that
misses returns the whole page and says the selector was a guess. A miss should make the next attempt
unnecessary, not merely possible.

**And it announced a plan it then abandoned.** "Let me check the METRICS tab again, then return to
GRAFANA" — followed by a completely different call, which failed, followed by a silent recovery. My
user watched a confident plan, then silence, then an answer, with nothing joining them. He described
it as *"it recovered, without telling me"*, which is the politest possible framing of a system whose
narration and behaviour had come apart.

The interesting part is that the model was not lying. It said what it intended, then adapted — which
is correct behaviour. The failure was mine: the interface showed intentions and hid outcomes. Steps
now report `✓` or `✗` with the reason. The prose is checkable against what happened, which is the
only form of trust that survives contact with a system that acts.

## Act 26 — The status line said "reading weights from disk". The file was in RAM.

Loading a 50 GB model is disk-bound, so the swap progress line said `reading weights from disk`. I
wrote that when it was true.

Then I added a tier that keeps both models' files in page cache — 125 GB of RAM against 67 GB of
models, so a swap can copy from memory instead of re-reading from an SSD. It worked: both models
measured 100% resident. The line kept saying `reading weights from disk`, because it was a constant
string.

My user read it and asked: *"it keeps reading qwen next from disk. not enough ram?"*

That question is the whole cost of the bug. Not that the message was wrong — that it was wrong in a
direction that gets acted on. It reads as *you are short of memory*, and the honest answer is that
71 GB were free and the time was going somewhere else entirely: `--no-mmap` copies ~50 GB out of
page cache into the process and then pushes ~20 GB across PCIe into VRAM. Twenty to thirty seconds
that no amount of RAM removes.

A status line that states a cause it has not measured is not a status line. It is a guess wearing a
uniform. It now measures — mincore, once, before the load — and says which of three things is
actually happening.

There is a second, funnier half. My first implementation of that page-cache tier used
`POSIX_FADV_WILLNEED` and nothing else, because it is elegant: no CPU, no copy, just a hint to the
kernel. The hint is capped far below 50 GB. It would have produced a warming step that reported
success, warmed almost nothing, and left the swaps exactly as slow as before — this project's
signature failure, written by me, deliberately, in the name of elegance. It now issues the hint
*and* reads the file, and there is a `cached_fraction()` built on `mincore` so the claim can be
checked instead of believed.

## Act 27 — Every chunk was missing the letter `n`, and every counter said 100%

I sampled six random chunks from a freshly ingested shelf of arXiv papers, expecting to skim them.

> "tatio **desig**, mixed-i itiative i teractio" · "**Co ectio s**" · "**Ma ageme t**" ·
> "i troduce a **ovel** match-a d-filter framework"

Every `n`, gone. Replaced by a newline. In every chunk, of every document, of that entire knowledge
base. The source text on disk was perfect — 79,425 bytes, 4,899 `n` characters, clean prose.

Nothing had errored. Parsing reported success. Chunk counts looked healthy. The progress bar said
100%. The documents still embedded, still matched queries, and would still have been cited — which
is the failure this whole project exists to prevent, arriving from a direction I had never checked.

The cause is a chain of individually reasonable decisions. RAGFlow's text parser lets you configure
a chunk delimiter, and supports writing it as `\n`, so it round-trips the value through
`unicode_escape`. Then it splits the document on **each character** of the result. Creating a
dataset through the API without naming a delimiter stores the default *double*-escaped, which
`unicode_escape` renders as backslash-plus-a-literal-`n` — and so the parser split on every letter
`n` in the English language and swallowed it.

Three things kept it hidden. It only affects the plain-text parser, so the PDF-parsed shelves were
fine. It is invisible in Cyrillic, which has no Latin `n` to lose — and one of the affected shelves
is Russian, and looked perfect. And the knowledge bases created *earlier* hold the correct value, so
the healthy majority argued that the code was fine and the new content was suspect.

Then the fix did not work, which is the part I would tell someone else about. I corrected the
knowledge base, re-parsed everything, and got byte-identical garbage. `parser_config` is copied
forward three times — knowledge base to document at upload, document to task at queue time — and the
parser reads the *last* copy. Fixing the first one changes nothing that already exists. It took
16,477 document rows and cancelling every in-flight task before the letter `n` came back.

And the verification is the lesson, not the fix. "The config now reads correctly" was true while the
data was still garbage; I had already believed it once. What settled it was counting the damage
signature in the rebuilt chunks: 1,190 of 1,201 contain the letter `n`, 1,109 contain `" the "`, and
**zero** contain `" a d "`.

The same day produced three more of the same species. A token-counting helper that wrapped its
exception and returned **0 tokens** for a real chunk. A dataset counter reading **196,782** for a
store holding 3,805 rows, because it accumulates and never decrements. And an unset environment
variable that quietly moved the entire corpus onto a different database while every status line
still said *done*.

Four bugs, three systems, one shape: **the system reported success and produced wrong data.** Not
one of them appeared in a log, a counter or a progress bar. Three were found by reading the stored
rows, the fourth by reading a container log.

I have a whole vocabulary for guarding against models that make things up. I had almost nothing for
infrastructure that makes things up — and infrastructure is more convincing, because it comes with
a progress bar.

## Act 28 — The database wasn't the bottleneck. My own reranker was.

I pointed a firehose at the corpus: an arXiv mirror, ~310,000 papers already on disk and 800,000
still downloading, ingesting continuously. The point was not to build a better shelf. It was load —
to find out where the document store breaks, and whether retrieval gets worse as the index grows.

Then a single search failed to return in 115 seconds, and I assumed I had my answer.

I was wrong about which component. Breaking it apart:

| what I asked for | time |
|---|---|
| one knowledge base, no reranking | 1.87 s |
| **arXiv alone — the biggest, 140k chunks** | **1.39 s** |
| all 22 knowledge bases, top 64 | 2.76 s |
| **all 22, with the reranker** | **42.5 s** |

The store is *faster* on its largest collection than on a small one, and going from one collection
to twenty-two costs under a second at 467,000 rows. The forty seconds is a cross-encoder reranker
that runs on the CPU — the same CPU the ingest was saturating.

So ingest load degrades **retrieval**, but through CPU contention, not through anything to do with
the index. And because the retrieval path tries the reranked query first, *every chat turn* pays it.
That is a "the chat feels stuck" complaint whose cause is a background job in a different subsystem,
with no error anywhere and both components behaving exactly as designed.

The measurement I built to test the database ended up testing the thing next to it. That keeps
happening, and I have stopped treating it as a detour.

## Act 29 — Two filters I measured, and then didn't ship

Adding a rule feels like progress. Both of these looked obviously right, and the measurement killed
them.

**The first: letter-spaced text.** Some PDFs position every glyph individually, so the extractor
cannot tell which gaps are word breaks, and you get `P ro d u c tio n R e a d y`. It tokenises into
nothing, matches no query, and still occupies a retrieval slot. My user asked the obvious question —
*"what's the point of letter-spaced PDFs?"* — and he was right that there isn't one.

So I wrote a detector: what fraction of a document's words are a single letter? Then I measured it
across 2,260 papers. Median 2.9%. And the highest scorer in the entire corpus, at 42.7%, is a
perfectly readable algebra paper from Kyoto — because mathematics is *full* of single-letter
variables. The two genuinely broken papers score 0.052 and 0.111, near the median, because only
their figure captions are damaged and the prose around them is clean.

Any threshold that catches the broken figures deletes the mathematics. Three chunks in 5,373 are
actually bad. The cure was more dangerous than the disease.

**The second: bibliographies.** This one is real — asking about approximate nearest neighbour search
returned six papers, and two of the six were pure reference lists (*"CIKM '24. ACM, 4906–4913.
doi:…"*) outranking chunks that explain the method. Across 138,000 chunks, 4,278 carry three or more
DOIs. I started writing a rule to detect them by DOI density and push them down the ranking.

My user stopped me twice, and both corrections were the same correction. First: the chunk cleaner is
its own subsystem with its own design — a cheap rule proposes *candidates*, a judge decides — and the
lesson written into it when it was built is *"my rules kept encoding the surface form instead of the
thing."* A DOI count is surface form. Second: this case was already specified, months earlier,
including the output contract — **demote by default**, as per-class weights in the reranking layer,
where it is configuration rather than code and can be *inverted* for navigational queries.

Which matters, because deleting bibliographies would be wrong anyway. *"Who wrote the RaBitQ
paper?"* is a real question, and the citation is the answer.

The pattern in both: I had a lever (delete), so everything looked like something to delete. The
useful additions turned out to be the other two — **exclude a whole collection** when it is
legitimate but wrong for this question, and **demote a chunk** when it is real but rarely the
answer. Deleting is for material that is actively poisonous, and that is a much smaller category
than it feels like at 2am.

## Act 30 — The fix that deleted itself, and the pause that rested nothing

Two failures in one night, and both of them had the same shape: something I had verified was true,
and had stopped being true without any event that looked like a change.

**The fix that deleted itself.** RAGFlow is pinned, so our fixes are local patches applied inside the
container. One of them stops tiktoken refusing to encode text that quotes `<|endoftext|>` — which
sounds exotic until you remember the corpus contains books *about* language models, so the string is
the subject matter.

I bumped the task executor from 1 worker to 8. Throughput went 25 → 95 docs/min. Then, out of
tidiness rather than suspicion, I ran the patch script's `--check`, and it said the tiktoken patch was
missing. Adding one line to a compose file makes `docker compose up` **recreate** the container rather
than restart it, and an in-container edit does not survive recreation. The image's pristine file came
back and parsing ran unpatched for hours. Nothing logged it. Nothing could: from the outside, "the
patch is applied" and "the patch was reverted eight hours ago" produce identical silence.

What made it diagnosable was the control case sitting right next to it. A second patch — a NUL byte
in a PDF's text layer must trigger OCR rather than kill the document — was in the *same container*,
applied the *same way*, and had survived. The only difference: that file is bind-mounted from the
repo, and `token_utils.py` was not. So the fix isn't vigilance, it's a mount. Mounted files survive
recreation and live in version control; in-container edits are a bet that nobody will ever run
`docker compose up` again.

Then the same night, the same shape a second time. I checked what those unpatched hours had cost, by
grouping failed documents by their error text. One tiktoken failure — visible, retryable, no harm
done. And 38 of `A string literal cannot contain NUL (0x00) characters` — the exact failure the *other*
patch prevents, the one I had just confirmed was applied and working.

It was applied. It does work. It cannot fire for these documents. The patch lives in the PDF parser,
and arXiv is not ingested as PDF: a host-side pdftotext writes `.txt` and we upload that, so the NUL
arrives as ordinary text and dies at insert without the PDF parser ever being on the path. I had
verified the mechanism — the probe encodes, the predicate matches, the module parses — and never asked
the one question verification doesn't cover: *does this code run for this input?*

**The pause that rested nothing.** Meanwhile the machine is a laptop doing a deliberate firehose
ingest, so there is a pause between batches to let it cool. My user's instinct was that a minute
wasn't enough, so I measured the tail instead of arguing about the number:

    140s   67 pending   serene 378%   ragflow 141%    draining
    150s   45 pending   serene  98%   ragflow   3%    RAGFlow idle
    200s   45 pending   serene 100%   ragflow   1%    still 100%
    210s   45 pending   serene   0%   ragflow   1%    finally quiet

The parser goes idle and the *database* keeps working for another minute — per-index refresh and
compaction, peaking at 876% (8.7 cores) during the drain. So a 60-second pause spent its entire
window on compaction. It rested the feeder, which was already doing nothing, and never once rested
the machine.

His fix was better than a bigger number: don't start a batch until the CPU is below 75 °C. Time and
CPU% were only ever proxies; temperature is the thing actually being protected. And he sharpened it
again unprompted — *below 75 for at least a minute*, because this workload's signature is periodic
bursts, so a single reading taken between two spikes reads cold and would launch a batch straight
into the next one.

Three details in that gate are the whole lesson. It resolves the sensor by *name*, because
`/sys/class/hwmon` numbering is assigned in probe order and moves across reboots — a thermal gate
reading the NVMe would still look like it works. An unreadable sensor *disables* the gate rather than
being read as cold. And if the machine never cools, the cycle is *skipped* rather than started hot, so
a hot afternoon throttles ingestion instead of either cooking the box or wedging the unit.

The common thread across all three: every one of these was verified at some point, and verification
of the mechanism is not verification of the effect. The patch was present and unreachable. The pause
was running and resting nothing. The gate would have been reading a sensor — just not that one.

## Appendix — the actual build order (a dev diary)
*Reconstructed from memory; the sequence is faithful, the exact dates aren't. This is the order
things actually happened — most beats are a thing I set out to do, the wall I hit, and the fix.*

1. **The premise.** Goal: an offline reference brain to help write `orioledb-waldump` in Rust
   (io_uring, reading OrioleDB's on-disk WAL/undo) on a plane with no internet. First move wasn't
   code — it was arguing with my own `PLAN.md` and fixing it (offline-weights trap, an API-doc
   sanitizer step, the reranker choice) before building.
2. **The resource-split bet.** GPU for the model, CPU/RAM for everything RAG. Ollama +
   qwen3-coder:30b, RAGFlow as the hub, bge-m3 for embeddings.
3. **RAGFlow crash-loop.** A master clone bind-mounts an entrypoint the release image lacks →
   pin to **v0.26.4**. First "the version matters" lesson.
4. **The corpus.** Fetch + sanitize (rustdoc/mdBook HTML → markdown; never ingest raw HTML). It
   kept growing as I added things — Go books, KDE/Wayland, the Ubuntu guide, a kernel course with
   diagrams, probabilistic-DS papers.
5. **The embeddings dead-end.** TEI-gpu's image is compute-cap 8.0; the RTX 5090 is 12.0 → refuses
   to load. TEI-cpu is too slow. Answer: **bge-m3 on Ollama** — multilingual, coexists in VRAM.
6. **The reranker.** The highest-ROI retrieval upgrade. Benchmarked models (bge-m3 was 14 s on CPU —
   too slow); picked **gte-multilingual** (Russian Postgres books!). transformers v5 broke its RoPE
   → pin **4.48.3** ("just google the error" — I'd prematurely written it off). RAGFlow's hardcoded
   30 s rerank timeout choked under parse load → patched to 180 s.
7. **Code structure.** RAG-chunking C can't answer "who calls this" — wired a **codebase-memory**
   graph in via mcp-proxy, plus source-grep / emacs / git MCP servers. When the graph came up empty,
   fall through to ripgrep.
8. **Make it feed itself.** An **ingestor** agent that classifies+routes+parses a folder/PDF/URL.
   The Russian PG books ingested as garbage (DeepDoc mangles Cyrillic CID fonts, Новиков→HOBMKOB) →
   reparse with `pdftotext -layout`; taught the ingestor to detect Cyrillic and route there.
9. **Claude Code on local qwen.** Ollama speaks the Anthropic API natively → the whole harness on
   qwen with three env vars, "no proxy." Fixed an auth conflict, wired the Oracle MCP servers in,
   added a discipline prompt to curb malformed tool calls.
10. **Fill the GPU.** Context to **56K** — the max where qwen *and* bge-m3 both stay VRAM-resident.
11. **Grounding as a primitive.** Packaged retrieve→rerank→extract-then-answer as **`ask_corpus`**,
    motivated by the model's confidently-mislabeled `pg_last_wal_replay_lsn`.
12. **Docs aren't the whole truth.** Source facts live in the repo, not the docs → **`ask_code`**.
    Even grounded, qwen renumbered an enum (real code 15 → 8), so `ask_code` attaches a **RAW
    SOURCE** block marked authoritative over the prose.
13. **Make the *real* Claude Code ground too.** Saved the routing as a memory and wired the tools
    into my own config — not just the local qwen's.
14. **LSP for truth, LLM for intent.** `oracle-lsp`: compiler-accurate hover/def/refs/symbols, then
    the piece I first skipped — the language server's own code actions (extract function, …) with
    **`suggest_refactor`** letting qwen reason over the *real* refactor menu.
15. **Tightening the tools.** Live failures fixed: `ask_code` rejecting a code-graph slug; a
    `source_search` that dumped an SVG's base64 and firehosed thousands of *usages* instead of the
    one definition (added noise filters + a broadness guard that steers you to anchor the def).
16. **The tool-call leak — the big one.** Red dots, no results. Not stale connections: qwen leaking
    `<function=…>` XML as text. Measured the endpoint matrix (Anthropic-streaming = 33% leak; the
    rest ~0%), built the translating **shim**, then a **salvage parser** for the residual 5%. From a
    third of tool calls silently dropped → 12/12 clean.
17. **Ops, throughout.** `oracle-ctl.sh` to free VRAM for gaming, `ingest-status.py` to watch the
    parse backlog drain, and finally — the first `git commit`.

18. **The SereneDB experiment — and reporting bugs the right way.** Swapping Elasticsearch for a
    Postgres-wire engine (one inverted index carrying both BM25 text and IVF vectors) hit a wall of
    *silent* failures: BM25 returning 0.0 for every row, `ORDER BY` over the scorer emptying the
    result set, a vector predicate quietly zeroing an ANN scan. Each one I distilled to a
    self-contained reproducer — a few rows, no corpus, run-and-see — and handed upstream. The
    SereneDB team turned two of the four around in a **same-day point release** (26.07.4); I verified
    the fixes against the released image, then deleted the workarounds from my connector and let it
    rely on the fixes. The measured payoff for the swap: **ES-parity retrieval recall at ~5× lower
    query latency and ~2.4× higher QPS**, in-app through RAGFlow's real pipeline — not a microbench.
    Lesson: a good bug report is a gift you can act on, and a minimal repro is what makes it one.

19. **Feeding the firehose — dedup is two problems, not one.** Cloned several book collections (~420
    files) to widen the corpus. Deduping *within* that pile was the expected hard part — a
    cheap→expensive cascade (filename containment → page-count → let qwen-next read the first pages
    and extract `{title, year, pages}`, then match in **Python**, not in the model). It caught the
    fun failures: three different "Deep Learning" books union-found into one group; two files with the
    same title, same 462 pages, different md5 — a second *scan*, not a newer *edition*, so "prefer the
    recent edition" had nothing to prefer. But the catch that mattered came later: **341 "unique"
    books, and 165 of them were already in the corpus** — whole collections I'd ingested months
    earlier, sitting behind other knowledge-base names. Deduping the incoming pile is not the same as
    deduping against what you already ingested; the second check keys off *what's parsed in a KB*, not
    *what's on disk*, and blind ingestion would have returned every one of those 165 books twice at
    retrieval. So the real ingest was 176 new books, not 341. And the last surprise was cost:
    the "good" parser (DeepDoc — real page positions, figure extraction) explodes each PDF into one
    task *per page*; 132 books = ~4,500 page-tasks, **~28 hours on CPU**. The figures aren't free, and
    for a shelf of general/fiction/pop-science books they may not be worth 28 hours — the fast
    `pdftotext → naive` lane exists for exactly that call.

20. **The label fleet — buying a training set the model can't fake.** The junk classifier needs
    labeled data, so I spent the expensive model hand-grading 10% of the corpus (24,766 chunks) into
    nine classes — many-readers/one-writer, disk-truth resume, two lanes retired at ~140k tokens
    each. The loop's real content was catching its own silent failures: agents reporting "25" and
    writing 24, or omitting `certainty` on the rows they were surest of. Fix was to make the importer
    announce `SHORT 24/25 — missing X` and never mark a short file done. Ended at 24,832 labels, 88.6%
    clean; the three lowest-confidence classes (figure-garbage, boilerplate, OCR-damaged code, all
    ~0.6) *are* the map of where the cheap classifier should defer to the expensive judge.

## Assets to include
- **`docs/screenshots/explain-with-oracle.png`** — the money shot for Act 20: select a phrase on any
  page, get a grounded explanation glued to the selection, every `[n]` a link to the source page.
- **`docs/screenshots/fact-check.png`** — the same selection fact-checked, verdict chip first
  (SUPPORTED), then the excerpts that support it. Pairs with the "grounding is a primitive" thread.
- The rank-3→rank-1 rerank A/B (real output).
- The mislabeled-LSN screenshot vs the grounded `ask_corpus` answer.
- The resource-split diagram.
- The reranker benchmark table (bge-m3 14s vs MiniLM/GTE ~1–3s, multilingual note).
- A short honest "what's still broken" list (Cyrillic space-stripping; weak-model tool-calling).

## Pull quotes
- "The model is fluent and it lies about specifics. Everything else is damage control."
- "Capacity is useless if it's slow."
- "Don't trust a weak model to decide when it's done."
- "A model is only as exact as its grounding."
- "Grounding put the truth on screen; the model still fumbled the transcription. So I stopped
  asking the model to read the number — I asked the compiler."
- "Deterministic, compiler-safe mechanics; the LLM only for the intent."
- "'No proxy' was an aesthetic, not a requirement — and correctness doesn't care about my aesthetics."
- "A third of the tool calls were being silently dropped as text. The fix was the thing I bragged about not needing."
- "A user's query is a question. A textbook's exercise section is also questions. So the book's own quiz out-competes its own chapter."
- "Garbage doesn't have to be wrong to poison you. It only has to be shaped like the query."
- "I diagnosed a system bug from my own typo."
- "The passage that answers scored 0.471. The passage about bats — literally 'flying mice' in Russian — scored 0.762. The embedder wasn't wrong; it was measuring resemblance, and I'd asked it for truth."
- "A hallucination wearing a footnote is worse than a naked one."
- "BM25 returned 0.0 for every row and never once raised. A scorer that can't score should crash, not shrug."
- "The engine wasn't broken — my dictionary was missing one flag. But 'fails silently' turned a config typo into a lost day."
- "The connector passed its unit test and returned zero recall in production. The interface it implemented lied by omission — two methods the retriever calls were never marked abstract."
- "The agent said 'batch done, 25' and had written 24. Every field perfect except the one that wasn't there."
- "A stuck agent doesn't trip an alarm — it keeps trying, forever, which is worse than failing."
- "The expensive model didn't just make the training set. By recording its own doubt, it drew the map of where cheap judgment ends."
- "'We have nothing on Kubernetes,' it said, having not looked. I'd written that inventory myself, months earlier, as an example."
- "A list in a prompt doesn't read as 'for example.' It reads as ground truth about the world."
- "I'd been treating the system prompt as the prompt, and tool docstrings as documentation — as if one were live wiring and the other a comment. They're the same wire."
- "A prompt may describe how to use a tool. It must never describe what the data contains. The data is allowed to change; the sentence about it never does."
- "Parity isn't a number you cite once; it's a number you re-earn every time the engine, the query, or the config moves."
- "The dedup grouped three different 'Deep Learning' books as one copy. I caught it by reading the tool's own suspect list — which is the entire reason that list exists."
- "Same title, same 462 pages, different md5. Not a newer edition — just a second scan. 'Prefer the recent edition' has nothing to prefer."
- "Use the weak local model to *extract* structure, then do the matching in code. Don't ask it to be the eyes and the algorithm."
- "They shipped the fix the same afternoon I filed the repro. Two of my four bugs, gone in a point release — so I deleted my own workarounds."
- "Deduping the pile you're about to add is the easy half. The hard half is that a third of it was already inside, under a different name."
- "The figures aren't free. DeepDoc turns 132 books into 4,500 page-tasks and 28 hours — and a fiction shelf doesn't need figures."
- "`[image omitted — model is text-only]`. True of the model, useless to me: it dropped the picture and answered anyway, from the filename."
- "The machine isn't text-only. It just can't be both at once — 20.6 GB and 17 GB in a 24 GB card is arithmetic, not policy."
- "The cache isn't an optimisation. Claude Code re-sends the whole transcript every turn, so without it the GPU swaps twice per turn to re-read a picture that hasn't changed."
- "The vision model reports; the text model reasons. Letting the weaker one conclude would smuggle a guess into the context wearing the clothes of an observation."
- "It quoted numbers that exist only in those pixels — and volunteered that it had never seen the image."
- "An identical request went from 9,325 tokens processed to 4. Then every answer in the product broke."
- "A rule written against one failure mode outlives the assumption that made it correct. Prompts have no type system to tell you."
- "Three separate vetoes on the same answer, each individually defensible, each written before the thing it was blocking existed."
- "It breaks the cache while still producing correct answers. The only symptom is latency — which is why it's a test and not a comment."
- "Asked what it thought of the page, it searched the corpus — because searching the corpus was the only thing a turn knew how to do."
- "The model decides 'move the right hand'. The component that owns the hand does the moving, and reports what actually happened."
- "A wrong read is a wrong answer. A wrong click is a wrong deed — in a session the model didn't authenticate and can't undo."
- "Delete, Rerun and New Run sat within a few hundred pixels of each other. So the acting tools aren't discouraged, they're absent."
- "It photographed itself: the screenshot included Oracle's own panel, so it read its own last answer as part of the website."
- "`no element matches` is true and useless. A miss should make the next attempt unnecessary, not merely possible."
- "The model wasn't lying — it said what it intended, then adapted. My interface showed intentions and hid outcomes."
- "'It recovered, without telling me' is the politest possible description of narration and behaviour coming apart."
- "The status line said 'reading weights from disk'. The file was 100% in RAM. He read it and asked if he needed more memory."
- "A status line that states a cause it hasn't measured is a guess wearing a uniform."
- "fadvise alone would have warmed almost nothing and reported success — this project's signature bug, written by me, in the name of elegance."
- "Every chunk was missing the letter `n`, and every counter said 100%."
- "I have a whole vocabulary for guarding against models that make things up. I had almost nothing for infrastructure that makes things up — and infrastructure is more convincing, because it comes with a progress bar."
- "'The config now reads correctly' was true while the data was still garbage. I had already believed it once."
- "The config is copied forward three times, and the parser reads the last copy. Fixing the first one changes nothing that already exists."
- "It is invisible in Cyrillic, which has no Latin `n` to lose — so one affected shelf looked perfect."
- "The database is faster on its largest collection than on a small one. The forty seconds was my own reranker, starved by my own ingest."
- "A 'the chat feels stuck' complaint whose cause is a background job in a different subsystem, with both components behaving exactly as designed."
- "The highest-scoring document in the corpus was a readable algebra paper — because mathematics is full of single-letter variables. Any threshold that catches the broken figures deletes the maths."
- "I had a lever, so everything looked like something to delete. Deleting is for material that is actively poisonous, and that is a much smaller category than it feels like at 2am."
- "From the outside, 'the patch is applied' and 'the patch was reverted eight hours ago' produce identical silence."
- "Adding one line to a compose file recreated the container, and a fix I'd verified quietly stopped existing. Nothing logged it. Nothing could."
- "The control case was sitting right next to it: same container, same patch, same night — and it survived, because that one was a mount."
- "The fix isn't vigilance, it's a mount. An in-container edit is a bet that nobody will ever run `docker compose up` again."
- "It was applied. It does work. It cannot fire for these documents."
- "I verified the mechanism and never asked whether the mechanism runs for this input. A patch is only as good as its reachability."
- "The parser went idle and the database kept working for another minute. The pause rested the feeder, which was already doing nothing."
- "Time and CPU% were only ever proxies. Temperature is the thing actually being protected."
- "Below 75 — for at least a minute. A single reading taken between two spikes reads cold, and would launch a batch straight into the next one."
- "A thermal gate reading the NVMe instead of the CPU is worse than no gate, because it still looks like it works."
- "An unreadable sensor disables the gate. 'I can't tell' must never resolve to 'cold'."
- "If it never cools, skip the batch — don't start hot, and don't wedge. A hot afternoon should throttle ingestion, not end it."
- "Verification of the mechanism is not verification of the effect. The patch was present and unreachable; the pause was running and resting nothing."
