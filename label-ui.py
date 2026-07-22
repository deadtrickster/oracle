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


def opus_label(cid: str) -> dict | None:
    """The fleet's label for this chunk, if any — shown alongside, never instead of, yours.
    Your submission writes a labeler='human' row, and the `effective` view puts human first, so
    seeing the machine's opinion cannot silently become the training label."""
    r = STATE["conn"].execute(
        "SELECT label, certainty, note FROM latest WHERE labeler = 'opus' AND chunk_id = ?",
        (cid,)).fetchone()
    if not r:
        return None
    return {"label": r["label"], "certainty": r["certainty"], "note": r["note"]}


def opus_queue(conn, ids: list[str], max_cert: float) -> tuple[list[int], dict[int, str]]:
    """Review queue driven by the fleet's own uncertainty: chunks Opus labeled with
    certainty <= max_cert and no human label yet, least-certain first — the rows where a human
    minute buys the most training signal. Returns (npz row order, row->nominated)."""
    row_of = {cid: i for i, cid in enumerate(ids)}
    rows, picked = [], {}
    for r in conn.execute(
            "SELECT chunk_id, nominated FROM latest WHERE labeler = 'opus' "
            "AND certainty IS NOT NULL AND certainty <= ? "
            "AND chunk_id NOT IN (SELECT chunk_id FROM latest WHERE labeler = 'human') "
            "ORDER BY certainty ASC", (max_cert,)):
        i = row_of.get(r["chunk_id"])
        if i is not None:
            rows.append(i)
            picked[i] = r["nominated"] or "RANDOM"
    return rows, picked


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
                "opus": opus_label(cid),
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
    opus = s["by_labeler"].get("opus", {})
    agree = STATE["conn"].execute(
        "SELECT count(*) n, sum(h.label = o.label) same FROM latest h "
        "JOIN latest o ON o.chunk_id = h.chunk_id AND o.labeler = 'opus' "
        "WHERE h.labeler = 'human'").fetchone()
    return {"total": sum(mine.values()), "by_label": mine,
            "with_note": s["with_note"].get("human", 0),
            "with_spans": s["spans"].get("human", 0),
            "opus_total": sum(opus.values()), "opus_by_label": opus,
            "overlap": agree["n"] or 0, "agree": agree["same"] or 0}


@app.get("/rubric", response_class=PlainTextResponse)
def rubric():
    return RUBRIC_PATH.read_text(encoding="utf-8")


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>oracle-label</title><style>
body{font-family:system-ui,sans-serif;max-width:980px;margin:1.5em auto;padding:0 1em;background:#14161a;color:#dde}
#doc{color:#8ab;font-size:.85em} #facts{color:#789;font-size:.8em;margin:.3em 0}
#opus{display:none;background:#1c2733;border-left:3px solid #d90;padding:.3em .6em;
      margin:.3em 0;font-size:.85em} #opus .hint{color:#678;font-size:.85em}
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
#help{position:fixed;top:1.5em;right:1em;width:19em;background:#181b21;border:1px solid #2a2e36;
      border-radius:8px;padding:.7em .9em;font-size:.78em;line-height:1.5;color:#bcd}
