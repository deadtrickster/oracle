#!/usr/bin/env bash
# label — start the chunk-labeling UI with everything defaulted.
#
#   label              # heuristic+random queue (blind labeling)
#   label uncertain    # review the Opus fleet's LOW-certainty labels, least certain first
#   label uncertain 0.6
#
# Anything after the mode is passed through to label-ui.py verbatim.
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
FEATURES="$HERE/features.npz"

[ -r "$FEATURES" ] || {
	echo "missing $FEATURES — build it first: uv run build-junk-features.py --out features.npz" >&2
	exit 1
}

mode="${1:-nominate}"
[ $# -gt 0 ] && shift

case "$mode" in
uncertain)
	max="${1:-0.8}"
	[ $# -gt 0 ] && shift
	exec uv run "$HERE/label-ui.py" --features "$FEATURES" \
		--queue uncertain --max-certainty "$max" "$@"
	;;
nominate)
	exec uv run "$HERE/label-ui.py" --features "$FEATURES" "$@"
	;;
*)
	echo "usage: label [nominate | uncertain [max-certainty]] [label-ui.py args...]" >&2
	exit 2
	;;
esac
