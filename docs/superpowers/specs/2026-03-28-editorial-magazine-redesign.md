# Editorial Magazine Redesign — spark.wedd.au

**Date**: 2026-03-28
**Status**: Approved (rev 2 — QA resolutions applied)
**Author**: Adrian + Claude
**QA**: Claude agent, Codex, Gemini
**Scope**: Full redesign of all pages — hero, nav, typography, feed, blog, thought permalink, dashboard, warm sections, charts, scroll animations, footer, chat widget.

---

## 1. Design Intent

spark.wedd.au serves two audiences: visitors meeting SPARK (a robot with an inner life) and technical visitors appreciating the craft behind it. The site should feel like a glossy editorial magazine about a thinking robot — not a developer dashboard, not a terminal.

**Warm = human** (Brain, FAQ sections). **Dark = machine** (everything else). This is an intentional design choice, not a leftover. The Racing section is removed from the public site (internal-only at picar.local:8420).

---

## 2. Design System Foundation

### 2.1 Typography

**Primary font**: Inter (Google Fonts variable font) — `'Inter', system-ui, -apple-system, sans-serif`. Loaded as **variable font** (`wght@300..800`) — single file, ~100KB woff2, supports all needed weights without separate requests.

**Monospace font**: Courier Prime (Google Fonts) — `'Courier Prime', 'Courier New', monospace`. Loaded at regular + italic (~45KB total). Used only for SPARK's voice (thought quotes, carousel text, blog post body) and technical elements (h3 labels, badges, code blocks).

**Playfair Display is removed entirely** from the site. One less font, one less voice.

**Type scale** (custom, not a strict mathematical ratio — optimised for editorial readability):

| Token              | Value                           | Weight | Use                                |
|---------------------|---------------------------------|--------|------------------------------------|
| `--text-display`    | `clamp(2.5rem, 6vw, 3.5rem)`   | 800    | h1 only                           |
| `--text-title`      | `1.75rem`                       | 700    | h2 section headings               |
| `--text-subtitle`   | `1.25rem`                       | 600    | Lead text, card titles, featured quotes |
| `--text-body`       | `1rem` (16px)                   | 300    | Body copy                         |
| `--text-small`      | `0.875rem`                      | 500    | Metadata, nav links, captions     |
| `--text-xs`         | `0.75rem`                       | 500    | Badges, labels                    |

**Font assignments**:
- **Headings (h1, h2)**: Inter. h1: weight 800, `letter-spacing: -0.04em`, `line-height: 1.05`. h2: weight 700, `letter-spacing: -0.025em`.
- **Body copy**: Inter 300, `line-height: 1.7`. Light weight reads elegant on dark backgrounds. If readability is poor on low-res displays, bump to 400 at implementation time.
- **Nav, metadata**: Inter 500, `letter-spacing: 0.01em`.
- **SPARK's voice** (thought quotes, carousel, blog body): Courier Prime italic.
- **Section labels (h3)**: Courier Prime regular, uppercase, `letter-spacing: 0.08em`.
- **Numeric displays**: `font-variant-numeric: tabular-nums` on timestamps, metrics, percentages.

### 2.2 Spacing

Remove `min-height: 100vh` from all sections. Sections use `padding: 4rem 2rem` on desktop, `padding: 2.5rem 1rem` on mobile. Let content determine height.

### 2.3 Containers

Three container classes:

| Class               | Max-width | Use                     |
|---------------------|-----------|-------------------------|
| `.container-hero`   | `1100px`  | Hero section only       |
| `.container`        | `900px`   | Homepage body sections  |
| `.container-narrow` | `680px`   | Feed, blog, thought     |

All containers: `margin: 0 auto; padding: 0 1.25rem;`. The transition from hero width to body width happens naturally at the section boundary — no special easing needed since sections have vertical padding between them.

### 2.4 Border radii

- `--radius-sm`: `8px` (badges, inputs)
- `--radius`: `12px` (cards)
- `--radius-lg`: `20px` (hero image — unchanged from current value)

### 2.5 Motion

All entrance animations gated behind `@media (prefers-reduced-motion: no-preference)`.

