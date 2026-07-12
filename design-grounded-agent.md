# Sketch — #2 Deterministic extract-then-answer agent (enforced scratchpad)

Goal: make the extract-then-answer discipline STRUCTURAL, not just prompted. In the chat (#1)
qwen *can* shortcut the extraction step; in an agent flow the scratchpad is a real intermediate
artifact between nodes, so it cannot be skipped. This is the C3L principle applied to RAG:
split into scoped, deterministic sub-steps a weak model does reliably.

## Canvas flow (RAGFlow agent, linear)

    Begin
      -> Retrieval        (kb_ids = all doc KBs; rerank_id = gte; top_k=64, top_n=8)
      -> Agent: EXTRACTOR (llm qwen; NO tools; writes the scratchpad)
      -> Agent: SYNTHESIZER (llm qwen; NO tools; answers only from scratchpad)
      -> Message

The Retrieval node runs FIRST and deterministically (model can't skip it) — that's what "forces
RAG first". Its output (the reranked chunks) is piped into the Extractor.

## Node configs

### Retrieval  (component_name: "Retrieval")
    kb_ids: [all doc dataset ids]      # rust, io_uring, linux, go, postgres, books, papers, ...
    rerank_id: "gte-multilingual-reranker-base@local-gte-rerank@Jina"
    top_k: 64                          # candidates the reranker scores (keeps it <30s on CPU)
    top_n: 8                           # chunks passed downstream
    similarity_threshold: 0.2
    outputs.formalized_content -> the retrieved+reranked chunk text

### Agent: EXTRACTOR
    llm_id: qwen3-coder:30b@ollama-oai@OpenAI-API-Compatible
    tools: []   mcp: []                # deliberately tool-less: pure text->text
    sys_prompt: |
      You extract facts. Input is retrieved documentation chunks. Output a numbered list of the
      VERBATIM sentences/snippets relevant to the user question, each tagged with its source doc.
      Copy exactly — never paraphrase, never add anything not in the chunks. If a chunk is
      irrelevant, skip it. If nothing is relevant, output exactly: NO_RELEVANT_FACTS.
    user_prompt: |
      Question: {sys.query}
      Chunks:
      {Retrieval@formalized_content}
    outputs.content -> the scratchpad (numbered verbatim facts)

### Agent: SYNTHESIZER
    llm_id: qwen3-coder:30b@ollama-oai@OpenAI-API-Compatible
    tools: []   mcp: []
    sys_prompt: |
      Answer the question using ONLY the numbered Facts provided. Every specific claim (names,
      flags, sizes, semantics, versions) must trace to a Fact; cite the fact number/source. Do
      NOT use general knowledge. If the Facts are NO_RELEVANT_FACTS or insufficient, reply: "The
      knowledge base doesn't cover this." Tag code fences by language.
    user_prompt: |
      Question: {sys.query}
      Facts:
      {Agent:EXTRACTOR@content}
    outputs.content -> final answer

## Optional stronger variant — per-chunk fresh context (your "loop")

Replace the single EXTRACTOR with a map over chunks so each fact is extracted with CLEAN context
(no cross-chunk contamination), then reduce:

    Retrieval -> (for each of top_n chunks) Agent:EXTRACT_ONE(chunk) -> concat scratchpad
              -> Agent: SYNTHESIZER

RAGFlow's canvas doesn't loop natively over a list inside one agent, so this variant is better
done as a small deterministic driver (like oracle-ingest-mcp.py / the ingestor): call the
extractor once per chunk via the API, accumulate, then one synthesis call. More robust, more
plumbing. Only worth it if single-context extraction (top_n=8 ~= 4K tokens, fits qwen's 56K
easily) proves to conflate chunks — usually it won't at top_n=8.

## Why this beats #1 (the prompt-only version)
- The scratchpad is a real artifact BETWEEN nodes -> qwen cannot skip extraction.
- Extraction is a tool-less copy task (low hallucination surface); synthesis sees only trusted
  text. Each sub-agent has one scoped job.
- Retrieval is a deterministic first node -> RAG genuinely runs first, every time.

## Cost / tradeoffs
- 2 (or N+1) qwen passes per question -> higher latency. At 56K ctx and top_n=8 the extractor
  input is small, so each pass is fast; still ~2x the single-pass chat.
- Over-caution: if the corpus lacks the answer it says so instead of guessing (the right trade
  for a trusted reference brain).
- Compose with reranker (already wired): rerank picks the right 8 chunks; extract-then-synth
  keeps generation grounded in them. Two halves of one goal.

## To build
Reuse create-omni.py's DSL scaffolding: 5 nodes (begin, Retrieval, EXTRACTOR, SYNTHESIZER,
Message), linear edges, Retrieval component params above, two tool-less Agent nodes. ~1 script,
same pattern as the existing agents. Title it "oracle-grounded".
