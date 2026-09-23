# systemd units

Service units for the twelve SPARK daemons (see CLAUDE.md "Systemd Services").
Units are copied, not symlinked, so the checked-in file and the installed one
can drift. `bin/px-deploy-check` reports that drift (#389/#390); one command
clears it — every `*.service`, `*.timer` and `<unit>.d/*.conf` drop-in, each
`systemd-analyze verify`'d first, then one `daemon-reload`:

```bash
bin/px-units-install --dry-run          # what would change
sudo /usr/local/sbin/px-units-install   # apply (NOPASSWD once the launcher is installed)
```

It never restarts or enables anything: a new setting lands at each unit's next
restart, and a never-installed unit (e.g. `px-io-attrib`) still needs
`enable --now`. The launcher's source is `sbin/px-units-install`.

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
systemctl show px-wake-listen.service -p MemoryHigh,MemoryMax,OOMPolicy,CPUWeight,MemoryCurrent
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
