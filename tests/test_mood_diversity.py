"""Mood diversity mechanism tests."""
from pxh.spark_config import SPARK_ANGLES, _pick_spark_angles


def test_angles_are_tuples_with_mood():
    """Each angle is (text, target_mood) tuple."""
    assert len(SPARK_ANGLES) >= 42
    for angle in SPARK_ANGLES:
        assert isinstance(angle, tuple), f"Expected tuple, got {type(angle)}: {angle}"
        assert len(angle) == 2, f"Expected (text, mood), got {angle}"
        text, mood = angle
        assert isinstance(text, str) and len(text) > 10
        assert isinstance(mood, str) and len(mood) > 2


def test_pick_angles_returns_tuples():
    """_pick_spark_angles returns (text, mood) tuples."""
    picked = _pick_spark_angles(5)
    assert len(picked) == 5
    for text, mood in picked:
        assert isinstance(text, str)
        assert isinstance(mood, str)


def test_angles_cover_all_moods():
    """Angle set covers all 12 moods at least twice."""
    from pxh.mind import VALID_MOODS
    mood_counts = {}
    for _text, mood in SPARK_ANGLES:
        mood_counts[mood] = mood_counts.get(mood, 0) + 1
    for m in VALID_MOODS:
        assert mood_counts.get(m, 0) >= 2, f"Mood '{m}' has < 2 angles"


def test_mood_fallback_is_random_not_content():
    """Invalid/missing mood should pick randomly, not default to content."""
    from pxh.mind import VALID_MOODS
    from pxh.spark_config import _SYS_RNG
    results = set()
    for _ in range(100):
        parsed_mood = "INVALID_MOOD"
        if parsed_mood not in VALID_MOODS:
            mood = _SYS_RNG.choice(sorted(VALID_MOODS))
        results.add(mood)
    assert len(results) >= 4, f"Fallback only produced {results}"


def test_mood_alpha_increased():
    """MOOD_ALPHA should be >= 0.5 (new mood gets majority weight)."""
    from pxh.mind import MOOD_ALPHA
    assert MOOD_ALPHA >= 0.5, f"MOOD_ALPHA={MOOD_ALPHA} is too sticky"


def test_reflection_suffix_discourages_default_moods():
    """Prompt suffix warns against defaulting to contemplative/content."""
    from pxh.spark_config import _SPARK_REFLECTION_SUFFIX
    assert "contemplative" in _SPARK_REFLECTION_SUFFIX.lower()
    assert "default" in _SPARK_REFLECTION_SUFFIX.lower() or "habit" in _SPARK_REFLECTION_SUFFIX.lower()
