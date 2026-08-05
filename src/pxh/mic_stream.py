"""arecord-backed microphone capture for the wake-word listener.

Why this exists
---------------
PyAudio/PortAudio's ALSA backend is broken on the C-Media USB mic fitted to
SPARK: opened at 44100 Hz it delivers only ~29,900 samples/sec, because it sits
in a permanent overrun-recovery loop (~7 DROP+PREPARE+START cycles per second,
visible under strace), discarding buffered audio on every cycle. With
``exception_on_overflow=False`` — which the listener must pass, or a single
overrun kills the daemon — roughly a third of every utterance is silently
spliced out. There is no clipping, no run of zeros and no envelope anomaly, so
the damage is invisible to every offline metric; only listening reveals it.

``arecord`` on the *identical* device is clean: a chirp-train loopback through
SPARK's own speaker recovered 18/18 chirps with max 2.6 ms timing deviation,
versus 13/18 and seconds of timeline compression through PyAudio.

So capture goes through an ``arecord`` subprocess emitting raw PCM on stdout.

Ring-buffer semantics
---------------------
A reader thread drains the pipe continuously into a bounded deque. This is what
makes the swap safe: the listener stops reading for many seconds at a time (STT,
then an LLM call), and a 64 KB pipe holds only ~0.37 s at 44.1 kHz. Without the
drain thread arecord would block on write, overrun its own ALSA buffer, and we
would have rebuilt the very failure we are fixing. Drops are counted and logged
rather than silent — the previous bug was invisible precisely because nothing
counted them.

The ``read(frames, exception_on_overflow=...)`` signature deliberately mirrors
``pyaudio.Stream.read`` so the listener's call sites are unchanged.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import threading
import time
from collections import deque

DEFAULT_RATE = 44100
DEFAULT_CHANNELS = 1
DEFAULT_CHUNK_FRAMES = 2048
DEFAULT_BUFFER_S = 10.0
DEFAULT_READ_TIMEOUT_S = 5.0

# A drop is only audio *loss* if the listener meant to be recording at the
# time. It deliberately stops reading for seconds during STT and the LLM call,
# and again while SPARK speaks; the ring overflowing then discards audio nobody
# wanted, and `flush()` would have thrown it away regardless. Counting those
# the same as a mid-utterance drop is what let `dropped_chunks` climb into the
# hundreds of thousands while capture was in fact healthy — saturating the one
# signal that was supposed to make dropped audio visible.
#
# The stream cannot infer intent: because a blocked reader drains the deque,
# an overflow *always* means nobody was inside read(). Only the caller knows
# whether that gap was a planned pause or a stall in the middle of an
# utterance, so it declares it via `capturing()`.

_CARD_RE = re.compile(
    r"^card (?P<card>\d+): (?P<short>\S+) \[(?P<long>[^\]]*)\], "
    r"device (?P<dev>\d+): (?P<rest>.*)$"
)


def parse_arecord_devices(listing: str) -> list[tuple[int, int, str]]:
    """Parse ``arecord -l`` output into (card, device, description) tuples."""
    out: list[tuple[int, int, str]] = []
    for line in listing.splitlines():
        m = _CARD_RE.match(line.strip())
        if not m:
            continue
        desc = f"{m['short']} [{m['long']}] {m['rest']}".strip()
        out.append((int(m["card"]), int(m["dev"]), desc))
    return out


def resolve_arecord_device(preferred: str = "USB", listing: str | None = None) -> str:
    """Return an ALSA device string (``plughw:C,D``) for the best capture device.

    ``plughw`` rather than ``hw`` so ALSA handles any rate/format conversion the
    device cannot do natively. Falls back to ``default`` when nothing matches,
    which at least gets audio rather than failing the daemon outright.
    """
    if listing is None:
        try:
            proc = subprocess.run(
                ["arecord", "-l"], capture_output=True, check=False, timeout=10
            )
            listing = proc.stdout.decode(errors="replace")
        except (OSError, subprocess.SubprocessError):
            return "default"

    devices = parse_arecord_devices(listing)
    if not devices:
        return "default"

    if preferred:
        needle = preferred.lower()
        for card, dev, desc in devices:
            if needle in desc.lower():
                return f"plughw:{card},{dev}"

    card, dev, _ = devices[0]
    return f"plughw:{card},{dev}"


class ArecordStream:
    """Blocking PCM source backed by an ``arecord`` subprocess.

    Drop-in for the subset of ``pyaudio.Stream`` the wake listener uses:
    ``read``, ``start_stream``, ``stop_stream``, ``close``.
    """

    def __init__(
        self,
        device: str = "default",
        rate: int = DEFAULT_RATE,
        channels: int = DEFAULT_CHANNELS,
        chunk_frames: int = DEFAULT_CHUNK_FRAMES,
        buffer_s: float = DEFAULT_BUFFER_S,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        log=None,
        command: list[str] | None = None,
        max_restarts: int = 5,
    ):
        self.device = device
        self.rate = rate
        self.channels = channels
        self.chunk_frames = chunk_frames
        self.sample_width = 2  # S16_LE
        self.chunk_bytes = chunk_frames * channels * self.sample_width
        self.read_timeout_s = read_timeout_s
        self._log = log or (lambda _msg: None)
        self._command = command
        self._max_restarts = max_restarts

        maxlen = max(2, int(buffer_s * rate / chunk_frames))
        self._buf: deque[bytes] = deque(maxlen=maxlen)
        self._leftover = b""
        self._cond = threading.Condition()
        self._proc: subprocess.Popen | None = None
        self._threads: list[threading.Thread] = []
        self._closing = False
        self._dead = False
        self._dead_reason = ""
        self.dropped_chunks = 0      # total, kept for back-compat
        self.dropped_active = 0      # dropped mid-capture — real audio loss
        self.dropped_idle = 0        # dropped while nobody was reading — benign
        self.restarts = 0
        self._last_drop_log = 0.0
        self._last_idle_drop_log = 0.0
        self._capturing = 0

    # -- lifecycle ---------------------------------------------------------

    def build_command(self) -> list[str]:
        if self._command is not None:
            return list(self._command)
        return [
            "arecord",
            "-D", self.device,
            "-f", "S16_LE",
            "-c", str(self.channels),
            "-r", str(self.rate),
            "-t", "raw",
            "--buffer-size", str(self.chunk_frames * 8),
            "--period-size", str(self.chunk_frames),
            "-q",
        ]

    def start_stream(self) -> None:
        """Spawn arecord and begin draining it. Idempotent."""
        with self._cond:
            if self._proc is not None and self._proc.poll() is None:
                return
        self._spawn()

    def _spawn(self) -> None:
        cmd = self.build_command()
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        with self._cond:
            self._proc = proc
            self._dead = False
            self._dead_reason = ""
        reader = threading.Thread(
            target=self._reader_loop, args=(proc,), daemon=True,
            name="arecord-reader",
        )
        errs = threading.Thread(
            target=self._stderr_loop, args=(proc,), daemon=True,
            name="arecord-stderr",
        )
        self._threads = [reader, errs]
        reader.start()
        errs.start()

    def _reader_loop(self, proc: subprocess.Popen) -> None:
        stdout = proc.stdout
        assert stdout is not None
        try:
            while True:
                data = stdout.read(self.chunk_bytes)
                if not data:
                    break  # EOF: arecord exited
                with self._cond:
                    if self._closing:
                        return
                    if len(self._buf) == self._buf.maxlen:
                        self.dropped_chunks += 1
                        active = self._capturing > 0
                        if active:
                            self.dropped_active += 1
                        else:
                            self.dropped_idle += 1
                        self._maybe_log_drop(active)
                    self._buf.append(data)
                    self._cond.notify_all()
        except (OSError, ValueError):
            pass  # pipe torn down under us

        if self._closing:
            return
        self._handle_exit(proc)

    def _stderr_loop(self, proc: subprocess.Popen) -> None:
        """Surface arecord's own overrun warnings — this daemon exists because
        dropped audio used to be invisible."""
        stderr = proc.stderr
        if stderr is None:
            return
        try:
            for raw in stderr:
                if self._closing:
                    return
                line = raw.decode(errors="replace").strip()
                if line:
                    self._log(f"arecord: {line}")
        except (OSError, ValueError):
            pass

    def _handle_exit(self, proc: subprocess.Popen) -> None:
        """arecord died unexpectedly: restart it, or mark the stream dead."""
        rc = proc.poll()
        if self.restarts >= self._max_restarts:
            with self._cond:
                self._dead = True
                self._dead_reason = (
                    f"arecord exited (rc={rc}) and restart limit "
                    f"({self._max_restarts}) reached"
                )
                self._cond.notify_all()
            self._log(f"arecord: {self._dead_reason}")
            return
        self.restarts += 1
        self._log(f"arecord exited (rc={rc}) — restart {self.restarts}/{self._max_restarts}")
        time.sleep(min(0.5 * self.restarts, 2.0))
        if self._closing:
            return
        try:
            self._spawn()
        except OSError as exc:
            with self._cond:
                self._dead = True
                self._dead_reason = f"arecord respawn failed: {exc}"
                self._cond.notify_all()
            self._log(self._dead_reason)

    def _maybe_log_drop(self, active: bool) -> None:
        """Log drops, loudly for real loss and rarely for benign backlog.

        Mid-capture drops mean an utterance was clipped and warrant the same
        5s cadence as before. Idle backlog is expected every voice turn, so it
        logs once a minute at most — frequently enough to spot a consumer that
        has genuinely wedged, quietly enough that it no longer drowns out the
        case this counter exists to surface.
        """
        now = time.monotonic()
        if active:
            if now - self._last_drop_log >= 5.0:
                self._last_drop_log = now
                self._log(
                    f"arecord ring buffer full DURING CAPTURE — dropped "
                    f"{self.dropped_active} chunks mid-utterance (audio lost)"
                )
        elif now - self._last_idle_drop_log >= 60.0:
            self._last_idle_drop_log = now
            self._log(
                f"arecord backlog dropped while idle — {self.dropped_idle} "
                f"chunks (expected during STT/LLM/speech; no capture affected)"
            )

    # -- reading -----------------------------------------------------------

    def read(self, num_frames: int, exception_on_overflow: bool = True) -> bytes:
        """Return exactly ``num_frames`` frames, blocking until available.

        ``exception_on_overflow`` is accepted for PyAudio call-site
        compatibility and ignored: overruns are handled by the ring buffer and
        reported via ``dropped_chunks`` and the log.
        """
        del exception_on_overflow  # signature compatibility only
        self.start_stream()
        want = num_frames * self.channels * self.sample_width
        parts: list[bytes] = []
        have = 0

        if self._leftover:
            take = min(want, len(self._leftover))
            parts.append(self._leftover[:take])
            self._leftover = self._leftover[take:]
            have += take

        deadline = time.monotonic() + self.read_timeout_s
        while have < want:
            with self._cond:
                while not self._buf:
                    if self._dead:
                        raise OSError(self._dead_reason or "arecord stream is dead")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise OSError(
                            f"arecord read timed out after {self.read_timeout_s}s "
                            f"(device {self.device})"
                        )
                    self._cond.wait(remaining)
                chunk = self._buf.popleft()
            need = want - have
            if len(chunk) > need:
                parts.append(chunk[:need])
                self._leftover = chunk[need:]
                have += need
            else:
                parts.append(chunk)
                have += len(chunk)

        return b"".join(parts)

    @contextlib.contextmanager
    def capturing(self):
        """Mark a region where dropped audio is real loss, not backlog.

        Wrap utterance recording in this. Inside it, a ring overflow means the
        capture loop stalled long enough to lose speech and is logged loudly;
        outside it, an overflow is the expected backlog from a planned pause
        and is logged quietly. Re-entrant, so nested capture helpers are safe.
        """
        with self._cond:
            self._capturing += 1
        try:
            yield self
        finally:
            with self._cond:
                self._capturing -= 1

    def flush(self) -> int:
        """Discard everything buffered right now; return chunks dropped.

        Used after SPARK speaks. Tighter than the old fixed-count read loop:
        it drops exactly the backlog, so a user who replies immediately after
        the chime is not clipped.
        """
        with self._cond:
            n = len(self._buf)
            self._buf.clear()
        self._leftover = b""
        return n

    # -- teardown ----------------------------------------------------------

    def stop_stream(self) -> None:
        self.close()

    def close(self) -> None:
        self._closing = True
        with self._cond:
            proc = self._proc
            self._cond.notify_all()
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except (OSError, subprocess.SubprocessError):
                try:
                    proc.kill()
                except OSError:
                    pass
        for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        for t in self._threads:
            t.join(timeout=2)
        self._threads = []
        with self._cond:
            self._buf.clear()
        self._leftover = b""

    def __enter__(self):
        self.start_stream()
        return self

    def __exit__(self, *exc):
        self.close()
        return False
