# -*- coding: utf-8 -*-
# HardSub Reader v1.0: NVDA global plugin
# Speaks hardcoded (burned-in) video subtitles using the Windows 11
# OneOCR engine (or legacy Windows OCR as fallback) running in an
# external sidecar process.
#
# Gestures (reassignable via NVDA Input Gestures dialog):
#   NVDA+alt+shift+s  toggle subtitle reading on/off
#   (unassigned)      toggle whether new subtitles interrupt speech
#                     (bindable via Input Gestures, HardSub Reader category)
#
# Settings: NVDA menu -> Preferences -> Settings -> HardSub Reader

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

import addonHandler
import config
import globalPluginHandler
import gui
import queueHandler
import speech
import tones
import ui
import wx
from gui import guiHelper
from gui import nvdaControls
from gui.settingsDialogs import SettingsPanel
from scriptHandler import script

try:
    addonHandler.initTranslation()
except Exception:
    pass

ADDON_DIR = os.path.dirname(__file__)
SIDECAR_DIR = os.path.normpath(
    os.path.join(ADDON_DIR, "..", "..", "sidecar"))
SIDECAR = os.path.join(SIDECAR_DIR, "subtitle_ocr_server.py")


def _machineArch():
    """The real OS architecture, seen from NVDA's 32-bit process.

    Environment variables are unreliable under emulation (a 32-bit
    process on ARM64 Windows can be told the machine is AMD64), so ask
    Windows directly via IsWow64Process2, which reports the true native
    machine. Falls back to environment variables on very old systems."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        pm = ctypes.c_ushort(0)
        nm = ctypes.c_ushort(0)
        if k32.IsWow64Process2(k32.GetCurrentProcess(),
                               ctypes.byref(pm), ctypes.byref(nm)):
            native = {0xAA64: "ARM64", 0x8664: "AMD64",
                      0x014C: "X86"}.get(nm.value)
            if native:
                return native
    except Exception:
        pass
    arch = (os.environ.get("PROCESSOR_ARCHITEW6432")
            or os.environ.get("PROCESSOR_ARCHITECTURE") or "")
    return arch.upper()


def findBundledHelper():
    """Self-contained helper exe matching this machine, if shipped."""
    name = ("hardsub_helper_arm64.exe" if "ARM64" in _machineArch()
            else "hardsub_helper_x64.exe")
    exe = os.path.join(SIDECAR_DIR, name)
    return [exe] if os.path.isfile(exe) else None
LOG_PATH = os.path.join(tempfile.gettempdir(), "hardSubReader_sidecar.log")

CREATE_NO_WINDOW = 0x08000000

MAX_RESTARTS = 5
RESTART_WINDOW_SECS = 120
RESTART_DELAY_SECS = 1.0

CONF_SECTION = "hardSubReader"
config.conf.spec[CONF_SECTION] = {
    "pollInterval": "float(default=0.3, min=0.1, max=2.0)",
    "regionPercent": "integer(default=30, min=10, max=100)",
    "stableFrames": "integer(default=2, min=1, max=5)",
    "repeatWindow": "integer(default=8, min=2, max=60)",
    "interrupt": "boolean(default=True)",
    "ocrLanguage": "string(default='en')",
}

# Module-level reference so the settings panel can apply changes live.
_plugin = None


def getConf(key):
    return config.conf[CONF_SECTION][key]


def findSystemPython():
    """Locate the user's native system Python (NOT NVDA's embedded one)."""
    env = os.environ.get("SUBTITLE_READER_PYTHON")
    if env and os.path.isfile(env):
        return [env]
    py = shutil.which("py")
    if py:
        return [py, "-3"]
    python = shutil.which("python")
    if python:
        return [python]
    return None


class HardSubReaderSettingsPanel(SettingsPanel):
    # Translators: title of the HardSub Reader settings panel.
    title = _("HardSub Reader")

    # Preset choices: (spoken label, config value). Dropdowns are fully
    # accessible with NVDA, unlike decimal spinner controls, and the labels explain
    # the tradeoff so users don't need to interpret raw numbers.
    SPEED_CHOICES = [
        # Translators: a response speed choice.
        (_("Fastest response, highest CPU and battery use"), 0.2),
        # Translators: a response speed choice.
        (_("Fast response, recommended"), 0.3),
        # Translators: a response speed choice.
        (_("Balanced"), 0.5),
        # Translators: a response speed choice.
        (_("Battery saver, subtitles may lag slightly"), 0.8),
    ]
    AREA_CHOICES = [
        # Translators: a scanned area choice.
        (_("Bottom quarter of the window"), 25),
        # Translators: a scanned area choice.
        (_("Bottom third of the window, recommended"), 33),
        # Translators: a scanned area choice.
        (_("Bottom half of the window"), 50),
        # Translators: a scanned area choice.
        (_("The whole window"), 100),
    ]
    FILTER_CHOICES = [
        # Translators: a misread filtering choice.
        (_("Off: speak immediately, may voice OCR misreads"), 1),
        # Translators: a misread filtering choice.
        (_("Normal: double-check each line first, recommended"), 2),
        # Translators: a misread filtering choice.
        (_("Strict: triple-check, slowest but cleanest"), 3),
    ]

    @staticmethod
    def _selectNearest(choices, value):
        return min(range(len(choices)),
                   key=lambda i: abs(choices[i][1] - value))

    def makeSettings(self, settingsSizer):
        helper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)

        # Translators: label for the response speed choice list.
        self.speedCtrl = helper.addLabeledControl(
            _("Response speed:"),
            wx.Choice, choices=[c[0] for c in self.SPEED_CHOICES])
        self.speedCtrl.SetSelection(self._selectNearest(
            self.SPEED_CHOICES, getConf("pollInterval")))

        # Translators: label for the scanned area choice list.
        self.areaCtrl = helper.addLabeledControl(
            _("Part of the video window to scan for subtitles:"),
            wx.Choice, choices=[c[0] for c in self.AREA_CHOICES])
        self.areaCtrl.SetSelection(self._selectNearest(
            self.AREA_CHOICES, getConf("regionPercent")))

        # Translators: label for the misread filtering choice list.
        self.filterCtrl = helper.addLabeledControl(
            _("Filtering of OCR misreads:"),
            wx.Choice, choices=[c[0] for c in self.FILTER_CHOICES])
        self.filterCtrl.SetSelection(self._selectNearest(
            self.FILTER_CHOICES, getConf("stableFrames")))

        # Translators: label for the repeat protection spin control.
        self.windowCtrl = helper.addLabeledControl(
            _("Do not repeat a subtitle line seen again within this many "
              "seconds:"),
            nvdaControls.SelectOnFocusSpinCtrl,
            min=2, max=60, initial=getConf("repeatWindow"))

        # Translators: label for the interrupt mode checkbox.
        self.interruptCtrl = helper.addItem(wx.CheckBox(
            self, label=_("When a new subtitle appears, stop reading the "
                          "previous one and read the new one right away")))
        self.interruptCtrl.SetValue(getConf("interrupt"))

        # Translators: label for the fallback OCR language field.
        self.langCtrl = helper.addLabeledControl(
            _("Subtitle language code, used only when the high-accuracy "
              "engine is unavailable (for example en, ar, tr):"),
            wx.TextCtrl)
        self.langCtrl.SetValue(getConf("ocrLanguage"))


    def onSave(self):
        c = config.conf[CONF_SECTION]
        c["pollInterval"] = self.SPEED_CHOICES[
            self.speedCtrl.GetSelection()][1]
        c["regionPercent"] = self.AREA_CHOICES[
            self.areaCtrl.GetSelection()][1]
        c["stableFrames"] = self.FILTER_CHOICES[
            self.filterCtrl.GetSelection()][1]
        c["repeatWindow"] = self.windowCtrl.GetValue()
        c["interrupt"] = self.interruptCtrl.GetValue()
        c["ocrLanguage"] = self.langCtrl.GetValue().strip() or "en"
        if _plugin is not None:
            _plugin.applySettings()


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    # Translators: category shown in the NVDA Input Gestures dialog.
    scriptCategory = _("HardSub Reader")

    def __init__(self):
        super().__init__()
        global _plugin
        _plugin = self
        self._proc = None
        self._enabled = False
        self._restartTimes = []
        self._lock = threading.Lock()
        gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(
            HardSubReaderSettingsPanel)

    # ------------------------------------------------------------------
    @script(
        # Translators: describes the toggle subtitle reading command.
        description=_("Toggles reading of hardcoded video subtitles on "
                      "and off"),
        gesture="kb:NVDA+alt+shift+s",
    )
    def script_toggleHardSubReader(self, gesture):
        with self._lock:
            if self._enabled:
                self._enabled = False
                self._stopProc()
                # Translators: announced when subtitle reading stops.
                ui.message(_("Subtitle reading off"))
            else:
                self._enabled = True
                self._restartTimes = []
                if self._startProc():
                    # Translators: announced when subtitle reading starts.
                    ui.message(_("Subtitle reading starting"))
                else:
                    self._enabled = False

    @script(
        # Translators: describes the toggle interrupt mode command.
        # Unassigned by default to avoid gesture conflicts; users can
        # bind it from the Input Gestures dialog, HardSub Reader category.
        description=_("Toggles whether a new subtitle interrupts the "
                      "previous one"),
    )
    def script_toggleInterruptMode(self, gesture):
        newVal = not getConf("interrupt")
        config.conf[CONF_SECTION]["interrupt"] = newVal
        if newVal:
            # Translators: announced when interrupt mode is enabled.
            ui.message(_("New subtitles interrupt previous speech"))
        else:
            # Translators: announced when interrupt mode is disabled.
            ui.message(_("Subtitles queue without interrupting"))

    # ------------------------------------------------------------------
    def applySettings(self):
        """Called by the settings panel; restart the helper if running so
        new settings take effect immediately."""
        with self._lock:
            if self._enabled and self._proc is not None:
                # Closing the process makes the reader thread exit; the
                # watchdog sees we're still enabled and restarts with the
                # new configuration.
                self._stopProc()

    def _sidecarArgs(self):
        return [
            "--interval", str(getConf("pollInterval")),
            "--region", str(getConf("regionPercent")),
            "--stable", str(getConf("stableFrames")),
            "--window", str(getConf("repeatWindow")),
            "--lang", getConf("ocrLanguage"),
        ]

    def _startProc(self):
        # Prefer the self-contained helper executable bundled with the
        # add-on (no Python required). Fall back to the script in the
        # user's system Python if no exe is present for this machine.
        command = findBundledHelper()
        if command is None:
            python = findSystemPython()
            if python is not None:
                command = python + ["-u", SIDECAR]
        if command is None:
            # Translators: error when no system Python is found.
            # Translators: error when no way to run the OCR helper exists.
            ui.message(_(
                "The OCR helper could not be started. Reinstall the "
                "add-on, or install Python from python.org to use the "
                "fallback mode."))
            return False
        try:
            # Keep the diagnostic log from growing forever: if it has
            # passed 1 MB, start it fresh. It only ever contains
            # timestamps and error details -- never subtitle text or
            # screen content.
            try:
                if os.path.getsize(LOG_PATH) > 1_000_000:
                    os.remove(LOG_PATH)
            except OSError:
                pass
            logFile = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
        except Exception:
            logFile = subprocess.DEVNULL
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8:replace"
        try:
            proc = subprocess.Popen(
                command + self._sidecarArgs(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=logFile,
                creationflags=CREATE_NO_WINDOW,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
        except Exception as e:
            # Translators: error when the helper process cannot start.
            ui.message(_("Could not start OCR helper: {error}").format(
                error=e))
            return False
        self._proc = proc
        threading.Thread(
            target=self._readLoop, args=(proc,), daemon=True).start()
        return True

    def _stopProc(self):
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.stdin.close()  # sidecar exits when stdin closes
            except Exception:
                pass
            try:
                proc.terminate()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _readLoop(self, proc):
        """Background thread: read JSON lines from the sidecar and speak."""
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                mtype = msg.get("type")
                if mtype == "subtitle":
                    text = msg.get("text", "")
                    if text:
                        self._speakSubtitle(text, msg.get("kind", "line"))
                elif mtype == "ready":
                    queueHandler.queueFunction(
                        queueHandler.eventQueue, tones.beep, 880, 60)
                    engine = msg.get("engine", "")
                    if "legacy" in engine.lower():
                        # Translators: warns that the fallback engine is
                        # in use, with reduced accuracy.
                        self._speak(_(
                            "Note: using the legacy OCR engine with "
                            "reduced accuracy. See the add-on help to set "
                            "up the OneOCR engine."))
                elif mtype == "error":
                    # Translators: spoken before an OCR helper error.
                    self._speak(_("Subtitle OCR error: {msg}").format(
                        msg=msg.get("message", "")))
        except Exception:
            pass
        with self._lock:
            if self._proc is proc:
                self._proc = None
            if not self._enabled:
                return
        self._scheduleRestart()

    def _scheduleRestart(self):
        now = time.time()
        self._restartTimes = [
            t for t in self._restartTimes if now - t < RESTART_WINDOW_SECS]
        if len(self._restartTimes) >= MAX_RESTARTS:
            with self._lock:
                self._enabled = False
            # Translators: announced when the helper crashes repeatedly.
            self._speak(_(
                "Subtitle reading stopped: the OCR helper keeps crashing. "
                "A diagnostic log was saved to {path}").format(
                    path=LOG_PATH))
            return
        self._restartTimes.append(now)

        def doRestart():
            with self._lock:
                if not self._enabled or self._proc is not None:
                    return
                if not self._startProc():
                    self._enabled = False
            queueHandler.queueFunction(
                queueHandler.eventQueue, tones.beep, 660, 40)

        threading.Timer(RESTART_DELAY_SECS, doRestart).start()

    # ------------------------------------------------------------------
    def _speak(self, text):
        queueHandler.queueFunction(queueHandler.eventQueue, ui.message, text)

    def _speakSubtitle(self, text, kind="line"):
        # Only a genuinely NEW line may interrupt ongoing speech. A suffix
        # (continuation of a line we already started reading) must queue,
        # otherwise it would cut off the very line it continues.
        if getConf("interrupt") and kind == "line":
            def speakNow(t=text):
                try:
                    speech.cancelSpeech()
                except Exception:
                    pass
                ui.message(t)
            queueHandler.queueFunction(queueHandler.eventQueue, speakNow)
        else:
            self._speak(text)

    # ------------------------------------------------------------------
    def terminate(self):
        global _plugin
        with self._lock:
            self._enabled = False
            self._stopProc()
        try:
            gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(
                HardSubReaderSettingsPanel)
        except ValueError:
            pass
        _plugin = None
        super().terminate()
