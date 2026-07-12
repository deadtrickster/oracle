# /// script
# requires-python = ">=3.10"
# dependencies = ["fastapi", "uvicorn", "sentence-transformers>=3.0", "torch", "transformers==4.48.3", "einops"]
# ///
"""Oracle reranker service — gte-multilingual-reranker-base on CPU.

Serves the Jina/Cohere/LocalAI-style rerank API that RAGFlow consumes:
  POST /rerank  {"model","query","documents":[...],"top_n"}
             -> {"results":[{"index","relevance_score"}], "model", "usage"}

transformers is PINNED to 4.48.3 — v5 removed create_position_ids_from_input_ids and broke
GTE/jina RoPE buffer init. Multilingual (verified on Russian), ~2.7s for 30 chunks on CPU.
Env: ORACLE_RERANK_MODEL (default gte), ORACLE_RERANK_PORT (default 9760),
     ORACLE_RERANK_THREADS (default 24).
"""
import os
import time

os.environ.setdefault("OMP_NUM_THREADS", os.environ.get("ORACLE_RERANK_THREADS", "24"))
import torch
torch.set_num_threads(int(os.environ.get("ORACLE_RERANK_THREADS", "24")))

from fastapi import FastAPI
from pydantic import BaseModel
from sentence_transformers import CrossEncoder
import uvicorn

MODEL = os.environ.get("ORACLE_RERANK_MODEL", "Alibaba-NLP/gte-multilingual-reranker-base")
PORT = int(os.environ.get("ORACLE_RERANK_PORT", "9760"))

print(f"[reranker] loading {MODEL} on CPU ...", flush=True)
_t = time.perf_counter()
_model = CrossEncoder(MODEL, device="cpu", max_length=512, trust_remote_code=True)
# warm up (first forward pass compiles/threads)
_model.predict([("warmup query", "warmup document text")])
print(f"[reranker] ready in {time.perf_counter()-_t:.1f}s", flush=True)

app = FastAPI(title="oracle-reranker")


class RerankRequest(BaseModel):
    query: str
    documents: list[str]
    model: str | None = None
    top_n: int | None = None


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL}


@app.post("/rerank")
def rerank(req: RerankRequest):
    docs = req.documents or []
    if not docs:
        return {"model": req.model or MODEL, "results": [], "usage": {"total_tokens": 0}}
    pairs = [(req.query, d) for d in docs]
    scores = _model.predict(pairs, batch_size=len(pairs))
    results = [{"index": i, "relevance_score": float(s)} for i, s in enumerate(scores)]
    results.sort(key=lambda r: r["relevance_score"], reverse=True)
    if req.top_n:
        results = results[: req.top_n]
    tokens = sum(len(d) // 4 for d in docs)
    return {"model": req.model or MODEL, "results": results, "usage": {"total_tokens": tokens}}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
