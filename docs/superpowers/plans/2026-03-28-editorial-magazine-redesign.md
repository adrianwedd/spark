# Editorial Magazine Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform spark.wedd.au from a developer dashboard with swapped colours into a glossy editorial magazine about a thinking robot.

**Architecture:** Static site on Cloudflare Pages (`site/` directory). REST API on Raspberry Pi at `spark-api.wedd.au`. No build step — raw HTML/CSS/JS. Inter variable font + Courier Prime for typography. CSS custom properties for theming. IntersectionObserver for scroll animations.

**Tech Stack:** HTML5, CSS3 (custom properties, grid, clamp), vanilla JS (ES6, no framework), Google Fonts (Inter variable, Courier Prime), Cloudflare Pages.

**Spec:** `docs/superpowers/specs/2026-03-28-editorial-magazine-redesign.md` (rev 2)

**Test command:** `python -m pytest tests/ -x -q` (623+ tests, all must pass after backend changes)

---

## File Map

### CSS (modify)
- `site/css/base.css` — design system tokens, Inter font, containers, spacing, z-index
- `site/css/dark.css` — dark section typography (Inter headings), carousel overrides
- `site/css/warm.css` — Playfair→Inter swap, warm transition gradient, remove racing
- `site/css/feed.css` — feed cards (top border), blog tiers, thought centrepiece, article layout
- `site/css/chat.css` — warm→dark full migration

### HTML (modify)
- `site/index.html` — hero, nav, remove racing, footer, font imports
- `site/feed/index.html` — font import, mood summary container
- `site/blog/index.html` — font import
- `site/thought/index.html` — centrepiece layout, prev/next, share

### JS (modify)
- `site/js/nav.js` — focus trap, backdrop, Escape key, scroll lock
- `site/js/charts.js` — line sparklines with terminal dot, threshold bars
- `site/js/feed.js` — salience tiers, mood summary, count display
- `site/js/blog.js` — three-tier cards, article-style single post
- `site/js/thought.js` — prev/next, share row, mood wash, static-first fallback
- `site/js/dashboard.js` — mood sentence, mood duration, grouped metrics, remove race
- `site/js/live.js` — remove race fetch, carousel timing/pause

### JS (create)
- `site/js/scroll-animate.js` — IntersectionObserver entrance animations

### Backend (modify)
- `bin/px-post` — add salience to feed.json posts

### Assets (create)
- `site/img/` — directory for hero photo (provided by Adrian)

---

## Task 1: Backend — Add salience to feed posts

**Files:**
- Modify: `bin/px-post:463-468`
- Test: `python -m pytest tests/test_post.py -x -q`

- [ ] **Step 1: Add salience field to feed post dict**

In `bin/px-post`, find the feed post construction (around line 463):

```python
    post = {
        "ts": thought.get("ts", ""),
        "thought": thought["thought"],
        "mood": thought.get("mood", ""),
        "posted_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
```

Change to:

```python
    post = {
        "ts": thought.get("ts", ""),
        "thought": thought["thought"],
        "mood": thought.get("mood", ""),
        "salience": thought.get("salience", 0),
        "posted_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_post.py -x -q`
Expected: All pass. This is a data-only change; existing tests don't assert the absence of salience.

- [ ] **Step 3: Commit**

```bash
git add bin/px-post
git commit -m "data: include salience in feed.json posts

Frontend needs salience for two-tier card rendering.
One-line addition to the feed post dict."
```

---

## Task 2: Design system foundation — base.css tokens

**Files:**
- Modify: `site/css/base.css`

- [ ] **Step 1: Update the Google Fonts import in all HTML files**

This task only changes base.css. The HTML font imports will be updated per-page in later tasks. For now, add the Inter variable font CSS custom properties and tokens to base.css.

In `site/css/base.css`, replace the `:root` block (lines 4-47) with the updated design tokens. Keep all existing properties and add the new ones:

```css
:root {
  /* ── Warm theme (legacy — Brain, FAQ sections only) ─────────── */
  --warm-bg: #fdf6ec;
  --warm-accent: #e8875a;
  --warm-text: #2d2d2d;
  --warm-muted: #8a7060;

  /* ── Dark theme — primary palette ───────────────────────────── */
  --dark-bg:            #1a181c;
  --dark-surface:       #24212a;
  --dark-surface-raised:#2d2a36;
  --dark-border:        #3d3844;
  --dark-border-subtle: rgba(255, 255, 255, 0.04);
  --dark-bg-glow-overlay: rgba(26, 24, 28, 0.04);
  --dark-text:          #e2ddd8;
  --dark-muted:         #968e96;
  --dark-code-bg:       #24212a;

  /* ── Copper accent ──────────────────────────────────────────── */
  --dark-accent:        #c48b6e;
  --dark-accent-hover:  #d49e82;
  --dark-accent-muted:  #9a6d52;
  --dark-glow:          rgba(196, 139, 110, 0.08);

  /* ── Supporting accents ─────────────────────────────────────── */
  --slate:              #6e8ec4;
  --slate-muted:        #4d6a9a;
  --sage:               #8eaa7e;
  --sage-muted:         #6a8060;

  /* ── Dynamic mood accent ────────────────────────────────────── */
  --spark-accent:       var(--dark-accent);
  --spark-glow:         var(--dark-glow);
  --chat-bubble-color:  var(--dark-accent);

  /* ── Type scale (custom editorial) ──────────────────────────── */
  --text-display:   clamp(2.5rem, 6vw, 3.5rem);
  --text-title:     1.75rem;
  --text-subtitle:  1.25rem;
  --text-body:      1rem;
  --text-small:     0.875rem;
  --text-xs:        0.75rem;

  /* ── Layout ─────────────────────────────────────────────────── */
  --nav-height:   56px;
  --radius-sm:     8px;
  --radius:       12px;
  --radius-lg:    20px;
  --transition:   0.2s ease;
}
```

- [ ] **Step 2: Update body font stack to Inter**

Replace the `body` rule (around line 50-56):

```css
body {
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  font-weight: 300;
  line-height: 1.7;
  font-variant-numeric: tabular-nums;
  overflow-x: hidden;
  background: var(--dark-bg);
  color: var(--dark-text);
}
```

- [ ] **Step 3: Remove min-height: 100vh from sections**

Replace the section rules (around lines 168-176):

```css
/* ── Sections ── */
section {
  padding: 4rem 2rem;
  overflow-x: hidden;
}
section:first-of-type { padding-top: calc(var(--nav-height) + 5rem); }
@media (max-width: 600px) {
  section { padding: 2.5rem 1rem; }
}
@media (max-width: 480px) {
  section { padding: 2.5rem 0.75rem; }
}
```

- [ ] **Step 4: Add container classes**

Replace the single `.container` rule (around line 186):

