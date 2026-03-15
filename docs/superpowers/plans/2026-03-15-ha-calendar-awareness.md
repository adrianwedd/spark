# HA Calendar Awareness Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give SPARK awareness of upcoming calendar events (Obi's and family) via Home Assistant, so it can proactively mention transitions, help with preparation, and be contextually aware of the day's schedule.

**Architecture:** New `_fetch_ha_calendar()` function in `bin/px-mind` alongside the existing `_fetch_ha_presence()`. Same pattern: HA REST API → cache → awareness dict → reflection prompt context. No new files created.

**Tech Stack:** HA REST API (`/api/calendars/{entity_id}`), existing `PX_HA_TOKEN` auth

**Issue:** #60

---

## File Structure

| File | Responsibility |
|------|---------------|
| `bin/px-mind` | Add `_fetch_ha_calendar()`, cache, awareness enrichment, prompt injection |
| `tests/test_mind_utils.py` | Tests for calendar parsing, event formatting, awareness injection |

---

## Chunk 1: Calendar Fetch and Awareness Integration

### Task 1: Calendar Fetch Function

**Files:**
- Modify: `bin/px-mind:~115-120` (constants)
- Modify: `bin/px-mind:~878` (new function after `_fetch_ha_presence`)
- Modify: `tests/test_mind_utils.py`

- [ ] **Step 1: Write tests**

  Add to `tests/test_mind_utils.py`:
  - `test_parse_ha_calendar_events` — given raw HA calendar JSON (`[{"summary": "Swimming", "start": {"dateTime": "..."}, "end": {"dateTime": "..."}}]`), verify `_parse_calendar_events()` returns `[{"title": "Swimming", "starts_in_mins": 45, "location": null}]`
  - `test_parse_ha_calendar_all_day_event` — all-day event with `"start": {"date": "2026-03-15"}` parsed correctly
  - `test_parse_ha_calendar_past_event_filtered` — events that have already ended are excluded
  - `test_parse_ha_calendar_empty` — empty list returns empty
  - `test_format_next_event_for_prompt` — `{"title": "Swimming", "starts_in_mins": 45}` → `"Next event: Swimming in 45 minutes"`
  - `test_format_next_event_happening_now` — `starts_in_mins: -10` (started 10 min ago, still running) → `"Happening now: Swimming (started 10 minutes ago)"`

- [ ] **Step 2: Run tests — expect FAIL**

  Run: `python -m pytest tests/test_mind_utils.py -v -k "calendar"`

- [ ] **Step 3: Add constants and fetch function to px-mind**

  Near the existing HA constants (~line 115-120), add:
  ```python
  HA_CALENDARS = [
      "calendar.obiwedd_gmail_com",   # Obi
      "calendar.calendar",            # Family/Adrian
  ]
  HA_CALENDAR_INTERVAL_S = 300   # refresh every 5 min
  HA_CALENDAR_HORIZON_H  = 8    # look ahead 8 hours
  ```

  After `_fetch_ha_presence()` (~line 878), add:
  ```python
  def _fetch_ha_calendar(dry: bool = False) -> list[dict] | None:
      """Fetch upcoming events from HA calendar entities.
      Returns list of {title, starts_in_mins, location, calendar} or None.
      """
      if dry or not HA_TOKEN:
          return None
      headers = {"Authorization": f"Bearer {HA_TOKEN}", "Accept": "application/json"}
      now = dt.datetime.now(dt.timezone.utc)
      start = now.isoformat()
      end = (now + dt.timedelta(hours=HA_CALENDAR_HORIZON_H)).isoformat()
      events = []
      for cal_id in HA_CALENDARS:
          url = f"{HA_HOST}/api/calendars/{cal_id}?start={start}&end={end}"
          try:
              req = urllib.request.Request(url, headers=headers)
              with urllib.request.urlopen(req, timeout=HA_TIMEOUT_S) as r:
                  raw = json.loads(r.read())
              events.extend(_parse_calendar_events(raw, cal_id, now))
          except Exception as exc:
              log(f"ha_calendar: {cal_id} fetch failed: {exc}")
      events.sort(key=lambda e: e["starts_in_mins"])
      return events if events else None

  def _parse_calendar_events(raw: list, cal_id: str, now: dt.datetime) -> list[dict]:
      """Parse HA calendar API response into simplified event dicts."""
      results = []
      for ev in raw:
          if not isinstance(ev, dict):
              continue
          title = ev.get("summary", "Something")
          location = ev.get("location") or None
          # Parse start time (dateTime for timed events, date for all-day)
          start_raw = ev.get("start", {})
          if "dateTime" in start_raw:
              start_dt = dt.datetime.fromisoformat(start_raw["dateTime"])
          elif "date" in start_raw:
              start_dt = dt.datetime.fromisoformat(start_raw["date"] + "T00:00:00").replace(
                  tzinfo=dt.timezone.utc)
          else:
              continue
          # Parse end time
          end_raw = ev.get("end", {})
          if "dateTime" in end_raw:
              end_dt = dt.datetime.fromisoformat(end_raw["dateTime"])
          elif "date" in end_raw:
              end_dt = dt.datetime.fromisoformat(end_raw["date"] + "T23:59:59").replace(
                  tzinfo=dt.timezone.utc)
          else:
              end_dt = start_dt + dt.timedelta(hours=1)
          # Skip events that have already ended
          if end_dt.astimezone(dt.timezone.utc) < now:
              continue
          starts_in = (start_dt.astimezone(dt.timezone.utc) - now).total_seconds() / 60
          results.append({
              "title": title,
              "starts_in_mins": round(starts_in),
              "location": location,
              "calendar": cal_id.split(".")[-1],
          })
      return results

  def _format_calendar_context(events: list[dict]) -> str:
      """Format calendar events for the reflection prompt."""
      if not events:
          return ""
      lines = []
      for ev in events[:3]:  # max 3 events in context
          mins = ev["starts_in_mins"]
          loc = f" at {ev['location']}" if ev.get("location") else ""
          if mins <= 0:
              lines.append(f"Happening now: {ev['title']}{loc} (started {abs(mins)} minutes ago)")
          elif mins < 60:
              lines.append(f"Coming up: {ev['title']}{loc} in {mins} minutes")
          else:
              hours = mins // 60
              lines.append(f"Later: {ev['title']}{loc} in {hours} hour{'s' if hours > 1 else ''}")
      return "Upcoming events:\n" + "\n".join(lines)
  ```

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

  ```
  git add bin/px-mind tests/test_mind_utils.py
  git commit -m "feat(mind): HA calendar fetch with event parsing and formatting"
  ```

