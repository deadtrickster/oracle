# Oracle — offline local coding + sysadmin assistant (RAG) for the OrioleDB WAL/undo inspector

**Goal.** An offline, GPU-backed *reference brain* — you type the code, it answers questions
**grounded in real docs** (Rust · io_uring · Linux/devops) + your books/papers/bookmarks. Built for
writing an **`orioledb-waldump` / `pg_inspect`-for-Oriole** tool in Rust (io_uring I/O, reading
Oriole's on-disk WAL + undo format) while offline on a plane.

> ⚠️ **Do the whole setup while you still have internet** (pull models, clone docs, ingest).
> Offline it only knows what's already on disk. You have today + tomorrow — plenty.

---

## Design rules (from requirements)
- **VRAM is for the model, nothing else.** GPU (24 GB) runs *only* Ollama's LLM.
- **Embeddings + vector store + retrieval + extraction run on CPU/RAM.** RAG may eat RAM freely (125 GB).
- **Extraction is a one-time offline batch** — even if it briefly uses the GPU during prep, it never
  competes with inference.
- **Nice GUI, strong document extraction** (2-column scientific PDFs must parse cleanly).

## Architecture
```
   GPU 24GB ─ Ollama (LLM only: qwen3-coder:30b)  ◀── chat model ── RAGFlow (GUI + RAG)
                                                                     ├ DeepDoc extraction (CPU): 2-col PDFs, tables, OCR
   CPU/RAM  ─ RAGFlow embeddings (bge, CPU) + vector store (RAM/disk) ┘
        │
   Emacs (gptel) ── quick ask-in-editor ──▶ Ollama
   codebase-memory-mcp ──▶ OrioleDB/PG *code structure* (already indexed; not RAG'd)
```
- **RAGFlow** = the hub: nice web GUI, best-in-class parsing (DeepDoc), CPU embeddings, RAM vector store,
  ingests files + URLs. Its *chat model* points at your external Ollama, so the **GPU only ever serves the LLM**.
- **codebase-memory** stays as-is for Oriole/PG code (its graph beats RAG-chunking C). Query via Claude Code or `cli`.

## Hardware notes
- 24 GB VRAM fits `qwen3-coder:30b` (Q4, ~19 GB) with room for KV cache.
- **Keep everything on `/` (the Gen5 NVMe — nvme1 w/ heatsink), NOT `/mnt/data` (Gen4).** ~1.1 TB free.
- Context is the real budget: on 24 GB the model's 256K max is unreachable (KV cache) → **~16–32K
  tokens practical**. Enforced server-side by the Step 1 systemd override (`OLLAMA_CONTEXT_LENGTH`,
  `OLLAMA_KV_CACHE_TYPE=q8_0` + flash attention). Corpus size is free — RAG injects only top-K chunks.

---

## Step 1 — Ollama + model (GPU)  ·  online, ~20 GB
```bash
curl -fsSL https://ollama.com/install.sh | sh    # needs sudo (interactive password)
# Verified best coder for 24 GB (July 2026 — re-check ollama.com/library for newer):
ollama pull qwen3-coder:30b       # ~19 GB · MoE 30B/3.3B-active · 256K ctx · ~30+ tok/s — PRIMARY
ollama pull codestral             # ~13 GB · 22B · fill-in-middle · lighter/battery (optional)
ollama run qwen3-coder:30b "hi"   # smoke test (loads on GPU)
```
Do **NOT** pull an embedding model into Ollama — RAGFlow does embeddings on CPU (keeps VRAM free).

**Then configure the service (REQUIRED — Docker can't reach Ollama without it).** The systemd unit
binds `127.0.0.1` by default, which is invisible from inside RAGFlow's containers:
```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_HOST=0.0.0.0"            # reachable from Docker (laptop: mind untrusted networks/firewall)
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"      # halves KV cache → more context in 24 GB
Environment="OLLAMA_FLASH_ATTENTION=1"       # required for quantized KV cache
Environment="OLLAMA_CONTEXT_LENGTH=32768"    # server-side default ctx, don't rely on clients setting num_ctx
EOF
sudo systemctl daemon-reload && sudo systemctl restart ollama
curl -s http://localhost:11434/api/version   # sanity: server answers
```

## Step 2 — RAGFlow (the hub)  ·  Docker, CPU parsing + embeddings
```bash
git clone https://github.com/infiniflow/ragflow ~/Projects/oracle/ragflow
cd ~/Projects/oracle/ragflow/docker
# DOC-ENGINE DECISION — make it NOW, switching later means re-ingesting everything:
#   default = Elasticsearch (battle-tested, but ES+MySQL+Redis+MinIO idle hot → battery cost on the plane)
#   lighter = DOC_ENGINE=infinity in .env (less RAM/CPU at idle; newer, less proven)
# Staying with the DEFAULT (ES) — retrieval quality/stability over watts; stop the stack when not in use:
#   docker compose stop   /   docker compose start
docker compose -f docker-compose.yml up -d          # DEFAULT (CPU) compose: DeepDoc + embeddings on
                                                     # CPU → zero VRAM. Do NOT use docker-compose-gpu.yml.
# open the web UI (default http://localhost — check the compose for the port), create admin account
```
Then in RAGFlow settings:
- **Model providers → add Ollama** → Base URL `http://host.docker.internal:11434` → set **chat model =
  `qwen3-coder:30b`**. (This is the only GPU user.)
  - `host.docker.internal` is NOT automatic on Linux — it works only if the ragflow service has
    `extra_hosts: host.docker.internal:host-gateway` (recent composes ship it; **verify**, add if missing).
    Fallback base URL: `http://172.17.0.1:11434` (docker0 bridge). Either way Ollama must listen on
    `0.0.0.0` — that's the Step 1 override.
- **Embedding model → `bge-m3@Ollama`** (set as tenant default; datasets inherit it).
  *Why not CPU-side in RAGFlow:* v0.26.4's "Builtin" embedding is a TEI sidecar container, and
  (a) tei-cpu ate 23 GB RAM + 21 cores and needed hours for this corpus, (b) the tei-gpu image is
  compiled for compute cap 8.0 and refuses the RTX 5090 (cap 12.0). Ollama's bge-m3 produces the
  same vectors, runs the one-time bulk embed on GPU fast, and at query time embeds a single short
  string — its 1.3 GB sits beside qwen's 20 GB inside 24 GB, so the GPU-for-LLM rule holds in
  spirit: nothing competes with inference.
- Leave DeepDoc parsing on; pick a **chunk method per knowledge base** (see Step 4).

> ⚠️ **Offline trap:** RAGFlow downloads its embedding + DeepDoc (layout/OCR) model weights from
> HuggingFace **on first use**, not at `compose up`. While still online you MUST fully parse **and**
> embed at least one document of every type you'll use (incl. one PDF in a "Paper"-method KB) —
> completing all of Step 4 online covers this. If you skip it, ingestion dies mid-flight.

## Step 3 — Assemble the doc corpus  ·  online
```bash
cd ~/Projects/oracle && bash fetch-corpus.sh
```
Gathers into `corpus/`:
- **Rust:** nomicon, book, reference, rust-by-example (markdown). (std API HTML already offline.)
- **io_uring:** liburing (2.14, installed) man pages→text, "Lord of the io_uring", Axboe's *Efficient
  IO* paper, **`io_uring.h`@v7.0** (the authoritative op/flag allowlist for the target kernel).
- **io_uring Rust:** `io-uring` + `tokio-uring` source + offline rustdoc.
- **Rust (max) additions:** **async-book**, plus the **full std/core/alloc API + tooling books** —
  but ⚠️ **NOT ingested as raw HTML**: the 785 MB rustup HTML is ~90 % boilerplate markup that would
  bloat ingestion and poison retrieval. It gets **sanitized to markdown first** (Step 3c); the
  `rustup-html` symlink from `fetch-corpus.sh` is the sanitizer's *input*, never uploaded itself.
  **rust-src** (std source, markdown doc-comments = the ground truth) stays as fallback/deep-dive.
  ▶ For full crate coverage, run `cargo doc` in the waldump project → `target/doc`, then Step 3c-sanitize
  that too before ingesting.
- **Emacs:** Emacs manual + **Elisp Reference** + Elisp Intro + misc (org/calc/tramp…), rendered from
  your self-built texinfo → plaintext.
- **PostgreSQL (PG 17.9):** your fork's **94 source READMEs** (WAL=`access/transam`, page layout,
  buffers, MVCC) + OrioleDB's own docs. Books (PG17 manual · Rogov · Suzuki[HTML] · Postgres Pro others)
  are **added by hand** — see `corpus/postgres/BOOKS-TO-ADD.md`.
- **Linux / devops (the big sysadmin haul):**
  - **All your system man pages → text** (sections 1/2/**3**/5/7/8 — incl. *all* of section 3:
    C library, pthreads, etc.): `cgroups(7)`, `proc(5)`/`proc_sys_vm(5)`, `sysctl(8)`,
    `namespaces(7)`, and **systemd's own**: `systemd.resource-control(5)` (MemoryMax / cgroup RSS
    limits), `systemd.unit(5)`, `systemd.service(5)`, `systemd.exec(5)`…
  - **Kernel admin docs, pinned to v7.0:** `sysctl/vm.rst` (**dirty-page knobs** — dirty_ratio,
    dirty_background_ratio, dirty_expire_centisecs…), `cgroup-v2.rst` (memory/RSS controllers),
    kernel/fs/net sysctls.

