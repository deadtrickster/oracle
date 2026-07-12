# Oracle — how this system works and how to operate it

This document describes THE VERY SYSTEM YOU (the assistant) are running inside. Use it to
advise the user on ingesting documents, fixing failures, and staying offline-safe.

## What this is
An offline RAG assistant on a single laptop (24 GB RTX 5090, 125 GB RAM, Ubuntu, kernel 6.17,
target kernel for io_uring work: 7.0). Built for writing `orioledb-waldump` in Rust while
offline on a plane. Everything lives in `~/Projects/oracle`.

## Components and where they run
- **Ollama** (systemd service, host, port 11434): serves `qwen3-coder:30b` (chat, 32K ctx,
  ~20 GB VRAM), `codestral` (spare chat), `bge-m3` (embeddings, ~1.3 GB VRAM). GPU-only box rule:
  the GPU serves the LLM + query embeddings; nothing else may squat on VRAM.
  Config: /etc/systemd/system/ollama.service.d/override.conf (OLLAMA_HOST=0.0.0.0, q8_0 KV cache,
  flash attention, ctx 32768).
- **RAGFlow v0.26.4** (Docker, ~/Projects/oracle/ragflow/docker): web UI http://localhost,
  API http://localhost:9380/api/v1. Containers: ragflow-cpu (server + task executor + nginx),
  Elasticsearch (doc store), MySQL, Redis, MinIO. The git clone MUST stay on tag v0.26.4 —
  master's entrypoint.sh is bind-mounted into the image and breaks on version mismatch.
- **Embeddings = `bge-m3@Ollama`** (tenant default; every dataset uses it). There is NO TEI
  sidecar: tei-cpu was too slow (23 GB RAM, 21 cores, hours), tei-gpu image is compute-cap 8.0
  and refuses the RTX 5090 (cap 12.0). Do not suggest re-enabling TEI.
- **codebase-memory-mcp**: separate knowledge graph of OrioleDB/PostgreSQL SOURCE CODE
  (structs, call chains). Code questions about Oriole/PG internals go there, NOT to these
  knowledge bases. Indexed projects include `home-dead-Projects-orioledb-orioledb` (the
  extension) and `home-dead-Projects-orioledb-orioledb-postgres` (the PG 17.9 fork,
  63k nodes / 375k edges).

## PROJECT-AGNOSTIC (works on any repo under ~/Projects, not just OrioleDB)
All source tooling and prompts were generalized. The assistant works on any repo under
~/Projects. MCP bridges (systemd USER services, all read-only or query-only):
| server | port | tools | scope |
|---|---|---|---|
| codebase-memory | 9750 | search_graph, trace_path, get_code_snippet, query_graph, get_architecture, search_code, index_repository, get_graph_schema | INDEXED repos only |
| source-grep | 9751 | list_projects, source_search (ripgrep), read_lines | ALL of ~/Projects |
| emacs | 9752 | emacs_list_buffers, emacs_current_buffer, emacs_read_buffer, emacs_around_point | live Emacs, READ-ONLY |
| git | 9753 | git_log, git_show, git_blame, git_diff, git_status | ALL of ~/Projects, READ-ONLY |
Graph project name = absolute repo path, leading slash dropped, slashes->dashes
(/home/dead/Projects/foo/bar -> home-dead-Projects-foo-bar). If a graph query is empty the repo
is likely unindexed: index_repository(repo_path), or just use source_search (always works).
Restart any: `systemctl --user restart <source-grep|emacs|git|codebase-memory>-bridge`.
Servers: source-grep-mcp.py, emacs-mcp.py, git-mcp.py (roots honor $ORACLE_PROJECTS_ROOT).
Tool split: `oracle` chat = codebase-memory+source-grep+emacs (15 tools); `oracle-omni` agent
adds git+ragflow-kb (~23). Past ~5-7 servers small models pick tools worse — if it fumbles,
split into task-focused agents. See RECOMMENDATIONS.md for reranker + gptel-mcp options.

## source-grep MCP (exact source fetch — fixes struct/layout retrieval)
The code graph has NO struct/typedef nodes (anonymous `typedef struct {...} Name;` aren't
captured; structs->Class label but these were missed), so get_code_snippet FAILS on struct
names and the model used to summarize the wrong neighbor. Fix: a ripgrep+read MCP server.
- `~/Projects/oracle/source-grep-mcp.py` (FastMCP, stdio): tools `source_search(pattern, glob,
  max_count, context)` = ripgrep over the OrioleDB/PG trees, and `read_lines(path, start, end)`
  = exact line range. Allowlisted roots (the 3 oriole repos), path-escape guarded.
