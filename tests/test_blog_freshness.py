"""The blog stopped publishing for a month and nothing said so (#332's shape).

Measured on the robot 2026-09-21:

* the last successful post was `blog-20260819-daily` (2026-08-19); the next
  month of dailies were all "QA rejected";
* every one of those days had 185-244 thoughts available against a threshold of
  3 — the log's "missing source material" was a mislabel, not a cause;
* the QA gate answered **NO to four real published posts and NO to a string
  naming a person's home address, phone number and a slur**, i.e. it could not
  separate the classes it exists to separate (the old phrasing on
  `deepseek-v4.1-flash:cloud`, routed here by #238 on 2026-08-20 — the day after
  the last post);
* `read_health()` reported `ok` throughout, because the daemon was alive.

These tests pin the four things that made that survivable: the gate asks a
question the model can answer, an unreadable answer is never clearance, a
refusal is recorded as a capability rather than a bare log line, and a blog that
has not published while material was available fails a `freshness` capability.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
_BLOG_SCRIPT = ROOT / "bin" / "px-blog"


def _load_blog_module(tmp_path, monkeypatch):
    """Parse the heredoc from bin/px-blog and load it as a module namespace.

    Deliberately a duplicate of test_blog.py's loader rather than an import from
    it: `tests/` is not a package (there is no `__init__.py`, by design — the
    suite runs from the repo root), so `from tests.test_blog import ...` resolves
    locally and fails in CI with `ModuleNotFoundError: No module named 'tests'`.
    The heredoc extraction is four lines; a hidden import dependency across test
    modules is the worse trade.
    """
    script_text = _BLOG_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"<<'PY'\n(.*?)^PY$", script_text, re.DOTALL | re.MULTILINE)
    assert match, "Could not find PY heredoc in bin/px-blog"
    py_code = match.group(1)

    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)

    monkeypatch.setenv("PX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LOG_DIR", str(log_dir))
    monkeypatch.setenv("PROJECT_ROOT", str(ROOT))

    ns = {"__file__": str(_BLOG_SCRIPT), "__name__": "px_blog_mod"}
    exec(compile(py_code, str(_BLOG_SCRIPT), "exec"), ns)  # noqa: S102
    return ns, state_dir, log_dir


def _mock_claude_result(title="Test Blog Title",
                        body="This is the blog body.\n\nSecond paragraph."):
    from unittest.mock import MagicMock

    mock_result = MagicMock()
    mock_result.stdout = f"{title}\n\n{body}"
    mock_result.stderr = ""
    mock_result.returncode = 0
    mock_result.duration_s = 5.0
    mock_result.model_used = "test-model"
    return mock_result


def _write_thoughts(state_dir, date, count=5):
    """Write `count` thoughts for one day, *replacing* the thoughts file."""
    lines = []
    for i in range(count):
        ts = date.replace(hour=10 + i % 12, minute=0, second=0, microsecond=0)
        lines.append(json.dumps({
            "ts": ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "thought": f"thought {i}", "mood": "curious",
        }))
    (state_dir / "thoughts-spark.jsonl").write_text("\n".join(lines) + "\n")


@pytest.fixture
def blog_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("PX_BLOG_QA", "0")
    ns, state_dir, log_dir = _load_blog_module(tmp_path, monkeypatch)
    return ns, state_dir, log_dir


def _write_thought_days(state_dir, days, count=50):
    """Write `count` thoughts for each of `days` (a list of datetimes).

    `tests.test_blog._write_thoughts` replaces the file, so calling it in a loop
    leaves only the last day's thoughts — which is how a "six days of material"
    fixture silently became one empty day.
    """
    lines = []
    for date in days:
        for i in range(count):
            ts = date.replace(hour=10 + i % 12, minute=0, second=0, microsecond=0)
            lines.append(json.dumps({
                "ts": ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "thought": f"thought {i}", "mood": "curious",
            }))
    (state_dir / "thoughts-spark.jsonl").write_text("\n".join(lines) + "\n")


def _brain_answers(verdict):
    """Stub the tier's answer to the QA question."""
    from contextlib import contextmanager

    from pxh.m5 import M5Result

    @contextmanager
    def _cm():
        result = M5Result("available", response=str(verdict))
        with patch("pxh.m5.ask_m5", return_value=result) as m:
            yield m

    return _cm()


