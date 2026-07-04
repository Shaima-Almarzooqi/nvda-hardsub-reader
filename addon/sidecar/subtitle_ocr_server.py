"""Subtitle OCR sidecar server (v1.0).

Runs OUTSIDE NVDA, in the system's native Python (ARM64 or x64), where the
OCR engine can load. NVDA's addon spawns this process and reads one JSON
object per line from stdout:

    {"type": "ready", "engine": "..."}                    engine loaded
    {"type": "subtitle", "kind": "line",   "text": ...}   new subtitle line
    {"type": "subtitle", "kind": "suffix", "text": ...}   continuation
    {"type": "error", "message": ...}                     problems

Engines, in order of preference:
  1. OneOCR  -- the Windows 11 Snipping Tool engine (best accuracy).
     Requires the 'oneocr' pip package and its engine files.
  2. Legacy Windows OCR via the 'winocr' pip package -- lower accuracy
     but works on Windows 10 and needs no engine-file setup.

All tunables can be overridden by command line arguments (the NVDA addon
passes the user's settings):
    --interval SECONDS   poll interval            (default 0.3)
    --region PERCENT     bottom strip height      (default 30)
    --stable N           stability frames         (default 2)
    --window SECONDS     repeat suppression       (default 8)
    --lang CODE          legacy-OCR language      (default en)

The process exits when its stdin is closed.
"""
import difflib
import faulthandler
import json
import sys
import threading
import time
import traceback

# UTF-8 or bust: Windows pipes default to a legacy locale encoding that
# cannot represent most non-English characters.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

faulthandler.enable(file=sys.stderr)

# ---------------------------------------------------------------------------
# Defaults (overridable via CLI)
# ---------------------------------------------------------------------------
POLL_INTERVAL = 0.3
REGION_FRACTION = 0.30
STABLE_FRAMES = 2
SIMILARITY_THRESHOLD = 0.85
REPEAT_WINDOW = 8.0
OCR_LANG = "en"
MIN_DIM = 64                 # pad captures below this size (DLL crash guard)

_PUNCT = ".,!?;:\"'\u2026\u2019\u2018\u201c\u201d-\u2013\u2014()[]"

# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def log(msg):
    sys.stderr.write(time.strftime("%H:%M:%S ") + msg + "\n")
    sys.stderr.flush()


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def normalize(s):
    repl = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
            "\u2013": "-", "\u2014": "-"}
    for a, b in repl.items():
        s = s.replace(a, b)
    return " ".join(s.split())


def similar(a, b):
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def word_keys(s):
    """Lowercased words with surrounding punctuation stripped."""
    out = []
    for w in s.split():
        w = w.strip(_PUNCT).lower()
        if w:
            out.append(w)
    return out


# ---------------------------------------------------------------------------
# The dedup / stability / extension brain. Pure Python, no Windows APIs,
# so it is directly unit-testable on any platform (see test_tracker.py).
# ---------------------------------------------------------------------------


