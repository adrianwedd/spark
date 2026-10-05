# Battery evidence retention (#439)

This is offline-tested source preparation, not a brownout fix, diagnosis, live
measurement, or deployment. Charging detection, ADC floor, warm-up counters,
shutdown thresholds, and confirmation policy are unchanged. The measured and
derived trace labels in `tests/test_battery_trend.py` are unchanged; all new
evidence tests use explicitly synthetic inputs.

## Store and bounds

When a separately authorised deployment runs the existing battery poller, its
new diagnostic writer targets `PX_STATE_DIR/battery-history.jsonl` (default:
`state/battery-history.jsonl`) and a single older `.1` file. Each is at most
256 KiB. Rotation replaces the older file before appending a line that would
exceed the current file's limit. No copy, temporary rotation file, or unbounded
archive is created. An oversized current file from outside this writer is
removed rather than archived. Bounds assume this poller is the sole writer;
external edits or multiple pollers are unsupported.

One sample line is appended per successful ADC acquisition, including rejected
readings and warm-up, at the existing 30-second cadence. Each appended line is
capped at 512 bytes, including newline and diagnostic counters. Thus the logical
append-volume ceiling is `512 * (86400 / 30)` bytes/day; rotations rename files,
not rewrite their contents. Filesystem metadata/block write amplification is
not bounded by that figure. The queue retains at most 64 sample lines, each
capped at 512 bytes before counter augmentation, plus one in-flight line.

All directory creation, stat, append, flush-on-file-close, and rotation happen
on the existing `StallProofLineWriter` background daemon-thread primitive.
The polling path only formats fixed fields and enqueues without waiting for
storage. Constructor startup also performs no filesystem work. A stalled
writer discards oldest queued samples; `queue_dropped` is the process-local
cumulative count carried on subsequent written records. `storage_errors` counts
failed background writes and oversized final records, and becomes visible when
storage recovers. Full, read-only, or unwritable storage cannot propagate a new
evidence error into the poller's decisions. Tests inject these errors and hold
a storage event blocked while the producer completes and fills/drops its queue.
This does not make the entire existing poller stall-proof: its pre-existing
battery.json, health, stderr, and alert paths remain as before.

## Record interpretation and compatibility

- `sample_ts`: Unix wall-clock seconds captured when the sample is enqueued,
  not when a delayed writer commits it. Clock steps remain possible.
- `raw_volts`: unrounded ADC result; `volts`: existing two-decimal value used by
  the poller. Neither is a smoothed or inferred pack-voltage trace.
- `classification`: `rejected_adc`, `warmup`, or `accepted`, describing the
  poller's existing branch, not the mind's separate percentage glitch filter.
- `charging`: unchanged detector state for an accepted sample; false in warm-up;
  previous reported state for a rejected ADC sample (no detector update).
- `queue_dropped` and `storage_errors`: cumulative counters since process start,
  not completeness guarantees. Non-finite ADC values are strings (`nan`, `inf`,
  `-inf`) in strict JSON; this encoding does not change their poller treatment.

The battery.json schema and detector module are unchanged. The writer uses
standard-library code and the existing logging module, compatible with the
project's Python 3.11 runtime; no new dependency or service configuration is
needed. Missing evidence support or writer startup failure falls back to no-op
retention without changing battery behavior. No history is fed into shutdown,
charging, health, API, model prompts, or a network service.

There is deliberately no fsync, retry backlog, or synchronous poll-path flush.
Queued data and the final unflushed/partial line may be lost on power failure;
ignore malformed partial lines when reading. A storage error can leave a partial
append, so parsers must skip malformed lines anywhere, not just the last one.
There is no guarantee the decisive last sample survives a brownout. Persistent
storage failure leaves no durable counter update. A permanently stalled writer
remains one daemon thread with a bounded queue. History can span restarts but
counters reset, and the size cap, not an age guarantee, determines retention.

## Later operator verification (not performed here)

Only after independent review and explicitly authorised deployment:

1. Without changing charging/discharge state or inducing a fault, read the two
   history files from the operator's authorised local session. Check file sizes,
   strict JSON fields, timestamps, and that newest accepted `volts` matches the
   contemporary battery.json sample when neither sample has advanced.
2. Check naturally occurring warm-up/rejected records if present; their absence
   is not permission to generate ADC faults or reboot/discharge the robot.
3. Account for queue/storage counters and malformed lines before interpreting
   a series. Do not infer pack condition from rejected/absent samples alone.
4. Preserve any future event series locally with its boot/restart context before
   rotation replaces it. Do not publish private state or claim this patch fixes
   the emergency shutdown. Reassess safety policy only against real evidence.

Offline acceptance: targeted `tests/test_battery_evidence.py` plus the existing
`tests/test_battery_trend.py`, bash syntax validation, and `git diff --check`.
No poller, GPIO, audio, sudo, process-control, service, or hardware path is
executed by these tests; the integration check parses the heredoc AST only.
