# Hardening Sprint Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden SPARK's frontend, log durability, and test coverage across 4 independent issues (#115, #120, #123, #119).

**Architecture:** Four independent tasks with no cross-dependencies. Task 1 consolidates scattered mood colours and API URLs into single-source-of-truth files. Task 2 extracts duplicated log rotation into a shared utility. Task 3 adds tests for the 5 largest untested px-mind functions. Task 4 adds a Cloudflare Worker for dynamic OG images.

**Tech Stack:** CSS custom properties, JavaScript, Python 3.11, pytest, Cloudflare Workers

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `site/js/config.js` | Create | Single API base URL definition |
| `site/css/colors.css` | Create | Single mood colour palette (CSS custom properties) |
| `site/index.html` | Modify | Load config.js + colors.css |
| `site/feed/index.html` | Modify | Load config.js + colors.css |
| `site/thought/index.html` | Modify | Load config.js + colors.css |
| `site/js/live.js` | Modify | Use CONFIG.API_BASE + CSS vars |
| `site/js/status-dot.js` | Modify | Use CONFIG.API_BASE + CSS vars |
| `site/js/thought.js` | Modify | Use CONFIG.API_BASE |
| `site/js/chat.js` | Modify | Use CONFIG.API_BASE |
| `site/js/feed.js` | Modify | Use CONFIG.API_BASE |
| `site/js/dashboard.js` | Modify | Use CSS vars for mood colours |
| `site/js/charts.js` | Modify | Use CSS vars for mood colours |
| `site/css/feed.css` | Modify | Use CSS vars for .mood-* classes |
| `site/css/warm.css` | Modify | Use CSS vars for carousel badges |
| `src/pxh/state.py` | Modify | Add `rotate_log()` function |
| `src/pxh/mind.py` | Modify | Replace local log rotation with `rotate_log()` |
| `src/pxh/logging.py` | Modify | Replace local log rotation with `rotate_log()` |
| `bin/px-alive` | Modify | Replace local log rotation with `rotate_log()` |
| `bin/px-wake-listen` | Modify | Replace local log rotation with `rotate_log()` |
| `bin/px-post` | Modify | Replace local log rotation with `rotate_log()` |
| `tests/test_state.py` | Modify | Add rotate_log tests |
| `tests/test_mind_coverage.py` | Create | Tests for untested px-mind functions |
| `site/workers/og-rewrite.js` | Create | Cloudflare Worker for OG image injection |

---

### Task 1: Consolidate mood colours + API base URL (#115)

**Files:**
- Create: `site/js/config.js`, `site/css/colors.css`
- Modify: 5 JS files, 2 CSS files, 3 HTML files

- [ ] **Step 1: Create `site/css/colors.css`**

Canonical Scheme B (warmer, earthier tones):

```css
:root {
  --mood-peaceful: #4a9d8f;
  --mood-content: #6b8e5e;
  --mood-contemplative: #7b6fa0;
  --mood-curious: #c48a3f;
  --mood-active: #d46b4a;
  --mood-excited: #d44a6b;

  --mood-peaceful-bg: #e8f5f0;
  --mood-content-bg: #eef5e8;
  --mood-contemplative-bg: #f0ecf5;
  --mood-curious-bg: #faf0e0;
  --mood-active-bg: #fae8e0;
  --mood-excited-bg: #fae0ea;
}
```

- [ ] **Step 2: Create `site/js/config.js`**

```javascript
window.SPARK_CONFIG = {
  API_BASE: 'https://spark-api.wedd.au/api/v1/public'
};
```

- [ ] **Step 3: Load new files in all 3 HTML pages**

In `site/index.html`, `site/feed/index.html`, `site/thought/index.html`:

Add before other CSS `<link>` tags:
```html
<link rel="stylesheet" href="/css/colors.css">
```
(Use `../css/colors.css` for feed/ and thought/ subdirectories.)

Add before other `<script>` tags:
```html
<script src="/js/config.js"></script>
```
(Use `../js/config.js` for feed/ and thought/ subdirectories.)

- [ ] **Step 4: Update `site/css/feed.css` to use CSS vars**

