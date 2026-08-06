"""Test suite for the sidecar's SubtitleTracker.

Imports the ACTUAL shipped module (not a copy of its logic), so any
corruption or regression in the real file fails here immediately.
Run: python test_tracker.py
"""
import importlib.util
import os
import re
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

# 3. A single-frame misread following a spoken line stays silent
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

# 13. Same-scan lines batch into ONE utterance (interrupt-safety fix)
res = [("line", "I'm not the owner of this phone,"),
       ("line", "but if you're looking for this phone..."),
       ("suffix", "right now.")]
b = mod.batch_results(res)
check("same-scan lines batched",
      b == [("line", "I'm not the owner of this phone,\n"
                     "but if you're looking for this phone..."),
            ("suffix", "right now.")], b)
b2 = mod.batch_results([("line", "Hello?")])
check("single line unchanged", b2 == [("line", "Hello?")], b2)

# 14. RTL leading punctuation relocated to the end
ar = "\u0644\u0645 \u0623\u0638\u0646 \u0623\u0646\u0646\u0627 \u0633\u0646\u0635\u0644"
fixed = mod.fix_rtl_leading_punct("." + ar)
check("rtl leading dot relocated", fixed == ar + ".", fixed)
check("rtl clean line untouched",
      mod.fix_rtl_leading_punct(ar) == ar, None)
check("latin line untouched",
      mod.fix_rtl_leading_punct(".Wait here.") == ".Wait here.", None)
check("rtl multi punct relocated",
      mod.fix_rtl_leading_punct("...\u061F" + ar) == ar + "...\u061F", None)

# 15. NoiseFilter: exact-line-only plain text matching
nf = mod.NoiseFilter(["Yapim: Ay Yapim", "Skip Ad"])
check("plain exact-line match filtered", nf.is_noise("Yapim: Ay Yapim"))
check("plain text substring NOT filtered (exact-line only)",
      not nf.is_noise("Before Yapim: Ay Yapim after"))
check("unrelated line not filtered", not nf.is_noise("Hello there."))
check("case-insensitive plain match",
      nf.is_noise("skip ad"))

# 16. NoiseFilter: regex rules via "regex:" prefix
nf2 = mod.NoiseFilter([r"regex:^\d{1,2}:\d{2}$", "Wait."])
check("regex matches bare timestamp", nf2.is_noise("12:34"))
check("regex does not match dialogue",
      not nf2.is_noise("It's almost 12:34, we should go."))
check("plain rule still works alongside regex",
      nf2.is_noise("Wait."))

# 17. NoiseFilter: invalid regex is skipped, not fatal, and reported
nf3 = mod.NoiseFilter(["regex:([unclosed", "Good line stays"])
check("invalid regex recorded as an error", len(nf3.errors) == 1)
check("invalid regex does not crash matching",
      not nf3.is_noise("anything at all"))
check("good rules still work despite a bad one",
      nf3.is_noise("Good line stays"))

# 18. filter_lines splits kept vs dropped, preserving order
nf4 = mod.NoiseFilter(["NOISE"])
kept, dropped = nf4.filter_lines(["Hello.", "NOISE", "World."])
check("filter_lines keeps good lines in order",
      kept == ["Hello.", "World."], kept)
check("filter_lines collects dropped lines", dropped == ["NOISE"], dropped)

# 19. Built-in patterns registry sanity: every entry compiles
PLUGIN_SRC = open(os.path.join(os.path.dirname(__file__), "addon",
                               "globalPlugins", "hardSubReader",
                               "__init__.py"), newline="").read()

# The rules the add-on actually passes to the helper. Parsed from
# source so the tests cannot pass against a copy that production
# does not use.
BUILTIN_RULES = dict(
    (k, r) for k, r in re.findall(
        r'\("(\w+)",.*?(?:r?"((?:regex|builtin):[^"]*)")\),',
        PLUGIN_SRC, re.S))
check("built-in rules were found in the add-on source",
      len(BUILTIN_RULES) == 4, sorted(BUILTIN_RULES))

for key, rule in BUILTIN_RULES.items():
    check(f"builtin '{key}' rule is well-formed",
          (rule.startswith("regex:") or rule.startswith("builtin:"))
          and mod.NoiseFilter([rule]).errors == [])

# 20. Two lines of ONE subtitle must never merge, however similar.
#     Two similar lines displayed together must produce two utterances.
a = "- come here"
b = "- come here?"
tr = mod.SubtitleTracker()
out = tr.update([a, b], 0.0) + tr.update([a, b], 0.3)
check("co-visible similar lines stay separate", len(out) == 2, out)

# 21. A line OCR misses for a single scan must still be spoken.
#     A single missed frame must not reset the stability counter.
top, bot = "top line of subtitle", "bottom line of subtitle"
tr = mod.SubtitleTracker()
got = []
for scan in ([top, bot], [bot], [top, bot], [top, bot]):
    got += tr.update(scan, 0.0)
check("line survives one missed scan",
      any(top in t for _k, t in got), got)

