"""Pure prompt-context formatting for SPARK's cognitive loop."""


def format_routines(routines: dict | None) -> str:
    if not routines:
        return ""
    parts = []
    if routines.get("meds_taken") is False:
        parts.append("Meds not yet taken today")
    elif routines.get("meds_taken") is True:
        parts.append("Meds taken today")
    water = routines.get("water_mins_ago")
    if water is not None:
        if water > 120:
            parts.append(f"Last water was {water // 60} hours ago")
        elif water > 60:
            parts.append("Water about an hour ago")
    return "Routine status: " + ". ".join(parts) if parts else ""


def format_household(ctx: dict | None) -> str:
    if not ctx:
        return ""
    parts = []
    if ctx.get("adrian_on_call"):
        parts.append("Adrian is on a video call — be quiet or whisper")
    elif ctx.get("adrian_mic_active"):
        parts.append("Adrian's microphone is active — be quiet")
    if ctx.get("office_light"):
        parts.append("Office light is on — Adrian is likely working")
    if ctx.get("media_playing"):
        title = ctx.get("media_title", "")
        parts.append(f"Music playing: {title}" if title else "Music is playing")
    return "Household context: " + ". ".join(parts) if parts else ""


def format_calendar(events: list[dict]) -> str:
    if not events:
        return ""
    lines = []
    for event in events[:3]:
        minutes = event["starts_in_mins"]
        title = event["title"]
        location = f" at {event['location']}" if event.get("location") else ""
        if minutes < 0:
            lines.append(
                f"Happening now: {title}{location} "
                f"(started {abs(minutes)} minutes ago)"
            )
        elif minutes < 60:
            lines.append(f"Coming up: {title}{location} in {minutes} minutes")
        else:
            lines.append(f"Later: {title}{location} in {minutes // 60} hours")
    return "\n".join(lines)


def format_introspection(introspection: dict) -> str:
    parts = []
    moods = introspection.get("mood_distribution", {})
    if moods:
        top = sorted(moods.items(), key=lambda item: -item[1])[:5]
        parts.append("Moods: " + ", ".join(
            f"{mood} {percentage:.0f}%" for mood, percentage in top
        ))
    config = introspection.get("config", {})
    if config:
        parts.append("Config: " + ", ".join(
            f"{key}={value}" for key, value in config.items()
        ))
    history = introspection.get("evolve_history", [])
    if history:
        parts.append(f"Evolution history: {len(history)} previous proposals")
    return "\n".join(parts) if parts else "No introspection data available."
