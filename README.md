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
CPU / RAM      RAGFlow + DeepDoc parsing · Elasticsearch (~365K chunks) · GTE reranker
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
