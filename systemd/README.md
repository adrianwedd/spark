# systemd units

`bin/px-deploy-check` compares copied unit files with the checkout. The
unprivileged `bin/px-units-install --dry-run` reports the same set: top-level
`*.service`, `*.timer`, and every corresponding `<unit>.d/*.conf` drop-in.

Applying a change uses **administrator-staged code and inputs**, not the
pi-writable checkout. The no-argument `/usr/local/sbin/px-units-install` launcher
runs isolated system Python, checks root ownership and rejects symlinks,
group/world-writable components and unexpected file types throughout its
implementation/input paths. It replaces the environment and executes only
`/usr/local/libexec/spark/px-units-install`, using the protected source tree
`/usr/local/share/spark/units/systemd`. It never stages or self-updates itself.

The installer validates all units/drop-ins before copying and performs one
`daemon-reload`; it never starts, restarts or enables a unit or changes sudoers.
**Reload can still apply live resource-containment settings** (below). Review
those changes and any pending restart-limit decision before applying. The
current robot-HAT hold and its two `90-hardware-disabled.conf` files must remain
intact; do not start hardware to validate this installer.

## Administrator bootstrap and snapshot updates (#390/#440/#442)

This is an authenticated administrator step, not a workaround for unavailable
sudo. The grant cannot turn a pi-writable file into a trusted input. First
prepare the reviewed archive on a trusted development host:

```sh
git archive --format=tar REVIEWED_COMMIT bin/px-units-install systemd > spark-units.tar
shasum -a 256 spark-units.tar
```

Record the full commit and archive SHA256 in the review receipt. Transfer the
archive without executing it. Obtain the expected hash independently from that
trusted receipt, **not from the pi checkout or a hash supplied beside an
untrusted replacement**. The administrator then authenticates and uses a fresh
root-only staging directory (replace the two REVIEWED placeholders):

```sh
sudo -i
set -eu
umask 077
spark_unit_stage=/root/spark-units-staging
[ ! -e "$spark_unit_stage" ]
install -d -o root -g root -m 0700 "$spark_unit_stage"
install -o root -g root -m 0600 /path/to/spark-units.tar "$spark_unit_stage/source.tar"
printf '%s  %s\n' REVIEWED_ARCHIVE_SHA256 "$spark_unit_stage/source.tar" | sha256sum -c -
tar --no-same-owner --no-same-permissions -xf "$spark_unit_stage/source.tar" -C "$spark_unit_stage"

# Fresh bootstrap refuses to overwrite an existing trusted installation.
[ ! -e /usr/local/libexec/spark/px-units-install ]
[ ! -e /usr/local/sbin/px-units-install ]
[ ! -e /usr/local/share/spark/units ]
install -d -o root -g root -m 0755 /usr/local/libexec/spark /usr/local/share/spark/units
install -o root -g root -m 0755 "$spark_unit_stage/bin/px-units-install" /usr/local/libexec/spark/px-units-install
cp -R "$spark_unit_stage/systemd" /usr/local/share/spark/units/
install -o root -g root -m 0755 "$spark_unit_stage/systemd/sbin/px-units-install" /usr/local/sbin/px-units-install
exit  # leave the administrator shell before using any checkout scripts
```

All ancestor directories must also be root-owned and not group/world writable;
the launcher refuses otherwise. Before installing or using a sudoers grant, inspect
the entire installed path with `namei -l`: a launcher cannot protect its own code
if its installation directory permits replacement. Snapshot files may be root-only (0600/0700) or
readable (0644/0755), never writable by pi/group/others. A later snapshot update
is the same administrator-controlled operation: retain the old implementation,
launcher and snapshot under a fresh root-owned backup name before replacement;
never overwrite custody evidence or restage through the NOPASSWD helper.

Install/validate any separately approved sudoers policy **after** the trusted
files exist (`systemd/sudoers.d/picar-x-services`, root:root, mode0644, `visudo -c -f`).
This helper neither installs it nor grants additional privileges. Then:

The comparison and deploy check below run as ordinary `pi`, after leaving the
root shell. Only the absolute installed launcher is privileged.

```sh
cd /home/pi/picar-x-hacking
bin/px-units-install --dry-run
sudo /usr/local/sbin/px-units-install
bin/px-deploy-check --strict
```

A checkout update does not update the trusted snapshot. If the gate still
reports drift, stage the newly reviewed units rather than letting the helper
read pi-writable inputs. Keep source revision, trusted snapshot, installed
files and running processes distinct; restart only the deliberately selected
units after the gate's process readback and current hardware/safety review.

This repairs the **unit-install helper's** code/input trust boundary. It does
not complete Spark's OS isolation: existing root units may still execute
checkout code, as documented separately under #281. No broad privilege-separation
claim follows from a root-owned launcher.

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
