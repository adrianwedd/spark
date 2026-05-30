# systemd units

Service units for the eleven SPARK daemons (see CLAUDE.md "Systemd Services").
Install to `/etc/systemd/system/` on the Pi, then `daemon-reload` + `enable --now`.

## Maintenance timer: pip /tmp cleanup

`spark-pip-cleanup.timer` sweeps leftover `/tmp/pip-*` scratch dirs (older than
1 day) left behind by failed/interrupted pip installs. Debian's default `/tmp`
policy only wipes on reboot, so they accumulate across uptime — an interrupted
install once left 4.5G behind. Install:

```bash
sudo cp systemd/spark-pip-cleanup.sh /usr/local/sbin/spark-pip-cleanup.sh
sudo chown root:root /usr/local/sbin/spark-pip-cleanup.sh
sudo chmod 755 /usr/local/sbin/spark-pip-cleanup.sh
sudo cp systemd/spark-pip-cleanup.service systemd/spark-pip-cleanup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spark-pip-cleanup.timer
```

Verify: `systemctl list-timers spark-pip-cleanup.timer`.