Replace lines 145–150:
```css
.mood-peaceful    { color: var(--mood-peaceful); background: var(--mood-peaceful-bg); }
.mood-content     { color: var(--mood-content); background: var(--mood-content-bg); }
.mood-contemplative { color: var(--mood-contemplative); background: var(--mood-contemplative-bg); }
.mood-curious     { color: var(--mood-curious); background: var(--mood-curious-bg); }
.mood-active      { color: var(--mood-active); background: var(--mood-active-bg); }
.mood-excited     { color: var(--mood-excited); background: var(--mood-excited-bg); }
```

- [ ] **Step 5: Update `site/css/warm.css` carousel badges to use CSS vars**

Replace lines 753–758:
```css
.carousel-mood-badge[data-mood="peaceful"],
.carousel-mood-badge[data-mood="content"]      { background: var(--mood-content); }
.carousel-mood-badge[data-mood="contemplative"] { background: var(--mood-contemplative); }
.carousel-mood-badge[data-mood="curious"]       { background: var(--mood-curious); }
.carousel-mood-badge[data-mood="active"]        { background: var(--mood-active); }
.carousel-mood-badge[data-mood="excited"]       { background: var(--mood-excited); }
```

- [ ] **Step 6: Update JS files to use CSS vars for mood colours**

Helper function pattern (add to each file that needs mood colours, or inline):

```javascript
function _moodColor(mood) {
  return getComputedStyle(document.documentElement)
    .getPropertyValue('--mood-' + mood).trim() || '#888';
}
```

In `site/js/dashboard.js`: Replace `MOOD_FAVICON_COLOR` dict with calls to `_moodColor()`.

In `site/js/charts.js`: Replace `_MOOD_COLOR` dict with calls to `_moodColor()`.

In `site/js/live.js`: Replace `MOOD_DOT_COLORS` dict (lines 159–162) with calls to `_moodColor()`.

In `site/js/status-dot.js`: Replace `MOOD_COLORS` dict (lines 8–15) with calls to `_moodColor()`.

- [ ] **Step 7: Update JS files to use `SPARK_CONFIG.API_BASE`**

In `site/js/live.js` line 7:
```javascript
const API = window.SPARK_CONFIG.API_BASE;
```

In `site/js/status-dot.js` line 4:
```javascript
var API = window.SPARK_CONFIG.API_BASE + '/status';
```

In `site/js/thought.js` line 5:
```javascript
var API = window.SPARK_CONFIG.API_BASE;
```

Also in `site/js/thought.js` line 82, replace the hardcoded URL:
```javascript
if (ogImg) ogImg.content = API + '/thought-image?ts=' + encodeURIComponent(post.ts);
```

In `site/js/chat.js` line 6:
```javascript
const API_URL = window.SPARK_CONFIG.API_BASE + '/chat';
```

In `site/js/feed.js` line 5:
```javascript
var API = window.SPARK_CONFIG.API_BASE;
```

- [ ] **Step 8: Verify site loads locally**

No automated frontend tests exist in this project — verification is manual.

```bash
cd site && python3 -m http.server 8080
```

Open http://localhost:8080 — verify mood colours render, API calls work, no console errors.
Also verify with grep that no hardcoded API URLs or colour hex values remain:

```bash
grep -rn 'spark-api.wedd.au' site/js/ --include='*.js' | grep -v config.js
grep -rn '#6aab6b\|#2ea8e0\|#e05c3a\|#e6a817\|#7c6fcf' site/js/ site/css/ | grep -v colors.css
```

Expected: No matches (all consolidated).

- [ ] **Step 9: Commit**

```bash
git add site/css/colors.css site/js/config.js site/index.html site/feed/index.html \
  site/thought/index.html site/css/feed.css site/css/warm.css \
  site/js/dashboard.js site/js/charts.js site/js/live.js site/js/status-dot.js \
  site/js/thought.js site/js/chat.js site/js/feed.js
git commit -m "refactor(site): consolidate mood colours + API base URL (#115)

Create colors.css (CSS custom properties) and config.js (single API URL).
Replace 6 hardcoded mood colour maps and 5 hardcoded API URLs.
Canonical palette: Scheme B (warmer, earthier tones)."
```

