# Testing

**Owns:** how the suite is isolated, what "green" means, and what it cannot
prove. `tests/conftest.py`, `pyproject.toml`.

---

## Invariant

### Tests are hermetic by default

This repository is checked out **on the robot it controls**. A test that reads
or writes live state is not a flaky test — it is a test that changes the
robot's behaviour and then reports on itself.

A test must not, unless explicitly marked `live`:

- read or write anything under the live `state/`
- read the live `/run/spark` runtime directory
- make a billed LLM call
- reach the network
- touch GPIO, the microphone, or the speaker

### Running

```bash
python -m pytest                    # full suite
python -m pytest -m "not live"      # skip hardware tests
python -m pytest tests/test_state.py
python -m pytest -k test_name
sudo .venv/bin/python -m pytest tests/test_tools_live.py -v -s   # live hardware
```

`testpaths = ["tests"]` and `norecursedirs` exclude `.worktrees` — pytest does
**not** honour `.gitignore`, and without these it collected every worktree's
same-named `tests/` tree and died with 229 import-mismatch errors. Bare
`pytest` from the repo root must be correct: the local run is this project's
real gate.

### Two isolation mechanisms, and they cover different things

**Autouse fixtures** (unconditional, every test) isolate **in-process** writes:

| Fixture | Redirects | Hazard if absent |
|---|---|---|
| `_isolate_health_writes` | `health.health_dir()` | mock health records overwrite the live dashboard |
| `_isolate_brain_mailbox` | `brain.brain_root()` | a real request lands in the running session's inbox, spends budget, and makes SPARK act |
| `_isolate_alive_heartbeat` | `PX_ALIVE_HEARTBEAT_DIR` | tests read the *live* robot's heartbeat and pass for the wrong reason |

Each redirects **only its own root**, not `PX_STATE_DIR` globally, because many
tests deliberately set `PX_STATE_DIR` themselves.

**The `isolated_project` fixture** is opt-in and isolates **subprocesses**. It
supplies an `env` dict with `PROJECT_ROOT`, `LOG_DIR`, `PX_SESSION_PATH`,
`PX_STATE_DIR`, `PX_BYPASS_SUDO=1`, `PX_VOICE_DEVICE=null`, and a pinned
night-silence window.

> **Session isolation is not yet on `master`.** In-process reads of
> `state/session.json` still reach the live file, which is why a live
> `spark_quiet_mode: true` reddens several `test_mind_utils` tests on the
> robot. An autouse `_isolate_session` fixture is landing via
> [#212](https://github.com/adrianwedd/spark/issues/210). Until it merges,
> isolate `PX_SESSION_PATH` before blaming a branch for those failures.

### The night-silence window must be pinned, or the suite is time-dependent

`isolated_project` sets `PX_NIGHT_SILENCE_START_H=99` (never true), because
`bin/tool-voice` evaluates policy for itself. Without it, **every subprocess
test of a speaking tool passes by day and returns `suppressed` after 19:00
Hobart.**

The env-var seam exists precisely because the enforcement points are
subprocesses — a test cannot monkeypatch inside `bin/tool-voice`. Tests that
mean to exercise night silence override **both** values.

### Targeted green is not repository green

Running `-k` on the tests you touched proves your change; it does not prove the
repository. Run the full suite before claiming done.

And a green suite does not prove the **live** paths. These require explicit
live evidence and cannot be inferred from tests:

- GPIO acquisition and handover
- the resident tmux sessions and their trust boundary
- audio actually reaching the speaker
- anything behind `sudo`

### Some tests are structural tripwires, not coverage

They fail on purpose when the code changes shape:

- `test_every_audio_producer_is_inventoried` — a new file reaching audio must
  be classified
- `test_reflection_awareness_json_is_allowlisted` — a new awareness key stays
  out of the prompt
- `test_launcher_renders_one_absolute_reply_spelling` — one spelling of
  `tool-brain-reply`
- `test_describe_scene_timeout_has_margin_over_claude` — pins a *relationship*
  and its surplus, not a literal

Do not "fix" these by updating the expected value. Fix the code, or make the
classification deliberately.

### `tests/test_policy_invariants.py` is not evolvable

It and `src/pxh/policy.py` are in `claude_session.BLACKLIST_FILES`. Evolvable
policy coverage lives in `tests/test_policy.py`. **Keep that split** — see
[architecture/policy-and-authority](architecture/policy-and-authority.md).

---

## Known limitations

- **A full run produces 1–2 failures that differ between runs** and pass in
  isolation: 15s subprocess timeouts under load, and a process-wide
  `time.sleep` patch in `test_race` catching leaked `test_api` threads.
  Re-run in isolation before believing any `test_race` / `test_api` /
  `test_evolve_coverage` failure.
- **Test runs write into the real `logs/`.** Millisecond-clustered entries, or
  ones naming `/tmp/pytest-of-pi/`, are artifacts rather than incidents.
- **Test runs can trip `px-post`'s in-memory 3-failure Bluesky auth disable.**
  `sudo systemctl restart px-post` afterwards.

---

## Why it looks like this

*History, not rule.*

Every autouse fixture here was added after a test run changed the live robot.
The health fixture was added when a suite run overwrote live health with mock
values and the dashboard reported whatever the tests last asserted. The brain
mailbox fixture was added because an unisolated test dropped a real request
into the running session's inbox and it was answered.

The heartbeat fixture is the subtlest of the three: `#192` moved the heartbeat
to tmpfs and `resolve_heartbeat_read_path()` prefers `/run/spark`
unconditionally — correct in production, but on a host where `px-alive` is
running it meant isolated tests read the live beat. Eight tests that passed on
a CI box failed on the robot, and would have passed there *for the wrong
reason* had the fixture been inverted.