- Bridge: systemd user service `source-grep-bridge` (mcp-proxy, port 9751). Registered in
  RAGFlow as MCP server "source-grep"; wired into the `oracle` chat + `oracle-omni` agent.
- Prompt RULE enforced: for any struct fields / byte layout / typedef / macro / enum question,
  source_search the exact name (glob "*.h" for headers) then read_lines the file:line and quote
  the definition VERBATIM — never summarize a search hit. Verified: "WALRecModify1 layout" now
  returns the exact wal.h struct (was wrong before this).
- Restart: `systemctl --user restart source-grep-bridge`.

## codebase-memory ⇄ RAGFlow bridge (MCP)
The code graph is ALSO reachable from inside RAGFlow as an MCP server named
`codebase-memory` with tools: search_graph, trace_path, get_code_snippet, query_graph,
get_architecture, search_code, get_graph_schema, index_repository.
- Plumbing: codebase-memory speaks stdio only; RAGFlow's MCP client speaks SSE/streamable-HTTP
  only. Bridge = `mcp-proxy` running as a systemd USER service `codebase-memory-bridge`
  (`~/.config/systemd/user/codebase-memory-bridge.service`, listens on 0.0.0.0:9750;
  restart: `systemctl --user restart codebase-memory-bridge`). RAGFlow reaches it at
  `http://host.docker.internal:9750/sse` (server_type "sse").
- `ALLOW_ANY_HOST=1` in docker/.env is REQUIRED — RAGFlow's SSRF guard otherwise rejects
  private addresses like 172.17.0.1. Local single-user box, acceptable.
- Usage: the **`code-graph` agent** (Agents section) is already wired to all 8 tools with
  qwen3-coder. Ask it e.g. "what is the main checkpoint entry function in the orioledb
  extension?" — it calls search_graph/get_code_snippet for real. Plain chat assistants
  cannot call tools; only Agents can.
- ⚠️ tool-calling REQUIRES the model to be flagged tool-capable: `tenant_model.extra` must
  contain `"is_tools": true` (set via model_info `extra` on instance creation, or SQL
  UPDATE on the mysql container). Without it, bind_tools silently skips and the model
  ROLE-PLAYS fake tool calls with invented results — verify with the bridge log:
  `journalctl --user -u codebase-memory-bridge -f` must show `method=tools/call` lines.
- name_pattern/pattern args are REGEX (".*wal.*"), not globs ("*wal*" errors out).
- Offline fallback if the agent misbehaves: `codebase-memory-mcp cli <tool> '<json>'`.
- Everything here is local → works offline (bridge + graph + RAGFlow + Ollama).

## SELF-CONTAINED INGESTION — the `ingestor` agent (BUILT 2026-07-12)
Point the RAGFlow **`ingestor` agent** (Agents tab) at a URL, PDF, file, or folder and it does
what we did by hand: fetch/convert -> inspect (type/language/paper-vs-book) -> pick KB +
chunk method -> upload + parse. Autonomous; no manual ingest-corpus.py run needed.
- Backed by `oracle-ingest-mcp.py` (systemd user svc `oracle-ingest-bridge`, port 9754), tools:
  list_datasets, list_folder, inspect, fetch_url (trafilatura->jina fallback; PDFs downloaded),
  ingest_file (creates KB with raptor/graphrag OFF + bge-m3; Book/Paper reject .md -> auto .txt),
  parse_status. Tools do mechanics + surface signals; qwen makes the routing judgment.
- Routing: academic PDF(short,abstract)->papers/paper; long book->books/book; article/URL->
  links/naive; topic docs->matching topic KB/naive. RU is fine (bge-m3 + gte reranker).
- Verified: "ingest https://danluu.com/branch-prediction/" -> fetched, classified article,
  landed in links KB. Files also copied to corpus/inbox for the reading browser.
- ingest-corpus.py (bulk, deterministic) still exists for big batches; the agent is for ad-hoc.