```css
/* ── Containers ── */
.container-hero { max-width: 1100px; margin: 0 auto; padding: 0 1.25rem; }
.container      { max-width: 900px;  margin: 0 auto; padding: 0 1.25rem; overflow-x: hidden; }
.container-narrow { max-width: 680px; margin: 0 auto; padding: 0 1.25rem; }
```

- [ ] **Step 5: Update nav link styling to Inter**

In the nav `.links a` rule (around line 101-106):

```css
#main-nav .links a {
  color: var(--dark-muted);
  text-decoration: none;
  font-size: var(--text-small);
  font-weight: 500;
  letter-spacing: 0.01em;
  transition: color var(--transition);
}
```

- [ ] **Step 6: Commit**

```bash
git add site/css/base.css
git commit -m "site: design system foundation — type scale, Inter font, containers

Add --text-* scale tokens, Inter font stack on body, three container
classes (.container-hero/container/container-narrow), remove
min-height: 100vh from sections."
```

---

## Task 3: Dark theme typography — dark.css

**Files:**
- Modify: `site/css/dark.css`

- [ ] **Step 1: Update hero h1 to Inter**

Replace the `[data-theme="dark"]#home h1` rule (around line 8-16):

```css
[data-theme="dark"]#home h1 {
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  font-size: var(--text-display);
  font-weight: 800;
  line-height: 1.05;
  letter-spacing: -0.04em;
  color: var(--dark-accent);
  text-shadow: 0 0 60px var(--dark-glow);
}
```

- [ ] **Step 2: Update h2 to Inter**

Replace the `[data-theme="dark"] h2` rule (around line 60-66):

```css
[data-theme="dark"] h2 {
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  font-size: var(--text-title);
  font-weight: 700;
  color: var(--dark-accent);
  margin-bottom: 1.5rem;
  letter-spacing: -0.025em;
  text-shadow: 0 0 40px var(--dark-glow);
}
```

- [ ] **Step 3: Keep h3 as Courier Prime labels**

Verify h3 rule (around line 68-75) already uses Courier Prime. It should read:

```css
[data-theme="dark"] h3 {
  font-family: 'Courier Prime', 'Courier New', monospace;
  font-size: var(--text-body);
  font-weight: 400;
  color: var(--dark-muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin: 2rem 0 0.5rem;
}
```

- [ ] **Step 4: Commit**

```bash
git add site/css/dark.css
git commit -m "site: dark typography — Inter for h1/h2, Courier Prime for h3 labels"
```

---

## Task 4: Warm theme typography — warm.css

**Files:**
- Modify: `site/css/warm.css`

- [ ] **Step 1: Replace Playfair Display with Inter in warm headings**

Replace the warm heading rules (lines 7-14):

```css
[data-theme="warm"] h1,
[data-theme="warm"] h2 {
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  color: var(--warm-text);
}

[data-theme="warm"] h1 { font-size: clamp(2.5rem, 6vw, 4rem); font-weight: 800; line-height: 1.1; }
[data-theme="warm"] h2 { font-size: var(--text-title); font-weight: 700; margin-bottom: 1.5rem; }
```

- [ ] **Step 2: Add warm→dark transition gradient**

After the `[data-theme="warm"]` base rule (line 1-5), add:

```css
/* Smooth dark→warm transition at section boundary */
[data-theme="warm"]::before {
  content: '';
  display: block;
  height: 80px;
  margin: -4rem -2rem 0;
  background: linear-gradient(to bottom, var(--dark-bg), var(--warm-bg) 80px);
  pointer-events: none;
}
/* First warm section after a dark section gets the gradient */
[data-theme="dark"] + .section-divider + [data-theme="warm"]::before,
[data-theme="dark"] + [data-theme="warm"]::before {
  display: block;
}
/* Warm sections adjacent to other warm sections don't need it */
[data-theme="warm"] + [data-theme="warm"]::before {
  display: none;
}
```

- [ ] **Step 3: Commit**

```bash
git add site/css/warm.css
git commit -m "site: warm typography — Inter replaces Playfair, add dark→warm gradient"
```

---

## Task 5: Navigation — HTML + CSS + JS

**Files:**
- Modify: `site/index.html:59-77` (nav), `site/css/base.css` (nav backdrop), `site/js/nav.js`

- [ ] **Step 1: Reduce nav to 5 items in index.html**

Replace the nav `<ul>` contents (index.html around lines 64-76):

```html
  <ul class="links" id="nav-links">
    <li><a href="/">Home</a></li>
    <li><a href="/#live">Live</a></li>
    <li><a href="/feed/">Feed</a></li>
    <li><a href="/blog/">Blog</a></li>
    <li><a href="https://github.com/adrianwedd/spark" target="_blank" rel="noopener noreferrer" class="nav-github">GitHub ↗</a></li>
  </ul>
```

- [ ] **Step 2: Update nav in feed, blog, and thought HTML files**

Apply the same 5-item nav to `site/feed/index.html`, `site/blog/index.html`, and `site/thought/index.html`. Each has the same `<ul class="links">` block — replace with the 5-item version. Set the appropriate `class="active"` on the current page's link.

- [ ] **Step 3: Add nav backdrop CSS to base.css**

After the mobile nav rules in base.css (around line 165), add:

```css
/* Mobile nav backdrop */
.nav-backdrop {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  z-index: 90;
}
#main-nav.nav-open ~ .nav-backdrop { display: block; }
```

- [ ] **Step 4: Add backdrop element to all HTML files**

In each HTML file, immediately after the closing `</nav>` tag, add:

```html
<div class="nav-backdrop" id="nav-backdrop"></div>
```

- [ ] **Step 5: Rewrite nav.js with focus trap**

Replace `site/js/nav.js` entirely:

```javascript
// nav.js — scroll spy + mobile hamburger with focus trap
(function () {
  // ── Scroll spy ──────────────────────────────────────────────────
  var sections = document.querySelectorAll('section[id]');
  var links = document.querySelectorAll('nav .links a');

  function onScroll() {
    if (window.location.pathname !== '/' && window.location.pathname !== '/index.html') return;
    var current = '';
    sections.forEach(function (sec) {
      if (sec.getBoundingClientRect().top <= 80) current = sec.id;
    });
    links.forEach(function (a) {
      a.classList.toggle('active', a.getAttribute('href') === '#' + current);
    });
  }
  document.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  // ── Mobile hamburger with focus trap ────────────────────────────
  var nav = document.getElementById('main-nav');
  var burger = document.getElementById('nav-burger');
  var backdrop = document.getElementById('nav-backdrop');
  var _prevFocus = null;

  function openMenu() {
    _prevFocus = document.activeElement;
    nav.classList.add('nav-open');
    burger.setAttribute('aria-expanded', 'true');
    document.body.style.overflow = 'hidden';
    // Focus first link
    var firstLink = nav.querySelector('#nav-links a');
    if (firstLink) firstLink.focus();
  }

  function closeMenu() {
    nav.classList.remove('nav-open');
    burger.setAttribute('aria-expanded', 'false');
    document.body.style.overflow = '';
    if (_prevFocus) _prevFocus.focus();
  }

  if (nav && burger) {
    burger.addEventListener('click', function () {
      nav.classList.contains('nav-open') ? closeMenu() : openMenu();
    });

    if (backdrop) {
      backdrop.addEventListener('click', closeMenu);
    }

    // Escape key closes
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && nav.classList.contains('nav-open')) closeMenu();
    });

    // Focus trap: Tab cycles within nav links + burger
    nav.addEventListener('keydown', function (e) {
      if (e.key !== 'Tab' || !nav.classList.contains('nav-open')) return;
      var focusable = nav.querySelectorAll('#nav-links a, .nav-burger');
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    });

    // Close on link click
    document.querySelectorAll('#nav-links a').forEach(function (a) {
      a.addEventListener('click', closeMenu);
    });
  }
})();
```

- [ ] **Step 6: Commit**

```bash
git add site/index.html site/feed/index.html site/blog/index.html site/thought/index.html site/css/base.css site/js/nav.js
git commit -m "site: 5-item nav with mobile focus trap, backdrop, Escape key"
```

---

## Task 6: Font imports — all HTML files

**Files:**
- Modify: `site/index.html`, `site/feed/index.html`, `site/blog/index.html`, `site/thought/index.html`

- [ ] **Step 1: Update index.html font import**

Replace the Playfair+Courier Prime import (index.html around line 35):

```html
  <link href="https://fonts.googleapis.com/css2?family=Courier+Prime:ital@0;1&family=Inter:wght@300..800&display=swap" rel="stylesheet">
```

- [ ] **Step 2: Update feed, blog, thought HTML font imports**

In each of `site/feed/index.html`, `site/blog/index.html`, `site/thought/index.html`, replace the existing Google Fonts `<link>` with:

```html
  <link href="https://fonts.googleapis.com/css2?family=Courier+Prime:ital@0;1&family=Inter:wght@300..800&display=swap" rel="stylesheet">
```

- [ ] **Step 3: Commit**

```bash
git add site/index.html site/feed/index.html site/blog/index.html site/thought/index.html
git commit -m "site: Inter variable font import replaces Playfair on all pages"
```

---

## Task 7: Hero section — HTML + CSS + JS

**Files:**
- Modify: `site/index.html:79-96`, `site/css/dark.css`, `site/js/dashboard.js`
- Create: `site/img/` directory

- [ ] **Step 1: Create img directory**

```bash
mkdir -p site/img
```

- [ ] **Step 2: Restructure hero HTML**

Replace `#home` section in index.html (around lines 79-96):

```html
<!-- ══════════════════════════════════════════════════════ HOME -->
<section id="home" data-theme="dark">
  <div class="container-hero">
    <div class="hero-grid">
      <div class="hero-text">
        <h1>SPARK</h1>
        <p id="hero-mood-sentence" class="hero-mood-sentence">A robot with an inner life.</p>
        <p class="hero-credit">Built by Adrian and Obi together.</p>
        <div class="hero-cta">
          <a href="/feed/" class="btn-ghost">Explore the feed →</a>
          <a href="#live" class="btn-ghost">Live dashboard ↓</a>
        </div>
      </div>
      <div class="hero-photo">
        <picture>
          <source srcset="img/spark-hero.webp" type="image/webp">
          <img src="img/spark-hero.jpg"
               alt="SPARK — a PiCar-X robot with ultrasonic sensors and a camera, sitting on a desk"
               loading="eager" class="hero-img">
        </picture>
        <div class="hero-status">
          <span id="status-dot-hero" class="status-dot-hero"></span>
          <span id="hero-online-label" class="hero-online-label">Checking...</span>
        </div>
      </div>
    </div>
  </div>

  <!-- Thought carousel (below hero grid, standard container) -->
  <div class="container">
    <div id="thought-carousel" class="thought-carousel" aria-live="polite" aria-label="SPARK's recent thoughts">
      <button id="carousel-pause" class="carousel-pause visually-hidden" aria-label="Pause thought carousel">Pause</button>
      <div class="carousel-slide active">
        <blockquote class="carousel-quote">Waiting for SPARK's thoughts…</blockquote>
      </div>
    </div>
  </div>
</section>
```

- [ ] **Step 3: Add hero CSS to dark.css**

Add after the existing `[data-theme="dark"]#home h1` rule:

```css
/* ── Hero grid ──────────────────────────────────────────────── */
.hero-grid {
  display: grid;
  grid-template-columns: 1fr 0.7fr;
  gap: 3rem;
  align-items: center;
}

.hero-mood-sentence {
  font-family: 'Inter', system-ui, sans-serif;
  font-size: var(--text-subtitle);
  font-weight: 600;
  color: var(--dark-text);
  margin: 1rem 0 0.5rem;
  line-height: 1.4;
}

.hero-credit {
  font-size: var(--text-small);
  font-weight: 500;
  color: var(--dark-muted);
  margin: 0 0 1.5rem;
}

.hero-cta {
  display: flex;
  gap: 1rem;
  flex-wrap: wrap;
}

.btn-ghost {
  display: inline-block;
  padding: 0.6rem 1.5rem;
  border: 1px solid var(--dark-accent);
  border-radius: var(--radius-sm);
  color: var(--dark-accent);
  font-family: 'Inter', system-ui, sans-serif;
  font-size: var(--text-small);
  font-weight: 500;
  text-decoration: none;
  transition: background var(--transition), color var(--transition);
}
.btn-ghost:hover {
  background: var(--dark-accent);
  color: var(--dark-bg);
}
.btn-ghost:focus-visible {
  outline: 2px solid var(--dark-accent);
  outline-offset: 2px;
}

.hero-img {
  width: 100%;
  height: auto;
  border-radius: var(--radius-lg);
  box-shadow: 0 0 60px var(--spark-glow);
  filter: contrast(1.05) brightness(1.02);
}

.hero-status {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  margin-top: 0.75rem;
  justify-content: center;
}

.status-dot-hero {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #ef4444;
  display: inline-block;
}
.status-dot-hero.green { background: var(--dark-accent); }

.hero-online-label {
  font-size: var(--text-xs);
  color: var(--dark-muted);
}

@media (max-width: 700px) {
  .hero-grid {
    grid-template-columns: 1fr;
    gap: 2rem;
    text-align: center;
  }
  .hero-photo { order: 2; }
  .hero-img { max-width: 280px; margin: 0 auto; display: block; }
  .hero-cta { justify-content: center; flex-direction: column; align-items: center; }
  .hero-status { justify-content: center; }
}
```

