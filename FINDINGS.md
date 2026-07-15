# WTF is going on — the mouse investigation, explained simply

*2026-07-13. Written after an evening that started with "let's ingest some biology books" and ended
with me being wrong three times in a row about my own system.*

---

## The thing that started it

You asked qwen, in Russian: **"какие виды мышей ты знаешь"** (what species of mice do you know).

It answered with a confident list, said it came from the family **Muridae**, and cited a source.

The list was wrong. That's the whole story — but *why* it was wrong turned out to be four different
things stacked on top of each other, and I got the first three wrong.

---

## First, how the machine is supposed to work

```
   your question
        │
        ▼
   [1] SEARCH the corpus  ──► finds ~64 candidate passages
        │
        ▼
   [2] RERANK them        ──► reorders those 64, best first
        │
        ▼
   [3] qwen WRITES an answer using the top few
        │
        ▼
   the answer you read
```

Three stages. Each can fail differently. **Almost all of my confusion came from blaming the wrong
stage.**

---

## How search actually works (the part that matters)

The search stage turns text into a list of numbers — a "vector" — and finds passages whose numbers
are *close* to your question's numbers. Close in numbers ≈ close in meaning. Usually.

Here's the catch, and it's not a bug, it's the *architecture*:

> The search model reads your **question** and the **passage** completely **separately**, and only
> compares the two summaries. It never sees them side by side.

So it can tell you **"this passage is about the same topic as your question."**
It genuinely **cannot** tell you **"this passage answers your question."**

Those are different things, and mixing them up is the root of everything below.

---

## Wrong diagnosis #1: "the corpus doesn't have mice in it"

It did. The answer was sitting in `ege_prakticheskaya-podgotovka` the whole time — a paragraph
about **Отряд Грызуны** (the *order* Rodentia) ending with a list: *мышь, полевка, крыса, хомяк…*

It had been there since before the conversation even started.

---

## Wrong diagnosis #2: "make it refuse when it's unsure"

I measured how confident search was, and got a beautiful clean split:

| | score |
|---|---|
| questions the corpus **can** answer | 0.47 – 0.65 |
| questions it **can't** | 0.24 – 0.36 |

Gorgeous. Obvious fix: refuse anything below ~0.42.

**This would have been a disaster.** The mice question scored **0.35** — in the "refuse" zone — *and
the corpus could answer it*. I'd have built a feature that confidently refuses good questions. I was
about ten minutes from shipping it.

---

## Wrong diagnosis #3: "keyword search is broken!"

I ran a test, saw that turning keyword-search up to 100% and down to 0% produced **byte-identical
results**, and concluded the whole keyword half of the engine was dead.

It wasn't. **I had typed the parameter name wrong.** The API ignored my nonsense parameter and did
the same thing every time.

I diagnosed a system bug from my own typo.

---

## What was actually happening — part 1: the textbook poisons itself

Your question is a **question**. A textbook's *"Вопросы для повторения"* section is also
**questions**. To a machine comparing shapes, those look extremely similar.

So **the book's own quiz out-competes the book's own chapter.**

Measured: `bogdanova` was **13.6% question-lists**. Six of the top thirty results for *"what is
photosynthesis"* were exercise questions, shoving the actual explanation down the page. We were also
indexing the publisher's copyright page (УДК, ББК, the editorial board) as if it were biology.

**Fixed.** `clean-corpus.py` strips them before ingestion — 1,869 blocks out of bogdanova alone. The
rule: *three or more questions in a row = quiz section, delete it. A single rhetorical question in
normal prose = keep it.*

---

## What was actually happening — part 2: the bats

Here's my favourite thing I learned all evening.

After all that cleaning, the mice question **still fails.** The correct passage (rodents) scores
**0.471**. It loses to a passage about **Рукокрылые** — bats — which scores **0.762**.

Why do bats beat rodents for a question about mice?

> Because in Russian, a bat is a **летучая мышь** — a **"flying mouse."**

The machine is not broken. It is doing its job *perfectly*. You asked about мыши, and it found the
passage most **about** мыши. It simply has no concept of "…but that's a *different animal*, and the
passage you want is the one that *lists species*."

**Resemblance is not truth.** That's the whole lesson, and no amount of cleaning fixes it — it needs
a different *kind* of model (one that reads the question and the passage *together*).

---

## What was actually happening — part 3: the real culprit

Search was **honest**. When I finally asked the tool directly, it said, in plain text:

> **"The corpus doesn't cover this."**

It abstained. It did the right thing.

**qwen then wrote a confident answer anyway** — on top of an admission that there was nothing to go
on. And here's the mechanism, which is genuinely nasty:

1. qwen didn't find mice, so it **rewrote its own search query**, inventing the term **"Muridae"**.
2. That query found a real passage — the one clearly labelled **"Отряд Грызуны"** (order Rodentia).
3. qwen **relabelled Rodentia as Muridae**, to match the term *it had just invented itself*.
4. It quietly dropped two animals from the list.
5. It attached a citation.

It invented a premise, went looking for evidence of it, found something adjacent, and filed off the
label that didn't match.

> **A hallucination wearing a footnote is more dangerous than a naked one.**

---

## And one more, found by accident

Asking in Russian, the answer came back **half in Chinese**:

> *…отряд Рукокрылые включает около 900 видов，其中包括一些能够飞行的动物，如蝙蝠（летучие мыши）*

qwen is a Chinese-trained model and drifts back to Chinese mid-sentence under Russian. (Same reason
it once emitted the garbled token **"ВыRIGHT"** at you.) Half your corpus is Russian, so this
matters. Fix is ours: our prompt never tells it what language to answer in.

---

## The thing I should have had from the start

I have no way to measure **search** on its own. My tests grade the **final answer** — so when an
answer is wrong, I can't tell whether search missed it or qwen fumbled it.

**That ambiguity is exactly the hole I fell into three times tonight.**

Jurafsky & Martin (in your own corpus, which I should have read hours earlier) spell out both the
cascade and the metric:

> *Cheap search first → expensive rerank of the top N.*

Which implies the thing I completely missed:

> **The first stage sets the ceiling. Reranking can only reorder what search already found.**

The rodent passage was **never in the top 64**. So every reranking experiment I ran was testing a
chunk that had already been thrown away. Useless.

**Next:** a `qrels` file — for each test question, the passage that *should* win — and one number:
**recall@64** (did search even find it?). That single number, which takes minutes to build,
would have told me in ten seconds that reranking was irrelevant.

---

## Scoreboard

| thing | verdict |
|---|---|
| Corpus missing mice | ❌ wrong — it was there |
| Abstention floor | ❌ wrong — would refuse good questions |
| Keyword search dead | ❌ wrong — my typo |
| Textbook quizzes poisoning search | ✅ real, measured, **fixed** |
| Bats beating rodents ("flying mice") | ✅ real, **unfixable by cleaning** — needs a cross-encoder |
| qwen fabricating on top of an honest abstention | ✅ **the actual bug** |
| qwen leaking Chinese into Russian answers | ✅ real, new, unfixed |
| No way to measure search by itself | ✅ **the reason I got fooled three times** |
