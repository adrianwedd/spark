# Audio and Microphone

**Owns:** capture and playback. `src/pxh/mic_stream.py`, `bin/tool-voice`,
`bin/px-wake-listen`, `bin/px-mic-check`, the espeak/aplay pipeline.

Whether SPARK is *allowed* to speak is
[architecture/policy-and-authority](../architecture/policy-and-authority.md).
This page is about whether the sound is correct.

---

## Invariant

### Capture is `arecord`. Never PyAudio.

PortAudio's ALSA backend is broken on the C-Media USB mic fitted to SPARK.
Opened at 44100 Hz it delivers only ~29,900 samples/sec, sitting in a permanent
overrun-recovery loop (~7 DROP+PREPARE+START cycles/sec under `strace`) and
discarding buffered audio each cycle.

The listener **must** pass `exception_on_overflow=False` or a single overrun
kills the daemon — so roughly a third of every utterance is silently spliced
out.

**There is no clipping, no run of zeros, and no envelope anomaly. Every offline
metric on the recorded WAV looks clean.** Only listening reveals it. This is
why the rule is stated as a prohibition rather than a preference.

`ArecordStream` mirrors `pyaudio.Stream.read/start_stream/close`, so call sites
are unchanged.

### The drain thread is load-bearing, not decoration

A reader thread drains the pipe continuously into a bounded deque. The listener
stops reading for seconds at a time (STT, then an LLM call), and a 64 KB pipe
holds only ~0.37s at 44.1 kHz. Without the drain thread `arecord` would block
on write and overrun its own ALSA buffer — rebuilding the exact bug being
fixed.

**Drops are counted and logged (`dropped_chunks`), never silent.** The original
failure was invisible precisely because nothing counted them.

### `bin/px-mic-check` is the regression test

Chirp-train loopback through SPARK's own speaker. Healthy: **18/18 chirps,
≤3 ms deviation, 0 drops.** The broken PyAudio path scored 13/18 with the
timeline compressed by seconds.

Needs the mic free — `sudo systemctl stop px-wake-listen` first.

### Root must set `PULSE_SERVER`, or audio silently fails

Speech path: `espeak --stdout` → WAV bytes → `aplay -D pulse` → PulseAudio →
HifiBerry DAC → speaker.

When a script runs as **root** (`px-perform`, `tool-voice`) it must set
`PULSE_SERVER=unix:/run/user/1000/pulse/native` in the `aplay` subprocess
environment. Root's `XDG_RUNTIME_DIR=/run/user/0` cannot find the pi-user
socket, and **`aplay` exits 0 while playing nothing.**

### `robot_hat.enable_speaker()` before any audio

It toggles GPIO 20 for the MAX98357A amplifier. Skip it and `aplay` again exits
0 with silence.

### PulseAudio holds the DAC exclusively

`aplay -D robothat` (ALSA bypass) fails "device busy". Route through PulseAudio.

### Whisper anti-hallucination settings are not tuning knobs

`temperature=0`, `condition_on_previous_text=False`, `no_speech_threshold=0.6`.
Post-filters reject: non-ASCII dominant, phantom phrases, repetitive text.

### Do not add `bpe_model` to `load_stt_model()`

The installed sherpa-onnx does not support the kwarg.

STT priority chain: SenseVoice (primary, ~5s) → faster-whisper (best AU accent)
→ sherpa-onnx Zipformer → Vosk (wake-word grammar only). Models are gitignored
and must be downloaded separately.

---

## Why it looks like this

*History, not rule.*

Two separate silent-failure classes shaped this page, and both share a
signature: **the tool reports success.**

PyAudio's overrun loop produced WAV files that passed every automated check.
The bug survived because everyone was measuring the recording rather than
listening to it. A chirp-train loopback was the first test that could see it,
which is why `px-mic-check` exists as a *timing* test rather than a
signal-quality one.

The root/PulseAudio failure is the same shape: `aplay` returns 0. Nothing logs
an error. The speaker is simply silent, and the natural conclusion is that the
hardware is broken.