class SubtitleTracker:
    """Feed OCR lines each poll; get back what should be spoken.

    update(lines, now) -> list of (kind, text) where kind is "line" for a
    new subtitle (may interrupt speech) or "suffix" for the continuation
    of an already-spoken line (must never interrupt).
    """

    def __init__(self, stable_frames=None, similarity=None,
                 repeat_window=None):
        self.stable_frames = stable_frames or STABLE_FRAMES
        self.similarity = similarity or SIMILARITY_THRESHOLD
        self.repeat_window = repeat_window or REPEAT_WINDOW
        # pending: key -> [consecutive polls seen, best reading]
        self.pending = {}
        # spoken: key -> (last seen-or-spoken time, display text)
        self.spoken = {}

    # -- internals ---------------------------------------------------------

    def _find_spoken_match(self, k):
        if k in self.spoken:
            return k
        for s in self.spoken:
            if similar(k, s) >= self.similarity:
                return s
        return None

    def _find_pending_match(self, k):
        for p in self.pending:
            if similar(k, p) >= self.similarity:
                return p
        return None

    def _extension_of(self, text):
        """If `text` continues an already-spoken line, return
        (spoken_key, suffix_to_speak); else None. Word-level and
        punctuation-forgiving; tolerates a partially captured last word."""
        new_words = text.split()
        new_wkeys = word_keys(text)
        for sk, (st, stext) in self.spoken.items():
            sw = word_keys(stext)
            n = len(sw)
            if not sw or n >= len(new_wkeys):
                continue
            head = new_wkeys[:n]
            if head == sw:
                return sk, " ".join(new_words[n:])
            if (head[:-1] == sw[:-1] and sw[-1]
                    and head[-1].startswith(sw[-1])):
                return sk, " ".join(new_words[n - 1:])
        return None

    # -- public ------------------------------------------------------------

    def update(self, lines, now):
        out = []
        matched_pending = set()
        for raw in lines:
            ln = normalize(raw)
            if not ln:
                continue
            k = ln.lower()

            # 1) Already spoken and still visible (or a jitter variant):
            #    refresh suppression. Exception: strictly more words may be
            #    a genuine continuation -> let it reach the stability gate.
            m = self._find_spoken_match(k)
            if m is not None and (
                    len(word_keys(ln)) <= len(word_keys(self.spoken[m][1]))):
                self.spoken[m] = (now, self.spoken[m][1])
                continue

            # 2) FUZZY stability gate: similar readings on consecutive
            #    polls accumulate (moving video jitters OCR output;
            #    requiring exact repeats silences whole subtitles).
            pk = self._find_pending_match(k)
            if pk is None:
                self.pending[k] = [1, ln]
                matched_pending.add(k)
                if self.stable_frames > 1:
                    continue
                pk = k
            else:
                entry = self.pending[pk]
                entry[0] += 1
                # keep the longer reading; ties keep the earlier one
                # (fade-out garbage tends to arrive after the good read)
                if len(ln) > len(entry[1]):
                    entry[1] = ln
                matched_pending.add(pk)
                if entry[0] < self.stable_frames:
                    continue

            # 3) Stable: speak the best accumulated reading.
            best = self.pending.pop(pk)[1]
            matched_pending.discard(pk)
            bk = best.lower()
            m = self._find_spoken_match(bk)
            if m is not None and (
                    len(word_keys(best)) <= len(word_keys(self.spoken[m][1]))):
                self.spoken[m] = (now, self.spoken[m][1])
                continue
            ext = self._extension_of(best)
            if ext is not None:
                sk, suffix = ext
                del self.spoken[sk]
                self.spoken[bk] = (now, best)
                if suffix:
                    out.append(("suffix", suffix))
            else:
                self.spoken[bk] = (now, best)
                out.append(("line", best))

        # 4) Pending candidates not seen (even fuzzily) this poll vanished.
        for k in list(self.pending):
            if k not in matched_pending:
                del self.pending[k]

        # 5) Suppression expires only after a line has been GONE for
        #    repeat_window seconds (visible lines were refreshed above).
        for k in list(self.spoken):
            if now - self.spoken[k][0] > self.repeat_window:
                del self.spoken[k]

        return out


# ---------------------------------------------------------------------------
# OCR engine loading with fallback (Windows-only at runtime)
# ---------------------------------------------------------------------------


def load_engine():
    """Try OneOCR first, then legacy Windows OCR. Returns
    (recognize_fn(img) -> str, engine_display_name) or raises."""
    # 1) OneOCR (Snipping Tool engine): best accuracy, Windows 11
    try:
        import oneocr
        engine = oneocr.OcrEngine()

        def rec_oneocr(img):
            for name in ("recognize_pil", "recognize", "ocr", "run"):
                fn = getattr(engine, name, None)
                if callable(fn):
                    try:
                        return _result_text(fn(img))
                    except TypeError:
                        continue
            raise RuntimeError("No usable recognize method on oneocr engine")

        return rec_oneocr, "OneOCR"
    except Exception:
        log("OneOCR unavailable, trying legacy Windows OCR:\n"
            + traceback.format_exc())

    # 2) Legacy Windows OCR: lower accuracy, works on Windows 10, no setup
    import winocr

    def rec_winocr(img):
        return _result_text(winocr.recognize_pil_sync(img, OCR_LANG))

    return rec_winocr, "Windows OCR (legacy, reduced accuracy)"


def _result_text(result):
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        if isinstance(result.get("text"), str):
            return result["text"]
        if "lines" in result:
            return "\n".join(
                (ln.get("text", "") if isinstance(ln, dict)
                 else getattr(ln, "text", "") or "")
                for ln in result["lines"])
    t = getattr(result, "text", None)
    if isinstance(t, str):
        return t
    return ""


# ---------------------------------------------------------------------------
# Windows-only capture machinery. Import guards keep this module loadable
# on non-Windows platforms for unit tests.
# ---------------------------------------------------------------------------

