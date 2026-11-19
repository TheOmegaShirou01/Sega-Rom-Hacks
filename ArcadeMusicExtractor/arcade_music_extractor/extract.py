"""Finding and exporting audio inside a combined sound-ROM image."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .codecs import FORMATS, normalize
from .romset import RomSet, natural_key
from .wavtools import write_wav

OKI_BANK = 0x40000  # the MSM6295 addresses 256 KB


@dataclass
class Segment:
    offset: int
    length: int
    label: str

    @property
    def end(self) -> int:
        return self.offset + self.length


@dataclass
class Settings:
    fmt: str = "oki"
    rate: int = 7575
    method: str = "table"        # "table" (OKI phrase table) or "split"
    high_nibble_first: bool = True
    min_run: int = 512           # padding bytes that separate two samples
    min_seconds: float = 0.05    # drop anything shorter than this
    normalize: bool = True

    @property
    def audio(self):
        return FORMATS[self.fmt]


# --------------------------------------------------------------------------
# OKI MSM6295 phrase tables
# --------------------------------------------------------------------------

def parse_oki_table(data: bytes, base: int = 0):
    """Read the 127-entry phrase table at ``base``.

    Returns ``(segments, valid, invalid)``.  Entry 0 is unused by the chip.
    Each entry: 3-byte start, 3-byte end (18-bit addresses), 2 unused bytes.
    """
    segs, valid, invalid = [], 0, 0
    limit = min(OKI_BANK, len(data) - base)
    for phrase in range(1, 128):
        e = data[base + phrase * 8: base + phrase * 8 + 8]
        if len(e) < 8:
            break
        if e[:6] in (b"\x00" * 6, b"\xff" * 6):
            continue
        start = int.from_bytes(e[0:3], "big")
        end = int.from_bytes(e[3:6], "big")
        if e[0] > 3 or e[3] > 3 or not (0x400 <= start < end < limit):
            invalid += 1
            continue
        valid += 1
        segs.append(Segment(base + start, end - start + 1,
                            "phrase %d" % phrase if base == 0
                            else "bank %X phrase %d" % (base // OKI_BANK, phrase)))
    return segs, valid, invalid


def oki_table_looks_valid(valid: int, invalid: int) -> bool:
    return (valid >= 2 and invalid * 4 <= valid) or (valid == 1 and invalid == 0)


def find_oki_phrases(data: bytes):
    """Scan the table at 0 and at every 256 KB bank (for banked boards)."""
    seen, out = set(), []
    for base in range(0, max(1, len(data)), OKI_BANK):
        segs, valid, invalid = parse_oki_table(data, base)
        if not oki_table_looks_valid(valid, invalid):
            continue
        for s in segs:
            if (s.offset, s.length) not in seen:
                seen.add((s.offset, s.length))
                out.append(s)
    return out


def oki_required_size(data: bytes) -> int:
    """Highest address referenced by a valid table at offset 0 (or 0)."""
    segs, valid, invalid = parse_oki_table(data, 0)
    if not oki_table_looks_valid(valid, invalid):
        return 0
    return max(s.end for s in segs)


# --------------------------------------------------------------------------
# Splitting raw sample ROMs
# --------------------------------------------------------------------------

def split_by_padding(data: bytes, min_run: int = 512, min_bytes: int = 64):
    """Cut the ROM wherever ``min_run`` or more identical bytes repeat.

    Sample ROMs pad between samples (and fill unused space) with a constant
    byte such as 00, 80 or FF, so this isolates individual samples/tracks for
    any format without needing to know the sound driver.
    """
    min_run = max(2, int(min_run))
    pattern = re.compile(rb"(.)\1{%d,}" % (min_run - 1), re.S)
    segs, pos = [], 0

    def add(a, b):
        if b - a >= min_bytes:
            segs.append(Segment(a, b - a, "region"))

    for m in pattern.finditer(data):
        add(pos, m.start())
        pos = m.end()
    add(pos, len(data))
    for i, s in enumerate(segs, 1):
        s.label = "sample %d" % i
    return segs


def find_segments(data: bytes, settings: Settings):
    audio = settings.audio
    min_bytes = max(16, int(settings.min_seconds * settings.rate / audio.samples_per_byte))
    if settings.method == "table":
        segs = find_oki_phrases(data)
    elif settings.method == "whole":
        segs = [Segment(0, len(data), "whole ROM")]
    else:
        segs = split_by_padding(data, settings.min_run, min_bytes)
    return [s for s in segs if s.length >= min_bytes or settings.method == "whole"]


def decode_segment(data: bytes, seg: Segment, settings: Settings):
    samples = settings.audio.decode(data[seg.offset:seg.end], settings.high_nibble_first)
    return normalize(samples) if settings.normalize else samples


def export_segments(data: bytes, segs, settings: Settings, out_dir: str,
                    prefix: str = "sample", progress=None):
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for i, seg in enumerate(segs, 1):
        path = os.path.join(out_dir, "%s_%03d_%06X.wav" % (prefix, i, seg.offset))
        write_wav(path, decode_segment(data, seg, settings), settings.rate)
        written.append(path)
        if progress:
            progress(i, len(segs))
    return written


# --------------------------------------------------------------------------
# Auto-detection of common arcade sound-ROM layouts
# --------------------------------------------------------------------------

@dataclass
class Plan:
    files: list
    settings: Settings
    combine: str = "concat"
    byteswap: bool = False
    reason: str = ""
    extra: list = field(default_factory=list)  # further plans (e.g. ADPCM-B)


_NEO_V = re.compile(r"-v(\d)(\d)?\.", re.I)
_CPS2_Q = re.compile(r"\.1[1-4]m$", re.I)
_SOUND_HINT = re.compile(r"snd|sound|oki|pcm|voice|adpcm|samp|music|bgm|\bvo", re.I)


def detect(romset: RomSet):
    """Guess which files hold samples and how to decode them.

    Returns a list of plans (best first); empty if nothing was recognised.
    """
    names = list(romset.files)
    plans = []

    neo = [(n, _NEO_V.search(n)) for n in names]
    neo = [(n, m) for n, m in neo if m]
    if neo:
        two_digit = any(m.group(2) for _, m in neo)
        a_files = [n for n, m in neo if not two_digit or m.group(1) == "1"]
        b_files = [n for n, m in neo if two_digit and m.group(1) == "2"]
        plan = Plan(a_files, Settings("adpcma", 18518, "split", min_run=256),
                    reason="Neo Geo V ROMs (YM2610 ADPCM-A)")
        if b_files:
            plan.extra.append(Plan(b_files, Settings("adpcmb", 22050, "split", min_run=1024),
                                   reason="Neo Geo V2x ROMs (YM2610 ADPCM-B)"))
        plans.append(plan)

    q = [n for n in names if _CPS2_Q.search(n)]
    if q:
        plans.append(Plan(q, Settings("pcm8s", 24000, "split", min_run=512),
                          byteswap=True, reason="CPS2 QSound sample ROMs"))

    for i, n in enumerate(names):
        need = oki_required_size(romset.files[n])
        if not need:
            continue
        files, size = [n], len(romset.files[n])
        j = i + 1
        while size < need and j < len(names):
            files.append(names[j])
            size += len(romset.files[names[j]])
            j += 1
        count = len(find_oki_phrases(b"".join(romset.files[f] for f in files)))
        plans.append(Plan(files, Settings("oki", 7575, "table"),
                          reason="OKI MSM6295 phrase table in %s (%d phrases)" % (n, count)))
        break

    return plans


def guess_sound_files(romset: RomSet):
    """Fallback suggestion when detect() finds nothing: name-based hints."""
    hinted = [n for n in romset.files if _SOUND_HINT.search(n)]
    return sorted(hinted, key=natural_key)
