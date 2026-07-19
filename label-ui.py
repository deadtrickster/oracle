#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["fastapi", "uvicorn", "numpy", "requests"]
# ///
"""Human chunk-labeling UI for the junk classifier (TODO G3.8).

Serves a local page (default :9770) that walks you through corpus chunks one at a time:
press 1-9 to label with a RUBRIC.md class, optionally add a note (why — notes become rubric
amendments and new heuristics), select text and press g to mark a GARBAGE SPAN (the split can be
inside a chunk), s to skip. Labels land in the SQLite labels DB (label-db.py) with labeler +
rubric-version provenance — the audit trail and the training set are the same rows.

Anti-anchoring by design: the queue mixes rule-nominated candidates with random chunks, but the UI
NEVER shows which nominator picked a chunk (or any model verdict) — the human labels blind, per the
labeling protocol (RUBRIC.md). The nominator IS recorded in the saved row for later rule-mining.

    uv run label-ui.py --features coll.npz [--db labels.db]
    # then open http://localhost:9770
"""
import argparse
import json
import re
import time
from pathlib import Path

import numpy as np
import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

import importlib.util
import os
_spec = importlib.util.spec_from_file_location("label_junk", Path(__file__).parent / "label-junk.py")
_lj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lj)
_spec2 = importlib.util.spec_from_file_location("label_db", Path(__file__).parent / "label-db.py")
_db = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(_db)

ES_URL = os.environ.get("ORACLE_ES_URL", "http://localhost:1200")
ES_AUTH = tuple(os.environ.get("ORACLE_ES_AUTH", "elastic:infini_rag_flow").split(":", 1))
CLASSES = _lj.CLASSES
RUBRIC_PATH = Path(__file__).parent / "RUBRIC.md"

app = FastAPI(title="oracle-label")
STATE: dict = {}


def _es_index() -> str:
    r = requests.get(f"{ES_URL}/_cat/indices?h=index&format=json", auth=ES_AUTH, timeout=30)
    return next(x["index"] for x in r.json() if re.fullmatch(r"ragflow_[0-9a-f]{32}", x["index"]))


def fetch_one(cid: str) -> str:
    r = requests.get(f"{STATE['idx']}/_doc/{cid}?_source=content_with_weight",
                     auth=ES_AUTH, timeout=30)
    if r.status_code != 200:
        return ""
    return r.json().get("_source", {}).get("content_with_weight", "")


def build_queue(d, per_class: int, n_random: int, seed: int):
    fn = list(d["feat_names"])
    surf = d["surf"]
    masks = _lj.nominators(surf, fn)
    rng = np.random.default_rng(seed)
    picked: dict[int, str] = {}
    for cls, mask in masks.items():
        rows = [r for r in np.where(mask)[0] if r not in picked]
        for r in rng.permutation(rows)[:per_class]:
            picked[int(r)] = cls
    pool = [r for r in range(len(d["ids"])) if r not in picked]
    for r in rng.permutation(pool)[:n_random]:
        picked[int(r)] = "RANDOM"
    order = rng.permutation(list(picked)).tolist()   # shuffled: no class runs to anchor on
    return order, picked


def already_labeled() -> set[str]:
    return _db.labeled_ids(STATE["conn"], "human")


@app.get("/api/next")
def api_next():
    d = STATE["d"]
    done = STATE["labeled"]
    while STATE["ptr"] < len(STATE["order"]):
        row = STATE["order"][STATE["ptr"]]
        cid = str(d["ids"][row])
        if cid in done:
            STATE["ptr"] += 1
            continue
        text = fetch_one(cid)
        if not text:
            STATE["ptr"] += 1
            continue
        fn = list(d["feat_names"])
        s = d["surf"][row]
        f = dict(zip(fn, s.tolist()))
        return {"chunk_id": cid, "row": row, "docnm": str(d["docnm"][row]),
                "text": text,
                "facts": {"tokens": int(f["n_tokens"]), "lines": int(f["n_lines"]),
                          "page": int(f["page_first"]) if f["page_first"] >= 0 else None,
                          "stopword%": round(100 * f["stopword_ratio"]),
                          "pdf": bool(f["is_pdf"])},
                "position": STATE["ptr"] + 1, "total": len(STATE["order"]),
                "labeled": len(done)}
    return {"done": True, "labeled": len(done), "total": len(STATE["order"])}


