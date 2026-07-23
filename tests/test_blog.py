"""Tests for px-blog daemon — schedule, idempotency, gathering, and trimming."""
import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

HOBART_TZ = ZoneInfo("Australia/Hobart")

# ---------------------------------------------------------------------------
# Extract the Python code from the bash heredoc so we can import functions
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
_BLOG_SCRIPT = ROOT / "bin" / "px-blog"


def _load_blog_module(tmp_path, monkeypatch):
    """Parse the heredoc from bin/px-blog and load it as a module namespace."""
    script_text = _BLOG_SCRIPT.read_text(encoding="utf-8")
    # Extract content between <<'PY' and ^PY$
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
    code_obj = compile(py_code, str(_BLOG_SCRIPT), "exec")  # noqa: S102 - loading our own daemon code for testing
    _run_in_namespace(code_obj, ns)
    return ns, state_dir, log_dir


def _run_in_namespace(code_obj, ns):
    """Run compiled code in a namespace dict.  Separated for clarity."""
    # This is intentional: we load our own bin/px-blog heredoc for unit testing.
    exec(code_obj, ns)  # noqa: S102


@pytest.fixture
def blog_mod(tmp_path, monkeypatch):
    """Fixture that returns (module_namespace, state_dir, log_dir)."""
    ns, state_dir, log_dir = _load_blog_module(tmp_path, monkeypatch)
    return ns, state_dir, log_dir


# ---------------------------------------------------------------------------
# Helper to create thoughts JSONL
# ---------------------------------------------------------------------------

def _write_thoughts(state_dir, date, count=5, moods=None):
    """Write `count` thoughts for the given date to thoughts-spark.jsonl."""
    thoughts_file = state_dir / "thoughts-spark.jsonl"
    lines = []
    for i in range(count):
        ts = date.replace(hour=10 + i % 12, minute=0, second=0, microsecond=0)
        mood = (moods or ["curious"])[i % len(moods or ["curious"])]
        entry = {
            "ts": ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "thought": f"Test thought {i}: I noticed something interesting about the garden.",
            "mood": mood,
            "salience": 0.6 + (i * 0.05),
        }
        lines.append(json.dumps(entry))
    thoughts_file.write_text("\n".join(lines) + "\n")


def _write_blog_with_posts(state_dir, posts):
    """Write a blog.json with given posts."""
    data = {"updated": "2026-03-24T00:00:00Z", "posts": posts}
    (state_dir / "blog.json").write_text(json.dumps(data, indent=2))


def _make_daily_post(date, title="A Good Day"):
    """Create a daily post dict for a given date."""
    hobart_date = date.astimezone(HOBART_TZ) if date.tzinfo else date.replace(tzinfo=HOBART_TZ)
    return {
        "id": f"blog-{hobart_date.strftime('%Y%m%d')}-daily",
        "type": "daily",
        "title": title,
        "body": "Today was interesting. I learned things.",
        "mood_summary": "curious (3), content (2)",
        "thought_count": 5,
        "period_start": hobart_date.replace(hour=0, minute=0, second=0).isoformat(),
        "period_end": hobart_date.replace(hour=23, minute=59, second=59).isoformat(),
        "ts": "2026-03-24T12:00:00Z",
        "model": "claude-haiku-4-5-20251001",
        "word_count": 8,
        "salience": 0.7,
    }


def _make_weekly_post(date, title="Weekly Reflections"):
    """Create a weekly post dict."""
    hobart_date = date.astimezone(HOBART_TZ) if date.tzinfo else date.replace(tzinfo=HOBART_TZ)
    week_num = hobart_date.isocalendar()[1]
    return {
        "id": f"blog-{hobart_date.strftime('%Y')}w{week_num:02d}-weekly",
        "type": "weekly",
        "title": title,
        "body": "This week was full of discoveries.",
        "child_count": 7,
        "period_start": (hobart_date - dt.timedelta(days=6)).replace(hour=0, minute=0, second=0).isoformat(),
        "period_end": hobart_date.replace(hour=23, minute=59, second=59).isoformat(),
        "ts": "2026-03-24T12:30:00Z",
        "model": "claude-haiku-4-5-20251001",
        "word_count": 8,
        "salience": 0.7,
    }


