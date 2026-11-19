"""Loading MAME-style ROM sets (zip archives, folders or single files)."""

from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass, field

MAX_MEMBER_SIZE = 256 * 1024 * 1024  # refuse absurd archive members


def natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


@dataclass
class RomSet:
    path: str
    files: dict = field(default_factory=dict)  # name -> bytes, natural order

    @property
    def name(self) -> str:
        base = os.path.basename(self.path.rstrip("\\/"))
        return os.path.splitext(base)[0] if os.path.isfile(self.path) else base

    @classmethod
    def load(cls, path: str) -> "RomSet":
        files = {}
        if os.path.isdir(path):
            for entry in os.listdir(path):
                full = os.path.join(path, entry)
                if os.path.isfile(full) and os.path.getsize(full) <= MAX_MEMBER_SIZE:
                    with open(full, "rb") as fh:
                        files[entry] = fh.read()
        elif zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if info.is_dir() or info.file_size > MAX_MEMBER_SIZE:
                        continue
                    files[info.filename.replace("\\", "/").split("/")[-1]] = zf.read(info)
        elif os.path.isfile(path):
            with open(path, "rb") as fh:
                files[os.path.basename(path)] = fh.read()
        else:
            raise FileNotFoundError(path)
        if not files:
            raise ValueError("No ROM files found in %s" % path)
        ordered = {k: files[k] for k in sorted(files, key=natural_key)}
        return cls(path=path, files=ordered)


def combine(romset: RomSet, names, mode: str = "concat", byteswap: bool = False) -> bytes:
    """Build one sound-ROM image from several ROM files.

    ``mode`` is ``concat`` (files back to back, the usual layout) or
    ``interleave`` (files hold alternating bytes, e.g. even/odd chips).
    ``byteswap`` swaps each 16-bit word afterwards (MAME's LOAD16_WORD_SWAP).
    """
    parts = [romset.files[n] for n in names]
    if not parts:
        return b""
    if mode == "interleave" and len(parts) > 1:
        size = min(len(p) for p in parts)
        out = bytearray(size * len(parts))
        for i, p in enumerate(parts):
            out[i::len(parts)] = p[:size]
        data = bytes(out)
    else:
        data = b"".join(parts)
    if byteswap:
        buf = bytearray(data[: len(data) & ~1])
        buf[0::2], buf[1::2] = buf[1::2], buf[0::2]
        data = bytes(buf) + data[len(buf):]
    return data