@app.post("/api/label")
async def api_label(payload: dict):
    d = STATE["d"]
    row = int(payload["row"])
    cid = str(d["ids"][row])
    label = payload["label"]
    if label == "SKIP":
        STATE["order"].append(STATE["order"][STATE["ptr"]])   # revisit at the end
        STATE["ptr"] += 1
        return {"ok": True}
    if label not in CLASSES:
        return JSONResponse({"error": f"unknown label {label}"}, status_code=400)
    rec = {"chunk_id": cid, "docnm": str(d["docnm"][row]),
           "kb_id": str(d["kb"][row]), "doc_id": str(d["doc"][row]),
           "label": label, "note": (payload.get("note") or "").strip(),
           # garbage SPANS within the chunk (exact substrings the labeler marked): the split can be
           # INSIDE a chunk (mixed diagram-OCR + prose), so class alone under-specifies the repair.
           # Spans are the gold standard the automatic excision gets judged against.
           "spans": [s for s in (payload.get("spans") or []) if isinstance(s, str) and s.strip()],
           "nominated": STATE["picked"].get(row, "RANDOM"),   # stored, never displayed
           "text": " ".join(fetch_one(cid).split())[:400]}
    _db.add_label(STATE["conn"], chunk_id=rec["chunk_id"], label=rec["label"], labeler="human",
                  kb_id=rec["kb_id"], doc_id=rec["doc_id"], docnm=rec["docnm"], note=rec["note"],
                  nominated=rec["nominated"], text=rec["text"], spans=rec["spans"])
    STATE["labeled"].add(cid)
    STATE["ptr"] += 1
    return {"ok": True}


@app.get("/api/stats")
def api_stats():
    s = _db.stats(STATE["conn"])
    mine = s["by_labeler"].get("human", {})
    return {"total": sum(mine.values()), "by_label": mine,
            "with_note": s["with_note"].get("human", 0),
            "with_spans": s["spans"].get("human", 0)}


@app.get("/rubric", response_class=PlainTextResponse)
def rubric():
    return RUBRIC_PATH.read_text(encoding="utf-8")


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>oracle-label</title><style>
body{font-family:system-ui,sans-serif;max-width:980px;margin:1.5em auto;padding:0 1em;background:#14161a;color:#dde}
#doc{color:#8ab;font-size:.85em} #facts{color:#789;font-size:.8em;margin:.3em 0}
#text{white-space:pre-wrap;background:#1b1e24;border:1px solid #333;border-radius:8px;
      padding:1em;font-family:ui-monospace,monospace;font-size:.92em;line-height:1.45;
      max-height:52vh;overflow-y:auto;margin:.6em 0}
