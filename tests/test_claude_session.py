"""Tests for Claude session manager — model routing, rate limiting, execution, whitelist."""
from __future__ import annotations

import datetime as dt
import json
import os
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state_dir(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir()
    return sd


def _write_session_log(state_dir, entries):
    log_file = state_dir / "claude_sessions.jsonl"
    lines = [json.dumps(e) for e in entries]
    log_file.write_text("\n".join(lines) + "\n" if lines else "")


def _ts_ago(seconds: int) -> str:
    """Return an ISO timestamp `seconds` ago."""
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts_hobart(hour: int, minute: int = 0) -> str:
    """Return an ISO timestamp for today at the given Hobart time (in UTC)."""
    now_hobart = dt.datetime.now(ZoneInfo("Australia/Hobart"))
    local = now_hobart.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return local.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Model Routing
# ---------------------------------------------------------------------------

class TestModelRouting:
    def test_evolve_uses_opus(self):
        from pxh.claude_session import _model_for_type
        assert "opus" in _model_for_type("evolve")

    def test_self_debug_uses_sonnet(self):
        from pxh.claude_session import _model_for_type
        assert "sonnet" in _model_for_type("self_debug")

    def test_research_uses_haiku(self):
        from pxh.claude_session import _model_for_type
        assert "haiku" in _model_for_type("research")

    def test_compose_uses_haiku(self):
        from pxh.claude_session import _model_for_type
        assert "haiku" in _model_for_type("compose")

    def test_conversation_uses_sonnet(self):
        from pxh.claude_session import _model_for_type
        assert "sonnet" in _model_for_type("conversation")

    def test_env_override(self):
        from pxh.claude_session import _model_for_type
        with patch.dict(os.environ, {"PX_CLAUDE_MODEL_EVOLVE": "claude-test-model"}):
            assert _model_for_type("evolve") == "claude-test-model"

    def test_unknown_type_raises(self):
        from pxh.claude_session import _model_for_type
        with pytest.raises(ValueError):
            _model_for_type("nonexistent")


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    def test_empty_log_allows(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        import pxh.claude_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "claude_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False):
            assert cs.check_budget("research") is None

    def test_global_cooldown_blocks(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        _write_session_log(sd, [{"ts": _ts_ago(60), "type": "research"}])
        import pxh.claude_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "claude_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False), \
             patch.object(cs, "COOLDOWN_S", 1800):
            result = cs.check_budget("compose")
            assert result is not None
            assert "cooldown" in result.lower()

    def test_self_debug_exempt_from_global_cooldown(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        # Session 60s ago — global cooldown should block others but not self_debug
        _write_session_log(sd, [{"ts": _ts_ago(60), "type": "research"}])
        import pxh.claude_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "claude_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False), \
             patch.object(cs, "COOLDOWN_S", 1800):
            assert cs.check_budget("self_debug") is None

    def test_daily_cap_blocks(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        # Write 8 sessions within the last hour (definitely today in any TZ)
        entries = [{"ts": _ts_ago(i * 60 + 1), "type": "conversation"} for i in range(8)]
        _write_session_log(sd, entries)
        import pxh.claude_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "claude_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False), \
             patch.object(cs, "DAILY_CAP", 8):
            result = cs.check_budget("research")
            assert result is not None
            assert "daily cap" in result.lower()

    def test_per_type_cooldown_blocks(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        _write_session_log(sd, [{"ts": _ts_ago(300), "type": "research"}])
        import pxh.claude_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "claude_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False), \
             patch.object(cs, "COOLDOWN_S", 0):  # no global cooldown for this test
            # research cooldown is 7200s, entry is 300s ago → blocked
            result = cs.check_budget("research")
            assert result is not None
            assert "cooldown" in result.lower()

    def test_per_type_quota_blocks(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        # 4 conversation sessions within last 10 min = at quota (4/day)
        entries = [{"ts": _ts_ago(i * 60 + 60), "type": "conversation"} for i in range(4)]
        _write_session_log(sd, entries)
        import pxh.claude_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "claude_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False), \
             patch.object(cs, "COOLDOWN_S", 0):
            result = cs.check_budget("conversation")
            assert result is not None
            assert "quota" in result.lower()

    def test_corrupt_log_lines_skipped(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        log_file = sd / "claude_sessions.jsonl"
        # Entry from 3 hours ago — past the 2h research cooldown
        log_file.write_text('{"ts": "' + _ts_ago(10800) + '", "type": "research"}\nNOT_JSON\n')
        import pxh.claude_session as cs
        with patch.object(cs, "SESSION_LOG", log_file), \
             patch.object(cs, "BUDGET_DISABLED", False), \
             patch.object(cs, "COOLDOWN_S", 0):
            # Should not crash, and should count the valid entry
            result = cs.check_budget("research")
            # Still allowed (1 research, quota is 3, past cooldown)
            assert result is None

    def test_priority_gating_blocks_low_priority(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        # 6 sessions today with cap=8 → 2 remaining → low priority blocked
        entries = [{"ts": _ts_ago(i * 30 + 60), "type": "conversation"} for i in range(6)]
        _write_session_log(sd, entries)
        import pxh.claude_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "claude_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False), \
             patch.object(cs, "DAILY_CAP", 8), \
             patch.object(cs, "COOLDOWN_S", 0):
            # compose is low priority (1) — should be blocked
            result = cs.check_budget("compose")
            assert result is not None
            assert "priority" in result.lower()

    def test_priority_gating_allows_high_priority(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        # 6 sessions today with cap=8 → 2 remaining
        entries = [{"ts": _ts_ago(i * 30 + 60), "type": "conversation"} for i in range(6)]
        _write_session_log(sd, entries)
        import pxh.claude_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "claude_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False), \
             patch.object(cs, "DAILY_CAP", 8), \
             patch.object(cs, "COOLDOWN_S", 0):
            # self_debug is high priority (5) — should be allowed
            assert cs.check_budget("self_debug") is None

    def test_cold_start_missing_log(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        import pxh.claude_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "nonexistent.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False):
            assert cs.check_budget("research") is None

    def test_budget_disabled_bypass(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        # Fill up daily cap
        entries = [{"ts": _ts_ago(i * 100 + 1), "type": "research"} for i in range(10)]
        _write_session_log(sd, entries)
        import pxh.claude_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "claude_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", True):
            assert cs.check_budget("research") is None


# ---------------------------------------------------------------------------
# Session Execution
# ---------------------------------------------------------------------------

class TestRunSession:
    def test_budget_exhausted_raises(self, tmp_path):
        _make_state_dir(tmp_path)
        import pxh.claude_session as cs
        with patch.object(cs, "check_budget", return_value="test block reason"):
            with pytest.raises(cs.SessionBudgetExhausted):
                cs.run_claude_session("research", "test prompt")

    def test_successful_session_logged(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        import pxh.claude_session as cs
        mock_result = MagicMock()
        mock_result.stdout = "test output"
        mock_result.stderr = ""
        mock_result.returncode = 0

        # This test is about the legacy subprocess path, which "research" no
        # longer takes by default — pin the routing off so it keeps testing
        # what it was written to test rather than silently testing nothing.
        with patch.dict(os.environ, {"PX_BRAIN_KINDS": ""}), \
             patch.object(cs, "check_budget", return_value=None), \
             patch("subprocess.run", return_value=mock_result), \
             patch.object(cs, "SESSION_LOG", sd / "claude_sessions.jsonl"), \
             patch.object(cs, "STATE_DIR", sd):
            result = cs.run_claude_session("research", "test prompt", timeout=10)
            assert result.stdout == "test output"
            assert result.returncode == 0
            assert "haiku" in result.model_used

            # Verify log was written
            log_file = sd / "claude_sessions.jsonl"
            assert log_file.exists()
            entry = json.loads(log_file.read_text().strip())
            assert entry["type"] == "research"
            assert entry["outcome"] == "success"

    def test_claude_env_vars_stripped(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        import pxh.claude_session as cs
        captured_env = {}

        def mock_run(cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            m = MagicMock()
            m.stdout = ""
            m.stderr = ""
            m.returncode = 0
            return m

        with patch.object(cs, "check_budget", return_value=None), \
             patch("subprocess.run", side_effect=mock_run), \
             patch.object(cs, "SESSION_LOG", sd / "claude_sessions.jsonl"), \
             patch.object(cs, "STATE_DIR", sd), \
             patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_FOO": "bar",
                                     "PX_BRAIN_KINDS": ""}):
            cs.run_claude_session("research", "test prompt", timeout=10)
            assert "CLAUDECODE" not in captured_env
            assert "CLAUDE_CODE_FOO" not in captured_env


# ---------------------------------------------------------------------------
# Whitelist
# ---------------------------------------------------------------------------

class TestWhitelist:
    def test_spark_config_allowed(self):
        from pxh.claude_session import file_in_whitelist
        assert file_in_whitelist("src/pxh/spark_config.py")

    def test_mind_allowed(self):
        from pxh.claude_session import file_in_whitelist
        assert file_in_whitelist("src/pxh/mind.py")

    def test_voice_loop_allowed(self):
        from pxh.claude_session import file_in_whitelist
        assert file_in_whitelist("src/pxh/voice_loop.py")

    def test_api_blacklisted(self):
        from pxh.claude_session import file_in_whitelist
        assert not file_in_whitelist("src/pxh/api.py")

    def test_px_evolve_blacklisted(self):
        from pxh.claude_session import file_in_whitelist
        assert not file_in_whitelist("bin/px-evolve")

    def test_new_tool_allowed(self):
        from pxh.claude_session import file_in_whitelist
        assert file_in_whitelist("bin/tool-newfeature")

    def test_test_file_allowed(self):
        from pxh.claude_session import file_in_whitelist
        assert file_in_whitelist("tests/test_new.py")

    def test_policy_module_is_blacklisted(self):
        """The constitutional rules are not SPARK's to rewrite (#174)."""
        from pxh.claude_session import file_in_whitelist
        assert not file_in_whitelist("src/pxh/policy.py")

    def test_policy_invariant_tests_are_blacklisted(self):
        """Protecting policy.py alone would leave the erosion path open: an
        evolution PR could delete the call site in voice_loop.py (whitelisted)
        and adjust its whitelisted tests to match. The integration assertions
        must be protected too."""
        from pxh.claude_session import file_in_whitelist
        assert not file_in_whitelist("tests/test_policy_invariants.py")

    def test_ordinary_policy_tests_remain_whitelisted(self):
        """Ordinary policy coverage must stay evolvable — only the pinned
        invariants are frozen."""
        from pxh.claude_session import file_in_whitelist
        assert file_in_whitelist("tests/test_policy.py")

    def test_env_blacklisted(self):
        from pxh.claude_session import file_in_whitelist
        assert not file_in_whitelist(".env")

    def test_persona_prompt_blacklisted(self):
        from pxh.claude_session import file_in_whitelist
        assert not file_in_whitelist("docs/prompts/persona-gremlin.md")
        assert not file_in_whitelist("docs/prompts/persona-vixen.md")

    def test_systemd_blacklisted(self):
        from pxh.claude_session import file_in_whitelist
        assert not file_in_whitelist("systemd/px-evolve.service")

    def test_prompt_docs_allowed(self):
        from pxh.claude_session import file_in_whitelist
        assert file_in_whitelist("docs/prompts/new-prompt.md")

    def test_tool_chat_blacklisted(self):
        from pxh.claude_session import file_in_whitelist
        assert not file_in_whitelist("bin/tool-chat")
        assert not file_in_whitelist("bin/tool-chat-vixen")


# ---------------------------------------------------------------------------
# Self-Debug Trigger (Task 5 prep)
# ---------------------------------------------------------------------------

class TestSelfDebugTrigger:
    """Verify self_debug is properly configured in mind.py action sets."""

    def test_self_debug_model_is_sonnet(self):
        from pxh.claude_session import _model_for_type
        assert "sonnet" in _model_for_type("self_debug")

    def test_self_debug_exempt_from_global_cooldown(self):
        from pxh.claude_session import _GLOBAL_COOLDOWN_EXEMPT
        assert "self_debug" in _GLOBAL_COOLDOWN_EXEMPT

    def test_self_debug_has_highest_priority(self):
        from pxh.claude_session import _PRIORITY
        assert _PRIORITY["self_debug"] == max(_PRIORITY.values())


# ---------------------------------------------------------------------------
# Conversation Depth Trigger (Task 6 prep)
# ---------------------------------------------------------------------------

class TestConversationDepthTrigger:
    """Test depth trigger phrase detection — implemented in voice_loop.py."""

    def test_think_deeper_triggers(self):
        from pxh.voice_loop import is_depth_trigger
        assert is_depth_trigger("think about that more")

    def test_go_deeper_triggers(self):
        from pxh.voice_loop import is_depth_trigger
        assert is_depth_trigger("go deeper on that")

    def test_explain_properly_triggers(self):
        from pxh.voice_loop import is_depth_trigger
        assert is_depth_trigger("explain that properly")

    def test_normal_text_does_not_trigger(self):
        from pxh.voice_loop import is_depth_trigger
        assert not is_depth_trigger("hello there")

    def test_case_insensitive(self):
        from pxh.voice_loop import is_depth_trigger
        assert is_depth_trigger("THINK ABOUT THAT MORE")


# ---------------------------------------------------------------------------
# Blog Session Type (Task 1)
# ---------------------------------------------------------------------------

class TestBlogSessionType:
    def test_blog_uses_haiku(self):
        from pxh.claude_session import _model_for_type
        assert "haiku" in _model_for_type("blog")

    def test_blog_env_override(self):
        from pxh.claude_session import _ENV_OVERRIDES
        assert "blog" in _ENV_OVERRIDES
        assert _ENV_OVERRIDES["blog"] == "PX_CLAUDE_MODEL_BLOG"

    def test_blog_cooldown(self):
        from pxh.claude_session import _TYPE_COOLDOWNS
        assert _TYPE_COOLDOWNS["blog"] == 1800

    def test_blog_quota(self):
        from pxh.claude_session import _TYPE_QUOTAS
        assert _TYPE_QUOTAS["blog"] == 5

    def test_blog_priority(self):
        from pxh.claude_session import _PRIORITY
        assert "blog" in _PRIORITY
        assert _PRIORITY["blog"] == 2

    def test_blog_exempt_from_global_cooldown(self):
        from pxh.claude_session import _GLOBAL_COOLDOWN_EXEMPT
        assert "blog" in _GLOBAL_COOLDOWN_EXEMPT


# ---------------------------------------------------------------------------
# budget_summary — one-line budget state for reflection context
# ---------------------------------------------------------------------------


class TestBudgetSummary:
    def test_reports_global_and_per_type_counts(self, tmp_path):
        import pxh.claude_session as cs
        sd = _make_state_dir(tmp_path)
        _write_session_log(sd, [
            {"ts": _ts_ago(3600), "type": "blog"},
            {"ts": _ts_ago(7200), "type": "research"},
        ])
        with patch.object(cs, "SESSION_LOG", sd / "claude_sessions.jsonl"):
            s = cs.budget_summary()
        assert "2/8" in s
        assert "research 1/3" in s
        assert "blog 1/5" in s

    def test_flags_blocked_types(self, tmp_path):
        import pxh.claude_session as cs
        sd = _make_state_dir(tmp_path)
        # research at quota (3 used), spaced out beyond cooldowns
        _write_session_log(sd, [
            {"ts": _ts_ago(30000), "type": "research"},
            {"ts": _ts_ago(20000), "type": "research"},
            {"ts": _ts_ago(10000), "type": "research"},
        ])
        with patch.object(cs, "SESSION_LOG", sd / "claude_sessions.jsonl"):
            s = cs.budget_summary()
        assert "research" in s
        # research must be marked unavailable in some form
        assert "research 3/3" in s
        low = s.lower()
        assert ("blocked" in low) or ("unavailable" in low)

    def test_empty_log(self, tmp_path):
        import pxh.claude_session as cs
        sd = _make_state_dir(tmp_path)
        _write_session_log(sd, [])
        with patch.object(cs, "SESSION_LOG", sd / "claude_sessions.jsonl"):
            s = cs.budget_summary()
        assert "0/8" in s


# ---------------------------------------------------------------------------
# Consolidate Session Type (Task 2)
# ---------------------------------------------------------------------------

def test_consolidate_session_type_registered():
    from pxh import claude_session as cs
    assert cs._model_for_type("consolidate").startswith("claude-haiku")
    assert cs._TYPE_QUOTAS["consolidate"] == 1
    assert cs._TYPE_COOLDOWNS["consolidate"] == 72000
    assert cs._PRIORITY["consolidate"] == 2
    assert cs._ENV_OVERRIDES["consolidate"] == "PX_CLAUDE_MODEL_CONSOLIDATE"


def test_consolidate_quota_one_per_day(tmp_path, monkeypatch):
    import datetime as dt
    import json
    from pxh import claude_session as cs
    log = tmp_path / "claude_sessions.jsonl"
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.write_text(json.dumps({"ts": now, "type": "consolidate"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(cs, "SESSION_LOG", log)
    monkeypatch.setattr(cs, "BUDGET_DISABLED", False)
    reason = cs.check_budget("consolidate")
    assert reason is not None and "quota" in reason


# ---------------------------------------------------------------------------
# Routing to the resident Claude session (the `claude -p` retirement)
# ---------------------------------------------------------------------------

def test_phase_one_types_default_to_the_resident_session():
    """Phase 1 of the `claude -p` retirement: research, compose and post QA."""
    from pxh import claude_session as cs
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PX_BRAIN_KINDS", None)
        kinds = cs._brain_kinds()
    assert kinds == {"research", "compose", "post_qa"}


def test_brain_kinds_is_read_at_call_time_not_import_time():
    """The rollout has to be widenable — and rollback-able — without a restart."""
    from pxh import claude_session as cs
    with patch.dict(os.environ, {"PX_BRAIN_KINDS": "research,compose,blog"}):
        assert "blog" in cs._brain_kinds()
    with patch.dict(os.environ, {"PX_BRAIN_KINDS": ""}):
        assert cs._brain_kinds() == frozenset()


def test_a_routed_type_never_spawns_a_subprocess(tmp_path, monkeypatch):
    """The whole point: for these types, no `claude -p` process is created."""
    from pxh import brain
    from pxh import claude_session as cs

    def _boom(*a, **k):
        raise AssertionError(f"run_claude_session spawned a subprocess: {a}")

    monkeypatch.setattr(cs.subprocess, "run", _boom)
    monkeypatch.setattr(brain, "ask_brain",
                        lambda kind, payload, timeout_s=None, model=None:
                        {"reply": "the answer"})
    monkeypatch.setattr(cs, "SESSION_LOG", tmp_path / "claude_sessions.jsonl")
    monkeypatch.setattr(cs, "BUDGET_DISABLED", True)

    result = cs.run_claude_session("research", "what is a wombat", timeout=5)
    assert result.returncode == 0
    assert result.stdout == "the answer"


def test_the_prompt_reaches_the_brain_intact(tmp_path, monkeypatch):
    from pxh import brain
    from pxh import claude_session as cs
    seen = {}

    def _capture(kind, payload, timeout_s=None, model=None):
        seen.update(kind=kind, payload=payload, timeout_s=timeout_s, model=model)
        return {"reply": "ok"}

    monkeypatch.setattr(brain, "ask_brain", _capture)
    monkeypatch.setattr(cs, "SESSION_LOG", tmp_path / "claude_sessions.jsonl")
    monkeypatch.setattr(cs, "BUDGET_DISABLED", True)

    cs.run_claude_session("compose", "write a haiku", timeout=42)
    assert seen["kind"] == "compose"
    assert seen["payload"]["prompt"] == "write a haiku"
    assert seen["timeout_s"] == 42
    assert seen["model"].startswith("claude-haiku")


def test_an_unavailable_brain_looks_like_a_failed_run_not_an_exception(
        tmp_path, monkeypatch):
    """Callers already handle a non-zero returncode from `claude -p`. Reusing
    that contract is what lets the brain fail without touching any of them."""
    from pxh import brain
    from pxh import claude_session as cs

    monkeypatch.setattr(brain, "ask_brain",
                        lambda kind, payload, timeout_s=None, model=None: None)
    monkeypatch.setattr(cs, "SESSION_LOG", tmp_path / "claude_sessions.jsonl")
    monkeypatch.setattr(cs, "BUDGET_DISABLED", True)

    result = cs.run_claude_session("research", "anything", timeout=5)
    assert result.returncode != 0
    assert "brain unavailable" in result.stderr
    assert result.stdout == ""


def test_a_structured_reply_is_handed_back_as_text(tmp_path, monkeypatch):
    """Every caller of run_claude_session reads stdout as text."""
    from pxh import brain
    from pxh import claude_session as cs

    monkeypatch.setattr(brain, "ask_brain",
                        lambda kind, payload, timeout_s=None, model=None:
                        {"reply": {"verdict": "yes"}})
    monkeypatch.setattr(cs, "SESSION_LOG", tmp_path / "claude_sessions.jsonl")
    monkeypatch.setattr(cs, "BUDGET_DISABLED", True)

    result = cs.run_claude_session("research", "anything", timeout=5)
    assert json.loads(result.stdout) == {"verdict": "yes"}


def test_budget_is_still_checked_before_the_brain_is_asked(tmp_path, monkeypatch):
    """Routing must not become a way around the quota."""
    from pxh import brain
    from pxh import claude_session as cs

    asked = []
    monkeypatch.setattr(brain, "ask_brain",
                        lambda *a, **k: asked.append(1) or {"reply": "x"})
    monkeypatch.setattr(cs, "check_budget", lambda t: "daily cap reached")

    with pytest.raises(cs.SessionBudgetExhausted):
        cs.run_claude_session("research", "anything")
    assert asked == [], "budget check must run before the request"


def test_a_routed_session_is_still_logged(tmp_path, monkeypatch):
    """state/claude_sessions.jsonl is how spend is reconstructed after the
    fact — a type that moves to the brain must not vanish from it."""
    from pxh import brain
    from pxh import claude_session as cs
    log = tmp_path / "claude_sessions.jsonl"

    monkeypatch.setattr(brain, "ask_brain",
                        lambda kind, payload, timeout_s=None, model=None:
                        {"reply": "ok"})
    monkeypatch.setattr(cs, "SESSION_LOG", log)
    monkeypatch.setattr(cs, "BUDGET_DISABLED", True)

    cs.run_claude_session("research", "anything", timeout=5)
    entries = [json.loads(line) for line in log.read_text().splitlines() if line]
    assert entries and entries[-1]["type"] == "research"