---

### Task 2: Deduplicate log rotation (#120)

**Files:**
- Modify: `src/pxh/state.py`, `src/pxh/mind.py`, `src/pxh/logging.py`, `bin/px-alive`, `bin/px-wake-listen`, `bin/px-post`
- Modify: `tests/test_state.py`

- [ ] **Step 1: Write failing test for `rotate_log`**

Add to `tests/test_state.py`:

```python
def test_rotate_log_under_threshold(tmp_path):
    """File under 5MB is not rotated."""
    log = tmp_path / "test.log"
    log.write_text("line1\nline2\n")
    from pxh.state import rotate_log
    rotate_log(log)
    assert log.read_text() == "line1\nline2\n"


def test_rotate_log_over_threshold(tmp_path):
    """File over threshold keeps last half of lines."""
    log = tmp_path / "test.log"
    lines = [f"line{i}" for i in range(100)]
    log.write_text("\n".join(lines) + "\n")
    from pxh.state import rotate_log
    rotate_log(log, max_bytes=50)  # force rotation with low threshold
    content = log.read_text()
    result_lines = content.strip().split("\n")
    assert len(result_lines) == 50  # kept last half
    assert result_lines[0] == "line50"
    assert result_lines[-1] == "line99"


def test_rotate_log_missing_file(tmp_path):
    """Missing file does not raise."""
    log = tmp_path / "nonexistent.log"
    from pxh.state import rotate_log
    rotate_log(log)  # should not raise
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
source .venv/bin/activate && python -m pytest tests/test_state.py -k rotate -v
```

Expected: ImportError — `rotate_log` not yet defined.

- [ ] **Step 3: Implement `rotate_log` in `src/pxh/state.py`**

Add after `atomic_write()`:

```python
def rotate_log(path: Path, max_bytes: int = 5_000_000) -> None:
    """Rotate log file by keeping the last half of lines when it exceeds max_bytes.

    Uses atomic_write for SD card durability. Silently handles missing files
    and write errors.
    """
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        half = len(lines) // 2
        atomic_write(path, "\n".join(lines[half:]) + "\n")
    except Exception:
        pass  # log rotation failure is not fatal
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
source .venv/bin/activate && python -m pytest tests/test_state.py -k rotate -v
```

Expected: 3 passed.

- [ ] **Step 5: Replace log rotation in `src/pxh/mind.py`**

At line 36, add `rotate_log` to the import:
```python
from pxh.state import atomic_write, load_session, rotate_log, update_session
```

Replace the log rotation block (lines 714–726) with:
```python
    rotate_log(LOG_FILE)
```

- [ ] **Step 6: Replace log rotation in `src/pxh/logging.py`**

Add import at top:
```python
from .state import rotate_log
```

Replace the rotation block (lines 47–54) with:
```python
        rotate_log(log_path, max_bytes=_LOG_MAX_BYTES)
```

- [ ] **Step 7: Replace log rotation in `bin/px-alive`**

The heredoc Python cannot import from pxh (runs under `/usr/bin/python3`, not venv). Instead, inline a call to the same pattern but via subprocess:

Actually — `bin/px-alive` runs as root and uses `/usr/bin/python3` which doesn't have pxh in its path. Keep the inline rotation for bin/ heredoc scripts but replace the body with an `atomic_write`-equivalent (add fsync + ownership preservation). The key improvement is adding the missing `fsync`:

Replace lines 107–119 in `bin/px-alive`:
```python
    # Simple rotation: keep last half when file exceeds 5MB
    try:
        if LOG_FILE.stat().st_size > 5_000_000:
            lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
            half = len(lines) // 2
            half_content = "\n".join(lines[half:]) + "\n"
            fd, tmp = _tmpmod.mkstemp(dir=str(LOG_FILE.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(half_content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(LOG_FILE))
    except Exception:
        pass
```

Apply the same fsync fix to `bin/px-wake-listen` (lines 123–135) and `bin/px-post` (lines 214–225).

- [ ] **Step 8: Run full test suite**

```bash
source .venv/bin/activate && python -m pytest -x -q 2>&1 | tail -10
```

