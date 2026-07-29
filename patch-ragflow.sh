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
#
# ── PATCH 3: WORKERS=1 is not a default, it is a bottleneck ──────────────────────────────────────
#
# RAGFlow ships one task executor. On this machine that left the CPU idle while a 10,000-document
# queue crawled: 25 documents/minute with cores to spare. Raising it to 8 gave 95 docs/min — 3.8x,
# not 8x, because the ceiling moved rather than vanished (GPU embedding went to ~70%, and SereneDB
# to ~3 cores sustained with compaction bursts to 5.5).
#
# This one lives in the VENDORED docker/docker-compose.yml rather than inside the container, which
# makes it MORE fragile, not less: a re-checkout of ragflow/ silently reverts it and throughput
# quietly drops back to 25/min with nothing in any log to say why. That already happened once with
# DOC_ENGINE. So it is checked here with the others.
set -uo pipefail

CONTAINER="${RAGFLOW_CONTAINER:-docker-ragflow-cpu-1}"
PDF_PARSER="/ragflow/deepdoc/parser/pdf_parser.py"
COMPOSE="${RAGFLOW_COMPOSE:-$HOME/Projects/oracle/ragflow/docker/docker-compose.yml}"
WORKERS="${RAGFLOW_WORKERS:-8}"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say() { printf '%s\n' "$*"; }

# Patch 3 first: it is a host-file check, so it is worth reporting even when the stack is down —
# which is exactly when someone is recreating containers and about to lose it.
if grep -qE '^\s*-\s*--workers=' "$COMPOSE" 2>/dev/null; then
	say "patch 3 (task executor --workers): present — $(grep -oE '\--workers=[0-9]+' "$COMPOSE" | head -1)"
elif [ ! -f "$COMPOSE" ]; then
	say "patch 3 (task executor --workers): compose file not found at $COMPOSE"
else
	say "patch 3 (task executor --workers): MISSING — throughput will be ~1/4 of measured"
	say "  add '- --workers=$WORKERS' to the ragflow-cpu command block in:"
	say "  $COMPOSE"
fi

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
	say "container $CONTAINER is not running — start the stack first"
	exit 1
fi

# Patch 1 is now a HOST file plus a bind mount, not an in-container sed.
#
# It used to be a sed inside the container, and that quietly failed: adding `--workers=8` to the
# compose file recreated the container, the image's pristine token_utils.py came back, and parsing
# ran unpatched for hours with nothing in any log saying so. Patch 2 sat in the same container and
# survived — because pdf_parser.py is bind-mounted from the repo and token_utils.py was not. That
# is the whole difference, so the fix is to mount this file too rather than to remember to re-run
# a script after every recreate.
#
# Both encode() calls must carry the flag: the counting one so token counts are right, the
# truncating one so it stops throwing.
HOST_TOKENS="$(dirname "$COMPOSE")/../common/token_utils.py"
sites="$(grep -c 'disallowed_special=()' "$HOST_TOKENS" 2>/dev/null || true)"
mounted=0
grep -q 'common/token_utils.py:/ragflow/common/token_utils.py' "$COMPOSE" 2>/dev/null && mounted=1

if [ "${sites:-0}" -ge 2 ] && [ "$mounted" -eq 1 ]; then
	say "patch 1 (tiktoken disallowed_special): applied on the host and mounted"
else
	say "patch 1 (tiktoken disallowed_special): NOT durable"
	[ "${sites:-0}" -ge 2 ] || say "  $HOST_TOKENS has ${sites:-0}/2 patched call sites"
	[ "$mounted" -eq 1 ] || say "  $COMPOSE does not mount common/token_utils.py — it will revert on recreate"
fi
[ "$CHECK_ONLY" -eq 1 ] && exit 0

# Verify by RUNNING it, not by re-grepping the file. A grep confirms a string is present; only the
# interpreter confirms the module still parses, that the mount actually reached the container, and
# that the string which caused the outage now encodes. That distinction is the point here: the grep
# above passed for weeks while the running process had the unpatched code loaded.
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
	# Deliberately no rollback. The file is now a bind mount of a version-controlled repo file, so
	# "restoring the original" would write through to the host and destroy the fix rather than undo
	# a bad edit. `git checkout` is the correct undo, and it belongs to a human.
	say "PATCH 1 FAILED VERIFICATION"
	say "  the running container does not have the fix loaded — check the mount, then restart:"
	say "  docker restart $CONTAINER   (a running process keeps the module it imported at start)"
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
