"""Editorial invariants for the public SPARK story."""
from pathlib import Path


SITE = Path(__file__).resolve().parents[1] / "site"


def _read(relative: str) -> str:
    return (SITE / relative).read_text(encoding="utf-8")


def test_homepage_leads_with_persistence_and_three_boundaries():
    page = _read("index.html")

    assert "A robot that stays around." in page
    assert "One persistent brain." in page
    assert "PERSISTS" in page
    assert "KNOWS HOW IT KNOWS" in page
    assert "CANNOT GRANT ITSELF POWER" in page


def test_homepage_explains_current_cognition_routes():
    page = _read("index.html")

    assert "Local perception" in page
    assert "M5 local cognition" in page
    assert "Resident Claude" in page
    assert "Deterministic policy" in page
    assert "public chat" in page
    assert "Obi chat" in page
    assert "post QA" in page
    assert "blog QA" in page
    assert "resident mode" in page


def test_homepage_names_durable_provenance_types():
    page = _read("index.html")

    for kind in ("observation", "report", "model_perception", "inference", "narrative", "verification"):
        assert f"<code>{kind}</code>" in page


def test_public_html_rejects_retired_architecture_story():
    public_html = "\n".join(
        _read(path) for path in (
            "index.html", "feed/index.html", "thought/index.html", "blog/index.html"
        )
    )

    assert "robot with an inner life" not in public_html.lower()
    assert "Claude CLI (px-spark)" not in public_html
    assert "Four-Tier LLM Fallback" not in public_html
    assert "Tier 1: Ollama on M5" not in public_html


def test_supporting_pages_place_content_in_continuous_life():
    feed = _read("feed/index.html")
    thought = _read("thought/index.html")
    blog = _read("blog/index.html")

    assert "between conversations" in feed
    assert "persistent robot" in thought
    assert "continuous life" in blog


def test_homepage_metadata_matches_new_story():
    page = _read("index.html")

    assert "SPARK — a robot that stays around" in page
    assert "persistent embodied agent" in page
    assert '"@type": "WebSite"' in page