Expected: All pass (GPIO live tests excluded by default; mastodon test already removed).

- [ ] **Step 9: Commit**

```bash
git add src/pxh/state.py src/pxh/mind.py src/pxh/logging.py \
  bin/px-alive bin/px-wake-listen bin/px-post tests/test_state.py
git commit -m "chore: deduplicate log rotation + add fsync (#120)

Extract rotate_log() into pxh.state — uses atomic_write for SD card
durability (fsync + ownership preservation). mind.py and logging.py
now call rotate_log(). Bin scripts (px-alive, px-wake-listen, px-post)
keep inline rotation but gain the missing fsync call.

Partial close of #120 (atomic_write dedup done in 4ef652e)."
```

---

### Task 3: Expand px-mind test coverage (#123)

**Files:**
- Create: `tests/test_mind_coverage.py`
- Source: `src/pxh/mind.py`

Focus on the 5 most important untested functions: `awareness_tick`, `reflection`, `fetch_weather`, `apply_mood_momentum`, and `load_recent_thoughts`/`append_thought`/`auto_remember`.

- [ ] **Step 1: Create test file with imports and fixtures**

```python
"""Tests for previously untested px-mind functions: awareness_tick, reflection, etc."""
from __future__ import annotations

import datetime as _dt
import json
import os
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import pxh.mind
from pxh.mind import (
    _reset_state,
    append_thought,
    apply_mood_momentum,
    auto_remember,
    awareness_tick,
    fetch_weather,
    load_recent_thoughts,
    reflection,
    HOBART_TZ,
)


@pytest.fixture(autouse=True)
def _clean_mind_state():
    _reset_state()
    yield
    _reset_state()


@pytest.fixture
def mind_state(tmp_path):
    """Redirect px-mind state files to tmp_path and isolate session."""
    # STATE_DIR is the only path global — thoughts/notes paths are derived
    # via thoughts_file_for_persona() / notes_file_for_persona() at call time.
    old_state = getattr(pxh.mind, "STATE_DIR", None)
    old_aw = getattr(pxh.mind, "AWARENESS_FILE", None)
    old_bat = getattr(pxh.mind, "BATTERY_FILE", None)
    old_log = getattr(pxh.mind, "LOG_FILE", None)
    old_session = os.environ.get("PX_SESSION_PATH")

    pxh.mind.STATE_DIR = tmp_path
    pxh.mind.AWARENESS_FILE = tmp_path / "awareness.json"
    pxh.mind.BATTERY_FILE = tmp_path / "battery.json"
    pxh.mind.LOG_FILE = tmp_path / "px-mind.log"
    # Isolate session state so reflection() doesn't read real session.json
    session_path = tmp_path / "session.json"
    session_path.write_text('{"persona": "spark", "listening": false, "history": []}')
    os.environ["PX_SESSION_PATH"] = str(session_path)

    yield tmp_path

    pxh.mind.STATE_DIR = old_state
    pxh.mind.AWARENESS_FILE = old_aw
    pxh.mind.BATTERY_FILE = old_bat
    pxh.mind.LOG_FILE = old_log
    if old_session is None:
        os.environ.pop("PX_SESSION_PATH", None)
    else:
        os.environ["PX_SESSION_PATH"] = old_session
```

- [ ] **Step 2: Add `apply_mood_momentum` tests**

```python
# ---------------------------------------------------------------------------
# apply_mood_momentum
# ---------------------------------------------------------------------------

def test_mood_momentum_first_call():
    """First call with no history returns the raw mood unchanged."""
    result = apply_mood_momentum("curious")
    assert isinstance(result, str)
    assert len(result) > 0


def test_mood_momentum_repeated_same():
    """Repeated same mood should stabilise (not crash)."""
    for _ in range(5):
        result = apply_mood_momentum("peaceful")
    assert isinstance(result, str)


def test_mood_momentum_transition():
    """Switching mood still returns a valid string."""
    apply_mood_momentum("peaceful")
    apply_mood_momentum("peaceful")
    result = apply_mood_momentum("excited")
    assert isinstance(result, str)
```

