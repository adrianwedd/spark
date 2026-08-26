"""Pure tests for src/pxh/quiet_mode.py — issue #209.

No I/O, no session file, no fixtures: `migrate_legacy()`/`resolve()` only
ever see the dict a caller hands them. Deliberately does not exercise
`state.py` wiring (that's live-gated, prepared separately) or session.json
corruption/self-heal (#208's scope, not this module's).
"""
import copy

from pxh import quiet_mode


# --- legacy migration ---------------------------------------------------


def test_legacy_true_migrates_to_indefinite_enabled_unknown_source():
    state = quiet_mode.migrate_legacy({"spark_quiet_mode": True})
    assert state == {
        "enabled": True,
        "source": "unknown",
        "reason": None,
        "set_at": None,
        "expires_at": None,
    }


def test_legacy_false_migrates_to_disabled_unknown_source():
    state = quiet_mode.migrate_legacy({"spark_quiet_mode": False})
    assert state["enabled"] is False
    assert state["source"] == "unknown"


def test_legacy_missing_migrates_to_disabled():
    state = quiet_mode.migrate_legacy({})
    assert state["enabled"] is False
    assert state["source"] == "unknown"


def test_legacy_truthy_non_true_value_does_not_count():
    # Same discipline as policy.py's `is True` check — 1/"true"/"yes" are not True.
    for value in (1, "true", "yes", [1]):
        state = quiet_mode.migrate_legacy({"spark_quiet_mode": value})
        assert state["enabled"] is False, value


# --- structured precedence ------------------------------------------------


def test_structured_state_wins_over_disagreeing_legacy_true():
    data = {
        "spark_quiet_mode": True,
        "quiet_state": {
            "enabled": False,
            "source": "tool_quiet",
            "reason": "resolved",
            "set_at": 100.0,
            "expires_at": None,
        },
    }
    state = quiet_mode.migrate_legacy(data)
    assert state["enabled"] is False
    assert state["source"] == "tool_quiet"


def test_structured_state_wins_over_disagreeing_legacy_false():
    data = {
        "spark_quiet_mode": False,
        "quiet_state": {
            "enabled": True,
            "source": "tool_transition",
            "reason": "buffer",
            "set_at": 100.0,
            "expires_at": 1300.0,
        },
    }
    state = quiet_mode.migrate_legacy(data)
    assert state["enabled"] is True
    assert state["source"] == "tool_transition"


def test_structured_state_with_null_quiet_state_falls_back_to_legacy():
    data = {"spark_quiet_mode": True, "quiet_state": None}
    state = quiet_mode.migrate_legacy(data)
    assert state["enabled"] is True
    assert state["source"] == "unknown"


# --- indefinite quiet ------------------------------------------------------


def test_indefinite_quiet_stays_active_at_any_future_now():
    data = {
        "quiet_state": {
            "enabled": True,
            "source": "tool_quiet",
            "reason": "three_ss",
            "set_at": 1000.0,
            "expires_at": None,
        }
    }
    assert quiet_mode.resolve(data, now=1000.0) is True
    assert quiet_mode.resolve(data, now=10_000_000.0) is True


# --- temporary quiet: before / at / after expiry ---------------------------


def test_temporary_quiet_active_before_expiry():
    data = {
        "quiet_state": {
            "enabled": True,
            "source": "tool_transition",
            "reason": "buffer",
            "set_at": 1000.0,
            "expires_at": 1000.0 + 1200,
        }
    }
    assert quiet_mode.resolve(data, now=1000.0 + 600) is True


def test_temporary_quiet_inactive_at_exact_expiry():
    data = {
        "quiet_state": {
            "enabled": True,
            "source": "tool_transition",
            "reason": "buffer",
            "set_at": 1000.0,
            "expires_at": 1000.0 + 1200,
        }
    }
    assert quiet_mode.resolve(data, now=1000.0 + 1200) is False


