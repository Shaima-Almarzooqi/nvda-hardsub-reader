"""Subtitle OCR sidecar server.

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
import re
import json
import os
import platform
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
REGION_FRACTION = 0.33   # matches the default offered in the settings
STABLE_FRAMES = 2
SIMILARITY_THRESHOLD = 0.85
REPEAT_WINDOW = 8.0
OCR_LANG = "en"
LANG_FILTER_ON = False
DETAILED_LOG = False
MIN_DIM = 64                 # pad captures below this size (DLL crash guard)

_PUNCT = ".,!?;:\"'\u2026\u2019\u2018\u201c\u201d-\u2013\u2014()[]"

# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def log(msg):
    sys.stderr.write(time.strftime("%H:%M:%S ") + msg + "\n")
    sys.stderr.flush()


def detail(msg):
    """Write a line only when detailed logging is switched on.

    Detailed logging records the text that was recognised, so it is
    off by default and enabled per session from the settings panel.
    """
    if DETAILED_LOG:
        log(msg)


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def normalize(s):
    repl = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
            "\u2013": "-", "\u2014": "-"}
    for a, b in repl.items():
        s = s.replace(a, b)
    return " ".join(s.split())


# How many consecutive scans a pending line may be missing before it is
# forgotten. 1 = tolerate a single dropped frame.
PENDING_GRACE_SCANS = 1

# Same-script language hints. A line is only judged when it has at
# least this many words, another language must reach this much
# evidence, and must beat the chosen language by this margin.
MIN_WORDS_FOR_LANG_HINT = 3
# How many separate words must carry a language's distinctive
# letters before those letters alone are treated as evidence about
# the whole line. One or two such words are usually names.
MIN_CHAR_WORDS = 3
MIN_HINT_EVIDENCE = 2
HINT_MARGIN = 2


def similar(a, b):
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


# ---------------------------------------------------------------------------
# Script and language handling. Every rule here is script-generic: it must
# behave identically for all supported writing systems, with no alphabet
# treated as a special case.
# ---------------------------------------------------------------------------

# Ordered ranges: (script name, first codepoint, last codepoint).
_SCRIPT_RANGES = (
    ("latin", 0x0041, 0x024F),
    ("greek", 0x0370, 0x03FF),
    ("greek", 0x1F00, 0x1FFF),
    ("cyrillic", 0x0400, 0x052F),
    ("armenian", 0x0530, 0x058F),
    ("hebrew", 0x0590, 0x05FF),
    ("arabic", 0x0600, 0x06FF),
    ("arabic", 0x0750, 0x077F),
    ("arabic", 0x08A0, 0x08FF),
    ("arabic", 0xFB50, 0xFDFF),   # presentation forms
    ("arabic", 0xFE70, 0xFEFF),   # presentation forms-B
    ("syriac", 0x0700, 0x074F),
    ("thaana", 0x0780, 0x07BF),
    ("devanagari", 0x0900, 0x097F),
    ("bengali", 0x0980, 0x09FF),
    ("gurmukhi", 0x0A00, 0x0A7F),
    ("gujarati", 0x0A80, 0x0AFF),
    ("oriya", 0x0B00, 0x0B7F),
    ("tamil", 0x0B80, 0x0BFF),
    ("telugu", 0x0C00, 0x0C7F),
    ("kannada", 0x0C80, 0x0CFF),
    ("malayalam", 0x0D00, 0x0D7F),
    ("sinhala", 0x0D80, 0x0DFF),
    ("thai", 0x0E00, 0x0E7F),
    ("lao", 0x0E80, 0x0EFF),
    ("tibetan", 0x0F00, 0x0FFF),
    ("myanmar", 0x1000, 0x109F),
    ("georgian", 0x10A0, 0x10FF),
    ("ethiopic", 0x1200, 0x137F),
    ("khmer", 0x1780, 0x17FF),
    ("hangul", 0x1100, 0x11FF),
    ("hangul", 0xAC00, 0xD7AF),
    ("hangul", 0x3130, 0x318F),
    ("kana", 0x3040, 0x309F),
    ("kana", 0x30A0, 0x30FF),
    ("han", 0x3400, 0x4DBF),
    ("han", 0x4E00, 0x9FFF),
    ("han", 0xF900, 0xFAFF),
    ("canadian", 0x1400, 0x167F),   # Inuktitut, Cree
    ("yi", 0xA000, 0xA48F),
    ("cherokee", 0x13A0, 0x13FF),
    ("mongolian", 0x1800, 0x18AF),
    ("nko", 0x07C0, 0x07FF),
)

# Primary language subtag -> accepted scripts.
#
# Built from compact per-script lists so it covers EVERY ISO 639-1 code
# (plus widely used three-letter ones), not a hand-picked subset. Any
# language whose code is not listed is handled by the fallbacks below,
# and a script name may also be typed directly.
_SCRIPT_LANGS = {
    "arabic": "ar fa ur ps ks sd ug ku arb pes urd pus snd uig ckb fas",
    "hebrew": "he yi iw heb yid",
    "cyrillic": ("ru uk bg sr mk be kk ky tg mn ab av ba ce cu cv kv os tt "
                 "rus ukr bel bul srp mkd kaz kir tgk mon tat"),
    "greek": "el ell gre grc",
    "armenian": "hy hye arm",
    "georgian": "ka kat geo",
    "ethiopic": "am ti amh tir",
    "devanagari": "hi mr ne sa bh pi hin mar nep san bho mai",
    "bengali": "bn as ben asm",
    "gurmukhi": "pa pan",
    "gujarati": "gu guj",
    "oriya": "or ori ory",
    "tamil": "ta tam",
    "telugu": "te tel",
    "kannada": "kn kan",
    "malayalam": "ml mal",
    "sinhala": "si sin",
    "thai": "th tha",
    "lao": "lo lao",
    "tibetan": "bo dz bod tib dzo",
    "myanmar": "my mya bur",
    "khmer": "km khm",
    "thaana": "dv div",
    "syriac": "syr syc",
    "canadian": "iu cr iku cre",
    "yi": "ii iii",
    "cherokee": "chr",
    "mongolian": "mvf",
    "nko": "nqo",
    "han": "zh zho chi cmn yue nan hak wuu",
}
# Languages that mix scripts.
_MULTI_SCRIPT_LANGS = {
    "ja": ("kana", "han"), "jpn": ("kana", "han"),
    "ko": ("hangul", "han"), "kor": ("hangul", "han"),
}

_LANG_SCRIPTS = {}
for _script, _codes in _SCRIPT_LANGS.items():
    for _c in _codes.split():
        _LANG_SCRIPTS[_c] = (_script,)
_LANG_SCRIPTS.update(_MULTI_SCRIPT_LANGS)

# Every remaining ISO 639-1 language is written in the Latin alphabet.
for _c in ("aa af ak an ay az bi bm br bs ca ch co cs cy da de ee en eo es "
           "et eu ff fi fj fo fr fy ga gd gl gn gv ha ho hr ht hu hz ia id "
           "ie ig ik io is it jv kg ki kj kl kr kw la lb lg li ln lt lu lv "
           "mg mh mi ms mt na nb nd ng nl nn no nr nv ny oc oj om pl pt qu "
           "rm rn ro rw sc se sg sk sl sm sn so sq ss st su sv sw tk tl tn "
           "to tr ts tw ty uz ve vi vo wa wo xh yo za zu "
           "eng fra fre deu ger spa ita por nld dut swe dan nor fin isl tur "
           "pol ces cze slk slv hrv hun ron rum lit lav est sqi alb eus baz "
           "cat glg ind msa may vie tgl fil swa afr zul xho som hau yor ibo "
           "aze uzb tuk kur lat epo").split():
    _LANG_SCRIPTS.setdefault(_c, ("latin",))

# A script name may be entered directly, for anything not covered above.
_KNOWN_SCRIPTS = {name for name, _lo, _hi in _SCRIPT_RANGES}
for _s in _KNOWN_SCRIPTS:
    _LANG_SCRIPTS.setdefault(_s, (_s,))
_LANG_SCRIPTS.setdefault("japanese", ("kana", "han"))
_LANG_SCRIPTS.setdefault("korean", ("hangul", "han"))
_LANG_SCRIPTS.setdefault("chinese", ("han",))
_LANG_SCRIPTS.setdefault("katakana", ("kana",))
_LANG_SCRIPTS.setdefault("hiragana", ("kana",))


def script_of(ch):
    """Script name for a character, or None if it is not a letter."""
    cp = ord(ch)
    for name, lo, hi in _SCRIPT_RANGES:
        if lo <= cp <= hi:
            return name
    return None


def has_letters(text):
    """True if the text contains at least one letter in ANY script."""
    return any(script_of(c) for c in text)


def letter_count(text):
    """Number of letters in any writing system.

    Used to identify fragments too short to be a word, while leaving
    genuine one- and two-letter interjections intact."""
    return sum(1 for c in text if script_of(c))


def script_histogram(text):
    counts = {}
    for c in text:
        s = script_of(c)
        if s:
            counts[s] = counts.get(s, 0) + 1
    return counts


def _letter_scripts(text):
    """Return scripts for actual letters in *text*.

    ``script_of`` deliberately uses broad Unicode blocks, some of which
    also contain digits and punctuation. Word-level cleanup must not
    mistake those characters for words, so it narrows the result with
    ``isalpha`` first.
    """
    out = []
    for ch in text:
        if not ch.isalpha():
            continue
        script = script_of(ch)
        if script:
            out.append(script)
    return out


# ---------------------------------------------------------------------------
# Same-script language hints.
#
# Script matching cannot separate languages that share an alphabet. These
# tables provide a second, weaker signal: letters used by only some
# languages, plus common function words.
#
# The decision is asymmetric by design. A line is discarded only when
# there is positive evidence that it belongs to a different language;
# absence of evidence means the line is kept. Short or ambiguous lines
# are therefore always read.
# ---------------------------------------------------------------------------

# language -> (distinctive letters, very common short words)
_LANG_HINTS = {
    # --- Latin script -----------------------------------------------------
    "en": ("", "the and of to is it in that you for was with this have not"),
    "tr": ("\u0131\u011f\u015f\u0130", "bir ve bu ile i\u00e7in ama de\u011fil "
           "\u00e7ok daha ne var gibi"),
    "es": ("\u00f1\u00bf\u00a1", "el la los las que de en un una por con no para "
           "es se lo"),
    "fr": ("\u0153", "le la les des que de est pas une pour dans qui vous je "
           "ne au"),
    "de": ("\u00df", "der die das und ist nicht ein eine mit sich auf von zu "
           "den dem"),
    "it": ("", "il la che di non per una sono con come pi\u00f9 sono nel gli"),
    "pt": ("\u00e3\u00f5", "o a que de para com uma n\u00e3o em por mais "
           "como est\u00e1 ele"),
    "nl": ("\u0132", "de het een van dat is niet en op voor met zijn maar ook"),
    "pl": ("\u0142\u0105\u0119\u017c\u017a\u0107\u0144\u015b",
           "nie jest to sie na do jak ale czy tak juz tylko"),
    "cs": ("\u0159\u016f\u011b", "je na se to ne ale jak co za tak jsem by"),
    "hu": ("\u0151\u0171", "az egy hogy nem is de meg csak van ez ki mint"),
    "ro": ("\u0103\u021b\u0219", "si de la nu ce el un o pe cu care este"),
    "sv": ("", "och att det som en av jag inte har den vi till"),
    "da": ("", "og det er en til at jeg ikke den med for af"),
    "no": ("", "og det er en til at jeg ikke den med for av"),
    "fi": ("", "on ei ja se ett\u00e4 en n\u00e4in mutta kun niin voi"),
    "id": ("", "yang dan di ini itu tidak untuk dengan dari saya kamu ada"),
    "ms": ("", "yang dan di ini itu tidak untuk dengan dari saya awak ada"),
    "vi": ("\u01b0\u01a1\u0111\u0103\u00e2\u00ea\u00f4"
           # tone-marked vowels, which carry most Vietnamese text
           "\u1ea1\u1ea3\u1ea5\u1ea7\u1ea9\u1eab\u1ead\u1eaf"
           "\u1eb1\u1eb3\u1eb5\u1eb7\u1eb9\u1ebb\u1ebd\u1ebf"
           "\u1ec1\u1ec3\u1ec5\u1ec7\u1ec9\u1ecb\u1ecd\u1ecf"
           "\u1ed1\u1ed3\u1ed5\u1ed7\u1ed9\u1edb\u1edd\u1edf"
           "\u1ee1\u1ee3\u1ee5\u1ee7\u1ee9\u1eeb\u1eed\u1eef"
           "\u1ef1\u1ef3\u1ef5\u1ef7\u1ef9",
           "khong la co cua toi nguoi va duoc mot nay cho"),
    "az": ("\u0259\u0131\u011f\u015f", "bir ve bu ile ucun amma deyil cox daha ne"),
    "hr": ("\u0111\u010d\u0107\u017e\u0161", "je ne se to na da li ali kako "
           "sto sam bi"),
    "tl": ("", "ang ng sa na ay mga ko po hindi ito para may"),
    "sw": ("", "na ya wa kwa ni katika hii yake sana kama lakini"),
    "af": ("", "die en van is nie het wat vir met om ook maar"),
    # --- Cyrillic script --------------------------------------------------
    "ru": ("\u044d\u044a\u044b", "\u0438 \u0432 \u043d\u0435 \u043d\u0430 "
           "\u044f \u0447\u0442\u043e \u043e\u043d \u0441 \u043a\u0430\u043a "
           "\u044d\u0442\u043e \u0442\u044b \u043c\u044b"),
    "uk": ("\u0456\u0457\u0454\u0491", "\u0456 \u0432 \u043d\u0435 "
           "\u043d\u0430 \u044f \u0449\u043e \u0432\u0456\u043d "
           "\u0437 \u044f\u043a \u0446\u0435 \u0442\u0438"),
    "bg": ("\u044a", "\u0438 \u0432 \u043d\u0435 \u043d\u0430 \u0430\u0437 "
           "\u0447\u0435 \u0442\u043e\u0439 \u0441\u044a\u0441 "
           "\u043a\u0430\u043a \u0442\u043e\u0432\u0430"),
    "sr": ("\u0452\u0459\u045a\u045b\u045f", "\u0438 \u0443 \u043d\u0435 "
           "\u043d\u0430 \u0458\u0430 \u0434\u0430 \u043e\u043d "
           "\u0441\u0430 \u043a\u0430\u043a\u043e \u0442\u043e"),
    "mk": ("\u0453\u045c\u0455", "\u0438 \u0432\u043e \u043d\u0435 "
           "\u043d\u0430 \u0458\u0430\u0441 \u0434\u0430 \u0442\u043e\u0458 "
           "\u0441\u043e \u043a\u0430\u043a\u043e"),
    # --- Arabic script ----------------------------------------------------
    "ar": ("\u0629", "\u0641\u064a \u0645\u0646 \u0639\u0644\u0649 "
           "\u0623\u0646 \u0644\u0627 \u0645\u0627 \u0647\u0630\u0627 "
           "\u0627\u0644\u0649 \u0647\u0644 \u0642\u062f"),
    "fa": ("\u067e\u0686\u0698\u06af\u06cc\u06a9",
           "\u0648 \u062f\u0631 \u0628\u0647 \u0627\u0632 "
           "\u06a9\u0647 \u0627\u06cc\u0646 \u0628\u0627 "
           "\u0631\u0627 \u0645\u0646"),
    "ur": ("\u0679\u0688\u0691\u06ba\u06d2\u06c1",
           "\u06a9\u06d2 \u06a9\u06cc \u06a9\u0627 \u0645\u06cc\u06ba "
           "\u06c1\u06d2 \u0633\u06d2 \u0646\u06c1 \u06c1\u0648"),
}


_HINT_RIVALS = {}
for _l in _LANG_HINTS:
    _sc = _LANG_SCRIPTS.get(_l)
    if _sc:
        _HINT_RIVALS.setdefault(_sc[0], []).append(_l)


def _hint_evidence(text, lang):
    """Evidence that `text` is written in `lang`, as two separate counts.

    The two kinds of evidence behave very differently and must not be
    added together blindly:

    * Function words belong to a language's grammar. They appear in
      sentences written in that language and essentially never inside a
      sentence written in another one.
    * Distinctive letters travel with individual words. Proper names,
      place names and borrowed words carry them across language
      boundaries all the time, so on their own they say little about
      what language the line is written in.

    Returned as (function-word hits, number of words carrying a
    distinctive letter) so the caller can weigh them appropriately.
    """
    hint = _LANG_HINTS.get(lang)
    if not hint:
        return 0, 0
    chars, words = hint
    wordset = set(words.split())
    keys = word_keys(text)
    func_hits = sum(1 for w in keys if w in wordset)
    char_words = 0
    if chars:
        charset = set(chars)
        char_words = sum(1 for w in keys
                         if any(c in charset for c in w))
    return func_hits, char_words


def _hint_score(text, lang):
    """Combined weight of the evidence, used only for ranking."""
    func_hits, char_words = _hint_evidence(text, lang)
    return func_hits + 2 * char_words


def _is_credible_language(text, lang):
    """Whether the evidence is strong enough to conclude that a line is
    written in `lang`, rather than merely containing a word from it.

    Either the line uses that language's grammar, or its distinctive
    letters are spread across enough of the line that they cannot be
    explained by one or two names.
    """
    func_hits, char_words = _hint_evidence(text, lang)
    if func_hits >= 1:
        return True
    total = len(word_keys(text))
    return (char_words >= MIN_CHAR_WORDS
            and char_words * 2 >= total)


class LanguageFilter:
    """Optional single-language mode.

    When a language code is set, words in other scripts are removed before
    a line reaches the tracker. This handles OCR joining credit text to a
    subtitle: the credit disappears and changing credit text can no longer
    make the subtitle look new. Languages sharing the selected script are
    never stripped word by word; the conservative whole-line hint check
    still handles those. Lines with no letters at all (numbers, symbols)
    are left alone for the noise filter to judge. Unset/unknown code = keep
    everything, exactly as before.
    """

    def __init__(self, code):
        self.code = (code or "").strip()
        primary = self.code.replace("_", "-").split("-")[0].lower()
        self.primary = primary
        self.scripts = _LANG_SCRIPTS.get(primary)
        self.active = bool(self.scripts)
        self.rewritten = []
        # A code was supplied but is not recognised. Remain inactive
        # rather than filtering everything, and let the caller report.
        self.unknown = bool(self.code) and not self.active

    def _clean_word(self, word):
        """Remove foreign-script letter runs from one OCR word.

        OCR normally separates credits and subtitles with whitespace, but
        it can glue the two scripts into one token. A word containing no
        selected-script letters is dropped altogether. In a mixed word,
        only foreign-script letters and an attached foreign prefix/suffix
        are removed. Same-script words are untouched.
        """
        letters = []
        for index, ch in enumerate(word):
            if not ch.isalpha():
                continue
            script = script_of(ch)
            if script:
                letters.append((index, script))
        if not letters:
            return word

        target_positions = [i for i, script in letters
                            if script in self.scripts]
        if not target_positions:
            return ""
        if all(script in self.scripts for _i, script in letters):
            return word

        first_target = target_positions[0]
        last_target = target_positions[-1]
        foreign_before = [i for i, script in letters
                          if script not in self.scripts and i < first_target]
        foreign_after = [i for i, script in letters
                         if script not in self.scripts and i > last_target]
        start = first_target if foreign_before else 0
        end = (min(foreign_after) - 1
               if foreign_after else len(word) - 1)

        cleaned = []
        for index in range(start, end + 1):
            ch = word[index]
            script = script_of(ch) if ch.isalpha() else None
            if script and script not in self.scripts:
                continue
            cleaned.append(ch)
        return "".join(cleaned)

    def clean_line(self, line):
        """Return tracker-ready text, or ``None`` when it should be dropped."""
        if not self.active:
            return line

        original_scripts = _letter_scripts(line)
        if not original_scripts:
            return line          # no letters: not ours to judge

        words = []
        for word in line.split():
            cleaned = self._clean_word(word)
            if cleaned:
                words.append(cleaned)
        result = " ".join(words)

        # The line contained letters, but stripping left no letters in the
        # requested script. Drop the entire residue, including numbers or
        # punctuation that happened to share the line.
        if not any(script in self.scripts
                   for script in _letter_scripts(result)):
            return None
        if self._wrong_same_script(result):
            return None
        return result

    def wrong_language(self, line):
        if not self.active:
            return False
        counts = script_histogram(line)
        if not counts:
            return False          # no letters: not ours to judge
        total = sum(counts.values())
        mine = sum(counts.get(s, 0) for s in self.scripts)
        if mine * 2 <= total:
            return True           # different alphabet: certain
        return self._wrong_same_script(line)

    def _wrong_same_script(self, line):
        """Second pass for languages that share the target alphabet.

        Discards a line only when another language scores clearly
        higher than the selected one. Short, neutral or ambiguous
        lines are kept."""
        if self.primary not in _LANG_HINTS:
            return False
        rivals = _HINT_RIVALS.get(self.scripts[0], ())
        if len(word_keys(line)) < MIN_WORDS_FOR_LANG_HINT:
            return False          # too short to judge
        mine = _hint_score(line, self.primary)
        best_other = 0
        for other in rivals:
            if other == self.primary:
                continue
            # A rival only counts if the line really is
            # written in it. Without this, a single foreign
            # name inside an otherwise ordinary line could
            # outweigh the language actually being read.
            if not _is_credible_language(line, other):
                continue
            best_other = max(best_other, _hint_score(line, other))
        return (best_other >= MIN_HINT_EVIDENCE
                and best_other >= mine + HINT_MARGIN)

    def filter_lines(self, lines):
        kept, dropped = [], []
        self.rewritten = []
        for ln in lines:
            cleaned = self.clean_line(ln)
            if cleaned is None:
                dropped.append(ln)
                continue
            kept.append(cleaned)
            if cleaned != ln:
                self.rewritten.append((ln, cleaned))
        return kept, dropped


def word_keys(s):
    """Lowercased words with surrounding punctuation stripped."""
    out = []
    for w in s.split():
        w = w.strip(_PUNCT).lower()
        if w:
            out.append(w)
    return out


_RTL_RANGES = ((0x0590, 0x08FF), (0xFB1D, 0xFDFF), (0xFE70, 0xFEFF))


def _is_rtl_char(ch):
    o = ord(ch)
    return any(a <= o <= b for a, b in _RTL_RANGES)


def fix_rtl_leading_punct(s):
    """In right-to-left text, the sentence-ending punctuation sits at the
    LEFT edge visually, so OCR (reading left to right spatially) often
    returns it at the START of the line. A leading dot then gets spoken
    ("dot") by the default voice before language switching kicks in.
    For predominantly RTL lines, relocate leading sentence punctuation to
    the end of the line, where it belongs."""
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return s
    rtl = sum(1 for c in letters if _is_rtl_char(c))
    if rtl / len(letters) < 0.6:
        return s
    i = 0
    while i < len(s) and s[i] in ".!?\u2026\u061F\u060C:;,":
        i += 1
    lead = s[:i]
    rest = s[i:].strip()
    if not lead or not rest:
        return s
    return rest + lead


class NoiseFilter:
    """Lines the user never wants read: exact-text phrases and/or regex
    patterns. Matching is EXACT-LINE only (the whole recognized line must
    match), never substring -- this prevents a filter for a short phrase
    from silently eating real dialogue that happens to contain it.

    Rules are plain strings. A rule prefixed with "regex:" is compiled as
    a case-insensitive regular expression; anything else is compared as
    literal text (case-insensitive, whitespace-trimmed). Invalid regex
    rules are skipped (never crash, never block the good rules) and
    reported back via .errors so the caller can inform the user.
    """

    def __init__(self, rules):
        self.errors = []       # (rule_text, error_message)
        self._literals = set()
        self._patterns = []    # compiled regex objects
        self._builtins = set()  # internal markers, e.g. no_letters
        for raw in rules:
            rule = raw.strip()
            if not rule:
                continue
            if rule.lower().startswith("builtin:"):
                self._builtins.add(rule[len("builtin:"):].strip())
                continue
            if rule.lower().startswith("regex:"):
                pattern_src = rule[len("regex:"):].strip()
                if not pattern_src:
                    continue
                try:
                    # Case-sensitive by design: a rule that specifies a
                    # letter case is about case, so ignoring case would
                    # widen it to match text it was never meant to.
                    # Plain-text phrases remain case-insensitive below.
                    self._patterns.append(re.compile(pattern_src))
                except re.error as e:
                    self.errors.append((raw, str(e)))
            else:
                self._literals.add(rule.lower())

    def is_noise(self, line):
        text = line.strip()
        if not text:
            return False
        if text.lower() in self._literals:
            return True
        if "no_letters" in self._builtins and letter_count(text) < 2:
            return True
        for pat in self._patterns:
            try:
                if pat.fullmatch(text):
                    return True
            except Exception:
                continue  # a pathological pattern must never crash capture
        return False

    def filter_lines(self, lines):
        """Returns (kept_lines, dropped_lines) for logging."""
        kept, dropped = [], []
        for ln in lines:
            (dropped if self.is_noise(ln) else kept).append(ln)
        return kept, dropped


# Built-in patterns for the settings-panel picker. Each key is a stable
# identifier stored in config; label/description are shown to the user;
# rule is what actually gets added to the filter list when checked.
# Built-in filter rules are defined by the add-on and passed in at
# start-up; the helper applies whatever it receives. Markers of
# the form "builtin:<name>" are resolved by NoiseFilter.


def batch_results(results):
    """Merge tracker output for ONE scan into utterances. Lines that
    appear in the same scan are one subtitle and must be spoken as one
    message; otherwise interrupt mode would cancel a subtitle's first
    line to speak its own second line. Suffixes stay separate (they
    never interrupt)."""
    lines = [t for k, t in results if k == "line"]
    out = []
    if lines:
        out.append(("line", "\n".join(lines)))
    out.extend((k, t) for k, t in results if k == "suffix")
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
        # pending: key -> [polls seen, best reading, last-seen scan]
        self.pending = {}
        self._scan = 0
        # key -> set of keys proven distinct from it by co-visibility
        self._distinct = {}
        # spoken: key -> (last seen-or-spoken time, display text)
        self.spoken = {}

    # -- internals ---------------------------------------------------------

    def _find_spoken_match(self, k, exclude=()):
        """Fuzzy-match against previously spoken lines.

        `exclude` holds lines spoken during the current scan. Those
        belong to the same subtitle and are displayed at the same
        moment, so they must not suppress one another regardless of
        how similar they appear."""
        if k in self.spoken and k not in exclude:
            return k
        for s in self.spoken:
            if s in exclude or self._known_distinct(k, s):
                continue
            if similar(k, s) >= self.similarity:
                return s
        return None

    def _known_distinct(self, a, b):
        """True if a and b have been displayed simultaneously."""
        return b in self._distinct.get(a, ())

    def _note_covisible(self, keys):
        """Record that these lines were displayed together, so they
        are not later treated as variant readings of one line."""
        for a in keys:
            bucket = self._distinct.setdefault(a, set())
            for b in keys:
                if a != b:
                    bucket.add(b)

    def _find_pending_match(self, k, exclude=()):
        """Fuzzy-match against candidates from earlier scans only.

        Lines visible in the same scan belong to one subtitle and are
        displayed simultaneously, so they cannot be variant readings
        of each other. Matching them would merge a similar pair into a
        single utterance and drop a line."""
        for p in self.pending:
            if p in exclude or self._known_distinct(k, p):
                continue
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
        self._scan += 1
        matched_pending = set()
        spoken_now = set()   # lines emitted during THIS scan
        scan_keys = []
        for _raw in lines:
            _n = normalize(_raw)
            if _n:
                scan_keys.append(_n.lower())
        if len(scan_keys) > 1:
            self._note_covisible(scan_keys)
        for raw in lines:
            ln = normalize(raw)
            if not ln:
                continue
            k = ln.lower()

            # 1) Already spoken and still visible (or a variant read):
            #    refresh suppression. Exception: strictly more words may be
            #    a genuine continuation -> let it reach the stability gate.
            m = self._find_spoken_match(k, exclude=spoken_now)
            if m is not None and (
                    len(word_keys(ln)) <= len(word_keys(self.spoken[m][1]))):
                self.spoken[m] = (now, self.spoken[m][1])
                continue

            # 2) FUZZY stability gate: similar readings on consecutive
            #    polls accumulate (motion varies recognition output;
            #    requiring exact repeats silences whole subtitles).
            pk = self._find_pending_match(k, exclude=matched_pending)
            if pk is None:
                self.pending[k] = [1, ln, self._scan]
                matched_pending.add(k)
                if self.stable_frames > 1:
                    continue
                pk = k
            else:
                entry = self.pending[pk]
                entry[0] += 1
                entry[2] = self._scan
                # keep the longer reading; ties keep the earlier one
                # (partial reads during fade-out arrive after the
                # complete one)
                if len(ln) > len(entry[1]):
                    entry[1] = ln
                matched_pending.add(pk)
                if entry[0] < self.stable_frames:
                    continue

            # 3) Stable: speak the best accumulated reading.
            best = self.pending.pop(pk)[1]
            matched_pending.discard(pk)
            bk = best.lower()
            m = self._find_spoken_match(bk, exclude=spoken_now)
            ext = self._extension_of(best)
            if m is not None and ext is None:
                # Close enough to a line already spoken, and not a
                # word-level continuation of it, so this is another
                # reading of the same line rather than new text.
                # It may be longer than what was spoken: recognition
                # can attach neighbouring on-screen text to a
                # subtitle, which would otherwise cause the whole
                # line to be read again each time that text changes.
                self.spoken[m] = (now, self.spoken[m][1])
                continue
            if ext is not None:
                sk, suffix = ext
                del self.spoken[sk]
                self.spoken[bk] = (now, best)
                spoken_now.add(bk)
                if suffix:
                    out.append(("suffix", suffix))
            else:
                self.spoken[bk] = (now, best)
                spoken_now.add(bk)
                out.append(("line", best))

        # 4) Discard candidates absent for longer than the grace window.
        #    A single missed scan no longer resets a line to zero:
        #    recognition may intermittently omit one line of a
        #    multi-line subtitle, which would otherwise prevent that
        #    line from ever reaching the stability threshold. One scan
        #    of grace allows for this while still requiring two
        #    sightings, so single-frame misreads are not admitted.
        for k in list(self.pending):
            if self._scan - self.pending[k][2] > PENDING_GRACE_SCANS:
                del self.pending[k]

        # 5) Suppression expires only after a line has been GONE for
        #    repeat_window seconds (visible lines were refreshed above).
        for k in list(self.spoken):
            if now - self.spoken[k][0] > self.repeat_window:
                del self.spoken[k]

        # 6) Forget co-visibility for lines that are no longer live,
        #    so the memory cannot grow without bound.
        live = set(self.pending) | set(self.spoken)
        for k in list(self._distinct):
            if k not in live:
                del self._distinct[k]
            else:
                self._distinct[k] &= live

        return out


# ---------------------------------------------------------------------------
# OCR engine loading with fallback (Windows-only at runtime)
# ---------------------------------------------------------------------------


def _pe_machine(path):
    """Read a PE file's machine type: 0x8664 = x64, 0xaa64 = ARM64."""
    try:
        with open(path, "rb") as f:
            data = f.read(4096)
        off = int.from_bytes(data[0x3C:0x40], "little")
        return hex(int.from_bytes(data[off + 4:off + 6], "little"))
    except Exception:
        return "unreadable"


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
        dll = os.path.join(os.path.expanduser("~"),
                           ".config", "oneocr", "oneocr.dll")
        log("OneOCR unavailable, trying legacy Windows OCR:\n"
            + traceback.format_exc()
            + f"diagnostic: this process machine={platform.machine()}, "
              f"oneocr.dll machine={_pe_machine(dll)} "
              "(0x8664=x64, 0xaa64=ARM64; these must match)")

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
    gdi32 = ctypes.windll.gdi32
