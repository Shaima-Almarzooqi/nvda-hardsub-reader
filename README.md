# HardSub Reader — NVDA add-on

Reads hardcoded (burned-in) video subtitles aloud in real time using the
high-accuracy Windows 11 OneOCR engine, with automatic fallback to legacy
Windows OCR on Windows 10. Works with any video source and any script the
engine supports, including right-to-left languages such as Arabic.

Works on both **x64 and ARM64** Windows: the OCR runs in your system
Python, and the Snipping Tool ships engine binaries matching your machine.

## Install (users)

1. Install Python 3.10+ for your architecture from python.org
   (check "Add Python to PATH" during setup).
2. In a command prompt: `pip install pillow oneocr winocr`
3. Windows 11 only, for the high-accuracy engine: right-click
   `setup_oneocr.ps1`, "Run with PowerShell" as administrator. (Skipping
   this is fine — the add-on falls back to the legacy engine and tells
   you so.)
4. Install the `.nvda-addon` file from the Releases page and restart NVDA.

## Use

- `NVDA+alt+s` — toggle subtitle reading (high beep = engine ready).
- `NVDA+alt+shift+s` — toggle interrupt mode.
- Settings: NVDA menu → Preferences → Settings → **HardSub Reader**.

Full documentation is bundled with the add-on (NVDA menu → Tools →
Add-on store → installed add-ons → HardSub Reader → help), and in
`addon/doc/en/readme.html`.

## Build (developers)

```
python build_addon.py     # produces hardSubReader-<version>.nvda-addon
python test_tracker.py    # runs the 13-scenario dedup test suite
```

The test suite imports the actual shipped sidecar module; run it before
every release.

## Architecture

- `addon/globalPlugins/hardSubReader/` — NVDA-side plugin: gestures,
  settings panel, watchdog, speech output.
- `addon/sidecar/subtitle_ocr_server.py` — external helper run in the
  system Python (NVDA's embedded 32-bit Python cannot load the OCR
  DLLs): screen capture, OCR with engine fallback, and the
  `SubtitleTracker` dedup core (fuzzy stability gate, repeat-suppression
  window, word-level extension handling).
- They communicate over JSON lines on stdout; the plugin restarts the
  helper automatically if it dies, with a crash-loop limit and a
  diagnostic log in `%TEMP%\hardSubReader_sidecar.log`.

## Publishing checklist (NVDA Add-on Store)

- [ ] Replace `copying.txt` with the full GPL v2 text (store requirement).
- [ ] Fill in your real name/email in `addon/manifest.ini` (author) and
      the repository URL.
- [ ] Tag a GitHub release and attach the built `.nvda-addon` file.
- [ ] Verify `lastTestedNVDAVersion` matches the NVDA version you tested.
- [ ] Submit via https://github.com/nvaccess/addon-datastore (see its
      README: you open a pull request adding metadata that points at your
      release download URL).

## License

GNU General Public License v2 (required for NVDA add-ons). See
`copying.txt`.
