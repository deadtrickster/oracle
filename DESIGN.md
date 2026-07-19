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
- **24 GB VRAM is the scarce resource.** It holds the LLM (+ query embeddings) and nothing else —
  *for a dense model*. A sparse MoE with few active params can bend this rule (see §2, MoE-offload).
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

**When the split bends: sparse-MoE offload.** "The GPU holds the *whole* model" is a rule for a
**dense** model, where every weight is on the bandwidth-bound hot path of every token, so any weight
in slow RAM stalls generation. It is **not** a law of the hardware — it is a consequence of dense
architecture. A **sparse Mixture-of-Experts** model breaks the assumption: only a few experts fire per
token (e.g. `qwen3-coder-next` is ~50 GB of weights but only **~3 B active** per token). So the
right split is by *what's hot*: keep attention, the router, and the dense/shared layers on the GPU;
put the ~50 GB of rarely-touched experts in the 125 GB of system RAM, paged in on demand. The hot
path stays GPU-resident and small; the cold experts ride the abundant side — the same
"split-by-appetite" principle, now applied *inside* one model instead of between the model and RAG.
This is exactly the appetite split we already make for RAG, so it is consistent, not an exception —
**provided the architecture earns it** (few active params, MoE routing). It would be wrong for a dense
30 B, which is why `qwen3-coder:30b` stays GPU-only and `qwen-next` (the 49.6 GB MoE) is the one model
allowed to use GPU **+** ~50 GB RAM. Loading it unloads the 30 B — they don't co-reside. (See
`CODER-NEXT-HANDOFF.md`.)

*Two things are doing the work here, and it's worth not conflating them:*
- **The offload itself is a runtime feature, not a build one.** `llama.cpp`/Ollama place the expert
  tensors on CPU (`--n-cpu-moe N`, or `-ot ".ffn_.*_exps.=CPU"`) — keep attention on the GPU, page
  experts from RAM. This works on *any* MoE GGUF; it is not specific to a vendor's weights.