- [ ] **Step 4: Add mood sentence JS to dashboard.js**

At the top of the `renderPresence` function in dashboard.js (around line 24), add the hero mood sentence update:

```javascript
    // Hero mood sentence
    var heroSentence = document.getElementById('hero-mood-sentence');
    if (heroSentence && state.mood) {
      var sentence = 'Feeling ' + state.mood + '.';
      var thought = state.last_thought || '';
      if (thought.length <= 80 && thought.length > 0) {
        sentence += ' ' + thought;
      } else if (thought.length > 80) {
        // Take first sentence
        var firstSentence = thought.split(/[.!?]/)[0];
        if (firstSentence && firstSentence.length <= 80) {
          sentence += ' ' + firstSentence + '.';
        }
      }
      heroSentence.textContent = sentence;
    }

    // Hero status dot
    var heroDot = document.getElementById('status-dot-hero');
    var heroLabel = document.getElementById('hero-online-label');
    if (heroDot) heroDot.classList.add('green');
    if (heroLabel) heroLabel.textContent = 'Online';
```

- [ ] **Step 5: Commit**

```bash
git add site/index.html site/css/dark.css site/js/dashboard.js site/img/
git commit -m "site: hero section — two-column layout, mood sentence, photo slot, CTAs"
```

---

## Task 8: Remove racing section + race widget

**Files:**
- Modify: `site/index.html`, `site/js/dashboard.js`, `site/js/live.js`

- [ ] **Step 1: Remove #racing section from index.html**

Delete the entire `#racing` section (around lines 560-574) and its preceding section-divider (if any).

- [ ] **Step 2: Remove race rendering from dashboard.js**

Find the `renderRace` function in dashboard.js and remove it. Also remove `renderRace` from the returned module object at the bottom of the file.

- [ ] **Step 3: Remove race fetch from live.js**

Find the race endpoint fetch in live.js (look for `/public/race` or `renderRace`) and remove the fetch call and its handler.

- [ ] **Step 4: Commit**

```bash
git add site/index.html site/js/dashboard.js site/js/live.js
git commit -m "site: remove racing section and race widget from public site"
```

---

## Task 9: Carousel — timing, pause, dark styling

**Files:**
- Modify: `site/js/live.js`, `site/css/dark.css`

- [ ] **Step 1: Update carousel timing from 10s to 8s**

In live.js, find the carousel interval (search for `10000` or `10_000`) and change to `8000`.

- [ ] **Step 2: Add pause-on-hover and pause-on-focus**

In live.js, find the carousel auto-advance logic and wrap it with pause detection:

```javascript
    // Carousel pause on hover/focus
    var carouselEl = document.getElementById('thought-carousel');
    var carouselPaused = false;
    var pauseBtn = document.getElementById('carousel-pause');

    if (carouselEl) {
      carouselEl.addEventListener('mouseenter', function () { carouselPaused = true; });
      carouselEl.addEventListener('mouseleave', function () { carouselPaused = false; });
      carouselEl.addEventListener('focusin', function () { carouselPaused = true; });
      carouselEl.addEventListener('focusout', function () { carouselPaused = false; });
    }
    if (pauseBtn) {
      pauseBtn.addEventListener('click', function () {
        carouselPaused = !carouselPaused;
        pauseBtn.textContent = carouselPaused ? 'Resume' : 'Pause';
        pauseBtn.setAttribute('aria-label', carouselPaused ? 'Resume thought carousel' : 'Pause thought carousel');
      });
    }
```

Then in the interval callback, add at the top: `if (carouselPaused) return;`

- [ ] **Step 3: Update dark carousel CSS**

The carousel dark overrides in dark.css should already have: dark surface background, no bubble tails, `--spark-accent` left border. Verify these are in place from the earlier Phase 1 work. If the `padding-bottom` reduction for no-tails is present, keep it. Remove any duplicate carousel-dot rules if they still exist.

- [ ] **Step 4: Add visually-hidden utility class to base.css**

In base.css, add:

```css
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
```

- [ ] **Step 5: Commit**

```bash
git add site/js/live.js site/css/dark.css site/css/base.css
git commit -m "site: carousel — 8s timing, pause on hover/focus, WCAG 2.2.2 pause button"
```

---

## Task 10: Feed page — cards, mood summary, salience tiers

**Files:**
- Modify: `site/css/feed.css`, `site/js/feed.js`, `site/feed/index.html`

- [ ] **Step 1: Update feed.css card styles**

Replace the `.feed-card, .thought-bubble` rule block with top-border cards. The key change: `border-left` → `border-top`, and add a `.feed-card--featured` modifier:

```css
.feed-card,
.thought-bubble {
  position: relative;
  background: var(--dark-surface);
  border: 1px solid var(--dark-border);
  border-top: 2px solid var(--spark-accent, var(--dark-accent));
  border-radius: var(--radius);
  padding: 1.75rem 2rem;
  text-decoration: none;
  color: inherit;
  display: block;
  transition: transform 0.25s cubic-bezier(0.25, 0.1, 0.25, 1), box-shadow 0.25s cubic-bezier(0.25, 0.1, 0.25, 1);
  margin-bottom: 0;
}

.feed-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 20px var(--dark-glow);
}

/* Featured card — high salience */
.feed-card--featured {
  border-top-width: 3px;
  padding: 2rem 2.25rem;
}
.feed-card--featured .feed-card-quote {
  font-size: var(--text-subtitle);
}
```

- [ ] **Step 2: Update feed-card-quote and metadata typography**

```css
.feed-card-quote {
  font-family: 'Courier Prime', monospace;
  font-style: italic;
  font-size: var(--text-body);
  line-height: 1.65;
  margin: 0 0 0.75rem;
  color: var(--dark-text);
}

.feed-card-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  font-family: 'Inter', system-ui, sans-serif;
  font-size: var(--text-small);
  font-weight: 500;
  color: var(--dark-muted);
}
```

- [ ] **Step 3: Update date header with copper rule**

```css
.feed-date-label {
  display: flex;
  align-items: center;
  gap: 1rem;
  font-family: 'Courier Prime', monospace;
  font-size: var(--text-xs);
  color: var(--dark-muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  padding: 1.5rem 0 0.5rem;
  margin-top: 0.5rem;
}
.feed-date-label::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--dark-accent-muted);
}
.feed-date-label:first-child {
  padding-top: 0;
  margin-top: 0;
}
```

- [ ] **Step 4: Update feed header in feed.css**

```css
.feed-header h1 {
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  font-size: var(--text-title);
  font-weight: 700;
  margin: 0 0 0.5rem;
  color: var(--dark-accent);
  letter-spacing: -0.025em;
}
```

- [ ] **Step 5: Add mood summary container to feed/index.html**

