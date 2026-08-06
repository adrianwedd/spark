"""Mux a still card and a speech WAV into a castable MP4.

Cast devices replace the receiver app on every play_media, so an image and
audio cannot be layered — the only way to show a card while speaking is to
ship one video.
"""
import os
import shutil
import subprocess
import threading
from pathlib import Path

from . import store

FFMPEG = os.environ.get("RELAY_FFMPEG") or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

MUX_TIMEOUT_S = 60

# /card is a sync endpoint, so FastAPI runs it in Starlette's threadpool and two
# simultaneous announcements would otherwise launch two x264 encodes at once,
# starving the Afterwords TTS process on the same box. Bound it to one at a
# time; a still-image encode is ~1s, so queueing costs less than contending.
_mux_gate = threading.Semaphore(1)


class MuxError(Exception):
    """The MP4 could not be produced."""


TAIL_S = 1.5


def mux(png_path: Path, wav_path: Path, tail_s: float = TAIL_S) -> Path:
    """Loop the card for the length of the speech plus a tail. Returns the MP4 path.

    The tail exists because Cast teardown clips the end of a stream; without it
    the last word is routinely lost.
    """
    out = store.private_path_ext("mp4")
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        FFMPEG, "-nostdin", "-y",
        "-loop", "1", "-i", str(png_path),
        "-i", str(wav_path),
        "-af", f"apad=pad_dur={tail_s}",
        "-threads", "2",             # leave cores for TTS synth on the same box
        "-c:v", "libx264", "-tune", "stillimage",
        "-pix_fmt", "yuv420p",       # Chromecast will not decode yuv444p
        "-r", "5",                   # still image; 5fps keeps the file tiny
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",   # moov atom first — Cast starts sooner
        "-shortest",
        str(out),
    ]

    try:
        with _mux_gate:
            proc = subprocess.run(cmd, capture_output=True, timeout=MUX_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise MuxError(f"ffmpeg could not be run: {exc}") from exc

    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        tail = proc.stderr.decode("utf-8", "replace")[-400:] if proc.stderr else ""
        out.unlink(missing_ok=True)
        raise MuxError(f"ffmpeg exited {proc.returncode}: {tail}")

    return out
