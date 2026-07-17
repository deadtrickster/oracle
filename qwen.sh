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

# Point at the Oracle shim (:11435), NOT Ollama directly (:11434). The shim translates
# Anthropic<->OpenAI and salvages qwen's leaked tool calls — Ollama's Anthropic streaming
# endpoint mangles ~33% of tool calls under load; via the shim it's ~0. See oracle-claude-shim.py.
export ANTHROPIC_BASE_URL="${ORACLE_SHIM_URL:-http://localhost:11435}"
# Use ONLY ANTHROPIC_AUTH_TOKEN for a custom endpoint; ANTHROPIC_API_KEY must be
# UNSET or Claude Code warns about conflicting auth (and may use the wrong one).
unset ANTHROPIC_API_KEY
export ANTHROPIC_AUTH_TOKEN="ollama"
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-qwen3-coder:30b}"
# fast/background slot — reuse qwen, or pull a tiny model (e.g. qwen2.5-coder:3b) offline first:
export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-qwen3-coder:30b}"
# qwen context is 56K (server OLLAMA_CONTEXT_LENGTH=57344); output cap:
export CLAUDE_CODE_MAX_OUTPUT_TOKENS="${CLAUDE_CODE_MAX_OUTPUT_TOKENS:-8192}"

# SEPARATE session/config storage. Claude Code keeps its config, settings, memory and
# session transcripts (projects/<encoded-cwd>/<uuid>.jsonl) under one config dir. Point the
# LOCAL (qwen) Claude at its own, so its history/memory never mixes with the real Claude
# Code's ~/.claude — `claude -c`/`-r` in here resumes only local sessions.
# Share them again by exporting CLAUDE_CONFIG_DIR=$HOME/.claude before running this.
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude-local}"
mkdir -p "$CLAUDE_CONFIG_DIR"

# Oracle MCP servers for the local agent: code graph (codebase-memory) + grounded
# doc Q&A (oracle-ask -> ask_corpus, cited answers). Deliberately MINIMAL — Claude
# Code already has Read/Grep/Bash/git natively, and extra tools raise qwen's
# malformed-call rate. --strict-mcp-config = use ONLY these (self-contained; your
# real Claude Code config is untouched).
# readlink -f so this still finds oracle-mcp.json when invoked through a symlink (~/bin/qwen)
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
MCP_CFG="$HERE/oracle-mcp.json"

# Tool-call discipline for a weak local model. Keep ALL tools enabled (nothing is
# hard-removed by default) and instead TEACH qwen the schema constraints it violates.
# The harness still validates and rejects bad calls, so a rare slip is non-fatal.
# Opt-in hard-disable if some tool keeps failing: CLAUDE_LOCAL_DISALLOW="ToolA ToolB"
# Default trim: codebase-memory exposes 14 tools but qwen only needs the read/query ones;
# drop the index/admin/write tools + the offline-useless web tools. Fewer tools = smaller
# surface = lower tool-call error rate for a weak model (the shim's salvage handles the rest).
DISALLOW="${CLAUDE_LOCAL_DISALLOW:-mcp__codebase-memory__index_repository mcp__codebase-memory__index_status mcp__codebase-memory__detect_changes mcp__codebase-memory__ingest_traces mcp__codebase-memory__manage_adr mcp__codebase-memory__delete_project mcp__codebase-memory__get_graph_schema WebSearch WebFetch}"

