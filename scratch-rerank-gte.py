# /// script
# requires-python = ">=3.10"
# dependencies = ["sentence-transformers>=3.0", "torch", "transformers", "einops"]
# ///
"""Benchmark gte-multilingual-reranker-base (~306M, multilingual) on CPU +
Russian correctness. Target: <=2-3s for 30 chunks."""
import os, time
os.environ.setdefault("OMP_NUM_THREADS", "24")
import torch; torch.set_num_threads(24)
from sentence_transformers import CrossEncoder

MODEL = "Alibaba-NLP/gte-multilingual-reranker-base"
EN = ("IORING_SETUP_SQPOLL creates a dedicated kernel polling thread for the submission queue "
      "so applications submit I/O without syscalls. ") * 4
Q = "What does IORING_SETUP_SQPOLL do?"

print(f"loading {MODEL} on CPU...")
t0 = time.perf_counter()
m = CrossEncoder(MODEL, device="cpu", max_length=512, trust_remote_code=True)
print(f"  load: {time.perf_counter()-t0:.1f}s")

for k in (10, 20, 30, 50):
    pairs = [(Q, EN)] * k
    m.predict(pairs[:2])
    best = min((lambda s=time.perf_counter(): (m.predict(pairs, batch_size=k), time.perf_counter()-s)[1])() for _ in range(3))
    print(f"  rerank {k:>2}: {best*1000:6.0f} ms")

ru_q = "Как работает буферный кэш и вытеснение страниц в PostgreSQL?"
ru_rel = ("Буферный кэш PostgreSQL хранит страницы в разделяемой памяти. Когда нужен новый буфер, "
          "применяется алгоритм вытеснения clock-sweep, выбирающий жертву по счётчику usage_count.")
ru_irr = ("Команда VACUUM удаляет мёртвые версии строк и обновляет карту видимости, но не возвращает "
          "место операционной системе без FULL.")
s = m.predict([(ru_q, ru_rel), (ru_q, ru_irr)])
print(f"\nRussian: relevant={s[0]:.3f} irrelevant={s[1]:.3f} -> {'OK' if s[0]>s[1] else 'FAIL'}")