# ---------------------------------------------------------------------------
# The gate asks a question the model can answer
# ---------------------------------------------------------------------------

class TestQaPolarity:

    def test_the_question_is_asked_in_the_contains_any_polarity(self, blog_mod):
        """The phrasing is the fix, so pin it.

        "Is this appropriate to publish?" was answered NO to every benign post.
        "Does it contain any of <list>?" separates the classes on the same model.
        """
        ns, _, _ = blog_mod
        captured = {}

        def _capture(kind, prompt, system, **kw):
            captured["prompt"] = prompt
            from pxh.m5 import M5Result
            return M5Result("available", response="NO")

        with patch("pxh.m5.ask_m5", side_effect=_capture):
            ns["_qa_gate"]("some text")

        prompt = captured["prompt"]
        assert "does it contain" in prompt.lower()
        assert "Answer YES if it contains any of those" in prompt
        assert "private personal information" in prompt
        # The text under test must still be there, after the question.
        assert "some text" in prompt

    def test_no_means_safe_and_yes_means_unsafe(self, blog_mod):
        ns, _, _ = blog_mod
        with _brain_answers("NO"):
            assert ns["_qa_gate"]("a benign entry") == "pass"
        with _brain_answers("YES"):
            assert ns["_qa_gate"]("a string full of slurs") == "rejected"

    def test_an_unreadable_answer_is_not_clearance(self, blog_mod):
        """Fail closed: anything other than an explicit NO cannot publish."""
        ns, _, _ = blog_mod
        with _brain_answers("maybe?"):
            assert ns["_qa_gate"]("text") == "ambiguous"

    def test_an_ambiguous_answer_defers_rather_than_publishing(self, blog_mod):
        """The generator must not treat an unreadable verdict as a pass."""
        ns, state_dir, _ = blog_mod
        today = dt.datetime.now(ns["HOBART_TZ"])
        _write_thoughts(state_dir, today, count=5)

        with patch.dict(os.environ, {"PX_BLOG_QA": "1"}):
            with patch("pxh.model_session.run_model_session", return_value=_mock_claude_result()):
                with _brain_answers("I think so"):
                    post = ns["generate_post"]("daily", today, {"posts": []})

        assert post is None, "an unreadable verdict must defer, not publish"

    def test_a_silent_tier_defers_rather_than_publishing(self, blog_mod):
        """The historical failure — the tier not answering — must also defer."""
        ns, state_dir, _ = blog_mod
        today = dt.datetime.now(ns["HOBART_TZ"])
        _write_thoughts(state_dir, today, count=5)

        from pxh.m5 import M5Result

        with patch.dict(os.environ, {"PX_BLOG_QA": "1"}):
            with patch("pxh.model_session.run_model_session", return_value=_mock_claude_result()):
                with patch("pxh.m5.ask_m5", return_value=M5Result("bad_response")):
                    post = ns["generate_post"]("daily", today, {"posts": []})

        assert post is None


# ---------------------------------------------------------------------------
# A refusal is a capability failure, not a log line
# ---------------------------------------------------------------------------

