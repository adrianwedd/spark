"""Editorial invariants for the public SPARK story and canonical roadmap."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"


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


def test_repository_readme_leads_with_current_architecture():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert readme.startswith("# SPARK\n")
    assert "A robot that stays around." in readme
    assert "one resident Claude brain" in readme
    assert "Reflection | M5/Ollama" in readme
    assert "Production forbids cold `claude -p` paths" in readme

    for retired in (
        "# PiCar-X Hacking",
        "### The Three Brains",
        "Claude-powered robot companion",
        "Four backends share the same",
    ):
        assert retired not in readme


def test_roadmap_is_specific_to_current_spark_not_generic_robotics_bingo():
    roadmap = (ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8")

    for current in (
        "Separate Obi companion UI from Adrian/admin UI",
        "Retire legacy pseudo-agency",
        "Finish GPIO lease migration",
        "Persistent spatial memory",
        "Explain “why did you do/say that?”",
        "Autonomous docking + energy awareness",
    ):
        assert current in roadmap

    for retired in (
        "reinforcement learning with a simulation “dream buffer”",
        "policy sharing across fleet units",
        "multi-car choreographed demos",
        "central knowledge base syncing maps/logs",
    ):
        assert retired not in roadmap