except Exception:  # not on Windows (unit tests)
    user32 = None
    gdi32 = None

LOCK_HWND = 0  # window handle to lock capture to; 0 = follow focus

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


class BITMAPINFOHEADER(ctypes.Structure if 'ctypes' in dir() else object):
    pass


if user32 is not None:
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD), ("biWidth", wt.LONG),
            ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
            ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
            ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
            ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
            ("biClrImportant", wt.DWORD)]


def capture_locked_window(hwnd):
    """Capture the bottom strip of a specific window even when it is not
    in the foreground. Returns a PIL Image, or None when capture is not
    possible (then the caller skips the frame: fail toward silence,
    never toward reading a different window's text)."""
    from PIL import Image, ImageGrab
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w < 300 or h < 200:
        return None
    # Cheapest correct path: when the locked window IS in the foreground,
    # a plain screen grab of its rectangle is accurate.
    if user32.GetForegroundWindow() == hwnd:
        strip_top = rect.bottom - int(h * REGION_FRACTION)
        return ImageGrab.grab(
            bbox=(rect.left, strip_top, rect.right, rect.bottom),
            all_screens=True)
    # Occluded/background: ask the window to render its own contents
    # (PrintWindow with PW_RENDERFULLCONTENT). Works for most apps; some
    # video pipelines return a blank frame, which is treated as failure.
    hdc_win = user32.GetWindowDC(hwnd)
    if not hdc_win:
        return None
    img = None
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    try:
        gdi32.SelectObject(hdc_mem, bmp)
        if user32.PrintWindow(hwnd, hdc_mem, 2):  # PW_RENDERFULLCONTENT
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h  # top-down
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0
            buf = ctypes.create_string_buffer(w * h * 4)
            got = gdi32.GetDIBits(hdc_mem, bmp, 0, h, buf,
                                  ctypes.byref(bmi), 0)
            if got:
                img = Image.frombuffer(
                    "RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)
    if img is None:
        return None
    strip = img.crop((0, h - int(h * REGION_FRACTION), w, h))
    # A blank render means this app doesn't support background capture.
    if strip.convert("L").getextrema() == (0, 0):
        return None
    return strip


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
    """Exit when the parent process (NVDA) closes stdin."""
    try:
        sys.stdin.read()
    except Exception:
        pass
    import os
    os._exit(0)


def parse_args():
    global POLL_INTERVAL, REGION_FRACTION, STABLE_FRAMES
    global REPEAT_WINDOW, OCR_LANG, LANG_FILTER_ON, DETAILED_LOG
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=POLL_INTERVAL)
    p.add_argument("--region", type=int, default=int(REGION_FRACTION * 100))
    p.add_argument("--stable", type=int, default=STABLE_FRAMES)
    p.add_argument("--window", type=float, default=REPEAT_WINDOW)
    p.add_argument("--lang", type=str, default=OCR_LANG)
    p.add_argument("--only-lang", action="store_true",
                   help="speak only lines written in --lang")
    p.add_argument("--detailed-log", action="store_true",
                   help="record recognised text and decisions in the log")
    p.add_argument("--hwnd", type=int, default=0)
    p.add_argument("--filters-b64", type=str, default="")
    a = p.parse_args()
    global LOCK_HWND
    LOCK_HWND = a.hwnd
    rules = []
    if a.filters_b64:
        try:
            import base64
            blob = base64.urlsafe_b64decode(
                a.filters_b64.encode("ascii")).decode("utf-8")
            rules = [ln for ln in blob.split("\n") if ln.strip()]
        except Exception:
            log("Could not decode --filters-b64; ignoring noise filters.")
    POLL_INTERVAL = max(0.1, min(2.0, a.interval))
    REGION_FRACTION = max(0.10, min(1.0, a.region / 100.0))
    STABLE_FRAMES = max(1, min(5, a.stable))
    REPEAT_WINDOW = max(2.0, min(60.0, a.window))
    OCR_LANG = a.lang
    LANG_FILTER_ON = a.only_lang
    DETAILED_LOG = a.detailed_log
    # Must stay last: everything after a return is unreachable,
    # and the settings above would silently never be applied.
    return NoiseFilter(rules)