#help h3{margin:.1em 0 .4em;font-size:1em;color:#dde}
#help .k{color:#8ab;font-weight:bold;font-family:ui-monospace,monospace;margin-right:.35em}
#help .cls{color:#dde;font-weight:600}
#help .desc{color:#89a;display:block;margin-left:1.5em}
#help hr{border:0;border-top:1px solid #2a2e36;margin:.5em 0}
@media (max-width:1400px){#help{display:none}}
</style></head><body>
<div id="bar"><span id="pos"></span>
<span><a href="/rubric" target="_blank">RUBRIC.md</a> (<kbd>r</kbd>) ·
<kbd>1</kbd>-<kbd>9</kbd> label · <kbd>g</kbd> mark selected text as garbage span · <kbd>s</kbd> skip · <kbd>n</kbd> note</span></div>
<div id="doc"></div><div id="facts"></div><div id="opus"></div><div id="text"></div>
<div id="btns"></div>
<div id="spans"></div>
<textarea id="note" rows="2" placeholder="optional: why this label (notes become rubric amendments)"></textarea>
<div id="tally"></div>
<div id="help"></div>
<script>
const CLASSES = __CLASSES__;
const HELP = __HELP__;
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
  const o = cur.opus, oel = document.getElementById('opus');
  if(o){
    oel.innerHTML = 'opus: <b>'+esc(o.label)+'</b> @ '+
      (o.certainty==null?'?':Math.round(o.certainty*100)+'%')+
      (o.note?' — <i>'+esc(o.note)+'</i>':'')+
      '  <span class="hint">(your label overrides)</span>';
    oel.style.display='block';
  } else { oel.style.display='none'; oel.innerHTML=''; }
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
const hp=document.getElementById('help');
hp.innerHTML='<h3>keys</h3>'+CLASSES.map((c,i)=>
  '<div><span class="k">'+(i+1)+'</span><span class="cls">'+c+'</span>'+
  '<span class="desc">'+esc(HELP[c]||'')+'</span></div>').join('')+
  '<hr><div><span class="k">s</span>skip (revisit at end)</div>'+
  '<div><span class="k">g</span>mark selected text as garbage span</div>'+
  '<div><span class="k">n</span>focus note field</div>'+
  '<div><span class="k">r</span>open RUBRIC.md</div>'+
  '<hr><div><a href="/review" target="_blank">review labeled chunks →</a></div>';
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


def class_help() -> dict[str, str]:
    """One-liners parsed from RUBRIC.md's `### CLASS — description (action)` headings — the help
    panel quotes the rubric instead of duplicating it, so it can never drift from the law."""
    out = {}
    for m in re.finditer(r"^### (\w+) — (.+)$", RUBRIC_PATH.read_text(encoding="utf-8"), re.M):
        out[m.group(1)] = m.group(2)
    return out


@app.get("/review", response_class=HTMLResponse)
def review(cls: str = "", labeler: str = "opus", minc: float = 0.0, maxc: float = 1.0,
           limit: int = 200):
    """Browse what the fleet (or any labeler) has already labeled — filter by class + certainty.
    /review                      all opus labels, newest first
    /review?cls=TOC              one class
    /review?maxc=0.8             low-certainty only
    /review?labeler=human        your own labels"""
    q = ("SELECT l.chunk_id, l.label, l.certainty, l.note, l.docnm, l.text, l.created_at, "
         " (SELECT h.label FROM latest h WHERE h.chunk_id = l.chunk_id AND h.labeler='human') "
         "   AS human_label "
         "FROM latest l WHERE l.labeler = ? "
         "AND COALESCE(l.certainty, 1.0) BETWEEN ? AND ? ")
    args = [labeler, minc, maxc]
    if cls:
        q += "AND l.label = ? "
        args.append(cls)
    q += "ORDER BY COALESCE(l.certainty, 1.0) ASC, l.id DESC LIMIT ?"
    args.append(limit)
    rows = STATE["conn"].execute(q, args).fetchall()
    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;")
    opts = "".join(f'<a href="/review?cls={c}">{c}</a> ' for c in CLASSES)
    trs = []
    for r in rows:
        cert = "" if r["certainty"] is None else f'{round(100 * r["certainty"])}%'
        human = f'<b class="hum">{esc(r["human_label"])}</b>' if r["human_label"] else "—"
        trs.append(
            f'<tr><td class="c">{esc(r["label"])}</td><td class="c">{cert}</td>'
            f'<td>{human}</td><td class="d">{esc(r["docnm"])}</td>'
            f'<td><details><summary>{esc(" ".join((r["text"] or "").split())[:140])}</summary>'
            f'<pre>{esc(r["text"])}</pre>'
            f'<div class="n">note: {esc(r["note"]) or "—"} · {esc(r["chunk_id"])}'
            f' · {esc(r["created_at"])}</div></details></td></tr>')
    return f"""<!doctype html><meta charset="utf-8"><title>labels — review</title><style>
body{{font-family:system-ui;background:#14161a;color:#dde;margin:1.5em;font-size:.9em}}
a{{color:#8ab;margin-right:.2em}} table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #2a2e36;padding:.35em .5em;text-align:left;vertical-align:top}}
td.c{{white-space:nowrap}} td.d{{color:#8ab;font-size:.85em;max-width:16em;overflow:hidden}}
pre{{white-space:pre-wrap;background:#1b1e24;padding:.8em;border-radius:6px;max-height:40vh;overflow:auto}}
summary{{cursor:pointer;color:#cdd}} .n{{color:#789;font-size:.85em}} .hum{{color:#9d7}}
</style>
<p><b>{len(rows)}</b> rows · labeler={esc(labeler)} · certainty {minc}–{maxc}
 · filter: <a href="/review">ALL</a> {opts}
 · <a href="/review?maxc=0.8">uncertain≤0.8</a> · <a href="/review?labeler=human">mine</a>
 · <a href="/">back to labeling</a></p>
<table><tr><th>label</th><th>cert</th><th>mine</th><th>doc</th><th>chunk</th></tr>
{''.join(trs)}</table>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return (PAGE.replace("__CLASSES__", json.dumps(CLASSES))
                .replace("__HELP__", json.dumps(class_help())))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--features", type=Path, required=True, help=".npz from build-junk-features.py")
    ap.add_argument("--db", type=Path, default=_db.DEFAULT_DB)
    ap.add_argument("--per-class", type=int, default=150)
    ap.add_argument("--random", type=int, default=400, dest="n_random")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--port", type=int, default=9770)
    ap.add_argument("--queue", choices=["nominate", "uncertain"], default="nominate",
                    help="nominate: heuristic+random queue (default). uncertain: review the fleet's "
                         "LOW-CERTAINTY labels, least certain first — your minute per chunk buys "
                         "the most training signal there.")
    ap.add_argument("--max-certainty", type=float, default=0.8,
                    help="--queue uncertain: only chunks Opus labeled at or below this certainty")
    args = ap.parse_args()

    d = np.load(args.features, allow_pickle=True)
    conn = _db.connect(args.db)
    if args.queue == "uncertain":
        ids = [str(x) for x in d["ids"]]
        order, picked = opus_queue(conn, ids, args.max_certainty)
        print(f"queue: uncertain (certainty <= {args.max_certainty}) — {len(order)} chunks")
    else:
        order, picked = build_queue(d, args.per_class, args.n_random, args.seed)
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