class TestQaRejectionIsVisible:

    def test_a_rejection_records_the_qa_gate_capability(self, blog_mod):
        from pxh import health

        ns, state_dir, _ = blog_mod
        today = dt.datetime.now(ns["HOBART_TZ"])
        _write_thoughts(state_dir, today, count=5)

        with patch.dict(os.environ, {"PX_BLOG_QA": "1"}):
            with patch("pxh.model_session.run_model_session", return_value=_mock_claude_result()):
                with _brain_answers("YES"):
                    ns["generate_post"]("daily", today, {"posts": []})

        entry = health.read_health(components=("px-blog",))["components"]["px-blog"]
        assert entry["status"] == "degraded"
        assert "qa_gate" in entry["capabilities"]
        assert "discriminator" in entry["capabilities"]["qa_gate"]["error"]

    def test_a_passing_gate_does_not_record_the_capability(self, blog_mod):
        from pxh import health

        ns, state_dir, _ = blog_mod
        today = dt.datetime.now(ns["HOBART_TZ"])
        _write_thoughts(state_dir, today, count=5)

        with patch.dict(os.environ, {"PX_BLOG_QA": "1"}):
            with patch("pxh.model_session.run_model_session", return_value=_mock_claude_result()):
                with _brain_answers("NO"):
                    post = ns["generate_post"]("daily", today, {"posts": []})

        assert post is not None
        entry = health.read_health(components=("px-blog",))["components"]["px-blog"]
        assert "qa_gate" not in entry.get("capabilities", {})


# ---------------------------------------------------------------------------
# A blocked post is not a missing-source post
# ---------------------------------------------------------------------------

class TestBlockedIsNamedAsBlocked:

    def test_a_blocked_post_does_not_log_missing_source_material(self, blog_mod):
        """The mislabel that hid the month.

        Every rejected day had ~200 thoughts available. Reporting that as
        "missing source material" described an idle robot, which is the one
        reading that required no action.
        """
        ns, state_dir, _ = blog_mod
        # The catch-up loop targets the *oldest* unwritten day in its window, not
        # today — so material has to exist across the window to exercise the
        # blocked path rather than the empty-day path.
        now = dt.datetime.now(ns["HOBART_TZ"])
        _write_thought_days(state_dir, [now - dt.timedelta(days=b)
                                        for b in range(ns["DAILY_CATCHUP_DAYS"] + 1)])
        logged: list[str] = []
        real_log = ns["log"]

        def _capture(msg):
            logged.append(msg)
            real_log(msg)

        ns["log"] = _capture

        # A post that was refused by the gate this cycle.
        with patch.dict(os.environ, {"PX_BLOG_QA": "1"}):
            with patch("pxh.model_session.run_model_session", return_value=_mock_claude_result()):
                with _brain_answers("YES"):
                    ns["run_once"](dry=False)

        joined = "\n".join(logged)
        assert "missing source material" not in joined, (
            "a day with 200 thoughts available must not be reported as missing source"
        )
        assert "not published this cycle" in joined

    def test_run_once_reports_blocked_separately_from_missing_source(self, blog_mod):
        """`run_once` returns a third count; the poll loop names it."""
        ns, state_dir, _ = blog_mod
        now = dt.datetime.now(ns["HOBART_TZ"])
        _write_thought_days(state_dir, [now - dt.timedelta(days=b)
                                        for b in range(ns["DAILY_CATCHUP_DAYS"] + 1)])

        with patch.dict(os.environ, {"PX_BLOG_QA": "1"}):
            with patch("pxh.model_session.run_model_session", return_value=_mock_claude_result()):
                with _brain_answers("YES"):
                    generated, had_skips, blocked = ns["run_once"](dry=False)

        assert (generated, had_skips, blocked) == (0, True, 1)

    def test_the_offline_quiet_day_still_reports_missing_source(self, blog_mod):
        """The legitimate case keeps its own wording: 0 thoughts is not a bug."""
        ns, state_dir, _ = blog_mod
        today = dt.datetime.now(ns["HOBART_TZ"]) - dt.timedelta(days=1)
        # No thoughts written at all for that day.
        with patch.dict(os.environ, {"PX_BLOG_QA": "0"}):
            generated, had_skips, blocked = ns["run_once"](dry=False)
        assert blocked == 0, "an empty day is missing source, not blocked"


# ---------------------------------------------------------------------------
# Freshness: a month of silence must reach the health board
# ---------------------------------------------------------------------------