---

### Task 2: Awareness Integration and Prompt Injection

**Files:**
- Modify: `bin/px-mind:~1248-1260` (awareness_tick, add calendar fetch)
- Modify: `bin/px-mind:~1335-1340` (awareness dict enrichment)
- Modify: `bin/px-mind:~1910-1920` (reflection prompt context)
- Modify: `tests/test_mind_utils.py`

- [ ] **Step 1: Write integration tests**

  - `test_awareness_includes_calendar` — mock `_fetch_ha_calendar` returning events, verify awareness dict has `next_event` key
  - `test_reflection_prompt_includes_calendar` — verify calendar context appears in reflection prompt when events are present
  - `test_reflection_prompt_no_calendar_when_empty` — no events → no calendar context in prompt

- [ ] **Step 2: Run tests — expect FAIL**

- [ ] **Step 3: Add calendar cache and awareness enrichment**

  Add cache variables near the existing HA cache (~line 1210):
  ```python
  _cached_ha_calendar: list[dict] | None = None
  _last_ha_calendar_fetch: float = 0.0
  ```

  In `awareness_tick()`, after the HA presence refresh block (~line 1259), add:
  ```python
  # Refresh HA calendar periodically
  if (now_mono - _last_ha_calendar_fetch) > HA_CALENDAR_INTERVAL_S:
      try:
          cal = _fetch_ha_calendar(dry)
          if cal is not None:
              _cached_ha_calendar = cal
      except Exception as exc:
          log(f"ha_calendar fetch failed: {exc}")
      _last_ha_calendar_fetch = now_mono
  ```

  In the awareness dict enrichment section (~line 1337), after `awareness["ha_presence"]`, add:
  ```python
  # Enrich with HA calendar (upcoming events)
  if _cached_ha_calendar:
      awareness["ha_calendar"] = _cached_ha_calendar
      # Convenience: first upcoming event for quick access
      upcoming = [e for e in _cached_ha_calendar if e["starts_in_mins"] >= -30]
      if upcoming:
          awareness["next_event"] = upcoming[0]
  ```

- [ ] **Step 4: Add calendar context to reflection prompt**

  In the reflection prompt builder (~line 1915), after the "Who's home" block, add:
  ```python
  # Calendar awareness
  cal_events = awareness.get("ha_calendar")
  if cal_events:
      cal_ctx = _format_calendar_context(cal_events)
      if cal_ctx:
          context_parts.append(cal_ctx)
  ```

- [ ] **Step 5: Run tests — expect PASS**

- [ ] **Step 6: Run full test suite**

  Run: `python -m pytest -q`

- [ ] **Step 7: Commit**

  ```
  git add bin/px-mind tests/test_mind_utils.py
  git commit -m "feat(mind): inject HA calendar events into awareness + reflection prompt

  Closes #60"
  ```
