"""Status-card rendering for Hub Max announcements.

Visual language is inherited from adrianwedd.com (see
scripts/generate-og-images.py in that repo): same palette, same top/bottom
accent bars, system sans rather than the SPARK thought card's mono-italic —
a card read from across a room needs a humanist sans, not a typewriter face.

Divergence from the reference worth stating: a missing truetype font is an
ERROR here, not a fallback. The reference tolerates ImageFont.load_default()
because a slightly-wrong OG image still functions; a bitmap-font card at two
metres is illegible, and raising routes the caller into the audio fallback,
which is the better failure.
"""
import datetime as _dt
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import store

W, H = 1280, 800

BG = (26, 24, 28)        # #1a181c  --color-surface
TEXT = (226, 221, 216)   # #e2ddd8  --color-text
MUTED = (150, 142, 150)  # #968e96  --color-text-muted
COPPER = (196, 139, 110) # #c48b6e  --color-accent
BORDER = (61, 56, 68)    # #3d3844  --color-border

VARIANT_ACCENTS = {
    "meds":  (248, 113, 113),  # #f87171  --color-status-error
    "wan":   (192, 132, 252),  # #c084fc  --color-status-experiment
    "task":  (196, 139, 110),  # #c48b6e  --color-accent
    "water": (74, 222, 128),   # #4ade80  --color-status-active
}

# Probed in order. SFPro-Bold.otf / SFNSDisplay-Bold.otf appear in the site's
# script but do NOT exist on this Mac (checked 2026-08-06) — its OG images have
# been rendering in Helvetica all along. SFNS.ttf is the real system face.
_FONT_CANDIDATES_BOLD = [
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
_FONT_CANDIDATES_REGULAR = [
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]

ACCENT_BAR = 6
MARGIN_X = 90
MAX_BODY_LINES = 6


class CardError(Exception):
    """A legible card could not be produced.

    Every failure inside render_card is normalised to this type. The endpoint's
    audio fallback is load-bearing, and Pillow raises a wide and version-
    dependent set (ValueError, TypeError, OSError) that a narrow except would
    let through as an HTTP 500 — silently dropping the announcement.
    """


def _font(candidates: list[str], size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in candidates:
        try:
            f = ImageFont.truetype(path, size)
        except OSError:
            continue
        if bold:
            # SFNS.ttf is a variable font; the Bold instance must be selected
            # explicitly or it renders at Regular weight. Static faces raise.
            try:
                f.set_variation_by_name("Bold")
            except (OSError, AttributeError, ValueError):
                pass
        return f
    raise CardError("no truetype font available — refusing to render a bitmap card")


def _wrap(draw, text: str, font, max_px: int, max_lines: int) -> list[str]:
    """Wrap to a PIXEL width, splitting tokens that do not fit on their own.

    textwrap.fill() counts characters, which neither respects the glyph widths
    of a proportional face nor breaks a single long token — a pasted URL in an
    LLM-written reminder would run straight off the canvas.
    """
    words, lines, cur = (text or "").split(), [], ""

    def width(t: str) -> float:
        return draw.textlength(t, font=font)

    for word in words:
        while width(word) > max_px:          # token longer than the whole line
            cut = len(word)
            while cut > 1 and width(word[:cut] + "-") > max_px:
                cut -= 1
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:cut] + "-")
            word = word[cut:]
        trial = f"{cur} {word}".strip()
        if width(trial) <= max_px:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and width(last + "…") > max_px:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def render_card(headline: str, body: str, variant: str = "task",
                when: str | None = None) -> Path:
    """Render a 1280x800 status card. Returns the PNG path. Raises CardError."""
    try:
        return _render(headline, body, variant, when)
    except CardError:
        raise
    except Exception as exc:                      # noqa: BLE001 — see CardError
        raise CardError(f"card render failed: {exc!r}") from exc


def _render(headline: str, body: str, variant: str, when: str | None) -> Path:
    accent = VARIANT_ACCENTS.get((variant or "").lower(), COPPER)

    f_head = _font(_FONT_CANDIDATES_BOLD, 64, bold=True)
    f_body = _font(_FONT_CANDIDATES_REGULAR, 32)
    f_meta = _font(_FONT_CANDIDATES_BOLD, 22, bold=True)

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # PIL rectangle endpoints are INCLUSIVE — [0,0,W,ACCENT_BAR] would be 7px
    # tall and one column off-canvas.
    draw.rectangle((0, 0, W - 1, ACCENT_BAR - 1), fill=accent)
    draw.rectangle((0, H - ACCENT_BAR, W - 1, H - 1), fill=accent)

    text_w = W - MARGIN_X * 2
    head_lines = _wrap(draw, headline, f_head, text_w, 2)
    body_lines = _wrap(draw, body, f_body, text_w, MAX_BODY_LINES)

    # Place the block optically rather than at a fixed y: a one-line card at a
    # fixed offset leaves the bottom two-thirds empty on a 16:10 screen. Bias
    # above centre (0.42) — text pinned to true centre reads as sagging.
    HEAD_LH, BODY_LH, RULE_H = 78, 46, 42
    block_h = len(head_lines) * HEAD_LH + RULE_H + len(body_lines) * BODY_LH
    usable_top, usable_bottom = ACCENT_BAR + 40, H - 110
    y = usable_top + int(((usable_bottom - usable_top) - block_h) * 0.42)

    for line in head_lines:
        draw.text((MARGIN_X, y), line, font=f_head, fill=TEXT)
        y += HEAD_LH

    y += 8
    draw.line([(MARGIN_X, y), (MARGIN_X + 180, y)], fill=BORDER, width=2)
    y += RULE_H - 8

    for line in body_lines:
        draw.text((MARGIN_X, y), line, font=f_body, fill=MUTED)
        y += BODY_LH

    bar_y = H - 78
    dot_r = 7
    draw.ellipse(
        [MARGIN_X, bar_y + 4, MARGIN_X + dot_r * 2, bar_y + 4 + dot_r * 2],
        fill=accent,
    )
    draw.text((MARGIN_X + dot_r * 2 + 14, bar_y), (variant or "task").upper(),
              font=f_meta, fill=accent)

    stamp = when or _dt.datetime.now().strftime("%H:%M")
    stamp_w = draw.textlength(stamp, font=f_meta)
    draw.text((W - MARGIN_X - stamp_w, bar_y), stamp, font=f_meta, fill=MUTED)

    out = store.private_path_ext("png")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out), "PNG", optimize=True)
    return out