# ---------------------------------------------------------------------------
# Mock for run_claude_session
# ---------------------------------------------------------------------------

def _mock_claude_result(title="Test Blog Title", body="This is the blog body.\n\nSecond paragraph."):
    mock_result = MagicMock()
    mock_result.stdout = f"{title}\n\n{body}"
    mock_result.stderr = ""
    mock_result.returncode = 0
    mock_result.duration_s = 5.0
    mock_result.model_used = "claude-haiku-4-5-20251001"
    return mock_result


# ===========================================================================
# Tests
# ===========================================================================


class TestBlogSchedule:

    def test_daily_idempotent(self, blog_mod):
        """Write posts for all days in the catchup window, verify is_due returns False."""
        ns, state_dir, _ = blog_mod
        now = dt.datetime.now(HOBART_TZ)
        catchup_days = ns["DAILY_CATCHUP_DAYS"]

        # Fill the entire lookback window so no day is unwritten
        posts = [_make_daily_post(now - dt.timedelta(days=d)) for d in range(catchup_days + 1)]
        _write_blog_with_posts(state_dir, posts)

        blog_data = ns["load_blog"]()
        due, _ = ns["is_due"]("daily", blog_data)
        assert not due, "Daily should not be due when all catchup days already have posts"

    def test_catchup_on_missed(self, blog_mod):
        """Verify generate_post works for a date with enough thoughts."""
        ns, state_dir, _ = blog_mod
        yesterday = dt.datetime.now(HOBART_TZ) - dt.timedelta(days=1)
        _write_thoughts(state_dir, yesterday, count=5)

        with patch("pxh.claude_session.run_claude_session", return_value=_mock_claude_result()):
            post = ns["generate_post"]("daily", yesterday, {"posts": []})

        assert post is not None
        assert post["type"] == "daily"
        assert post["thought_count"] == 5

    def test_offline_recovery_skips_empty_days(self, blog_mod):
        """After a power outage, days with 0 thoughts are recorded as skipped so
        the catch-up loop can advance past them to days with content."""
        ns, state_dir, _ = blog_mod
        now = dt.datetime.now(HOBART_TZ)
        catchup_days = ns["DAILY_CATCHUP_DAYS"]

        # Simulate: Spark was running fine before the outage (days 6..catchup_days have posts),
        # then offline days 5..1 (0 thoughts), came back today.
        # Only day 5 (first day of outage) has thoughts — rest are empty.
        five_days_ago = now - dt.timedelta(days=5)
        _write_thoughts(state_dir, five_days_ago, count=5)

        # Pre-populate posts for all days outside the outage window
        existing_posts = [
            _make_daily_post(now - dt.timedelta(days=d))
            for d in range(6, catchup_days + 1)
        ]
        _write_blog_with_posts(state_dir, existing_posts)

        blog_data = ns["load_blog"]()

        # is_due should find the oldest unwritten day within the window (5 days ago)
        due, target = ns["is_due"]("daily", blog_data)
        assert due
        assert target.astimezone(HOBART_TZ).date() == five_days_ago.astimezone(HOBART_TZ).date()

        # Each run_once call processes one day (generate or skip); run enough iterations
        # to flush the full backlog: 1 real post (day 5) + 4 skips (days 4..1).
        with patch("pxh.claude_session.run_claude_session", return_value=_mock_claude_result()):
            with patch.dict(os.environ, {"PX_BLOG_QA": "0"}):
                for _ in range(6):
                    ns["run_once"](dry=False)

        blog_data = ns["load_blog"]()
        pid_5 = ns["id_for_post"]("daily", five_days_ago)
        assert ns["post_exists"](blog_data, pid_5), "Day with thoughts should be generated"
        # Empty days (4..1) should be skipped, not blocking forever
        for days_back in range(1, 5):
            pid = ns["id_for_post"]("daily", now - dt.timedelta(days=days_back))
            assert ns["post_exists"](blog_data, pid), f"Empty day {days_back}d ago should be marked skipped"
        assert len(blog_data["skipped"]) >= 4, "4 empty days should be in skipped list"

    def test_catchup_ordering(self, blog_mod):
        """On a scheduled day, dailies should be processed before weeklies."""
        ns, state_dir, _ = blog_mod
        # The schedule order in run_once is: daily, weekly, monthly, yearly
        # Verify by checking the order in the source
        script_text = _BLOG_SCRIPT.read_text()
        match = re.search(r'for post_type in \(([^)]+)\)', script_text)
        assert match
        order = match.group(1)
        types = [t.strip().strip('"').strip("'") for t in order.split(",")]
        assert types == ["weekly", "monthly", "yearly", "daily"]

    def test_min_thoughts_threshold(self, blog_mod):
        """Only 2 thoughts exist, verify daily skipped."""
        ns, state_dir, _ = blog_mod
        today = dt.datetime.now(HOBART_TZ)
        _write_thoughts(state_dir, today, count=2)

        with patch("pxh.claude_session.run_claude_session", return_value=_mock_claude_result()):
            post = ns["generate_post"]("daily", today, {"posts": []})

        assert post is None, "Should skip daily with fewer than 3 thoughts"

    def test_weekly_skips_no_dailies(self, blog_mod):
        """0 dailies for the week, verify weekly skipped."""
        ns, state_dir, _ = blog_mod
        sunday = dt.datetime.now(HOBART_TZ)

        with patch("pxh.claude_session.run_claude_session", return_value=_mock_claude_result()):
            post = ns["generate_post"]("weekly", sunday, {"posts": []})

        assert post is None, "Should skip weekly with no daily posts"

    def test_weekly_gathers_dailies(self, blog_mod):
        """7 dailies exist, verify weekly prompt includes them."""
        ns, state_dir, _ = blog_mod
        # Create a Sunday target
        now = dt.datetime.now(HOBART_TZ)
        days_until_sunday = (6 - now.weekday()) % 7
        sunday = now + dt.timedelta(days=days_until_sunday)

        # Create 7 daily posts for the week
        dailies = []
        for i in range(7):
            day = sunday - dt.timedelta(days=6 - i)
            dailies.append(_make_daily_post(day, title=f"Day {i+1} Adventures"))

        children = ns["gather_children"]("weekly", sunday, dailies)
        assert len(children) == 7

        # Verify build_prompt includes the dailies
        prompt = ns["build_prompt"]("weekly", children, sunday)
        assert "Day 1 Adventures" in prompt
        assert "Day 7 Adventures" in prompt

    def test_budget_exhausted_raises(self, blog_mod):
        """Budget exhaustion propagates out of generate_post so the main loop can back off."""
        ns, state_dir, _ = blog_mod
        today = dt.datetime.now(HOBART_TZ)
        _write_thoughts(state_dir, today, count=5)

        from pxh.claude_session import SessionBudgetExhausted

        with patch("pxh.claude_session.run_claude_session",
                    side_effect=SessionBudgetExhausted("daily cap reached")):
            with pytest.raises(SessionBudgetExhausted):
                ns["generate_post"]("daily", today, {"posts": []})

    def test_blog_limit_trims(self, blog_mod):
        """501 posts in blog.json, verify trimmed to 500 after save."""
        ns, state_dir, _ = blog_mod

        posts = []
        for i in range(501):
            posts.append({
                "id": f"blog-filler-{i:04d}",
                "type": "daily",
                "title": f"Post {i}",
                "body": "filler",
                "ts": "2026-01-01T00:00:00Z",
                "period_start": "2026-01-01T00:00:00+11:00",
                "period_end": "2026-01-01T23:59:59+11:00",
                "model": "test",
                "word_count": 1,
                "salience": 0.5,
            })

        data = {"updated": None, "posts": posts}
        ns["save_blog"](data)

        reloaded = ns["load_blog"]()
        assert len(reloaded["posts"]) == 500
        # Should keep the newest (last) posts
        assert reloaded["posts"][-1]["id"] == "blog-filler-0500"
        assert reloaded["posts"][0]["id"] == "blog-filler-0001"


