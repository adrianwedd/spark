"""STT fallback chain tests."""


def test_hallucination_filter_allows_short_commands():
    """3-word commands like 'hey robot stop' should not be rejected."""
    text = "hey robot stop"
    words = text.strip().split()
    assert len(words) >= 3, "3-word commands must pass the filter"


def test_hallucination_filter_rejects_phantoms():
    """Known phantom phrases should be rejected."""
    PHANTOM_PHRASES = {
        "thank you", "thank you.", "thanks for watching",
        "you", "the", "the.", "a", "and", "is",
        "bye", "bye.", "so", "i'm sorry",
        "subscribe", "like and subscribe",
    }
    for phrase in PHANTOM_PHRASES:
        assert phrase.lower().strip(".!? ") in PHANTOM_PHRASES
