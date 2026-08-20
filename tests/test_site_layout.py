"""Browser-based layout regression tests for the live dashboard.

These tests render the static site in a real headless browser (Playwright/Chromium)
and assert structural geometry — not pixels.  They exist because a previous "site
QA" checked that the right elements existed but never checked that they composed
correctly: the Environment section's Sound + Noise row had an arbitrary max-width
that left a huge dead region to the right, while Temp/Wind and Humidity/Rain below
it correctly filled the container width.

The bug passed correctness QA and failed composition QA.  These tests close that
gap by asserting the *geometry* of the rendered layout at multiple viewport widths.

Requires: playwright (dev dependency).  Skipped automatically when playwright or
a browser binary is not installed, so the main pytest gate stays green on
environments that only run the Python suite.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# ── Optional dependency: playwright ──────────────────────────────────────────
# The test suite's primary gate runs `pytest -m "not live"` in CI.  Playwright
# requires a browser binary install step that is not part of the standard test
# workflow.  We import lazily and skip the entire module when it is unavailable
# rather than adding a hard dependency to the main test path.

try:
    from playwright.sync_api import sync_playwright as _pw_sync_playwright  # noqa: F401
    _PW_AVAILABLE = True
except ImportError:
    _PW_AVAILABLE = False

SITE_DIR = Path(__file__).resolve().parents[1] / "site"
INDEX_HTML = SITE_DIR / "index.html"

pytestmark = pytest.mark.skipif(
    not _PW_AVAILABLE or not INDEX_HTML.exists(),
    reason="playwright not installed or site/index.html not found",
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _launch_browser():
    """Launch a headless Chromium browser via Playwright."""
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    return pw, browser


def _open_page(browser, viewport_width=1280, viewport_height=900):
    """Open index.html at the given viewport size and return the page."""
    page = browser.new_page(viewport={"width": viewport_width, "height": viewport_height})
    page.goto(INDEX_HTML.as_uri())
    # Wait for the world details to be open (it's a <details> that defaults to open
    # in the HTML, but give the browser a moment to lay out).
    page.wait_for_load_state("networkidle")
    return page


def _get_box(page, selector):
    """Return the bounding box of the first element matching selector, or None."""
    el = page.query_selector(selector)
    if not el:
        return None
    return el.bounding_box()


# ── Tests: desktop layout (1280px wide) ──────────────────────────────────────

class TestEnvironmentLayoutDesktop:
    """Desktop: Sound+Noise row must fill the container like the weather grid."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        pw, browser = _launch_browser()
        self.pw = pw
        self.browser = browser
        self.page = _open_page(browser, viewport_width=1280, viewport_height=900)
        yield
        self.page.close()
        self.browser.close()
        self.pw.stop()

    def test_sound_noise_row_width_approximates_weather_grid(self):
        """The sound-noise row width should be within ~5% of the weather grid width.

        Before the fix, sound-noise-row had max-width:480px while the weather grid
        filled the full container (~840px), leaving a large dead region.
        """
        sn_box = _get_box(self.page, ".band-world .sound-noise-row")
        wx_box = _get_box(self.page, ".band-world .wx-card-grid")
        assert sn_box is not None, ".sound-noise-row not found"
        assert wx_box is not None, ".wx-card-grid not found"

        # Both should be close to the full container width.
        # Tolerance: 5% of the weather grid width (accounts for scrollbar rounding).
        tolerance = wx_box["width"] * 0.05
        delta = abs(sn_box["width"] - wx_box["width"])
        assert delta <= tolerance, (
            f"sound-noise-row width ({sn_box['width']:.1f}px) differs from "
            f"weather grid width ({wx_box['width']:.1f}px) by {delta:.1f}px "
            f"(tolerance {tolerance:.1f}px) — dead-space regression"
        )

    def test_sound_noise_right_edge_aligns_with_weather_grid(self):
        """Right edges of sound-noise row and weather grid must align within 8px."""
        sn_box = _get_box(self.page, ".band-world .sound-noise-row")
        wx_box = _get_box(self.page, ".band-world .wx-card-grid")
        assert sn_box is not None
        assert wx_box is not None

        # Left edges should also align (both start at the same x).
        assert abs(sn_box["x"] - wx_box["x"]) <= 2, (
            f"left edge misaligned: sound-noise x={sn_box['x']:.1f}, "
            f"weather grid x={wx_box['x']:.1f}"
        )

        # Right edges should align within 8px tolerance.
        sn_right = sn_box["x"] + sn_box["width"]
        wx_right = wx_box["x"] + wx_box["width"]
        right_delta = abs(sn_right - wx_right)
        assert right_delta <= 8, (
            f"right edge misaligned: sound-noise right={sn_right:.1f}, "
            f"weather grid right={wx_right:.1f}, delta={right_delta:.1f}px"
        )

    def test_sound_noise_cards_are_side_by_side(self):
        """Sound and Noise cards must be in a 2-column row, not stacked."""
        sound_box = _get_box(self.page, ".band-world .wx-card-sound")
        noise_box = _get_box(self.page, ".band-world .wx-card-noise")
        assert sound_box is not None and noise_box is not None

        # They should be on the same row (same top y within 2px).
        assert abs(sound_box["y"] - noise_box["y"]) <= 2, (
            f"Sound and Noise cards are not on the same row: "
            f"sound y={sound_box['y']:.1f}, noise y={noise_box['y']:.1f}"
        )
        # Sound should be to the left of Noise.
        assert sound_box["x"] < noise_box["x"], "Sound card should be left of Noise card"

    def test_no_child_overflow_in_environment(self):
        """No element inside band-world should overflow its parent's width."""
        overflow = self.page.evaluate("""() => {
            const band = document.querySelector('.band-world');
            if (!band) return [];
            const bandRect = band.getBoundingClientRect();
            const results = [];
            band.querySelectorAll('*').forEach(el => {
                const r = el.getBoundingClientRect();
                const right = r.right;
                const bandRight = bandRect.right;
                // Allow 1px rounding; flag anything overflowing by more than 1px
                if (right > bandRight + 1) {
                    results.push({
                        tag: el.tagName,
                        class: el.className.toString().slice(0, 60),
                        right: Math.round(right),
                        bandRight: Math.round(bandRight),
                        overflow: Math.round(right - bandRight),
                    });
                }
            });
            return results;
        }""")
        assert overflow == [], (
            f"Child elements overflow band-world: {json.dumps(overflow[:5], indent=2)}"
        )

    def test_band_world_fills_container(self):
        """band-world should fill most of the container width (no dead space)."""
        band_box = _get_box(self.page, ".band-world")
        container_box = _get_box(self.page, "#live .container")
        assert band_box is not None and container_box is not None

        # band-world should be at least 80% of the container width.
        ratio = band_box["width"] / container_box["width"]
        assert ratio >= 0.80, (
            f"band-world fills only {ratio:.1%} of container width — "
            f"dead-space regression (band={band_box['width']:.1f}px, "
            f"container={container_box['width']:.1f}px)"
        )


