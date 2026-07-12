# /// script
# requires-python = ">=3.10"
# dependencies = ["sentence-transformers", "torch"]
# ///
"""Benchmark + Russian-correctness check for a multilingual small cross-encoder
reranker on CPU. mmarco-mMiniLMv2-L12 is trained on mMARCO (incl. Russian)."""
import os, time
os.environ.setdefault("OMP_NUM_THREADS", "24")
import torch; torch.set_num_threads(24)
from sentence_transformers import CrossEncoder

MODEL = "Alibaba-NLP/gte-multilingual-reranker-base"
EN_CHUNK = ("IORING_SETUP_SQPOLL creates a dedicated kernel polling thread for the submission "
            "queue so applications submit I/O without syscalls. ") * 4
Q = "What does IORING_SETUP_SQPOLL do?"

print(f"loading {MODEL} on CPU...")
t0 = time.perf_counter()
m = CrossEncoder(MODEL, device="cpu", max_length=512, trust_remote_code=True)
print(f"  load: {time.perf_counter()-t0:.1f}s")

for k in (10, 30, 50):
    pairs = [(Q, EN_CHUNK)] * k
    m.predict(pairs[:2])
    runs = [(lambda: (lambda t: time.perf_counter()-t)(time.perf_counter()) if not m.predict(pairs, batch_size=k) is None else 0)() for _ in range(3)]
    # simpler timing:
    best = min((lambda: (lambda s: (m.predict(pairs, batch_size=k), time.perf_counter()-s)[1])(time.perf_counter()))() for _ in range(3))
    print(f"  rerank {k:>2}: {best*1000:6.0f} ms")

# --- Russian correctness: relevant RU pair must outscore irrelevant RU pair ---
ru_query = "Как работает буферный кэш и вытеснение страниц в PostgreSQL?"
ru_relevant = ("Буферный кэш PostgreSQL хранит страницы в разделяемой памяти. Когда нужен новый "
               "буфер, применяется алгоритм вытеснения (clock-sweep), который выбирает жертву среди "
               "буферов по счётчику использования usage_count.")
ru_irrelevant = ("Команда VACUUM удаляет мёртвые версии строк и обновляет карту видимости, но не "
                 "возвращает место операционной системе без FULL.")
scores = m.predict([(ru_query, ru_relevant), (ru_query, ru_irrelevant)])
print(f"\nRussian sanity: relevant={scores[0]:.3f}  irrelevant={scores[1]:.3f}  "
      f"-> {'OK (relevant ranked higher)' if scores[0] > scores[1] else 'FAIL'}")
