"""Tkinter desktop UI for Windows (also runs anywhere Tk is available)."""

from __future__ import annotations

import os
import queue
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__, mame
from .codecs import FORMATS
from .extract import Settings, decode_segment, detect, export_segments, find_segments, guess_sound_files
from .romset import RomSet, combine
from .wavtools import split_wav_file, write_wav

try:
    import winsound
except ImportError:  # not on Windows
    winsound = None

FORMAT_KEYS = list(FORMATS)
FORMAT_LABELS = ["%s  -  %s" % (f.label, f.examples) for f in FORMATS.values()]
METHODS = {"OKI phrase table": "table", "Split on padding": "split", "Whole ROM as one file": "whole"}


class Worker:
    """Runs jobs off the UI thread and marshals callbacks back onto it."""

    def __init__(self, root):
        self.root, self.q, self.busy = root, queue.Queue(), False
        root.after(50, self._poll)

    def run(self, job, done=None, status=None):
        if self.busy:
            messagebox.showinfo("Busy", "Please wait for the current task to finish.")
            return
        self.busy = True

        def target():
            try:
                result = job()
                self.q.put((done, (result,), None))
            except Exception as exc:  # report every failure in the UI
                self.q.put((None, (), exc))
            finally:
                self.q.put(("idle", (), None))

        if status:
            status(True)
        threading.Thread(target=target, daemon=True).start()

    def call(self, fn, *args):
        self.q.put((fn, args, None))

    def _poll(self):
        try:
            while True:
                fn, args, exc = self.q.get_nowait()
                if fn == "idle":
                    self.busy = False
                    self.root.event_generate("<<WorkerIdle>>")
                elif exc is not None:
                    messagebox.showerror("Error", "%s: %s" % (type(exc).__name__, exc))
                elif fn:
                    fn(*args)
        except queue.Empty:
            pass
        self.root.after(50, self._poll)


class Player:
    def __init__(self):
        self.tmp = os.path.join(tempfile.gettempdir(), "arcade_music_extractor_preview.wav")

    def play(self, samples, rate):
        if winsound is None:
            raise RuntimeError("Preview playback is only available on Windows.")
        self.stop()
        write_wav(self.tmp, samples, rate)
        winsound.PlaySound(self.tmp, winsound.SND_FILENAME | winsound.SND_ASYNC)

    def stop(self):
        if winsound is not None:
            winsound.PlaySound(None, winsound.SND_PURGE)


class ExtractTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        self.romset = None
        self.data = b""
        self.segments = []
        self.settings = None
        self.extra_plans = []
        self._build()

    # ---------------- layout ----------------
    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="ROM set:").pack(side="left")
        self.rom_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.rom_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="Open ZIP / ROM...", command=self.open_file).pack(side="left")
        ttk.Button(top, text="Open Folder...", command=self.open_folder).pack(side="left", padx=(4, 0))

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, pady=8)

        # Left: ROM files
        left = ttk.LabelFrame(body, text="ROM files (checked = sound data, in load order)", padding=4)
        body.add(left, weight=1)
        self.files = ttk.Treeview(left, columns=("use", "size"), show="tree headings",
                                  selectmode="extended", height=14)
        self.files.heading("#0", text="File")
        self.files.heading("use", text="Use")
        self.files.heading("size", text="Size")
        self.files.column("#0", width=170)
        self.files.column("use", width=40, anchor="center")
        self.files.column("size", width=80, anchor="e")
        self.files.pack(fill="both", expand=True)
        self.files.bind("<Double-1>", lambda e: self.toggle_selected())
        self.files.bind("<space>", lambda e: self.toggle_selected())
        fb = ttk.Frame(left)
        fb.pack(fill="x", pady=(4, 0))
        ttk.Button(fb, text="Toggle", command=self.toggle_selected).pack(side="left")
        ttk.Button(fb, text="Up", width=4, command=lambda: self.move(-1)).pack(side="left", padx=2)
        ttk.Button(fb, text="Down", width=5, command=lambda: self.move(1)).pack(side="left")
        ttk.Button(fb, text="Auto-detect", command=self.auto_detect).pack(side="right")

        # Right: settings + results
        right = ttk.Frame(body)
        body.add(right, weight=3)

        opts = ttk.LabelFrame(right, text="Decoding", padding=6)
        opts.pack(fill="x")
        ttk.Label(opts, text="Format:").grid(row=0, column=0, sticky="w")
        self.fmt_var = tk.StringVar(value=FORMAT_LABELS[0])
        fmt_box = ttk.Combobox(opts, textvariable=self.fmt_var, values=FORMAT_LABELS,
                               state="readonly", width=60)
        fmt_box.grid(row=0, column=1, columnspan=5, sticky="we", padx=4)
        fmt_box.bind("<<ComboboxSelected>>", lambda e: self.on_format())

        ttk.Label(opts, text="Sample rate (Hz):").grid(row=1, column=0, sticky="w", pady=4)
        self.rate_var = tk.IntVar(value=FORMATS["oki"].default_rate)
        ttk.Spinbox(opts, from_=1000, to=96000, textvariable=self.rate_var, width=8).grid(
            row=1, column=1, sticky="w", padx=4)
        ttk.Label(opts, text="Find samples by:").grid(row=1, column=2, sticky="e")
        self.method_var = tk.StringVar(value="OKI phrase table")
        ttk.Combobox(opts, textvariable=self.method_var, values=list(METHODS),
                     state="readonly", width=22).grid(row=1, column=3, sticky="w", padx=4)

        ttk.Label(opts, text="Combine files:").grid(row=2, column=0, sticky="w")
        self.combine_var = tk.StringVar(value="concat")
        ttk.Combobox(opts, textvariable=self.combine_var, values=["concat", "interleave"],
                     state="readonly", width=10).grid(row=2, column=1, sticky="w", padx=4)
        self.swap_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Byte-swap 16-bit", variable=self.swap_var).grid(row=2, column=2, sticky="w")
        self.lowfirst_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Low nibble first", variable=self.lowfirst_var).grid(row=2, column=3, sticky="w")

        ttk.Label(opts, text="Padding run (bytes):").grid(row=3, column=0, sticky="w", pady=4)
        self.run_var = tk.IntVar(value=512)
        ttk.Spinbox(opts, from_=16, to=65536, increment=64, textvariable=self.run_var, width=8).grid(
            row=3, column=1, sticky="w", padx=4)
        ttk.Label(opts, text="Min length (s):").grid(row=3, column=2, sticky="e")
        self.minlen_var = tk.DoubleVar(value=0.05)
        ttk.Spinbox(opts, from_=0.0, to=600.0, increment=0.5, textvariable=self.minlen_var, width=8).grid(
            row=3, column=3, sticky="w", padx=4)
        self.norm_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Normalize volume", variable=self.norm_var).grid(row=3, column=4, sticky="w")
        opts.columnconfigure(5, weight=1)

        res = ttk.LabelFrame(right, text="Samples / tracks found", padding=4)
        res.pack(fill="both", expand=True, pady=(8, 0))
        cols = ("label", "offset", "bytes", "secs")
        self.results = ttk.Treeview(res, columns=cols, show="headings", selectmode="extended")
        for c, t, w, a in (("label", "Name", 180, "w"), ("offset", "Offset", 90, "e"),
                           ("bytes", "Bytes", 90, "e"), ("secs", "Duration", 80, "e")):
            self.results.heading(c, text=t, command=lambda c=c: self.sort_results(c))
            self.results.column(c, width=w, anchor=a)
        sb = ttk.Scrollbar(res, orient="vertical", command=self.results.yview)
        self.results.configure(yscrollcommand=sb.set)
        self.results.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.results.bind("<Double-1>", lambda e: self.play())

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(6, 0))
        ttk.Button(actions, text="Scan ROM", command=self.scan).pack(side="left")
        ttk.Button(actions, text="Play", command=self.play).pack(side="left", padx=(16, 0))
        ttk.Button(actions, text="Stop", command=self.app.player.stop).pack(side="left", padx=4)
        ttk.Button(actions, text="Export selected...", command=lambda: self.export(True)).pack(side="right")
        ttk.Button(actions, text="Export all...", command=lambda: self.export(False)).pack(side="right", padx=4)

    # ---------------- ROM files ----------------
    def open_file(self):
        path = filedialog.askopenfilename(title="Open ROM set",
                                          filetypes=[("ROM sets", "*.zip"), ("All files", "*.*")])
        if path:
            self.load(path)

    def open_folder(self):
        path = filedialog.askdirectory(title="Open folder of ROM files")
        if path:
            self.load(path)

    def load(self, path):
        def job():
            return RomSet.load(path)

        def done(romset):
            self.romset = romset
            self.rom_var.set(path)
            self.files.delete(*self.files.get_children())
            for name, blob in romset.files.items():
                self.files.insert("", "end", iid=name, text=name, values=("", "%d KB" % (len(blob) // 1024)))
            self.clear_results()
            self.app.set_status("Loaded %d files from %s" % (len(romset.files), os.path.basename(path)))
            self.auto_detect(quiet=True)

        self.app.worker.run(job, done, self.app.busy)

    def set_used(self, names):
        for iid in self.files.get_children():
            self.files.set(iid, "use", "")
        for idx, name in enumerate(names):
            if self.files.exists(name):
                self.files.set(name, "use", "\u2714")
                self.files.move(name, "", idx)

    def used_files(self):
        return [i for i in self.files.get_children() if self.files.set(i, "use")]

    def toggle_selected(self):
        for iid in self.files.selection():
            self.files.set(iid, "use", "" if self.files.set(iid, "use") else "\u2714")

    def move(self, delta):
        for iid in (self.files.selection() if delta < 0 else reversed(self.files.selection())):
            self.files.move(iid, "", self.files.index(iid) + delta)

    def auto_detect(self, quiet=False):
        if not self.romset:
            return
        plans = detect(self.romset)
        if not plans:
            hints = guess_sound_files(self.romset)
            self.set_used(hints)
            if not quiet:
                messagebox.showinfo(
                    "Auto-detect",
                    "No known sound-ROM layout recognised.\n\nTick the sample ROMs, pick the "
                    "sound chip format and use 'Split on padding'.\n\nIf the game's music is FM "
                    "synthesis (YM2151/YM2203/YM2612...), use the 'Record with MAME' tab instead.")
            self.app.set_status("Auto-detect: nothing recognised" + (", name hints applied" if hints else ""))
            return
        plan = plans[0]
        self.apply_plan(plan)
        self.extra_plans = [p for p in plans[1:]] + plan.extra
        msg = "Auto-detect: " + plan.reason
        if self.extra_plans:
            msg += "  (also found: %s)" % "; ".join(p.reason for p in self.extra_plans)
        self.app.set_status(msg)

    def apply_plan(self, plan):
        s = plan.settings
        self.set_used(plan.files)
        self.fmt_var.set(FORMAT_LABELS[FORMAT_KEYS.index(s.fmt)])
        self.rate_var.set(s.rate)
        self.method_var.set(next(k for k, v in METHODS.items() if v == s.method))
        self.run_var.set(s.min_run)
        self.combine_var.set(plan.combine)
        self.swap_var.set(plan.byteswap)

    def on_format(self):
        key = FORMAT_KEYS[FORMAT_LABELS.index(self.fmt_var.get())]
        self.rate_var.set(FORMATS[key].default_rate)
        if key != "oki" and METHODS[self.method_var.get()] == "table":
            self.method_var.set("Split on padding")

    # ---------------- scanning / results ----------------
    def current_settings(self):
        return Settings(
            fmt=FORMAT_KEYS[FORMAT_LABELS.index(self.fmt_var.get())],
            rate=int(self.rate_var.get()),
            method=METHODS[self.method_var.get()],
            high_nibble_first=not self.lowfirst_var.get(),
            min_run=int(self.run_var.get()),
            min_seconds=float(self.minlen_var.get()),
            normalize=self.norm_var.get())

    def clear_results(self):
        self.results.delete(*self.results.get_children())
        self.segments = []

    def scan(self):
        if not self.romset:
            messagebox.showinfo("Scan", "Open a ROM set first.")
            return
        names = self.used_files()
        if not names:
            messagebox.showinfo("Scan", "Tick at least one ROM file (double-click a file).")
            return
        try:
            settings = self.current_settings()
        except (tk.TclError, ValueError) as exc:
            messagebox.showerror("Settings", str(exc))
            return
        combine_mode, swap = self.combine_var.get(), self.swap_var.get()
        romset = self.romset

        def job():
            data = combine(romset, names, combine_mode, swap)
            return data, find_segments(data, settings)

        def done(result):
            self.data, self.segments = result
            self.settings = settings
            self.results.delete(*self.results.get_children())
            audio = settings.audio
            for i, seg in enumerate(self.segments):
                self.results.insert("", "end", iid=str(i), values=(
                    seg.label, "%06X" % seg.offset, seg.length,
                    "%.2f s" % audio.duration(seg.length, settings.rate)))
            total = sum(audio.duration(s.length, settings.rate) for s in self.segments)
            msg = "Found %d samples (%.1f s of audio) in %d KB" % (
                len(self.segments), total, len(self.data) // 1024)
            if not self.segments and settings.method == "table":
                msg += " - no valid OKI phrase table; try 'Split on padding'"
            self.app.set_status(msg)

        self.app.worker.run(job, done, self.app.busy)

    def sort_results(self, col):
        items = [(self.results.set(i, col), i) for i in self.results.get_children()]
        def key(t):
            v = t[0].replace(" s", "")
            try:
                return int(v, 16) if col == "offset" else float(v)
            except ValueError:
                return v
        items.sort(key=key, reverse=getattr(self, "_sort_rev", False))
        self._sort_rev = not getattr(self, "_sort_rev", False)
        for idx, (_, iid) in enumerate(items):
            self.results.move(iid, "", idx)

    def selected_segments(self):
        return [self.segments[int(i)] for i in self.results.selection()]

    def play(self):
        segs = self.selected_segments()
        if not segs:
            return
        seg, settings, data = segs[0], self.settings, self.data
        self.app.set_status("Playing %s..." % seg.label)

        def job():
            self.app.player.play(decode_segment(data, seg, settings), settings.rate)

        self.app.worker.run(job, None, self.app.busy)

    def export(self, selected_only):
        segs = self.selected_segments() if selected_only else list(self.segments)
        if not segs:
            messagebox.showinfo("Export", "Nothing to export - scan the ROM and select samples.")
            return
        out = filedialog.askdirectory(title="Export WAV files to")
        if not out:
            return
        out = os.path.join(out, self.romset.name)
        data, settings, prefix = self.data, self.settings, "%s_%s" % (self.romset.name, self.settings.fmt)

        def progress(i, n):
            self.app.worker.call(self.app.set_status, "Exporting %d / %d..." % (i, n))

        def job():
            return export_segments(data, segs, settings, out, prefix, progress)

        def done(written):
            self.app.set_status("Exported %d WAV files to %s" % (len(written), out))
            if messagebox.askyesno("Export complete", "Exported %d files.\nOpen the folder?" % len(written)):
                open_folder(out)

        self.app.worker.run(job, done, self.app.busy)


class MameTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        self._build()

    def _row(self, parent, row, label, var, browse=None, width=60):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=var, width=width).grid(row=row, column=1, sticky="we", padx=4)
        if browse:
            ttk.Button(parent, text="Browse...", command=browse).grid(row=row, column=2)

    def _build(self):
        intro = ("Most arcade soundtracks (YM2151, YM2203, YM2612, Namco, Konami SCC...) are "
                 "synthesised live by the sound CPU, so they are not stored as audio in the ROM. "
                 "This tab runs the game in MAME (which you supply) and records its audio output "
                 "with -wavwrite, then splits the recording into tracks at the silences.")
        ttk.Label(self, text=intro, wraplength=820, justify="left").pack(fill="x")

        form = ttk.LabelFrame(self, text="Record", padding=8)
        form.pack(fill="x", pady=8)
        self.mame_var = tk.StringVar(value=self._guess_mame())
        self.rompath_var = tk.StringVar()
        self.game_var = tk.StringVar()
        self.secs_var = tk.IntVar(value=300)
        self.lua_var = tk.StringVar()
        self.out_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Music"))
        self.split_var = tk.BooleanVar(value=True)
        self._row(form, 0, "MAME executable:", self.mame_var, self.pick_mame)
        self._row(form, 1, "ROM folder:", self.rompath_var, self.pick_rompath)
        self._row(form, 2, "Game (MAME short name):", self.game_var)
        ttk.Label(form, text="e.g. sf2, outrun, gradius, mslug - the zip name without .zip").grid(
            row=2, column=2, sticky="w")
        ttk.Label(form, text="Seconds to record:").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Spinbox(form, from_=10, to=36000, increment=30, textvariable=self.secs_var, width=8).grid(
            row=3, column=1, sticky="w", padx=4)
        self._row(form, 4, "Sound-test Lua script (optional):", self.lua_var, self.pick_lua)
        ttk.Button(form, text="Save Lua template...", command=self.save_lua_template).grid(
            row=5, column=1, sticky="w", padx=4)
        self._row(form, 6, "Output folder:", self.out_var, self.pick_out)
        ttk.Checkbutton(form, text="Split recording into tracks", variable=self.split_var).grid(
            row=7, column=1, sticky="w", padx=4, pady=3)
        ttk.Button(form, text="Record", command=self.record).grid(row=8, column=1, sticky="w", padx=4, pady=6)
        form.columnconfigure(1, weight=1)

        split = ttk.LabelFrame(self, text="Split an existing WAV recording into tracks", padding=8)
        split.pack(fill="x")
        self.thresh_var = tk.DoubleVar(value=-50.0)
        self.gap_var = tk.DoubleVar(value=1.5)
        self.minlen_var = tk.DoubleVar(value=2.0)
        for col, (label, var, lo, hi, inc) in enumerate((
                ("Silence level (dBFS):", self.thresh_var, -90, -10, 5),
                ("Gap between tracks (s):", self.gap_var, 0.1, 30, 0.5),
                ("Min track length (s):", self.minlen_var, 0.1, 600, 1))):
            ttk.Label(split, text=label).grid(row=0, column=col * 2, sticky="w")
            ttk.Spinbox(split, from_=lo, to=hi, increment=inc, textvariable=var, width=7).grid(
                row=0, column=col * 2 + 1, sticky="w", padx=(4, 16))
        ttk.Button(split, text="Split WAV...", command=self.split_existing).grid(row=0, column=6)

        logf = ttk.LabelFrame(self, text="MAME output", padding=4)
        logf.pack(fill="both", expand=True, pady=(8, 0))
        self.log = tk.Text(logf, height=10, wrap="none", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

    @staticmethod
    def _guess_mame():
        for p in (r"C:\mame\mame.exe", r"C:\Program Files\MAME\mame.exe",
                  os.path.join(os.path.expanduser("~"), "mame", "mame.exe")):
            if os.path.exists(p):
                return p
        return ""

    def pick_mame(self):
        p = filedialog.askopenfilename(title="Locate mame.exe",
                                       filetypes=[("MAME", "mame*.exe"), ("All files", "*.*")])
        if p:
            self.mame_var.set(p)
            rom_dir = os.path.join(os.path.dirname(p), "roms")
            if not self.rompath_var.get() and os.path.isdir(rom_dir):
                self.rompath_var.set(rom_dir)

    def pick_rompath(self):
        p = filedialog.askdirectory(title="Folder containing the ROM zips")
        if p:
            self.rompath_var.set(p)

    def pick_lua(self):
        p = filedialog.askopenfilename(title="Lua script", filetypes=[("Lua", "*.lua"), ("All files", "*.*")])
        if p:
            self.lua_var.set(p)

    def pick_out(self):
        p = filedialog.askdirectory(title="Output folder")
        if p:
            self.out_var.set(p)

    def save_lua_template(self):
        p = filedialog.asksaveasfilename(title="Save Lua template", defaultextension=".lua",
                                         initialfile="soundtest.lua", filetypes=[("Lua", "*.lua")])
        if p:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(mame.LUA_TEMPLATE)
            self.lua_var.set(p)
            open_folder(p)

    def append_log(self, line):
        self.log.insert("end", line + "\n")
        self.log.see("end")

    def split_kwargs(self):
        return dict(threshold_db=float(self.thresh_var.get()), min_silence=float(self.gap_var.get()),
                    min_length=float(self.minlen_var.get()))

    def record(self):
        exe, game = self.mame_var.get().strip(), self.game_var.get().strip()
        if not exe or not os.path.exists(exe):
            messagebox.showerror("MAME", "Select your mame.exe first (download MAME from mamedev.org).")
            return
        if not game:
            messagebox.showerror("MAME", "Enter the game's MAME short name (e.g. sf2).")
            return
        out_dir = self.out_var.get().strip() or os.getcwd()
        os.makedirs(out_dir, exist_ok=True)
        wav = os.path.join(out_dir, "%s.wav" % game)
        cmd = mame.build_command(exe, game, wav, int(self.secs_var.get()),
                                 self.rompath_var.get().strip(), self.lua_var.get().strip())
        do_split, kwargs = self.split_var.get(), self.split_kwargs()
        self.log.delete("1.0", "end")
        self.append_log(" ".join('"%s"' % c if " " in c else c for c in cmd))
        self.app.set_status("Recording %s with MAME..." % game)

        def job():
            code = mame.run(cmd, log=lambda l: self.app.worker.call(self.append_log, l))
            if not os.path.exists(wav):
                raise RuntimeError("MAME exited with code %d and wrote no audio. Check the log "
                                   "(missing ROMs or BIOS such as neogeo.zip are the usual cause)." % code)
            tracks = split_wav_file(wav, os.path.join(out_dir, game + "_tracks"), **kwargs) if do_split else []
            return wav, tracks

        def done(result):
            wav_path, tracks = result
            msg = "Recorded %s" % wav_path
            if do_split:
                msg += " and split it into %d tracks" % len(tracks)
            self.app.set_status(msg)
            if messagebox.askyesno("Recording complete", msg + ".\nOpen the folder?"):
                open_folder(out_dir)

        self.app.worker.run(job, done, self.app.busy)

    def split_existing(self):
        p = filedialog.askopenfilename(title="WAV to split", filetypes=[("WAV", "*.wav")])
        if not p:
            return
        out = os.path.splitext(p)[0] + "_tracks"
        kwargs = self.split_kwargs()

        def done(written):
            self.app.set_status("Split into %d tracks in %s" % (len(written), out))
            if written and messagebox.askyesno("Split complete", "Wrote %d tracks.\nOpen the folder?" % len(written)):
                open_folder(out)

        self.app.worker.run(lambda: split_wav_file(p, out, **kwargs), done, self.app.busy)


def open_folder(path):
    if os.path.isfile(path):
        path = os.path.dirname(path)
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            os.system('open "%s"' % path)
        else:
            os.system('xdg-open "%s" >/dev/null 2>&1 &' % path)
    except OSError:
        pass


class App:
    def __init__(self, root):
        self.root = root
        root.title("Arcade Music Extractor %s" % __version__)
        root.geometry("1100x720")
        root.minsize(900, 600)
        try:
            if sys.platform == "win32":
                ttk.Style().theme_use("vista")
        except tk.TclError:
            pass
        self.worker = Worker(root)
        self.player = Player()

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.extract_tab = ExtractTab(nb, self)
        nb.add(self.extract_tab, text="  Extract from ROM  ")
        nb.add(MameTab(nb, self), text="  Record with MAME  ")
        nb.add(self._about(nb), text="  Help  ")

        bar = ttk.Frame(root)
        bar.pack(fill="x", side="bottom")
        self.status = tk.StringVar(value="Open a MAME ROM set (.zip) to begin.")
        ttk.Label(bar, textvariable=self.status, anchor="w").pack(side="left", fill="x", expand=True, padx=6)
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=160)
        self.progress.pack(side="right", padx=6, pady=3)
        root.bind("<<WorkerIdle>>", lambda e: self.busy(False))

        if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
            self.extract_tab.load(sys.argv[1])

    def _about(self, master):
        f = ttk.Frame(master, padding=12)
        text = tk.Text(f, wrap="word", relief="flat", font=("Segoe UI", 10))
        text.insert("1.0", HELP_TEXT)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True)
        return f

    def set_status(self, msg):
        self.status.set(msg)

    def busy(self, on):
        if on:
            self.progress.start(12)
        else:
            self.progress.stop()


HELP_TEXT = """Arcade Music Extractor

TWO KINDS OF ARCADE AUDIO
1. Sampled audio stored in the ROM (ADPCM / PCM). The "Extract from ROM" tab decodes it straight to WAV:
   - OKI MSM6295 (CPS1, Toaplan, Kaneko, NMK, many Korean/Taiwanese games) - uses the chip's phrase table, so every sample/track is cut exactly.
   - Yamaha ADPCM-A / ADPCM-B (Neo Geo V ROMs, Taito)
   - Yamaha YMZ280B (Cave shooters, where the whole soundtrack is streamed ADPCM)
   - 8-bit PCM (CPS2 QSound, SegaPCM in OutRun/Space Harrier/System 16, Konami K054539)
   - 16-bit PCM
2. Synthesised music (YM2151, YM2203, YM2612, Namco WSG, SCC...). The notes are generated live by the sound CPU, so the only faithful way to rip them is to emulate the board. The "Record with MAME" tab runs your copy of MAME with -wavwrite and splits the recording into tracks.

QUICK START (ROM extraction)
1. Open the game's MAME .zip. Clones need the parent set's sound ROMs - open the parent zip.
2. Auto-detect ticks the sound ROMs and picks a format when it recognises the layout (Neo Geo, CPS2, any OKI phrase table).
   Otherwise tick the sample ROMs yourself (double-click), choose the chip format, and use "Split on padding".
3. Scan ROM, double-click a row to preview, then Export.

TIPS
- Wrong pitch? Change the sample rate. OKI: 7575 or 6060 Hz are typical (1 MHz clock / 132 or 165). Neo Geo ADPCM-A: 18518 Hz.
- Static or noise instead of sound usually means the wrong format, wrong file order, or the files need "interleave" / "byte-swap".
- Long tracks being cut up? Raise the padding run. Too many tiny pieces? Raise the minimum length.
- The MAME driver source (e.g. src/mame/capcom/cps1.cpp) lists each game's sound ROMs and load order.

RECORDING WITH MAME
- Attract mode often plays only one or two tunes. For a full rip, use the game's sound test, or the Lua template: it writes sound codes to the sound latch one by one so the whole soundtrack is captured in one go.
- Neo Geo games also need neogeo.zip in the ROM folder.

Only use ROMs you own and are legally entitled to use.
"""


def main():
    if sys.platform == "win32":
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()
