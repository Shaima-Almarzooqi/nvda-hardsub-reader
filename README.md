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
no internet connection is used and nothing is sent anywhere. The only
file the add-on writes is a small troubleshooting log in your temporary
folder, which never contains subtitle text or screen content.

## Installation

1. Download the `.nvda-addon` file from the
   [Releases page](../../releases), open it to install, and restart
   NVDA.
2. On Windows 11, after installing and restarting NVDA, the add-on
   offers once to set up the high-accuracy OneOCR engine — answer Yes
   and approve the administrator prompt (a one-time step that copies the
   engine files from your own Snipping Tool). You can also start it any
   time from the "Set up the high-accuracy OneOCR engine now" button in
   the add-on settings. Declining is fine — the add-on uses the fallback
   engine automatically.

## How to use

- **NVDA+alt+shift+s** — start or stop subtitle reading. A high beep
  means the engine is ready. Keep the video window focused.
- Interrupt behavior (whether a new subtitle cuts off the previous one)
  is in the add-on settings, with an assignable keystroke in the Input
  Gestures dialog.
- Settings live under NVDA menu → Preferences → Settings → **HardSub
  Reader**, and full documentation is bundled with the add-on's help.

## About this project

This add-on was created through AI-assisted development ("vibe coding"):
the author has no programming background and built it in collaboration
with an AI assistant (Anthropic's Claude), directing the design and
extensively testing every version in real-world use as a blind screen
reader user. The code includes an automated test suite covering the
subtitle-detection logic, and issue reports are very welcome.

For developers: `python build_addon.py` builds the package,
`python test_tracker.py` runs the test suite, and the GitHub Actions
workflow builds the self-contained helpers and assembles the complete
release package.

## License

GNU General Public License v2. See `copying.txt`.
