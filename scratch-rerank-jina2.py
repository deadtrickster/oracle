# /// script
# requires-python = ">=3.10"
# dependencies = ["torch", "transformers", "einops"]
# ///
import os, time
os.environ.setdefault("OMP_NUM_THREADS","24")
import torch; torch.set_num_threads(24)
from transformers import AutoModelForSequenceClassification
M="jinaai/jina-reranker-v2-base-multilingual"
EN=("IORING_SETUP_SQPOLL creates a dedicated kernel polling thread for the submission queue so applications submit I/O without syscalls. ")*4
Q="What does IORING_SETUP_SQPOLL do?"
print(f"loading {M} ...")
t0=time.perf_counter()
m=AutoModelForSequenceClassification.from_pretrained(M, torch_dtype="auto", trust_remote_code=True)
m.eval(); print(f"  load: {time.perf_counter()-t0:.1f}s")
for k in (10,20,30,50):
    p=[[Q,EN]]*k; m.compute_score(p[:2], max_length=512)
    best=min((lambda s=time.perf_counter():(m.compute_score(p,max_length=512),time.perf_counter()-s)[1])() for _ in range(3))
    print(f"  rerank {k:>2}: {best*1000:6.0f} ms")
rq="Как работает буферный кэш и вытеснение страниц в PostgreSQL?"
rr="Буферный кэш PostgreSQL хранит страницы в разделяемой памяти; алгоритм clock-sweep выбирает жертву по usage_count."
ri="Команда VACUUM удаляет мёртвые версии строк, но не возвращает место ОС без FULL."
s=m.compute_score([[rq,rr],[rq,ri]], max_length=512)
print(f"\nRussian: rel={s[0]:.3f} irr={s[1]:.3f} -> {'OK' if s[0]>s[1] else 'FAIL'}")
