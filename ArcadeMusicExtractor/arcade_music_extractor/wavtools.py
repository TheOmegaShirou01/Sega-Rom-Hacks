"""WAV writing and silence-based splitting of recorded audio."""

from __future__ import annotations

import os
import sys
import wave
from array import array


def write_wav(path: str, samples: array, rate: int, channels: int = 1) -> None:
    data = samples
    if sys.byteorder == "big":
        data = array("h", samples)
        data.byteswap()
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(int(rate))
        wf.writeframes(data.tobytes())


def read_wav(path: str):
    """Return (samples, rate, channels) for a 16-bit PCM WAV file."""
    with wave.open(path, "rb") as wf:
        if wf.getsampwidth() != 2:
            raise ValueError("Only 16-bit PCM WAV files are supported")
        rate, channels = wf.getframerate(), wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples, rate, channels


def find_sound_spans(samples: array, rate: int, channels: int = 1,
                     threshold_db: float = -50.0, min_silence: float = 1.5,
                     min_length: float = 2.0, block_ms: float = 20.0):
    """Locate non-silent stretches; returns [(start_frame, end_frame), ...].

    Silence is any run of blocks whose peak stays below ``threshold_db`` for at
    least ``min_silence`` seconds.  Spans shorter than ``min_length`` seconds
    are dropped.
    """
    block = max(1, int(rate * block_ms / 1000.0)) * channels
    limit = int(32767 * (10 ** (threshold_db / 20.0)))
    gap_blocks = max(1, int(min_silence * 1000.0 / block_ms))
    loud = []
    for pos in range(0, len(samples), block):
        chunk = samples[pos:pos + block]
        loud.append(max(max(chunk), -min(chunk)) > limit)

    spans, start, quiet = [], None, 0
    for i, is_loud in enumerate(loud):
        if is_loud:
            if start is None:
                start = i
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet >= gap_blocks:
                spans.append((start, i - quiet + 1))
                start, quiet = None, 0
    if start is not None:
        spans.append((start, len(loud) - quiet))

    frames_per_block = block // channels
    total = len(samples) // channels
    out = []
    for s, e in spans:
        fs, fe = s * frames_per_block, min(total, e * frames_per_block)
        if (fe - fs) / float(rate) >= min_length:
            out.append((fs, fe))
    return out


def split_wav_file(path: str, out_dir: str, **kwargs):
    """Split a recording into tracks; returns the list of files written."""
    samples, rate, channels = read_wav(path)
    spans = find_sound_spans(samples, rate, channels, **kwargs)
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0]
    written = []
    for i, (fs, fe) in enumerate(spans, 1):
        name = os.path.join(out_dir, "%s_track%02d.wav" % (base, i))
        write_wav(name, samples[fs * channels:fe * channels], rate, channels)
        written.append(name)
    return written
