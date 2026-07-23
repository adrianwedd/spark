from pxh.context_format import (
    format_calendar,
    format_household,
    format_introspection,
    format_routines,
)


def test_formatters_are_pure_and_empty_safe():
    assert format_routines(None) == ""
    assert format_household(None) == ""
    assert format_calendar([]) == ""
    assert "No introspection data" in format_introspection({})


def test_context_domains_remain_separate():
    assert "Meds" in format_routines({"meds_taken": False})
    assert "video call" in format_household({"adrian_on_call": True})
    assert "Coming up" in format_calendar([
        {"title": "School pickup", "starts_in_mins": 20}
    ])
    assert "curious 60%" in format_introspection({
        "mood_distribution": {"curious": 60}
    })
