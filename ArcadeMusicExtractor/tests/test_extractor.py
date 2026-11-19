import io
import math
import os
import tempfile
import unittest
import zipfile
from array import array

from arcade_music_extractor import codecs
from arcade_music_extractor.extract import (Settings, detect, export_segments, find_oki_phrases,
                                            find_segments, split_by_padding)
from arcade_music_extractor.romset import RomSet, combine
from arcade_music_extractor.wavtools import find_sound_spans, read_wav, write_wav


def sine(n, period=40, amp=1500):
    return [int(amp * math.sin(2 * math.pi * i / period)) for i in range(n)]


def oki_encode(samples):
    """Reference OKI ADPCM encoder (mirrors the decoder's state machine)."""
    diff, nxt = codecs._OKI_DIFF, codecs._OKI_NEXT
    sig = st = 0
    nibbles = []
    for target in samples:
        best = min(range(16), key=lambda n: abs(max(-2048, min(2047, sig + diff[st + n])) - target))
        sig = max(-2048, min(2047, sig + diff[st + best]))
        st = nxt[st + best]
        nibbles.append(best)
    if len(nibbles) % 2:
        nibbles.append(0)
    return bytes((nibbles[i] << 4) | nibbles[i + 1] for i in range(0, len(nibbles), 2))


def build_oki_rom(clips, size=0x40000):
    rom = bytearray(size)
    pos = 0x400
    for i, clip in enumerate(clips, 1):
        data = oki_encode(clip)
        start, end = pos, pos + len(data) - 1
        rom[i * 8:i * 8 + 6] = start.to_bytes(3, "big") + end.to_bytes(3, "big")
        rom[start:start + len(data)] = data
        pos = (end + 0x100) & ~0xFF
    return bytes(rom)


class CodecTests(unittest.TestCase):
    def test_oki_roundtrip(self):
        src = sine(2000)
        out = codecs.decode_oki(oki_encode(src))
        err = sum(abs((o >> 4) - s) for o, s in zip(out, src)) / len(src)
        self.assertLess(err, 60)

    def test_oki_nibble_order(self):
        data = bytes([0x70])
        self.assertNotEqual(list(codecs.decode_oki(data, True)), list(codecs.decode_oki(data, False)))

    def test_adpcma_wraps_in_12_bits(self):
        out = codecs.decode_adpcm_a(bytes([0x77]) * 200)
        self.assertTrue(all(-2048 * 16 <= s < 2048 * 16 for s in out))
        self.assertTrue(any(s < 0 for s in out))  # wrapped around

    def test_delta_t_silence_and_saturation(self):
        self.assertEqual(len(codecs.decode_adpcm_b(b"\x00" * 10)), 20)
        loud = codecs.decode_adpcm_b(bytes([0x77]) * 100)
        self.assertEqual(loud[-1], 32767)
        loud = codecs.decode_ymz280b(bytes([0xFF]) * 100)
        self.assertEqual(loud[-1], -32768)

    def test_pcm(self):
        self.assertEqual(list(codecs.decode_pcm8_signed(bytes([0x7F, 0x80, 0]))), [32512, -32768, 0])
        self.assertEqual(list(codecs.decode_pcm8_unsigned(bytes([0x80, 0xFF]))), [0, 32512])
        self.assertEqual(list(codecs.decode_pcm16_be(b"\x12\x34")), [0x1234])
        self.assertEqual(list(codecs.decode_pcm16_le(b"\x12\x34")), [0x3412])


class ExtractTests(unittest.TestCase):
    def test_oki_phrase_table(self):
        rom = build_oki_rom([sine(3000), sine(5000, 25), sine(1200, 60)])
        segs = find_oki_phrases(rom)
        self.assertEqual(len(segs), 3)
        self.assertEqual(segs[0].offset, 0x400)
        self.assertEqual(segs[1].length, 2500)

    def test_random_data_has_no_table(self):
        import random
        rnd = random.Random(1)
        self.assertEqual(find_oki_phrases(bytes(rnd.randrange(256) for _ in range(0x2000))), [])

    def test_split_by_padding(self):
        data = b"\x12\x34" * 300 + b"\x80" * 1000 + b"\x56\x78" * 400 + b"\xff" * 2000
        segs = split_by_padding(data, min_run=512)
        self.assertEqual([(s.offset, s.length) for s in segs], [(0, 600), (1600, 800)])

    def test_detect_and_export_oki_zip(self):
        rom = build_oki_rom([sine(4000), sine(4000, 30)], size=0x40000)
        with tempfile.TemporaryDirectory() as tmp:
            zpath = os.path.join(tmp, "game.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("prog.bin", b"\x4e\x75" * 1000)
                zf.writestr("snd1.bin", rom[:0x20000])
                zf.writestr("snd2.bin", rom[0x20000:])
            rs = RomSet.load(zpath)
            plans = detect(rs)
            self.assertEqual(plans[0].settings.fmt, "oki")
            self.assertEqual(plans[0].files, ["snd1.bin"])  # table only reaches 0x400+4000
            data = combine(rs, plans[0].files)
            segs = find_segments(data, plans[0].settings)
            files = export_segments(data, segs, plans[0].settings, os.path.join(tmp, "out"))
            self.assertEqual(len(files), 2)
            samples, rate, ch = read_wav(files[0])
            self.assertEqual((rate, ch, len(samples)), (7575, 1, 4000))

    def test_detect_neogeo_and_cps2(self):
        rs = RomSet("x", {"201-p1.p1": b"\0", "201-v1.v1": b"\0", "201-v2.v2": b"\0"})
        p = detect(rs)[0]
        self.assertEqual((p.settings.fmt, p.files), ("adpcma", ["201-v1.v1", "201-v2.v2"]))
        rs = RomSet("x", {"001-v11.v11": b"\0", "001-v21.v21": b"\0"})
        p = detect(rs)[0]
        self.assertEqual(p.files, ["001-v11.v11"])
        self.assertEqual(p.extra[0].settings.fmt, "adpcmb")
        rs = RomSet("x", {"sfa.11m": b"\0", "sfa.12m": b"\0", "sfa.03": b"\0"})
        p = detect(rs)[0]
        self.assertTrue(p.byteswap)
        self.assertEqual(p.settings.fmt, "pcm8s")

    def test_combine_modes(self):
        rs = RomSet("x", {"a": b"\x01\x03", "b": b"\x02\x04"})
        self.assertEqual(combine(rs, ["a", "b"], "interleave"), b"\x01\x02\x03\x04")
        self.assertEqual(combine(rs, ["a", "b"], "concat", byteswap=True), b"\x03\x01\x04\x02")


class WavTests(unittest.TestCase):
    def test_find_sound_spans(self):
        rate = 8000
        tone = array("h", sine(rate * 3, amp=10000))
        gap = array("h", bytes(2 * rate * 2))
        spans = find_sound_spans(gap + tone + gap + tone + gap, rate, min_silence=1.0, min_length=1.0)
        self.assertEqual(len(spans), 2)
        self.assertAlmostEqual(spans[0][0] / rate, 2.0, delta=0.05)

    def test_wav_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.wav")
            write_wav(p, array("h", [1, -2, 3]), 22050)
            self.assertEqual(read_wav(p), (array("h", [1, -2, 3]), 22050, 1))


if __name__ == "__main__":
    unittest.main()