def test_temporary_quiet_inactive_after_expiry():
    data = {
        "quiet_state": {
            "enabled": True,
            "source": "tool_transition",
            "reason": "buffer",
            "set_at": 1000.0,
            "expires_at": 1000.0 + 1200,
        }
    }
    assert quiet_mode.resolve(data, now=1000.0 + 1201) is False


def test_expiry_never_mutates_the_record_on_disk():
    # resolve() is read-time only — an expired record stays enabled=True in
    # migrate_legacy()'s view; only a real writer clears it.
    data = {
        "quiet_state": {
            "enabled": True,
            "source": "tool_transition",
            "reason": "buffer",
            "set_at": 1000.0,
            "expires_at": 1000.0 + 1200,
        }
    }
    assert quiet_mode.resolve(data, now=1000.0 + 1201) is False
    assert quiet_mode.migrate_legacy(data)["enabled"] is True


def test_disabled_record_ignores_a_malformed_expires_at():
    data = {"quiet_state": {"enabled": False, "expires_at": "banana"}}
    assert quiet_mode.resolve(data, now=1000.0) is False


def test_well_formed_record_with_malformed_expires_at_treated_as_indefinite():
    data = {
        "quiet_state": {
            "enabled": True,
            "source": "tool_transition",
            "expires_at": "banana",
        }
    }
    assert quiet_mode.resolve(data, now=0.0) is True
    assert quiet_mode.resolve(data, now=10_000_000.0) is True


def test_past_negative_expires_at_is_a_valid_already_expired_value():
    data = {"quiet_state": {"enabled": True, "source": "x", "expires_at": -5.0}}
    assert quiet_mode.resolve(data, now=0.0) is False


# --- malformed records fail conservatively ----------------------------------


def test_non_dict_quiet_state_fails_conservatively_enabled():
    for garbage in ("banana", 1, [1, 2], True):
        state = quiet_mode.migrate_legacy({"quiet_state": garbage})
        assert state["enabled"] is True, garbage
        assert state["source"] == "malformed_fallback"


def test_dict_quiet_state_missing_enabled_fails_conservatively():
    state = quiet_mode.migrate_legacy({"quiet_state": {"source": "tool_quiet"}})
    assert state["enabled"] is True
    assert state["source"] == "malformed_fallback"


def test_dict_quiet_state_with_non_bool_enabled_fails_conservatively():
    for garbage in ("yes", 1, None, [True]):
        state = quiet_mode.migrate_legacy({"quiet_state": {"enabled": garbage}})
        assert state["enabled"] is True, garbage
        assert state["source"] == "malformed_fallback"


def test_malformed_quiet_state_resolves_active_regardless_of_legacy_bool():
    # Malformed structured state is evidence something broke, not evidence
    # quiet mode is off — it does not fall back to the legacy bool either.
    data = {"spark_quiet_mode": False, "quiet_state": "banana"}
    assert quiet_mode.resolve(data, now=0.0) is True


# --- no mutation of input unless explicitly intended ------------------------


def test_migrate_legacy_does_not_mutate_input():
    data = {
        "spark_quiet_mode": True,
        "quiet_state": {
            "enabled": True,
            "source": "tool_quiet",
            "reason": "three_ss",
            "set_at": 1.0,
            "expires_at": None,
        },
    }
    before = copy.deepcopy(data)
    quiet_mode.migrate_legacy(data)
    assert data == before


def test_resolve_does_not_mutate_input():
    data = {
        "quiet_state": {
            "enabled": True,
            "source": "tool_transition",
            "reason": "buffer",
            "set_at": 1000.0,
            "expires_at": 1000.0 + 1200,
        }
    }
    before = copy.deepcopy(data)
    quiet_mode.resolve(data, now=1000.0 + 1201)
    assert data == before


def test_new_state_does_not_read_or_touch_any_session_dict():
    state = quiet_mode.new_state(enabled=True, source="tool_quiet")
    assert state["enabled"] is True
    assert state["reason"] is None
    assert state["set_at"] is None
    assert state["expires_at"] is None


