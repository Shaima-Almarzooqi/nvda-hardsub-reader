# Changelog

## 1.0.1
- The OCR helper now ships as a self-contained program: installing
  Python is no longer required. A system Python (with Pillow) is used
  automatically as a fallback if the bundled helper cannot run.
- The OneOCR wrapper module (MIT licensed) is bundled inside the
  add-on; `pip install oneocr` is no longer required.
- Fixed a keystroke conflict with NVDA's built-in sound split toggle
  (NVDA 2024.2+): subtitle reading is now toggled with NVDA+alt+shift+s,
  and the interrupt-mode toggle ships unassigned (bindable via Input
  Gestures).

## 1.0.0
- First public release.
- Settings panel in NVDA Settings (refresh interval, scan region height,
  confirmation polls, repeat suppression window, interrupt mode, legacy
  OCR language, Python path).
- Automatic engine fallback: OneOCR (Windows 11) with legacy Windows OCR
  (Windows 10) as fallback, announced at startup.
- Bundled setup_oneocr.ps1 for one-step OneOCR engine setup.
- Input gestures categorized and reassignable; translator comments added
  throughout for localization.

## 0.x (development history)
- 0.6: clean rebuild around a unit-tested SubtitleTracker core.
- 0.5.x: fuzzy stability gate (fixes silence on busy backgrounds).
- 0.4.x: 0.3s polling, interrupt mode, word-level extension handling.
- 0.3: per-line dedup with repeat-suppression window.
- 0.2.x: watchdog auto-restart, UTF-8 pipeline, crash guards, logging.
- 0.1: initial prototype (OneOCR sidecar architecture).
