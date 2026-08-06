import pytest
from PIL import Image

from announce_relay import card


def test_render_card_writes_png_at_hub_dimensions(tmp_dirs):
    p = card.render_card("8pm dose", "Time for your evening meds.", "meds")
    assert p.exists()
    assert p.parent == tmp_dirs["priv"]
    with Image.open(p) as img:
        assert img.size == (1280, 800)


def test_accent_bar_uses_variant_colour(tmp_dirs):
    p = card.render_card("WAN down", "Starlink dropped at 3am.", "wan")
    with Image.open(p) as img:
        # Top accent bar, sampled inside the 6px band.
        assert img.convert("RGB").getpixel((640, 2)) == (192, 132, 252)  # #c084fc


def test_unknown_variant_falls_back_to_copper_not_error(tmp_dirs):
    p = card.render_card("Something", "Body text.", "not-a-variant")
    with Image.open(p) as img:
        assert img.convert("RGB").getpixel((640, 2)) == (196, 139, 110)  # #c48b6e


def test_ground_colour_is_site_surface(tmp_dirs):
    p = card.render_card("Hi", "There.", "task")
    with Image.open(p) as img:
        # Well below the accent bar, right of the text column.
        assert img.convert("RGB").getpixel((1240, 400)) == (26, 24, 28)  # #1a181c


def test_long_body_truncates_without_overflowing(tmp_dirs):
    # Sampled INSIDE the bottom accent bar (y=797, bar spans 794-800) with a
    # non-copper variant, so this fails loudly if body text overruns the bar.
    # An earlier draft sampled y=742, which is above the bar and passed
    # trivially for any MAX_BODY_LINES — it verified nothing.
    body = "word " * 400
    p = card.render_card("Task", body, "meds")
    with Image.open(p) as img:
        assert img.convert("RGB").getpixel((640, 797)) == (248, 113, 113)  # #f87171


def test_unbreakable_token_is_split_not_run_off_canvas(tmp_dirs):
    # A pasted URL is a single token far wider than the text column.
    p = card.render_card("Link", "https://" + "x" * 300 + ".example.com/path", "task")
    with Image.open(p) as img:
        rgb = img.convert("RGB")
        # Right margin must stay clear of glyphs down the whole text column.
        for y in range(150, 700, 10):
            assert rgb.getpixel((1275, y)) == (26, 24, 28)


def test_missing_truetype_raises_rather_than_bitmap_fallback(tmp_dirs, monkeypatch):
    monkeypatch.setattr(card, "_FONT_CANDIDATES_BOLD", [])
    monkeypatch.setattr(card, "_FONT_CANDIDATES_REGULAR", [])
    with pytest.raises(card.CardError):
        card.render_card("8pm dose", "Body.", "meds")


def test_pillow_failure_is_normalised_to_carderror(tmp_dirs, monkeypatch):
    # The audio fallback keys off CardError; a raw ValueError would 500.
    def boom(*a, **kw):
        raise ValueError("pillow internals changed")
    monkeypatch.setattr(card, "_render", boom)
    with pytest.raises(card.CardError):
        card.render_card("x", "y", "task")
