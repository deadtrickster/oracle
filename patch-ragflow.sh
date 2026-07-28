#!/usr/bin/env bash
# patch-ragflow.sh — reapply our fixes inside the pinned RAGFlow container.
#
# RAGFlow is pinned to v0.26.4 (upgrading has broken this stack before), so fixes cannot come from
# a version bump. Editing files inside the container works and survives `docker restart`, but is
# LOST on `docker compose down`/`up` or any image pull — at which point ingestion quietly starts
# failing again in exactly the same way, weeks later, with nobody remembering why it used to work.
#
# So the patches live here: idempotent, re-runnable, and each one carrying the evidence for why it
# exists. Run this after any container recreate.
#
#   ./patch-ragflow.sh          apply (idempotent)
#   ./patch-ragflow.sh --check  report what is/isn't applied, change nothing
#
# ── PATCH 1: tiktoken refuses to encode `<|endoftext|>` ──────────────────────────────────────────
#
# Found by diagnosing 3 stuck documents out of 15,017. Five page-tasks had failed with:
#
#   Encountered text corresponding to disallowed special token '<|endoftext|>'.
#
# tiktoken refuses, by default, to encode text containing a special-token string — a sensible guard
# against text smuggling control tokens into a prompt. But we are COUNTING and TRUNCATING here, not
# building a prompt, so the guard protects nothing and only rejects the text.
#
# It bit two books about language models. Books that explain `<|endoftext|>` naturally print it, so
# this is not an exotic input for this corpus — it is the corpus's own subject matter. Deterministic:
# it fails identically on every retry, and on any future document that quotes a special token.
#
# RAGFlow already has the guard in one of the two call sites, which is what makes the failure look
# arbitrary: num_tokens_from_string() wraps encode() in try/except (and silently returns 0, so a
# quoted special token also makes a chunk look zero-length), while truncate() calls it bare and
# throws. Passing `disallowed_special=()` at both sites makes them agree and treats these strings as
# what they are here: ordinary text.
# ── PATCH 2: a NUL in the text layer must trigger OCR, not kill the document ─────────────────────
#
# The remaining stuck document failed with:
#
#   Insert chunk error: ['A string literal cannot contain NUL (0x00) characters.']
#
# A NUL cannot come from real text. It means that page's embedded text layer is damaged, so the
# characters pdfplumber handed back are not what the page says — and by the time the doc store
# rejects them, a whole page-task is dead.
#
# The tempting fix is to strip the NULs and carry on. That is wrong, and the reason is the point:
# stripping keeps whatever surrounded the NUL, which came from the same broken extraction, and
# silently indexes it as if it were the page. The corpus then contains confident nonsense that
# retrieval will happily cite — the exact failure this project exists to prevent. A page we cannot
# read must be re-read, not tidied up.
#
# RAGFlow already does precisely that for two other kinds of damage: `_is_garbled_text` (PUA/CID)
# and `_is_garbled_by_font_encoding` (subset fonts mapping CJK to ASCII). Both respond by setting
# `self.page_chars[pi] = []`, which makes the OCR path supply the text instead. So this patch adds
# no new machinery — it teaches an existing, proven fallback to recognise one more symptom, and a
# NUL is the least ambiguous symptom of the three.
set -uo pipefail

CONTAINER="${RAGFLOW_CONTAINER:-docker-ragflow-cpu-1}"
TARGET="/ragflow/common/token_utils.py"
PDF_PARSER="/ragflow/deepdoc/parser/pdf_parser.py"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say() { printf '%s\n' "$*"; }

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
	say "container $CONTAINER is not running — start the stack first"
	exit 1
fi

# Count call sites that still lack the flag. Both encode() calls in this file must carry it: the
# counting one so token counts are right, the truncating one so it stops throwing.
unpatched="$(docker exec "$CONTAINER" bash -c \
	"grep -c 'encoder\.encode(string)' '$TARGET' || true" 2>/dev/null | tr -d '\r')"
unpatched="${unpatched:-0}"

if [ "$unpatched" -eq 0 ]; then
	say "patch 1 (tiktoken disallowed_special): already applied"
	[ "$CHECK_ONLY" -eq 1 ] && exit 0
else
	say "patch 1 (tiktoken disallowed_special): $unpatched call site(s) to fix"
	if [ "$CHECK_ONLY" -eq 1 ]; then exit 0; fi
	# Back up once — the first run captures the pristine file, later runs must not overwrite that
	# backup with an already-patched copy.
	docker exec "$CONTAINER" bash -c \
		"[ -f '$TARGET.orig' ] || cp '$TARGET' '$TARGET.orig'"
	docker exec "$CONTAINER" bash -c \
		"sed -i 's/encoder\.encode(string)/encoder.encode(string, disallowed_special=())/g' '$TARGET'"
