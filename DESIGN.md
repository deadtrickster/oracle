# Oracle — Full Design Document

An offline, GPU-backed reference brain: a local coding + sysadmin assistant grounded in real
docs and your own books/papers, built to survive a plane with no internet. Everything runs on
one laptop (RTX 5090 24 GB, 125 GB RAM, 24 cores).

---

## 1. Goal & constraints

**Goal.** Answer coding/systems questions *grounded in exact, offline, citable sources* — not a
model reciting fuzzy training memory. Built while writing `orioledb-waldump` in Rust (io_uring
I/O, reading OrioleDB's on-disk WAL/undo format).

**Hard constraints:**
- **Offline.** Once you unplug, it only knows what's on disk. All fetching/pulling happens online, up front.
- **24 GB VRAM is the scarce resource.** It holds the LLM (+ query embeddings) and nothing else.
- **125 GB RAM + 24 cores are abundant** and cheap — push everything non-LLM there.
- **Version-exact.** io_uring/kernel pinned to the *target* kernel (7.0), PG to the fork's 17.9, etc.
- **Weak local model.** qwen3-coder:30b is the brain — capable but far below frontier; the whole
  design assumes the model is the weak link and scaffolds around it.

## 2. The core architectural bet: split by resource appetite

LLM inference is **memory-bandwidth-bound** → wants fast VRAM (GDDR7 ~1.8 TB/s).
RAG (embeddings, vector store, parsing, reranking) is **capacity/throughput-bound** → happy on
CPU + cheap DDR. So:

```
  GPU (24 GB GDDR7) ── qwen3-coder:30b (chat/synthesis) + bge-m3 (query embeddings)
  CPU / 125 GB RAM  ── DeepDoc parsing · vector store (Elasticsearch) · GTE reranker ·
                        codebase-memory graph · all MCP tool servers
```

This is why a **dedicated-VRAM + big-system-RAM box beats a unified-memory box** for this
workload: unified memory forces the LLM weights and RAG data to fight over one pool; the split
gives each what it wants in separate pools. The whole system is organized around keeping the GPU
for the model and pushing everything else to the abundant side.

## 3. Components

| Layer | Choice | Why |
|---|---|---|
| LLM serving | **Ollama** (qwen3-coder:30b, codestral) | native Anthropic + OpenAI APIs; systemd; GPU-only |
| Context | **56K** (`OLLAMA_CONTEXT_LENGTH=57344`), q8_0 KV + flash-attn | max where qwen+bge-m3 both stay GPU-resident (2.1 GB free); 64K evicts bge, 96K spills to CPU |
| RAG hub | **RAGFlow v0.26.4** (Docker) | best-in-class DeepDoc parsing (2-col PDFs, tables), CPU embeddings, GUI, agents, MCP client |
| Embeddings | **bge-m3** via Ollama | multilingual (Russian PG books!), 0.66 GB, coexists with qwen |
| Reranker | **gte-multilingual-reranker-base** (CPU service) | 2-stage retrieval; multilingual; ~2.7 s/30 chunks; the highest-ROI retrieval upgrade |
| Code structure | **codebase-memory** graph | call graphs / struct lookups that RAG-chunking C can't do |
| Reading UI | **miniserve** :9800 | browse the raw corpus (rendered docs, diagrams) |
| Editor | **Emacs gptel** → Ollama | quick ask-in-editor |

## 4. The corpus

Materialized on disk under `corpus/` (self-contained; RAGFlow's ES/MinIO/MySQL are *derived,
disposable* indexes rebuildable from it):
- **Rust**: prose (book/nomicon/reference/by-example/async) + sanitized std/core/alloc API +
  tooling books + 10 OSS books (Comprehensive Rust, blog_os, rustc-dev-guide, …).
- **io_uring**: liburing man pages, `io_uring.h@v7.0` (op/flag ground truth), Axboe paper, LotI.
- **Linux**: 10k man pages (merged), kernel v7.0 admin docs, bash/glibc/git manuals, Wayland/KDE,
  Ubuntu Server Guide, the SO2 kernel course (with diagrams).
- **Go**: official docs + spec + 10 OSS books (Learn Go w/ Tests, both blockchain books, …).
- **PostgreSQL**: PG17 source READMEs, OrioleDB docs, + 7 **Russian** Postgres Pro books (Rogov,
  Lesovsky, Morgunov, …), DDIA, Database Internals.
- **Papers**: Dremel, bauplan (agents/Zerrow), NanoLog, + probabilistic DS (HyperLogLog,
  Count-Min, Xor/Fuse filters, MinHash).
- **Emacs**: manual, Elisp reference, 63 misc manuals.
- **meta**: the system's own docs + scripts (so it can explain itself offline).

**API-doc sanitizer** (`sanitize-apidocs.py`): rustdoc/mdBook HTML → clean per-module markdown
(extract `<main-content>`, merge item pages). 785 MB of HTML → 100 MB of ingestable markdown;
raw HTML is never ingested.

## 5. The grounding pipeline (the heart)

The lesson learned repeatedly (LLM-authored C++ book, qwen's mislabeled `pg_last_wal_replay_lsn`,
even my own over-claim about a reranker): **a model is only as exact as its grounding.** So:

```
  question
    → retrieve top-64 (bge-m3 embeddings, all doc KBs)
    → rerank → top-8 (GTE cross-encoder; graceful fallback to embedding order if CPU busy)
    → extract-then-answer synthesis (qwen): answer ONLY from retrieved text, cite sources,
      or say "the corpus doesn't cover this" — never fill from training weights
    → grounded, cited answer
```

Two failure modes, two fixes:
- **Wrong chunks retrieved** → the **reranker** floats the right chunk up (measured: io_uring.h@v7.0
  went rank 3 → rank 1).
- **Hallucination during generation** → **extract-then-answer** anchors every claim to quoted text.

Packaged as **`ask_corpus`** — one MCP tool that runs the whole pipeline internally and returns a
grounded answer. Any caller (local Claude Code, gptel, RAGFlow agents) gets grounding for free;
the anti-hallucination work happens *inside* the tool, so a weak caller can't skip it.

**Docs are not the whole truth — route by what the question is about.** The corpus holds
*documentation*; it does **not** contain a repo's own source facts. "What WAL record types does
OrioleDB have" is answered by the extension's `wal_record.h` X-macro, not any doc — and
`ask_corpus` correctly *abstains* on it. So there is a symmetric primitive for source:

- **`ask_code(question, project)`** — the same extract-then-answer discipline over the actual
  source under `~/Projects`. It derives ripgrep patterns from the question, greps (`--sort=path`
  so `include/` *definitions* rank above `src/` *usages*), reads the matches, and synthesizes a
  cited answer — plus a **RAW SOURCE** block of the literal definition lines, marked authoritative
  over the prose, because models *miscopy value tables* (qwen renumbered an enum whose real code
  was 15 to 8, even when grounded). `project` accepts a path (`orioledb/orioledb-postgres`) or a
  codebase-memory slug (`home-dead-Projects-orioledb-orioledb-postgres`).

**The precision ceiling of grep, and the LSP fix.** Even grounded in the right lines, a model can
misread an *exact* fact — a resolved type, an enum member's value, a signature. Grep finds text;
it doesn't *resolve* symbols. So the last layer is **"LSP for truth, LLM for intent"**: a language
server (rust-analyzer/clangd/gopls/pyright) is the compiler's ground truth. `lsp_hover(file,line,col)`
returns the resolved type/value/doc the compiler *knows*; `lsp_definition`/`lsp_references`/
`lsp_symbols` are semantic (no false positives in strings/comments). This is the real fix for the
miscopied-value-table class of error — you ask the compiler instead of trusting a summary.

**Refactoring is where LSP + LLM compose best.** The server already offers deterministic,
compiler-safe refactorings (rust-analyzer's "Extract into function", "Inline variable", …). We do
**not** replace those — we *add* LLM-backed actions over the same "do something to a code region"
model: `lsp_code_actions` surfaces the server's real menu; `suggest_refactor` has qwen reason over
that **actual** menu (recommend a listed action by its exact title + add naming/structure/
correctness improvements the compiler can't judge); `explain_code`/`propose_improvement` cover
intent-level review. The LLM chooses among *real* refactors, not imagined ones.

## 6. Interfaces (one brain, many front-ends)

- **RAGFlow chat** (`oracle`): docs auto-retrieved + extract-then-answer prompt + code-graph
  tools + Emacs read tools. The daily driver.
- **`oracle-grounded` agent**: every question forced through `ask_corpus` (strongest grounding).
- **`oracle-omni` / `code-graph` agents**: tool-driven code + doc exploration.
- **`ingestor` agent**: point it at a folder/PDF/URL → it classifies, routes, and ingests
  autonomously (self-contained ingestion; the system feeds itself).
- **Local Claude Code** (`claude-local.sh`): the *full Claude Code harness* driven by offline
  qwen via Ollama's native Anthropic API. MCP-wired to codebase-memory + `oracle-ask`
  (`ask_corpus`/`ask_code`) + `oracle-lsp`. An appended **discipline prompt** — precision over
  speed, never answer from weights — is the "prompt loop" that makes a weak model behave: it
  **routes by question type**: documentation/concept/library-API → `ask_corpus`; a repo's own
  source facts → `ask_code`/the code graph; an *exact* symbol type/value → `lsp_hover`; a
  refactor → `lsp_code_actions`/`suggest_refactor`. The same routing is saved as a memory so my
  *real* Claude Code (not just the local one) reaches for these tools too.
- **gptel** (Emacs) + **miniserve** (reading) round it out.

## 7. MCP servers (the tool layer)

All read-only or query-only, bridged stdio→SSE via mcp-proxy, systemd user services:

| server | port | role |
|---|---|---|
| codebase-memory | 9750 | code graph (indexed repos) |
| source-grep | 9751 | ripgrep + read_lines over ~/Projects (exact source the graph lacks) |
| emacs | 9752 | read the user's live buffers (never writes) |
| git | 9753 | log/blame/diff/show (read-only) |
| oracle-ingest | 9754 | classify + route + ingest (powers the ingestor agent) |
| oracle-ask | 9755 | `ask_corpus` (docs) + `ask_code` (source) grounded Q&A |
| oracle-lsp | 9756 | rust-analyzer/clangd/gopls/pyright: hover/def/refs/symbols + code actions + `suggest_refactor` |
| reranker | 9760 | GTE cross-encoder HTTP (Jina rerank API) |

## 8. Key decisions & rationale (the non-obvious ones)

- **RAGFlow pinned to v0.26.4.** The compose bind-mounts the repo's entrypoint.sh into the image;
  a master clone references a script the release image lacks → crash loop.
- **bge-m3 on Ollama, not TEI.** TEI-cpu is slow; TEI-gpu's image is compute-cap 8.0 and the 5090
  is 12.0 → refuses to load. Ollama's bge-m3 is multilingual and coexists in VRAM.
- **Streaming tool-calls need the OpenAI-compat provider.** Plain `@Ollama` mangles streaming tool
  calls (emits them as text). All tool-using agents use `qwen…@OpenAI-API-Compatible`.
- **Disable RAPTOR + GraphRAG per dataset.** Both run the LLM per document at ingest → hours per
  KB. Off → minutes.
- **GTE reranker via pinned transformers 4.48.3.** v5 removed `create_position_ids_from_input_ids`
  → breaks GTE/jina RoPE. Pinned in the service venv = reproducible + offline-safe.
- **Reranker needs bounded top_k.** GTE-on-CPU can't rerank 1024 candidates in the 30 s HTTP
  timeout → cap top_k to 64; `ask_corpus` falls back to embedding order if the reranker is busy.
- **Local Claude Code patch:** MCP tools in the RAGFlow *chat* (not just agents) via a bind-mounted
  `mcp_chat_tools.py` + `dialog_service.py` hook.

## 9. Lessons (transferable beyond this box)

1. **The model is the weak link; scaffold around it.** Deterministic control, scoped sub-tasks,
   tool schemas that validate — not open-ended trust. (C3L's thesis; our agents embody it.)
2. **Grounding beats weights for specifics.** Retrieve exact text; make generation cite it or
   abstain. `ask_corpus` is this as a primitive.
3. **Measure, don't assert.** Every latency/quality claim here was benchmarked (reranker models,
   context ceiling, A/B retrieval) — estimates were wrong more than once.
4. **Split resources by appetite.** Fast VRAM for the bandwidth-bound LLM; abundant CPU/RAM for
   the throughput-bound RAG. The architecture, not a compromise.
5. **Version-pin and materialize.** Offline means the corpus is the source of truth and every dep
   is pinned; nothing fetches at runtime.

## 10. What's still open
- Reranker A/B on Russian corpus pending full book parse (deferred until CPU frees).
- `qwen2.5-coder:3b` for Claude Code's fast/background slot (pull while online).
- `oracle-lsp` surfaces code actions but doesn't *apply* them — resolving a chosen action's
  `WorkspaceEdit` into a preview diff (still read-only) is the natural next step.
- LSP cold-start: the first `code_action` after a server boots can miss while rust-analyzer
  indexes; servers are cached per repo, so it's a one-time warm-up, not per-call.

**Resolved along the way:** Russian PDFs (DeepDoc garbles Cyrillic CID fonts, Новиков→HOBMKOB) are
now reparsed with `pdftotext -layout` into `postgres/ru-books/*.txt`; the ingestor autodetects
Cyrillic and routes there.
