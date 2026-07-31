#!/usr/bin/env bash
# mem-forensics.sh - where the RAM and, more importantly, the SWAP actually went.
#
#   ./mem-forensics.sh            full report
#   ./mem-forensics.sh --top 20   more processes
#   PID=1234 ./mem-forensics.sh   also break down that process's mappings
#   sudo ./mem-forensics.sh       adds per-mapping detail for processes you do not own
#
# ## Why this exists
#
# `top` and `ps` report RSS, and RSS stops meaning "how much memory this process is using" the moment
# the box starts swapping. During a load test here, one process showed RSS falling 34 -> 18 GB while
# `MemAvailable` recovered 10 -> 38 GB. Both looked like the job finishing. Neither was: the pages
# were migrating to swap and the process still held ~80 GB. Reading RSS alone produced two wrong
# conclusions in one afternoon.
#
# So this reports `VmRSS + VmSwap` per process, and then goes after the harder question: swap that
# belongs to NO process.
#
# ## The unattributed-swap problem
#
# Summing every process's `VmSwap` usually does not equal `SwapTotal - SwapFree`, and the gap is not
# an error. Swapped-out **shmem** (tmpfs files, `MAP_SHARED` anonymous, memfd, SysV segments) is
# charged to the shmem accounting, not to any task's `VmSwap`. A database that keeps its buffer pool
# in shared memory can therefore have tens of GB in swap that no per-process view will show you.
#
# This box: 34.9 GB Shmem with only 657 MB visible in tmpfs mounts, which means it is anonymous
# shared memory - almost certainly a buffer pool - and it is swappable.
set -uo pipefail

TOP="${TOP:-12}"
[ "${1:-}" = "--top" ] && TOP="${2:-12}"

g() { awk -v k="$1" '$1==k":"{printf "%.1f", $2/1048576}' /proc/meminfo; }

hdr() { printf '\n\033[1m%s\033[0m\n' "$*"; }

hdr "SYSTEM"
printf '  %-18s %8s GB\n' \
	"MemTotal" "$(g MemTotal)" \
	"MemFree" "$(g MemFree)" \
	"MemAvailable" "$(g MemAvailable)" \
	"Cached" "$(g Cached)" \
	"AnonPages" "$(g AnonPages)" \
	"Shmem" "$(g Shmem)" \
	"Slab" "$(g Slab)" \
	"PageTables" "$(g PageTables)"

swap_total="$(g SwapTotal)"
swap_free="$(g SwapFree)"
swap_used="$(awk -v t="$swap_total" -v f="$swap_free" 'BEGIN{printf "%.1f", t-f}')"
printf '\n  %-18s %8s GB\n' "SwapTotal" "$swap_total" "SwapUsed" "$swap_used" \
	"SwapCached" "$(g SwapCached)"

# Committed_AS is what would be needed if every allocation were touched. Above MemTotal+SwapTotal
# means the system is relying on overcommit and would OOM if the promises were called in.
committed="$(g Committed_AS)"
capacity="$(awk -v m="$(g MemTotal)" -v s="$swap_total" 'BEGIN{printf "%.1f", m+s}')"
printf '\n  %-18s %8s GB   (RAM+swap capacity %s GB)\n' "Committed_AS" "$committed" "$capacity"

hdr "PROCESSES  (RSS + swap, which is the number that does not lie)"
printf '  %-18s %-9s %9s %9s %9s\n' COMMAND PID RSS SWAP TOTAL
total_vmswap=0
while read -r tot rss swp pid comm; do
	printf '  %-18s %-9s %8.1fG %8.1fG %8.1fG\n' \
		"$comm" "$pid" "$((rss))e-6" "$((swp))e-6" "$((tot))e-6" 2>/dev/null ||
		printf '  %-18s %-9s %8.1fG %8.1fG %8.1fG\n' "$comm" "$pid" \
			"$(awk -v v="$rss" 'BEGIN{print v/1048576}')" \
			"$(awk -v v="$swp" 'BEGIN{print v/1048576}')" \
			"$(awk -v v="$tot" 'BEGIN{print v/1048576}')"
done < <(
	for p in /proc/[0-9]*; do
		[ -r "$p/status" ] || continue
		read -r rss swp comm < <(awk '/^VmRSS:/{r=$2} /^VmSwap:/{s=$2} /^Name:/{n=$2} END{print r+0, s+0, n}' "$p/status" 2>/dev/null)
		[ "${rss:-0}" -eq 0 ] && [ "${swp:-0}" -eq 0 ] && continue
		echo "$((rss + swp)) $rss $swp ${p#/proc/} $comm"
	done | sort -rn | head -"$TOP"
)

# Sum every task's VmSwap, then compare against actual swap in use. The difference is the swap that
# no process owns - shmem, and it is invisible to ps/top entirely.
total_vmswap="$(awk '{s+=$1} END{printf "%.1f", s/1048576}' < <(
	for p in /proc/[0-9]*; do awk '/^VmSwap:/{print $2}' "$p/status" 2>/dev/null; done
))"