fi

# Verify by RUNNING it, not by re-grepping what we just wrote. A sed that matched nothing and a sed
# that worked look identical to grep; only the interpreter knows whether the file still parses and
# whether the string that caused the outage now encodes.
say "verifying inside the container…"
if docker exec "$CONTAINER" /ragflow/.venv/bin/python -c "
import sys
sys.path.insert(0, '/ragflow')
from common.token_utils import num_tokens_from_string, truncate
probe = 'a book about tokens prints <|endoftext|> in its text'
n = num_tokens_from_string(probe)
t = truncate(probe, 8)
assert n > 0, 'num_tokens_from_string returned 0 — still swallowing the error'
assert isinstance(t, str), 'truncate did not return a string'
print(f'  ok: counted {n} tokens and truncated to {t!r}')
" 2>&1; then
	say "patch 1 verified"
else
	say "PATCH 1 FAILED VERIFICATION — restoring the original"
	docker exec "$CONTAINER" bash -c "[ -f '$TARGET.orig' ] && cp '$TARGET.orig' '$TARGET'"
	exit 1
fi

# ── patch 2 ──────────────────────────────────────────────────────────────────────────────────────
MARK="oracle-nul-ocr-fallback"
if docker exec "$CONTAINER" grep -q "$MARK" "$PDF_PARSER" 2>/dev/null; then
	say "patch 2 (NUL -> OCR fallback): already applied"
else
	say "patch 2 (NUL -> OCR fallback): applying"
	if [ "$CHECK_ONLY" -eq 1 ]; then exit 0; fi
	docker exec "$CONTAINER" bash -c "[ -f '$PDF_PARSER.orig' ] || cp '$PDF_PARSER' '$PDF_PARSER.orig'"
	# Insert BEFORE the existing "Strategy 1" comment, so the cheapest and most certain test runs
	# first: the other two sample 200 chars and reason about ratios, this one is a substring check
	# on a condition that has exactly one meaning.
	# -i is required: without it docker exec does not forward stdin, so the heredoc below never
	# arrives and the edit silently does nothing (caught once by the verify step, which is why the
	# verify re-reads the file rather than trusting this command's exit status).
	docker exec -i "$CONTAINER" /ragflow/.venv/bin/python - "$PDF_PARSER" <<'PY'
import sys, re
path = sys.argv[1]
src = open(path, encoding="utf-8").read()
anchor = "                        # Strategy 1: PUA / CID garbling\n"
if anchor not in src:
    print("  anchor not found — RAGFlow changed; patch NOT applied")
    sys.exit(2)
patch = (
    "                        # Strategy 0 (oracle-nul-ocr-fallback): a NUL byte in the extracted\n"
    "                        # text layer. Real text never contains one, so this page's embedded\n"
    "                        # text is damaged and everything around the NUL came from the same\n"
    "                        # broken extraction. Clear it and let OCR read the page: stripping the\n"
    "                        # NUL would index the surrounding nonsense as if it were the page.\n"
    "                        if any('\\x00' in (c.get('text') or '') for c in page_ch):\n"
    "                            logging.warning(\n"
    "                                'Page %d: NUL byte in the text layer (%d chars), clearing to use OCR fallback.',\n"
    "                                page_from + pi + 1, len(page_ch))\n"
    "                            self.page_chars[pi] = []\n"
    "                            continue\n"
)
open(path, "w", encoding="utf-8").write(src.replace(anchor, patch + anchor, 1))
print("  inserted NUL check ahead of the existing strategies")
PY
fi

say "verifying patch 2…"
if docker exec "$CONTAINER" /ragflow/.venv/bin/python -c "
import ast, sys
src = open('$PDF_PARSER', encoding='utf-8').read()
ast.parse(src)                      # the file must still be importable Python
assert '$MARK' in src, 'marker missing'
# And the check must actually fire on the shape of data pdfplumber produces.
page_ch = [{'text': 'a'}, {'text': '\x00'}, {'text': 'b'}]
assert any('\x00' in (c.get('text') or '') for c in page_ch), 'NUL predicate does not match'
clean = [{'text': 'a'}, {'text': 'b'}]
assert not any('\x00' in (c.get('text') or '') for c in clean), 'NUL predicate fires on clean text'
print('  ok: parses, marker present, predicate matches NUL and only NUL')
" 2>&1; then
	say "patch 2 verified"
else
	say "PATCH 2 FAILED VERIFICATION — restoring the original"
	docker exec "$CONTAINER" bash -c "[ -f '$PDF_PARSER.orig' ] && cp '$PDF_PARSER.orig' '$PDF_PARSER'"
	exit 1
fi

say
say "restart the executor so it picks the change up:  docker restart $CONTAINER"
