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
    log_file = state_dir / "model_sessions.jsonl"
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
# Known kinds
# ---------------------------------------------------------------------------
#
# `TestModelRouting` lived here. It pinned a kind→Claude-model-id table and the
# `PX_CLAUDE_MODEL_*` overrides that fed it. #317 Phase 3 deleted both, so the
# tests went with them rather than being rewritten to assert a table that no
# longer exists — a test that pins "consolidate runs on Haiku" against a module
# that cannot reach Haiku is a test that lies about what it proves.
#
# What is still worth pinning is the part that was load-bearing under the model
# table: the registry of kinds this dispatcher recognises, because everything
# else in this file (quotas, cooldowns, priorities) is keyed to it.

class TestKnownKinds:
    def test_every_table_kind_is_a_known_kind(self):
        """A kind in a quota or cooldown table but not in the registry is
        unreachable — `check_budget` refuses it before any table is read."""
        from pxh.model_session import KNOWN_KINDS, _PRIORITY, _TYPE_COOLDOWNS, _TYPE_QUOTAS
        for table in (_PRIORITY, _TYPE_COOLDOWNS, _TYPE_QUOTAS):
            assert set(table) <= KNOWN_KINDS, sorted(set(table) - KNOWN_KINDS)

    def test_unknown_kind_is_refused_not_defaulted(self):
        """Fail closed: an unrecognised kind gets no quota and no cooldown,
        it gets a refusal."""
        from pxh.model_session import check_budget
        assert check_budget("nonexistent") == "unknown session type: nonexistent"

    def test_no_claude_model_ids_remain(self):
        """The table is gone, and so is the vocabulary it was spelled in."""
        from pxh import model_session
        for gone in ("_DEFAULT_MODELS", "_ENV_OVERRIDES", "_model_for_type"):
            assert not hasattr(model_session, gone), f"{gone} outlived the session it served"


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    def test_empty_log_allows(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        import pxh.model_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "model_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False):
            assert cs.check_budget("research") is None

    def test_global_cooldown_blocks(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        _write_session_log(sd, [{"ts": _ts_ago(60), "type": "research"}])
        import pxh.model_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "model_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False), \
             patch.object(cs, "COOLDOWN_S", 1800):
            result = cs.check_budget("compose")
            assert result is not None
            assert "cooldown" in result.lower()

    def test_self_debug_exempt_from_global_cooldown(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        # Session 60s ago — global cooldown should block others but not self_debug
        _write_session_log(sd, [{"ts": _ts_ago(60), "type": "research"}])
        import pxh.model_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "model_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False), \
             patch.object(cs, "COOLDOWN_S", 1800):
            assert cs.check_budget("self_debug") is None

    def test_a_session_that_was_never_answered_does_not_arm_the_cooldown(self, tmp_path):
        """#310: rc=1/`brain_unavailable` is a record that nothing was spent.

        On 2026-09-15 a `research` attempt that never reached the model was the
        only thing that refused `consolidate` its own retry for half an hour.
        """
        sd = _make_state_dir(tmp_path)
        _write_session_log(sd, [{
            "ts": _ts_ago(60), "type": "research", "model": "haiku",
            "duration_s": 0.0, "returncode": 1, "outcome": "brain_unavailable",
        }])
        import pxh.model_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "model_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False), \
             patch.object(cs, "COOLDOWN_S", 1800):
            assert cs.check_budget("compose") is None

    def test_an_answered_session_still_arms_the_cooldown(self, tmp_path):
        """The gate is narrowed to spend, not removed."""
        sd = _make_state_dir(tmp_path)
        _write_session_log(sd, [{
            "ts": _ts_ago(60), "type": "research", "model": "haiku",
            "duration_s": 12.0, "returncode": 0, "outcome": "success",
        }])
        import pxh.model_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "model_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False), \
             patch.object(cs, "COOLDOWN_S", 1800):
            assert "global cooldown" in cs.check_budget("compose")

    def test_an_entry_with_no_returncode_counts_as_spend(self, tmp_path):
        """An unknown shape keeps the gate that already applied.

        Entries predate the field only in theory, and the safe direction on a
        parse failure is the narrower cooldown, not a widened one.
        """
        sd = _make_state_dir(tmp_path)
        _write_session_log(sd, [{"ts": _ts_ago(60), "type": "research"}])
        import pxh.model_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "model_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False), \
             patch.object(cs, "COOLDOWN_S", 1800):
            assert "global cooldown" in cs.check_budget("compose")

    def test_daily_cap_blocks(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        # Write 8 sessions within the last hour (definitely today in any TZ)
        entries = [{"ts": _ts_ago(i * 60 + 1), "type": "conversation"} for i in range(8)]
        _write_session_log(sd, entries)
        import pxh.model_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "model_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False), \
             patch.object(cs, "DAILY_CAP", 8):
            result = cs.check_budget("research")
            assert result is not None
            assert "daily cap" in result.lower()

    def test_per_type_cooldown_blocks(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        _write_session_log(sd, [{"ts": _ts_ago(300), "type": "research"}])
        import pxh.model_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "model_sessions.jsonl"), \
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
        import pxh.model_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "model_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False), \
             patch.object(cs, "COOLDOWN_S", 0):
            result = cs.check_budget("conversation")
            assert result is not None
            assert "quota" in result.lower()

    def test_corrupt_log_lines_skipped(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        log_file = sd / "model_sessions.jsonl"
        # Entry from 3 hours ago — past the 2h research cooldown
        log_file.write_text('{"ts": "' + _ts_ago(10800) + '", "type": "research"}\nNOT_JSON\n')
        import pxh.model_session as cs
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
        import pxh.model_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "model_sessions.jsonl"), \
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
        import pxh.model_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "model_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False), \
             patch.object(cs, "DAILY_CAP", 8), \
             patch.object(cs, "COOLDOWN_S", 0):
            # self_debug is high priority (5) — should be allowed
            assert cs.check_budget("self_debug") is None

    def test_cold_start_missing_log(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        import pxh.model_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "nonexistent.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", False):
            assert cs.check_budget("research") is None

    def test_budget_disabled_bypass(self, tmp_path):
        sd = _make_state_dir(tmp_path)
        # Fill up daily cap
        entries = [{"ts": _ts_ago(i * 100 + 1), "type": "research"} for i in range(10)]
        _write_session_log(sd, entries)
        import pxh.model_session as cs
        with patch.object(cs, "SESSION_LOG", sd / "model_sessions.jsonl"), \
             patch.object(cs, "BUDGET_DISABLED", True):
            assert cs.check_budget("research") is None


# ---------------------------------------------------------------------------
# Session Execution
# ---------------------------------------------------------------------------

class TestRunSession:
    def test_budget_exhausted_raises(self, tmp_path):
        _make_state_dir(tmp_path)
        import pxh.model_session as cs
        with patch.object(cs, "check_budget", return_value="test block reason"):
            with pytest.raises(cs.SessionBudgetExhausted):
                cs.run_model_session("research", "test prompt")

    def test_no_session_type_spawns_a_process(self, tmp_path):
        """Replaces test_claude_env_vars_stripped.

        That test checked CLAUDECODE and CLAUDE_CODE_* were scrubbed from the
        environment handed to a nested `claude -p`. There is no nested process
        now, so there is no environment to scrub — the guarantee it was
        approximating is simply that nothing is spawned, for any kind, routed
        or not (#317 Phase 2 asserts that over the whole kind table).
        """
        sd = _make_state_dir(tmp_path)
        import pxh.model_session as cs

        def _boom(*a, **k):
            raise AssertionError("cold-started Claude")

        with patch.object(cs, "check_budget", return_value=None), \
             patch("subprocess.run", side_effect=_boom), \
             patch.object(cs, "SESSION_LOG", sd / "model_sessions.jsonl"), \
             patch.object(cs, "STATE_DIR", sd), \
             patch.dict(os.environ, {"CLAUDECODE": "1", "PX_BRAIN_KINDS": ""}):
            with pytest.raises(cs.ColdStartForbidden):
                cs.run_model_session("evolve", "test prompt", timeout=10)


class TestWhitelist:
    def test_spark_config_allowed(self):
        from pxh.model_session import file_in_whitelist
        assert file_in_whitelist("src/pxh/spark_config.py")

    def test_mind_allowed(self):
        from pxh.model_session import file_in_whitelist
        assert file_in_whitelist("src/pxh/mind.py")

    def test_voice_loop_allowed(self):
        from pxh.model_session import file_in_whitelist
        assert file_in_whitelist("src/pxh/voice_loop.py")

    def test_api_blacklisted(self):
        from pxh.model_session import file_in_whitelist
        assert not file_in_whitelist("src/pxh/api.py")

    def test_px_evolve_blacklisted(self):
        from pxh.model_session import file_in_whitelist
        assert not file_in_whitelist("bin/px-evolve")

    def test_new_tool_allowed(self):
        from pxh.model_session import file_in_whitelist
        assert file_in_whitelist("bin/tool-newfeature")

    def test_test_file_allowed(self):
        from pxh.model_session import file_in_whitelist
        assert file_in_whitelist("tests/test_new.py")

    def test_policy_module_is_blacklisted(self):
        """The constitutional rules are not SPARK's to rewrite (#174)."""
        from pxh.model_session import file_in_whitelist
        assert not file_in_whitelist("src/pxh/policy.py")

    def test_policy_invariant_tests_are_blacklisted(self):
        """Protecting policy.py alone would leave the erosion path open: an
        evolution PR could delete the call site in voice_loop.py (whitelisted)
        and adjust its whitelisted tests to match. The integration assertions
        must be protected too."""
        from pxh.model_session import file_in_whitelist
        assert not file_in_whitelist("tests/test_policy_invariants.py")

    def test_ordinary_policy_tests_remain_whitelisted(self):
        """Ordinary policy coverage must stay evolvable — only the pinned
        invariants are frozen."""
        from pxh.model_session import file_in_whitelist
        assert file_in_whitelist("tests/test_policy.py")

    def test_env_blacklisted(self):
        from pxh.model_session import file_in_whitelist
        assert not file_in_whitelist(".env")

    def test_persona_prompt_blacklisted(self):
        from pxh.model_session import file_in_whitelist
        assert not file_in_whitelist("docs/prompts/persona-gremlin.md")
        assert not file_in_whitelist("docs/prompts/persona-vixen.md")

    def test_systemd_blacklisted(self):
        from pxh.model_session import file_in_whitelist
        assert not file_in_whitelist("systemd/px-evolve.service")

    def test_prompt_docs_allowed(self):
        from pxh.model_session import file_in_whitelist
        assert file_in_whitelist("docs/prompts/new-prompt.md")

    def test_tool_chat_blacklisted(self):
        from pxh.model_session import file_in_whitelist
        assert not file_in_whitelist("bin/tool-chat")
        assert not file_in_whitelist("bin/tool-chat-vixen")


# ---------------------------------------------------------------------------
# Self-Debug Trigger (Task 5 prep)
# ---------------------------------------------------------------------------

class TestSelfDebugTrigger:
    """Verify self_debug is properly configured in mind.py action sets."""

    def test_self_debug_is_a_known_kind(self):
        from pxh.model_session import KNOWN_KINDS
        assert "self_debug" in KNOWN_KINDS

    def test_self_debug_exempt_from_global_cooldown(self):
        from pxh.model_session import _GLOBAL_COOLDOWN_EXEMPT
        assert "self_debug" in _GLOBAL_COOLDOWN_EXEMPT

    def test_self_debug_has_highest_priority(self):
        from pxh.model_session import _PRIORITY
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
    def test_blog_is_a_known_kind(self):
        from pxh.model_session import KNOWN_KINDS
        assert "blog" in KNOWN_KINDS

    def test_blog_cooldown(self):
        from pxh.model_session import _TYPE_COOLDOWNS
        assert _TYPE_COOLDOWNS["blog"] == 1800

    def test_blog_quota(self):
        from pxh.model_session import _TYPE_QUOTAS
        assert _TYPE_QUOTAS["blog"] == 5

    def test_blog_priority(self):
        from pxh.model_session import _PRIORITY
        assert "blog" in _PRIORITY
        assert _PRIORITY["blog"] == 2

    def test_blog_exempt_from_global_cooldown(self):
        from pxh.model_session import _GLOBAL_COOLDOWN_EXEMPT
        assert "blog" in _GLOBAL_COOLDOWN_EXEMPT


# ---------------------------------------------------------------------------
# budget_summary — one-line budget state for reflection context
# ---------------------------------------------------------------------------


# budget_summary()'s _today_entries() filters by Hobart *calendar day*, not a
# rolling window. Fixtures built from _ts_ago() ("N seconds before the real
# wall clock") flip from "today" to "yesterday" whenever the suite happens to
# run within N seconds of Hobart midnight -- with offsets up to 30000s (8h20m)
# that's most of every night (#213). Freeze the clock both sides see to a
# fixed Hobart noon instead, so the offsets always land in the same frozen
# "today" no matter what wall-clock time CI actually runs at.
_FROZEN_HOBART_NOON = dt.datetime(2026, 6, 15, 12, 0, tzinfo=ZoneInfo("Australia/Hobart"))


class _FrozenDatetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return _FROZEN_HOBART_NOON.astimezone(tz) if tz else _FROZEN_HOBART_NOON.replace(tzinfo=None)


def _ts_frozen_ago(seconds: int) -> str:
    """Return an ISO timestamp `seconds` before the frozen anchor above."""
    t = _FROZEN_HOBART_NOON.astimezone(dt.timezone.utc) - dt.timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestBudgetSummary:
    def test_reports_global_and_per_type_counts(self, tmp_path):
        import pxh.model_session as cs
        sd = _make_state_dir(tmp_path)
        _write_session_log(sd, [
            {"ts": _ts_frozen_ago(3600), "type": "blog"},
            {"ts": _ts_frozen_ago(7200), "type": "research"},
        ])
        with patch.object(cs, "SESSION_LOG", sd / "model_sessions.jsonl"), \
                patch.object(cs.dt, "datetime", _FrozenDatetime):
            s = cs.budget_summary()
        assert "2/8" in s
        assert "research 1/3" in s
        assert "blog 1/5" in s

    def test_flags_blocked_types(self, tmp_path):
        import pxh.model_session as cs
        sd = _make_state_dir(tmp_path)
        # research at quota (3 used), spaced out beyond cooldowns
        _write_session_log(sd, [
            {"ts": _ts_frozen_ago(30000), "type": "research"},
            {"ts": _ts_frozen_ago(20000), "type": "research"},
            {"ts": _ts_frozen_ago(10000), "type": "research"},
        ])
        with patch.object(cs, "SESSION_LOG", sd / "model_sessions.jsonl"), \
                patch.object(cs.dt, "datetime", _FrozenDatetime):
            s = cs.budget_summary()
        assert "research" in s
        # research must be marked unavailable in some form
        assert "research 3/3" in s
        low = s.lower()
        assert ("blocked" in low) or ("unavailable" in low)

    def test_empty_log(self, tmp_path):
        import pxh.model_session as cs
        sd = _make_state_dir(tmp_path)
        _write_session_log(sd, [])
        with patch.object(cs, "SESSION_LOG", sd / "model_sessions.jsonl"):
            s = cs.budget_summary()
        assert "0/8" in s


# ---------------------------------------------------------------------------
# Consolidate Session Type (Task 2)
# ---------------------------------------------------------------------------

def test_consolidate_session_type_registered():
    from pxh import model_session as cs
    from pxh import memory
    assert "consolidate" in cs.KNOWN_KINDS
    # Two per night (#291). Was 1/72000, which made the second of
    # memory.MAX_ATTEMPTS_PER_DAY's two attempts structurally unspendable —
    # attempt 1 consumed the only slot attempt 2 could ever have used.
    assert cs._TYPE_QUOTAS["consolidate"] == memory.MAX_ATTEMPTS_PER_DAY == 2
    # The cooldown is *not* the retry gap, and must not be made equal to it
    # again (#310): this clock starts when an attempt finished, that one when it
    # started, so an attempt that spends its whole 600s deadline puts the retry
    # 1800s into a 2400s cooldown and it is refused. The gap has to be wider
    # than the cooldown, not the same as it.
    assert cs._TYPE_COOLDOWNS["consolidate"] == 2400
    assert memory.RETRY_SPACING_S > cs._TYPE_COOLDOWNS["consolidate"]
    assert cs._PRIORITY["consolidate"] == 2


def test_consolidate_quota_is_two_per_day(tmp_path, monkeypatch):
    import datetime as dt
    import json
    from pxh import model_session as cs
    log = tmp_path / "model_sessions.jsonl"
    # Two attempts already spent tonight, spaced far enough apart that neither
    # cooldown is what refuses the third — the quota must be.
    now = dt.datetime.now(dt.timezone.utc)
    log.write_text("".join(
        json.dumps({"ts": (now - dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "type": "consolidate"}) + "\n" for h in (3, 2)), encoding="utf-8")
    monkeypatch.setattr(cs, "SESSION_LOG", log)
    monkeypatch.setattr(cs, "BUDGET_DISABLED", False)
    reason = cs.check_budget("consolidate")
    assert reason is not None and "quota" in reason


def test_consolidate_second_attempt_is_admitted(tmp_path, monkeypatch):
    """The retry memory.py schedules must actually get past the budget gate."""
    import datetime as dt
    import json
    from pxh import model_session as cs
    from pxh import memory
    log = tmp_path / "model_sessions.jsonl"
    then = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        seconds=memory.RETRY_SPACING_S)
    log.write_text(json.dumps(
        {"ts": then.strftime("%Y-%m-%dT%H:%M:%SZ"), "type": "consolidate"}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(cs, "SESSION_LOG", log)
    monkeypatch.setattr(cs, "BUDGET_DISABLED", False)
    assert cs.check_budget("consolidate") is None


# ---------------------------------------------------------------------------
# The resident transport is gone (#317 Phase 3)
# ---------------------------------------------------------------------------

def test_every_kind_has_one_backend_or_none():
    """Nothing is routed to a CLI any more, and that is the state #317 aimed at.

    The dial's original failure mode was a kind *missing* from it — disabled by
    accident. With every kind migrated and `self_debug` last, the honest
    reading inverts: a kind is either on the cognition tier or deliberately has
    no backend, and `tests/test_resident_routing.py` is what keeps a new kind
    from landing in a gap.
    """
    from pxh import model_session as cs
    assert cs._COGNITION_KINDS == {"consolidate", "research", "compose",
                                  "blog", "self_debug"}
    # `evolve` is absent on purpose: it needs a git worktree, and a tool-free
    # API call cannot provide one. Absent means *disabled*, not "takes the old
    # path" — there is no old path.
    assert "evolve" not in cs._COGNITION_KINDS


def test_the_routing_dial_is_gone_rather_than_empty():
    """An empty dial is a route that is one environment variable from working.

    `PX_BRAIN_KINDS` and both spellings of the accessor are deleted in the same
    change as the session they routed to, so "rolled back into the mailbox"
    stops being a shape anyone can reach for.
    """
    from pxh import model_session as cs
    for name in ("brain_kinds", "_brain_kinds", "_DEFAULT_BRAIN_KINDS",
                 "RESIDENT_PROVIDER"):
        assert not hasattr(cs, name), f"{name} outlived the transport it named"


def test_reflection_has_no_cold_rollback_dial():
    """The dial's off position used to mean `claude -p`. It has no meaning now.

    `mind._reflection_via_brain_enabled()` was removed with the cold path it
    selected. Had it stayed, narrowing PX_BRAIN_KINDS would have silently
    disabled reflection while still reading like a routing choice — a lever
    that does something other than what its name says.
    """
    from pxh import mind
    assert not hasattr(mind, "_reflection_via_brain_enabled")


def test_budget_is_still_checked_before_the_provider_is_asked(tmp_path, monkeypatch):
    """Routing must not become a way around the quota.

    Driven through the cognition path, since that is now the only door — the
    assertion is about the *order* of the check, not about which provider.
    """
    from pxh import model_session as cs, m5

    asked = []
    monkeypatch.setattr(m5, "ask_m5", lambda *a, **k: asked.append(1) or
                        m5.M5Result(status="available", response="x"))
    monkeypatch.setattr(cs, "check_budget", lambda t: "daily cap reached")

    with pytest.raises(cs.SessionBudgetExhausted):
        cs.run_model_session("research", "anything")
    assert asked == [], "budget check must run before the request"