- **Scroll entrance**: `IntersectionObserver` with `threshold: 0.1`, `rootMargin: '0px 0px -40px 0px'`. Animation: `opacity: 0 → 1`, `translateY(16px) → 0`, `0.4s cubic-bezier(0.25, 0.1, 0.25, 1)`. One-shot (no re-trigger on scroll back).
- **Card hover**: `translateY(-2px)`, `transition: all 0.25s cubic-bezier(0.25, 0.1, 0.25, 1)`.
- **Mood pulse**: Existing animation retained (slow/mid/fast by arousal).

`scroll-animate.js` is loaded on **all pages** (homepage, feed, blog, thought) via a `<script defer>` tag.

### 2.6 Polish details

- Mood badges: solid semi-transparent background using `--mood-*-surface` vars (no `backdrop-filter` — removed for performance on Pi/low-end devices). Frosted-glass effect is a progressive enhancement only if we confirm smooth performance on Pi hardware.
- Hero photo: `filter: contrast(1.05) brightness(1.02)` to pop on dark background.
- Thought quotes: `text-indent: -0.4em` for hanging punctuation on opening quote marks.

### 2.7 Breakpoints

| Token     | Value   | Use                                    |
|-----------|---------|----------------------------------------|
| `--bp-xs` | `480px` | Compact mobile (reduced section padding) |
| `--bp-sm` | `600px` | Feed card mobile padding               |
| `--bp-md` | `700px` | Hamburger nav, hero column stack       |
| `--bp-lg` | `900px` | Presence grid 3→2 col, metric grid    |
| `--bp-xl` | `1100px` | Hero container cap                    |