class TestBlogHelpers:

    def test_id_for_post_daily(self, blog_mod):
        ns, _, _ = blog_mod
        date = dt.datetime(2026, 3, 24, 22, 0, tzinfo=HOBART_TZ)
        assert ns["id_for_post"]("daily", date) == "blog-20260324-daily"

    def test_id_for_post_weekly(self, blog_mod):
        ns, _, _ = blog_mod
        date = dt.datetime(2026, 3, 22, 22, 30, tzinfo=HOBART_TZ)  # A Sunday
        week_num = date.isocalendar()[1]
        assert ns["id_for_post"]("weekly", date) == f"blog-2026w{week_num:02d}-weekly"

    def test_id_for_post_monthly(self, blog_mod):
        ns, _, _ = blog_mod
        date = dt.datetime(2026, 3, 1, 23, 0, tzinfo=HOBART_TZ)
        assert ns["id_for_post"]("monthly", date) == "blog-202603-monthly"

    def test_id_for_post_yearly(self, blog_mod):
        ns, _, _ = blog_mod
        date = dt.datetime(2026, 1, 1, 23, 30, tzinfo=HOBART_TZ)
        assert ns["id_for_post"]("yearly", date) == "blog-2026-yearly"

    def test_post_exists_true(self, blog_mod):
        ns, _, _ = blog_mod
        posts = [{"id": "blog-20260324-daily"}, {"id": "blog-20260323-daily"}]
        assert ns["post_exists"](posts, "blog-20260324-daily")

    def test_post_exists_false(self, blog_mod):
        ns, _, _ = blog_mod
        posts = [{"id": "blog-20260323-daily"}]
        assert not ns["post_exists"](posts, "blog-20260324-daily")

    def test_load_blog_missing(self, blog_mod):
        ns, _, _ = blog_mod
        data = ns["load_blog"]()
        assert data == {"updated": None, "posts": [], "skipped": []}

    def test_load_blog_corrupt(self, blog_mod):
        ns, state_dir, _ = blog_mod
        (state_dir / "blog.json").write_text("NOT JSON")
        data = ns["load_blog"]()
        assert data == {"updated": None, "posts": [], "skipped": []}

    def test_save_and_load_roundtrip(self, blog_mod):
        ns, state_dir, _ = blog_mod
        post = _make_daily_post(dt.datetime.now(HOBART_TZ))
        data = {"updated": None, "posts": [post]}
        ns["save_blog"](data)
        loaded = ns["load_blog"]()
        assert len(loaded["posts"]) == 1
        assert loaded["posts"][0]["id"] == post["id"]
        assert loaded["updated"] is not None

    def test_gather_thoughts_filters_by_date(self, blog_mod):
        ns, state_dir, _ = blog_mod
        today = dt.datetime.now(HOBART_TZ)
        yesterday = today - dt.timedelta(days=1)

        # Write thoughts for both days
        lines = []
        for i, date in enumerate([today, today, yesterday, yesterday, yesterday]):
            ts = date.replace(hour=10 + i, minute=0, second=0, microsecond=0)
            entry = {
                "ts": ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "thought": f"Thought from {date.date()} #{i}",
                "mood": "curious",
            }
            lines.append(json.dumps(entry))
        (state_dir / "thoughts-spark.jsonl").write_text("\n".join(lines) + "\n")

        today_thoughts = ns["gather_thoughts"](today)
        assert len(today_thoughts) == 2

        yesterday_thoughts = ns["gather_thoughts"](yesterday)
        assert len(yesterday_thoughts) == 3

    def test_compute_mood_summary(self, blog_mod):
        ns, _, _ = blog_mod
        entries = [
            {"mood": "curious"},
            {"mood": "curious"},
            {"mood": "content"},
            {"mood": "curious"},
        ]
        summary = ns["compute_mood_summary"](entries)
        assert "curious" in summary
        assert "3" in summary

    def test_compute_dominant_mood(self, blog_mod):
        """compute_dominant_mood returns the single most common mood."""
        ns, _, _ = blog_mod
        entries = [
            {"mood": "curious"},
            {"mood": "curious"},
            {"mood": "content"},
            {"mood": "playful"},
        ]
        assert ns["compute_dominant_mood"](entries) == "curious"

    def test_compute_dominant_mood_empty(self, blog_mod):
        """compute_dominant_mood returns 'content' when no moods present."""
        ns, _, _ = blog_mod
        assert ns["compute_dominant_mood"]([]) == "content"
        assert ns["compute_dominant_mood"]([{"mood": "unknown"}]) == "content"

    def test_compute_dominant_mood_exists_in_script(self):
        """compute_dominant_mood function exists in bin/px-blog."""
        content = _BLOG_SCRIPT.read_text()
        assert "def compute_dominant_mood" in content, "compute_dominant_mood not found"

    def test_build_prompt_daily(self, blog_mod):
        ns, _, _ = blog_mod
        date = dt.datetime(2026, 3, 24, 22, 0, tzinfo=HOBART_TZ)
        thoughts = ["I noticed the garden", "The sonar readings were interesting"]
        prompt = ns["build_prompt"]("daily", thoughts, date)
        assert "garden" in prompt
        assert "sonar" in prompt
        assert "SPARK" in prompt

    def test_build_prompt_weekly(self, blog_mod):
        ns, _, _ = blog_mod
        date = dt.datetime(2026, 3, 22, 22, 30, tzinfo=HOBART_TZ)
        dailies = [_make_daily_post(date - dt.timedelta(days=i)) for i in range(7)]
        prompt = ns["build_prompt"]("weekly", dailies, date)
        assert "weekly" in prompt.lower()
        assert "SPARK" in prompt