def main():
    noise_filter = parse_args()
    lang_filter = LanguageFilter(OCR_LANG if LANG_FILTER_ON else "")
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

    if lang_filter.unknown:
        log(f"Unrecognised language code {lang_filter.code!r}; "
            "reading all languages")
        emit({"type": "filter_warning",
              "message": f"Language code '{lang_filter.code}' was "
                         "not recognised. All languages will be "
                         "read."})
    if noise_filter.errors:
        bad = "; ".join(f"'{r}': {e}" for r, e in noise_filter.errors[:3])
        log(f"Ignored invalid filter rule(s): {bad}")
        emit({"type": "filter_warning",
              "message": f"{len(noise_filter.errors)} filter rule(s) "
                         "could not be understood and were skipped."})
    emit({"type": "ready", "engine": engine_name})
    log(f"runtime: frozen={getattr(sys, 'frozen', False)} "
        f"machine={platform.machine()} exe={sys.executable}")
    if DETAILED_LOG:
        log("Detailed logging is on: recognised subtitle text is "
            "recorded in this file.")
    log(f"Engine ready ({engine_name}); interval={POLL_INTERVAL}s "
        f"region={int(REGION_FRACTION*100)}% stable={STABLE_FRAMES} "
        f"window={REPEAT_WINDOW}s")

    tracker = SubtitleTracker()
    consecutive_failures = 0
    quiet_scans = 0

    if LOCK_HWND:
        log(f"locked to window handle {LOCK_HWND}")

    while True:
        t0 = time.perf_counter()
        lines = []
        try:
            if LOCK_HWND:
                if not user32.IsWindow(LOCK_HWND):
                    # The video window was closed: tell NVDA and exit
                    # cleanly instead of reading some other window.
                    emit({"type": "window_gone"})
                    return
                if user32.IsIconic(LOCK_HWND):
                    img = None  # minimized: nothing to read this frame
                else:
                    img = capture_locked_window(LOCK_HWND)
                    if img is None:
                        detail("scan: the locked window returned no image")
            else:
                region = get_capture_region()
                img = ImageGrab.grab(bbox=region, all_screens=True)
            if img is not None:
                img = safe_image(img)
                raw = recognize(img)
                if DETAILED_LOG:
                    _found = [ln for ln in raw.split("\n") if ln.strip()]
                    if _found:
                        if quiet_scans:
                            detail("scan: %d scans with no text"
                                   % quiet_scans)
                            quiet_scans = 0
                        detail("scan: recognised %d line(s): %r"
                               % (len(_found), _found))
                    else:
                        quiet_scans += 1
                lines = [fix_rtl_leading_punct(ln)
                         for ln in raw.split("\n") if ln.strip()]
                lines, dropped = noise_filter.filter_lines(lines)
                lines, wrong_lang = lang_filter.filter_lines(lines)
                for original, cleaned in lang_filter.rewritten:
                    detail("language filter removed other-script text: "
                           "%r -> %r" % (original, cleaned))
                for ln in wrong_lang:
                    log("Filtered as other language: %r" % ln)
                for d in dropped:
                    log(f"Filtered as noise: {d!r}")
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            log(f"Capture/OCR failure {consecutive_failures}:\n"
                + traceback.format_exc())
            if consecutive_failures == 5:
                emit({"type": "error", "message": f"OCR keeps failing: {e}"})

        try:
            results = tracker.update(lines, time.time())
            if results:
                detail("tracker: %r" % (results,))
            for kind, text in batch_results(results):
                detail("sent to screen reader: kind=%s text=%r"
                       % (kind, text))
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
