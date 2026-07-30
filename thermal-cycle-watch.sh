#!/usr/bin/env bash
# thermal-cycle-watch.sh — how much of the CPU's temperature swing reaches the board?
#
#   ./thermal-cycle-watch.sh [seconds]     default 1200 (~1.5 gate cycles), samples every 10s
#
# ## Why this exists
#
# The arXiv ingest duty-cycles the machine: the thermal gate in arxiv-tail.sh feeds a batch, the box
# heats to ~84 C, the gate waits until it is back under 75 C for a minute, repeat — about 4 cycles an
# hour. Reasonable question: is that thermal cycling bad for the solder?
#
# Solder-joint fatigue is driven by the strain from CTE mismatch each cycle, and to a first
# approximation (Coffin-Manson) damage scales as N * dT^n with n ~ 2. So the number that matters is
# the swing AT THE JOINT, not at the die — and the die is the one sensor everybody looks at.
#
# Between the die and a board-level joint sit the package substrate, the heatsink and the PCB, all of
# which have far more thermal mass than a 100 mm^2 piece of silicon. So a die transient is damped
# before it becomes board strain. This script measures that damping instead of assuming it: sample
# coretemp (die) against acpitz (chassis/board) and the two NVMe controllers (board-mounted, far from
# the CPU) over at least one full gate cycle, and report each one's peak-to-trough amplitude.
#
# Read-only: it samples sysfs and changes nothing.
set -uo pipefail

DURATION="${1:-1200}"
EVERY="${WATCH_EVERY:-10}"

# Resolve sensors by NAME, not by hwmon index — the numbering is probe-order and moves across
# reboots. Same reasoning as the gate in arxiv-tail.sh.
declare -A SENSOR
for h in /sys/class/hwmon/hwmon*; do
	n="$(cat "$h/name" 2>/dev/null)" || continue
	case "$n" in
	coretemp) [ -r "$h/temp1_input" ] && SENSOR[die]="$h/temp1_input" ;;
	acpitz) [ -r "$h/temp1_input" ] && SENSOR[chassis]="$h/temp1_input" ;;
	nvme)
		# Two drives; keep both, first one found wins each slot.
		if [ -z "${SENSOR[nvme1]:-}" ]; then
			[ -r "$h/temp1_input" ] && SENSOR[nvme1]="$h/temp1_input"
		elif [ -z "${SENSOR[nvme2]:-}" ]; then
			[ -r "$h/temp1_input" ] && SENSOR[nvme2]="$h/temp1_input"
		fi
		;;
	esac
done

if [ -z "${SENSOR[die]:-}" ]; then
	echo "no coretemp sensor — nothing to compare against" >&2
	exit 1
fi

ORDER=(die chassis nvme1 nvme2)
present=()
for k in "${ORDER[@]}"; do [ -n "${SENSOR[$k]:-}" ] && present+=("$k"); done

read_c() { awk '{printf "%d", $1/1000}' "$1" 2>/dev/null; }

printf '%-10s' elapsed
for k in "${present[@]}"; do printf '%9s' "$k"; done
printf '\n'

declare -A MIN MAX
for k in "${present[@]}"; do
	MIN[$k]=999
	MAX[$k]=-999
done

t=0
while [ "$t" -lt "$DURATION" ]; do
	printf '%-10s' "${t}s"
	for k in "${present[@]}"; do
		v="$(read_c "${SENSOR[$k]}")"
		v="${v:-0}"
		printf '%8s°' "$v"
		[ "$v" -lt "${MIN[$k]}" ] && MIN[$k]="$v"
		[ "$v" -gt "${MAX[$k]}" ] && MAX[$k]="$v"
	done
	printf '\n'
	sleep "$EVERY"
	t=$((t + EVERY))
done

echo
echo "AMPLITUDE over ${DURATION}s"
die_amp=$((MAX[die] - MIN[die]))
for k in "${present[@]}"; do
	amp=$((MAX[$k] - MIN[$k]))
	if [ "$k" = die ] || [ "$die_amp" -le 0 ]; then
		printf '  %-8s %3s..%3s C   swing %3s C\n' "$k" "${MIN[$k]}" "${MAX[$k]}" "$amp"
	else
		# Share of the die's swing that reached this sensor. This ratio is the whole point: fatigue
		# scales with the swing where the joint is, and Coffin-Manson squares it, so a sensor seeing
		# a third of the die's swing sees roughly a ninth of the damage per cycle.
		pct=$((100 * amp / die_amp))
		printf '  %-8s %3s..%3s C   swing %3s C   = %s%% of die\n' \
			"$k" "${MIN[$k]}" "${MAX[$k]}" "$amp" "$pct"
	fi
done