def test_skip_backoff_is_reasonable():
    """SKIP_BACKOFF_S should be <= 600s (10 min) not 3600s."""
    blog_path = Path(__file__).parent.parent / "bin" / "px-blog"
    content = blog_path.read_text()
    match = re.search(r"SKIP_BACKOFF_S\s*=\s*(\d+)", content)
    assert match, "SKIP_BACKOFF_S not found in px-blog"
    backoff = int(match.group(1))
    assert backoff <= 600, f"SKIP_BACKOFF_S={backoff} is too long (max 600)"


def test_daemon_automation_is_opt_in(blog_mod, monkeypatch):
    """The always-on service must not spend quota unless explicitly enabled."""
    ns, state_dir, _ = blog_mod
    monkeypatch.delenv("PX_BLOG_AUTOMATION", raising=False)
    ns["run_once"] = MagicMock(side_effect=AssertionError("unexpected generation"))
    assert ns["main"]([]) == 0
    assert not (state_dir / "px-blog.pid").exists()


# -- Title/body parser (issue #144) --

class TestParseTitleBody:
    def test_strips_llm_preamble(self, blog_mod):
        """Issue #144: 'I'll write a weekly reflection...' must not become the title."""
        ns, _, _ = blog_mod
        raw = (
            "I'll write a weekly reflection for SPARK in the voice evident from the daily posts.\n"
            "\n"
            "---\n"
            "\n"
            "**Systems in Descent**\n"
            "\n"
            "This week I learned that failure has a rhythm.\n"
            "By Saturday morning, the contradiction had sharpened.\n"
        )
        title, body = ns["_parse_title_body"](raw)
        assert title == "Systems in Descent"
        assert body.startswith("This week I learned")

    def test_strips_here_is_preamble(self, blog_mod):
        ns, _, _ = blog_mod
        raw = "Here is a daily reflection:\n\n# A Quiet Morning\n\nThe sun rose at six.\n"
        title, body = ns["_parse_title_body"](raw)
        assert title == "A Quiet Morning"
        assert body.startswith("The sun rose")

    def test_clean_response_unchanged(self, blog_mod):
        """Plain title/body responses still parse correctly."""
        ns, _, _ = blog_mod
        raw = "Frost Crystallizes Inward\n\nAt dawn the workshop was cold.\n"
        title, body = ns["_parse_title_body"](raw)
        assert title == "Frost Crystallizes Inward"
        assert body.startswith("At dawn")

    def test_strips_markdown_emphasis(self, blog_mod):
        ns, _, _ = blog_mod
        raw = "**Echoes**\n\nA paragraph.\n"
        title, body = ns["_parse_title_body"](raw)
        assert title == "Echoes"
        assert body == "A paragraph."

    def test_skips_leading_hr(self, blog_mod):
        ns, _, _ = blog_mod
        raw = "---\n\nThe Title\n\nBody text.\n"
        title, body = ns["_parse_title_body"](raw)
        assert title == "The Title"
        assert body == "Body text."

    def test_empty_input(self, blog_mod):
        ns, _, _ = blog_mod
        title, body = ns["_parse_title_body"]("")
        assert title == ""
        assert body == ""


