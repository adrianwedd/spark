#!/bin/bash
# Root installer for zram swap (issue #283 / #247). Run as root:
#   sudo bash systemd/zram/install-zram.sh
# Everything here is reversible; rollback block at the bottom of this file.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"

echo "== 1. verify zstd is loadable"
modprobe zstd
grep -q zstd /proc/crypto || { echo "FATAL: zstd not in /proc/crypto"; exit 1; }

echo "== 2. install systemd-zram-generator"
apt-get install -y systemd-zram-generator

echo "== 3. install config files"
install -m 0644 "$REPO/systemd/zram/zram-generator.conf" /etc/systemd/zram-generator.conf
install -m 0644 "$REPO/systemd/zram/99-spark-zram.conf" /etc/sysctl.d/99-spark-zram.conf

echo "== 4. activate (no reboot needed)"
# daemon-reload runs the zram generator, which *materializes*
# systemd-zram-setup@zram0.service from the config; starting it before the
# reload finishes races the generator and the unit isn't there yet. Reload,
# then start explicitly, then give udev/swapon a moment to attach.
systemctl daemon-reload
systemctl start systemd-zram-setup@zram0.service
systemctl start dev-zram0.swap 2>/dev/null || true
sysctl -p /etc/sysctl.d/99-spark-zram.conf
for _ in 1 2 3 4 5; do grep -qw zram0 /proc/swaps && break; sleep 1; done

echo "== 5. verify"
swapon --show
grep -w zram0 /proc/swaps | awk '$NF == 100 {ok=1} END {exit !ok}' \
  || { echo "FATAL: zram0 not active at priority 100"; exit 1; }
cat /sys/block/zram0/comp_algorithm
echo "OK: zram0 active at priority 100; /var/swap remains as fallback (prio -2)."

: <<'ROLLBACK'
# Exact rollback (run as root):
swapoff /dev/zram0
systemctl stop systemd-zram-setup@zram0.service
rm -f /etc/systemd/zram-generator.conf /etc/sysctl.d/99-spark-zram.conf
systemctl daemon-reload
sysctl vm.page-cluster=3 vm.swappiness=60
apt-get remove -y systemd-zram-generator   # optional
# /var/swap (dphys-swapfile) was never touched and remains the active swap.
ROLLBACK