In `site/feed/index.html`, after the `<p class="feed-subtitle">` (around line 62), add:

```html
      <p id="feed-mood-summary" class="feed-subtitle"></p>
```

- [ ] **Step 6: Update feed.js — salience tiers and mood summary**

In `site/js/feed.js`, update the `renderCard` function to accept salience and add the featured class:

Find the card creation (search for `a.className = 'feed-card'`) and change to:

```javascript
    var salience = post.salience || 0;
    a.className = salience >= 0.85 ? 'feed-card feed-card--featured' : 'feed-card';
```

For the mood-colored top border, add after creating the card element:

```javascript
    // Mood-colored top border
    var moodColor = _moodCSSColor(post.mood);
    if (moodColor) a.style.borderTopColor = moodColor;
```

Add a helper function at the top of the IIFE:

```javascript
  function _moodCSSColor(mood) {
    if (!mood) return '';
    return getComputedStyle(document.documentElement)
      .getPropertyValue('--mood-' + mood.toLowerCase()).trim() || '';
  }
```

For the mood summary, add to the `render` function after clearing the list:

```javascript
    // Mood summary
    var summaryEl = document.getElementById('feed-mood-summary');
    if (summaryEl && _allPosts.length > 0) {
      var counts = {};
      _allPosts.forEach(function (p) {
        var m = (p.mood || 'unknown').toLowerCase();
        counts[m] = (counts[m] || 0) + 1;
      });
      var sorted = Object.entries(counts).sort(function (a, b) { return b[1] - a[1]; });
      var top3 = sorted.slice(0, 3).map(function (e) { return e[1] + ' ' + e[0]; }).join(', ');
      summaryEl.textContent = _allPosts.length + ' thoughts \u2014 ' + top3 + (sorted.length > 3 ? ', \u2026' : '');
    }
```

- [ ] **Step 7: Add count display below load-more button**

In the `updateLoadMore` function in feed.js, after creating the button, add:

```javascript
    // Count display
    var countEl = document.getElementById('feed-count');
    if (!countEl) {
      countEl = document.createElement('p');
      countEl.id = 'feed-count';
      countEl.style.cssText = 'text-align:center;font-size:var(--text-xs);color:var(--dark-muted);margin-top:0.5rem;';
    }
    countEl.textContent = 'Showing ' + _displayedCount + ' of ' + _allPosts.length + ' thoughts';
    btn.after(countEl);
```

- [ ] **Step 8: Commit**

```bash
git add site/css/feed.css site/js/feed.js site/feed/index.html
git commit -m "site: feed redesign — top-border cards, salience tiers, mood summary, count"
```

---

## Task 11: Blog page — three-tier cards, article layout

**Files:**
- Modify: `site/css/feed.css`, `site/js/blog.js`

- [ ] **Step 1: Add blog tier CSS to feed.css**

Add after the existing blog card styles:

```css
/* Blog tier: essay (essay, monthly, yearly) */
.feed-card--essay {
  border-top: 3px solid var(--dark-accent);
  padding: 2.25rem;
}
.feed-card--essay .blog-card-title {
  font-family: 'Inter', system-ui, sans-serif;
  font-size: var(--text-subtitle);
  font-weight: 600;
  color: var(--dark-accent);
}
.feed-card--essay .feed-card-quote {
  -webkit-line-clamp: 2;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* Blog tier: weekly */
.feed-card--weekly .blog-card-title {
  font-family: 'Inter', system-ui, sans-serif;
  font-size: var(--text-body);
  font-weight: 600;
  color: var(--dark-text);
}
.feed-card--weekly .feed-card-quote {
  font-size: var(--text-small);
  -webkit-line-clamp: 1;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* Blog tier: daily */
.feed-card--daily {
  border-top: 1px solid var(--dark-border);
  padding: 1rem 1.5rem;
}
.feed-card--daily .blog-card-title {
  font-family: 'Inter', system-ui, sans-serif;
  font-size: var(--text-body);
  font-weight: 600;
  color: var(--dark-text);
  margin-bottom: 0;
}

/* Blog article (single post — no card) */
.blog-article {
  max-width: 680px;
  margin: 0 auto;
}
.blog-article .blog-post-title {
  font-family: 'Inter', system-ui, sans-serif;
  font-size: clamp(1.5rem, 4vw, 2.25rem);
  font-weight: 700;
  color: var(--dark-accent);
  letter-spacing: -0.025em;
  line-height: 1.2;
  margin-bottom: 1rem;
}
.blog-article .blog-post-body {
  font-family: 'Courier Prime', monospace;
  font-size: var(--text-body);
  line-height: 1.8;
  color: var(--dark-text);
}
.blog-article .blog-post-body p { margin-bottom: 1.25rem; }
.blog-article .feed-card-meta { margin-bottom: 1.5rem; }
.blog-article .blog-divider {
  border: none;
  border-top: 1px solid var(--dark-border);
  margin: 1.5rem 0;
}
```

- [ ] **Step 2: Update blog.js renderCard for three tiers**

In blog.js `renderCard` function (around line 127), update the card class assignment:

```javascript
  function renderCard(post) {
    var a = document.createElement('a');
    var type = post.type || 'daily';
    var tier = (type === 'essay' || type === 'monthly' || type === 'yearly') ? 'essay'
             : (type === 'weekly') ? 'weekly' : 'daily';
    a.className = 'feed-card feed-card--' + tier;
    a.href = postURL(post);

    // Mood-coloured top border
    var mood = (post.mood || post.mood_summary || '').split(',')[0].replace(/\([^)]*\)/g, '').trim().toLowerCase();
    var moodColor = mood ? getComputedStyle(document.documentElement)
      .getPropertyValue('--mood-' + mood).trim() : '';
    if (moodColor && tier !== 'daily') a.style.borderTopColor = moodColor;
```

For daily cards, omit the excerpt and mood badge:

```javascript
    if (tier === 'daily') {
      // Title only, no excerpt, no mood badge
      if (post.title) {
        var title = document.createElement('p');
        title.className = 'blog-card-title';
        title.textContent = post.title;
        a.appendChild(title);
      }
      var meta = document.createElement('div');
      meta.className = 'feed-card-meta';
      if (post.type) {
        var typeBadge = document.createElement('span');
        typeBadge.className = 'blog-type-badge blog-type-' + type;
        typeBadge.textContent = typeLabel(post.type);
        meta.appendChild(typeBadge);
      }
      var time = document.createElement('time');
      time.dateTime = post.ts || '';
      time.textContent = formatTime(post.ts || '');
      meta.appendChild(time);
      a.appendChild(meta);
      return a;
    }
```

- [ ] **Step 3: Update blog.js renderSinglePost for article layout**

In `renderSinglePost` (around line 65), change the article class:

```javascript
    article.className = 'blog-article';
```

Add a divider between meta and body:

```javascript
    // Divider
    var hr = document.createElement('hr');
    hr.className = 'blog-divider';
    article.appendChild(hr);
```

- [ ] **Step 4: Commit**

```bash
git add site/css/feed.css site/js/blog.js
git commit -m "site: blog — three-tier cards (essay/weekly/daily), article-style single post"
```

---

## Task 12: Thought permalink — centrepiece, prev/next, share, mood wash

**Files:**
- Modify: `site/thought/index.html`, `site/js/thought.js`, `site/css/feed.css`

- [ ] **Step 1: Update thought/index.html for centrepiece layout**

Replace the `<main>` content:

```html
<main class="thought-page">
  <div class="container-narrow">

    <nav class="breadcrumb" aria-label="Breadcrumb">
      <a href="/">SPARK</a> <span class="breadcrumb-sep" aria-hidden="true">/</span>
      <a href="/feed/">Feed</a> <span class="breadcrumb-sep" aria-hidden="true">/</span>
      <span>This thought</span>
    </nav>

    <article id="thought-card" class="thought-centrepiece" hidden>
      <blockquote id="thought-text" class="thought-centrepiece-quote"></blockquote>
      <hr class="thought-mood-rule">
      <div class="thought-centrepiece-meta">
        <span id="thought-mood" class="mood-badge"></span>
        <time id="thought-time"></time>
      </div>
    </article>

    <nav id="thought-nav" class="thought-prev-next" hidden>
      <a id="thought-prev" href="#" class="thought-nav-link">← Earlier thought</a>
      <a id="thought-next" href="#" class="thought-nav-link">Later thought →</a>
    </nav>

    <div id="thought-share" class="thought-share" hidden>
      <a id="thought-share-bsky" href="#" target="_blank" rel="noopener noreferrer" class="thought-share-link">Share on Bluesky</a>
      <button id="thought-copy-link" class="thought-share-link">Copy link</button>
    </div>

    <div id="thought-loading" class="feed-loading">Loading thought...</div>

    <div id="thought-not-found" class="feed-empty" hidden>
      <p>This thought has drifted off the feed.</p>
      <p><a href="/feed/">Browse all thoughts</a></p>
    </div>

  </div>
</main>
```

- [ ] **Step 2: Add thought centrepiece CSS to feed.css**

```css
/* ── Thought centrepiece (permalink page) ──────────────── */

.thought-centrepiece {
  text-align: center;
  padding: 4rem 0;
  max-width: 600px;
  margin: 0 auto;
}

.thought-centrepiece-quote {
  font-family: 'Courier Prime', monospace;
  font-style: italic;
  font-size: clamp(1.25rem, 3vw, 1.75rem);
  line-height: 1.7;
  color: var(--dark-text);
  margin: 0 0 2rem;
}

.thought-mood-rule {
  border: none;
  border-top: 2px solid var(--dark-accent);
  max-width: 120px;
  margin: 0 auto 1.5rem;
}

.thought-centrepiece-meta {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 0.75rem;
  font-size: var(--text-small);
  color: var(--dark-muted);
}

.thought-prev-next {
  display: flex;
  justify-content: space-between;
  padding: 1rem 0;
  border-top: 1px solid var(--dark-border-subtle);
}

.thought-nav-link {
  color: var(--dark-accent);
  text-decoration: none;
  font-size: var(--text-small);
  font-weight: 500;
}
.thought-nav-link:hover { color: var(--dark-accent-hover); text-decoration: underline; }

.thought-share {
  display: flex;
  justify-content: center;
  gap: 1.5rem;
  padding: 1rem 0;
  font-size: var(--text-xs);
}

.thought-share-link {
  color: var(--dark-muted);
  text-decoration: none;
  background: none;
  border: none;
  cursor: pointer;
  font-family: inherit;
  font-size: inherit;
}
.thought-share-link:hover { color: var(--dark-accent); }
```

- [ ] **Step 3: Update thought.js — prev/next, share, mood wash, static-first**

This is the most complex JS change. In thought.js, update the `showThought` function to:

1. Set mood-colored rule: `document.querySelector('.thought-mood-rule').style.borderTopColor = moodColor;`
2. Set mood wash background on the page container:
```javascript
    // Mood wash
    var page = document.querySelector('.thought-page');
    if (page && moodColor) {
      var r = parseInt(moodColor.slice(1,3),16);
      var g = parseInt(moodColor.slice(3,5),16);
      var b = parseInt(moodColor.slice(5,7),16);
      page.style.background = 'radial-gradient(ellipse at 50% 0%, rgba(' + r + ',' + g + ',' + b + ',0.25) 0%, transparent 50%)';
    }
```

3. Set up prev/next navigation:
```javascript
    // Prev/next
    var navEl = document.getElementById('thought-nav');
    var prevLink = document.getElementById('thought-prev');
    var nextLink = document.getElementById('thought-next');
    if (navEl && allPosts && allPosts.length > 1) {
      var idx = allPosts.findIndex(function (p) { return p.ts === post.ts; });
      if (idx > 0) {
        prevLink.href = '/thought/?ts=' + encodeURIComponent(allPosts[idx - 1].ts);
        prevLink.hidden = false;
      } else {
        prevLink.hidden = true;
      }
      if (idx < allPosts.length - 1) {
        nextLink.href = '/thought/?ts=' + encodeURIComponent(allPosts[idx + 1].ts);
        nextLink.hidden = false;
      } else {
        nextLink.hidden = true;
      }
      navEl.hidden = false;
    }
```

4. Set up share links:
```javascript
    // Share
    var shareEl = document.getElementById('thought-share');
    var bskyLink = document.getElementById('thought-share-bsky');
    var copyBtn = document.getElementById('thought-copy-link');
    if (shareEl) {
      var permalink = 'https://spark.wedd.au/thought/?ts=' + encodeURIComponent(post.ts);
      var shareText = '"' + (post.thought || '').substring(0, 200) + '" — SPARK ' + permalink;
      bskyLink.href = 'https://bsky.app/intent/compose?text=' + encodeURIComponent(shareText);
      copyBtn.onclick = function () {
        navigator.clipboard.writeText(permalink).then(function () {
          copyBtn.textContent = 'Copied!';
          setTimeout(function () { copyBtn.textContent = 'Copy link'; }, 2000);
        });
      };
      shareEl.hidden = false;
    }
```

5. Change fallback order to static-first: swap the fetch chain so `/data/feed.json` is tried before the live API.

- [ ] **Step 4: Commit**

```bash
git add site/thought/index.html site/js/thought.js site/css/feed.css
git commit -m "site: thought permalink — centrepiece layout, mood wash, prev/next, share"
```

---

## Task 13: Scroll animations

