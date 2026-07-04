"""Test suite for the sidecar's SubtitleTracker.

Imports the ACTUAL shipped module (not a copy of its logic), so any
corruption or regression in the real file fails here immediately.
Run: python test_tracker.py
"""
import importlib.util
import sys
from pathlib import Path

SIDECAR = Path(__file__).parent / "addon" / "sidecar" / "subtitle_ocr_server.py"
spec = importlib.util.spec_from_file_location("sidecar", SIDECAR)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # must import cleanly even off-Windows

POLL = 0.3


def run(script):
    """script: list of (repeat_count, [lines]) steps."""
    tr = mod.SubtitleTracker()
    t = 0.0
    events = []
    for count, lines in script:
        for _ in range(count):
            events.extend(tr.update(lines, t))
            t += POLL
    return events


passed = 0

def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        sys.exit(1)


# 1. Basic: a line spoken exactly once with ~0.6s latency
ev = run([(4, ["I never lied to you."]), (2, [])])
check("basic single line", ev == [("line", "I never lied to you.")], ev)

# 2. Jittered readings every poll (busy background) must be spoken once
ev = run([(1, ["The storm is coming tonight."]),
          (1, ["The st0rm is coming tonight,"]),
          (1, ["The storm ls coming tonight."]),
          (2, [])])
texts = [x[1] for x in ev]
check("jitter accumulates (no silence)",
      sum(1 for x in texts if "storm" in x.lower()) == 1, ev)

# 3. One-frame fade garbage after a spoken line stays silent
ev = run([(3, ["The truth was buried with him."]),
          (1, ["Tne trutl was buriecl witl hin,"]),
          (2, [])])
check("fade garbage filtered",
      [x[1] for x in ev] == ["The truth was buried with him."], ev)

# 4. Growth: partial then full -> one line + one suffix, no stutter
ev = run([(2, ["You promised me"]),
          (1, ["You promised me you'd stay."]),
          (3, ["You promised me you'd stay."]),
          (2, [])])
check("extension no stutter",
      ev == [("line", "You promised me"), ("suffix", "you'd stay.")], ev)

# 5. Punctuated partial (the original stutter report)
ev = run([(2, ["I can."]), (3, ["I can do that."]), (2, [])])
check("punctuation-forgiving extension",
      ev == [("line", "I can."), ("suffix", "do that.")], ev)

# 6. Persistent credit + changing subtitles: credit exactly once
CREDIT = "Yap\u0131m: Ay Yap\u0131m"
ev = run([(4, ["Wait.", CREDIT]),
          (4, ["What did you say?", CREDIT]),
          (4, ["Nothing. Forget it.", CREDIT]),
          (2, [])])
texts = [x[1] for x in ev]
check("credit spoken once", texts.count(CREDIT) == 1, ev)
check("all dialogue through with credit on screen",
      all(x in texts for x in
          ("Wait.", "What did you say?", "Nothing. Forget it.")), ev)

# 7. Flicker (one empty poll) must not repeat
ev = run([(4, ["Run!"]), (1, []), (3, ["Run!"]), (2, [])])
check("flicker no repeat", [x[1] for x in ev] == ["Run!"], ev)

# 8. Genuine repeat after the window must be spoken again
tr = mod.SubtitleTracker()
t = 0.0; events = []
for _ in range(4): events.extend(tr.update(["Get out!"], t)); t += POLL
for _ in range(40): events.extend(tr.update([], t)); t += POLL  # 12s gap
for _ in range(4): events.extend(tr.update(["Get out!"], t)); t += POLL
check("repeat after window",
      [x[1] for x in events] == ["Get out!", "Get out!"], events)

# 9. Fast dialogue: 0.9s lines all caught
ev = run([(3, ["Wait."]), (3, ["What?"]), (3, ["Nothing."]), (2, [])])
check("fast dialogue caught",
      [x[1] for x in ev] == ["Wait.", "What?", "Nothing."], ev)

# 10. Turkish / non-Latin text flows through untouched
ev = run([(3, ["\u0130yi geceler, g\u00f6r\u00fc\u015f\u00fcr\u00fcz."]), (2, [])])
check("non-Latin text", ev[0][1] == "\u0130yi geceler, g\u00f6r\u00fc\u015f\u00fcr\u00fcz.", ev)

# 11. Two-line subtitle -> both lines, each once
ev = run([(3, ["If you tell anyone,", "I can't protect you."]), (2, [])])
check("two-line subtitle",
      sorted(x[1] for x in ev) == sorted(
          ["If you tell anyone,", "I can't protect you."]), ev)

# 12. Empty input forever -> total silence, no errors
ev = run([(20, [])])
check("silence on empty", ev == [], ev)

print(f"\nAll {passed} tests passed against the real shipped module.")
