#!/usr/bin/env bash
# Syntax-gate every file in the extension, the way CHROME will parse it.
#
# Why this exists: `node --check background.js` PASSED on a file containing two adjacent string
# literals with no `+` between them — a Python habit that is a syntax error in JavaScript. Chrome
# refused to register the service worker ("Status code: 15") and the extension was dead, while the
# check I trusted said it was fine. Plain `--check` does not parse a .js file as an ES module, so
# the module-only path was never really tested.
#
# So: module files are copied to .mjs and checked as modules; everything else is checked as a
# classic script, which is how Chrome injects them. A gate that can pass on a broken file is worse
# than no gate, because it converts "I did not check" into "I checked".
set -euo pipefail

cd "$(dirname "$0")"

# background.js is `"type": "module"` in the manifest and popup.js is `<script type="module">` in
# popup.html; both import from sse.js. Everything else is INJECTED (executeScript/content_scripts),
# which is always a classic script — an `import` in one of those fails at run time, not load time,
# so the split matters.
MODULES=(background.js sse.js popup.js)
CLASSIC=(chat.js overlay.js cite.js regionselect.js dwell.js)

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fail=0

for f in "${MODULES[@]}"; do
	[[ -f $f ]] || continue
	cp "$f" "$tmp/${f%.js}.mjs"
	if node --check "$tmp/${f%.js}.mjs"; then
		printf '  ok    %-20s (module)\n' "$f"
	else
		printf '  FAIL  %-20s (module)\n' "$f"
		fail=1
	fi
done

for f in "${CLASSIC[@]}"; do
	[[ -f $f ]] || continue
	# .cjs forces classic-script parsing regardless of any package.json nearby
	cp "$f" "$tmp/${f%.js}.cjs"
	if node --check "$tmp/${f%.js}.cjs"; then
		printf '  ok    %-20s (classic)\n' "$f"
	else
		printf '  FAIL  %-20s (classic)\n' "$f"
		fail=1
	fi
done

# The manifest is JSON and is equally fatal when malformed.
if node -e 'JSON.parse(require("fs").readFileSync("manifest.json","utf8"))'; then
	printf '  ok    %-20s (json)\n' manifest.json
else
	printf '  FAIL  %-20s (json)\n' manifest.json
	fail=1
fi

# Injected functions are serialised and run in the PAGE, so they cannot reference anything in this
# file's module scope. Syntax checking cannot see that — the failure is a runtime ReferenceError
# inside the page, where nothing here is watching.
if node check-injected.mjs >/dev/null 2>&1; then
	printf '  ok    %-20s (injected scope)\n' background.js
else
	node check-injected.mjs | grep -E '^(FAIL|      )' || true
	printf '  FAIL  %-20s (injected scope)\n' background.js
	fail=1
fi

exit "$fail"
