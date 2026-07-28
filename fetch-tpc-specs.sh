#!/usr/bin/env bash
# fetch-tpc-specs.sh — download the current TPC benchmark specifications into the corpus.
#
# TPC publishes every specification as a free PDF; there is no login and no paywall. The index page
# is regenerated whenever a spec is revised (the version is in the filename — tpc-c_v5.11.0.pdf), so
# this scrapes the index rather than hardcoding URLs: hardcoded ones rot silently into 404s, and a
# corpus that quietly stops updating is worse than one that fails loudly.
#
# Idempotent: an already-downloaded file of the same version is skipped, so re-running after a TPC
# revision fetches only what changed.
#
#   ./fetch-tpc-specs.sh            benchmark + common specs (the default)
#   ./fetch-tpc-specs.sh --all      also the governance documents (bylaws, membership, procedures)
#   ./fetch-tpc-specs.sh --list     show what would be fetched, download nothing
#
# WHAT IS SKIPPED BY DEFAULT, and why: the TPC index mixes benchmark specifications with the
# organisation's governance documents. Bylaws, the contributor licence, membership rules and meeting
# procedures are about running the consortium, not about running a benchmark. In a corpus they are
# pure dilution — they answer no technical question, and retrieval that surfaces them for "what does
# TPC-C say about..." has spent a slot on nothing (Axiom 1). `--all` includes them if you disagree.
set -uo pipefail

INDEX="https://www.tpc.org/tpc_documents_current_versions/current_specifications5.asp"
DEST="${TPC_DEST:-$HOME/Documents/Books/TPC}"
# corpus/ is disposable and holds symlinks; the files themselves live in Documents.
LINKS="${TPC_LINKS:-$HOME/Projects/oracle/corpus/tpc_raw}"
UA="Mozilla/5.0 (X11; Linux x86_64) oracle-corpus/1.0"

# Governance, not specification — see the note above.
GOVERNANCE='Bylaws|CLA_|Membership|Policies|Procedures|Fair_Use|fair_use'

MODE="${1:-}"
case "$MODE" in
--all | --list | "") ;;
*)
	echo "usage: $0 [--all|--list]" >&2
	exit 2
	;;
esac

html="$(mktemp)"
trap 'rm -f "$html"' EXIT

if ! curl -sfL -m 60 -A "$UA" "$INDEX" -o "$html"; then
	echo "could not fetch the TPC index — no network, or the page moved" >&2
	exit 1
fi

# The page has TWO tables: current specifications, then — under an "Obsolete" heading near the
# bottom — the retired ones (TPC-A, TPC-B, TPC-D, TPC-R, TPC-W, TPC-App, TPC-DI, TPC-VMS). Grepping
# the whole page for .pdf pulls in all of them, and an obsolete benchmark spec in the corpus is
# actively harmful: it answers questions in the voice of a current standard while describing one
# nobody has run for twenty years.
#
# The marker is a heading, not a per-row attribute, so the split has to be POSITIONAL: take only the
# links that appear before the obsolete heading. Done in Python because that is a document-structure
# question, and sed/awk on HTML is how you get a scraper that silently returns the wrong half.
mapfile -t urls < <(
	python3 - "$html" <<'EXTRACT'
import re
import sys

html = open(sys.argv[1], errors="replace").read()
links = [(m.start(), m.group(1)) for m in
         re.finditer(r'href="(https://www\.tpc\.org/[^"]+\.pdf)"', html, re.I)]
if not links:
    sys.exit(0)
first_link = links[0][0]
# The heading that opens the obsolete table: the first "Obsolete" AFTER the first PDF link (earlier
# occurrences are navigation chrome and would truncate everything).
cut = next((m.start() for m in re.finditer(r"Obsolete", html, re.I) if m.start() > first_link),
           len(html))
# Obsolete specs worth keeping anyway, because something current is defined in terms of them.
# tpc-b: pgbench IS a TPC-B implementation — it is the workload behind a large share of the
# PostgreSQL numbers anyone will ask about, so the spec that defines it is not history, it is the
# reference for a benchmark still being run today. Anything added here needs that kind of reason;
# "it might be interesting" is how a corpus fills with retired standards.
KEEP_OBSOLETE = re.compile(r"/tpc-b_", re.I)

seen = set()
for pos, url in links:
    obsolete = pos >= cut
    if obsolete and not KEEP_OBSOLETE.search(url):
        continue
    key = url.lower()
    if key not in seen:
        seen.add(key)
        print(url)
EXTRACT
)

if [ "${#urls[@]}" -eq 0 ]; then
	echo "the index page contained no PDF links — its markup probably changed" >&2
	exit 1
fi

mkdir -p "$DEST"
got=0
skipped=0
filtered=0

for url in "${urls[@]}"; do
	name="${url##*/}"
	if [ "$MODE" != "--all" ] && printf '%s' "$name" | grep -qE "$GOVERNANCE"; then
		filtered=$((filtered + 1))
		continue
	fi
	if [ "$MODE" = "--list" ]; then
		printf '  %s\n' "$name"
		continue
	fi
	if [ -s "$DEST/$name" ]; then
		skipped=$((skipped + 1))
		continue
	fi
	printf '  fetching %s\n' "$name"
	# Download to a temp name and move only on success, so an interrupted run never leaves a
	# truncated PDF that later looks downloaded and ingests as garbage.
	if curl -sfL -m 300 -A "$UA" "$url" -o "$DEST/.$name.part"; then
		# A PDF starts with %PDF. An HTML error page served with a .pdf URL does not, and would
		# otherwise be handed to the parser as a document.
		if head -c 4 "$DEST/.$name.part" | grep -q '%PDF'; then
			mv "$DEST/.$name.part" "$DEST/$name"
			got=$((got + 1))
		else
			echo "    NOT A PDF (server returned something else) — discarded" >&2
			rm -f "$DEST/.$name.part"
		fi
	else
		echo "    download failed" >&2
		rm -f "$DEST/.$name.part"
	fi
	sleep 1 # be a polite guest on someone else's server
done

if [ "$MODE" = "--list" ]; then
	echo "($filtered governance document(s) filtered; --all includes them)"
	exit 0
fi

# Wire the symlink farm the ingester reads. The PDFs live in Documents/Books (kept, backed up);
# corpus/ is disposable and rebuildable, so it only ever holds links into that.
mkdir -p "$LINKS"
linked=0
for f in "$DEST"/*.pdf; do
	[ -e "$f" ] || continue
	name="${f##*/}"
	if [ ! -e "$LINKS/$name" ]; then
		ln -s "$f" "$LINKS/$name" && linked=$((linked + 1))
	fi
done

echo
echo "downloaded $got, already present $skipped, filtered $filtered (governance)"
echo "files:    $DEST"
echo "symlinks: $LINKS (+$linked new)"
echo "total specs: $(find "$DEST" -maxdepth 1 -name '*.pdf' | wc -l)"