# 22. A single-frame misread must still be rejected.
tr = mod.SubtitleTracker()
got = []
for scan in (["steady line"], ["steady line", "XQZ"], ["steady line"],
             ["steady line"], ["steady line"]):
    got += tr.update(scan, 0.0)
check("single-frame garbage still rejected",
      not any("XQZ" in t for _k, t in got), got)

# 23. Built-in filters must not remove dialogue in any writing system.
nfb = mod.NoiseFilter(list(BUILTIN_RULES.values()))
for phrase in ["wait for me now", "Alexander", "Hello?", "Wait.",
               "\u0623\u0647\u0644\u0627!", "\u0643\u062a\u0627\u0628 \u062c\u062f\u064a\u062f"]:
    check(f"builtins keep dialogue {phrase!r}",
          not nfb.is_noise(phrase))

# 24. On-screen text that is not dialogue is still removed.
for junk in ["CHANNEL NAME", "STUDIO", "12:34", "0:15 / 1:00:00",
             "S", "D", "0", "..."]:
    check(f"builtins drop noise {junk!r}", nfb.is_noise(junk))

# 25. Regex rules are case-SENSITIVE; plain phrases are not.
nfc = mod.NoiseFilter([r"regex:^[A-Z]{3,}$", "hello there"])
check("regex rule respects case", nfc.is_noise("ABC")
      and not nfc.is_noise("abc"))
check("plain phrase ignores case", nfc.is_noise("Hello There"))

# 26. Language filter: keeps the chosen script, drops the rest,
#     and is inert when unset. Checked across several scripts.
lang_cases = [
    ("ar", "\u0645\u0631\u062d\u0628\u0627 \u0628\u0643", False),
    ("ar", "CHANNEL NAME", True),
    ("ja", "\u3053\u3093\u306b\u3061\u306f", False),
    ("ja", "Forward World", True),
    ("ko", "\uc548\ub155\ud558\uc138\uc694", False),
    ("th", "\u0E2A\u0E27\u0E31\u0E2A", False),
    ("ta", "\u0BB5\u0BA3\u0B95\u0BCD\u0B95\u0BAE\u0BCD", False),
    ("ru", "\u041F\u0440\u0438\u0432\u0435\u0442", False),
    ("en", "Hello there", False),
    ("en", "\u0645\u0631\u062d\u0628\u0627 \u0628\u0643", True),
    ("en-US", "Hello", False),
    ("", "\u0645\u0631\u062d\u0628\u0627 \u0628\u0643", False),
    ("zz", "anything at all", False),
]
for code, line, want in lang_cases:
    check(f"lang {code!r} vs {line[:10]!r}",
          mod.LanguageFilter(code).wrong_language(line) == want)

# 27. Language coverage is not a hand-picked shortlist: every ISO
#     639-1 code resolves, a script name may be typed directly, and
#     an unknown code stays inert rather than muting everything.
iso6391 = ("aa ab af ak am ar as av ay az ba be bg bh bi bm bn bo br "
           "bs ca ce ch co cr cs cu cv cy da de dv dz ee el en eo es "
           "et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho "
           "hr ht hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg "
           "ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo "
           "lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl "
           "nn no nr nv ny oc oj om or os pa pi pl ps pt qu rm rn ro "
           "ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv "
           "sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz "
           "ve vi vo wa wo xh yi yo za zh zu").split()
unresolved = [c for c in iso6391 if not mod.LanguageFilter(c).active]
check(f"every ISO 639-1 code resolves ({len(iso6391)} codes)",
      unresolved == [], unresolved)
check("script name accepted directly",
      mod.LanguageFilter("arabic").active
      and mod.LanguageFilter("japanese").active)
check("unknown code stays inert and is flagged",
      mod.LanguageFilter("zz").unknown
      and not mod.LanguageFilter("zz").wrong_language("anything"))
check("empty code is not flagged as unknown",
      not mod.LanguageFilter("").unknown)
check("uppercase and regional forms work",
      mod.LanguageFilter("AR").active
      and mod.LanguageFilter("ar-SA").active
      and mod.LanguageFilter("en_US").active)

# 28. Same-script languages: only dropped on positive evidence.
same_script = [
    ("tr", "bu bir kelime dizisi", False),
    ("tr", "Arkadaslar bu bir sey degil", False),
    ("tr", "The end of the story is that you have not", True),
    ("tr", "Directed by someone and produced with care", True),
    ("en", "The quick brown fox is not here", False),
    ("en", "Bu bir sey degil ve daha cok var", True),
    ("ru", "\u044f \u043d\u0435 \u0437\u043d\u0430\u044e "
           "\u0447\u0442\u043e \u044d\u0442\u043e", False),
    ("ru", "\u044f \u043d\u0435 \u0437\u043d\u0430\u044e "
           "\u0449\u043e \u0446\u0435 \u0457\u0457", True),
    ("ar", "\u0641\u064a \u0645\u0646 \u0639\u0644\u0649 "
           "\u0623\u0646 \u0644\u0627", False),
    ("ar", "\u0648 \u062f\u0631 \u0628\u0647 \u0627\u0632 "
           "\u06a9\u0647 \u0627\u06cc\u0646 \u06af", True),
]
for code, line, want in same_script:
    check(f"same-script {code!r} vs {line[:16]!r}",
          mod.LanguageFilter(code).wrong_language(line) == want)

