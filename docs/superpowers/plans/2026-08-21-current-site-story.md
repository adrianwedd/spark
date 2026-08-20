# Current SPARK Site Story Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every public site surface explain SPARK as a persistent embodied agent with local cognition, durable epistemic memory, and deterministic authority boundaries.

**Architecture:** Rewrite static HTML and narrowly extend existing CSS; do not add backend APIs. Pin the story with static regression tests, then verify the architectural source tests and inspect rendered desktop/mobile pages.

**Tech Stack:** Static HTML/CSS/JavaScript, pytest, Playwright, Cloudflare Pages.

**Spec:** `docs/superpowers/specs/2026-08-21-current-site-story-design.md`

## Global Constraints

- Keep the existing visual system; this is not a wholesale redesign.
- Do not expose private state or add a public API for decorative metrics.
- Do not claim that a value is live unless an existing public endpoint supplies it.
- Avoid generic AI-marketing language.
- Preserve dirty work and run targeted tests only on SPARK.

---

### Task 1: Pin the public narrative

**Files:**
- Create: `tests/test_site_story.py`

**Interfaces:**
- Consumes: static public HTML in `site/`
- Produces: regression checks for required and forbidden public claims

- [ ] Write tests that require the hero, three pillars, cognition path, current route claims, and updated supporting-page metadata.
- [ ] Add checks rejecting “robot with an inner life”, “Claude CLI (px-spark)”, and “Four-Tier LLM Fallback” from public HTML.
- [ ] Run `python -m pytest tests/test_site_story.py -v` and confirm the checks fail against the old story.

### Task 2: Rewrite the homepage hierarchy

**Files:**
- Modify: `site/index.html`
- Modify: `site/css/dark.css`
- Modify: `site/css/warm.css`

**Interfaces:**
- Consumes: the existing hero, themed sections, and dashboard
- Produces: the persistent-agent hero, pillars, cognition flow, and evidence-led architecture copy

- [ ] Replace homepage and schema metadata with the persistent-agent proposition.
- [ ] Rewrite the hero and first warm section into the short explainer and three pillars.
- [ ] Replace the stale voice-loop/fallback architecture with the current local → M5 → resident brain → policy flow.
- [ ] Reframe the dashboard as live evidence without adding data fields.
- [ ] Demote and tighten provenance/habit copy while retaining its mechanisms.
- [ ] Rewrite stale FAQ and docs labels surfaced on the homepage.
- [ ] Add responsive CSS only for the new editorial components.
- [ ] Run `python -m pytest tests/test_site_story.py tests/test_site_layout.py -v`.

### Task 3: Align supporting public pages

**Files:**
- Modify: `site/feed/index.html`
- Modify: `site/thought/index.html`
- Modify: `site/blog/index.html`

**Interfaces:**
- Consumes: shared navigation and feed-page styles
- Produces: metadata and landing copy consistent with the homepage

- [ ] Update title-adjacent descriptions, Open Graph, Twitter, and visible subtitles.
- [ ] Keep dynamic worker rewriting intact.
- [ ] Run `python -m pytest tests/test_site_story.py -v`.

### Task 4: Verify claims and presentation

**Files:**
- Modify only if verification finds a discrepancy.

**Interfaces:**
- Consumes: architectural source tests and rendered pages
- Produces: evidence for the final claim ledger and screenshots

- [ ] Run targeted resident routing, M5, provenance, policy, vision, health, and containment tests.
- [ ] Serve `site/` locally and inspect homepage, feed, thought, and blog at desktop and mobile widths.
- [ ] Save before/after screenshots outside the deployable site tree.
- [ ] Scan public HTML for the known stale phrases and generic marketing language.

### Task 5: Preview, PR, and merge

**Files:**
- No source files unless CI or preview reveals an issue.

**Interfaces:**
- Consumes: focused branch with verified site changes
- Produces: merged PR and production deployment via the repository workflow

- [ ] Commit exact-path changes on a focused branch and push it.
- [ ] Open a PR and wait for CI and preview deployment.
- [ ] Inspect the preview on desktop and mobile; fix any issue and repeat verification.
- [ ] Merge only after required checks and preview are good.
