# Operating Spark with the robot HAT disabled

The Pi, camera, microphone and speech services can remain operational while the
HAT motor, servo, sonar and battery capabilities are unavailable. Do not infer
battery charge from the Pi being powered, or from an old battery state file.

Use the existing saved session controls to set `confirm_motion_allowed=false`
and `roaming_allowed=false`. Preserve their prior values before changing them.
Create `state/robot-hat-disabled` with the reason for the hardware hold.
The existing shared `px-env` launcher checks this marker before HAT-dependent
tool and direct helper calls, returning `unavailable` before GPIO acquisition.
Speech, camera, cognition and the emergency-stop path remain available.

The two tracked `90-hardware-disabled.conf` drop-ins use systemd's
`ConditionPathExists` to keep `px-alive` and `px-battery-poll` from starting,
including after a reboot or a manual start, while that marker exists. Install
only these named drop-ins when other unit drift is outside the current scope:

```sh
sudo install -d /etc/systemd/system/px-alive.service.d /etc/systemd/system/px-battery-poll.service.d
sudo install -m 0644 systemd/px-alive.service.d/90-hardware-disabled.conf /etc/systemd/system/px-alive.service.d/
sudo install -m 0644 systemd/px-battery-poll.service.d/90-hardware-disabled.conf /etc/systemd/system/px-battery-poll.service.d/
sudo systemctl daemon-reload
sudo systemctl stop px-alive px-battery-poll
sudo systemctl start px-alive px-battery-poll
systemctl show px-alive px-battery-poll -p ActiveState -p ConditionResult
```

The start is a verification: both units should remain inactive with a false
condition. Do not install new sudo privileges to work around missing admin
access. Existing exact-command grants may permit stopping alive but not the
battery poller or installing these drop-ins; an administrator must authenticate
for that part.

Keep camera streaming, wake listening, mind, API, TTS and network services on.
Preserve invalid/stale `battery.json` outside the active state location. Battery
readers reject samples older than 90 seconds, future or timezone-free stamps,
invalid percentages and voltages outside the supported 2S range (5–9 V).
Sonar readers reject missing, invalid, future or stale data; the normal runtime
path is `/run/spark/sonar_live.json`, with the state directory as a fallback.
API history must not forward-fill battery percentages across missing samples.
The local dashboard explicitly clears the previous battery/sonar display.

After repair, remove the marker, deliberately restore the desired saved motion
permissions and start the two services. Keep old battery records archived;
wait for valid fresh telemetry instead of copying an old percentage back.
Removing the marker alone does not start a stopped service.
