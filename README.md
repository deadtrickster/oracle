# Oracle

An **offline, GPU-backed reference brain**: a local coding + sysadmin assistant grounded in real
documentation, books, papers, and source code — built to survive a long flight with no internet.
Everything runs on one laptop (RTX 5090 24 GB, 125 GB RAM, 24 cores).

The one-line thesis:

> **An assistant whose answers you can trust when there is no network to check them against.**

Offline is what makes that hard. Online, a wrong answer is an inconvenience. Offline, a confident
wrong answer *is the output*. Nearly every failure documented in this repo has the same shape:
*the system did less than it claimed and said nothing* — and the work here is hunting that shape
through every layer: parsers, tokenizers, retrieval, reranking, serving, prompts, and the models
themselves.

## The two axioms

Everything in this repo that generalises past this machine is a corollary of these two
(full statements with their corollaries: [DESIGN.md](DESIGN.md) §9.0):

1. **Context occupation defocuses — and it is shared.** Filling a window with material degrades
   reasoning, in the 480B frontier model and the local 30B alike; a big window is a higher
   *tolerance*, not immunity. So minimising context occupation is a **quality** measure, not a
   speed one. Confirmed the hard way: after hours reading a local model's Russian-Chinese hybrid
   output, Claude code-switched into Russian in an English document — the exact failure it was
   documenting. Same law, different constant.
2. **The harness must close the loop; never paper over it with prompt.** The model decides "move
   the right hand"; the harness must *actually move it*, verified, like a closed-circuit stepper.
   When tooling misbehaves, fix the tool: the shim salvages malformed tool calls (33% dropped →
   0%) instead of begging for better formatting; tools redirect on a scoped miss instead of
   dead-ending. And prompt can *cause* the bug: two hardcoded project names dragged every search
   toward them. **Removing prompt bias beats adding prompt rules.**

## Highlights — what actually happened here

- **A ~50 GB MoE runs fast on a 24 GB GPU.** Ollama got 80% of the way; the last 20% was raw
  llama.cpp with hand-tuned MoE expert offload. Capacity is useless if it's slow.
- **Claude Code runs on a local qwen** — through a translation shim that *salvages* malformed tool
  calls. Before the shim, a third of tool calls were being silently dropped as plain text. The fix
  was the closed-loop harness, not a sterner prompt (Axiom 2 of this repo).
- **The corpus poisoned itself.** A user's query is a question; a textbook's exercise section is
  *also* questions — so the book's own quiz out-competed its own chapters at retrieval. Garbage
  doesn't have to be wrong to poison you; it only has to be shaped like the query. Hence the
  curation cascade, the labeling rubric, and the junk classifier.
- **The embedder measures resemblance, not truth.** The passage that answered a query scored
  0.471; a passage about bats — literally "flying mice" in Russian — scored 0.762. Retrieval
  config here exists because of measurements like that one.
- **The chunk dashboard lied by 120K.** RAGFlow reported ~365K chunks; reading its counter code
  (Python *and* the Go rewrite — same bug, faithfully ported) showed the real corpus is 243,900.
  Root-caused to a knowledge-base counter leak in the SDK parse path; reported upstream with a
  fix design.
- **One man page existed 28 times.** `io_uring_check_version.3.txt`, duplicated by a rename
  registry the dedup check didn't know about. Existence checks are not integrity checks; size
  checks are not integrity checks — a lesson this repo paid for more than once, including a
  1.4 GB model file whose corruption only safetensors header arithmetic caught.
- **Three OCRs walked into a bar, and the winner wasn't an OCR.** For scanned Russian textbooks,
  a dedicated OCR pipeline lost to a 30B vision LLM running on the same GPU: 2,614 pages
  transcribed locally, then re-transcribed by a frontier model into a gold set — which becomes
  the fine-tuning data to make the local VL model better. The system feeds itself.
- **The electricity company ran the durability test.** Power was cut mid-ingestion; the pipeline
  resumed from disk truth without losing a page. Every long-running job here is built to be
  killed.

## Read this first

The code is the *result*; the documents are the *point*:

- **[BLOG.md](BLOG.md)** — the build story in acts. Every act is a real failure, measured, with
  the fix and the lesson. Start here.
- **[DESIGN.md](DESIGN.md)** — the full design: architecture, the corpus, the grounding pipeline,
  retrieval config, serving (including running a 50 GB MoE on a 24 GB GPU), and the lessons that
  generalize past this machine.
- **[TODO.md](TODO.md)** — the durable state of the work: the checklist, the measurement log
  (including negative results, kept on purpose), and the ideas deliberately parked.

## What's inside, roughly

```
GPU  (24 GB)   qwen3-coder:30b / Qwen3-Coder-Next (tuned llama.cpp, MoE offload)
               qwen3-vl:30b (vision: scanned-book transcription) · bge-m3 embeddings
CPU / RAM      RAGFlow + DeepDoc parsing · Elasticsearch (243,900 chunks — counted, not believed) · GTE reranker
               code-graph, ripgrep, LSP and ask_corpus/ask_code MCP servers
```

- **Corpus**: Rust, Go, C++, Linux/man-pages, io_uring, PostgreSQL (+ Russian Postgres Pro books),
  DuckDB, Kubernetes, Emacs, ML textbooks, papers, and a 161-book tech-book collection — parsed,
  curated, embedded, and page-mapped back to the original PDFs for one-click verification.
- **Grounding tools**: `ask_corpus` / `search_corpus` (retrieve + rerank + cite, or raw chunks),
  `ask_code` (grep-grounded source answers with a RAW SOURCE block), LSP tools ("compiler for
  truth, LLM for intent"), a corpus browser that renders the actual cited page.
- **Local agent**: Claude Code driven by a local qwen through a translation shim that *salvages*
  malformed tool calls (closed-loop harness beats prompt exhortation — the repo's Axiom 2).
- **Curation**: a rules→LLM-judge cascade that deletes retrieval poison (exercises, ToC, index,
  OCR garbage), a versioned labeling rubric with a human-in-the-loop labeling UI, and an
  in-progress trained junk classifier.
- **Eval harness**: conversation-shaped suites with frozen rubrics; prompt changes are run as
  tournaments and *judged, not admired*.

## Honesty note

This repo is a collaboration between a human architect and AI pair (Claude, plus local qwen doing
bulk work). The judgment calls, the vetoes, and the standards are human; a large share of the
keystrokes are not. Commits say so. The documents record what failed as prominently as what
worked — that's deliberate; the negative results are the expensive part.

## Running it

This is a personal system, not a product — paths, models, and service wiring assume this specific
machine. If you still want to explore: [PLAN.md](PLAN.md) is the build sequence,
[OPERATIONS.md](OPERATIONS.md) the runbook, and every script prints its purpose in its docstring.
Expect to adapt, not to `make install`.
