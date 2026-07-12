#!/usr/bin/env bash
# Run Claude Code CLI against your LOCAL qwen (offline), via Ollama's native
# Anthropic Messages API (Ollama >=0.14; you have 0.31.2). No proxy needed.
#
# REVIEW BEFORE USE. This points `claude` at your local model instead of Anthropic.
# Weaker than real Claude Code — best for scoped edits/fixes (see C3L's pattern:
# corpus/tooling/C3L — deterministic loop + one small task at a time).
#
#   ./claude-local.sh            # start local Claude Code here
#   ./claude-local.sh --help
#
# The Oracle MCP servers (code graph, ripgrep, git, docs) can be added to this
# local Claude Code with `claude mcp add --transport sse <name> <url>`, e.g.:
#   claude mcp add --transport sse codebase-memory http://localhost:9750/sse
#   claude mcp add --transport sse source-grep     http://localhost:9751/sse
#   claude mcp add --transport sse ragflow-kb      http://localhost:9382/sse   # docs retrieval
# -> then offline qwen can navigate your source AND your corpus.

export ANTHROPIC_BASE_URL="http://localhost:11434"
# Use ONLY ANTHROPIC_AUTH_TOKEN for a custom endpoint; ANTHROPIC_API_KEY must be
# UNSET or Claude Code warns about conflicting auth (and may use the wrong one).
unset ANTHROPIC_API_KEY
export ANTHROPIC_AUTH_TOKEN="ollama"
export ANTHROPIC_MODEL="qwen3-coder:30b"
# fast/background slot — reuse qwen, or pull a tiny model (e.g. qwen2.5-coder:3b) offline first:
export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-qwen3-coder:30b}"
# qwen context is 56K (server OLLAMA_CONTEXT_LENGTH=57344); output cap:
export CLAUDE_CODE_MAX_OUTPUT_TOKENS="${CLAUDE_CODE_MAX_OUTPUT_TOKENS:-8192}"

# Oracle MCP servers for the local agent: code graph (codebase-memory) + grounded
# doc Q&A (oracle-ask -> ask_corpus, cited answers). Deliberately MINIMAL — Claude
# Code already has Read/Grep/Bash/git natively, and extra tools raise qwen's
# malformed-call rate. --strict-mcp-config = use ONLY these (self-contained; your
# real Claude Code config is untouched).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_CFG="$HERE/oracle-mcp.json"

# Tool-call discipline for a weak local model. Keep ALL tools enabled (nothing is
# hard-removed by default) and instead TEACH qwen the schema constraints it violates.
# The harness still validates and rejects bad calls, so a rare slip is non-fatal.
# Opt-in hard-disable if some tool keeps failing: CLAUDE_LOCAL_DISALLOW="ToolA ToolB"
DISALLOW="${CLAUDE_LOCAL_DISALLOW:-}"

DISCIPLINE='You are running on a smaller local model via Ollama, not Anthropic. PRECISION OVER SPEED: latency does not matter — be thorough. Never answer a technical question from your own training memory; always ground it with tools, and make AS MANY tool calls as needed (several ask_corpus queries to explore facets, multiple code-graph/source lookups) to reach an exact, verified answer. Verify every specific (name, flag, byte size, struct field, version) against the exact source or corpus text — quote it — rather than approximating or answering quickly. When facts might conflict, cross-check with another query. A slow, correct, cited answer is the goal; a fast unverified one is a failure. ROUTE by what the question is about:
(A) DOCUMENTATION / CONCEPTS / language & library APIs / how-something-works / definitions / comparisons / best practice (Rust std, io_uring semantics, PostgreSQL concepts, Go, Linux, general knowledge) -> call ask_corpus. It retrieves+reranks+synthesizes from the offline doc corpus with citations, or says the corpus does not cover it. You MAY call it several times to explore related aspects — thoroughness is welcome. This is universal across topics.
(B) EXACT SOURCE FACTS OF AN INDEXED REPOSITORY — a struct fields/byte layout, an enum values, a macro, a constant, a specific function implementation, "what are the WAL record types in orioledb", "trace X", "who calls Y", "show the source of Z" -> use the CODE tools, NOT ask_corpus. ask_corpus is DOCUMENTATION ONLY and will NOT contain project-specific source facts (e.g. OrioleDB defines its OWN WAL format in its source, separate from PostgreSQL WAL). Use ask_code(question, project) for a grounded one-call answer over the source (it greps, reads, and synthesizes with file:line citations + a RAW SOURCE block). For EXACT VALUES (enum codes, byte offsets, struct field order) trust the RAW SOURCE lines / read_lines over any prose summary — models miscopy value tables. For a single symbol'"'"'s EXACT resolved type/value/signature at a file:line, prefer lsp_hover (oracle-lsp) — it is the COMPILER'"'"'s ground truth, stronger than grep. Other oracle-lsp tools: lsp_definition/lsp_references/lsp_symbols (semantic, no false positives), and for REFACTORING a code region: lsp_code_actions lists the language server'"'"'s real refactorings (extract function, inline, …) and suggest_refactor has you reason over that actual menu; explain_code/propose_improvement for intent-level review. You can also drive the raw tools directly: search_graph/trace_path/get_code_snippet/query_graph/search_code (codebase-memory), source_search+read_lines (grep exact source; glob "*.h" for C headers, quote VERBATIM). Projects e.g. home-dead-Projects-orioledb-orioledb (extension), home-dead-Projects-orioledb-orioledb-postgres (PG fork).
When unsure whether it is a doc concept or a source fact, prefer the code tools for anything that concerns THIS codebase specifically, and ask_corpus for general/library knowledge. Only read a project file when a CODING task needs its current contents; read the SMALLEST relevant portion (grep first) and never read the same file twice. Obey every tool JSON schema EXACTLY: only documented fields, one call at a time, respect array-size limits (AskUserQuestion allows AT MOST 4 options). Prefer a sensible default over asking. Keep edits small and scoped; do not repeat a failed fix. If a tool call is rejected for a schema error, fix the args and retry once, then continue in plain text.'

DISALLOW_ARGS=()
[ -n "$DISALLOW" ] && DISALLOW_ARGS=(--disallowed-tools $DISALLOW)

echo "[claude-local] model=$ANTHROPIC_MODEL via $ANTHROPIC_BASE_URL (offline)"
echo "[claude-local] MCP: codebase-memory (code graph) + oracle-ask (ask_corpus/ask_code) + oracle-lsp (hover/refactor)"
echo "[claude-local] all tools enabled; schema-discipline prompt appended${DISALLOW:+ (disabled: $DISALLOW)}"
echo "[claude-local] expect weaker agentic behavior than real Claude Code; keep tasks scoped."
# Trim Claude Code's built-in system prompt (frees context; qwen window is 56K).
exec claude --mcp-config "$MCP_CFG" --strict-mcp-config \
  --exclude-dynamic-system-prompt-sections \
  "${DISALLOW_ARGS[@]}" \
  --append-system-prompt "$DISCIPLINE" "$@"
