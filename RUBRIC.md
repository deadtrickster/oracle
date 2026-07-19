# Chunk-labeling rubric — the fixed definition of junk

**Version 1.1 (2026-07-19).** This document is the *single source of truth* for what each label
means. Every grader — the qwen weak-labeler (`label-junk.py` injects this file verbatim into its
prompt), Claude as blind auditor, and the human adjudicator — applies **this** definition, not their
own. A grader is not a measuring instrument; a written rubric is. If graders disagree, the bug is in
this document: amend it, bump the version, note it in the changelog, relabel what the amendment
touches.

## Why each rule is written down

Labels train the junk classifier (TODO G3.8). A wobbling definition trains the wobble into the
model. The three defenses (per the labeling-best-practices review, 2026-07-19):
1. **Fixed rubric + worked examples sent with every batch** — graders apply one definition.
2. **Blind spot-check:** before training, a second grader (Claude) blind-labels ≥100 of qwen's
   chunks. Agreement ≥95% → trust the batch. Below → stop, fix the rubric, relabel.
3. **Disagreements are rubric bugs**, not noise. Each one is adjudicated by the human, and the
   resolution becomes a rule or example below.

## The classes and their actions

| label | action in cleanup |
|---|---|
| CLEAN | keep |
| TOC, INDEX, EXERCISE, BIBLIOGRAPHY, DEBRIS | delete |
| FIGURE_GARBAGE | excise garbage span, remove+reingest remainder (delete if nothing remains) |
| OCR_DAMAGED_CODE | delete (misquotable as API truth) — *pending adjudication, see Open questions* |
| BOILERPLATE | strip the repeated line, keep the chunk |

## Global rules (apply before any class rule)

- **G1 — If genuinely unclear, label CLEAN.** Deleting real knowledge is strictly worse than
  keeping a stray junk chunk.
- **G2 — Judge the chunk, not the book.** A quiz book's prose explanation is CLEAN; a reference
  manual's exercise section is EXERCISE.
- **G3 — Structure beats surface.** `?` is a PostgreSQL operator; counting question marks once
  deleted a jsonb operator table and a WAL chapter. A table of operators/functions/values is CLEAN
  no matter how many `?` it contains.
- **G4 — Precedence when several classes apply:** the class that describes the chunk's *dominant
  purpose* wins. A ToC page with OCR damage is TOC (the damage doesn't change what it is). A prose
  paragraph with an embedded shredded diagram is FIGURE_GARBAGE (that's the class whose *action*
  repairs it). An exercise list inside an index page: whichever occupies more of the chunk.
- **G5 — Language-neutral.** Rules apply to English and Russian alike (`Вопросы для повторения` is
  EXERCISE; `УДК/ББК` front-matter is BOILERPLATE).

## Class definitions

### CLEAN — real content (keep)
Prose, explanations, arguments, worked derivations; **working code from text-born sources** (md/txt
docs, cppreference); tables of operators, functions, flags, or data; math formulas — including ones
OCR salted with stray fullwidth parens or Greek; chart *legends* and figure *captions* that are
themselves readable sentences; technical prose that poses a question and answers it.
**Not CLEAN just because it looks technical:** see OCR_DAMAGED_CODE.

### TOC — table of contents (delete)
Chapter/section titles with page numbers; dotted leaders (`Balancing Resources ..... 123`); "Related
sections include: .124". Front-of-book or per-chapter. Signals: leader dots, page-number line ends,
title-case fragments, near-zero stopwords.

### INDEX — back-of-book index / glossary (delete)
Short alphabetized entries with page numbers; `term — definition` lists in glossary form. Signals:
alphabetized line starts, mostly-short lines, page numbers at line ends, definition dashes.

### EXERCISE — quiz apparatus (delete)
Numbered question runs; multiple-choice options (`a. organ  b. organelle`); fill-in-the-blank
(`________`); answer keys (`1. b  2. a`); `Вопросы для повторения`. **Trap (G3):** an operator table
is not a quiz; prose that answers its own question is CLEAN.

### BIBLIOGRAPHY — references (delete)
Citation keys (`[RYSTSOV16]`), `Author (2019)`, `et al.`, `pp. 45–67`, ISBN, bulk URLs/DOIs.
A *single* inline citation inside prose does not make the chunk BIBLIOGRAPHY (G4: dominant purpose).

### FIGURE_GARBAGE — flattened diagram OCR (excise + reingest)
A diagram/chart the layout model missed, OCR'd into the text stream: box-drawing/geometric glyphs
(`口□■●`), letter runs (`DDDDDD`), shredded non-words (`rylooku`, `arely consulted`), loose axis
numbers. Often **interleaved with legit prose in the same chunk** — that's why the action is excision,
not deletion. **Trap:** a bare list of numbers in a *data table* is CLEAN; the garbage class needs
glyph noise or shredded words, not numbers alone.

### OCR_DAMAGED_CODE — code OCR'd from page images, visibly damaged (delete, pending)
Code listings whose text came from OCR of a page image and shows damage: dropped letters
(`GridPa e` for `GridPane`), digit/letter swaps (`R0UND`), fullwidth punctuation (`，` `（）`),
scrambled line numbers. **Undamaged code is CLEAN** wherever it came from. The reason this is junk:
a model that retrieves `GridPa e` and quotes it as API truth reproduces the miscopied-value-table
failure.

### DEBRIS — meaningless fragments (delete)
A stray running head, a lone page number, a ToC line embedded as its own chunk
(`133 The nature of preferences10 reward functions 138`), a sub-15-word fragment carrying no fact.
**Trap:** a short chunk that *does* carry a fact (a definition, a constant) is CLEAN — size alone
doesn't convict.

### BOILERPLATE — per-page furniture (strip, keep chunk)
Watermarks (`Licensed to …`), running headers repeating the book title, publisher front-matter
(УДК/ББК, edition/copyright notices). The action differs from DEBRIS: boilerplate rides *inside*
otherwise-good chunks, so the line is stripped and the chunk survives.

## Span marking — the split can be INSIDE a chunk

A chunk's class alone under-specifies the repair when garbage and content share the chunk (the
FIGURE_GARBAGE case; sometimes BOILERPLATE). When labeling such a chunk in the UI, **select the
garbage text and press `g`** — each marked span is stored with the label (exact substrings, `spans`
table). Rules:

