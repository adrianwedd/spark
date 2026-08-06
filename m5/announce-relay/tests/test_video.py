import shutil
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image

from announce_relay import video

# Applied per-test, NOT module-wide: test_missing_ffmpeg_raises_muxerror
# monkeypatches FFMPEG to a nonexistent path and must still run on a host
# without the toolchain — it is the test for exactly that situation.
needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH",
)


def _make_wav(path: Path, seconds: float = 1.0):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"\x00\x00" * int(24000 * seconds))
    return path


def _make_png(path: Path):
    Image.new("RGB", (1280, 800), (26, 24, 28)).save(str(path), "PNG")
    return path


def _probe(path: Path, entries: str) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", entries, "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


@needs_ffmpeg
def test_mux_produces_mp4(tmp_dirs, tmp_path):
    png = _make_png(tmp_path / "c.png")
    wav = _make_wav(tmp_path / "s.wav", 1.0)
    out = video.mux(png, wav)
    assert out.exists() and out.suffix == ".mp4"
    assert out.stat().st_size > 0


@needs_ffmpeg
def test_mux_output_is_yuv420p(tmp_dirs, tmp_path):
    png = _make_png(tmp_path / "c.png")
    wav = _make_wav(tmp_path / "s.wav", 1.0)
    out = video.mux(png, wav)
    assert _probe(out, "stream=pix_fmt") == "yuv420p"


@needs_ffmpeg
def test_mux_duration_is_speech_plus_tail(tmp_dirs, tmp_path):
    png = _make_png(tmp_path / "c.png")
    wav = _make_wav(tmp_path / "s.wav", 2.0)
    out = video.mux(png, wav, tail_s=1.5)
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(out)],
        capture_output=True, text=True, check=True).stdout.strip())
    assert 3.2 < dur < 4.2       # 2.0 speech + 1.5 tail, container slack


def test_missing_ffmpeg_raises_muxerror(tmp_dirs, tmp_path, monkeypatch):
    monkeypatch.setattr(video, "FFMPEG", "/nonexistent/ffmpeg")
    png = _make_png(tmp_path / "c.png")
    wav = _make_wav(tmp_path / "s.wav", 1.0)
    with pytest.raises(video.MuxError):
        video.mux(png, wav)


@needs_ffmpeg
def test_ffmpeg_nonzero_exit_raises_muxerror(tmp_dirs, tmp_path):
    png = _make_png(tmp_path / "c.png")
    bad_wav = tmp_path / "not-audio.wav"
    bad_wav.write_bytes(b"definitely not a wav")
    with pytest.raises(video.MuxError):
        video.mux(png, bad_wav)