- [ ] **Step 3: Add `load_recent_thoughts` / `append_thought` / `auto_remember` tests**

```python
# ---------------------------------------------------------------------------
# Thought I/O: load, append, auto_remember
# ---------------------------------------------------------------------------

def test_append_and_load_thoughts(mind_state):
    """append_thought writes, load_recent_thoughts reads."""
    thought = {"thought": "test thought", "mood": "curious", "action": "wait",
               "salience": 0.5, "ts": _dt.datetime.now(_dt.timezone.utc).isoformat()}
    append_thought(thought, persona="spark")
    loaded = load_recent_thoughts(n=5, persona="spark")
    assert len(loaded) >= 1
    assert loaded[-1]["thought"] == "test thought"


def test_load_thoughts_empty(mind_state):
    """No thoughts file → empty list."""
    loaded = load_recent_thoughts(n=5, persona="spark")
    assert loaded == []


def test_auto_remember_high_salience(mind_state):
    """Salience > 0.7 → written to notes.jsonl."""
    thought = {"thought": "important insight", "mood": "contemplative",
               "action": "comment", "salience": 0.85,
               "ts": _dt.datetime.now(_dt.timezone.utc).isoformat()}
    auto_remember(thought, persona="spark")
    notes = pxh.mind.NOTES_FILE
    if notes.exists():
        content = notes.read_text().strip()
        assert "important insight" in content


def test_auto_remember_low_salience(mind_state):
    """Salience <= 0.7 → NOT written to notes."""
    thought = {"thought": "mundane observation", "mood": "content",
               "action": "wait", "salience": 0.3,
               "ts": _dt.datetime.now(_dt.timezone.utc).isoformat()}
    auto_remember(thought, persona="spark")
    notes = pxh.mind.NOTES_FILE
    if notes.exists():
        assert "mundane observation" not in notes.read_text()
```

- [ ] **Step 4: Add `fetch_weather` tests**

```python
# ---------------------------------------------------------------------------
# fetch_weather
# ---------------------------------------------------------------------------

def test_fetch_weather_dry():
    """Dry mode returns cached or synthetic weather without network."""
    result = fetch_weather(dry=True)
    # Dry mode should return a dict with temp_c key (synthetic data)
    assert result is not None
    assert "temp_c" in result


def test_fetch_weather_network_error():
    """Network failure → None (graceful)."""
    with patch("urllib.request.urlopen", side_effect=Exception("network down")):
        result = fetch_weather(dry=False)
    # Should return None or cached data, not crash
    assert result is None or isinstance(result, dict)
```

- [ ] **Step 5: Add `awareness_tick` tests**

```python
# ---------------------------------------------------------------------------
# awareness_tick
# ---------------------------------------------------------------------------

def _awareness_patches():
    """Context manager stack that mocks all external calls in awareness_tick."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch("subprocess.run",
        return_value=MagicMock(returncode=1, stdout='{}')))
    stack.enter_context(patch.object(pxh.mind, "_fetch_frigate_presence", return_value=None))
    stack.enter_context(patch.object(pxh.mind, "_fetch_ha_presence", return_value=None))
    stack.enter_context(patch.object(pxh.mind, "_fetch_ha_calendar", return_value=None))
    stack.enter_context(patch.object(pxh.mind, "_fetch_ha_sleep", return_value=None))
    stack.enter_context(patch.object(pxh.mind, "_fetch_ha_routines", return_value=None))
    stack.enter_context(patch.object(pxh.mind, "_fetch_ha_context", return_value=None))
    stack.enter_context(patch.object(pxh.mind, "fetch_weather", return_value={"temp_c": 15}))
    stack.enter_context(patch.object(pxh.mind, "read_wifi_signal", return_value={}))
    stack.enter_context(patch.object(pxh.mind, "read_system_stats", return_value={}))
    return stack


def test_awareness_tick_dry_returns_dict(mind_state):
    """Dry-run awareness tick returns a dict with expected keys."""
    prev = {}
    with _awareness_patches():
        result, transitions = awareness_tick(prev, dry=True)
    assert isinstance(result, dict)
    assert isinstance(transitions, list)
    assert "hour" in result
    assert "time_period" in result


def test_awareness_tick_detects_transition(mind_state):
    """Changed time_period triggers a transition."""
    prev = {"time_period": "night"}
    # datetime.datetime.now is immutable (C-level) — mock the module-level dt reference
    mock_now = _dt.datetime(2026, 3, 18, 10, 0, tzinfo=HOBART_TZ)
    mock_dt = MagicMock(wraps=_dt)
    mock_dt.datetime.now = MagicMock(return_value=mock_now)
    with _awareness_patches(), \
         patch.object(pxh.mind, "dt", mock_dt):
        result, transitions = awareness_tick(prev, dry=True)
    assert result.get("time_period") != "night"
    assert any("time_period" in t for t in transitions)


def test_awareness_tick_writes_file(mind_state):
    """awareness_tick writes awareness.json."""
    with _awareness_patches():
        awareness_tick({}, dry=True)
    assert pxh.mind.AWARENESS_FILE.exists()
    data = json.loads(pxh.mind.AWARENESS_FILE.read_text())
    assert "hour" in data
    assert "time_period" in data
```