DISCIPLINE='NAME DISAMBIGUATION (important): the working directory (~/Projects/oracle) and the many oracle-* tool names refer to THIS project — codenamed "Oracle", an offline docs+code assistant you run inside. It has NOTHING to do with Oracle Database or Oracle Corporation; never let the word "oracle" pull an answer toward Oracle DBMS. The databases in this corpus/codebase are PostgreSQL and OrioleDB (a Postgres storage extension) — not Oracle. You are running on a smaller local model via Ollama, not Anthropic. PRECISION OVER SPEED: latency does not matter — be thorough. Never answer a technical question from your own training memory; always ground it with tools, and make AS MANY tool calls as needed (several ask_corpus queries to explore facets, multiple code-graph/source lookups) to reach an exact, verified answer. Verify every specific (name, flag, byte size, struct field, version) against the exact source or corpus text — quote it — rather than approximating or answering quickly. When facts might conflict, cross-check with another query. A slow, correct, cited answer is the goal; a fast unverified one is a failure. ROUTE by what the question is about:
(A) DOCUMENTATION / CONCEPTS / language & library APIs / how-something-works / definitions / comparisons / best practice (Rust std, io_uring semantics, PostgreSQL concepts, Go, Linux, general knowledge) -> call ask_corpus. It retrieves+reranks+synthesizes from the offline doc corpus with citations, or says the corpus does not cover it. You MAY call it several times to explore related aspects — thoroughness is welcome. This is universal across topics. The corpus is NOT limited to programming — it also holds non-technical material (biology and other subjects). NEVER refuse a question as out-of-scope or claim you are "only a coding assistant": for ANY knowledge question, route it to ask_corpus and answer whatever the corpus grounds, or say the corpus does not cover it. The topic (mice, giraffes, anything) is irrelevant to whether you should try.
(B) EXACT SOURCE FACTS OF AN INDEXED REPOSITORY — a struct fields/byte layout, an enum values, a macro, a constant, a specific function implementation, "what are the WAL record types in orioledb", "trace X", "who calls Y", "show the source of Z" -> use the CODE tools, NOT ask_corpus. ask_corpus is DOCUMENTATION ONLY and will NOT contain project-specific source facts (e.g. OrioleDB defines its OWN WAL format in its source, separate from PostgreSQL WAL). Use ask_code(question, project) for a grounded one-call answer over the source (it greps, reads, and synthesizes with file:line citations + a RAW SOURCE block). For EXACT VALUES (enum codes, byte offsets, struct field order) trust the RAW SOURCE lines / read_lines over any prose summary — models miscopy value tables. READ THE SOURCE, DO NOT JUST SYNTHESIZE: for any question about a SPECIFIC codebase (a struct, an enum value, a type or OID mapping, a serialization or on-the-wire path, key ordering, a byte layout), you MUST open the actual source with source_search + read_lines and QUOTE the real lines; do NOT answer from ask_code synthesis alone, and NEVER fall back to generic parametric knowledge about how databases usually work. If ask_corpus or ask_code return only generic material, treat that as evidence the corpus does NOT cover this code, and switch to source_search / read_lines on the real repo. An answer about a specific codebase that cites no file:line from that repo is a FAILURE. For a single symbol'"'"'s EXACT resolved type/value/signature at a file:line, prefer lsp_hover (oracle-lsp) — it is the COMPILER'"'"'s ground truth, stronger than grep. Other oracle-lsp tools: lsp_definition/lsp_references/lsp_symbols (semantic, no false positives), and for REFACTORING a code region: lsp_code_actions lists the language server'"'"'s real refactorings (extract function, inline, …) and suggest_refactor has you reason over that actual menu; explain_code/propose_improvement for intent-level review. You can also drive the raw tools directly: search_graph/trace_path/get_code_snippet/query_graph/search_code (codebase-memory), source_search+read_lines (grep exact source; glob "*.h" for C headers, quote VERBATIM). To target a specific repo, FIRST call list_projects (source-grep or codebase-memory) to get the EXACT indexed project ids — never guess, hardcode, or reuse a project name from memory; the correct id comes from list_projects, not from this prompt.
Do NOT infer the subject of a question from the working directory or the oracle-* tool names — the folder you happen to run in, and those tool names, are just this environment; the cwd can be anything and is NOT a hint about what a question is about (in particular, the name "oracle" here is an environment label, not Oracle the database — the DBs in play are PostgreSQL and OrioleDB). Before you scope a search or an answer to a specific repo/project, CONFIRM that repo actually relates to the question (it really contains the symbol/topic); if you intend to treat some project as the target for the asked thing, verify it first rather than assuming. If a scoped search comes back empty, you are probably in the WRONG project — broaden or switch (source_search will tell you where the pattern actually occurs), do not narrow further. When unsure whether it is a doc concept or a source fact, prefer the code tools for anything that concerns a specific codebase, and ask_corpus for general/library knowledge. Only read a project file when a CODING task needs its current contents; read the SMALLEST relevant portion (grep first) and never read the same file twice. Obey every tool JSON schema EXACTLY: only documented fields, one call at a time, respect array-size limits (AskUserQuestion allows AT MOST 4 options). Prefer a sensible default over asking. Keep edits small and scoped; do not repeat a failed fix. If a tool call is rejected for a schema error, fix the args and retry once, then continue in plain text.'

# Experiment hook: append extra discipline rules from a file, if ORACLE_DISCIPLINE_EXTRA points at
# one. Used by eval-agent.py to A/B-test DISCIPLINE tweaks against the EVAL.md suites WITHOUT editing
# this script. Unset in normal use -> production discipline exactly as above.
if [ -n "${ORACLE_DISCIPLINE_EXTRA:-}" ] && [ -r "$ORACLE_DISCIPLINE_EXTRA" ]; then
	DISCIPLINE="$DISCIPLINE
$(cat "$ORACLE_DISCIPLINE_EXTRA")"
	echo "[claude-local] discipline EXTRA appended from $ORACLE_DISCIPLINE_EXTRA"
fi

DISALLOW_ARGS=()
# shellcheck disable=SC2206  # DISALLOW is a space-separated tool list; the split into array elements is intended
[ -n "$DISALLOW" ] && DISALLOW_ARGS=(--disallowed-tools $DISALLOW)

echo "[claude-local] model=$ANTHROPIC_MODEL via $ANTHROPIC_BASE_URL (offline; shim salvages tool calls)"
echo "[claude-local] MCP: codebase-memory (code graph) + oracle-ask (ask_corpus/ask_code) + oracle-lsp (hover/refactor)"
echo "[claude-local] all tools enabled; schema-discipline prompt appended${DISALLOW:+ (disabled: $DISALLOW)}"
echo "[claude-local] expect weaker agentic behavior than real Claude Code; keep tasks scoped."
# Trim Claude Code's built-in system prompt (frees context; qwen window is 56K).
exec claude --mcp-config "$MCP_CFG" --strict-mcp-config \
	--exclude-dynamic-system-prompt-sections \
	"${DISALLOW_ARGS[@]}" \
	--append-system-prompt "$DISCIPLINE" "$@"