# 29. Short or neutral same-script lines are ALWAYS kept: never
#     lose dialogue to a guess.
for line in ["Subscribe", "Istanbul", "Ankara 2024", "OK", "Hello"]:
    check(f"short line kept under tr: {line!r}",
          not mod.LanguageFilter("tr").wrong_language(line))

# 30. A language with no hint data never drops same-script lines.
check("language without hints is script-only",
      not mod.LanguageFilter("la").wrong_language("Hello there friend"))

# 31. Lines without letters are never judged by language.
check("language filter ignores letterless lines",
      not mod.LanguageFilter("ar").wrong_language("12:34"))

# 32. The "Restore defaults" values must match the declared config
#     defaults exactly, and must not touch engine bookkeeping.
plugin_src = open(os.path.join(os.path.dirname(__file__), "addon",
                               "globalPlugins", "hardSubReader",
                               "__init__.py"), newline="").read()
spec_defaults = dict(re.findall(
    r'"(\w+)":\s*"(?:float|integer|boolean|string)\(default=([^,)]+)',
    plugin_src))
reset_block = re.search(r"RESETTABLE_DEFAULTS = \{(.*?)\n\}",
                        plugin_src, re.S).group(1)
reset_values = dict(re.findall(r'"(\w+)":\s*([^,\n]+),', reset_block))

def _norm(v):
    return str(v).strip().strip("'\"")

mismatched = [k for k, v in reset_values.items()
              if _norm(spec_defaults.get(k, "<absent>")) != _norm(v)]
check("restore-defaults values match the config spec",
      mismatched == [], mismatched)
check("restore-defaults leaves engine bookkeeping alone",
      "preferredHelper" not in reset_values
      and "engineSetupOffered" not in reset_values)
check("every user setting is resettable",
      set(spec_defaults) - set(reset_values)
      == {"preferredHelper", "engineSetupOffered"},
      sorted(set(spec_defaults) - set(reset_values)))

# 33. The rules the add-on sends must not remove short words or text
#     in writing systems that use no Latin, Arabic or Cyrillic
#     letters. Checked against the add-on source, not a copy.
live = mod.NoiseFilter(list(BUILTIN_RULES.values()))
for sample in ["\u0645\u0627 \u0647\u0648 \u061F",
               "\u3053\u3093\u306b\u3061\u306f",
               "\u0BB5\u0BA3\u0B95\u0BCD\u0B95\u0BAE\u0BCD",
               "\uc548\ub155\ud558\uc138\uc694",
               "\u0E2A\u0E27\u0E31\u0E2A\u0E14\u0E35",
               "\u0393\u03b5\u03b9\u03ac \u03c3\u03bf\u03c5"]:
    check(f"live rules keep {sample[:8]!r}", not live.is_noise(sample))

check("live rules still remove non-dialogue text",
      live.is_noise("CHANNEL NAME") and live.is_noise("12:34")
      and live.is_noise("S"))

# 33. The rules the add-on sends must keep dialogue and remove
#     non-dialogue, in every writing system. These use the rules
#     parsed from the add-on source, not a copy.
shipped = mod.NoiseFilter(list(BUILTIN_RULES.values()))
for phrase in ["\u0645\u0627 \u0647\u0648 \u061F",
               "\u3053\u3093\u306b\u3061\u306f",
               "\u0BB5\u0BA3\u0B95\u0BCD\u0B95\u0BAE\u0BCD",
               "\u0623\u0647\u0644\u0627!",
               "wait for me now", "Alexander", "Hello?"]:
    check(f"shipped rules keep dialogue {phrase!r}",
          not shipped.is_noise(phrase))
for junk in ["CHANNEL NAME", "STUDIO", "12:34", "S", "...",
             "0:15 / 1:00:00"]:
    check(f"shipped rules remove {junk!r}", shipped.is_noise(junk))
check("no shipped rule fails to compile", shipped.errors == [],
      shipped.errors)

# 35. Every command-line setting must actually reach the helper.
#     These are applied by parse_args, which the rest of the suite
#     never calls, so a setting could be silently discarded while
#     all other tests passed.
_argv = sys.argv
sys.argv = ["helper", "--interval", "0.8", "--region", "100",
            "--stable", "3", "--window", "25", "--lang", "ar",
            "--only-lang", "--detailed-log", "--hwnd", "1234"]
try:
    mod.parse_args()
finally:
    sys.argv = _argv
for name, want in [("POLL_INTERVAL", 0.8), ("REGION_FRACTION", 1.0),
                   ("STABLE_FRAMES", 3), ("REPEAT_WINDOW", 25.0),
                   ("OCR_LANG", "ar"), ("LANG_FILTER_ON", True),
                   ("DETAILED_LOG", True), ("LOCK_HWND", 1234)]:
    check(f"setting {name} reaches the helper",
          getattr(mod, name) == want, getattr(mod, name))

print(f"\nAll {passed} tests passed against the real shipped module.")