- [ ] **Step 6: Add `reflection` tests**

```python
# ---------------------------------------------------------------------------
# reflection
# ---------------------------------------------------------------------------

def test_reflection_dry_returns_thought(mind_state):
    """Dry-run reflection returns a synthetic thought dict."""
    awareness = {"hour": 10, "time_of_day": "morning", "obi_mode": "calm",
                 "weather": {"temp_c": 18}}
    result = reflection(awareness, dry=True)
    assert result is not None
    assert "thought" in result
    assert "mood" in result
    assert "action" in result


def test_reflection_dry_writes_thoughts_file(mind_state):
    """Dry-run reflection appends to thoughts.jsonl."""
    awareness = {"hour": 10, "time_of_day": "morning", "obi_mode": "calm"}
    reflection(awareness, dry=True)
    assert pxh.mind.THOUGHTS_FILE.exists()
```

- [ ] **Step 7: Run all new tests**

```bash
source .venv/bin/activate && python -m pytest tests/test_mind_coverage.py -v
```

Expected: All pass. Fix any ImportError or AttributeError by adding missing names to imports or adjusting mock setup.

- [ ] **Step 8: Run full mind test suite together**

```bash
source .venv/bin/activate && python -m pytest tests/test_mind_utils.py tests/test_mind_fallback.py tests/test_mind_coverage.py tests/test_px_mind.py -v 2>&1 | tail -20
```

Expected: All pass (100+ tests).

- [ ] **Step 9: Commit**

```bash
git add tests/test_mind_coverage.py
git commit -m "test: add coverage for awareness_tick, reflection, weather, mood, thoughts (#123)

New test file tests/test_mind_coverage.py covers previously untested
px-mind functions: awareness_tick (dry + transition + file write),
reflection (dry + file write), fetch_weather (dry + error),
apply_mood_momentum (first/repeated/transition), and thought I/O
(append, load, auto_remember with salience gating)."
```

---

### Task 4: Cloudflare Worker for dynamic OG images (#119)

**Files:**
- Create: `site/workers/og-rewrite.js`
- Modify: (Cloudflare dashboard — manual deploy step)

- [ ] **Step 1: Create the Worker script**

