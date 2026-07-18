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
silently discarded by the code that actually ran. **Nothing errored. Nothing warned.** It is the same
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

The fix is small and it went upstream as two PRs. But the thing I keep turning over is that I sat on
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

## Assets to include
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
