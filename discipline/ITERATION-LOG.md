# DISCIPLINE iteration log

Autonomous tuning of the qwen DISCIPLINE prompt against the EVAL.md suites (A/B/C/D), via
`python3 eval-agent.py --tournament` (pre-approved). Each variant is a `discipline/*.txt` file
appended to the production prompt through qwen.sh's `ORACLE_DISCIPLINE_EXTRA` hook. Nothing here is
committed without the user; these are on-disk working versions.

## Baseline references
- **Suite A (pre-tournament):** 4/4 (A1/A2/A4 pass, A3 lenient). No grounding decay. A4 = 19/19 real WAL codes.
- **Suite B (pre read-source tweak):** 0/4 — synthesized generic answers, B2 never read source.

## Variants
- `v1-antidecay.txt` — re-ground every turn; reproduce enumerations verbatim (incl. across translation); honest tool naming.
- `v2-full.txt` — v1 + do not agree to a false premise (no C++21); match label/rank to source (Rodentia ≠ Muridae); answer the exact question asked.

## Runs
| when | variant | A | B | C | D | total | notes |
|---|---|---|---|---|---|---|---|
| 2026-07-18 07:15 | baseline | 4/4 | 0/4 | 3/5 | 2/3 | 9/16 | |
| 2026-07-18 07:15 | v1-antidecay | 3/4 | 0/4 | 3/5 | 3/3 | 9/16 | A4 scored 0/19 (search noise) |
| 2026-07-18 07:15 | v2-full | 4/4 | 0/4 | 4/5 | 2/3 | **10/16** | nominal winner — but see below |

## ⚠️ STOP — round 1 found the BENCHMARK is not trustworthy (do NOT keep iterating on it)

The ranking (10 vs 9 vs 9) is within noise AND contaminated. Three findings, all verified:

1. **CONTAMINATION (fatal for C/D).** qwen's `source_search`/`read_lines` reads **`EVAL.md` itself** —
   the answer key — plus `TODO.md`/`FINDINGS.md`, from its `~/Projects` search path. EVAL.md's B/C/D
   sections contain the expected answers (kJson=114, JsonBinCore, the marmot/muskrat translations, the
   shared_ptr-container note). So C/D "passes" may be the model reading the test. Verified: ≥5 tournament
   sessions read EVAL.md. (Dark confirmation: B still scored 0/4 even with the answers readable — the
   model is too weak to use the leaked key. So B is genuinely model-bound; C/D grading is inflated.)
2. **GRADER FALSE-POSITIVE.** C5's trap regex `c\+\+17...(no new)` matched a CORRECT answer
   ("no new *classes*, but `shared_ptr<T[]>` and `weak_from_this` were added"). The rubric fails right
   answers. Rubric lives in the FROZEN `eval-agent.py` — do NOT edit it unilaterally; it is a user
   decision (changing it resets the baseline).
3. **HIGH RUN-TO-RUN VARIANCE.** A4: 19/19 → 0/19 → 19/19 across variants (same tools, search-path
   noise). D3: full list → generic non-answer. One run per cell cannot rank variants; need N runs + median.

**B is model-bound:** reads source 16–23× and still fails; a prompt tweak (incl. the read-source
mandate now in baseline) does NOT move it off 0/4. This is the case for `qwen-next`, not more prompt.

### Decision: PAUSED, awaiting the user. Fixes needed BEFORE any more iteration (all touch things I may
not change autonomously):
- **De-contaminate:** keep EVAL.md/TODO.md/FINDINGS.md (and the answer key generally) out of qwen's
  searchable tree during eval — run in a cwd without them, or exclude via the source tool. (Harness/cwd
  = `eval-agent.py`, frozen.)
- **Fix the C5 rubric** to distinguish "no new *classes*" (pass) from "nothing in C++17" (fail).
  (Frozen `eval-agent.py`.)
- **Add repeats** (N=3, median) to beat variance. (Harness change.)
Until these are done, prompt iteration is measuring noise + leakage. Not proceeding.