.weird{color:#ff5c5c;font-weight:bold}
mark{background:#7a2f2f;color:#fdd;border-radius:3px}
.chip{display:inline-block;background:#3a2626;border:1px solid #644;border-radius:5px;
      padding:.1em .5em;margin:.15em;font-size:.8em;font-family:ui-monospace,monospace;color:#e99}
.chip b{cursor:pointer;color:#faa;margin-left:.4em}
#btns button{margin:.15em;padding:.45em .7em;border-radius:6px;border:1px solid #444;
             background:#242832;color:#dde;cursor:pointer;font-size:.9em}
#btns button:hover{background:#33394a} .num{color:#8ab;font-weight:bold;margin-right:.3em}
#note{width:100%;background:#1b1e24;color:#dde;border:1px solid #333;border-radius:6px;
      padding:.5em;font-size:.9em;margin:.4em 0}
#bar{display:flex;justify-content:space-between;color:#789;font-size:.85em}
#tally{color:#9a8;font-size:.8em;margin-top:.4em} a{color:#8ab}
kbd{background:#242832;border:1px solid #444;border-radius:4px;padding:0 .3em;font-size:.85em}
</style></head><body>
<div id="bar"><span id="pos"></span>
<span><a href="/rubric" target="_blank">RUBRIC.md</a> (<kbd>r</kbd>) ·
<kbd>1</kbd>-<kbd>9</kbd> label · <kbd>g</kbd> mark selected text as garbage span · <kbd>s</kbd> skip · <kbd>n</kbd> note</span></div>
<div id="doc"></div><div id="facts"></div><div id="text"></div>
<div id="btns"></div>
<div id="spans"></div>
<textarea id="note" rows="2" placeholder="optional: why this label (notes become rubric amendments)"></textarea>
<div id="tally"></div>
<script>
const CLASSES = __CLASSES__;
let cur = null, pending = null, spans = [];
const W = /[\\u2500-\\u257f\\u2580-\\u259f\\u25a0-\\u25ff\\u2600-\\u26ff\\u2700-\\u27bf\\u3000-\\u303f\\u3040-\\u30ff\\u4e00-\\u9fff\\uff00-\\uffef\\ufffd]/g;
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;')}
function render(){
  document.getElementById('pos').textContent =
    cur.done ? 'QUEUE DONE — '+cur.labeled+' labeled' :
    '#'+cur.position+' / '+cur.total+'  ('+cur.labeled+' labeled)';
  if(cur.done){document.getElementById('text').textContent='All queued chunks labeled. 🎉';return}
  document.getElementById('doc').textContent = cur.docnm;
  const f = cur.facts;
  document.getElementById('facts').textContent =
    f.tokens+' tokens · '+f.lines+' lines · stopwords '+f['stopword%']+'%'+
    (f.page!==null?' · p.'+f.page:'')+(f.pdf?' · pdf':'');
  renderText();
  document.getElementById('note').value='';
}
function renderText(){
  let html = esc(cur.text);
  for(const s of spans){
    const e = esc(s);
    const i = html.indexOf(e);
    if(i >= 0) html = html.slice(0,i)+'<mark>'+e+'</mark>'+html.slice(i+e.length);
  }
  document.getElementById('text').innerHTML = html.replace(W, m=>'<span class="weird">'+m+'</span>');
  document.getElementById('spans').innerHTML = spans.map((s,i)=>
    '<span class="chip">'+esc(s.length>60?s.slice(0,60)+'…':s)+'<b data-i="'+i+'">✕</b></span>').join('');
  document.querySelectorAll('#spans b').forEach(b=>b.onclick=()=>{spans.splice(+b.dataset.i,1);renderText()});
}
function markSpan(){
  const sel = window.getSelection().toString();
  if(sel && cur && cur.text.includes(sel) && !spans.includes(sel)){
    spans.push(sel); window.getSelection().removeAllRanges(); renderText();
  }
}
async function next(){spans = []; cur = await (await fetch('/api/next')).json(); render(); stats();}
async function stats(){
  const s = await (await fetch('/api/stats')).json();
  document.getElementById('tally').textContent =
    'labeled: '+s.total+'  '+JSON.stringify(s.by_label)+'  notes: '+s.with_note+'  with-spans: '+s.with_spans;
}
async function submit(label){
  if(cur.done) return;
  await fetch('/api/label',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({row:cur.row,label:label,note:document.getElementById('note').value,spans:spans})});
  next();
}
const btns=document.getElementById('btns');
CLASSES.forEach((c,i)=>{const b=document.createElement('button');
  b.innerHTML='<span class="num">'+(i+1)+'</span>'+c;
  b.onclick=()=>submit(c); btns.appendChild(b);});
const sk=document.createElement('button'); sk.innerHTML='<span class="num">s</span>SKIP';
sk.onclick=()=>submit('SKIP'); btns.appendChild(sk);
document.addEventListener('keydown',e=>{
  const noteFocused = document.activeElement===document.getElementById('note');
  if(e.key==='Enter' && pending){submit(pending); pending=null; e.preventDefault(); return}
  if(noteFocused){ if(e.key==='Escape') document.activeElement.blur(); return }
  const k=parseInt(e.key);
  if(k>=1 && k<=CLASSES.length){submit(CLASSES[k-1])}
  else if(e.key==='s'){submit('SKIP')}
  else if(e.key==='g'){markSpan()}
  else if(e.key==='n'){document.getElementById('note').focus(); e.preventDefault()}
  else if(e.key==='r'){window.open('/rubric')}
});
next();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE.replace("__CLASSES__", json.dumps(CLASSES))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--features", type=Path, required=True, help=".npz from build-junk-features.py")
    ap.add_argument("--db", type=Path, default=_db.DEFAULT_DB)
    ap.add_argument("--per-class", type=int, default=150)
    ap.add_argument("--random", type=int, default=400, dest="n_random")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--port", type=int, default=9770)
    args = ap.parse_args()

    d = np.load(args.features, allow_pickle=True)
    order, picked = build_queue(d, args.per_class, args.n_random, args.seed)
    conn = _db.connect(args.db)
    conn2 = None  # sqlite conns are per-thread; uvicorn default is one worker thread — fine
    STATE.update(d=d, conn=conn, order=order, picked=picked, ptr=0,
                 idx=f"{ES_URL}/{_es_index()}")
    STATE["labeled"] = already_labeled()
    print(f"db: {args.db} (rubric v{_db.rubric_version()}); "
          f"queue: {len(order)} chunks ({len(STATE['labeled'])} already labeled)")
    print(f"open http://localhost:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
