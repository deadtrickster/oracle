#!/usr/bin/env bash
# read-perf.sh - read perf.data files without remembering the incantation each time.
#
#   ./read-perf.sh FILE...              top symbols, flat
#   ./read-perf.sh -g FILE              with the call graph, caller-oriented
#   ./read-perf.sh -c FILE...           compare several files side by side
#   ./read-perf.sh -s PATTERN FILE...   share of each profile matching PATTERN
#   ./read-perf.sh -f FILE              render a flamegraph SVG next to the .data
#   ./read-perf.sh -d DIR               every .data under DIR, one summary line each
#
# ## Why
#
# `perf report` defaults are wrong for this work in two ways that cost real time.
#
# Without `--no-children -g none` it prints a CHILDREN column and an expanded call graph, so `head`
# shows one enormous tree for the top entry and nothing else. That is how a run got summarised as
# "22% ollama" when ollama was one of five processes - the rest was below the fold.
#
# And percentages must not be summed across greps. Doing that produced a "94% k-means" figure in a
# profile whose parse frames were already 49%, i.e. 143% of the samples. Use -s, which sums each
# symbol once.
set -uo pipefail

MODE=top
PATTERN=""
case "${1:-}" in
-g)
	MODE=graph
	shift
	;;
-c)
	MODE=compare
	shift
	;;
-s)
	MODE=share
	PATTERN="${2:-}"
	shift 2
	;;
-f)
	MODE=flame
	shift
	;;
-d)
	MODE=dir
	shift
	;;
-h | --help)
	sed -n '2,20p' "$0"
	exit 0
	;;
esac

[ "$#" -eq 0 ] && {
	sed -n '2,20p' "$0"
	exit 1
}

# HYBRID CPUS RECORD TWO EVENTS, AND perf REPORTS EACH SEPARATELY.
#
# This box is an Intel Core Ultra 9 275HX: P-cores and E-cores are different PMUs, so a plain
# `perf record` collects both `cpu_core/cycles/P` and `cpu_atom/cycles/P`. `perf report` then prints
# one full symbol table PER EVENT, back to back. Naively piping that to `head` reads the FIRST
# section only - which is cpu_atom, the minority of cycles - and silently reports a partial view.
#
# Measured cost of not knowing this: a capture where the same symbol was 35.60% (atom) and 93.39%
# (core). Reported as 35.60%. Cycle-weighted it is ~74%.
#
# So: aggregate across events, weighted by each event's cycle count, and say how many events there
# were. `--percentage absolute` keeps percentages comparable across sections.
events() { perf report -i "$1" --stdio --sort symbol --no-children -g none 2>/dev/null | awk '/^# Samples:/{n++} END{print n+0}'; }

flat() {
	perf report -i "$1" --stdio --sort symbol --no-children -g none --percentage absolute 2>/dev/null |
		awk '
      # One "Event count" line per event section. Accumulate the total ONCE per event, and weight
      # each symbol percentage by the cycles of the event it came from.
      /^# Event count \(approx\.\): /{ ec=$NF; total+=ec; next }
      /^ +[0-9]+\.[0-9]+%/{
        p=$1; sub(/%$/,"",p); sym=$0; sub(/^ +[0-9.]+% +/,"",sym)
        w[sym]+=p*ec              # percent x cycles
      }
      END{ if (total>0) for (s in w) printf "  %6.2f%%  %s\n", w[s]/total, s }
    ' | sort -rn
}

case "$MODE" in
top)
	for f in "$@"; do
		[ -s "$f" ] || {
			echo "  $f: empty or missing"
			continue
		}
		printf '\n\033[1m%s\033[0m  (%s)\n' "$(basename "$f")" "$(du -h "$f" | cut -f1)"
		flat "$f" | head -14 | sed 's/^ */  /' | cut -c1-110
	done
	;;

graph)
	# Caller-oriented, 0.5% threshold. Answers "what leads into this" - the question that
	# distinguished a spinning feeder from a spinning recv loop when the flat view could not.
	perf report -i "$1" --stdio --sort symbol -g graph,0.5,caller 2>/dev/null | head -60
	;;

compare)
	# Same symbols, several profiles, side by side. Phase changes show up as a symbol appearing or
	# vanishing, which a sequence of separate reports hides.
	tmp="$(mktemp -d)"
	trap 'rm -rf "$tmp"' EXIT
	for f in "$@"; do flat "$f" | sed 's/^ *//' | awk '{p=$1; sub(/%$/,"",p); $1=""; $2=""; sub(/^ +/,""); print p"\t"$0}' >"$tmp/$(basename "$f").tsv"; done
	awk -F'\t' '{seen[$2]=1} END{for (s in seen) print s}' "$tmp"/*.tsv | while read -r sym; do
		line="$(printf '%.48s' "$sym")"
		out=""
		for f in "$@"; do
			v="$(awk -F'\t' -v s="$sym" '$2==s{print $1; exit}' "$tmp/$(basename "$f").tsv")"
			out="$out$(printf '%7s' "${v:-.}")"
		done
		printf '  %-50s%s\n' "$line" "$out"
	done | sort -k2 -hr | head -18
	printf '  %-50s' "columns:"
	for f in "$@"; do printf '%7.7s' "$(basename "$f" .data)"; done
	echo
	;;

share)
	# Sum each matching symbol ONCE. perf prints a symbol per line; grepping and adding can
	# double-count across sections and produce >100%, which it has.
	printf '  %-44s %8s\n' FILE "SHARE%"
	for f in "$@"; do
		v="$(flat "$f" | sed 's/^ *//' | awk -v p="$PATTERN" 'tolower($0) ~ tolower(p) {gsub(/%/,"",$1); s+=$1} END{printf "%.2f", s+0}')"
		printf '  %-44s %8s\n' "$(basename "$f" .data)" "$v"
	done
	;;

flame)
	C="$(command -v inferno-collapse-perf || echo "$HOME/.cargo/bin/inferno-collapse-perf")"
	F="$(command -v inferno-flamegraph || echo "$HOME/.cargo/bin/inferno-flamegraph")"
	[ -x "$C" ] || command -v "$C" >/dev/null 2>&1 || {
		echo "inferno not installed: cargo install inferno" >&2
		exit 1
	}
	out="${1%.data}.svg"
	perf script -i "$1" 2>/dev/null | "$C" 2>/dev/null | "$F" --title "$(basename "$1" .data)" >"$out" 2>/dev/null
	[ -s "$out" ] && echo "  $out" || echo "  render produced nothing - check: perf report -i $1"
	;;

dir)
	printf '  %-46s %8s  %s\n' FILE SIZE 'TOP SYMBOL'
	find "$1" -name '*.data' -size +100k | sort | while read -r f; do
		printf '  %-46s %8s  %s\n' "$(basename "$f" .data)" "$(du -h "$f" | cut -f1)" \
			"$(flat "$f" | head -1 | sed 's/^ *//' | cut -c1-56)"
	done
	;;
esac
