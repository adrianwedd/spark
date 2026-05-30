#!/bin/sh
# Sweep leftover pip build/unpack scratch dirs from failed/interrupted installs.
# pip normally cleans these, but an interrupted run (e.g. OOM) leaves them behind.
# Default Debian /tmp policy only wipes on reboot, so they accumulate across uptime.
# Install to /usr/local/sbin/spark-pip-cleanup.sh (root:root, 0755).
find /tmp -maxdepth 1 -name 'pip-*' -mtime +1 -exec rm -rf {} + 2>/dev/null
exit 0
