# HardSub Reader — NVDA add-on

Many videos have subtitles burned directly into the picture — foreign
films, social media clips, older DVDs, lecture recordings. Screen readers
normally can't access them at all. HardSub Reader fixes that: it reads
these hardcoded subtitles aloud in real time while you watch.

It works with any video source — streaming sites, media players, video
calls — and in the languages supported by the OCR engine, including
right-to-left scripts such as Arabic. Recognition is powered by the
high-accuracy OneOCR engine built into Windows 11, with an automatic
fallback engine on Windows 10. Everything runs locally on your computer:
no internet connection is used and nothing is sent anywhere.

## Installation

1. Install Python 3.10 or newer from python.org (tick "Add Python to
   PATH" during setup).
2. Open a command prompt and run: `pip install pillow oneocr winocr`
3. On Windows 11, right-click `setup_oneocr.ps1` from this repository and
   choose "Run with PowerShell" as administrator. This one-time step
   enables the high-accuracy engine. Skipping it is fine — the add-on
   will use the fallback engine and tell you so.
4. Download the `.nvda-addon` file from the
   [Releases page](../../releases), open it to install, and restart NVDA.

## How to use

- **NVDA+alt+s** — start or stop subtitle reading. A high beep means the
  engine is ready. Keep the video window focused.
- **NVDA+alt+shift+s** — choose whether a new subtitle interrupts the one
  currently being read.
- Settings live under NVDA menu → Preferences → Settings → **HardSub
  Reader**, and full documentation is bundled with the add-on's help.

## About this project

This add-on was created through AI-assisted development ("vibe coding"):
the author has no programming background and built it in collaboration
with an AI assistant (Anthropic's Claude), directing the design and
extensively testing every version in real-world use as a blind screen
reader user. The code includes an automated test suite covering the
subtitle-detection logic, and issue reports are very welcome.

For developers: `python build_addon.py` builds the package and
`python test_tracker.py` runs the test suite.

## License

GNU General Public License v2. See `copying.txt`.
