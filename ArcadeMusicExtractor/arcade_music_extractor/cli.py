"""Command-line interface (also handy for batch jobs)."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__, mame
from .codecs import FORMATS
from .extract import Settings, detect, export_segments, find_segments
from .romset import RomSet, combine
from .wavtools import split_wav_file


def _plan_from_args(romset, args):
    if args.format == "auto":
        plans = detect(romset)
        if not plans:
            sys.exit("Could not recognise the sound ROMs; pass --format and --files.")
        flat = []
        for p in plans:
            flat.append(p)
            flat.extend(p.extra)
        return flat
    files = args.files.split(",") if args.files else list(romset.files)
    fmt = FORMATS[args.format]
    method = args.method or ("table" if args.format == "oki" else "split")
    from .extract import Plan
    return [Plan(files, Settings(args.format, args.rate or fmt.default_rate, method,
                                 min_run=args.min_run),
                 combine=args.combine, byteswap=args.byteswap, reason="manual")]


def _apply_overrides(plan, args):
    s = plan.settings
    if args.rate:
        s.rate = args.rate
    if args.method:
        s.method = args.method
    if args.min_run:
        s.min_run = args.min_run
    s.min_seconds = args.min_seconds
    s.normalize = not args.no_normalize
    s.high_nibble_first = not args.low_nibble_first


def cmd_scan(args, export=False):
    romset = RomSet.load(args.rom)
    for n, plan in enumerate(_plan_from_args(romset, args), 1):
        _apply_overrides(plan, args)
        data = combine(romset, plan.files, plan.combine, plan.byteswap)
        segs = find_segments(data, plan.settings)
        s = plan.settings
        print("[%s] %s" % (plan.reason, ", ".join(plan.files)))
        print("    format=%s rate=%d method=%s -> %d segments"
              % (s.fmt, s.rate, s.method, len(segs)))
        if export:
            out = os.path.join(args.out, romset.name)
            written = export_segments(data, segs, s, out, prefix="%s_%s" % (romset.name, s.fmt))
            print("    wrote %d WAV files to %s" % (len(written), out))
        else:
            for i, seg in enumerate(segs, 1):
                print("    %3d  %-22s @%06X  %8d bytes  %6.2fs"
                      % (i, seg.label, seg.offset, seg.length,
                         s.audio.duration(seg.length, s.rate)))


def cmd_split(args):
    written = split_wav_file(args.wav, args.out, threshold_db=args.threshold,
                             min_silence=args.gap, min_length=args.min_length)
    print("wrote %d tracks to %s" % (len(written), args.out))


def cmd_mame(args):
    import shutil
    if not (os.path.exists(args.mame) or shutil.which(args.mame)):
        sys.exit("MAME not found: %s (use --mame to point at mame.exe)" % args.mame)
    wav = os.path.abspath(args.wav or "%s.wav" % args.game)
    cmd = mame.build_command(args.mame, args.game, wav, args.seconds,
                             args.rompath, args.lua or "")
    print(" ".join('"%s"' % c if " " in c else c for c in cmd))
    code = mame.run(cmd, log=print)
    if code != 0 or not os.path.exists(wav):
        sys.exit("MAME exited with code %d" % code)
    if args.split:
        out = os.path.splitext(wav)[0] + "_tracks"
        written = split_wav_file(wav, out)
        print("split into %d tracks in %s" % (len(written), out))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ArcadeMusicExtractor",
                                 description="Extract music and samples from arcade ROMs.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def rom_args(p):
        p.add_argument("rom", help="ROM set (.zip), folder, or single ROM file")
        p.add_argument("--format", default="auto", choices=["auto"] + list(FORMATS))
        p.add_argument("--files", help="comma-separated ROM files in load order")
        p.add_argument("--combine", default="concat", choices=["concat", "interleave"])
        p.add_argument("--byteswap", action="store_true")
        p.add_argument("--method", choices=["table", "split", "whole"])
        p.add_argument("--rate", type=int, help="sample rate in Hz")
        p.add_argument("--min-run", type=int, default=0, help="padding run that separates samples")
        p.add_argument("--min-seconds", type=float, default=0.05)
        p.add_argument("--no-normalize", action="store_true")
        p.add_argument("--low-nibble-first", action="store_true")

    p = sub.add_parser("scan", help="list the samples/tracks found")
    rom_args(p)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("extract", help="export samples/tracks as WAV")
    rom_args(p)
    p.add_argument("-o", "--out", default="extracted")
    p.set_defaults(func=lambda a: cmd_scan(a, export=True))

    p = sub.add_parser("split", help="split a long WAV recording into tracks")
    p.add_argument("wav")
    p.add_argument("-o", "--out", default="tracks")
    p.add_argument("--threshold", type=float, default=-50.0, help="silence level in dBFS")
    p.add_argument("--gap", type=float, default=1.5, help="seconds of silence between tracks")
    p.add_argument("--min-length", type=float, default=2.0)
    p.set_defaults(func=cmd_split)

    p = sub.add_parser("mame", help="record a game's audio with MAME")
    p.add_argument("game", help="MAME short name, e.g. sf2")
    p.add_argument("--mame", default="mame.exe" if os.name == "nt" else "mame")
    p.add_argument("--rompath", default="")
    p.add_argument("--seconds", type=int, default=300)
    p.add_argument("--wav")
    p.add_argument("--lua", help="autoboot Lua script (e.g. a sound-test driver)")
    p.add_argument("--split", action="store_true", help="split the recording into tracks")
    p.set_defaults(func=cmd_mame)

    p = sub.add_parser("lua-template", help="print a sound-test Lua script template")
    p.set_defaults(func=lambda a: print(mame.LUA_TEMPLATE))

    args = ap.parse_args(argv)
    args.func(args)