- **Unsloth's edge is quality-per-gigabyte, which is exactly what a memory-constrained hybrid run
  needs.** An 80 B model compressed to ~50 GB to fit the box *will* lose something to quantization;
  the question is how much. Unsloth's **Dynamic** quants (`UD-*`) allocate bits *per-tensor by
  importance* — sensitive layers (attention, router) keep precision, the bulky experts are squeezed —
  so accuracy-per-byte is high (their Aider-Polyglot "score vs VRAM" curve shows even 3-bit `UD-IQ3`
  holding up). That is why reaching for the Unsloth build is the right call for offload: not because it
  "does" the hybrid placement (it doesn't — the runtime does), but because when you're *forced* to keep
  half the model in slower RAM at a low bit-width, you want the compression that costs the least
  quality. And the two compose neatly — the tensors Dynamic squeezes hardest (experts) are the ones
  offload puts in RAM anyway.

**Serving qwen-next: raw llama.cpp, tuned — and why Ollama isn't enough.** Ollama runs qwen-next at a
respectable ~23 tok/s, but it fixes `--threads`/`--ubatch` internally and offloads by whole layers, so
you can't tune the MoE hybrid. Running llama.cpp's `llama-server` directly (the *same binary* Ollama
bundles — point `GGML_BACKEND_PATH` at `cuda_v13/libggml-cuda.so` + `LD_LIBRARY_PATH` at the Ollama lib
dirs to get its CUDA backend) exposes the knobs, and a config sweep on this box (24 GB VRAM, 24 cores)
found the win:

- **Prompt processing (PP) and token generation (TG) want *opposite* tuning, and one thread count can't
  serve both.** PP ingests the whole prompt at once — matrix×matrix, compute-bound, parallel — so it
  wants **all cores** (`--threads-batch 24`) and a **fat `--ubatch`** (2048 → ~1.5–2× the PP of Ollama's
  512, because a bigger batch amortizes the streamed expert weights over more tokens). TG is one token
  at a time — matrix×vector, **memory-bandwidth-bound** — so piling threads on it just makes them fight
  for DDR5 bandwidth: **`--threads 24` collapsed TG to ~2 tok/s; `--threads 8` gave ~34.** The
  counterintuitive result — *fewer* threads for generation — is the whole reason to leave Ollama, which
  uses one `--threads` for both.
- **`--n-cpu-moe` fills VRAM deliberately.** `-ncmoe 28–30` keeps ~22.5 GB of experts on the GPU (the
  24 GB ceiling; `≤22` OOMs) and offloads the overflow to RAM — the *opposite* of the naive
  `-ot ".ffn_.*_exps.=CPU"` (banishes all experts → wastes VRAM → 1.5 tok/s).
- **`--no-mmap` over mmap** for a coding agent: it loads experts into anon RAM so the PP GEMMs run
  fault-free (~1233 vs ~860 tok/s PP), at the cost of a ~48 s cold start — worth it because grep-heavy
  agent turns hammer PP. (`--mlock` is the middle ground: mmap + pinned, best TG ~38, mmap-class PP, and
  it guarantees the model never swaps.)

**Net: PP ~1.2 k tok/s and TG ~32–34 — beating Ollama on both — from `oracle-qwen-next.service`.** The
stack is wired so a *single* qwen-next serves everything: the agent (`qwen.sh` → shim, which already
POSTs to `/v1/chat/completions`, repointed at `:18080`) and synthesis (`ask_corpus`/`ask_code`, whose
`_chat` now uses the same OpenAI endpoint). Ollama is demoted to serving **only bge-m3** embeddings,
which fit in the ~3 GB VRAM headroom — so there is exactly one big model on the GPU and no swap thrash
(the failure mode when two qwen-nexts — Ollama's synth copy and the tuned server — fought for VRAM and
one landed on CPU at 100%).

**Two gotchas when Claude Code drives a raw llama-server (both cost a debugging session):**
- **`--jinja` is mandatory.** Claude Code sends its tool schema on *every* request; llama-server under
  `--no-jinja` rejects any request carrying a `tools` param (`tools param requires --jinja flag`, HTTP
  500), so *every* turn fails — even a bare "continue" — not just tool-heavy ones. Enabling `--jinja`
  (which applies the model's chat template) is what makes the agent path work at all.
- **Match the server's `-c` to the context window Claude Code *believes* the model has, or exceed it.**
  Claude Code auto-compacts as a conversation approaches the window it *thinks* the model has (200K for
  an unrecognized model). It never compacts below that. So a 143K-token conversation sails past a server
  capped at `-c 131072` **without ever triggering compaction** — the safety net is calibrated to the
  believed 200K, not the real 128K — and llama-server rejects the request (`exceeds the available
  context size`), surfacing as an empty "Worked 0s" turn. Fix: set `-c 262144` (256K) so the real limit
  *exceeds* Claude Code's belief; the hybrid-attention KV cache is small enough that 256K still fits in
  ~21 GB. Now compaction fires at ~200K with the server holding comfortably more. The bug was never the
  size — it was two components disagreeing about how big the box is.

**KV prefix caching and warm session-switching.** After the first turn of a session, follow-up turns
are *much* faster — the log line `selected slot by LCP similarity, sim_best = 0.994` is why. llama-server
keeps each slot's KV cache and, on a new request, routes it to the slot whose cached tokens best match
the new prompt's **prefix** (longest-common-prefix). A coding conversation's prefix (system prompt +
history) is ~99% stable turn-to-turn, so all but the newly-added tokens are reused instead of re-processed
— the first turn pays full prompt-processing over the whole ~90 K-token context (~350 tok/s PP, several
minutes), every turn after pays only the delta. CUDA-graph capture (`graphs reused = …`) compounds it.
The cache is **per slot** (each slot holds one sequence's KV; reuse is by routing, not cross-slot sharing);
`kv_unified` only controls whether slots draw from one shared KV *memory pool* vs. fixed partitions — the
cached *content* is per-sequence either way. This sets up a real `--parallel` trade-off:

| `--parallel` | warm caches | switching cost | concurrent speed |
|---|---|---|---|
| **1** | one session warm | switch = full cold re-PP | n/a (requests queue) |
| **2–3** | each session keeps its slot warm | instant | tanks to ~0.2 tok/s **iff two generate at once** |

The 0.2 tok/s collapse is caused by two requests running at the *same instant* (one slot's 24-thread PP
starves the other's bandwidth-bound TG), **not** by having multiple slots. So `--parallel 2` used
*sequentially* (one session active at a time) gives warm switching for free — which is why we run 2, not 1.
The hard limit is VRAM: two warm prefixes = two KV caches resident; with `kv_unified` the ~256 K pool is
*shared*, so two ~120 K sessions coexist but two huge ones evict each other, and splitting into fixed
128 K partitions would reintroduce the overflow bug above. Forking the scheduler can't move that ceiling.

*Parked option — persist KV to RAM/disk (no fork).* llama-server already exposes `--slot-save-path` plus
`POST /slots/{id}?action=save|restore`, which dump/reload a slot's KV as a file (a memcpy, ~instant vs a
multi-minute re-PP). It isn't automatic because "session stop" is a *client-side* event the server never
hears — neither Claude Code nor the shim maps it to a save call. qwen-next's hybrid-linear KV is small and
we have 125 GB RAM, so pointing `--slot-save-path` at a tmpfs and teaching **the shim** to save the active
slot on idle (keyed by session) and restore it before the next session's first request would give
effectively unlimited warm sessions, surviving even server restarts. Caveat: a dump is valid only while
the token prefix is unchanged — editing the DISCIPLINE system prompt, a **context compaction that rewrites
history**, or any model/quant/flag change voids it — and it's coupled to the exact llama.cpp build.

## 3. Components

| Layer | Choice | Why |
|---|---|---|
| LLM serving | **Ollama** (qwen3-coder:30b, codestral) + **tuned llama.cpp** (qwen3-coder-next, `:18080`) | Ollama for GPU-only models + APIs; raw llama-server for the MoE-offload qwen-next it can't tune (see §2) |
| Context | **56K** (`OLLAMA_CONTEXT_LENGTH=57344`), q8_0 KV + flash-attn | max where qwen+bge-m3 both stay GPU-resident (2.1 GB free); 64K evicts bge, 96K spills to CPU |
| RAG hub | **RAGFlow v0.26.4** (Docker) | best-in-class DeepDoc parsing (2-col PDFs, tables), CPU embeddings, GUI, agents, MCP client |
| Embeddings | **bge-m3** via Ollama | multilingual (Russian PG books!), 0.66 GB, coexists with qwen |
| Reranker | **gte-multilingual-reranker-base** (CPU service) | 2-stage retrieval; multilingual; ~2.7 s/30 chunks; the highest-ROI retrieval upgrade |
| Code structure | **codebase-memory** graph | call graphs / struct lookups that RAG-chunking C can't do |
| Reading UI | **oracle-browser** :9765 | search → the rendered source page w/ query highlighted; `/browse` corpus tree (subsumed the old miniserve :9800) |
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

- **C/C++**: cppreference (6,635 pages, sanitized HTML→md), serenedb's deps (abseil/fmt/simdjson/faiss).
- **DuckDB / Kubernetes**: official docs (the serenedb engine; k8s ops).
- **Biology**: 6 Russian books (`bio`, text) + 4 OpenStax textbooks (`bio-books`, PDF). See below —
  the biology corpus is where every corpus-quality bug surfaced, because it is the only one written
  in an inflected language, half of it OCR'd, and full of exercise questions.

**API-doc sanitizer** (`sanitize-apidocs.py`): rustdoc/mdBook HTML → clean per-module markdown
(extract `<main-content>`, merge item pages). 785 MB of HTML → 100 MB of ingestable markdown;
raw HTML is never ingested.

### 4.1 Corpus hygiene — what we deliberately do *not* ingest

A textbook is not all answers. It is also exercise questions, answer keys, indexes, bibliographies
and publisher front-matter. We were embedding all of it, and it actively hurt.

**The exercise-question trap (measured 2026-07-13).** A user's query is a *question*. A chunk of
"Вопросы для повторения" is also *questions*. So they embed close together, and **the textbook's own
question lists out-compete the passages that answer the query**. In the `bio` KB, bogdanova was
**13.6% question-list chunks**; 6 of the top 30 hits for *"что такое фотосинтез"* were exercise
questions displacing real content. We were also retrieving the УДК/ББК/editorial-board page.

`clean-corpus.py` runs **before** ingest and strips two things:
1. **Question runs** — a run of ≥3 consecutive *standalone, short, interrogative* paragraphs is
   exercise material. A *lone* rhetorical question inside prose survives. **OPT-IN per corpus**
   (`books.toml`), default **off**.
2. **Page ranges** (manual scalpel, `books.toml`): answer keys, indexes, front-matter — the things no
   heuristic can see. Possible only because we now emit page markers (below).

**Why opt-in, and why the obvious heuristic is wrong.** The first version also dropped any paragraph
containing ≥3 question marks. On the biology textbooks it looked fine. On the Postgres Pro books it
ate the **WAL chapter** of `pg_monitoring` (technical prose that poses questions and then answers
them) and — the one that settles the argument — **a jsonb operator table**:

```
@>(jsonb,jsonb) | jsonb_contains | 7    ?(jsonb,text) | jsonb_exists | 9
```

**`?`, `?|` and `?&` are PostgreSQL operators.** A table of them is indistinguishable from a list of
questions if you are counting question marks. Counting `?` is not a signal; **structure** is. So the
inline rule was deleted, and stripping is now something a corpus opts into: right for an exam-prep
textbook whose quizzes out-compete its own chapters, wrong for a reference manual. A destructive
filter must prefer precision to recall — the cost of a false positive (silently deleting the WAL
chapter) is far higher than the cost of a false negative (one quiz block survives).

**Page markers.** RAGFlow's *naive* (text) parser records no positions — every chunk gets the stub
`positions=[[2,1,1,1,1]]`. Only the DeepDoc PDF parsers (`book`/`paper`) store real page+bbox. So
`pdf2txt.sh` and `ocr-pdf.sh` carry the page number **in the text itself** as `[[p.N]]`. That buys
three things: page-range exclusion, citations that can say *"Chebyshev, p. 412"*, and a corpus
browser that can deep-link into the original PDF.

**PDF ingestion decision matrix** (learned the hard way):

| | route | why |
|---|---|---|
| Cyrillic PDFs | `pdftotext` → `.txt`, naive | DeepDoc garbles Cyrillic CID fonts (Новиков → HOBMKOB) |
| scanned PDFs | `ocr-pdf.sh` (tesseract) → `.txt` | no text layer at all |
| Latin PDFs | **PDF → DeepDoc** (`book`) | keeps page+bbox positions **and extracts figures** — a biology textbook is half diagrams, which `pdftotext` silently discards |

Two traps in that last row, both of which fail *silently*: RAGFlow's uploader accepts up to 1 GB but
its **DeepDoc worker refuses files >128 MB** — and still reports `run=DONE, progress=1.0` with **zero
chunks**. The OpenStax books ship at 178–455 MB, so they are downsampled with Ghostscript (`/ebook`,
150 dpi) to 29–73 MB. Page counts are preserved 1:1 — splitting the PDFs instead would restart page
numbering per part and destroy the mapping. Originals stay pristine in `~/Documents/Books/`; only
`corpus/` (disposable) holds the compressed copies. `ingest-corpus.py` also caps upload batches by
**bytes**, not just file count (batching 1.3 GB of PDF into one multipart request → HTTP 413).

### 4.2 Curation is a JUDGMENT — filter chunks, and judge them with a model

Two corrections to §4.1, both learned by getting it wrong.

**Wrong layer.** `clean-corpus.py` filters `.txt` — so every PDF that goes to DeepDoc (all the English
books) escaped filtering **entirely**. We had filtered the *format* instead of the *thing*.
⇒ **`clean-chunks.py` filters CHUNKS**, which is where both parsers converge and what actually gets
embedded and retrieved. Parser-agnostic by construction.

**Wrong judge.** Every rule we wrote encoded the *surface form* rather than the thing:
- `?` is **not** a question — `?`, `?|`, `?&` are **jsonb operators**, and the "count question marks"
  rule deleted a PostgreSQL operator table and a chapter of WAL prose;
- a question **need not contain** `?` — `A11. Корнеплод — это... 1) ... 2) ...` and OpenStax's
  `3. The smallest unit ... is the ________.  a. organ  b. organelle` are both invisible to a
  `?`-based rule. Our "cleaned" biology books were still full of quiz items;
- (and, in §5.2, `мышей` is not `мышь` until a stemmer makes them one token).

*Purpose cannot be compiled into a regex.* ⇒ **`chunk_judge.py`: qwen decides.** Prompt adapted from
MT-Bench (via Lambert, *RLHF* §5.7 — in our own corpus): explicit criteria, a one-sentence explanation
*before* a strictly-formatted verdict, an instruction not to be swayed by length, and — crucially —
**our own scar tissue as counterexamples** (it is told that `?` is a PostgreSQL operator; a generic
judge would repeat the rule's mistake).

**It is a CASCADE, not a replacement** — the same shape SLP3 gives for retrieval (§5.2). Rules cannot
score 283k chunks *well*; qwen cannot score them *fast*.

```
  every chunk → cheap recall-oriented rule (flag anything questionish; false positives are FINE)
              → 1.7% survive as candidates
              → qwen judges each one          → 26 minutes, not 26 hours
```

Safety properties, each one paid for by a bug elsewhere in this system:
- **A judge error votes KEEP.** A failed judge must never become a silent deleter (cf. the reranker's
  invisible timeout fallback, §8).
- **"If unclear → CONTENT."** Deleting real knowledge is far worse than keeping a stray quiz item.
- **Every verdict is written to a JSONL audit trail.** Swapping a rule we can inspect for a model we
  cannot would be a bad trade.

Measured (2026-07-14): **7/7** on the labelled fixtures (`tests/test-judge.py`). On the real corpus it
cut **286 chunks** from the OpenStax books (the multiple-choice questions no rule of ours could see)
and **zero** from the reference manuals (Rust Patterns 43 judged/43 kept, Database Internals 37/37) —
it distinguishes a textbook from a manual without being told which is which. On `bio` it *rescued*
25% of what the rule had flagged.

**And it did not improve retrieval.** Deleting `bio`'s 221 exercise chunks moved the gold passage from
rank 32 to **31**. The corpus-poisoning thesis failed its own test: the passage that beats the rodent
list is not a quiz, it is **Рукокрылые** — *летучие мыши*, "flying mice". Corpus hygiene is defensible
on its own terms (less noise in every context window — Axiom 1), but it is **not** a retrieval fix.
The retrieval fix is the pool and the reranker (§5.2, and TODO §E Phase 2).

**The honest limit.** None of this is a retrieval silver bullet. Cleaning removes material that was
competing *unfairly*; it does not make a weakly-embedded passage win. Our worst case — *"какие виды
мышей"* — stays broken after cleaning: the passage listing rodents (cos **0.471**) still loses to one
about **Рукокрылые / летучие мыши** — literally *"flying mice"* (cos **0.762**). The bi-encoder is not
being stupid; it is doing exactly its job, which is **topical proximity, not answerability**. Only a
cross-encoder sees query and passage together and can judge whether a passage *answers*. That is why
the reranker is not a nicety — and why its silent 30 s timeout fallback (§8) is a correctness bug,
not a performance one.

### 4.3 Diagram-OCR garbage — a *third* noise class, and why it needs remove+reingest, not delete

Exercises and apparatus (§4.2) are *whole-chunk* problems: a quiz chunk is a quiz chunk, delete it.
**Diagram-OCR garbage is not.** DeepDoc's `book`/`paper` parsers pull out regions the layout model
labels `figure` (`_extract_table_figure(need_image=True, …)` in `rag/app/{paper,book}.py`), but when
the layout model **mislabels a diagram as `text`**, its OCR is flattened into the text stream — box-
drawing glyphs (`口□□□`), repeat runs (`DDDDDD`), shredded words (`rylooku`, `arely consulted`), loose
chart axis numbers. Observed live: two chunks of the ClickHouse huge-pages paper interleaved that noise
*with legit prose* (`huge pages`, `backend reads a buffer`, `shared_buffers`) in the **same** chunk. So
the split is **inside** the chunk — a whole-chunk `to_delete` throws away the good text; keeping it keeps
the noise.

The fix is a three-way classify in the post-pass, plus a per-chunk repair that respects embeddings:

| class | action |
|---|---|
| ToC / index (pure apparatus) | delete immediately — the existing `to_delete` path |
| **junk** (diagram OCR mixed with prose) | excise the garbage span, write `(chunk_id, doc_id, cleaned_text, snippet)` to a **worklist file** |
| clean | keep |

Then a loop over the worklist **removes the old chunk and re-adds the cleaned text as a new chunk**.
Why not `PATCH` the content in place? Because `PATCH …/chunks/{id}` updates the **text but not the
embedding vector** — a patched chunk keeps its *garbage* embedding and still gets retrieved. `DELETE` +
add-chunk forces a **fresh embedding** on the clean text. The worklist file is the checkpoint: you review
what is about to be re-cut before anything destructive runs, and it is the audit trail. If excision leaves
nothing legit, it collapses to a plain delete.

**Root cause is upstream, and we will patch it later — like the word boundaries.** The garbage exists
because the layout model (`deepdoc/parser/pdf_parser.py`, `_layouts_rec` → onnx DLA) mislabels the
diagram. The parser *already* has box-level garbled-text filters right where the fix belongs
(`pdf_parser.py` ~810: **Strategy 1** PUA/unmapped-CID chars, **Strategy 2** font-encoding garble → clear
the box, fall back to OCR) — the same place we earlier fixed word-boundary inference "from geometry
instead, as pdftotext does." A **Strategy 3** (box-drawing/replacement-glyph density, long repeat runs →
drop the box *before* chunk assembly) is the correct upstream fix: it removes the garbage box while keeping
adjacent prose boxes, so no mixed chunk is ever formed. But that is a change to RAGFlow's code inside the
container against pinned **v0.26.4** — deferred on purpose. For now the retrieval-side remove+reingest
pass cleans what is already indexed; **[TODO §G: return and patch DeepDoc]** closes it at the source.

**The deterministic detector hit its precision ceiling — next step is a trained CPU classifier.**
The glyph-density rule shipped in `chunk_judge.find_diagram_garbage` is *safe* (a token-diff over every
flagged chunk in `papers` showed it removes only stray glyphs — `●`, `口`, `（）`, `DDDDDD` — never a
real word) but it cannot *decide*: it flags 9% of `papers` and 15% of `books`, because **a per-token
rule cannot tell a garbage `●` from a meaningful one** (`●` inside flattened-diagram OCR vs `●` as a
chart-legend marker or bullet). That is a fuzzy judgment — the same shape as exercise curation
(§4.2: *"purpose cannot be compiled into a regex"*) and the same lesson as the reranker: judgment
calls want a model. So the detector is demoted to **recall-oriented candidate generator**, and the
verdict moves to a **trained classifier on the CPU tier** (with the reranker and bge-m3 — never
fighting the GPU).

Crucially it is **one** model over the **whole** junk taxonomy, not a figure-garbage classifier: ToC,
index, glossary, exercises, bibliography, figure-OCR garbage, per-page boilerplate. It **replaces the
patchwork** — the `is_obvious_toc` rule, the statistical boilerplate strip, the recall rules, the GPU
qwen judge, and this new glyph detector collapse into a single multi-class CPU model whose output
drives the action (`keep` / `delete` / `excise` / `strip`).

- **Features are already computed:** every chunk's **bge-m3 embedding sits in ES** (`q_1024_vec`,
  1024-dim) — semantic features for 203 K chunks for free — plus **38 surface features**, each earning
  its place as a signal for a specific class, most paid for by a documented scar: weird-glyph density +
  repeat runs + unique-token ratio (figure garbage — OCR gibberish never recurs), dotted-leader density
  (ToC), short-line / page-number-line / definition-dash / **alphabetized-line-order** ratios
  (index/glossary — an index is sorted), MC-option + numbered-question + `?` + **answer-key-pair**
  density (exercises), cite-key + URL + **year + et-al/pp/ISBN** density (bibliography),
  **stopword ratio + sentence-end-line ratio** (the strongest cheap prose-vs-apparatus split — §5.1b's
  47-char layout debris has neither; measured on `books`: stopword 0.15 vs 0.30, sentence-end 0.28 vs
  0.62 for index-ish vs prose), **code-token ratio** (protects operator tables — the jsonb `?` scar,
  §4.2), **title-overlap** (running heads repeat the doc name), HTML-table flag, `[[p.N]]` density, and
  **provenance/position**: source format, `img_id` (present ≈ DeepDoc-parsed; it is the chunk's own
  page-crop, so provenance not "contains a figure"), and `page_num_int` → **relative position in the
  document at train time** (ToC lives in the first ~5% of a book, index/bibliography in the last ~5%).
  Doc-level features (page-relative rank, distance from the document's embedding centroid — garbage is
  an outlier against its own book) are derived at train time from the stored `page_first`/`doc_id`/`emb`.
  The trusted regexes become **numeric features**, so the model learns the thresholds and combinations
  the rules had to hardcode. (Extractor: `build-junk-features.py`, read-only, `q_1024_vec` + 38 surface
  features → `.npz`.)
- **Model:** gradient-boosted trees / MLP over those features first; if precision stalls, fine-tune a
  small multilingual encoder on CPU — *training time is explicitly not a constraint* (hours are fine);
  only scoring must stay cheap, and a forward pass over stored vectors is.
- **Labels by bootstrap, not hand-labelling 200 K:** rules nominate candidates, the qwen judge
  weak-labels a few thousand, a human spot-checks via the same JSONL audit trail as §4.2.
- **Output classes:** `clean` / `excise` (surgical token removal, then remove+reingest) / `delete`.
- **A probe on the RAW collection KB (uncurated) validated the signals and grew the taxonomy.** The
  flagged ToC chunks were real ToC; index-ish separated at alpha-sorted 0.90 / stopwords 0.08 vs prose
  0.28. But most flagged "debris" was a class we didn't have: **OCR-mangled code listings** — numbered
  Java lines OCR'd from textbook page images (`GridPa e` for `GridPane`, `R0UND` with a zero, fullwidth
  `，` commas). Code in md/txt must be KEPT (the jsonb scar); code OCR'd from PDF images is *damaged* —
  a model quoting `GridPa e` as API truth is the miscopied-value-table failure — so the verdict hinges
  on the interaction `code_ratio × is_pdf × weird_density` (the fullwidth glyphs are already in our
  weird range). Rules can't weigh a three-way interaction; the model can. Strongest concrete argument
  for the classifier so far.

**Where the chunks actually live, and the read/write asymmetry.** RAGFlow stores every chunk in
**Elasticsearch** (`DOC_ENGINE=elasticsearch`, ES 8.11.3 in `docker-es01-1`, exposed on host
**`localhost:1200`**, auth `elastic:infini_rag_flow`). All KBs share one tenant index
`ragflow_<tenant_id>` (here `ragflow_a73b470e7d6111f1b22afb6d9f0455fb`, ~203 K chunks / 4.3 GB); each
chunk doc's `_id` is the RAGFlow chunk id, with `content_with_weight` (the text), `doc_id`, `kb_id`,
`docnm_kwd`, the BM25 token fields (`content_ltks`/`content_sm_ltks`), and the embedding
`q_1024_vec` (bge-m3, 1024-dim). This makes the cleanup pass **read direct from ES but write through
the API**: the detection scan filters the index by `kb_id` and pulls `content_with_weight` in bulk —
far faster than the API's 100-per-page pagination across 161 books — but a *write* must go through
RAGFlow's add-chunk endpoint so it regenerates `q_1024_vec` **and** the token fields. Writing raw to ES
would mean recomputing the bge-m3 vector and the tokenizations by hand and matching the index mapping —
fragile, and exactly the embedding-consistency trap the remove+reingest design exists to avoid.

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

### 5.1 Synthesis is for weak readers — a strong reader wants the raw chunks

The synthesis step (stage 3) exists to protect a **56K-context weak model** from a firehose of
passages. But it is *lossy compression performed by the weakest component in the pipeline*, and when
the caller is a strong model, it is pure loss.

Observed directly (2026-07-13): asked to explain corpus cleaning, `ask_corpus` returned a competent
paragraph — and, as "evidence", a **hypothetical Python function it had invented**. Retrieving the
same material as **raw chunks** and reading them unsynthesised instead yielded the two facts that
actually mattered, verbatim from Jurafsky & Martin:

> *"The bi-encoder … is less accurate, since its relevance decision can't take full advantage of all
> the possible interactions"* — i.e. **topical proximity, not answerability**, stated as architecture.
>
> *"Use cheaper methods (like BM25) as the first pass … then use expensive methods … to rerank only
> the top N"* — from which follows the thing we had missed entirely: **the first stage sets the
> ceiling; rerank can only reorder what the first pass already found.**

qwen's summary contained neither. It could not have: summarising *is* discarding, and it discards
what it does not recognise as important.

**Principle: never put a weak model between a strong model and the source.** It is the same defect as
letting qwen summarise grep output (it miscopies value tables — hence the RAW SOURCE block in
`ask_code`), and the same one as the Muridae fabrication (qwen writing prose on top of an honest
abstention). The corpus is a **library**, and a strong reader should be allowed into the stacks.

⇒ **`search_corpus` (shipped 2026-07-15)**: same retrieval + rerank, returns the top-k chunks
**verbatim** with source + page marker, no synthesis. `ask_corpus` stays for weak callers; strong
callers (including *me*, on the plane) read for themselves.

### 5.3 Retrieval config, as shipped

- **Pool = 64, synthesis slice = 18** (`_retrieve`/`_diversify`). recall@64 measured at 100%, but
  the gold often ranks 15–18, so a narrow slice retrieved the answer and then dropped it before
  synthesis. 64 stays inside the reranker's 30 s timeout (~10 s); 256 would need the reranker
  parallelised first, so it is deferred.
- **Query normalization** (`_normalize_query`, retrieval only): strip conversational framing
  ("какие виды X ты знаешь" → "виды X"; "tell me about Y" → "Y"). Filler drags the dense vector
  toward other *questions* and dilutes the lexical match; synthesis still sees the original.
- **Output language pinned** to the question's language in the synthesis prompt — qwen is
  Chinese-trained and otherwise code-switches into Chinese on Russian input (half the corpus).
- **Reranker fallback is visible**: every answer is tagged `[reranked]` or `[embedding-order
  (reranker busy)]`, so a silent-timeout degradation is observable, not invisible.

### 5.1b Chunk size — the `book` parser silently ignored `chunk_token_num`

A retrieval system's unit of truth is the **chunk**. Ours were 47 characters.

`chunk_token_num = 512` is set on every KB, and the `naive` parser honours it (median chunk **1168
chars**). The DeepDoc `book` parser does not — median **47 chars** in `books`, **67** in `bio-books`.
A 20× disagreement between two parsers in the same system, on the same setting.

Cause (`rag/app/book.py`): the parser takes `hierarchical_merge` whenever a bullet/heading pattern is
detected — i.e. for **every real textbook** — and that function never reads `chunk_token_num`. It
accumulates against a **hardcoded 218-token** limit, *and only merges singleton groups*: anything the
bullet detector groups together is emitted as-is, however small. DeepDoc's layout analysis classifies
TOC lines, running heads and page numbers as "sections", each matches a bullet pattern, and each
becomes its own chunk. `naive_merge` — the one branch that honours the setting — was dead code for
real books.

Measured on SLP3 (500 chunks): **256 under 50 characters**, 399 under 150, **none over 1000**. A
representative chunk, in its entirety:

```
133 The nature of preferences10 reward functions 138
```

That is a table-of-contents line, embedded and indexed as if it were a passage.

**Why it matters more than it sounds.** ~126k of ~300k chunks in the corpus are layout debris — and
they are concentrated in our *best* sources (SLP3, DDIA, Sutton & Barto, CLRS, Database Internals). A
50-char chunk's embedding is close to noise, and noise is exactly what wins when everything scores
~0.35 (§5.2). And a top-8 retrieval hands the model **~500 characters of rubble** — an **independent
second cause** of recall@8 = 40%, on top of the pool being too small.

**Fix — TWO bugs on the same branch** (which is *why* nobody noticed it was broken: it was already
dead code). Both patched in `rag/app/book.py`, bind-mounted:

1. **`chunk_token_num` is never consulted.** Take `naive_merge` when it is set.
2. **The `naive_merge` branch destroys the page positions.** The DeepDoc position tag is
   `@@page\tx0\tx1\ttop\tbottom##` — a **double** at-sign. Upstream splits on a **single** `@`:

   ```python
   "foo@@1\t2\t3\t4\t5##".split("@")   ->   ["foo", "", "1\t2\t3\t4\t5##"]     # THREE parts
   ```

   so `len(pr) == 2` is false, the else-branch fires, and **the position tag is discarded**. Split on
   `"@@"` instead: `naive_merge`'s `add_chunk()` re-appends `pos` to the text
   (`if t.find(pos) < 0: t += pos`), and `tokenize_chunks → pdf_parser.crop()` then recovers page+bbox.

Measured on one book before re-parsing all 19 (`lbdl.pdf`):

| | unpatched | patched |
|---|---|---|
| chunks | 637 | **66** |
| median chars | 47 | **2302** |
| chunks < 50 chars | 51% | **0%** |
| chunks with page positions | 0/66 | **66/66** |

So we keep DeepDoc's page mapping **and** get sane chunks — but only after fixing bug 2. The
trade-off we thought we faced (good chunks *or* a corpus browser) did not exist; the code was simply
wrong in two places. Requires re-parsing `books` and `bio-books`.

**And the lesson, which is this system's recurring one:** the setting was accepted by the API, stored
in the config, and displayed back to us — then silently ignored by the code path that actually ran.
Nothing errored. Nothing warned. We only found it because the chunk *counts* looked odd (4 English
books → 60k chunks; 6 Russian books → 6.6k), and someone asked why.

### The corpus browser — the verification surface

The promise the page markers were *for*: **a grounded answer you can check against the original,
offline.** `ask_corpus`/`search_corpus` give the passage; the browser (`oracle-browser.py`, `:9765`)
gives the passage **and** the source page it came from, one click away. The design principles that
emerged, each from a concrete failure:

- **Show the page, not the chunk.** A retrieved chunk is reconstructed `pdftotext` — re-wrapped, salted
  with `[[p.N]]` markers, fragmented by diagrams. Unreadable. So a result renders the *actual PDF page*
  (`pdftoppm`, 200 dpi) via DeepDoc bbox or the `[[p.N]]` marker map. You read the typeset page, not our
  reconstruction of it.
- **The page lookup is a two-source trust rule.** DeepDoc PDFs carry a real `[page,x0,x1,top,bot]` bbox
  — trusted *only* when non-degenerate, because naive/text docs store a bogus counter there
  (`[[619,618,…]]`) that once put the viewer past the end of the book. Text docs are located instead by
  probing the source `.txt` for a **distinctive multi-word phrase** (the first word alone matched a ToC
  entry and sent every link to p.9) with a separator class that spans **digits** (a `"кластера. 4.
  Перед…"` chunk otherwise failed to match and fell back to ugly text).
- **Highlight the intent, not the string.** Query *anchor nouns* are boxed on the page image (word
  bboxes from `pdftotext -bbox`, expressed as page-fraction % so they track the responsive image) and
  `<mark>`ed in text/markdown. Matching is by Cyrillic/Latin **stem** with a conversational stoplist, so
  *"какие виды мышей ты знаешь"* highlights `вид`/`мыш` and drops the scaffolding.
- **One browser, three source shapes.** PDFs → rendered page; markdown → GitHub-flavoured render
  (front-matter stripped), framed in serif to sit beside the page images, with a left **nav tree** of
  the doc's directory for continuous reading; everything else → raw. `/browse` is the corpus folder
  tree — which retired the separate static-file server (miniserve) that used to do that job.
- **Native-feeling mechanics.** ←/→ flip pages *in place* (next image decoded before the swap → no
  flash) with ±3 precached; headers show the real source filename / front-matter title, never the
  `<subdir>__<file>.txt` ingest slug.

**What the browser exposed:** apparatus — a book's index, table of contents, bibliography — is the
*densest possible keyword match* and the *least useful answer*. It out-ranked real chapters on keyword
queries (`raft` → the index). The fix was not a browser feature but a corpus one: the curation judge
(above) was widened to DROP apparatus, and unambiguous ToC chunks (≥4 dotted-leader lines) were deleted
outright. *Garbage doesn't have to be wrong to poison you; it only has to be shaped like the query.*

### The eval harness — validating the agent, and tuning the prompt against it

The browser lets a human verify one answer. The eval harness (`eval-agent.py` + `EVAL.md`) does the
same thing systematically, for the whole agent — because the recurring failure of this system is a
model that is *fluent and wrong*, and fluency is exactly what a casual read rewards. So the answer key
is written down BEFORE the run, and a change to the prompt or the tooling is **judged, not admired**.

Three design choices carry the weight:

- **A suite is a CONVERSATION, not a bag of prompts.** The failure we most need to catch —
  **grounding decay**, where qwen grounds turn 1 and then answers the rest from parametric memory — only
  appears on turn 2+. So the harness drives all questions in one session (`--session-id` then `--resume`)
  and records **tool calls per turn**, not just the final text. Zero tool calls on a later turn is the
  smoking gun.
- **It measures the agent you actually ship.** The driver runs qwen through `~/bin/qwen` → `qwen.sh`,
  *never* bare `claude`, so the production DISCIPLINE prompt, the MCP config, and the tool trim are all
  injected; testing a bare client would measure a different agent. Injection is verified from qwen.sh's
  banner every turn (Claude Code doesn't persist the appended system prompt to the transcript).
- **The grader is a frozen instrument.** Each question's rubric encodes `must`/`trap`/`grounded`/
  `read_source`, and — for enumerations — compares the model's set against the **real source set**
  (e.g. A4's `WAL_REC_*` codes are diffed against `wal_record.h`: fabricate one and it fails). The
  rubric is never edited to make a variant pass; that would be gaming the ruler.

**The finding that justified all of it: grounding ≠ correctness.** On **Suite A** (Postgres/OrioleDB,
well covered by both corpus and clean source) qwen is genuinely good — it read `wal_record.h` and
enumerated all 19 real WAL records with zero fabrication. On **Suite B** (serenedb, a large private C++
codebase with *no* corpus coverage) it scored **0/4**: it called tools and then synthesized generic,
confidently-wrong answers, on B2 never opening the source at all. The facts it missed were all
`grep`-findable in seconds — so the bottleneck is not the tools but the model's search-and-synthesis on
an unfamiliar target. That is the boundary of the local stack, and the concrete case for evaluating a
larger model (`qwen-next`).

**Tuning the prompt is itself a closed loop.** Rather than argue about prompt wording, DISCIPLINE
tweaks are run as a **tournament**: each variant is a `discipline/*.txt` file appended to production via
qwen.sh's `ORACLE_DISCIPLINE_EXTRA` hook, and `--tournament` scores baseline + every variant across all
suites with the *same frozen rubric*, then ranks them. The prompt improves or it doesn't, measured
against a constant — the same discipline we apply to the corpus and the tools, now applied to the words
we put in the model's mouth. (Working state and the iteration log live under `discipline/`.)

### 5.2 The lexical channel — a 1972 solution we weren't using

Retrieval is hybrid: RAGFlow blends a **token** score with a **vector** score, and the default weight
is `vector_similarity_weight = 0.3` — i.e. **70% of the score is lexical**. That half was broken for
half our corpus, and it took a measured retrieval eval to see it.

RAGFlow's tokenizer stems **English** (Porter: `running`/`runs` → `run`) and leaves **Cyrillic**
lowercased and otherwise untouched. In an inflected language that is fatal:

```
query   "какие виды мышей ты знаешь"   →  token "мышей"   (genitive plural)
chunk   "Представители: мышь, полевка"  →  token "мышь"    (nominative)
```

Two unrelated tokens. They never match. So **the only informative, high-IDF term in the query matches
nothing**, while `виды` ("species" — in a *biology* textbook) matches everywhere and steers the query
into noise. IDF is not missing here; **IDF cannot rescue a term that never matches.** Spärck Jones
solved term weighting in 1972 and it does not fire without the stemmer it depends on.

Fix (`ragflow/rag/nlp/rag_tokenizer.py`, bind-mounted): run the Russian **Snowball** stemmer over
Cyrillic tokens. It is applied on **both** sides by construction — the same `tokenize()` is the single
entry point for the indexer (`rag/nlp/__init__.py:360`, `content_ltks`) and the query builder
(`rag/nlp/query.py:61`) — which is the whole point: a stemmer is only useful as an **invariant**.

```
мышь / мыши / мышей / мышам / мышью   →  мыш       one invariant
мышца / мышцы / мышц                  →  мышц    ┐ verified DISJOINT — the feared
мышечный / мышечные                   →  мышечн  ┘ mouse↔muscle collision does not occur
```

Cost: every Cyrillic document indexed before the patch must be **re-parsed**, or its stored tokens
will no longer match a stemmed query. (Done for `bio` and the 7 Postgres Pro books.)

Measured effect on the gold passage's rank, before any of this (query-side experiments that led to
the diagnosis):

| query | gold rank |
|---|---|
| `какие виды мышей ты знаешь` *(as typed)* | 30 |
| `виды мышей` *(filler stripped)* | 3 |
| `виды мышей мышь` *(+ nominative — the morphology fix by hand)* | 2 → **1** after rerank |
| `виды мышей семейство мышиные Muridae` *(qwen's own rewrite)* | 8 → **25** after rerank |

Note the last row: the model's "helpful" query reformulation made retrieval **strictly worse**, and it
then fabricated a taxonomy to justify the result it got. Query rewriting by the weak model is not a
neutral act.

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
  qwen, via a thin local **shim** (`oracle-claude-shim.py`, :11435) that speaks the Anthropic
  Messages API to Claude Code and translates to Ollama's OpenAI endpoint — necessary because
  Ollama's *Anthropic streaming* endpoint mangles ~33% of qwen's tool calls under load (§8). The
  shim also **salvages** any tool call qwen leaks as text, taking the residual failure rate to ~0.
  MCP-wired to codebase-memory + `oracle-ask` (`ask_corpus`/`ask_code`) + `oracle-lsp` (the
  codebase-memory tool set is trimmed to the read/query tools to shrink a weak model's surface).
  An appended **discipline prompt** — precision over speed, never answer from weights — is the
  "prompt loop" that makes a weak model behave: it
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
| claude-shim | 11435 | Anthropic↔OpenAI translation + tool-call salvage for local Claude Code (not MCP; an API shim) |

### 7.1 Surviving a power loss (learned 2026-07-19, the involuntary reboot test)

The electricity failed mid-ingest — an unplanned run of the "does the stack come up from cold" test
(TODO G3.4). What the episode established:

- **What recovers by itself:** the docker stack (restart policies), Ollama, ES/MySQL/MinIO/Redis, the
  systemd user services — and, notably, **RAGFlow's parse queue: it lives in Redis, which persists**,
  so queued-but-unstarted page tasks resume on their own after boot.
- **What does NOT:** tasks **in flight** at the moment of death are popped from Redis and simply gone.
  A book parsed as N page-range tasks that loses one finishes its others and then sits `RUNNING` at
  partial progress forever — **silently missing a page range**, the "did less than it claimed and said
  nothing" class. Remedy: `requeue-orphans.py` — after a boot, every `RUNNING` doc is by definition
  orphaned; re-trigger it (RAGFlow answers `code=102` and dedupes against anything Redis already
  resumed). Cost, honestly: requeued docs restart from page 1, so partial progress is redone — the
  price of guaranteed completeness. Residual risk to verify per incident: crash-partial docs may end
  up with duplicated chunks if re-parse doesn't clear the partial set; check one when it completes,
  exact-content dedupe over the KB if needed.
- **Stateful toggles must be *enabled*, not just *started*.** qwen-next's unit was `enabled`, so the
  reboot resurrected it — 21.6 GB of VRAM held against a `backend.env` that pointed at the 30B,
  mid-ingest. `oracle-backend` now flips units with `systemctl enable --now`/`disable --now`, so the
  chosen state is what a reboot lands in. General rule: any A/B switch expressed as systemd units must
  encode the choice in *enablement*, or a power cycle silently re-decides it.
- **Progress metrics change meaning when the units change.** Post-crash the ingest "looked stalled" at
  +10 chunks/min — but chunks are now ~2,100 chars (the §5.1b parser fix), ~46× the old 47-char debris,
  and the requeue had just dumped ~3,900 page tasks back into the queue behind the heaviest,
  OCR-densest books (the fast text-layer PDFs finish first; the tail is the expensive half). Watch
  pages and task-queue depth, not chunks/min.

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
- **C/C++ LSP: drive clangd directly, not via multilspy.** multilspy (the Python LSP client)
  only wraps a fixed allow-list — `rust, go, python, java, typescript, …` but **no C/C++** — so
  the whole DB-internals corpus (all C/C++) had no LSP tier. But clangd is installed (it's what the
  user's Emacs/eglot drives, via mise), so `oracle-lsp` ships a ~120-line raw stdio LSP client
  (`ClangdClient`) for C/C++ and keeps multilspy for the languages it does support. hover/def/refs/
  symbols/code-actions work immediately per-file; `workspace/symbol` serves once clangd's background
  index warms. Caveat that still routes to grep: X-macro-generated members (`WAL_REC_*`, `PG_RMGR`)
  aren't LSP symbols, and the PG fork lacks `compile_commands.json`.
- **Embedding batch size is the throughput knob — 16 → 64 was ~8×.** Ingestion crawled; the
  parse backlog wouldn't drain. Measured: bge-m3@Ollama is *overhead*-bound, not GPU-bound (the
  card sits at ~0% during embed). At RAGFlow's default batch 16 it does ~12 chunks/s; at 64,
  ~100 chunks/s (plateaus there — 256 is no better). Parallel requests are *slower* (Ollama
  serializes GPU work: 52 chunks/s at ×4, 33 at ×8), so `OLLAMA_NUM_PARALLEL` is the wrong lever.
  Fix: `EMBEDDING_BATCH_SIZE=64` (`.env`) **and** patch `OllamaEmbed.encode` to honor it instead
  of re-splitting to 16 (both caps gated it). Per-batch embed time dropped from 300–550 s to ~2 s.
- **A shim after all — Ollama's Anthropic *streaming* endpoint mangles tool calls.** The original
  design ran Claude Code straight at Ollama's native Anthropic API, "no proxy." Measured under load
  (14 tools + big prompt, 6+ runs per cell): Anthropic **streaming** leaks qwen's tool call as raw
  `<function=...>` text **~33%** of the time; Anthropic non-streaming and OpenAI-streaming are both
  ~0%. Claude Code only speaks streaming-Anthropic, so it hits the broken path. And 0.31.2 is the
  latest Ollama — no upstream fix to wait for. Fix: a thin **shim** translating to Ollama's OpenAI
  endpoint (the robust path) with real streaming. Even the OpenAI endpoint leaks ~5% under load, so
  the shim adds a **salvage parser** that recovers qwen's leaked `<function=NAME><parameter=…>` XML
  (or a `<tool_call>{json}`) into a proper `tool_use` — net ~0% failures. Lesson: "no proxy" was an
  aesthetic, not a requirement; correctness beat it.

## 9. Lessons (transferable beyond this box)

### 9.0 The two axioms

Everything else in this section is a corollary of these. They are stated first because they are the
only parts that generalise past this machine.

**Axiom 1 — context occupation DEFOCUSES, and it is SHARED.**
Filling a context window with material degrades reasoning: the model attends through noise, anchors
on irrelevant hits, loses the thread. This applies to the 480B model and the local 30B one alike —
it is the *same* axiom, differing only by a **scale factor**. A large window means a higher
tolerance, not immunity.

*Corollaries:* a "wasteful" tool call is not merely slow — its output is pumped into the context and
competes for attention with what matters, so **minimising context occupation is a QUALITY measure,
not a speed one**. And bulk work (digesting many files, triage) should be *offloaded* to the local
model, not to save tokens — tokens are cheap — but to avoid the **compaction** that degrades the
strong model's reasoning for the rest of the session.

*Confirmed the hard way (2026-07-13):* context doesn't just distract, it **contaminates**. After
hours reading qwen's Russian-Chinese hybrid output, Claude code-switched into Russian
("литерально") in an English document — the identical failure it had spent the evening
documenting in qwen. Same law, different constant.

**Axiom 2 — the HARNESS must do its job (closed-loop); do not paper over it with prompt.**
The model's job is the *thinking* decision — "move the right hand." The harness's job is to
**actually move the right hand, reliably** — like a closed-circuit stepper that verifies its
position instead of silently losing steps. When the tooling misbehaves, **fix the tool**, do not add
another paragraph of prompt telling the model to compensate. *Piling up prompt workarounds for the
harness's own failures is the anti-pattern.*

*In practice:* the shim **salvages** qwen's leaked `<function=…>` calls instead of begging it to
format correctly (33% → 0%); `source_search` **redirects** ("no matches under X, but it occurs in
<other repo>") instead of a prompt rule "don't search the wrong repo"; `ask_code` returns a **RAW
SOURCE** block instead of "please read the numbers carefully". And note the routing defect we found
was *caused by prompt*: two hardcoded project names in the system prompt dragged every search toward
them. **Removing prompt bias beats adding prompt rules.**

*Shipped 2026-07-15 — the full closed-loop set, each a harness fix where the tool previously returned
an error it had the information to avoid:* `source_search`/`ask_code` now **accept the graph slug they
print themselves**; **auto-relax** a too-strict anchor to the bare identifier and report where it
occurs (our own "anchor the definition" advice missed `class _LIBCPP_TEMPLATE_VIS auto_ptr`);
`ask_code` **redirects** on a scoped miss instead of dead-ending; `source_search` emits **absolute
paths** so a `file:line` feeds straight to `Read`. And the two *prompt-debias* fixes: the hardcoded
project names are gone (call `list_projects`), and the DISCIPLINE no longer frames qwen as
coding-only (it refused biology as "out of scope" — the prompt over-narrowed the domain, same bug
class as the hardcoded names).

### 9.1 Corollaries

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
