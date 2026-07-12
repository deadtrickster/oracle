# /// script
# requires-python = ">=3.10"
# dependencies = ["sentence-transformers", "torch"]
# ///
"""Benchmark bge-reranker-v2-m3 on CPU with a realistic RAG rerank workload:
one query vs N doc chunks (~400 tokens each). Reports real latency on this box."""
import os
import time

os.environ.setdefault("OMP_NUM_THREADS", "24")
import torch
torch.set_num_threads(24)
from sentence_transformers import CrossEncoder

CHUNK = ("The IORING_SETUP_SQPOLL flag, when set during io_uring_setup, instructs the kernel "
         "to create a dedicated kernel thread that polls the submission queue. This allows an "
         "application to submit I/O without ever making a system call, since the kernel side "
         "picks up submission queue entries automatically. The thread will go to sleep after a "
         "period of inactivity controlled by sq_thread_idle, and the application must call "
         "io_uring_enter with IORING_ENTER_SQ_WAKEUP to wake it if it may have slept. ") * 3
QUERY = "What does IORING_SETUP_SQPOLL do and how does the polling thread sleep?"

print("loading bge-reranker-v2-m3 on CPU...")
t0 = time.perf_counter()
model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L6-v2", device="cpu", max_length=512)
print(f"  load: {time.perf_counter()-t0:.1f}s  (one-time at service start)")

for k in (10, 20, 30, 50):
    pairs = [(QUERY, CHUNK) for _ in range(k)]
    model.predict(pairs[:2])  # warmup
    runs = []
    for _ in range(3):
        t = time.perf_counter()
        model.predict(pairs, batch_size=k)
        runs.append(time.perf_counter() - t)
    best = min(runs)
    print(f"  rerank {k:>2} chunks: {best*1000:6.0f} ms  ({best/k*1000:.0f} ms/chunk)")
