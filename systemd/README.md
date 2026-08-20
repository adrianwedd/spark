# systemd units

Service units for the twelve SPARK daemons (see CLAUDE.md "Systemd Services").
Install to `/etc/systemd/system/` on the Pi, then `daemon-reload` + `enable --now`.

Units are plain `cp`'d, not symlinked — the checked-in file and the deployed
file can drift, and nothing detects that automatically. When editing a unit,
redeploy it.

## Resource containment (#217/#218/#219)

`*.service.d/10-containment.conf` drop-ins set `MemoryHigh`/`MemoryMax`/
`OOMPolicy` (and `CPUWeight` on a few) for the twelve daemons named in
`tests/test_systemd_containment.py::CONTAINED_UNITS`. Rationale and the
measured baseline each limit was set against live in each drop-in's own
comment header and in `docs/operations/resource-containment.md` — read that
before changing a number, not just this file.

Requires #218's cgroup-memory + PSI kernel cmdline fix to already be live
(`cat /sys/fs/cgroup/cgroup.controllers` must list `memory`), or every limit
below parses and is silently unenforceable.

Install/update (on `systemd 252`, confirmed empirically 2026-08-20:
`daemon-reload` alone applies `MemoryHigh`/`MemoryMax`/`CPUWeight`/`OOMPolicy`
to an already-running unit's cgroup — **no restart needed** for these
properties specifically):

```bash
for d in systemd/*.service.d; do
  unit=$(basename "$d" .service.d)
  sudo mkdir -p "/etc/systemd/system/${unit}.service.d"
  sudo cp "$d"/*.conf "/etc/systemd/system/${unit}.service.d/"
done
sudo systemctl daemon-reload
```

Verify a unit picked it up without restarting it:

```bash
systemctl show px-brain.service -p MemoryHigh,MemoryMax,OOMPolicy,CPUWeight,MemoryCurrent
```

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