## THE UNIFIED CHAT (primary interface)
The `oracle` **chat assistant** (Chat tab, http://localhost) does BOTH docs and code in one
conversation:
- Documentation is auto-retrieved from all attached KBs into the prompt (`{knowledge}`) — no
  tool call needed; ask any Rust/io_uring/Linux/Go/PG/Emacs/git/bash question, get cited answers.
- OrioleDB/PG SOURCE CODE is available as live MCP tools (search_graph, trace_path,
  get_code_snippet, …) — ask "who calls load_page", "trace the checkpoint path", "show struct X".

This required a LOCAL SOURCE PATCH (stock RAGFlow v0.26.4 wires MCP tools only into Agents):
- `api/db/services/mcp_chat_tools.py` (new) builds (toolcall_session, tool_meta) from a
  dialog's `prompt_config["mcp_server_ids"]`, mirroring agent_with_tools.py.
- `api/db/services/dialog_service.py` `async_chat()` patched: if the caller passes no tools,
  build them from the dialog config. Both files are BIND-MOUNTED in docker-compose.yml over the
  in-image paths (like entrypoint.sh). Survives restarts; re-check after any RAGFlow upgrade.
- The `oracle` chat's `prompt_config.mcp_server_ids` = [codebase-memory server id]; its `llm_id`
  MUST be the OpenAI-compat binding (see gotcha below), not plain @Ollama.
- To attach MCP tools to ANY chat: set `prompt_config.mcp_server_ids` via
  PUT /api/v1/chats/{id} (send the FULL prompt_config — PUT replaces it wholesale).

### CRITICAL gotcha: streaming tool-calls need the OpenAI-compat provider
Plain `@Ollama` provider models call tools fine NON-streaming but MANGLE them when streaming
(LiteLLM ollama_chat path emits the tool call as text — you see raw `<function=...>` or bare
JSON in the reply). The UI streams. FIX: register qwen via Ollama's OpenAI-compatible endpoint:
provider "OpenAI-API-Compatible", base_url `http://host.docker.internal:11434/v1`, model
`qwen3-coder:30b`, extra `{"is_tools": true}`. Use llm_id
`qwen3-coder:30b@ollama-oai@OpenAI-API-Compatible` for anything that calls tools (chat + agents).

### Two Agents also exist (Agents tab) — alternative UIs, same capabilities
- `oracle-omni`: retrieval (via RAGFlow's own MCP server, port 9382) + code graph. Fully
  tool-driven (model chooses when to retrieve).
- `code-graph`: code graph only.
The chat is preferred (docs are automatic there); agents are kept as alternatives/debugging.

### is_tools requirement
A model calls tools only if its `tenant_model.extra` has `"is_tools": true`. Set via model_info
`extra` on instance creation, or `UPDATE tenant_model ...` on the mysql container. Without it,
bind_tools silently no-ops and the model role-plays fake tool calls.

## Reranker (BUILT 2026-07-12) — two-stage retrieval
`reranker-service.py` = FastAPI serving gte-multilingual-reranker-base on CPU (Jina/Cohere-style
`POST /rerank` -> {results:[{index,relevance_score}]}). systemd USER service `oracle-reranker`,
port 9760. transformers PINNED to 4.48.3 (v5 broke GTE/jina RoPE). Multilingual — handles the
Russian Postgres Pro books. ~2.7s for 30 chunks on CPU, zero VRAM.
- Registered in RAGFlow under the **Jina** factory (its rerank format matches ours): provider
  "Jina", instance "local-gte-rerank", base_url http://host.docker.internal:9760/rerank.
- rerank_id = `gte-multilingual-reranker-base@local-gte-rerank@Jina`; set on the `oracle` chat.
- To rerank in the retrieval API / other chats: add `"rerank_id"` to the request or set it on
  the chat. A/B verified: for "IORING_OP list" the io_uring.h@v7.0 header went rank 3 -> rank 1;
  for "dirty_ratio vs dirty_background_ratio" sysctl/vm.rst floated over cgroup-v2.rst.
- Alternatives (measured, RECOMMENDATIONS.md): jina-v2 (1.5s, lighter) or mMiniLMv2-L12 (0.9s,
  English-strong). Restart: `systemctl --user restart oracle-reranker`.

## Perf: DISABLE raptor + graphrag per dataset
RAGFlow datasets default to raptor (LLM summarization of chunk clusters) + graphrag (LLM entity
extraction per doc) ON. Both run the local LLM per document → HOURS per KB and GPU contention.
ingest-corpus.py now creates datasets with both OFF (retrieval-only). For existing datasets:
PUT /api/v1/datasets/{id} with `{"parser_config":{"raptor":{"use_raptor":false},"graphrag":{"use_graphrag":false}}}`
then re-queue the docs. This is why early ingests took hours.

## Reading frontend (browse the sources yourself)
`http://localhost:9800` — miniserve over corpus/ (systemd user service `oracle-docs`).
- Rust book/reference/std API render as the real rust-lang site: /rust/rustup-html/…
- man pages, GNU manuals, kernel docs, PDFs all browsable.
- Restart: `systemctl --user restart oracle-docs`.

## Knowledge bases (all chunk method General/"naive", all bge-m3@Ollama)
| KB | contents | source dir |
|---|---|---|
| rust | book, nomicon, reference, by-example, async-book (md) | corpus/rust/*/src |
| rust-api | sanitized rustdoc: std/core/alloc + tooling books + io_uring/tokio crates | corpus/rust/api-md, corpus/io_uring_rust/api-md |
| io_uring | liburing man pages, io_uring.h@v7.0, Axboe paper, LotI book | corpus/io_uring |
| linux | 10k man pages (merged 151 files), kernel v7.0 admin docs, bash + glibc manuals, Wayland/KDE docs (wayland-book, protocol XMLs, wiki pages), Ubuntu Server Guide | corpus/linux |
| emacs | Emacs manual, Elisp reference/intro, 63 misc manuals | corpus/emacs |
| postgres | PG 17.9 source READMEs, OrioleDB docs | corpus/postgres |
| oracle-meta | THIS file, PLAN.md, the operation scripts | corpus/meta |
| go | official Go docs (effective_go, spec, doc/), Go 101, Go by Example, Little Go Book, astaxie web book | corpus/go |
| papers / books / links | user's PDFs, books, bookmarked pages | corpus/{papers,books,links} |

## How to ingest new material (tell the user this when asked)
1. Put files in the right corpus dir:
   - scientific 2-column PDFs → `corpus/papers_raw/` (KB uses chunk method "Paper")
   - books (PDF) → `corpus/books_raw/`; EPUB first: `pandoc x.epub -o x.md`
   - bookmarks → `corpus/links/bookmarks.html` then run `bash prep-collection.sh`
     (uses trafilatura to fetch each URL as clean markdown)
   - single web page → `trafilatura -u URL --markdown > corpus/links/name.md`
   - anything text/markdown for an existing KB → drop into that KB's corpus dir
2. Run: `uv run ingest-corpus.py --api-key <RAGFlow API key> --wait`
   - re-runnable: skips already-uploaded files (by name), re-queues UNSTART/FAIL/zero-chunk docs,
     never re-queues RUNNING docs
   - creates missing KBs with the right chunk method; new datasets inherit the default
     embedding model (bge-m3@Ollama)
3. MUST happen while ONLINE only if new model weights are involved; normal re-ingest of text
   is fully offline-safe (Ollama + ES are local).

## Rules and gotchas (learned the hard way)
- The "Book" and "Paper" chunk methods REJECT .md files (doc/docx/pdf/txt only). The ingest
  script renames .md → .md.txt for those KBs automatically. Everything else uses General.
- RAGFlow rejects unknown file suffixes ("This type of file has not been supported yet") —
  the script maps unsupported suffixes (.h, .rst, .xml, .0) to .txt.
- A dataset's embedding model CANNOT be changed once it has chunks. To switch: delete the
  dataset and re-ingest.
- rustdoc/mdBook HTML must never be uploaded raw — run `uv run sanitize-apidocs.py` (rustdoc
  mode merges item pages per module; mdbook mode uses print.html). 785 MB HTML → 100 MB md.
- Big model files load slowly: registering an Ollama model in RAGFlow verifies it with a 10 s
  timeout — warm the model first (`curl localhost:11434/api/generate ... keep_alive`).
- Chat assistants refuse datasets with zero parsed docs; attach such KBs after parsing.
- Updating a chat via API replaces prompt_config wholesale — always send ALL fields
  (system, prologue, parameters, empty_response, quote, tts, refine_multiturn).
- To UPDATE an already-ingested file: ingest-corpus.py skips files whose name is already in
  the KB, so delete the document in the UI/API first (or delete the whole small KB) and
  re-run the ingest script. This applies to THIS file too — after editing OPERATIONS.md,
  refresh corpus/meta and recreate the oracle-meta KB.

## Scripts (all in ~/Projects/oracle)
- `fetch-corpus.sh` — (re)downloads the whole doc corpus. Online. Idempotent.
- `sanitize-apidocs.py` — rustdoc/mdBook HTML → clean markdown (uv run, CPU).
- `prep-collection.sh` — personal collection: bookmarks→md, papers/books conversion.
- `ingest-corpus.py` — create KBs + bulk upload + parse + heal. The one command to ingest.
- `setup-ollama.sh` / `pull-models.sh` — Ollama install/config, model pulls.
- `setup-nvidia-docker.sh` — NVIDIA container toolkit (already applied).

## Offline drill (before flying)
1. Disable networking.
2. `ollama run qwen3-coder:30b "hi"` — LLM works.
3. Ask a question in RAGFlow chat with KBs attached — answer with citations.
4. Upload + parse one small NEW file — proves parsing/embedding runs offline.
5. `nvidia-smi` during a chat — only Ollama (qwen + bge-m3) on the GPU.