# --- one-shot durable migration helpers (#303) ---------------------------
#
# These are the pure halves of state._persist_canonical_quiet(); the
# write-path pins (file actually converted, exactly once, under the lock)
# live in tests/test_state.py.


def test_needs_migration_true_for_legacy_only_shape():
    assert quiet_mode.needs_migration({"spark_quiet_mode": True}) is True
    assert quiet_mode.needs_migration({"spark_quiet_mode": False}) is True


def test_needs_migration_true_when_quiet_state_is_explicit_null():
    # JSON null and a missing key are the same "never written" claim —
    # default_state() historically wrote quiet_state: null next to the bool.
    assert quiet_mode.needs_migration(
        {"spark_quiet_mode": True, "quiet_state": None}
    ) is True


def test_needs_migration_false_without_a_legacy_key():
    # Nothing to import — inventing a record here would be fabricated
    # provenance for a session that never had legacy authority.
    assert quiet_mode.needs_migration({}) is False
    assert quiet_mode.needs_migration({"quiet_state": None}) is False


def test_needs_migration_false_once_canonical_state_exists():
    data = {
        "spark_quiet_mode": True,
        "quiet_state": {"enabled": False, "source": "tool_quiet"},
    }
    assert quiet_mode.needs_migration(data) is False


def test_needs_migration_false_for_malformed_quiet_state():
    # Garbage must stay on disk as evidence and keep failing closed at read
    # time — a durable migration must not launder it into a tidy record.
    data = {"spark_quiet_mode": False, "quiet_state": {"enabled": "yes"}}
    assert quiet_mode.needs_migration(data) is False
    assert quiet_mode.resolve(data, now=0.0) is True  # still fails closed


def test_migration_record_imports_legacy_true_conservatively():
    record = quiet_mode.migration_record({"spark_quiet_mode": True})
    assert record["enabled"] is True
    assert record["source"] == quiet_mode.SOURCE_UNKNOWN
    assert record["reason"] == quiet_mode.REASON_LEGACY_MIGRATION
    # Nothing fabricated: no set_at (nobody recorded when), no expires_at
    # (the legacy shape had no TTL — inventing one would auto-clear a latch
    # whose origin is exactly what we don't know).
    assert record["set_at"] is None
    assert record["expires_at"] is None


def test_migration_record_imports_legacy_false_as_disabled():
    record = quiet_mode.migration_record({"spark_quiet_mode": False})
    assert record["enabled"] is False
    assert record["source"] == quiet_mode.SOURCE_UNKNOWN
    assert record["reason"] == quiet_mode.REASON_LEGACY_MIGRATION


def test_migration_record_strict_identity_on_the_legacy_bool():
    # Same `is True` discipline as migrate_legacy — a stray "true"/1 does
    # not count as a real toggle, so it imports as disabled.
    assert quiet_mode.migration_record({"spark_quiet_mode": "true"})["enabled"] is False
    assert quiet_mode.migration_record({"spark_quiet_mode": 1})["enabled"] is False


def test_migration_preserves_read_time_resolution():
    # A file must resolve identically before and after its migration write —
    # the migration changes the shape, never the effective answer.
    for legacy in (True, False, "true", None):
        data = {"spark_quiet_mode": legacy} if legacy is not None else {}
        before = quiet_mode.resolve(data, now=12345.0)
        migrated = {"quiet_state": quiet_mode.migration_record(data)}
        assert quiet_mode.resolve(migrated, now=12345.0) is before


def test_has_canonical_state_only_for_well_formed_records():
    assert quiet_mode.has_canonical_state(
        {"quiet_state": {"enabled": True, "source": "x"}}
    ) is True
    assert quiet_mode.has_canonical_state({"quiet_state": None}) is False
    assert quiet_mode.has_canonical_state({}) is False
    assert quiet_mode.has_canonical_state({"quiet_state": {"enabled": "yes"}}) is False
    assert quiet_mode.has_canonical_state({"quiet_state": True}) is False