**Files:**
- Create: `site/js/scroll-animate.js`
- Modify: all 4 HTML files (add script tag)

- [ ] **Step 1: Create scroll-animate.js**

```javascript
// scroll-animate.js — IntersectionObserver entrance animations
(function () {
  'use strict';
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        entry.target.classList.add('animate-in');
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.1, rootMargin: '0px 0px -40px 0px' });

  function init() {
    document.querySelectorAll('.feed-card, .thought-centrepiece, .warm-card, .band, section > .container, section > .container-hero, section > .container-narrow').forEach(function (el) {
      el.classList.add('animate-ready');
      observer.observe(el);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
```

- [ ] **Step 2: Add animation CSS to base.css**

```css
/* ── Scroll entrance animations ── */
@media (prefers-reduced-motion: no-preference) {
  .animate-ready {
    opacity: 0;
    transform: translateY(16px);
  }
  .animate-in {
    opacity: 1;
    transform: translateY(0);
    transition: opacity 0.4s cubic-bezier(0.25, 0.1, 0.25, 1),
                transform 0.4s cubic-bezier(0.25, 0.1, 0.25, 1);
  }
}
```

- [ ] **Step 3: Add script tag to all 4 HTML files**

In each HTML file, before the closing `</body>`, add:

```html
<script defer src="../js/scroll-animate.js"></script>
```

(For index.html, use `src="js/scroll-animate.js"` without the `../` prefix.)

- [ ] **Step 4: Commit**

```bash
git add site/js/scroll-animate.js site/css/base.css site/index.html site/feed/index.html site/blog/index.html site/thought/index.html
git commit -m "site: scroll entrance animations — IntersectionObserver, respects prefers-reduced-motion"
```

---

## Task 14: Charts — line sparklines, terminal dot, threshold bars

**Files:**
- Modify: `site/js/charts.js`

- [ ] **Step 1: Update drawSparkline to line-only with terminal dot**

Replace the `drawSparkline` function in charts.js:

```javascript
  function drawSparkline(canvas, points, field) {
    if (!canvas || !points || points.length < 2) return;
    var ctx = canvas.getContext('2d');
    var W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);

    var vals = points.map(function (p) { return p[field]; }).filter(function (v) { return v !== null && v !== undefined; });
    if (vals.length < 2) return;

    var minV = Math.min.apply(null, vals);
    var maxV = Math.max.apply(null, vals);
    var range = maxV - minV || 1;

    function toX(i) { return (i / (points.length - 1)) * (W - 4) + 2; }
    function toY(v) { return H - 4 - ((v - minV) / range) * (H - 8); }

    var accent = getComputedStyle(document.documentElement)
      .getPropertyValue('--spark-accent').trim() || '#c48b6e';

    // Line stroke only — no fill, no axes
    ctx.beginPath();
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';

    var lastX, lastY, first = true;
    points.forEach(function (p, i) {
      var v = p[field];
      if (v === null || v === undefined) return;
      var x = toX(i), y = toY(v);
      if (first) { ctx.moveTo(x, y); first = false; }
      else ctx.lineTo(x, y);
      lastX = x; lastY = y;
    });
    ctx.stroke();

    // Terminal dot — 4px circle at the end
    if (lastX !== undefined) {
      ctx.beginPath();
      ctx.arc(lastX, lastY, 2, 0, Math.PI * 2);
      ctx.fillStyle = accent;
      ctx.fill();
    }
  }
```

- [ ] **Step 2: Update drawWaveform for refined bars**

Replace the bar drawing loop in `drawWaveform`:

```javascript
    var BAR_COUNT = 32;
    var BAR_W = 3;
    var GAP = 1;
    var MAX_H = H - 4;
    var BASE_H = 2;
```

And update the drawing to use rounded caps:

```javascript
    ctx.lineCap = 'round';
    for (var i = 0; i < BAR_COUNT; i++) {
      var barH = BASE_H + Math.round(rand() * MAX_H * amplitude);
      var x = i * (BAR_W + GAP);
      ctx.fillRect(x, H - barH, BAR_W, barH);
    }
```

Set opacity: `ctx.globalAlpha = 0.6;` before drawing, `ctx.globalAlpha = 1.0;` after.

- [ ] **Step 3: Commit**

```bash
git add site/js/charts.js
git commit -m "site: charts — line sparklines with terminal dot, refined waveform bars"
```

---

## Task 15: Footer redesign

**Files:**
- Modify: `site/index.html`, `site/css/dark.css`

- [ ] **Step 1: Update footer HTML in index.html**

Replace the footer content:

```html
<footer class="site-footer" data-theme="dark">
  <div class="container">
    <div class="footer-grid">
      <div class="footer-col">
        <h4 class="footer-heading">Site</h4>
        <nav class="footer-links" aria-label="Site navigation">
          <a href="/">Home</a>
          <a href="/#live">Live</a>
          <a href="/#how-it-works">How It Works</a>
          <a href="/#spark-brain">Brain</a>
          <a href="/#faq">FAQ</a>
          <a href="/#docs">Docs</a>
          <a href="/#roadmap">Roadmap</a>
          <a href="/feed/">Feed</a>
          <a href="/blog/">Blog</a>
        </nav>
      </div>
      <div class="footer-col">
        <h4 class="footer-heading">External</h4>
        <nav class="footer-links" aria-label="External links">
          <a href="https://bsky.app/profile/spark.wedd.au" target="_blank" rel="noopener noreferrer">Bluesky</a>
          <a href="https://github.com/adrianwedd/spark" target="_blank" rel="noopener noreferrer">GitHub</a>
        </nav>
      </div>
    </div>
    <p class="site-footer-credit">SPARK — built by Adrian and Obi together.</p>
  </div>
</footer>
```

- [ ] **Step 2: Update footer CSS in dark.css**

Replace the site-footer rules:

```css
.site-footer {
  padding: 4rem 1.25rem 2.5rem;
  text-align: left;
  font-size: var(--text-small);
  background: var(--dark-bg);
  border-top: 1px solid var(--dark-border);
}
.footer-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 2rem;
  margin-bottom: 2rem;
}
.footer-heading {
  font-family: 'Courier Prime', monospace;
  font-size: var(--text-xs);
  font-weight: 400;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--dark-muted);
  margin: 0 0 0.75rem;
}
.site-footer .footer-links {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.site-footer .footer-links a {
  color: var(--dark-accent);
  text-decoration: none;
  font-weight: 500;
}
.site-footer .footer-links a:hover { text-decoration: underline; color: var(--dark-accent-hover); }
.site-footer-credit {
  margin: 0;
  color: var(--dark-muted);
  font-weight: 300;
  text-align: center;
}

@media (max-width: 480px) {
  .footer-grid { grid-template-columns: 1fr; gap: 1.5rem; }
}
```