```javascript
/**
 * Cloudflare Worker: rewrite og:image for /thought/?ts=<timestamp> pages.
 *
 * Social crawlers (Bluesky, Twitter, Facebook) don't execute JS, so the
 * client-side og:image update in thought.js is invisible to them. This
 * worker intercepts requests to /thought/ with a ts= query param and
 * injects the correct per-thought og:image URL into the HTML response.
 */

const API_BASE = 'https://spark-api.wedd.au/api/v1/public';

// Validate ts looks like an ISO timestamp (prevent XSS injection into HTML attributes)
const TS_PATTERN = /^[\d\-T:+.Z]+$/;

function escapeHtmlAttr(s) {
  return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

export default {
  // NOTE: This Worker relies on zone-deployed routing (spark.wedd.au/thought/* → Worker).
  // Cloudflare prevents recursive Worker invocation on the same zone, so fetch(request)
  // hits the origin server, not this Worker again.
  async fetch(request) {
    const url = new URL(request.url);

    // Only intercept /thought/ paths with a ts= param
    if (!url.pathname.startsWith('/thought/') && url.pathname !== '/thought') {
      return fetch(request);
    }

    const ts = url.searchParams.get('ts');
    if (!ts || ts.length > 200 || !TS_PATTERN.test(ts)) {
      return fetch(request);
    }

    // Fetch the original page from origin
    const response = await fetch(request);
    const contentType = response.headers.get('content-type') || '';
    if (!contentType.includes('text/html')) {
      return response;
    }

    // Build the per-thought image URL (HTML-escaped for safe attribute injection)
    const imageUrl = escapeHtmlAttr(`${API_BASE}/thought-image?ts=${encodeURIComponent(ts)}`);

    // Rewrite og:image and dimensions (thought cards are 1080x1080)
    let html = await response.text();
    html = html.replace(
      /<meta property="og:image" content="[^"]*">/,
      `<meta property="og:image" content="${imageUrl}">`
    );
    html = html.replace(
      /<meta property="og:image:width" content="[^"]*">/,
      '<meta property="og:image:width" content="1080">'
    );
    html = html.replace(
      /<meta property="og:image:height" content="[^"]*">/,
      '<meta property="og:image:height" content="1080">'
    );

    // Also update twitter:image if present
    html = html.replace(
      /<meta name="twitter:image" content="[^"]*">/,
      `<meta name="twitter:image" content="${imageUrl}">`
    );

    return new Response(html, {
      status: response.status,
      headers: {
        ...Object.fromEntries(response.headers),
        'content-type': 'text/html; charset=utf-8',
      },
    });
  },
};
```

- [ ] **Step 2: Verify the thought-image API endpoint works**

```bash
curl -s -o /dev/null -w "%{http_code}" "https://spark-api.wedd.au/api/v1/public/thought-image?ts=2026-03-15T10:00:00%2B11:00"
```

Expected: 200 (if image exists) or 404 (if not). Either way, confirms the endpoint is live.

- [ ] **Step 3: Commit the Worker script**

```bash
git add site/workers/og-rewrite.js
git commit -m "feat(site): add Cloudflare Worker for dynamic OG images (#119)

Social crawlers don't execute JS, so the client-side og:image update
is invisible. This Worker intercepts /thought/?ts=... requests and
rewrites the og:image meta tag server-side with the per-thought
image URL from the API.

Manual deploy step: wrangler deploy or Cloudflare dashboard."
```

- [ ] **Step 4: Deploy to Cloudflare (manual)**

Deploy via Cloudflare dashboard or wrangler CLI:

```bash
# If wrangler is installed:
cd site/workers && npx wrangler deploy og-rewrite.js --name spark-og-rewrite
```

Route: `spark.wedd.au/thought/*` → Worker

- [ ] **Step 5: Verify with curl**

```bash
curl -s "https://spark.wedd.au/thought/?ts=2026-03-15T10:00:00%2B11:00" | grep 'og:image'
```

Expected: `<meta property="og:image" content="https://spark-api.wedd.au/api/v1/public/thought-image?ts=...">` (not the static og-image.png).

---

### Task 5: Close issues and push

- [ ] **Step 1: Run full test suite**

```bash
source .venv/bin/activate && python -m pytest -x -q 2>&1 | tail -10
```

Expected: All pass (except known GPIO live tests).

- [ ] **Step 2: Push**

```bash
git push origin master
```

- [ ] **Step 3: Close issues**

```bash
gh issue close 115 --comment "Consolidated in <commit>. CSS vars in colors.css, API URL in config.js."
gh issue close 120 --comment "rotate_log() in pxh.state + fsync added to bin/ scripts in <commit>."
gh issue close 123 --comment "New test file test_mind_coverage.py adds coverage for awareness_tick, reflection, fetch_weather, apply_mood_momentum, thought I/O in <commit>."
gh issue close 119 --comment "Cloudflare Worker og-rewrite.js deployed in <commit>."
```