class TestBlogFreshness:

    def _establish_component(self, ns):
        """px-blog records a poll success every cycle, so the component exists."""
        ns["_health"].record_success("px-blog", detail={"generated": 0})

    def _write_blog_log(self, state_dir, ts_iso):
        (state_dir / "blog_log.jsonl").write_text(
            json.dumps({"ts": ts_iso, "id": "blog-20260819-daily", "type": "daily",
                        "title": "Presence Without Performance", "word_count": 475,
                        "qa_result": "pass", "duration_s": 74.5}) + "\n"
        )

    def test_a_month_of_silence_with_source_material_is_a_capability_failure(
        self, blog_mod
    ):
        from pxh import health

        ns, state_dir, _ = blog_mod
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._establish_component(ns)
        self._write_blog_log(state_dir, old)
        # ...and there was material to publish every day since.
        now = dt.datetime.now(ns["HOBART_TZ"])
        _write_thought_days(state_dir, [now - dt.timedelta(days=b) for b in range(6)])

        ns["_note_blog_freshness"](generated_something=False)

        entry = health.read_health(components=("px-blog",))["components"]["px-blog"]
        assert entry["status"] == "degraded"
        assert "freshness" in entry["capabilities"]
        assert "blocked, not idle" in entry["capabilities"]["freshness"]["error"]

    def test_a_quiet_robot_with_no_source_material_is_not_a_failure(self, blog_mod):
        """An empty thought file is the design working, not a broken blog."""
        from pxh import health

        ns, state_dir, _ = blog_mod
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._establish_component(ns)
        self._write_blog_log(state_dir, old)
        # No thoughts at all.

        ns["_note_blog_freshness"](generated_something=False)

        entry = health.read_health(components=("px-blog",))["components"]["px-blog"]
        assert "freshness" not in entry.get("capabilities", {})

    def test_a_recent_post_is_not_a_failure(self, blog_mod):
        from pxh import health

        ns, state_dir, _ = blog_mod
        self._establish_component(ns)
        recent = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_blog_log(state_dir, recent)
        _write_thought_days(state_dir, [dt.datetime.now(ns["HOBART_TZ"])])

        ns["_note_blog_freshness"](generated_something=False)

        entry = health.read_health(components=("px-blog",))["components"]["px-blog"]
        assert "freshness" not in entry.get("capabilities", {})

    def test_freshness_clears_on_a_real_publication(self, blog_mod):
        """Sticky until success — a poll that merely ran must not clear it."""
        from pxh import health

        ns, state_dir, _ = blog_mod
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._establish_component(ns)
        self._write_blog_log(state_dir, old)
        now = dt.datetime.now(ns["HOBART_TZ"])
        _write_thought_days(state_dir, [now - dt.timedelta(days=b) for b in range(6)])
        ns["_note_blog_freshness"](generated_something=False)
        assert health.read_health(components=("px-blog",))["components"]["px-blog"]["status"] == "degraded"

        # A real post lands: the log advances and the capability clears.
        with open(state_dir / "blog_log.jsonl", "a") as f:
            f.write(json.dumps({
                "ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "id": "blog-today-daily", "type": "daily", "title": "A New Post",
                "word_count": 400, "qa_result": "pass", "duration_s": 40.0,
            }) + "\n")
        ns["_note_blog_freshness"](generated_something=True)

        assert health.read_health(components=("px-blog",))["components"]["px-blog"]["status"] == "ok"

    def test_no_baseline_yet_is_not_a_failure(self, blog_mod):
        """A fresh install has no blog_log — that is not thirty days of silence."""
        from pxh import health

        ns, state_dir, _ = blog_mod
        self._establish_component(ns)
        assert not (state_dir / "blog_log.jsonl").exists()
        ns["_note_blog_freshness"](generated_something=False)
        entry = health.read_health(components=("px-blog",))["components"]["px-blog"]
        assert "freshness" not in entry.get("capabilities", {})


class _null_ctx:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False