- [ ] **Step 3: Update footers on feed, blog, thought pages**

These pages have a simpler footer. Update each to include at minimum: Home, Feed, Blog, Bluesky, GitHub links and the credit line. Use the same `.site-footer` class and `.footer-links` (horizontal for sub-pages).

- [ ] **Step 4: Commit**

```bash
git add site/index.html site/css/dark.css site/feed/index.html site/blog/index.html site/thought/index.html
git commit -m "site: footer — two-column link grid, Inter typography, all navigation links"
```

---

## Task 16: Chat widget — full warm→dark migration

**Files:**
- Modify: `site/css/chat.css`

- [ ] **Step 1: Replace all warm references in chat.css**

Apply these replacements throughout chat.css:

| Find | Replace |
|------|---------|
| `var(--warm-accent, #e8875a)` | `var(--dark-accent, #c48b6e)` |
| `var(--warm-accent)` | `var(--dark-accent)` |
| `var(--warm-bg, #fdf8f0)` | `var(--dark-surface, #24212a)` |
| `var(--warm-bg)` | `var(--dark-surface)` |
| `var(--warm-text)` | `var(--dark-text)` |
| `var(--warm-muted)` | `var(--dark-muted)` |

- [ ] **Step 2: Commit**

```bash
git add site/css/chat.css
git commit -m "site: chat widget — full warm-to-dark palette migration"
```

---

## Task 17: Dashboard — grouped metrics, mood duration

**Files:**
- Modify: `site/js/dashboard.js`, `site/index.html`

- [ ] **Step 1: Add mood duration display**

In dashboard.js `renderPresence` function, after the mood pulse update, add:

```javascript
    // Mood duration
    var durationEl = document.getElementById('mood-duration');
    if (durationEl && state.mood && state.last_spoken_ts) {
      var moodSince = new Date(state.last_spoken_ts);
      var nowMs = Date.now();
      var diffMin = Math.round((nowMs - moodSince.getTime()) / 60000);
      if (diffMin < 1) durationEl.textContent = state.mood + ' just now';
      else if (diffMin < 60) durationEl.textContent = state.mood + ' for ' + diffMin + ' min';
      else durationEl.textContent = state.mood + ' for ' + Math.round(diffMin / 60) + 'h';
    }
```

- [ ] **Step 2: Add mood-duration element in index.html**

In the mood presence card (around the pulse circle), add below the mood-word span:

```html
<p id="mood-duration" class="mood-duration"></p>
```

Add CSS in dark.css:

```css
.mood-duration {
  font-family: 'Inter', system-ui, sans-serif;
  font-size: var(--text-small);
  font-weight: 500;
  color: var(--dark-muted);
  text-align: center;
  margin-top: 0.5rem;
}
```

- [ ] **Step 3: Commit**

```bash
git add site/js/dashboard.js site/index.html site/css/dark.css
git commit -m "site: dashboard — mood duration display below pulse circle"
```

---

## Task 18: Use container-narrow on feed/blog/thought pages

**Files:**
- Modify: `site/feed/index.html`, `site/blog/index.html`, `site/thought/index.html`, `site/css/feed.css`

- [ ] **Step 1: Update HTML containers**

In each of `feed/index.html`, `blog/index.html`, and `thought/index.html`, change `class="container"` to `class="container-narrow"` on the main content wrapper.

- [ ] **Step 2: Remove the container override in feed.css**

Delete the `.container { max-width: 680px; }` rule from feed.css (around line 19-22) since `.container-narrow` now handles this.

- [ ] **Step 3: Commit**

```bash
git add site/feed/index.html site/blog/index.html site/thought/index.html site/css/feed.css
git commit -m "site: use container-narrow (680px) on feed, blog, thought pages"
```

---

## Task 19: Run tests and final verification

**Files:** None (verification only)

- [ ] **Step 1: Run the full test suite**

```bash
python -m pytest tests/ -x -q
```

Expected: All 623+ tests pass. The only backend change was adding `salience` to px-post feed dict.

- [ ] **Step 2: Verify site locally**

```bash
cd site && python3 -m http.server 8080
```

Open `http://picar.local:8080` and check:
- Hero renders with mood sentence and photo slot (placeholder until photo provided)
- Nav has 5 items
- Feed page shows cards with top borders
- Blog page shows tiered cards
- Thought permalink shows centrepiece layout
- Footer has two-column links
- Scroll animations fire on scroll
- Mobile hamburger has backdrop and focus trap
- Inter font loads (check Network tab)

- [ ] **Step 3: Final commit if any fixups needed**

```bash
git add -A
git commit -m "site: editorial magazine redesign — fixups from manual verification"
```

---

## Deferred Items

These spec items are intentionally deferred to a follow-up session:

- **Dashboard grouped metric tiles** (spec section 8.3): Restructuring CPU+Temp, RAM+Disk, Battery into grouped tiles requires significant HTML/JS refactoring of the metric rendering in dashboard.js and the metric HTML in index.html (~50 lines of markup + JS render logic). The current tile layout works; grouping is a polish improvement.
- **Hero photo**: The photo slot is built but the actual images (`site/img/spark-hero.webp` + `.jpg` at 3 sizes) must be provided by Adrian before the hero renders correctly.

---

## Summary

| Task | Description | Files | Est. |
|------|-------------|-------|------|
| 1 | Backend: salience in feed posts | bin/px-post | 2 min |
| 2 | Design tokens: base.css | base.css | 5 min |
| 3 | Dark typography: dark.css | dark.css | 3 min |
| 4 | Warm typography: warm.css | warm.css | 3 min |
| 5 | Navigation: HTML + CSS + JS | 6 files | 10 min |
| 6 | Font imports: all HTML | 4 HTML files | 3 min |
| 7 | Hero section | index.html, dark.css, dashboard.js | 10 min |
| 8 | Remove racing | index.html, dashboard.js, live.js | 5 min |
| 9 | Carousel: timing, pause | live.js, dark.css, base.css | 5 min |
| 10 | Feed: cards, summary, tiers | feed.css, feed.js, feed/index.html | 10 min |
| 11 | Blog: three tiers, article | feed.css, blog.js | 10 min |
| 12 | Thought: centrepiece | thought/index.html, thought.js, feed.css | 10 min |
| 13 | Scroll animations | scroll-animate.js, base.css, 4 HTML | 5 min |
| 14 | Charts: sparklines, bars | charts.js | 5 min |
| 15 | Footer redesign | index.html, dark.css, 3 HTML | 5 min |
| 16 | Chat: warm→dark | chat.css | 3 min |
| 17 | Dashboard: mood duration | dashboard.js, index.html, dark.css | 5 min |
| 18 | Container-narrow on sub-pages | 3 HTML, feed.css | 3 min |
| 19 | Tests + verification | — | 5 min |