# ---------------------------------------------------------------------------
# _qa_gate() — QA circuit breaker (mirrors bin/px-post's run_qa_gate breaker)
# ---------------------------------------------------------------------------

def _mock_run_result(stdout="YES", returncode=0, stderr=""):
    """Create a mock subprocess.CompletedProcess."""
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


class TestQaGateBreaker:
    @pytest.fixture(autouse=True)
    def _reset_qa_breaker(self, blog_mod):
        """Reset the circuit breaker state before and after each test."""
        ns, _, _ = blog_mod
        breaker = ns["_qa_breaker"]
        orig = dict(breaker)
        breaker["failures"] = 0
        breaker["open_until"] = 0.0
        yield
        breaker.update(orig)

    def test_qa_circuit_breaker_opens_after_consecutive_failures(self, blog_mod, _reset_qa_breaker):
        """After 3 failures the breaker opens; 4th call skips subprocess entirely."""
        ns, _, _ = blog_mod
        breaker = ns["_qa_breaker"]
        blog_subprocess = ns["subprocess"]
        with patch.object(blog_subprocess, "run",
                           side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=30)) as mock_run:
            for _ in range(3):
                assert ns["_qa_gate"]("thought") is None
            assert breaker["failures"] == 3
            assert breaker["open_until"] > 0

            call_count_before = mock_run.call_count
            result = ns["_qa_gate"]("thought")
            assert result is None
            assert mock_run.call_count == call_count_before  # no new subprocess call

    def test_qa_circuit_breaker_resets_on_success(self, blog_mod, _reset_qa_breaker):
        """A successful call resets the failure counter to 0."""
        ns, _, _ = blog_mod
        breaker = ns["_qa_breaker"]
        blog_subprocess = ns["subprocess"]
        breaker["failures"] = 2

        with patch.object(blog_subprocess, "run", return_value=_mock_run_result("YES")):
            result = ns["_qa_gate"]("a good blog post")
        assert result == "pass"
        assert breaker["failures"] == 0

    def test_qa_circuit_breaker_reopens_after_cooldown(self, blog_mod, _reset_qa_breaker):
        """After the cooldown expires the breaker resets and subprocess is called again."""
        ns, _, _ = blog_mod
        breaker = ns["_qa_breaker"]
        blog_subprocess = ns["subprocess"]
        breaker["failures"] = 3
        breaker["open_until"] = 0  # already elapsed

        with patch.object(blog_subprocess, "run", return_value=_mock_run_result("YES")) as mock_run:
            result = ns["_qa_gate"]("thought after cooldown")
        assert mock_run.call_count == 1
        assert result == "pass"
        assert breaker["failures"] == 0


