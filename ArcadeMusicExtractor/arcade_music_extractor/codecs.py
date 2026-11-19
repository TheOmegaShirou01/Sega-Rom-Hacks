"""Audio decoders for the sample formats used by arcade sound chips.

Every decoder takes raw ROM bytes and returns a mono ``array('h')`` of signed
16-bit samples.  The algorithms follow the behaviour of the real chips (as
documented by MAME's implementations) so the output matches what the hardware
would play.
"""

from __future__ import annotations

import sys
from array import array
from dataclasses import dataclass
from typing import Callable

# Step-size table shared by the OKI MSM5205/MSM6295 and the YM2610 ADPCM-A unit.
# Equal to floor(16 * 1.1 ** n) for n in 0..48.
_OKI_STEPS = (
    16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88,
    97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371,
    408, 449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411,
    1552,
)
_OKI_INDEX_SHIFT = (-1, -1, -1, -1, 2, 4, 6, 8)
_ADPCMA_INDEX_SHIFT = (-1, -1, -1, -1, 2, 5, 7, 9)


def _build_tables(index_shift, diff_fn):
    """Flatten (step index, nibble) -> (difference, next state) lookup tables.

    States are stored pre-multiplied by 16 so the decode loop only needs one
    addition to form the table index.
    """
    diff = [0] * (49 * 16)
    nxt = [0] * (49 * 16)
    for idx, step in enumerate(_OKI_STEPS):
        for nib in range(16):
            d = diff_fn(step, nib)
            diff[idx * 16 + nib] = -d if nib & 8 else d
            nidx = min(48, max(0, idx + index_shift[nib & 7]))
            nxt[idx * 16 + nib] = nidx * 16
    return diff, nxt


def _oki_diff(step, nib):
    # MAME okiadpcm: bit-weighted sum, each term rounded down individually.
    return ((step if nib & 4 else 0) + (step >> 1 if nib & 2 else 0)
            + (step >> 2 if nib & 1 else 0) + (step >> 3))


def _jedi_diff(step, nib):
    # YM2610 ADPCM-A ("jedi table"): (2n + 1) * step / 8.
    return ((2 * (nib & 7) + 1) * step) >> 3


_OKI_DIFF, _OKI_NEXT = _build_tables(_OKI_INDEX_SHIFT, _oki_diff)
_ADPCMA_DIFF, _ADPCMA_NEXT = _build_tables(_ADPCMA_INDEX_SHIFT, _jedi_diff)


def _nibble_pairs(high_first: bool):
    """Per-byte (first, second) nibble lookup to keep the hot loop tight."""
    if high_first:
        return [(b >> 4, b & 15) for b in range(256)]
    return [(b & 15, b >> 4) for b in range(256)]


def decode_oki(data: bytes, high_first: bool = True) -> array:
    """OKI MSM6295 / MSM5205 4-bit ADPCM (12-bit output, saturating)."""
    diff, nxt, pairs = _OKI_DIFF, _OKI_NEXT, _nibble_pairs(high_first)
    out = array("h", bytes(4 * len(data)))
    sig = st = o = 0
    for b in data:
        n1, n2 = pairs[b]
        sig += diff[st + n1]
        st = nxt[st + n1]
        if sig > 2047:
            sig = 2047
        elif sig < -2048:
            sig = -2048
        out[o] = sig << 4
        sig += diff[st + n2]
        st = nxt[st + n2]
        if sig > 2047:
            sig = 2047
        elif sig < -2048:
            sig = -2048
        out[o + 1] = sig << 4
        o += 2
    return out


def decode_adpcm_a(data: bytes, high_first: bool = True) -> array:
    """Yamaha YM2610/YM2608 ADPCM-A (Neo Geo). 12-bit accumulator that wraps."""
    diff, nxt, pairs = _ADPCMA_DIFF, _ADPCMA_NEXT, _nibble_pairs(high_first)
    out = array("h", bytes(4 * len(data)))
    acc = st = o = 0
    for b in data:
        n1, n2 = pairs[b]
        acc = ((acc + diff[st + n1] + 2048) & 0xFFF) - 2048
        st = nxt[st + n1]
        out[o] = acc << 4
        acc = ((acc + diff[st + n2] + 2048) & 0xFFF) - 2048
        st = nxt[st + n2]
        out[o + 1] = acc << 4
        o += 2
    return out