These are documented reference values. CSS uses raw `px` in `@media` queries (CSS custom properties don't work in media queries).

### 2.8 z-index strategy

| Layer                    | z-index |
|--------------------------|---------|
| Page content             | auto    |
| Mobile nav backdrop      | 90      |
| Mobile nav panel         | 95      |
| Fixed nav bar            | 100     |
| Chat widget bubble       | 9000    |
| Chat widget panel        | 9001    |

---

## 3. Hero Section

### 3.1 Layout

Two-column on desktop (text 60% left, photo 40% right) via `display: grid; grid-template-columns: 1fr 0.7fr; gap: 3rem; align-items: center;`. Stacked on mobile (text above, photo below). Uses `.container-hero` (1100px).

### 3.2 Left column

- **h1**: "SPARK" — Inter 800, `--text-display`, copper (`--dark-accent`), subtle `text-shadow: 0 0 60px var(--dark-glow)`.
- **Live mood sentence**: Inter 600, `--text-subtitle`, off-white (`--dark-text`). Populated by JS from `/api/v1/public/status`. **Algorithm**: `"Feeling {mood}."` as the base. If `last_thought` is available and ≤80 chars, append it as a second sentence. If >80 chars, truncate to the first sentence (split on `.!?`). If the API is unreachable, show: *"A robot with an inner life."*
- **Credit line**: Inter 500, `--text-small`, muted (`--dark-muted`). "Built by Adrian and Obi together."
- **CTA row**: Two ghost buttons — "Explore the feed →" (`/feed/`) and "Live dashboard ↓" (`#live`). Copper border + text. Hover: copper background with `var(--dark-bg)` text. Focus: `2px solid var(--dark-accent)` outline, `2px offset`. Disabled state: not applicable (always active links).

### 3.3 Right column

- **Photo**: Responsive `<picture>` element:
  - `site/img/spark-hero.webp` (primary, ~60-80KB)
  - `site/img/spark-hero.jpg` (fallback, ~100-120KB)
  - `srcset`: `spark-hero-600w.webp 600w, spark-hero-900w.webp 900w, spark-hero.webp 1200w`
  - `sizes`: `(max-width: 700px) 280px, 40vw`
  - `alt`: "SPARK — a PiCar-X robot with ultrasonic sensors and a camera, sitting on a desk"`
  - `loading="eager"` (LCP element)
  - `border-radius: var(--radius-lg)` (20px), mood-colored `box-shadow: 0 0 60px var(--spark-glow)`.
  - `filter: contrast(1.05) brightness(1.02)`.
  - Aspect ratio: landscape, roughly 4:3 or 3:2. Photo must be provided before hero can be implemented.
- **Status line**: Below photo — status dot + "Online" / "Sleeping" in `--text-xs` muted.

### 3.4 Thought carousel (below two-column hero)

Moves below the hero columns, centered in `.container` (900px). Pull-quote style: no card background, large Courier Prime italic text, mood badge, timestamp. Single thought at a time, crossfading every 8 seconds.

**Accessibility**: Carousel retains `aria-live="polite"` on the container. Pauses on hover AND on focus within the carousel region (CSS `:hover` and JS `focusin`/`focusout`). A visually-hidden "Pause" / "Resume" button is included for keyboard users (satisfies WCAG 2.2.2 Pause, Stop, Hide). No visible dots — the pause button is the only control. Click/swipe-to-advance from current implementation is preserved.

### 3.5 Mobile (below 700px)

Single column. Photo shrinks to `max-width: 280px`, centered. CTA buttons stack vertically. Mood sentence stays above photo.

### 3.6 Offline degradation

If API unreachable: mood sentence shows static fallback, photo glow defaults to copper, carousel shows cached thoughts from localStorage (feed.js already caches to `spark_feed_cache`; carousel reads from same cache).

---

## 4. Navigation

### 4.1 Structure

5 items: Home (`/`), Live (`/#live`), Feed (`/feed/`), Blog (`/blog/`), GitHub (external).

**Removed from nav** (confirmed intentional): How It Works, Brain, Racing, FAQ, Docs, Roadmap. These homepage sections are reachable by scrolling and via the footer link grid. Racing is removed entirely from the public homepage.

Logo: "SPARK" in Courier Prime + status dot. Height: `56px`.

### 4.2 Styling

Inter 500, `--text-small`. Active link: copper color. Hover: `--dark-text`. GitHub link: opacity treatment (existing pattern).

### 4.3 Mobile (below 700px)

Hamburger → slide-down panel with backdrop overlay (`.nav-backdrop`, `position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 90;`). Nav panel at `z-index: 95`. 5 links, well-spaced.

**Focus trap implementation**: On open, JS saves the previously focused element, moves focus to the first nav link, and traps Tab/Shift-Tab within the panel links + close button. On close (button click, Escape key, or backdrop click), focus returns to the previously focused element. `aria-expanded` toggled on the hamburger button. Page scroll locked (`overflow: hidden` on `<body>`) while open.

---

## 5. Feed Page (`/feed/`)

### 5.1 Header

- **h1**: "Thought Feed" — Inter 700, `--text-title`, copper.
- **Summary line**: Mood distribution computed client-side from the fetched feed data. Counts moods across **all loaded posts** (not time-windowed — avoids timezone/week-boundary ambiguity). Format: *"147 thoughts — 38 curious, 31 contemplative, 24 peaceful, ..."* (top 3 moods shown, rest omitted). If zero posts: *"No thoughts yet. SPARK is still thinking."* Offline fallback: *"A stream of SPARK's inner life — thoughts that crossed the salience threshold."*

### 5.2 Card design — two tiers

**Data requirement**: Feed posts must include `salience` field. `bin/px-post` writes salience to the queue (line 678) but omits it from `feed.json` posts (line 463-467). **Fix needed**: add `"salience": thought.get("salience", 0)` to the feed post dict in px-post.

**Standard card** (salience < 0.85):
- Background: `--dark-surface`. Border: `1px solid var(--dark-border)`. Radius: `var(--radius)`.
- Mood-colored top border: `border-top: 2px solid var(--mood-*)`. No left border (replaces current `border-left`).
- Padding: `1.75rem 2rem`.
- Quote: Courier Prime italic, `--text-body`, `--dark-text`.
- Spacer: `0.75rem`.
- Metadata row: mood badge (left), timestamp (right). Inter 500, `--text-small`.
- Hover: `translateY(-2px)`, faint glow.

**Featured card** (salience >= 0.85):
- `border-top: 3px solid var(--mood-*)`.
- Quote: Courier Prime italic, `--text-subtitle` (1.25rem).
- Padding: `2rem 2.25rem`.
- Otherwise identical structure.

### 5.3 Date headers

Full-width, flex row. Courier Prime regular, `--text-xs`, uppercase, `letter-spacing: 0.08em`. A thin copper `1px` rule stretches after the text via a flex layout: `.feed-date-label { display: flex; align-items: center; gap: 1rem; }` with `::after { content: ''; flex: 1; height: 1px; background: var(--dark-accent-muted); }`.

### 5.4 Pagination

"Load more" ghost button (unchanged). Below: *"Showing 20 of 147 thoughts"* in `--text-xs` muted. Total count comes from the feed data array length.

### 5.5 Scroll entrance

Cards fade in with staggered delay: `50ms * index` per batch, capped at `250ms`.

---

## 6. Blog Page (`/blog/`)

### 6.1 Header

- **h1**: "SPARK's Blog" — Inter 700, `--text-title`, copper.
- **Subtitle**: *"Reflections, essays, and the arc of a thinking life."* — Inter 500, `--text-small`, muted.

### 6.2 Card hierarchy — three tiers

**Data confirmed**: Blog API already returns `type`, `title`, `body`, `mood_summary`, `salience`, `word_count`, `thought_count` per post. No API changes needed.

**Essay cards** (type: essay, monthly, yearly):
- Title: Inter 600, `--text-subtitle`, copper.
- Excerpt: Courier Prime italic, `--text-body`, 2 lines max (`overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2;`).
- `border-top: 3px solid var(--mood-*)`. Mood derived from `mood_summary` (first mood in the comma-separated list).
- Padding: `2.25rem`.
- Meta: type badge + mood badge + timestamp.

**Weekly cards** (type: weekly):
- Title: Inter 600, `--text-body`, off-white.
- Excerpt: Courier Prime italic, `--text-small`, 1 line (`-webkit-line-clamp: 1`).
- `border-top: 2px solid var(--mood-*)`.
- Standard padding.

**Daily cards** (type: daily):
- Title as link, no separate excerpt.
- Type badge + timestamp inline. No mood badge.
- `border-top: 1px solid var(--dark-border)` (muted, not mood-colored).
- Reduced padding: `1rem 1.5rem`.

**Empty tier handling**: If there are no essay/monthly/yearly posts, the page renders only weekly + daily tiers. No special empty state needed — the tiers are just CSS class assignments, not separate DOM sections.

### 6.3 Single post view

The article breaks OUT of the card. New class: `.blog-article` (no background, no border, no card styling).

- **Back link**: "← Blog" — Inter 500, copper, `--text-small`.
- **Title**: Inter 700, `clamp(1.5rem, 4vw, 2.25rem)`, copper.
- **Meta row**: type badge + mood badge + timestamp.
- **Divider**: `1px solid var(--dark-border)`.
- **Body**: Courier Prime, `1rem`, `line-height: 1.8`, max-width `680px`. Paragraphs (`\n{2,}` splits): `margin-bottom: 1.25rem`. Single newlines within a paragraph are ignored (current behavior, preserved).

### 6.4 Date grouping

Same as feed: uppercase Courier Prime date headers with copper rule.

---

## 7. Thought Permalink (`/thought/?ts=`)

### 7.1 Layout

No card container. The thought floats as a centrepiece.

- **Breadcrumb**: "SPARK / Feed / This thought" — Inter 500, `--text-small`, muted. Copper links.
- **Thought text**: Courier Prime italic, `clamp(1.25rem, 3vw, 1.75rem)`, centered, max-width `600px`. Vertical padding: `4rem 0`.
- **Mood rule**: `2px solid var(--mood-*)`, `max-width: 120px`, centered.
- **Meta**: Mood badge + timestamp, centered.
- **Prev/next**: "← Earlier thought" and "Later thought →" as copper ghost links, flex-spaced. Determined from the feed data array (the same data source used to render the thought) — no separate API call. **Edge cases**: If first thought, hide "← Earlier". If last thought, hide "Later →". If the only thought, hide both.

### 7.2 Mood wash

Subtle mood-colored radial gradient at top of page. Uses a **dedicated higher-opacity variable** since `--mood-*-surface` (12% opacity) is too faint for a radial wash. Implementation: inline style on the page container set by JS — `background: radial-gradient(ellipse at 50% 0%, rgba(R,G,B,0.25) 0%, transparent 50%)` where RGB is computed from the mood color. The gradient is purely decorative — no text sits on the tinted area (breadcrumb and thought text are below the gradient's visible zone).

### 7.3 Share row

Below prev/next: "Share on Bluesky" (link to `https://bsky.app/intent/compose?text=...` with URL-encoded thought text + permalink) and "Copy link" button (JS `navigator.clipboard.writeText()`; button text changes to "Copied!" for 2s). Both in Inter 500, `--text-xs`, muted.

### 7.4 Data source

Thought data resolves from the static feed snapshot on Cloudflare Pages first (`/data/feed.json`), then falls back to the live Pi API, then GitHub mirror, then localStorage cache. This is a change from the current API-first order — the static snapshot is always available even when the Pi sleeps.

### 7.5 Not-found state

*"This thought has drifted off the feed."* + link to `/feed/`.

---

## 8. Dashboard (Live Section)

### 8.1 Presence band (always visible)

3-column grid:

- **Mood card** (left): Pulse circle (retained). Below: *"contemplative for 23 min"* — Inter 500, `--text-small`, muted. Mood duration computed from `last_spoken_ts` and current `mood` in the status API. Sparklines move to World band.
- **State card** (center): Obi mode, presence, ambient, distance. Labels: Inter 500, `--text-xs`. Values: Inter 600, `--text-body`. Proximity bar: 10px height, mood-colored fill.
- **Speech card** (right): Last spoken, persona. Same typographic cleanup.

### 8.2 World band (collapsed)

Weather, sparklines, detection list. Sparkline canvases: 32px height (up from 20px).

### 8.3 Machine band (collapsed)

Grouped metric tiles:
- Row 1: CPU+Temp (one tile), RAM+Disk (one tile), Battery (one tile).
- Each tile: primary value large (`--text-subtitle`), secondary below (`--text-xs`). Inline sparkline accent.
- Row 2: Services strip (unchanged — dark theme overrides already exist at dark.css:180).

### 8.4 Race widget

Removed from public site. Stays on local dashboard (`picar.local:8420`). Remove from: `site/index.html` (racing section + nav link), `site/js/dashboard.js` (race render function), `site/js/live.js` (race fetch).

---

## 9. Charts & Sparklines

### 9.1 Sparklines (mood, sonar, vitals)

- Height: 32px desktop, 28px mobile.
- Style: single 1.5px stroke in `--spark-accent`. No fill, no axes, no labels.
- Terminal dot: 4px circle at the line's end — anchors "now."
- Background: transparent.

### 9.2 Audio visualizer (speech card)

- Vertical bars: 3px width, 1px gap, `lineCap: 'round'`. Height: 32px.
- Color: `--spark-accent` at 60% opacity. Decays to 2px baseline when silent.

### 9.3 Dashboard metric bars

- Height: 8px, `border-radius: 4px`.
- Fill: `--spark-accent` (normal). Amber `#f59e0b` above 80%. Red `#ef4444` above 95%.
- Battery inverted: red below 20%, amber below 30%.

### 9.4 Services strip

Unchanged — colored dots are already effective.

---

## 10. Warm Sections (Brain, FAQ)

### 10.1 Transition

The dark→warm boundary gets a smooth `80px` gradient fade: `linear-gradient(to bottom, var(--dark-bg), var(--warm-bg) 80px)` on a pseudo-element. Intentional, not abrupt.

### 10.2 Warm card refresh

`.warm-card` retains white bg and soft shadow. Updates:
- Headings switch to Inter (matching the site-wide type system).
- `border-radius: var(--radius)` (12px).
- Hover lift preserved.

### 10.3 Warm headings

h2: Inter 700, `--warm-text`. No more Playfair Display.

### 10.4 Racing section

Removed from public homepage. Technical details live in the repo README.

---

## 11. Unchanged Sections

The following homepage sections receive the new typography (Inter headings, Courier Prime for code) but no layout changes:

- **How It Works** (`#how-it-works`): Dark section. h2/h3 switch to Inter/Courier Prime respectively. `pre.arch` blocks retain Courier Prime. Voice sample `<audio>` elements unstyled (browser default). `min-height: 100vh` removed.
- **Docs** (`#docs`): Dark section. h2 to Inter, collapsible tool docs retain Courier Prime for code. `min-height: 100vh` removed.
- **Roadmap** (`#roadmap`): Dark section. h2 to Inter, roadmap items retain current structure. `min-height: 100vh` removed.

---

## 12. Chat Widget

Full migration of `chat.css` from warm to dark palette. All warm references need updating:

| Current var               | Replacement              | Lines  |
|---------------------------|--------------------------|--------|
| `--warm-accent`           | `--dark-accent`          | 18, 25, 88, 112, 167, 173, 189 |
| `--warm-bg`               | `--dark-surface`         | 47, 160 |
| `--warm-text`             | `--dark-text`            | 69, 161 |
| `--chat-bubble-color` fallback `--warm-accent` | `--dark-accent` | 18, 112, 173 |

Chat panel interior: `--dark-surface` background. Chat bubble accent: `--dark-accent` (already set via `--chat-bubble-color` in base.css, but fallback chain needs updating).

---

## 13. Offline Banner

`.feed-offline-banner` class (already implemented in feed.css). All three JS files (feed.js, blog.js, thought.js) use the class instead of inline styles.

---

## 14. API Data Contract

### 14.1 Feed posts (`/api/v1/public/feed`)

Current fields: `ts`, `thought`, `mood`, `posted_ts`.
**Required addition**: `salience` (float, 0-1). Source: already available in px-post queue data (line 678). Fix: add to feed.json post dict at px-post line 463-467.

### 14.2 Blog posts (`/api/v1/public/blog`)

Already includes: `id`, `type`, `title`, `body`, `mood_summary`, `salience`, `ts`, `source`, `word_count`, `thought_count`. No changes needed.

### 14.3 Status (`/api/v1/public/status`)

Already includes: `persona`, `mood`, `last_thought`, `last_spoken_ts`, `salience`, `ts`, `listening`. No changes needed.

---

## 15. Files Changed

### CSS
- `site/css/base.css` — type scale tokens, Inter variable font stack, container classes, remove `min-height: 100vh`, breakpoint documentation, z-index layer
- `site/css/dark.css` — h1/h2 to Inter, carousel dark overrides, dashboard grouped metrics, remove race widget styles
- `site/css/warm.css` — replace Playfair with Inter, warm transition gradient, remove racing section styles
- `site/css/feed.css` — two-tier feed cards (top border, not left), three-tier blog cards, date headers with copper rule, thought permalink centrepiece, blog article layout
- `site/css/colors.css` — unchanged (mood palette already correct)
- `site/css/chat.css` — full warm-to-dark migration (all 8+ var references)

### HTML
- `site/index.html` — hero restructure (two-column + carousel), nav to 5 items, remove `#racing` section, footer link grid, Inter variable font import, `scroll-animate.js` script tag
- `site/feed/index.html` — update font import (Inter replaces Playfair), add mood summary container, `scroll-animate.js` script tag
- `site/blog/index.html` — update font import, `scroll-animate.js` script tag
- `site/thought/index.html` — update font import, restructure for centrepiece layout, add prev/next + share containers, `scroll-animate.js` script tag

### JS
- `site/js/dashboard.js` — live mood sentence in hero, mood duration display, grouped metric rendering, remove race widget
- `site/js/live.js` — remove race fetch, carousel timing 10s→8s, carousel pause-on-hover/focus
- `site/js/feed.js` — salience-based card tier selection, staggered scroll animation, mood distribution summary, count display
- `site/js/blog.js` — three-tier card rendering, blog-article class for single post (replaces feed-card)
- `site/js/thought.js` — prev/next navigation, share row, mood wash background, static-snapshot-first fallback order
- `site/js/charts.js` — line sparklines (stroke, not fill), terminal dot, threshold-colored metric bars
- `site/js/nav.js` — focus trap on mobile, backdrop overlay element, Escape key close, scroll lock
- New: `site/js/scroll-animate.js` — IntersectionObserver entrance animations (all pages)

### Backend (minor)
- `bin/px-post` — add `salience` field to feed.json post dict (1-line change at line ~465)

### Assets
- `site/img/spark-hero.webp` + `.jpg` — hero photo of SPARK at 3 sizes (600w, 900w, 1200w). Provided by Adrian.

### Removed
- Playfair Display font import (all HTML files)
- `#racing` section and nav link (index.html)
- Race widget and fetch (dashboard.js, live.js)
- Race API endpoint references from public site JS
