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
