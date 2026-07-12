# Oracle — overnight research + recommendations (2026-07-12)

Scouted what people run in comparable local-RAG / coding-agent setups and applied the safe,
high-value pieces. This file is the "for your review" list: things I did NOT apply autonomously
because they touch your Emacs config, VRAM, or the working retrieval path, plus the reasoning.

## Applied tonight (live, reversible)
- **Generalized everything off OrioleDB** → works on any repo under `~/Projects`.
- **source-grep MCP** now spans all of `~/Projects` + a `list_projects` tool.
- **emacs MCP (read-only)**: list/read buffers, current buffer, around-point. Wired into the
  `oracle` chat and `oracle-omni` agent. It CANNOT modify Emacs (fixed read-only elisp only).
- **git MCP (read-only)**: log/show/blame/diff/status over any repo. Added to `oracle-omni`
  only (not the chat — see tool-count note).
- Verified on non-Oriole repos: found+quoted `serenedb`'s `PgType` struct verbatim; read your
  live `*scratch*` buffer.

## Recommended, NOT applied (need your call)

### 1. Reranker — the single highest-ROI retrieval upgrade
**MEASURED on this box (CPU, 24 threads, rerank 30 chunks of ~400 tok):**
| model | params | 30ch | 50ch | Russian | notes |
|---|---|---|---|---|---|
| ms-marco-MiniLM-L6 | 22M | 0.15s | 0.9s | ENGLISH-ONLY — breaks on RU books | standard code |
| mmarco-mMiniLMv2-L12 | 118M | 0.9s | 0.9s | ✓ +9.5/-4.2 | standard code, no pin |
| jina-reranker-v2-base-multilingual | 278M | 1.5s | 2.3s | ✓ 0.76/0.16 | needs transformers<5 + trust_remote_code |
| gte-multilingual-reranker-base | 306M | 2.7s | 4.2s | ✓ 0.95/0.04 (best) | needs transformers<5 + trust_remote_code |
| bge-reranker-v2-m3 | 568M | 14.4s | 21.7s | ✓ best | TOO SLOW on CPU |
User's Postgres Pro books are RUSSIAN → reranker MUST be multilingual (English-only MiniLM
would mis-rank them). Budget = 2-3s. **Pick: gte-multilingual-reranker-base @ top-30 (2.7s, best
quality/RU) or jina-v2 @ top-30 (1.5s, more headroom).** Both need `transformers==4.48.3` pinned
in the service venv (v5 removed create_position_ids_from_input_ids / broke RoPE buffer init) —
robust when pinned + remote code cached offline. Benchmarks: scratch-rerank-*.py in this dir.

Original notes:
A cross-encoder reranker (`bge-reranker-v2-m3`) reorders the top-K chunks by true relevance
before they hit the model. Consensus across sources: biggest quality win in RAG. BUT:
- Ollama does NOT serve rerankers; RAGFlow's Ollama reranker registration errors out.
- TEI-gpu won't run on the RTX 5090 (compute cap 12.0 vs image's 8.0) — same wall we hit for
  embeddings. TEI-**cpu** rerank works but is slow.
- Cleanest offline path: run a tiny local reranker HTTP service (e.g. the pattern in
  github.com/s-kostyaev/reranker, or a 20-line FastAPI + `sentence-transformers` CrossEncoder
  on CPU), then register it in RAGFlow as an "OpenAI-API-Compatible"-style rerank endpoint.
- Risk: RAGFlow rerank-model registration is finicky (see infiniflow/ragflow issues #12399,
  #16115) and I didn't want to destabilize the working retrieval while you slept.
- **My recommendation:** worth doing, but as a supervised 30-min task — I can build the CPU
  reranker service and wire it in while you can watch retrieval quality before/after.

### 2. gptel + mcp.el — the SAME tools inside Emacs
gptel (already installed) supports MCP via `mcp.el`/`mcp-hub`. You could get code-graph +
ripgrep + git tools directly in an Emacs gptel buffer, no browser. Ready-to-review snippet is
in `emacs-gptel-mcp.el` (NOT added to init.el — you said don't change Emacs on the fly). It
needs `M-x package-install mcp` (online) and a config block. Caveat from the field: gptel's
agent/tool mode is still early and "falls apart on complex tasks" — fine for one-shot tool
calls (read a buffer, grep a repo), shaky for multi-step agentic loops. Review and apply when
you want it.

### 3. Tool-count discipline (a real finding)
Field consensus: past ~5–7 MCP servers / a few dozen tools, small models get WORSE at picking
the right tool. Current load:
- `oracle` chat: codebase-memory(8) + source-grep(3) + emacs(4) = 15 tools. Tested OK.
- `oracle-omni`: + git(5) + ragflow-kb(2) ≈ 23 tools. Tested OK on 3 probes, but this is near
  the edge. If you notice it fumbling tool choice, the fix is to split into task-focused agents
  (a "code" agent, a "workspace/git/emacs" agent) rather than one mega-agent.
- I deliberately kept git OFF the chat to hold the daily driver leaner.

### 4. Other tools people use that I skipped (and why)
- **Context7 / web-docs MCP**: fetches version-correct library docs — but it's ONLINE. Useless
  on the plane; your sanitized rustdoc/API KBs already cover this offline.
- **Playwright/browser MCP**: web automation — online, irrelevant to this task.
- **Filesystem MCP (Anthropic reference, read/WRITE)**: I intentionally did NOT add a
  write-capable filesystem tool — you want the model advising, not editing your tree unattended.
  source-grep + emacs give it read-visibility without write risk.

## Sources
- Best MCP servers 2026: builder.io/blog/best-mcp-servers-2026, firecrawl.dev/blog/best-mcp-servers-for-developers
- Reranking: localaimaster.com/blog/reranking-cross-encoders-guide, machinelearningmastery.com top-5-reranking-models
- Local reranker service: github.com/s-kostyaev/reranker
- gptel + MCP: github.com/karthink/gptel, gptel.org/manual.html, blog.kaorubb.org gpt-mcp-setup
- RAGFlow rerank issues: infiniflow/ragflow #12399, #16115, #7105
