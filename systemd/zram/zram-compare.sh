#!/bin/bash
# Read-only before/after snapshot for the zram change (#283). No sudo, no writes.
#   bash systemd/zram/zram-compare.sh
# Run it once now (baseline is already in docs) and again a few days after
# install to fill the "after" column. Everything here is a plain read.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1

echo "=== zram-compare @ $(date -Is) ==="

echo "-- swap devices (want: zram0 prio 100 above /var/swap prio -2)"
swapon --show || true

echo "-- sysctls (want post-install: swappiness=100 page-cluster=0)"
sysctl vm.swappiness vm.page-cluster 2>/dev/null

echo "-- zram stats (only after install)"
if [ -e /sys/block/zram0/mm_stat ]; then
  echo "comp_algorithm: $(cat /sys/block/zram0/comp_algorithm)"
  # mm_stat fields: orig_data compr_data mem_used ...
  awk '{printf "orig=%.1fMB compr=%.1fMB used=%.1fMB ratio=%.2fx\n",
        $1/1048576,$2/1048576,$3/1048576,($2>0?$1/$2:0)}' /sys/block/zram0/mm_stat
else
  echo "(zram0 not present — pre-install)"
fi

echo "-- IO / memory PSI"
cat /proc/pressure/io /proc/pressure/memory

echo "-- vmstat swap in/out (si/so should trend to ~0 on SD after zram)"
vmstat 1 3 | tail -3

echo "-- overruns today ($(date +%F))"
grep -c "$(date +%F).*overrun" logs/px-wake-listen.log 2>/dev/null || echo 0

echo "-- IO PSI at the last 20 overruns (psi_io_some_avg10)"
grep overrun logs/px-wake-listen.log | tail -20 \
  | grep -o 'psi_io_some_avg10_at=[0-9.]*' | cut -d= -f2 \
  | awk '{s+=$1; n++} END{if(n)printf "n=%d mean=%.1f%%\n",n,s/n; else print "none"}'

echo "-- swap_free_kb at the last 20 overruns (baseline median was 10212 of 524284)"
grep overrun logs/px-wake-listen.log | tail -20 \
  | grep -o 'swap_free_kb_at=[0-9.]*' | cut -d= -f2 \
  | sort -n | awk '{a[NR]=$1} END{if(NR)printf "n=%d min=%s median=%s max=%s\n",NR,a[1],a[int(NR/2)+1],a[NR]; else print "none"}'

echo "-- px-brain / px-wake-listen memory.current vs MemoryHigh (#270 axis)"
for s in px-brain px-wake-listen; do
  systemctl show "$s" -p MemoryCurrent,MemoryHigh 2>/dev/null \
    | paste -sd' ' | sed "s/^/$s /"
done
echo "=== end ==="
