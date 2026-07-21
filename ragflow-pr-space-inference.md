# Upstream PR draft — infiniflow/ragflow

**STATUS (2026-07-21): both submitted.**
PR 1 → [infiniflow/ragflow#16958](https://github.com/infiniflow/ragflow/pull/16958) — **MERGED**.
PR 2 → [infiniflow/ragflow#16959](https://github.com/infiniflow/ragflow/pull/16959) — open
(closes upstream #12109).

**Checked against `origin/main` (51 commits ahead of our `v0.26.4` pin, as of 2026-07-14): both bugs
are still present, unfixed — in BOTH the Python and the Go implementation.**

## Scope — two PRs, each touching Python and Go

### PR 1 — word boundaries + OCR-fallback guard
| file | change |
|---|---|
| py `deepdoc/parser/pdf_parser.py` | gap-based space insertion in `__ocr` (absent today); OCR-alphabet guard |
| go `internal/deepdoc/parser/pdf/layout/chars_boxes.go` | remove the `asciiWordPattern` gate; fix the threshold (`gap >= min(width)/2` under-inserts even for English) |
| go `internal/deepdoc/parser/pdf/util/garbled.go` + caller | same OCR-alphabet guard — `IsGarbledPage()` currently triggers an OCR pass for scripts the recogniser cannot spell |

### PR 2 — `chunk_token_num` is ignored (**closes #12109**)
| file | change |
|---|---|
| py `rag/app/book.py` | honour `chunk_token_num`; fix `split("@")` → `split("@@")`, which silently discards page positions |
| py `rag/app/paper.py` | honour `chunk_token_num` — **this is the reporter's own case in #12109** (chunks too BIG, breaking a 2048-token reranker) |
| go `internal/ingestion/component/chunker/hierarchy.go` | honour it — **the Go pipeline never reads it at all** |

The Go finding is the one that should land PR 2: `chunk_token_num: 512` exists **only** in
`internal/common/parser_config.go` defaults and in tests. No chunker in `internal/ingestion` or
`internal/parser` ever consumes it. It is a config knob wired to nothing.

**One defect, two opposite symptoms:** `book.py` produces chunks that are far too SMALL (median 47
chars — page numbers and TOC lines indexed as passages); `paper.py` produces chunks that are too BIG
(the reported bug). Neither reads the setting.

**Prior art search:**
- No issue or PR mentions the space-glyph / word-welding problem. **This is novel.**
- **Issue #12109 is OPEN and is the same root cause as PR 2** — *"`chunk_token_num` is ignored by
  chunk policies"*. Their symptom is the opposite of ours (chunks too **big**, breaking a reranker's
  2048-token input, via `paper.py`'s section chunking); ours is chunks too **small** (`book.py`'s
  `hierarchical_merge`). Same defect: **the chunk policies never read `chunk_token_num`.** PR 2
  should reference it.
- PR #13234 (merged) fixed `chunk_token_num` only for MinerU/docling/paddleocr — not for the
  `hierarchical_merge` path that every real book takes.
- **PR #16323 (merged 2026-06-25) migrated `pdf_parser.py` to Go.** Both implementations now ship.
  See the note in PR 1 — the Go port has the *same* bug, in a more explicit form.

---

## PR 1 — `deepdoc`: infer word boundaries from character geometry when a PDF has no space glyphs

### Summary

Many PDFs encode **no space glyphs at all** — words are separated purely by horizontal positioning.
DeepDoc only emits a space when `pdfplumber` reports a literal `" "` character, so the extracted text
comes out welded into a single token. This forces an unnecessary OCR pass on Latin documents, and
**destroys non-Latin documents entirely**.

This is not an exotic corner case. In a 16-book technical library, **8 books are affected** — every
TeX-family document (pdfTeX, LuaTeX, xdvipdfmx) plus one produced by iText.

### The cause, from the PDF itself

pdfTeX does not emit spaces as characters. In TeX, interword space is **glue** (a stretchable skip
used for justification), not a character — so it is written as a *position adjustment* in the `TJ`
operator. From SLP3's content stream:

```
[(Summary)-250(of)-250(Contents)] TJ
[(I)-1000(Lar)10(ge)-250(Language)-250(Models)] TJ
```

The words are separate strings; `-250` (thousandths of an em) **is** the space. Note `(Lar)10(ge)` —
intra-word kerning uses the identical mechanism, one order of magnitude smaller. That is exactly why
a geometric threshold separates them cleanly.

### Reproduction (all files freely downloadable)

```python
import pdfplumber
chars = pdfplumber.open("ed3book.pdf").pages[40].chars
print(sum(1 for c in chars if c["text"] == " "), "/", len(chars))   # -> 0 / 2959
```

| file | producer | space glyphs |
|---|---|---|
| [SLP3, Jurafsky & Martin](https://web.stanford.edu/~jurafsky/slp3/ed3book.pdf) | pdfTeX-1.40.21 | **0.0%** |
| [Sutton & Barto, *RL: An Introduction*](http://incompleteideas.net/book/RLbook2020.pdf) | LaTeX | **0.0%** |
| [Rogov, *PostgreSQL 18 Internals*](https://postgrespro.com/education/books/internals) (ru) | LuaTeX-1.17.0 | **0.0%** |
| [Dive into Deep Learning](https://d2l.ai/d2l-en.pdf) | xdvipdfmx | **0.0%** |
| Designing Data-Intensive Applications | Antenna House | 14.6% — fine |
| Database Internals | Adobe PDF Library | 14.0% — fine |

(Normal English prose is ~15% spaces. Affected files sit at 0.0–0.4%.)

### What goes wrong

`RAGFlowPdfParser.__ocr()` builds the box text like this:

```python
for c in Recognizer.sort_Y_firstly(box_chars, m_ht):
    if c["text"] == " " and b["text"]:
        ...
        b["text"] += " "
    else:
        b["text"] += c["text"]      # no space glyph -> characters simply concatenate
```

Result:

```
SLP3 p.41   ->  2.9•MINIMUMEDITDISTANCE33substitutionshaveacostof2(exceptsubstitutionofidentical
PG18 p.21   ->  Окнигекак-тоиначе.Такиепометкимогутоказатьсяполезнымидлятех,ктоещенеобновил
```

**Consequence 1 — needless OCR over a perfectly good text layer.** The welded text trips the existing
garbled-text heuristics, `b["text"]` is cleared, and the page is re-OCR'd. For Latin scripts this
*works*, so the bug is invisible — but it pays a full OCR pass (the dominant cost of ingestion) for
pages whose text was already exact, and it injects OCR artifacts into text that wasn't.

**Consequence 2 — non-Latin documents are destroyed.** `rag/res/deepdoc/ocr.res` (the recognition
dictionary) contains **6270 CJK characters, 52 Latin, and 6 Cyrillic**. When the fallback fires on a
Russian document, the recogniser cannot represent the alphabet and the output is garbage.

Worth being explicit, because it is easy to misdiagnose: **the encoding is not the problem.**
`pdfplumber` extracts the Cyrillic perfectly — 1327 Cyrillic characters and **0 PUA/unmapped
characters** on the page tested. The characters were always correct. Only the word boundaries were
missing — and losing them also destroys tokenisation, so lexical/BM25 matching cannot fire on the
document at all.

### The Go port has the same bug — and states it explicitly

`internal/deepdoc/parser/pdf/layout/chars_boxes.go` **does** implement the gap heuristic:

```go
// Insert space between adjacent ASCII words with a visible gap.
gap := c.X0 - prev.X1
minWidth := math.Min(c.X1-c.X0, prev.X1-prev.X0)
if gap >= minWidth/2 && asciiWordPattern.MatchString(prevText+currText) {
    textParts = append(textParts, " ")
}

var asciiWordPattern = regexp.MustCompile(`^[0-9a-zA-Z,.:;!%]+$`)
```

Two problems:

1. **It is ASCII-gated.** Cyrillic and every other non-Latin script is excluded by construction, so
   they stay welded. Note that the Python code's own space regex — a few lines from the bug —
   *already includes* Cyrillic: `r"[0-9a-zA-Zа-яА-Я,.?;:!%%]"`. The Go port ported that character
   class and **dropped the `а-яА-Я`**. A gap is a gap regardless of script.

2. **The threshold `gap >= min(width)/2` under-inserts, even for English.** On these fonts it works
   out to ~2.5pt, while actual word gaps are 1.6–2.4pt — just below it.

### Fix

Derive the boundary from geometry, which `pdfplumber` already provides on every char (`x0`, `x1`,
`top`) — i.e. what `pdftotext` has always done. Measured on PG18 Internals (mean char width 5.22pt):
gaps *within* a word are ~0.0pt; gaps *between* words are 1.6–2.4pt — an order of magnitude apart,
so they separate cleanly.

**Threshold validated against `pdftotext`'s word segmentation** (reference: RU 253 words, EN 442
words on the sampled pages):

| rule | RU words (ref 253) | EN words (ref 442) |
|---|---|---|
| ragflow's `gap >= min(width)/2` | **150** ❌ | 416 |
| `gap > 0.20 × mean_width` | 259 | **442** ✅ |
| **`gap > 0.25 × mean_width`** | **258** (−2.0%) | **440** (−0.5%) |
| `gap > 0.30 × mean_width` | **253** ✅ | 439 |

`0.25 × mean char width` is within 2% of the reference on both scripts. The existing rule recovers
150 of 253 Russian words.

+16 lines, no new dependencies, language-agnostic:

```python
m_wd = np.mean([c["x1"] - c["x0"] for c in box_chars if c["text"].strip()] or [0])
space_gap = m_wd * 0.2
prev = None

for c in Recognizer.sort_Y_firstly(box_chars, m_ht):
    if c["text"] == " " and b["text"]:
        ...
    else:
        if prev is not None and b["text"] and not b["text"].endswith(" "):
            same_line = abs(c["top"] - prev["top"]) < m_ht / 2
            # a line break is always a word boundary; on one line, a visible gap is
            if not same_line or (space_gap > 0 and c["x0"] - prev["x1"] > space_gap):
                b["text"] += " "
        b["text"] += c["text"]
        ...
    prev = c
```

### Result

```
SLP3    before:  2.9•MINIMUMEDITDISTANCE33substitutionshaveacostof2(exceptsubstitutionofidentical
        after:   2.9 • MINIMUM EDIT DISTANCE 33 substitutions have a cost of 2 (except substitution of identical

PG18    before:  Окнигекак-тоиначе.Такиепометкимогутоказатьсяполезнымидлятех,ктоещенеобновил
        after:   О книге как-то иначе. Такие пометки могут оказаться полезными для тех, кто еще не обновил
```

Both now come straight from the text layer, with no OCR pass at all.

### Companion guard: don't fall back to an OCR that cannot spell the script

Even with word boundaries fixed, the other garbled-text strategies (PUA/CID, font-encoding) can still
fire and clear `b["text"]`, handing the page to an OCR model that **cannot represent the script**.

`rag/res/deepdoc/ocr.res` — the recognition alphabet — holds **6270 CJK, 52 Latin, 6 Cyrillic**
characters. Measured coverage of the extracted text:

| page | chars present in the OCR alphabet |
|---|---|
| English (SLP3) | **99.0%** |
| Russian (PG18 Internals) | **19.8%** |

So on a Russian page the fallback discards a usable text layer in favour of a model that can only
spell one character in five. The result is guaranteed garbage — and this, not the font encoding, is
the true origin of "DeepDoc garbles Cyrillic".

The guard is cheaper than language identification, needs no language list, and **self-corrects** if
`ocr.res` is ever replaced with a multilingual model:

```python
if total_count > 0 and not self._ocr_can_represent(b["text"]):
    continue          # keep the extracted text; OCR cannot improve on it
```

where `_ocr_can_represent()` simply checks that ≥80% of the text's characters exist in `ocr.res`.
(RAGFlow already samples a small window to detect script — `is_english(random_choices(..., k=200))` —
but only to tune a table-of-contents heuristic, never before this fallback.)

---

## PR 2 — `rag/app/book.py`: `chunk_token_num` is ignored, and page positions are dropped

Independent of PR 1; send separately.

**Bug A — `chunk_token_num` is never read.** `book.chunk()` takes `hierarchical_merge` whenever a
bullet/heading pattern is detected — i.e. for every real book — and that path never consults
`chunk_token_num`. It accumulates against a **hardcoded 218-token** limit and only merges *singleton*
groups; anything else is emitted as-is. Since DeepDoc classifies TOC lines and running heads as
sections, each becomes its own chunk.

Measured with `chunk_token_num=512` on both parsers:

| parser | median chunk | chunks < 50 chars |
|---|---|---|
| `naive` | 1168 chars | ~0% |
| `book` | **47 chars** | **51%** |

Half of a 500-chunk sample of SLP3 was under 50 characters — page numbers and TOC lines, embedded and
indexed as if they were passages. A representative chunk, in full:

```
133 The nature of preferences10 reward functions 138
```

**Bug B — the `naive_merge` branch discards page positions.** The position tag is
`@@page\tx0\tx1\ttop\tbottom##` — a **double** at-sign — and the code splits on a **single** `@`:

```python
"foo@@1\t2\t3\t4\t5##".split("@")   ->   ["foo", "", "1\t2\t3\t4\t5##"]     # three parts, not two
```

so `len(pr) == 2` is false, the `else` fires, and the tag is dropped. Chunks come out with
`positions = []`. (This is presumably *why* nobody noticed the branch was broken — it was already
dead code for any document with headings.)

**Fix:** take `naive_merge` when `chunk_token_num` is set, and split on `"@@"`. Measured on one book:

| | before | after |
|---|---|---|
| chunks | 637 | 66 |
| median chars | 47 | **2302** |
| < 50 chars | 51% | **0%** |
| with page positions | 0/66 | **66/66** |
