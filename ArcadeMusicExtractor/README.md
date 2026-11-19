# Arcade Music Extractor

A Windows desktop app for pulling music and samples out of arcade ROM sets (MAME `.zip` files) and saving them as WAV.

Arcade games store audio in two different ways, and the app handles both:

| How the game makes sound | Examples | What the app does |
|---|---|---|
| **Sampled audio in ROM** (ADPCM / PCM) | CPS1 & Toaplan (OKI MSM6295), Neo Geo (YM2610 ADPCM-A/B), Cave shooters (YMZ280B), CPS2 (QSound), OutRun / System 16 (SegaPCM) | **Extract from ROM** tab: decodes the sample ROMs straight to WAV, one file per sample or track |
| **Synthesised music** (FM / wavetable) | YM2151, YM2203, YM2612, Namco WSG, Konami SCC | **Record with MAME** tab: runs the game in your copy of MAME with `-wavwrite` and splits the recording into tracks |

Synthesised music isn't stored as audio anywhere in the ROM. The sound CPU's driver plays the notes live, so the only accurate way to rip it is to emulate the board.

## Download / build

* **Prebuilt:** every push to this folder runs the *Build Arcade Music Extractor (Windows)* GitHub Actions workflow. It produces `ArcadeMusicExtractor-windows.zip` (in the run's **Artifacts**) containing:
  * `ArcadeMusicExtractor.exe`: the GUI (double-click it)
  * `ArcadeMusicExtractor-cli.exe`: the same tool for the command line
  
  Pushing a tag named `extractor-v*` (e.g. `extractor-v1.0.0`) also attaches the zip to a GitHub Release.
* **Run from source:** install Python 3.9+ from python.org (it includes Tk), then:
  ```
  cd ArcadeMusicExtractor
  python run_extractor.py
  ```
  The app uses only the Python standard library, so there's nothing to `pip install`.
* **Build the exe yourself:** `pip install pyinstaller`, then
  `pyinstaller --onefile --windowed --name ArcadeMusicExtractor run_extractor.py`

## Extract from ROM

1. **Open ZIP / ROM...** and pick the game's MAME zip. Clone sets usually don't include the sound ROMs, so open the parent set.
2. **Auto-detect** (runs automatically when you open a zip) ticks the sound ROMs and chooses a decoder when it recognises the layout:
   * any ROM with a valid **OKI MSM6295 phrase table**. The table holds the exact start and end of every sample, so the cuts are exact.
   * **Neo Geo** `*-v1`, `*-v2`... ROMs → ADPCM-A (and `*-v2x` → ADPCM-B on early boards)
   * **CPS2** `*.11m`–`*.14m` → QSound 8-bit PCM, byte-swapped
3. If nothing is recognised, double-click the sample ROMs to tick them, put them in load order (**Up/Down**), choose the chip **Format**, and set **Find samples by** to *Split on padding*.
4. **Scan ROM**. Double-click a row to preview it (Windows only), then **Export all...** or **Export selected...**.

### Supported formats

| Format | Default rate | Used by |
|---|---|---|
| OKI MSM6295 / MSM5205 ADPCM | 7575 Hz | CPS1, Toaplan, Kaneko, NMK, Video System, many Korean/Taiwanese boards |
| Yamaha ADPCM-A | 18518 Hz | Neo Geo MVS, Taito F2 / B System |
| Yamaha ADPCM-B / Delta-T | 22050 Hz | Neo Geo ADPCM-B, YM2608, Y8950 |
| Yamaha YMZ280B ADPCM | 16000 Hz | Cave, Psikyo, Kaneko |
| 8-bit signed PCM | 24000 Hz | CPS2 QSound, Konami K054539 |
| 8-bit unsigned PCM | 15625 Hz | SegaPCM (OutRun, Space Harrier, System 16/18), Namco |
| 16-bit PCM (LE / BE) | 22050 Hz | generic |

The decoders follow the real chips' algorithms (step tables, 12-bit wrap or saturation, adaptive delta), so the output sounds like the hardware.

### Troubleshooting

* **Wrong pitch:** change the sample rate. On OKI boards it depends on the clock: 1 MHz ÷ 132 = 7575 Hz and ÷ 165 = 6060 Hz are the usual values. Other games run the chip at 1.056 MHz, 1.5 MHz, and so on.
* **Static / noise:** wrong format, wrong file order, or the ROMs need *interleave* or *byte-swap*. MAME's driver source (e.g. `src/mame/capcom/cps1.cpp`) lists every game's sound ROMs and how they're loaded.
* **Long tracks are chopped up:** increase *Padding run*. **Lots of tiny clips:** increase *Min length*.
* **Banked OKI boards** (NMK112 and similar) are handled by also scanning for phrase tables at every 256 KB bank. Boards with exotic bank switching may need *Split on padding* instead.

## Record with MAME

1. Get MAME from mamedev.org and point the app at `mame.exe` and your ROM folder.
2. Enter the game's MAME short name (`sf2`, `outrun`, `gradius`...) and how many seconds to record.
3. **Record**. MAME runs muted and unthrottled (much faster than real time). The app writes `<game>.wav`, then splits it at the silences into `<game>_tracks\<game>_track01.wav`...

Attract mode usually plays only a tune or two. To get the whole soundtrack:
* use the game's built-in sound test, or
* **Save Lua template...**: a MAME autoboot script that writes each sound code to the sound latch in turn, waits, and moves on. Fill in the CPU tag, latch address and the list of codes for your game. The MAME debugger or the game's own sound test will show them.

Neo Geo games also need `neogeo.zip` in the ROM folder.

You can also **Split WAV...** any long recording, including ones you made yourself, using an adjustable silence level, gap length, and minimum track length.

## Command line

```
ArcadeMusicExtractor-cli scan    game.zip                      # list what was found
ArcadeMusicExtractor-cli extract game.zip -o out               # auto-detect + export WAVs
ArcadeMusicExtractor-cli extract game.zip --format ymz280b --files u1.bin,u2.bin --rate 16000 --min-run 2048 -o out
ArcadeMusicExtractor-cli split   recording.wav -o tracks --gap 2
ArcadeMusicExtractor-cli mame    sf2 --mame C:\mame\mame.exe --seconds 600 --split
ArcadeMusicExtractor-cli lua-template > soundtest.lua
```

(From source: `python -m arcade_music_extractor <command> ...`.)

## Development

```
cd ArcadeMusicExtractor
python -m unittest discover -s tests -v
```

Layout: `codecs.py` (chip decoders), `romset.py` (zip/folder loading, interleave/byte-swap), `extract.py` (OKI phrase tables, padding splitter, auto-detect), `wavtools.py` (WAV I/O, silence splitting), `mame.py` (MAME recording and the Lua template), `gui.py` (Tkinter UI), `cli.py`.

Only use ROMs you own and are legally entitled to use.
