#!/usr/bin/env python3
"""SQLite store for junk-classifier labels — the single labels database (TODO G3.8).

Labels are relational data: latest-label-per-(chunk,labeler), agreement joins between labelers,
class distributions, span sets — all SQL, not JSONL grepping. The DB is also a publishable artifact:
every row carries labeler + rubric_version + timestamp provenance, and spans live in their own table
(the split can be INSIDE a chunk, so a class alone under-specifies the repair).

Schema:
  labels(id, chunk_id, kb_id, doc_id, docnm, label, note, nominated, labeler, rubric_version,
         text, created_at)   -- append-only; latest row per (chunk_id, labeler) is current
  spans(label_id -> labels.id, span)   -- exact substrings marked as garbage within the chunk
  latest view: current label per (chunk_id, labeler)

CLI:  ./label-db.py stats [--db labels.db]
      ./label-db.py export [--db labels.db] [--labeler human]   # JSONL to stdout (for publishing)
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

CLASSES = ["CLEAN", "TOC", "INDEX", "EXERCISE", "BIBLIOGRAPHY", "FIGURE_GARBAGE",
           "OCR_DAMAGED_CODE", "DEBRIS", "BOILERPLATE"]
DEFAULT_DB = Path(__file__).parent / "labels.db"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS labels (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  chunk_id       TEXT NOT NULL,
  kb_id          TEXT,
  doc_id         TEXT,
  docnm          TEXT,
  label          TEXT NOT NULL CHECK (label IN ({",".join(repr(c) for c in CLASSES)})),
  note           TEXT NOT NULL DEFAULT '',
  nominated      TEXT,                  -- which heuristic queued it (never shown to the labeler)
  labeler        TEXT NOT NULL,         -- 'human' | 'qwen' | 'opus' | 'claude'
  rubric_version TEXT NOT NULL,
  text           TEXT NOT NULL DEFAULT '',
  certainty      REAL,                  -- model labelers only (0..1); NULL for human rows
  created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_labels_chunk   ON labels(chunk_id);
CREATE INDEX IF NOT EXISTS idx_labels_labeler ON labels(labeler);
CREATE TABLE IF NOT EXISTS spans (
  label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
  span     TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS latest AS
  SELECT * FROM labels WHERE id IN (
    SELECT max(id) FROM labels GROUP BY chunk_id, labeler);
-- The training-set view: HUMAN overrides any model labeler for the same chunk. Precedence is by
-- labeler class, then recency; certainty rides along so training can weight by it.
CREATE VIEW IF NOT EXISTS effective AS
  SELECT * FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY chunk_id
        ORDER BY (labeler = 'human') DESC, id DESC) AS _rn
    FROM latest)
  WHERE _rn = 1;
"""


def _migrate(conn) -> None:
    """Idempotent column adds for DBs created before a schema change (SQLite has no IF NOT EXISTS
    for columns). The effective view depends on nothing new, so CREATE VIEW IF NOT EXISTS covers it."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(labels)")}
    if "certainty" not in cols:
        conn.execute("ALTER TABLE labels ADD COLUMN certainty REAL")
        conn.commit()


def rubric_version() -> str:
    """The version stated in RUBRIC.md — stored on every row so a label is traceable to the
    definition it was made under."""
    text = (Path(__file__).parent / "RUBRIC.md").read_text(encoding="utf-8")
    m = re.search(r"\*\*Version ([0-9.]+)", text)
    return m.group(1) if m else "unknown"


def connect(db: Path = DEFAULT_DB) -> sqlite3.Connection:
    # check_same_thread=False: the labeling UI serves endpoints from uvicorn's threadpool, so the
    # one connection is touched from several threads. Access is sequential (single labeler, one
    # request at a time) and every write commits immediately, so this is safe here.
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.row_factory = sqlite3.Row
    return conn


def add_label(conn, *, chunk_id: str, label: str, labeler: str, kb_id: str = "", doc_id: str = "",
              docnm: str = "", note: str = "", nominated: str = "", text: str = "",
              certainty: float | None = None, spans: list[str] | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO labels (chunk_id, kb_id, doc_id, docnm, label, note, nominated, labeler,"
        " rubric_version, text, certainty) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (chunk_id, kb_id, doc_id, docnm, label, note, nominated, labeler, rubric_version(), text,
         certainty))
    for s in spans or []:
        conn.execute("INSERT INTO spans (label_id, span) VALUES (?,?)", (cur.lastrowid, s))
    conn.commit()
    return cur.lastrowid


def labeled_ids(conn, labeler: str) -> set[str]:
    return {r["chunk_id"] for r in
            conn.execute("SELECT chunk_id FROM latest WHERE labeler = ?", (labeler,))}


def stats(conn) -> dict:
    out = {}
    for r in conn.execute("SELECT labeler, label, count(*) n FROM latest GROUP BY 1,2"):
        out.setdefault(r["labeler"], {})[r["label"]] = r["n"]
    notes = {r["labeler"]: r["n"] for r in conn.execute(
        "SELECT labeler, count(*) n FROM latest WHERE note != '' GROUP BY 1")}
    spans_n = {r["labeler"]: r["n"] for r in conn.execute(
        "SELECT l.labeler, count(*) n FROM spans s JOIN latest l ON l.id = s.label_id GROUP BY 1")}
    return {"by_labeler": out, "with_note": notes, "spans": spans_n}


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    db = DEFAULT_DB
    if "--db" in sys.argv:
        db = Path(sys.argv[sys.argv.index("--db") + 1])
    conn = connect(db)
    if cmd == "stats":
        print(json.dumps(stats(conn), indent=2, ensure_ascii=False))
    elif cmd == "export":
        labeler = None
        if "--labeler" in sys.argv:
            labeler = sys.argv[sys.argv.index("--labeler") + 1]
        q = "SELECT * FROM latest" + (" WHERE labeler = ?" if labeler else "")
        for r in conn.execute(q, (labeler,) if labeler else ()):
            row = dict(r)
            row["spans"] = [s["span"] for s in conn.execute(
                "SELECT span FROM spans WHERE label_id = ?", (r["id"],))]
            print(json.dumps(row, ensure_ascii=False))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
