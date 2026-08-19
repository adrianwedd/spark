# Testing

## The rule

> **A suite that only ever runs in one environment cannot distinguish
> "my code is correct" from "my machine is the code."**

Every practice below follows from that one line.

## Where tests run

CI is the gate. `.github/workflows/tests.yml` runs `pytest -m "not live"` on
every pull request, on Python 3.11 to match the Pi (Bookworm ships 3.11.2 — a
version skew would let CI pass on syntax the robot cannot run).

The workflow passes nothing else on purpose. Pre-setting `LOG_DIR`,
`PX_BYPASS_SUDO`, or any other robot-shaped variable in the job would mask the
exact failures the job exists to surface.

**Do not run the suite on SPARK.** The robot is the deployment target, not the
test runner. A green run on the Pi tells you the tests agree with the Pi, which
is the one thing the suite must not be allowed to assume.

Live hardware tests are marked `live`, deselected in CI, and run deliberately:

```bash
sudo .venv/bin/python -m pytest tests/ -m live -v   # on the Pi, on purpose
```

## Two ways a test lies

Both are invisible in a single-environment suite, because in that suite the
thing being assumed is always present when you look.

### 1. The environment leaks *into* the test

The assertion depends on something true of this machine rather than something
true of the code. It passes here and would pass nowhere else, or — worse — it
passes here and asserts nothing anywhere else.

Worked examples, all found by the first CI run:

| Symptom | What was actually asserted |
|---|---|
| `test_mind_fallback` ×4 | `claude` is on `$PATH` here, so `call_claude_haiku` reached the mocked `subprocess.run`. Elsewhere the tier short-circuits at `mind.py:2445` and the Claude fallback these tests are named for never happens. |
| `test_tool_wander_sudo_env_carries_home` | The literal string `HOME=/home/pi`. `bin/tool-wander:81` reads `pwd.getpwuid(os.getuid()).pw_dir` precisely so a clobbered `$HOME` cannot reintroduce the bug — the test pinned this robot's value instead of the code's property. |
| `test_tool_voice_lock_timeout` | Lock contention, but only on a host with `espeak`. Without a player `bin/tool-voice:229` takes the `not DEFAULT_PLAYER` branch, never enters `with VOICE_LOCK`, and reports ok. |

The fix is never to make the runner more robot-like. Installing `claude` and
`espeak` on CI would have turned all six green while leaving every assertion
exactly as false as it was. Use the seam instead — `PX_CLAUDE_BIN`,
`PX_VOICE_PLAYER`, `pwd.getpwuid()` — and force the precondition the test
claims to be testing.

**A test named for a fallback path must force the state that causes the
fallback.** Depending on whether an executable happens to be on `PATH` is not a
precondition, it is a coincidence.

### 2. The test leaks *out* into production

The inverse, and the subject of #221: a test whose effects escape the test.
State, sockets, log files, and child processes are all state.

The `TestRaceEndpoint` failures are the concurrency-shaped version. `POST
/api/v1/race/{action}` returns 202 as soon as `_run_race` reaches the executor;
`subprocess.Popen` fires later on a pool thread. A mock scoped to the request:

```python
with patch("pxh.api.subprocess.Popen", side_effect=fake_popen):
    resp = api_client.post(...)      # 202 — the work is merely queued
# mock uninstalled here, job still pending
while not captured and time.monotonic() < deadline:   # waiting for a mock
    time.sleep(0.05)                                  # that no longer exists
```

…spends its timeout waiting for a mock it removed, and on the losing path spawns
the real `bin/px-race`. The rule: **a mock must outlive the work it mocks.** If
the assertion is about a background effect, the patch stays installed until the
effect is observed — see `_mock_popen` / `_await_spawn` in `tests/test_api.py`.

## Adding a test

1. Name the precondition and force it. If the test needs a binary, a player, or
   a socket, set the seam; do not hope for it.
2. Assert the code's property, not this host's value.
3. If the behaviour happens off the request thread, hold the mock until you have
   observed it.
4. Mark it `live` only if it genuinely requires hardware. `live` is a deselect,
   not a place to hide flakes.
5. Let CI tell you whether it works. That is the entire point.