try:
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
except Exception:  # not on Windows (unit tests)
    user32 = None

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


def virtual_screen():
    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return x, y, x + w, y + h


def primary_screen():
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def get_capture_region():
    """Bottom strip of the foreground window, validated; primary-screen
    strip as fallback. Returns (left, top, right, bottom)."""
    left = top = right = bottom = None
    hwnd = user32.GetForegroundWindow()
    if hwnd and not user32.IsIconic(hwnd):
        rect = wt.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            left, top = rect.left, rect.top
            right, bottom = rect.right, rect.bottom

    def degenerate(l, t, r, b):
        return l is None or (r - l) < 300 or (b - t) < 200

    if degenerate(left, top, right, bottom):
        left, top, right, bottom = primary_screen()

    vl, vt, vr, vb = virtual_screen()
    left, right = max(left, vl), min(right, vr)
    top, bottom = max(top, vt), min(bottom, vb)
    if (right - left) < 300 or (bottom - top) < 200:
        left, top, right, bottom = primary_screen()

    height = bottom - top
    strip_top = bottom - int(height * REGION_FRACTION)
    return (left, strip_top, right, bottom)


def safe_image(img):
    """Guarantee an RGB image of at least MIN_DIM in both dimensions."""
    from PIL import Image
    img = img.convert("RGB")
    w, h = img.size
    if w >= MIN_DIM and h >= MIN_DIM:
        return img
    canvas = Image.new("RGB", (max(w, MIN_DIM), max(h, MIN_DIM)), (0, 0, 0))
    canvas.paste(img, (0, 0))
    return canvas


def watch_stdin():
    """Exit when the parent (NVDA) closes our stdin."""
    try:
        sys.stdin.read()
    except Exception:
        pass
    import os
    os._exit(0)


def parse_args():
    global POLL_INTERVAL, REGION_FRACTION, STABLE_FRAMES
    global REPEAT_WINDOW, OCR_LANG
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=POLL_INTERVAL)
    p.add_argument("--region", type=int, default=int(REGION_FRACTION * 100))
    p.add_argument("--stable", type=int, default=STABLE_FRAMES)
    p.add_argument("--window", type=float, default=REPEAT_WINDOW)
    p.add_argument("--lang", type=str, default=OCR_LANG)
    a = p.parse_args()
    POLL_INTERVAL = max(0.1, min(2.0, a.interval))
    REGION_FRACTION = max(0.10, min(1.0, a.region / 100.0))
    STABLE_FRAMES = max(1, min(5, a.stable))
    REPEAT_WINDOW = max(2.0, min(60.0, a.window))
    OCR_LANG = a.lang


def main():
    parse_args()
    threading.Thread(target=watch_stdin, daemon=True).start()

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    try:
        from PIL import ImageGrab
        recognize, engine_name = load_engine()
    except Exception as e:
        log("Engine init failed:\n" + traceback.format_exc())
        emit({"type": "error",
              "message": "No OCR engine available. Install the oneocr "
                         "package (Windows 11) or winocr (Windows 10). "
                         f"Details: {e}"})
        sys.exit(1)

    emit({"type": "ready", "engine": engine_name})
    log(f"Engine ready ({engine_name}); interval={POLL_INTERVAL}s "
        f"region={int(REGION_FRACTION*100)}% stable={STABLE_FRAMES} "
        f"window={REPEAT_WINDOW}s")

    tracker = SubtitleTracker()
    consecutive_failures = 0

    while True:
        t0 = time.perf_counter()
        lines = []
        try:
            region = get_capture_region()
            img = ImageGrab.grab(bbox=region, all_screens=True)
            img = safe_image(img)
            raw = recognize(img)
            lines = [ln for ln in raw.split("\n") if ln.strip()]
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            log(f"Capture/OCR failure {consecutive_failures}:\n"
                + traceback.format_exc())
            if consecutive_failures == 5:
                emit({"type": "error", "message": f"OCR keeps failing: {e}"})

        try:
            for kind, text in tracker.update(lines, time.time()):
                emit({"type": "subtitle", "kind": kind, "text": text})
        except Exception:
            # The tracker must never kill the process; log and continue.
            log("Tracker error:\n" + traceback.format_exc())

        elapsed = time.perf_counter() - t0
        time.sleep(max(0.05, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        log("Fatal error in sidecar:\n" + traceback.format_exc())
        emit({"type": "error", "message": "Sidecar crashed; see log."})
        sys.exit(1)