- Label = the chunk's dominant-purpose class (G4); spans = the parts the cleanup should excise.
- FIGURE_GARBAGE with spans → excise spans, keep the rest (remove+reingest). With NO spans → the
  whole chunk is garbage → delete.
- BOILERPLATE: mark the repeated line as the span.
- Human-marked spans are the gold standard the automatic excision (`find_diagram_garbage`) is
  judged against.

## Storage & provenance

Labels live in the SQLite labels DB (`label-db.py`, default `labels.db`) — append-only rows with
`labeler` (human | qwen | claude), `rubric_version` (this document's version at labeling time),
timestamps, the hidden `nominated` heuristic, and per-label spans. Latest row per (chunk, labeler)
is current. Agreement checks: `label-agreement.py <a> <b>`; export for publishing:
`label-db.py export`.

## Worked examples (real chunks, correct answers)

| chunk (abbreviated) | label |
|---|---|
| `.121 .122 Balancing Resources in Java... .123 Checking the Balance... .124 Challenges.. .124` | TOC |
| `@>(jsonb,jsonb) \| jsonb_contains \| 7    ?(jsonb,text) \| jsonb_exists \| 9` | CLEAN (G3) |
| `1GB huge pages 4KB pages backend reads a buffer 口□□□I TLB LB mis hit rylooku DDDDDD shared_buffers` | FIGURE_GARBAGE |
| `GridPa e gridPa e = ew GridPa e(); 56 gridPa e.add( ew Label("JDBC Driver")，O,O);` | OCR_DAMAGED_CODE |
| `3. The smallest unit ... is the ________.  a. organ  b. organelle  c. cell` | EXERCISE |
| `133 The nature of preferences10 reward functions 138` | DEBRIS |
| `[RYSTSOV16] Rystsov, D. et al. (2016), pp. 45-67. ISBN 978-...` | BIBLIOGRAPHY |
| `Huge pages turn shared_buffers from a per-connection page-table tax into a flat cache.` | CLEAN |
| `(22) H()\|≤ J。 \|G(,t)\|" dt≤A\|e-zlog2 l2/m\| + Bl2m\|` | CLEAN (damaged math is still the only copy of the math; G1) |

## Open questions (to adjudicate — each resolution amends this rubric)

1. **OCR_DAMAGED_CODE: delete or excise?** Delete loses the (damaged) only copy; keeping risks a
   model quoting `GridPa e` as truth. Current lean: delete. *Human call.*
2. **Damaged math** (HyperLogLog-style formulas with stray CJK): currently CLEAN per G1 — the math
  is often the chunk's whole value and no clean copy exists. Revisit if retrieval surfaces them
  badly.
3. **Code snippet with no prose around it** (clean, but context-free): currently CLEAN. Is a bare
   30-line listing with no explanation worth retrieval slots? *Human call.*

## Changelog

- **1.1 (2026-07-19)** — span marking (garbage portions within a chunk are selectable; spans stored
  with the label; FIGURE_GARBAGE action clarified: spans→excise, no-spans→delete); storage moved to
  the SQLite labels DB with labeler + rubric-version provenance.
- **1.0 (2026-07-19)** — initial version: 9 classes, global rules G1–G5, worked examples from the
  observed corpus (jsonb scar, ClickHouse diagram, Liang OCR-code, Pragmatic-Programmer ToC).