# ── Tests: laptop layout (1024px wide) ──────────────────────────────────────

class TestEnvironmentLayoutLaptop:
    """Ordinary laptop viewport — same alignment invariants."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        pw, browser = _launch_browser()
        self.pw = pw
        self.browser = browser
        self.page = _open_page(browser, viewport_width=1024, viewport_height=768)
        yield
        self.page.close()
        self.browser.close()
        self.pw.stop()

    def test_sound_noise_aligns_with_weather_grid(self):
        sn_box = _get_box(self.page, ".band-world .sound-noise-row")
        wx_box = _get_box(self.page, ".band-world .wx-card-grid")
        assert sn_box and wx_box
        tolerance = wx_box["width"] * 0.05
        assert abs(sn_box["width"] - wx_box["width"]) <= tolerance


# ── Tests: tablet layout (768px wide) ────────────────────────────────────────

class TestEnvironmentLayoutTablet:
    """Tablet viewport — 2-column layout should hold."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        pw, browser = _launch_browser()
        self.pw = pw
        self.browser = browser
        self.page = _open_page(browser, viewport_width=768, viewport_height=1024)
        yield
        self.page.close()
        self.browser.close()
        self.pw.stop()

    def test_sound_noise_aligns_with_weather_grid(self):
        sn_box = _get_box(self.page, ".band-world .sound-noise-row")
        wx_box = _get_box(self.page, ".band-world .wx-card-grid")
        assert sn_box and wx_box
        tolerance = wx_box["width"] * 0.05
        assert abs(sn_box["width"] - wx_box["width"]) <= tolerance


# ── Tests: narrow mobile (375px wide) ───────────────────────────────────────

class TestEnvironmentLayoutMobile:
    """Narrow mobile — cards should stack vertically, no horizontal overflow."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        pw, browser = _launch_browser()
        self.pw = pw
        self.browser = browser
        self.page = _open_page(browser, viewport_width=375, viewport_height=812)
        yield
        self.page.close()
        self.browser.close()
        self.pw.stop()

    def test_sound_noise_stacks_vertically(self):
        """On mobile (375px), Sound and Noise should stack (not side-by-side)."""
        sound_box = _get_box(self.page, ".band-world .wx-card-sound")
        noise_box = _get_box(self.page, ".band-world .wx-card-noise")
        assert sound_box and noise_box

        # Noise should be below Sound (greater y), not beside it.
        assert noise_box["y"] > sound_box["y"] + sound_box["height"] * 0.5, (
            f"Expected Noise below Sound on mobile: "
            f"sound y={sound_box['y']:.1f} h={sound_box['height']:.1f}, "
            f"noise y={noise_box['y']:.1f}"
        )

    def test_no_horizontal_overflow_mobile(self):
        """No element should exceed the viewport width on mobile."""
        overflow = self.page.evaluate("""() => {
            const vw = document.documentElement.clientWidth;
            const results = [];
            document.querySelectorAll('#live *').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.right > vw + 1) {
                    results.push({
                        tag: el.tagName,
                        class: el.className.toString().slice(0, 50),
                        right: Math.round(r.right),
                        vw: vw,
                    });
                }
            });
            return results;
        }""")
        assert overflow == [], (
            f"Horizontal overflow on mobile: {json.dumps(overflow[:5], indent=2)}"
        )

    def test_weather_cards_stack_vertically(self):
        """Weather grid should also stack on mobile."""
        cards = self.page.query_selector_all(".band-world .wx-card-grid .wx-card")
        assert len(cards) >= 4, "Expected at least 4 weather cards"
        # Each card should be full-width (not half-width)
        grid_box = _get_box(self.page, ".band-world .wx-card-grid")
        assert grid_box
        for card in cards:
            box = card.bounding_box()
            if box:
                # Card width should be close to grid width (stacked, not side-by-side)
                assert box["width"] >= grid_box["width"] * 0.9, (
                    f"Weather card width {box['width']:.1f}px is less than "
                    f"90% of grid width {grid_box['width']:.1f}px — not stacked"
                )