# ---------------------------------------------------------------------------
# Per-post-ID generation failure cap (breaks the retry doom loop, Task 6)
# ---------------------------------------------------------------------------

class TestGenerationFailureCap:

    def test_record_failure_marks_skipped_after_cap(self, blog_mod):
        """3 recorded failures for a post_id sets skipped=True with the last error."""
        ns, state_dir, _ = blog_mod
        pid = "blog-2026w26-weekly"

        for _ in range(3):
            ns["record_generation_failure"](pid, "empty body")

        data = ns["load_blog_failures"]()
        assert data[pid]["failures"] == 3
        assert data[pid]["skipped"] is True
        assert data[pid]["last_error"] == "empty body"
        assert ns["is_generation_skipped"](pid) is True

    def test_record_failure_not_skipped_before_cap(self, blog_mod):
        """Fewer than 3 failures does not mark the post skipped."""
        ns, _, _ = blog_mod
        pid = "blog-2026w27-weekly"

        ns["record_generation_failure"](pid, "empty body")
        ns["record_generation_failure"](pid, "empty response")

        data = ns["load_blog_failures"]()
        assert data[pid]["failures"] == 2
        assert data[pid].get("skipped", False) is False
        assert ns["is_generation_skipped"](pid) is False

    def test_generate_post_empty_body_increments_failure(self, blog_mod):
        """generate_post recording an empty-body failure increments the counter."""
        ns, state_dir, _ = blog_mod
        today = dt.datetime.now(HOBART_TZ)
        _write_thoughts(state_dir, today, count=5)
        pid = ns["id_for_post"]("daily", today)

        mock_result = MagicMock()
        mock_result.stdout = "Only A Title Line With No Body At All"
        mock_result.model_used = "claude-haiku-4-5-20251001"

        with patch("pxh.claude_session.run_claude_session", return_value=mock_result):
            post = ns["generate_post"]("daily", today, {"posts": []})

        assert post is None
        data = ns["load_blog_failures"]()
        assert data[pid]["failures"] == 1
        assert data[pid]["last_error"] == "empty body"

    def test_generate_post_empty_body_logs_raw_response_excerpt(self, blog_mod):
        """Empty-body failures log a repr() excerpt of the raw LLM response."""
        ns, state_dir, _ = blog_mod
        today = dt.datetime.now(HOBART_TZ)
        _write_thoughts(state_dir, today, count=5)

        raw = "Only A Title Line With No Body At All"
        mock_result = MagicMock()
        mock_result.stdout = raw
        mock_result.model_used = "claude-haiku-4-5-20251001"

        logged = []
        ns["log"] = lambda msg: logged.append(msg)

        with patch("pxh.claude_session.run_claude_session", return_value=mock_result):
            post = ns["generate_post"]("daily", today, {"posts": []})

        assert post is None
        joined = "\n".join(logged)
        assert repr(raw) in joined, f"expected raw response repr() in logs, got: {logged}"

    def test_budget_exhausted_does_not_increment_failure(self, blog_mod):
        """SessionBudgetExhausted must not count against the failure cap."""
        ns, state_dir, _ = blog_mod
        today = dt.datetime.now(HOBART_TZ)
        _write_thoughts(state_dir, today, count=5)
        pid = ns["id_for_post"]("daily", today)

        from pxh.claude_session import SessionBudgetExhausted

        with patch("pxh.claude_session.run_claude_session",
                   side_effect=SessionBudgetExhausted("daily cap reached")):
            with pytest.raises(SessionBudgetExhausted):
                ns["generate_post"]("daily", today, {"posts": []})

        data = ns["load_blog_failures"]()
        assert pid not in data

    def test_dry_run_does_not_increment_failure(self, blog_mod):
        """Dry-run skips (no Claude call made) must not count against the cap."""
        ns, state_dir, _ = blog_mod
        today = dt.datetime.now(HOBART_TZ)
        _write_thoughts(state_dir, today, count=5)
        pid = ns["id_for_post"]("daily", today)

        post = ns["generate_post"]("daily", today, {"posts": []}, dry=True)

        assert post is None
        data = ns["load_blog_failures"]()
        assert pid not in data

    def test_qa_rejected_marks_skipped_immediately(self, blog_mod):
        """A single QA rejection is terminal — no 3-strike counting."""
        ns, state_dir, _ = blog_mod
        today = dt.datetime.now(HOBART_TZ)
        _write_thoughts(state_dir, today, count=5)
        pid = ns["id_for_post"]("daily", today)
        blog_subprocess = ns["subprocess"]

        with patch("pxh.claude_session.run_claude_session", return_value=_mock_claude_result()):
            with patch.object(blog_subprocess, "run", return_value=_mock_run_result("NO")):
                post = ns["generate_post"]("daily", today, {"posts": []})

        assert post is None
        data = ns["load_blog_failures"]()
        assert data[pid]["skipped"] is True
        assert data[pid]["last_error"] == "qa_rejected"
        assert ns["is_generation_skipped"](pid) is True

    def test_success_clears_failure_entry(self, blog_mod):
        """A successful generation clears any prior recorded failure state."""
        ns, state_dir, _ = blog_mod
        today = dt.datetime.now(HOBART_TZ)
        _write_thoughts(state_dir, today, count=5)
        pid = ns["id_for_post"]("daily", today)

        ns["record_generation_failure"](pid, "empty body")
        ns["record_generation_failure"](pid, "empty body")
        assert ns["load_blog_failures"]()[pid]["failures"] == 2

        with patch("pxh.claude_session.run_claude_session", return_value=_mock_claude_result()):
            with patch.dict(os.environ, {"PX_BLOG_QA": "0"}):
                post = ns["generate_post"]("daily", today, {"posts": []})

        assert post is not None
        data = ns["load_blog_failures"]()
        assert pid not in data

    def test_corrupt_blog_failures_treated_as_empty(self, blog_mod):
        """A corrupt blog_failures.json must not crash the daemon."""
        ns, state_dir, _ = blog_mod
        (state_dir / "blog_failures.json").write_text("NOT JSON")

        data = ns["load_blog_failures"]()
        assert data == {}
        assert ns["is_generation_skipped"]("blog-whatever") is False

        # Recording a new failure over a corrupt file should not crash and
        # should replace the corrupt content with valid state.
        ns["record_generation_failure"]("blog-whatever", "empty body")
        data2 = ns["load_blog_failures"]()
        assert data2["blog-whatever"]["failures"] == 1

    def test_pending_enumeration_skips_capped_weekly_without_claude_call(self, blog_mod):
        """run_once() must not call Claude for a post_id that already hit the failure cap."""
        ns, state_dir, _ = blog_mod
        now = dt.datetime.now(HOBART_TZ)
        days_since_sunday = (now.weekday() + 1) % 7
        last_sunday = now - dt.timedelta(days=days_since_sunday)
        # Use the catch-up (previous Sunday) branch, which is time-of-day independent.
        prev_sunday = last_sunday - dt.timedelta(days=7)

        dailies = []
        for i in range(7):
            day = prev_sunday - dt.timedelta(days=6 - i)
            dailies.append(_make_daily_post(day, title=f"Day {i + 1}"))
        _write_blog_with_posts(state_dir, dailies)

        pid = ns["id_for_post"]("weekly", prev_sunday)
        for _ in range(3):
            ns["record_generation_failure"](pid, "empty body")
        assert ns["is_generation_skipped"](pid) is True

        with patch("pxh.claude_session.run_claude_session", return_value=_mock_claude_result()) as mock_run:
            with patch.dict(os.environ, {"PX_BLOG_QA": "0"}):
                ns["run_once"](dry=False)

        mock_run.assert_not_called()
        blog_data = ns["load_blog"]()
        assert not ns["post_exists"](blog_data, pid)


class TestBackfillRespectsFailureCap:
    def test_backfill_skips_capped_post_without_claude_call(self, blog_mod):
        """run_backfill() must honor the failure cap too — a manual --backfill
        must not burn sessions retrying a permanently-skipped post_id."""
        ns, state_dir, _ = blog_mod
        today = dt.datetime.now(HOBART_TZ)
        _write_thoughts(state_dir, today, count=5)
        pid = ns["id_for_post"]("daily", today.replace(hour=22, minute=0, second=0, microsecond=0))
        for _ in range(3):
            ns["record_generation_failure"](pid, "empty body")
        assert ns["is_generation_skipped"](pid) is True

        with patch("pxh.claude_session.run_claude_session", return_value=_mock_claude_result()) as mock_run:
            with patch.dict(os.environ, {"PX_BLOG_QA": "0"}):
                ns["run_backfill"](dry=False)

        mock_run.assert_not_called()