hdr "SWAP ATTRIBUTION"
printf '  %-34s %8s GB\n' "sum of every process VmSwap" "$total_vmswap"
printf '  %-34s %8s GB\n' "actual swap in use" "$swap_used"
unattributed="$(awk -v a="$swap_used" -v b="$total_vmswap" 'BEGIN{printf "%.1f", a-b}')"
printf '  %-34s %8s GB  <- belongs to no process\n' "unattributed (shmem/tmpfs)" "$unattributed"

if awk -v u="$unattributed" 'BEGIN{exit !(u>1)}'; then
	cat <<'EOF'

  That gap is swapped-out shared memory. It is charged to Shmem, not to any task, so ps and top
  cannot show it. Sources, in the order worth checking:
EOF
	printf '\n  %-42s %6s %6s\n' MOUNT SIZE USED
	df -h --output=target,size,used -t tmpfs 2>/dev/null | tail -n +2 |
		sort -k3 -hr | head -5 | awk '{printf "  %-42s %6s %6s\n", $1, $2, $3}'
	echo
	echo "  If tmpfs usage is small but Shmem is large, it is anonymous shared memory -"
	echo "  MAP_SHARED|MAP_ANONYMOUS or memfd, typically a database buffer pool. Check the"
	echo "  big process's smaps for large mappings with a Swap: line:"
	echo "    sudo awk '/^[0-9a-f]/{a=\$0} /^Swap:/{if (\$2>1048576) print \$2/1048576\" MB \"a}' /proc/<pid>/smaps"
fi

# Drill into one process's mappings. Per-process VmSwap says HOW MUCH is swapped; this says WHAT -
# whether it is one enormous allocation or ten thousand small ones, and whether huge pages are in
# play. Those two facts change the diagnosis completely.
if [ -n "${PID:-}" ] && [ -r "/proc/$PID/smaps" ]; then
	hdr "MAPPINGS of pid $PID  (set PID=<pid> to change)"
	printf '  %-9s %10s %10s %10s  %s\n' PERMS SIZE RSS SWAP BACKING
	awk '
    /^[0-9a-f]+-[0-9a-f]+ /{ perms=$2; name=($6==""?"[anon]":$6); sz=0; rss=0; sw=0; have=1 }
    /^Size:/{ sz=$2 } /^Rss:/{ rss=$2 } /^Swap:/{ sw=$2
      if (have && (sw > 262144 || rss > 262144))
        printf "  %-9s %9.1fG %9.1fG %9.1fG  %s\n", perms, sz/1048576, rss/1048576, sw/1048576, name
      have=0 }
  ' "/proc/$PID/smaps" 2>/dev/null | sort -k4 -hr | head -10

	awk '
    /^[0-9a-f]+-[0-9a-f]+ /{ k=($6==""?"anon":($6 ~ /\/dev\/shm|memfd/ ? "shmem" : "file")) }
    /^Swap:/{ s[k]+=$2 } /^Rss:/{ r[k]+=$2 }
    END{ printf "\n  by backing:"; for (t in s) printf "  %s rss %.1fG swap %.1fG;", t, r[t]/1048576, s[t]/1048576; print "" }
  ' "/proc/$PID/smaps" 2>/dev/null

	# Huge pages matter here. A multi-GB region on 4 KB pages is millions of PTEs, and every page
	# faulted back from swap is a separate 4 KB round trip. Observed on this box: a 114 GB anonymous
	# mapping with AnonHugePages 0 and THPeligible 0, which is close to the worst case for a linear
	# scan over a working set that does not fit.
	awk '/^AnonHugePages:/{h+=$2} /^THPeligible:/{e+=$2} END{
      printf "  huge pages: AnonHugePages %.1f GB, THPeligible %d\n", h/1048576, e }' \
		"/proc/$PID/smaps" 2>/dev/null
	echo "  mappings: $(grep -c '^[0-9a-f]*-[0-9a-f]* ' "/proc/$PID/maps" 2>/dev/null)"
	echo
	echo "  One huge mapping is worse than many small ones under pressure: the kernel cannot tell"
	echo "  the hot part from the cold part, so LRU evicts pages the process is about to touch."
fi

hdr "PRESSURE"
if [ -r /proc/pressure/memory ]; then
	sed 's/^/  /' /proc/pressure/memory
else
	echo "  (no PSI)"
fi
a_in="$(awk '/^pswpin/{print $2}' /proc/vmstat)"
a_out="$(awk '/^pswpout/{print $2}' /proc/vmstat)"
sleep 5
b_in="$(awk '/^pswpin/{print $2}' /proc/vmstat)"
b_out="$(awk '/^pswpout/{print $2}' /proc/vmstat)"
awk -v ai="$a_in" -v bi="$b_in" -v ao="$a_out" -v bo="$b_out" \
	'BEGIN{printf "  swap-in %6.1f MB/s   swap-out %6.1f MB/s   (5s sample)\n", (bi-ai)*4/1024/5, (bo-ao)*4/1024/5}'
echo
echo "  Sustained swap-in means thrashing: pages are being read back as fast as they are evicted."
echo "  Swap-out alone is just the kernel making room, which is not by itself a problem."
