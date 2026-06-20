"""Pure helpers for the wake-word listener (unit-testable without audio)."""
import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def effective_onset_rms(sleep_mode: bool) -> int:
    """Speech-onset RMS threshold; lower (more sensitive) when asleep for whisper wake."""
    if sleep_mode:
        return _int_env("PX_WHISPER_ONSET_RMS", 450)
    return _int_env("PX_SPEECH_ONSET_RMS", 800)