def _decode_yamaha_delta(data: bytes, scale, shift, lo, hi, high_first) -> array:
    """Yamaha delta-T style ADPCM: adaptive step multiplied every nibble."""
    pairs = _nibble_pairs(high_first)
    out = array("h", bytes(4 * len(data)))
    acc = o = 0
    step = lo
    for b in data:
        for n in pairs[b]:
            mag = ((2 * (n & 7) + 1) * step) >> 3
            acc = acc - mag if n & 8 else acc + mag
            if acc > 32767:
                acc = 32767
            elif acc < -32768:
                acc = -32768
            out[o] = acc
            o += 1
            step = (step * scale[n & 7]) >> shift
            if step < lo:
                step = lo
            elif step > hi:
                step = hi
    return out


def decode_adpcm_b(data: bytes, high_first: bool = True) -> array:
    """Yamaha ADPCM-B / Delta-T (YM2610 ADPCM-B, YM2608, Y8950, YM2612 ext.)."""
    return _decode_yamaha_delta(data, (57, 57, 57, 57, 77, 102, 128, 153),
                                6, 127, 24576, high_first)


def decode_ymz280b(data: bytes, high_first: bool = True) -> array:
    """Yamaha YMZ280B 4-bit ADPCM (Cave, Psikyo, Kaneko, etc.)."""
    return _decode_yamaha_delta(data, (230, 230, 230, 230, 307, 409, 512, 614),
                                8, 0x7F, 0x6000, high_first)


def decode_pcm8_signed(data: bytes, high_first: bool = True) -> array:
    """8-bit two's complement PCM (QSound, Konami K054539/K007232-style)."""
    table = [((b ^ 0x80) - 128) << 8 for b in range(256)]
    return array("h", [table[b] for b in data])


def decode_pcm8_unsigned(data: bytes, high_first: bool = True) -> array:
    """8-bit offset-binary PCM (SegaPCM, Namco C140 8-bit, etc.)."""
    table = [(b - 128) << 8 for b in range(256)]
    return array("h", [table[b] for b in data])


def _pcm16(data: bytes, big_endian: bool) -> array:
    out = array("h")
    out.frombytes(data[: len(data) & ~1])
    if big_endian != (sys.byteorder == "big"):
        out.byteswap()
    return out


def decode_pcm16_le(data: bytes, high_first: bool = True) -> array:
    return _pcm16(data, big_endian=False)


def decode_pcm16_be(data: bytes, high_first: bool = True) -> array:
    return _pcm16(data, big_endian=True)


@dataclass(frozen=True)
class AudioFormat:
    key: str
    label: str
    decoder: Callable[[bytes, bool], array]
    default_rate: int
    samples_per_byte: float
    examples: str

    def decode(self, data: bytes, high_first: bool = True) -> array:
        return self.decoder(data, high_first)

    def duration(self, nbytes: int, rate: int) -> float:
        return nbytes * self.samples_per_byte / float(rate or 1)


FORMATS = {f.key: f for f in (
    AudioFormat("oki", "OKI MSM6295 / MSM5205 ADPCM", decode_oki, 7575, 2,
                "CPS1, Toaplan, Video System, Kaneko, NMK, Afega, many Korean/Taiwanese boards"),
    AudioFormat("adpcma", "Yamaha ADPCM-A (YM2610 / YM2608)", decode_adpcm_a, 18518, 2,
                "Neo Geo MVS (V ROMs), Taito F2/B System"),
    AudioFormat("adpcmb", "Yamaha ADPCM-B / Delta-T", decode_adpcm_b, 22050, 2,
                "Neo Geo ADPCM-B (V2x ROMs), YM2608, Y8950"),
    AudioFormat("ymz280b", "Yamaha YMZ280B ADPCM", decode_ymz280b, 16000, 2,
                "Cave (DoDonPachi, ESP Ra.De.), Psikyo, Kaneko"),
    AudioFormat("pcm8s", "8-bit signed PCM", decode_pcm8_signed, 24000, 1,
                "CPS2 QSound, Konami K054539"),
    AudioFormat("pcm8u", "8-bit unsigned PCM", decode_pcm8_unsigned, 15625, 1,
                "Sega System 16 / OutRun SegaPCM, Namco"),
    AudioFormat("pcm16le", "16-bit signed PCM (little endian)", decode_pcm16_le, 22050, 0.5,
                "Generic"),
    AudioFormat("pcm16be", "16-bit signed PCM (big endian)", decode_pcm16_be, 22050, 0.5,
                "Generic, 68000-based boards"),
)}


def normalize(samples: array, target: float = 0.95) -> array:
    """Scale samples so the loudest peak reaches ``target`` of full scale."""
    if not samples:
        return samples
    peak = max(max(samples), -min(samples))
    if peak == 0:
        return samples
    gain = (32767 * target) / peak
    if 0.98 < gain < 1.02:
        return samples
    return array("h", [max(-32768, min(32767, int(s * gain))) for s in samples])