> 🎯 **io_uring + kernel API target kernel 7.0**, not the running 6.17 (io_uring is version-specific;
> 7.0 is your bootable 7.x, and it's additive so 7.0-targeted code also runs on 7.1). The corpus pins
> kernel docs + `io_uring.h` to the `v7.0` tag.
  - (Optional) drop an **Arch Wiki** markdown mirror into `corpus/linux/archwiki/`.

## Step 3b — Your personal collection  ·  RAGFlow parses it directly
RAGFlow's DeepDoc handles the hard formats, so mostly **just upload** (Step 4). Two helpers:
- **Bookmarks / links** → RAGFlow ingests files best; pre-fetch URLs to clean markdown:
  ```bash
  uv tool install trafilatura
  grep -oiP 'href="\Khttps?://[^"]+' corpus/links/bookmarks.html | sort -u > corpus/links/urls.txt
  trafilatura --input-file corpus/links/urls.txt --output-dir corpus/links --markdown
  ```
  (or `prep-collection.sh`). Then upload `corpus/links/*.md`.
- **Two-column scientific PDFs** → upload to a KB whose **chunk method = "Paper"**. DeepDoc's layout
  model then **auto-detects the columns** and reads them in order — automatic *once you pick "Paper"
  per-KB* (it doesn't guess doc type for you). **Spot-check one parsed paper** in the UI; for any that
  come out garbled (dense math / scanned), run **MinerU** on just those (`uv tool install mineru`) and
  upload the markdown. Runs on **CPU** (default compose) → zero VRAM.
- **Books** (PDF/EPUB) → upload directly (EPUB: `pandoc x.epub -o x.md` first if needed).

## Step 3c — API-doc HTML → clean markdown (`sanitize-apidocs.py`, to be written)
Rustdoc/mdBook HTML has a clean extraction anchor, so sanitizing is reliable — and where upstream
markdown exists (the prose books) we already clone it in Step 3; this step covers what has **no**
usable markdown distribution (rendered std API, tooling books, crate rustdoc).
- **rustdoc pages** (std/core/alloc/proc_macro + any `cargo doc` output): extract
  `<section id="main-content">`, convert to markdown, and **merge item pages per module** →
  `corpus/rust/api-md/std/<module>.md` etc. One file per module ≈ hundreds of files instead of tens
  of thousands, with great chunk locality. **Skip:** `src/` source-view pages, redirect stubs,
  `search-index*`, settings/help/all.html, `.js`/static assets.
- **mdBook chapters** (cargo, clippy, rustc, rustdoc-book, edition-guide, embedded-book,
  unstable-book, error index): extract `<main>`, concat chapters in order → one
  `corpus/rust/api-md/books/<book>.md` each.
- Same script over `corpus/io_uring_rust/api/` → `corpus/io_uring_rust/api-md/`.
- Implementation: Python + beautifulsoup4 + markdownify, multiprocessing (24 cores), run via
  `uv run --with beautifulsoup4,markdownify` — one-time batch, CPU only.
- Spot-check a few module files; any that come out garbled → ingest that module's `rust-src`
  source (doc comments are markdown) instead.
- Same recipe extends to other languages' HTML API docs later (anything with a stable content
  container: doxygen, sphinx `<div role="main">`, …).

## Step 4 — Create RAGFlow knowledge bases + ingest
Make one KB per topic and pick the matching **chunk method** (RAGFlow templates):
| Knowledge base | Sources | Chunk method |
|---|---|---|
| `rust` | corpus/rust prose (book/nomicon/reference/by-example/async-book), io_uring_rust sources | General / Book |
| `rust-api` | corpus/rust/api-md (Step 3c: std/core/alloc per-module + tooling books), io_uring_rust/api-md | General |
| `io_uring` | corpus/io_uring, man-txt | General |
| `linux` | corpus/linux/man-merged (10,362 pages pre-merged into 151 files), kernel-docs | General |
| `emacs` | corpus/emacs (manual, elisp ref/intro, 63 misc) | General |
| `postgres` | corpus/postgres/readmes + OrioleDB docs | General |
| `papers` | your scientific PDFs | **Paper** |
| `books` | corpus/books | **Book** |
| `links` | corpus/links/*.md | General |
(Never upload `corpus/rust/rustup-html` — raw HTML is sanitizer input only, see Step 3c.)

**Bulk upload — use `ingest-corpus.py` (written, re-runnable):** after registering in the UI, adding
the Ollama provider, setting the default embedding model, and creating an API key:
```bash
uv run ingest-corpus.py --api-key <KEY> --wait   # creates all KBs, uploads, parses, waits
```
It skips already-uploaded files, so re-run it after dropping papers/books/links into corpus/.
(Man pages were pre-concatenated to corpus/linux/man-merged/ — 151 files instead of 10k.)

Let it parse + embed (CPU — **all of it must finish while online** — see the Step 2 offline-trap
note). Then in chat, attach the relevant KB(s) so answers are grounded + cited. Retrieval is
RAM-resident → fast; keep Top-K ~5–8.

## Step 5 — Emacs integration (gptel → Ollama)
```elisp
(use-package gptel :ensure t
  :config
  (setq gptel-model 'qwen3-coder:30b
        gptel-backend (gptel-make-ollama "Ollama"
                        :host "localhost:11434" :stream t
                        :models '(qwen3-coder:30b codestral))))
;; M-x gptel (chat) or select code + M-x gptel-send.  No RAG here — use RAGFlow when you need docs.
```
Goes in `~/.emacs.d/init.el` (the live config on this machine; no gptel block there yet).

## Step 6 — codebase-memory for Oriole internals (already indexed)
For "where's the WAL record written / show the undo struct / trace the checkpoint path":
```bash
codebase-memory-mcp cli search_graph      '{"project":"home-dead-Projects-orioledb-orioledb-postgres","name_pattern":"*wal*"}'
codebase-memory-mcp cli get_code_snippet  '{"project":"...","qualified_name":"<struct|fn>"}'
codebase-memory-mcp cli trace_path        '{"project":"...","function_name":"<writer>","mode":"calls"}'
```
(Or just ask Claude Code — it already has these tools.) Authoritative source for Oriole's on-disk format.

---

## Daily workflow (the task)
1. **Understand the format** → codebase-memory (Oriole/PG structs, byte layouts, write paths).
2. **Write the Rust reader** → RAGFlow chat grounded in `#rust` + `#io_uring` ("SQE to read N bytes at
   aligned offset", "safely cast &[u8] to #[repr(C)] struct", nomicon aliasing rules).
3. **Systems/ops questions** → `#linux` ("O_DIRECT alignment reqs", "dirty_ratio vs
   dirty_background_ratio", "cap RSS with systemd MemoryMax/cgroup v2").
4. **Quick edits while typing** → emacs gptel.

## Offline verification (before you fly)
```bash
# simulate the plane: disable networking, then:
ollama run qwen3-coder:30b "hi"              # LLM offline
# RAGFlow: ask with a KB attached → retrieves + cites, GPU shows only the LLM (nvidia-smi)
# RAGFlow: also upload + parse one NEW small PDF while offline → proves DeepDoc/embedding
#          weights are cached locally, not re-downloaded from HuggingFace (the Step 2 trap)
codebase-memory-mcp cli list_projects '{}'   # code graph offline
```

## Notes
- Everything is local/offline once downloaded. Only Steps 1–3 need internet.
- **VRAM check:** during a RAGFlow query, `nvidia-smi` should show *only* the Ollama model resident —
  no embedder, no parser. If something else is on the GPU, switch that component to CPU.
- codebase-memory re-index of giants can hit its 16 GB wrapper cap (recovers); bump
  `~/.local/bin/codebase-memory-mcp-capped` MemoryMax if you want them to finish.
- Model pick verified July 2026 — glance at ollama.com/library for anything newer that fits 24 GB.
- Kernel `v7.0` tag **verified to exist** on torvalds/linux (checked 2026-07-11) — the pinned fetches are valid.
- Machine state at plan time (2026-07-11): Docker ✓ (user in docker group), uv ✓, rustc + rustup HTML
  docs + rust-src ✓, makeinfo ✓, pdftotext ✓; **missing:** ollama, pandoc, mandoc, trafilatura, ragflow
  clone; corpus/ dirs exist but are empty; GPU idle. Ollama install + systemd override need interactive
  sudo — the user runs those; everything else is sudo-free.
