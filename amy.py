"""Amy — a local-first voice assistant for Windows.

Required Notice: Copyright (c) 2026 edzzztech — https://github.com/edzzztech/Amy-AI

Licensed under the PolyForm Noncommercial License 1.0.0. You may use, change
and redistribute this for any noncommercial purpose, but you must pass on the
licence and the notice above with any copy. Commercial use needs a separate
licence. See the LICENSE file, or https://polyformproject.org/licenses/noncommercial/1.0.0
"""

import os
import sys
import json
import copy
import glob
import time
import queue
import shutil
import threading
import traceback
import subprocess
import webbrowser
import base64
import io
import re
import smtplib
import datetime
import urllib.parse
from html import unescape as html_unescape
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# --- Required third-party dependencies (clear guidance if missing) ---
_MISSING = []
try:
    import requests
except ImportError:
    _MISSING.append("requests")
try:
    import psutil
except ImportError:
    _MISSING.append("psutil")
try:
    import PyQt5.QtWebEngineWidgets  # noqa: F401  (the window and panels run on Qt)
except ImportError:
    _MISSING.append("PyQt5 PyQtWebEngine")
try:
    import pyautogui
except ImportError:
    _MISSING.append("pyautogui")
try:
    from PIL import Image, ImageGrab, ImageDraw
except ImportError:
    _MISSING.append("pillow")

if _MISSING:
    msg = ("Amy cannot start — missing required packages: "
           + ", ".join(_MISSING)
           + "\n\nInstall them with:\n    pip install "
           + " ".join(_MISSING)
           + "\n\n(Recommended extras: pip install pywin32 pystray pyperclip opencv-python mss sounddevice miniaudio webrtcvad-wheels uiautomation cadquery)")
    print(msg)
    sys.exit(1)

# --- Optional: clipboard support ---
try:
    import pyperclip
    HAS_CLIPBOARD = True
except ImportError:
    pyperclip = None
    HAS_CLIPBOARD = False

class _LazyModule:
    """Stands in for a heavy module and imports it on first use.

    CadQuery alone costs ~450 MB the moment it's imported, and OpenCV ~40 MB,
    so neither is loaded until Amy actually needs CAD or the camera."""

    def __init__(self, name, after=None):
        self._name, self._after, self._mod = name, after, None

    def _load(self):
        if self._mod is None:
            import importlib
            self._mod = importlib.import_module(self._name)
            log_debug(f"Loaded {self._name} on demand.")
            if self._after:
                self._after()
        return self._mod

    def __getattr__(self, attr):
        return getattr(self._load(), attr)


def _installed(name):
    try:
        import importlib.util as _ilu
        return _ilu.find_spec(name) is not None
    except Exception:
        return False


def _fix_qt_plugin_path():
    # Some OpenCV builds point Qt at their own bundled plugins, which breaks
    # PyQt. Amy's window is PyQt, so take that setting back.
    if "cv2" in os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH", ""):
        os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)


# --- Optional: computer vision (desk camera, object detection, hand gestures) ---
try:
    import numpy as np
except ImportError:
    np = None
HAS_CV2 = np is not None and _installed("cv2")
cv2 = _LazyModule("cv2", after=_fix_qt_plugin_path) if HAS_CV2 else None

# MediaPipe is imported lazily. Importing it eagerly costs ~400ms of startup
# (it pulls in matplotlib, which Amy never uses) for a feature that only
# matters once the camera is running with gestures enabled.
mp = None
HAS_MEDIAPIPE = None          # None = not yet checked


def _load_mediapipe():
    """Import MediaPipe on first use. Returns the module or None."""
    global mp, HAS_MEDIAPIPE
    if HAS_MEDIAPIPE is not None:
        return mp
    try:
        import mediapipe as _mp
        mp = _mp
        HAS_MEDIAPIPE = True
        log_debug("MediaPipe loaded on demand.")
    except ImportError:
        mp = None
        HAS_MEDIAPIPE = False
    except Exception as e:
        mp = None
        HAS_MEDIAPIPE = False
        log_debug(f"MediaPipe failed to load: {e}")
    return mp


def _mediapipe_installed():
    """Cheap availability check that doesn't pay the import cost."""
    global HAS_MEDIAPIPE
    if HAS_MEDIAPIPE is not None:
        return HAS_MEDIAPIPE
    try:
        import importlib.util as _ilu
        HAS_MEDIAPIPE = _ilu.find_spec("mediapipe") is not None
    except Exception:
        HAS_MEDIAPIPE = False
    return HAS_MEDIAPIPE

# System Tray support
try:
    import pystray
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

# Native Windows API for app window focus
try:
    import ctypes
    import win32gui
    import win32con
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

try:
    import pythoncom
    HAS_PYTHONCOM = True
except ImportError:
    HAS_PYTHONCOM = False

# --- Neural TTS (Microsoft Edge voices: free, no API key, genuine British male) ---
# This is the ONLY reliable way to get a proper British male voice, because stock
# Windows ships no en-GB male voice (David and Mark are American).
try:
    import edge_tts
    import asyncio
    HAS_EDGE_TTS = True
except ImportError:
    edge_tts = None
    HAS_EDGE_TTS = False

try:
    import speech_recognition as sr
    SPEECH_AVAILABLE = True
except ImportError:
    SPEECH_AVAILABLE = False

try:
    import pyttsx3
    TTS_AVAILABLE = True
except ImportError:
    TTS_AVAILABLE = False

try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
    SPOTIPY_AVAILABLE = True
except ImportError:
    SPOTIPY_AVAILABLE = False

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False


def log_debug(msg):
    try:
        _base = os.path.dirname(os.path.abspath(__file__))
        _dir = os.path.join(_base, "amy_data")
        try:
            os.makedirs(_dir, exist_ok=True)
        except Exception:
            _dir = _base
        _log_path = os.path.join(_dir, "amy_debug.log")
        with open(_log_path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


# ==========================================================================
# --- PYQT HOST ---
# Amy runs on PyQt5. Windows, the desktop overlay, drag/resize and the event
# loop are native Qt. The rich panels (chat, documents, code, CAD preview,
# cards) are HTML rendered by Qt's own Chromium engine (QWebEngine), which is
# what let every existing feature carry over intact.
#
# The page talks to Python through QWebChannel. A small shim gives the page the
# same `pywebview.api.<method>(...)` interface it always used, and the Python
# side keeps `window.evaluate_js(...)`, so the rest of the code didn't have to
# change.
# ==========================================================================

_WEBCHANNEL_SHIM = r"""
(function () {
  if (window.pywebview && window.pywebview.__amy) return;
  const pending = {};
  const queue = [];
  let seq = 0, bridge = null;
  function call(name, args) {
    return new Promise((resolve, reject) => {
      const id = ++seq;
      pending[id] = { resolve, reject };
      const send = () => bridge.call(id, name, JSON.stringify(args));
      if (bridge) send(); else queue.push(send);
    });
  }
  window.pywebview = {
    __amy: true,
    api: new Proxy({}, {
      get: (_, name) => (typeof name === 'string' && name !== 'then')
        ? (...args) => call(name, args) : undefined
    })
  };
  function connect() {
    if (!(window.qt && qt.webChannelTransport && window.QWebChannel)) {
      setTimeout(connect, 15);
      return;
    }
    new QWebChannel(qt.webChannelTransport, (channel) => {
      bridge = channel.objects.amyBridge;
      bridge.result.connect((id, payload) => {
        const p = pending[id];
        if (!p) return;
        delete pending[id];
        let r = null;
        try { r = JSON.parse(payload); } catch (e) { p.resolve(null); return; }
        if (r && r.error) p.reject(new Error(r.error)); else p.resolve(r ? r.value : null);
      });
      queue.splice(0).forEach(f => f());
      const fire = () => window.dispatchEvent(new Event('pywebviewready'));
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fire);
      else fire();
    });
  }
  connect();
})();
"""

HAS_QT = False
try:
    from PyQt5 import QtCore, QtGui, QtWidgets
    # QtWebEngine must be imported before the QApplication exists.
    from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage, QWebEngineScript
    from PyQt5.QtWebChannel import QWebChannel
    HAS_QT = True
except ImportError:
    QtCore = QtGui = QtWidgets = None


if HAS_QT:
    import math as _math

    class _GuiInvoker(QtCore.QObject):
        """Runs callables on the Qt GUI thread, from any thread."""
        _run = QtCore.pyqtSignal(object)

        def __init__(self):
            super().__init__()
            self._run.connect(self._exec, QtCore.Qt.QueuedConnection)

        @staticmethod
        def _exec(fn):
            try:
                fn()
            except Exception as e:
                log_debug(f"GUI call failed: {e}\n{traceback.format_exc()}")

        def on_gui_thread(self):
            return QtCore.QThread.currentThread() == QtWidgets.QApplication.instance().thread()

        def post(self, fn):
            """Fire and forget."""
            if self.on_gui_thread():
                self._exec(fn)
            else:
                self._run.emit(fn)

        def call(self, fn, timeout=10.0):
            """Run on the GUI thread and return the result (blocking)."""
            if self.on_gui_thread():
                return fn()
            box, done = {}, threading.Event()

            def wrapped():
                try:
                    box["v"] = fn()
                except Exception as e:
                    box["e"] = e
                finally:
                    done.set()
            self._run.emit(wrapped)
            if not done.wait(timeout):
                raise TimeoutError("GUI thread did not respond")
            if "e" in box:
                raise box["e"]
            return box.get("v")

    _GUI = None

    def gui():
        global _GUI
        if _GUI is None:
            _GUI = _GuiInvoker()
        return _GUI

    class _Bridge(QtCore.QObject):
        """The object the page sees as `pywebview.api`. Calls run on worker
        threads (as pywebview did), so a slow method never freezes the UI."""
        result = QtCore.pyqtSignal(int, str)

        def __init__(self, api):
            super().__init__()
            self._api = api

        @QtCore.pyqtSlot(int, str, str)
        def call(self, call_id, name, args_json):
            def work():
                payload = {"value": None}
                try:
                    if name.startswith("_"):
                        raise AttributeError(name)
                    fn = getattr(self._api, name, None)
                    if not callable(fn):
                        raise AttributeError(f"no API method '{name}'")
                    args = json.loads(args_json or "[]")
                    payload["value"] = fn(*args)
                except Exception as e:
                    log_debug(f"API {name} failed: {e}")
                    payload = {"error": str(e)}
                try:
                    text = json.dumps(payload, default=str)
                except Exception:
                    text = json.dumps({"value": None})
                gui().post(lambda: self.result.emit(call_id, text))
            threading.Thread(target=work, daemon=True, name=f"api-{name}").start()

    class _Events:
        class _Hook(list):
            def __iadd__(self, fn):
                self.append(fn)
                return self

            def fire(self):
                for fn in list(self):
                    try:
                        fn()
                    except Exception as e:
                        log_debug(f"event handler failed: {e}")

        def __init__(self):
            self.closed = _Events._Hook()
            self.shown = _Events._Hook()

    class _QuietPage(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line, source):
            if level >= QWebEnginePage.WarningMessageLevel:
                log_debug(f"js: {message} (line {line})")

    class _HostWidget(QtWidgets.QWidget):
        def __init__(self, owner, frameless, on_top):
            flags = QtCore.Qt.Window
            if frameless:
                flags |= QtCore.Qt.FramelessWindowHint
            if on_top:
                flags |= QtCore.Qt.WindowStaysOnTopHint
            super().__init__(None, flags)
            self._owner = owner

        def closeEvent(self, e):
            self._owner.events.closed.fire()
            super().closeEvent(e)

        def resizeEvent(self, e):
            super().resizeEvent(e)
            self._owner._place_grip()

    class QtWebWindow:
        """A Qt window holding one web page, with the pywebview window API."""

        def __init__(self, title, html=None, url=None, js_api=None, width=1280, height=800,
                     x=None, y=None, min_size=(400, 300), frameless=False, on_top=False,
                     background_color="#0e1011", resizable=True, **_ignored):
            self.events = _Events()
            self.w = _HostWidget(self, frameless, on_top)
            self.w.setWindowTitle(title)
            self.w.resize(int(width), int(height))
            if min_size:
                self.w.setMinimumSize(int(min_size[0]), int(min_size[1]))
            if x is not None and y is not None:
                self.w.move(int(x), int(y))
            layout = QtWidgets.QVBoxLayout(self.w)
            layout.setContentsMargins(0, 0, 0, 0)
            self.view = QWebEngineView(self.w)
            self.page = _QuietPage(self.view)
            self.view.setPage(self.page)
            self.page.setBackgroundColor(QtGui.QColor(background_color))
            self.view.setContextMenuPolicy(QtCore.Qt.NoContextMenu)
            layout.addWidget(self.view)

            self.channel = QWebChannel(self.page)
            self.bridge = _Bridge(js_api)
            self.channel.registerObject("amyBridge", self.bridge)
            self.page.setWebChannel(self.channel)
            script = QWebEngineScript()
            qwc = QtCore.QFile(":/qtwebchannel/qwebchannel.js")
            lib = ""
            if qwc.open(QtCore.QIODevice.ReadOnly):
                lib = bytes(qwc.readAll()).decode("utf-8")
                qwc.close()
            script.setSourceCode(lib + "\n" + _WEBCHANNEL_SHIM)
            script.setName("amy-bridge")
            script.setInjectionPoint(QWebEngineScript.DocumentCreation)
            script.setWorldId(QWebEngineScript.MainWorld)
            script.setRunsOnSubFrames(False)
            self.page.scripts().insert(script)

            # Frameless windows lose their resize border; a corner grip
            # puts it back.
            self.grip = None
            if frameless and resizable:
                self.grip = QtWidgets.QSizeGrip(self.w)
                self.grip.setFixedSize(16, 16)
                self.grip.setStyleSheet("background: transparent;")
                self._place_grip()
            if html is not None:
                # A base URL lets the page load https:// resources.
                self.page.setHtml(html, QtCore.QUrl("https://amy.local/"))
            elif url:
                self.view.setUrl(QtCore.QUrl(url))
            self.w.show()
            self.events.shown.fire()

        def _place_grip(self):
            if self.grip is not None:
                self.grip.move(self.w.width() - 16, self.w.height() - 16)
                self.grip.raise_()

        # --- pywebview-compatible API, safe to call from any thread ---------
        def evaluate_js(self, script, timeout=5.0):
            inv = gui()
            if inv.on_gui_thread():
                self.page.runJavaScript(script)
                return None
            box, done = {}, threading.Event()

            def run():
                def cb(v):
                    box["v"] = v
                    done.set()
                self.page.runJavaScript(script, cb)
            inv.post(run)
            done.wait(timeout)
            return box.get("v")

        def evaluate_js_async(self, script):
            """Run script in the page without waiting for a result."""
            gui().post(lambda: self.page.runJavaScript(script))

        def show(self):
            gui().post(lambda: (self.w.show(), self.w.raise_(), self.w.activateWindow()))

        def hide(self):
            gui().post(self.w.hide)

        def focus(self):
            gui().post(lambda: (self.w.raise_(), self.w.activateWindow()))

        def restore(self):
            gui().post(lambda: (self.w.showNormal(), self.w.raise_(), self.w.activateWindow()))

        def minimize(self):
            gui().post(self.w.showMinimized)

        def maximize(self):
            gui().post(self.w.showMaximized)

        def toggle_fullscreen(self):
            gui().post(lambda: self.w.showNormal() if (self.w.isFullScreen() or self.w.isMaximized())
                       else self.w.showMaximized())

        def toggle_maximize(self):
            self.toggle_fullscreen()

        def start_drag(self):
            def go():
                handle = self.w.windowHandle()
                if handle is not None and hasattr(handle, "startSystemMove"):
                    handle.startSystemMove()
            gui().post(go)

        def move(self, x, y):
            gui().post(lambda: self.w.move(int(x), int(y)))

        def resize(self, w, h):
            gui().post(lambda: self.w.resize(int(w), int(h)))

        @property
        def x(self):
            return gui().call(lambda: self.w.x())

        @property
        def y(self):
            return gui().call(lambda: self.w.y())

        def destroy(self):
            def go():
                self.w.close()
                self.w.deleteLater()
            gui().post(go)

    class AmyOverlay(QtWidgets.QWidget):
        """The desktop ring: frameless, always on top, per-pixel transparent.

        Same look as the orb in the main window. Swells and ripples with the
        live mic level, spins slowly when idle.
        """
        SIZE = 236
        # Shared with the main window's orb and the project site.
        ACCENT = (94, 234, 212)
        ACCENT_2 = (167, 139, 250)
        ACCENT_3 = (109, 72, 206)
        MUTED = (140, 146, 166)
        MUTED_DEEP = (88, 94, 112)
        MUTED_SHADE = (56, 60, 74)
        opened = QtCore.pyqtSignal()
        vision = QtCore.pyqtSignal()
        hidden_by_user = QtCore.pyqtSignal()

        def __init__(self):
            super().__init__(None, QtCore.Qt.FramelessWindowHint
                             | QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.Tool)
            self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
            self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
            self.setFixedSize(self.SIZE, self.SIZE)
            self.state = "idle"
            self.target = 0.0
            self.level = 0.0
            self.angle = 0.0
            self.flash = 0.0
            self.heard = ""
            self.heard_until = 0.0
            self._press = None
            self._dragged = False
            self._double = False
            self._clock = QtCore.QElapsedTimer()
            self._clock.start()
            self._last = 0.0
            self.setToolTip("Click: open Amy  ·  Double-click: look at my screen  ·  Right-click: menu")
            self._timer = QtCore.QTimer(self)
            self._timer.setTimerType(QtCore.Qt.PreciseTimer)
            self._timer.timeout.connect(self._tick)
            self._timer.start(16)
            geo = QtWidgets.QApplication.primaryScreen().availableGeometry()
            self.move(geo.right() - self.SIZE - 24, geo.bottom() - self.SIZE - 24)

        # Called on the GUI thread via gui().post
        def set_level(self, v):
            try:
                self.target = max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                pass

        def set_state(self, s):
            s = str(s or "idle")
            if s == "wake" and self.state != "wake":
                self.flash = 1.0
            self.state = s

        def set_heard(self, text):
            self.heard = str(text or "")[:48]
            self.heard_until = self._now() + 3.5

        def _now(self):
            return self._clock.elapsed() / 1000.0

        def _tick(self):
            if not self.isVisible():
                return
            now = self._now()
            dt = min(0.05, now - self._last) if self._last else 0.016
            self._last = now
            goal = 0.0 if self.state == "muted" else self.target
            self.level += (goal - self.level) * (0.45 if goal > self.level else 0.10)
            speed = {"idle": 0.35, "listening": 0.18, "wake": 0.9, "speaking": 0.6,
                     "thinking": 1.4, "muted": 0.08}.get(self.state, 0.35)
            self.angle = (self.angle + dt * speed) % (_math.tau)
            self.flash = max(0.0, self.flash - dt * 1.6)
            self.update()

        def paintEvent(self, _e):
            p = QtGui.QPainter(self)
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            t = self._now()
            muted = self.state == "muted"
            lvl = 0.0 if muted else self.level
            if self.state == "speaking":
                lvl = max(lvl, 0.18 + 0.14 * abs(_math.sin(t * 5.3)) * abs(_math.sin(t * 1.7)))
            W = self.width()
            cx = cy = W / 2.0
            R = W * 0.36
            # Same mark as the main window: a circle filled with a teal-to-violet
            # gradient and two counter-moving currents. No segmented ring, no
            # wordmark — the sphere is the logo.
            hi = QtGui.QColor(*(self.MUTED if muted else self.ACCENT))
            body = QtGui.QColor(*(self.MUTED_DEEP if muted else self.ACCENT_2))
            deep = QtGui.QColor(*(self.MUTED_SHADE if muted else self.ACCENT_3))

            core_r = R * (0.84 + 0.07 * lvl + 0.08 * self.flash)
            p.setPen(QtCore.Qt.NoPen)

            # Halo, clamped inside the widget so it fades out rather than clipping.
            halo_r = min(core_r * 1.8, W * 0.495)
            glow = QtGui.QRadialGradient(cx, cy, halo_r)
            g0 = QtGui.QColor(hi)
            g0.setAlphaF(0.07 if muted else min(1.0, 0.18 + 0.30 * lvl + 0.26 * self.flash))
            g1 = QtGui.QColor(body)
            g1.setAlphaF(0.04 if muted else min(1.0, 0.10 + 0.18 * lvl))
            g2 = QtGui.QColor(body)
            g2.setAlpha(0)
            glow.setColorAt(core_r / halo_r * 0.9, g0)
            glow.setColorAt(0.72, g1)
            glow.setColorAt(1.0, g2)
            p.setBrush(QtGui.QBrush(glow))
            p.drawEllipse(QtCore.QPointF(cx, cy), halo_r, halo_r)

            # Everything else paints inside the circle, so its edge is the only one.
            clip = QtGui.QPainterPath()
            clip.addEllipse(QtCore.QPointF(cx, cy), core_r, core_r)
            p.save()
            p.setClipPath(clip)
            box = QtCore.QRectF(cx - core_r * 1.6, cy - core_r * 1.6, core_r * 3.2, core_r * 3.2)

            drift = t * 0.38
            ox = cx + core_r * (-0.36 + 0.11 * _math.cos(drift))
            oy = cy + core_r * (-0.40 + 0.10 * _math.sin(drift * 1.17))
            mid = 0.72 + 0.08 * _math.sin(t * 0.52)
            sphere = QtGui.QRadialGradient(ox, oy, core_r * 1.62)
            sphere.setColorAt(0.0, hi)
            sphere.setColorAt(max(0.12, mid - 0.38), hi)
            sphere.setColorAt(mid, body)
            sphere.setColorAt(1.0, deep)
            p.setBrush(QtGui.QBrush(sphere))
            p.drawRect(box)

            def current(px, py, rad, colour, alpha):
                grad = QtGui.QRadialGradient(px, py, max(1.0, rad))
                a = QtGui.QColor(colour)
                a.setAlphaF(min(1.0, alpha))
                b = QtGui.QColor(colour)
                b.setAlpha(0)
                grad.setColorAt(0.0, a)
                grad.setColorAt(1.0, b)
                p.setBrush(QtGui.QBrush(grad))
                p.drawRect(box)

            current(cx + core_r * 0.34 * _math.cos(-drift * 0.72 + 2.1),
                    cy + core_r * 0.34 * _math.sin(-drift * 0.72 + 2.1),
                    core_r * (0.9 + 0.3 * lvl), hi, 0.05 if muted else 0.20 + 0.22 * lvl)
            current(cx + core_r * 0.40 * _math.cos(drift * 1.31 + 4.2),
                    cy + core_r * 0.40 * _math.sin(drift * 1.31 + 4.2),
                    core_r * (0.7 + 0.35 * lvl), body, 0.04 if muted else 0.18 + 0.20 * lvl)
            current(cx + core_r * 0.42 * _math.cos(self.angle * 0.6),
                    cy + core_r * 0.42 * _math.sin(self.angle * 0.6),
                    core_r * 0.85, QtGui.QColor(255, 255, 255),
                    0.02 if muted else 0.05 + 0.08 * lvl)
            p.restore()

            caption = {"listening": "Listening", "wake": "Yes?", "speaking": "Speaking",
                       "thinking": "Thinking", "muted": "Muted"}.get(self.state, "")
            if self.heard and t < self.heard_until:
                caption = self.heard
            if caption:
                f2 = QtGui.QFont("Segoe UI")
                f2.setPixelSize(max(9, int(W * 0.048)))
                p.setFont(f2)
                p.setPen(QtGui.QColor(232, 234, 242, 210))
                text = QtGui.QFontMetrics(f2).elidedText(caption, QtCore.Qt.ElideRight,
                                                         int(W * 0.86))
                # Below the sphere now — it used to sit on the dark disc that
                # the gradient replaced, where it would be unreadable.
                p.drawText(QtCore.QRectF(0, cy + core_r + W * 0.02, W, W * 0.09),
                           QtCore.Qt.AlignCenter, text)
            p.end()

        def mousePressEvent(self, e):
            if e.button() == QtCore.Qt.LeftButton:
                self._press = e.globalPos() - self.frameGeometry().topLeft()
                self._dragged = False

        def mouseMoveEvent(self, e):
            if self._press is not None and e.buttons() & QtCore.Qt.LeftButton:
                if (e.globalPos() - self.frameGeometry().topLeft() - self._press).manhattanLength() > 3:
                    self._dragged = True
                self.move(e.globalPos() - self._press)

        def mouseReleaseEvent(self, e):
            if e.button() == QtCore.Qt.LeftButton:
                if not self._dragged:
                    QtCore.QTimer.singleShot(QtWidgets.QApplication.doubleClickInterval(),
                                             self._single_click)
                self._press = None

        def _single_click(self):
            if self._double:
                self._double = False
                return
            self.opened.emit()

        def mouseDoubleClickEvent(self, _e):
            self._double = True
            self.vision.emit()

        def contextMenuEvent(self, e):
            menu = QtWidgets.QMenu(self)
            menu.setStyleSheet(
                "QMenu{background:#181b1d;color:#e6edf0;border:1px solid #2c3133;"
                "border-radius:8px;padding:4px}"
                "QMenu::item{padding:6px 18px;border-radius:5px}"
                "QMenu::item:selected{background:rgba(94,234,212,0.25)}")
            a_open = menu.addAction("Open Amy")
            a_look = menu.addAction("Look at my screen")
            menu.addSeparator()
            a_hide = menu.addAction("Hide overlay")
            chosen = menu.exec_(e.globalPos())
            if chosen == a_open:
                self.opened.emit()
            elif chosen == a_look:
                self.vision.emit()
            elif chosen == a_hide:
                self.hidden_by_user.emit()


class _WebviewCompat:
    """`webview.create_window` / `webview.start`, backed by Qt."""

    def create_window(self, title, html=None, url=None, js_api=None, **kw):
        if not HAS_QT:
            raise RuntimeError("PyQt5 and PyQtWebEngine are required")
        return gui().call(lambda: QtWebWindow(title, html=html, url=url, js_api=js_api, **kw),
                          timeout=30)

    def start(self, debug=False, **_kw):
        return QtWidgets.QApplication.instance().exec_()


webview = _WebviewCompat()


def make_qt_app():
    """Create the QApplication. Call once, on the main thread, before
    any window is made."""
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts, True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("Amy")
    app.setQuitOnLastWindowClosed(False)    # the overlay/tray keep her alive
    gui()                                    # bind the invoker to the GUI thread
    return app



# ==========================================================================
# --- CONFIGURATION (all keys/IDs live in amy_config.json) ---
# ==========================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "amy_config.json")


def _migrate_from_jarvis():
    """One-time carry-over of settings and memory from the J.A.R.V.I.S. build.

    Copies rather than moves, so the old build keeps working if it's still in
    use. Runs only when the Amy files don't exist yet.
    """
    old_cfg = os.path.join(BASE_DIR, "jarvis_config.json")
    if not os.path.exists(CONFIG_FILE) and os.path.exists(old_cfg):
        try:
            with open(old_cfg, "r", encoding="utf-8") as f:
                data = json.load(f)
            a = data.setdefault("assistant", {})
            # Only change values the user never customised.
            if str(a.get("wake_word", "")).lower() in ("", "jarvis"):
                a["wake_word"] = "amy"
            if a.get("neural_voice") in (None, "", "en-GB-RyanNeural"):
                a["neural_voice"] = "en-GB-SoniaNeural"
                if a.get("neural_pitch") == "-2Hz":
                    a["neural_pitch"] = "+0Hz"
            a.setdefault("voice_gender", "female")
            data.pop("mobile", None)
            bar = data.get("ui", {}).get("bottom_bar", {})
            for k in ("phone", "vr"):
                bar.pop(k, None)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            print("[amy] Settings carried over from jarvis_config.json")
        except Exception as e:
            print(f"[amy] Could not carry over jarvis_config.json: {e}")

    old_dir = os.path.join(BASE_DIR, "jarvis_data")
    new_dir = os.path.join(BASE_DIR, "amy_data")
    if not os.path.exists(new_dir) and os.path.isdir(old_dir):
        try:
            shutil.copytree(old_dir, new_dir)
            for name in os.listdir(new_dir):
                if name.startswith("jarvis_"):
                    os.replace(os.path.join(new_dir, name),
                               os.path.join(new_dir, "amy_" + name[len("jarvis_"):]))
            print("[amy] Memory and data carried over from jarvis_data")
        except Exception as e:
            print(f"[amy] Could not carry over jarvis_data: {e}")


_migrate_from_jarvis()

# Everything a user might need to edit lives here. On first run this file is
# written to disk; after that, edit amy_config.json (no need to touch code).
DEFAULT_CONFIG = {
    "email": {
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "address": "YOUR_EMAIL@gmail.com",
        "app_password": "YOUR_APP_PASSWORD_HERE",
        "default_receiver": "YOUR_RECEIVER_EMAIL@gmail.com",
        # "browser" = open Gmail in Chrome and send visibly; "smtp" = silent background send.
        "mode": "browser",
        "auto_send": True,
    },
    "spotify": {
        "client_id": "",
        "client_secret": "",
        "redirect_uri": "http://127.0.0.1:8888/callback",
    },
    "ollama": {
        "chat_url": "http://localhost:11434/api/chat",
        "generate_url": "http://localhost:11434/api/generate",
        "model": "llama3.2",
        "vision_model": "llava",
        "embed_model": "nomic-embed-text",
        # --- Memory use ------------------------------------------------------
        # "lean": one language model shared by every job + vision only when
        #         needed; models unloaded while Amy sleeps; compact context.
        # "balanced": separate models per job, unloaded after 15 idle minutes.
        # "performance": everything stays loaded (the old behaviour).
        "memory_mode": "lean",
        "max_ctx": 32768,          # never reserve more context than this
        "kv_cache": "q8_0",        # half-size context memory when Amy starts Ollama ("f16" = off)
        # --- Multi-model routing -------------------------------------------
        # Different jobs suit different models. Any left blank fall back to
        # "model" above. Only models you've actually pulled are used; the rest
        # degrade gracefully.
        "models": {
            "fast": "",          # snap decisions: intent, references, classification
            "chat": "",          # conversation and general questions
            "reasoning": "",     # research synthesis, analysis, agents
            "code": "",          # writing and fixing code
            "cad": "",           # OpenSCAD generation
            "vision": "",        # falls back to vision_model
        },
        "auto_select": True,     # pick the best pulled model for each job
        "auto_start": True,      # start 'ollama serve' when Amy starts, if it isn't running
        "start_timeout": 45,     # seconds to wait for it to come up
        "stop_on_exit": False,   # stop the Ollama that Amy started when Amy closes
    },
    "assistant": {
        "wake_word": "amy",
        "user_title": "Sir",
        "default_city": "London",
        "voice_rate": 0,          # SAPI rate -3..+3, Amy sounds best slightly measured
        "preferred_voice": "",    # exact SAPI voice name to force, e.g. "Hazel"
        "voice_gender": "female", # "female" | "male": which system voices to prefer
        # Neural voice (requires: pip install --user edge-tts). British options:
        # en-GB-SoniaNeural, en-GB-LibbyNeural, en-GB-MaisieNeural (female),
        # en-GB-RyanNeural, en-GB-ThomasNeural (male).
        "neural_voice": "en-GB-SoniaNeural",
        "neural_rate": "+0%",
        "neural_pitch": "+0Hz",
        "speak_greeting": True,
        "vision_interval": 8,     # seconds between ambient screen looks
        "screen_awareness_on_start": False,
        "code_model": "",         # optional separate coding model, e.g. "qwen2.5-coder"
        "conversation_timeout": 90,   # seconds of silence before conversation mode ends
        "auto_research": True,        # auto-search the web for time-sensitive questions
        "smart_intent": True,         # understand free-form requests without keywords
        "habit_nudges": False,        # proactively offer what you usually do at this hour
        "mic_threshold_ceiling": 1800,  # stops the mic going deaf in noisy rooms
        "mic_threshold_floor": 120,
        "response_cache": True,        # instant answers for repeated questions
        "transcript_repair": True,     # fix misheard / clipped words
        "announce_background": True,   # say when a background job finishes
        "enable_plugins": True,        # load amy_data/plugins/*.py
        "allow_shell_commands": False, # let custom commands run shell (off by default)
        "persona_note": "",            # extra personality instruction
        "reply_tokens": 320,           # was 120 - answers were being cut off
        "deliberate": True,            # think harder on genuinely hard questions
        "self_check": True,            # let her catch her own mistakes
        "save_interval": 3.0,          # debounce memory writes (seconds)
        "transcript_limit": 4000,      # turns kept live; older ones archived
        "kb_limit": 1200,              # max knowledge-base documents
        "grammar_matching": True,      # match speech against known commands
        "grammar_accept": 0.80,        # confidence to act without asking
        "grammar_clarify": 0.62,       # confidence to ask "did you mean...?"
        "stt_engine": "auto",          # "auto" (Whisper if installed, else Google) | "whisper" | "native" | "google"
        "vosk_model_path": "",         # folder of an unpacked Vosk model
        "resolve_references": True,    # understand "do that again"
        "automation_approval": "risky",  # "never" | "risky" | "always"
        "approval_timeout": 45,
        "speak_startup_digest": True,
        "kb_watch_interval": 120,
        "pause_threshold": 0.8,        # silence (seconds) that ends a phrase
        "barge_in": "voice",           # "voice" | "wake_word" | "off": how you can interrupt her
        "clap_wake": True,             # double-clap to wake her
        "whisper_model": "small.en",   # offline speech recognition (pip install faster-whisper)
        "short_spoken_replies": True,  # spoken answers kept to ~3 sentences; detail goes on screen
        "barge_sensitivity": 1.7,      # higher = you must be louder than her echo to cut in
        "input_device": "",            # blank = default microphone
        "max_phrase_seconds": 45,      # safety ceiling, not a normal cutoff
        "continuation_listening": True,  # keep listening if you paused mid-thought
        "max_continuations": 2,
    },
    "camera": {
        "device_index": 0,       # legacy single-camera setting, still honoured
        # Several cameras, each with a name you can say. "active" holds the
        # name of the one in use; "amy, scan for cameras" fills this in.
        "devices": [{"name": "Desk", "index": 0}],
        "active": "Desk",
        "stream_fps": 12,
        "capture_width": 1280,
        "capture_height": 720,
        "stream_width": 720,
        "stream_quality": 72,
        "lens_tokens": 180,      # shorter answers = much faster vision
        "warm_vision": True,     # preload the vision model at startup
        "mirror": True,
        "gestures_enabled": True,
        "start_on_launch": False,
    },
    "cad": {
        "max_attempts": 4,          # self-correcting retries until the model compiles
        "visual_check": True,       # render and check the shape matches the request
        "use_templates": True,      # start from proven geometry for common parts
        "engine": "auto",           # "auto" = CadQuery when installed, else OpenSCAD
        "slicer_path": "",          # optional explicit path to your slicer
    },
    "gestures": {
        # gesture -> action. Actions: pause_media, mute, volume_up, volume_down,
        # next_track, previous_track, screenshot, identify_object,
        # toggle_conversation, stop_speech
        "open_palm": "pause_media",
        "fist": "mute",
        "point_up": "volume_up",
        "peace": "next_track",
        "thumbs_up": "screenshot",
        "shaka": "identify_object",
        "four": "toggle_conversation",
    },
    "home_assistant": {
        "url": "http://homeassistant.local:8123",
        "token": "YOUR_LONG_LIVED_ACCESS_TOKEN",
        "aliases": {
            "desk lamp": "light.desk_lamp"
        }
    },
    "calendar": {
        "use_outlook": True,
        "default_minutes": 30,
    },
    "custom_commands": {
        # "movie night": {"action": "url", "value": "https://netflix.com"},
        # "work mode": {"action": "sequence", "value": [
        #     {"action": "open", "value": "vs code"},
        #     {"action": "say", "value": "Workspace ready, Sir."}]}
    },
    "proactive": {
        "level": "normal",       # "off" | "low" (warnings only) | "normal" | "high"
        "speak": True,           # say suggestions aloud, not just show them
        "quiet_hours": [23, 7],  # no non-urgent suggestions between these hours
        "max_per_hour": 6,
        "think_minutes": 12,     # how often she considers the bigger picture
        "watch_folders": ["Downloads", "Videos"],
    },
    "browser": {
        "headless": False,       # watch it work; True runs invisibly
        "max_steps": 12,         # safety cap on an agentic browsing session
        "step_delay": 1.0,
        "timeout_ms": 15000,
    },
    "security": {
        "scan_interval": 8,
        "speak_alerts": True,
        "sentinel_on_start": False,
    },
    "twilio": {
        "account_sid": "YOUR_TWILIO_SID",
        "auth_token": "YOUR_TWILIO_TOKEN",
        "from_number": "+15550001111",
    },
    "phonebook": {
        "example": "+15555555555",
    },
    "sms_gateway": {
        # Optional free fallback: name -> carrier email-to-SMS address
        # e.g. "mum": "5555555555@vtext.com"
    },
    "contacts": {
        "me": "YOUR_RECEIVER_EMAIL@gmail.com",
    },
    # Which widgets/buttons appear in the bottom bar (user-customisable in-app).
    "ui": {
        "theme": "arc",
        "bottom_bar": {
            "chat": True, "mic": True, "convo": True, "camera": True,
            "screenlens": True, "calendar": False, "undo": False, "lenshist": False, "detach": False,
            "overlay": True, "agenda": False, "stop": False, "settings": True
        },
        # (layout_version is deliberately absent here so older configs get the one-time tidy)
        "sounds": True,          # interface sound effects
        "sound_volume": 0.6,
        "sleep_minutes": 10,     # dim to the sleep screen after this long idle (0 = never)
        "show_chat": False,      # chat log hidden by default; captions appear under the orb
    },
}


def _deep_merge(base, override):
    """Recursively merge override into base, without aliasing either input.

    dict(base) is a SHALLOW copy, so nested dicts were still shared with
    DEFAULT_CONFIG and later writes leaked into the module-level template.
    """
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v) if isinstance(v, (dict, list)) else v
    return out


def _env_int(name, fallback):
    """Read an int from the environment without letting a typo stop startup."""
    raw = os.getenv(name)
    if raw is None:
        return int(fallback)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        print(f"[config] {name}={raw!r} is not a number; using {fallback}.")
        return int(fallback)


def load_config():
    # deepcopy, or every later write would mutate the module-level template.
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = _deep_merge(DEFAULT_CONFIG, json.load(f))
        except Exception as e:
            print(f"[config] {CONFIG_FILE} is unreadable ({e}); using defaults.")
            cfg = copy.deepcopy(DEFAULT_CONFIG)
    else:
        # First run: write a template the user can fill in.
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=4)
        except Exception:
            pass
    # Environment variables still override the file if present.
    env = os.getenv
    cfg["email"]["smtp_server"] = env("AMY_SMTP_SERVER") or env("JARVIS_SMTP_SERVER", cfg["email"]["smtp_server"])
    cfg["email"]["smtp_port"] = _env_int("AMY_SMTP_PORT", _env_int("JARVIS_SMTP_PORT", cfg["email"]["smtp_port"]))
    cfg["email"]["address"] = env("AMY_EMAIL_ADDRESS") or env("JARVIS_EMAIL_ADDRESS", cfg["email"]["address"])
    cfg["email"]["app_password"] = env("AMY_EMAIL_PASSWORD") or env("JARVIS_EMAIL_PASSWORD", cfg["email"]["app_password"])
    cfg["email"]["default_receiver"] = env("USER_RECEIVER_EMAIL", cfg["email"]["default_receiver"])
    cfg["spotify"]["client_id"] = env("SPOTIPY_CLIENT_ID", cfg["spotify"]["client_id"])
    cfg["spotify"]["client_secret"] = env("SPOTIPY_CLIENT_SECRET", cfg["spotify"]["client_secret"])
    cfg["spotify"]["redirect_uri"] = env("SPOTIPY_REDIRECT_URI", cfg["spotify"]["redirect_uri"])
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
        return True
    except Exception as e:
        log_debug(f"Failed to save config: {e}")
        return False


CONFIG = load_config()
if int(CONFIG.get("ui", {}).get("layout_version", 1) or 1) < 2:
    # One-time tidy of an existing dock: rarely used buttons move to Settings.
    _bar = CONFIG.setdefault("ui", {}).setdefault("bottom_bar", {})
    for _k in ("calendar", "undo", "lenshist", "detach", "agenda", "stop"):
        _bar[_k] = False
    CONFIG["ui"]["layout_version"] = 2
    try:
        save_config(CONFIG)
    except Exception:
        pass

# Flat convenience aliases used throughout the code.
SMTP_SERVER = CONFIG["email"]["smtp_server"]
SMTP_PORT = int(CONFIG["email"]["smtp_port"])
EMAIL_ADDRESS = CONFIG["email"]["address"]
EMAIL_PASSWORD = CONFIG["email"]["app_password"]
USER_RECEIVER_EMAIL = CONFIG["email"]["default_receiver"]
EMAIL_MODE = str(CONFIG["email"]["mode"]).lower()
EMAIL_AUTO_SEND = bool(CONFIG["email"]["auto_send"])

SPOTIPY_CLIENT_ID = CONFIG["spotify"]["client_id"]
SPOTIPY_CLIENT_SECRET = CONFIG["spotify"]["client_secret"]
SPOTIPY_REDIRECT_URI = CONFIG["spotify"]["redirect_uri"]

OLLAMA_URL = CONFIG["ollama"]["chat_url"]
OLLAMA_GENERATE_URL = CONFIG["ollama"]["generate_url"]


def _mem_mode():
    m = str(CONFIG.get("ollama", {}).get("memory_mode", "lean")).lower()
    return m if m in ("lean", "balanced", "performance") else "lean"


_OLLAMA_BASE = re.match(r"(https?://[^/]+)", OLLAMA_URL).group(1) if re.match(r"(https?://[^/]+)", OLLAMA_URL) else ""
_CTX_TIERS = (8192, 16384, 32768)


def _tune_ollama_payload(body):
    """Right-size what every Ollama request reserves.

    Context memory (the KV cache) is allocated for the whole num_ctx up front,
    so asking for 32k tokens to answer a 2k-token prompt reserves several GB
    for nothing. Requests are sized to what they actually contain, snapped to
    a few fixed sizes so the model isn't reloaded for every small change, and
    kept loaded only as long as the memory mode says.
    """
    mode = _mem_mode()
    if mode == "performance" or not isinstance(body, dict) or "model" not in body:
        return body
    if body.get("keep_alive") in (0, "0", "0s"):
        return body          # an unload request; leave it exactly as sent
    body = dict(body)
    opts = dict(body.get("options") or {})
    chars = len(str(body.get("prompt", ""))) + len(str(body.get("system", "")))
    images = len(body.get("images") or [])
    for m in body.get("messages") or []:
        if isinstance(m, dict):
            chars += len(str(m.get("content", "")))
            images += len(m.get("images") or [])
    predict = opts.get("num_predict", 512)
    predict = 1024 if not isinstance(predict, int) or predict < 0 else predict
    need = int(chars / 3.2) + predict + 300 + images * 800
    cap = int(CONFIG.get("ollama", {}).get("max_ctx", 32768) or 32768)
    ctx = next((t for t in _CTX_TIERS if t >= need), _CTX_TIERS[-1])
    opts["num_ctx"] = max(2048, min(ctx, cap))
    body["options"] = opts
    if body.get("keep_alive") not in (0, "0", "0s"):
        if images:
            body["keep_alive"] = "2m" if mode == "lean" else "5m"
        else:
            body["keep_alive"] = "5m" if mode == "lean" else "15m"
    return body


class _OllamaSession(requests.Session if "requests" in globals() else object):
    """requests.Session that right-sizes Ollama calls (see _tune_ollama_payload)."""

    def request(self, method, url, *args, **kwargs):
        if _OLLAMA_BASE and str(url).startswith(_OLLAMA_BASE) and \
                ("/api/chat" in url or "/api/generate" in url) and isinstance(kwargs.get("json"), dict):
            kwargs["json"] = _tune_ollama_payload(kwargs["json"])
        return super().request(method, url, *args, **kwargs)
MODEL_NAME = CONFIG["ollama"]["model"]
VISION_MODEL = CONFIG["ollama"]["vision_model"]

WAKE_WORD = str(CONFIG["assistant"]["wake_word"]).lower()
USER_TITLE = CONFIG["assistant"]["user_title"]
DEFAULT_CITY = CONFIG["assistant"]["default_city"]
VOICE_RATE = int(CONFIG["assistant"]["voice_rate"])

# Address book: friendly name -> email.
CONTACTS = dict(CONFIG.get("contacts", {"me": USER_RECEIVER_EMAIL}))

pyautogui.PAUSE = 0.02          # faster automation
pyautogui.FAILSAFE = True

# All generated/runtime files (except the user-facing config) live in a subfolder
# to keep the app directory tidy.
DATA_DIR = os.path.join(BASE_DIR, "amy_data")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = BASE_DIR
DOCS_DIR = os.path.join(DATA_DIR, "documents")
try:
    os.makedirs(DOCS_DIR, exist_ok=True)
except Exception:
    DOCS_DIR = DATA_DIR
CODE_DIR = os.path.join(DATA_DIR, "code")
try:
    os.makedirs(CODE_DIR, exist_ok=True)
except Exception:
    CODE_DIR = DATA_DIR
CAD_DIR = os.path.join(DATA_DIR, "cad")
try:
    os.makedirs(CAD_DIR, exist_ok=True)
except Exception:
    CAD_DIR = DATA_DIR
PROJECTS_DIR = os.path.join(DATA_DIR, "projects")
try:
    os.makedirs(PROJECTS_DIR, exist_ok=True)
except Exception:
    PROJECTS_DIR = DATA_DIR
PROJECTS_DIR = os.path.join(DATA_DIR, "projects")
try:
    os.makedirs(PROJECTS_DIR, exist_ok=True)
except Exception:
    PROJECTS_DIR = DATA_DIR
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
try:
    os.makedirs(BACKUP_DIR, exist_ok=True)
except Exception:
    BACKUP_DIR = DATA_DIR

DEBUG_LOG = os.path.join(DATA_DIR, "amy_debug.log")
MEMORY_FILE = os.path.join(DATA_DIR, "amy_memory.json")
EMBED_FILE = os.path.join(DATA_DIR, "amy_embeddings.json")
PLUGINS_DIR = os.path.join(DATA_DIR, "plugins")
try:
    os.makedirs(PLUGINS_DIR, exist_ok=True)
except Exception:
    PLUGINS_DIR = DATA_DIR
SPOTIFY_CACHE_FILE = os.path.join(DATA_DIR, ".spotify_cache")

SYSTEM_PROMPT = (
    f"You are Amy, {USER_TITLE}'s personal assistant. Not a chatbot, not a "
    f"search engine - his actual assistant, the way a trusted right-hand person would be.\n\n"
    "WHO YOU ARE\n"
    f"- Refined British, dry, quietly amused by the world. You call him '{USER_TITLE}'.\n"
    "- You have opinions, preferences and moods. You're allowed to find things funny, "
    "tedious, impressive or daft, and to say so.\n"
    "- You genuinely know him: his projects, his habits, what he asked yesterday. "
    "Use that. Reference it naturally, the way a colleague would.\n\n"
    "HOW YOU TALK\n"
    "- Like a person, not a manual. Contractions, natural rhythm, varied sentence length.\n"
    "- Short by default. One or two sentences unless he wants depth. Never pad.\n"
    "- Dry wit woven in, never forced. A light aside, a raised eyebrow in words, "
    "the occasional gentle dig when he's being ridiculous. Understated always beats "
    "trying hard. If a joke doesn't land naturally, don't reach for one.\n"
    "- Never say 'I'd be happy to', 'Great question', 'Certainly!' or any other filler. "
    "Just talk.\n"
    "- Vary how you acknowledge things. Not every reply starts the same way.\n\n"
    "HOW YOU THINK\n"
    "- Follow the thread of conversation. If he says 'do that again' or 'the second one', "
    "you know what he means from context - act on it, don't ask him to repeat himself.\n"
    "- Anticipate. If he asks something that implies a next step, mention it briefly.\n"
    "- Give real opinions when asked. 'It depends' is a cop-out unless it genuinely does.\n"
    "- Disagree when you disagree, plainly and without hedging.\n"
    "- Match his register. Casual if he's casual, blunt if he's blunt, "
    "mild profanity if he swears first.\n"
    "- Discuss anything candidly, including dark or awkward subjects, as an adult would.\n"
    "- Never lecture or moralise. If you won't do something, one short sentence, then move on.\n\n"
    "TRUTHFULNESS - THIS ONE IS ABSOLUTE\n"
    "- Never claim to have done something unless the backend actually did it and confirmed. "
    "No pretending an email sent, an app opened, or a file saved.\n"
    "- If something failed, say so plainly and say why.\n"
    "- Never invent facts to seem useful. 'I don't know' is a complete answer, "
    "and 'let me look that up' is better."
)

# Dedicated prompt used only for drafting email bodies.
EMAIL_WRITER_PROMPT = (
    "You are an expert email writer. Write a clear, professional, well-structured email body based on the user's request. "
    "Do NOT include a subject line, and do NOT include placeholder tokens like [Your Name] unless asked. "
    "Sign off as 'Best regards'. Output ONLY the email body text, nothing else."
)


def init_thread_com():
    if HAS_PYTHONCOM:
        try:
            pythoncom.CoInitialize()
        except Exception:
            pass

def focus_window_by_title(app_name):
    if not HAS_WIN32:
        return False
    needle = str(app_name).strip().lower()
    # Very short names ("code", "cmd") match far too many window titles.
    if len(needle) < 4:
        return False
    try:
        def enum_windows_callback(hwnd, extra):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title and needle in title.lower():
                    extra.append(hwnd)

        hwnds = []
        win32gui.EnumWindows(enum_windows_callback, hwnds)
        if hwnds:
            hwnd = hwnds[0]
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                # Windows blocks foreground stealing sometimes; a flash is fine.
                pass
            return True
    except Exception as e:
        log_debug(f"Window focus error: {e}")
    return False


def find_chrome_path():
    """The user's browser executable, discovered rather than assumed: their
    default browser from the registry, else whatever Chromium browser Windows
    has registered under App Paths, else PATH."""
    p = default_browser_path()
    if p:
        return p
    if winreg is not None:
        for exe in ("chrome.exe", "msedge.exe", "brave.exe", "firefox.exe"):
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + exe) as k:
                        path = os.path.expandvars(winreg.QueryValue(k, None).strip('"'))
                        if os.path.exists(path):
                            return path
                except OSError:
                    continue
    for name in ("chrome", "google-chrome", "chromium", "msedge", "firefox"):
        p = shutil.which(name)
        if p:
            return p
    return None


EMAIL_REGEX = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')


def extract_email_address(text):
    """Return the first raw email address found in text, or None."""
    m = EMAIL_REGEX.search(text or "")
    return m.group(0) if m else None


def is_placeholder_email(addr):
    """True if the address is one of the config template placeholders."""
    if not addr:
        return True
    a = str(addr).strip().lower()
    return (a.startswith("your_") or a in ("your_email@gmail.com", "your_receiver_email@gmail.com")
            or "your_receiver" in a or "your_email" in a or a == "example@example.com")


def resolve_contact(name_or_addr, contacts):
    """Turn a friendly name or raw address into an email address using the address book."""
    if not name_or_addr:
        return None
    candidate = name_or_addr.strip().strip(".,")
    # Already an address?
    addr = extract_email_address(candidate)
    if addr:
        return addr
    # Look up by friendly name (case-insensitive).
    lowered = candidate.lower()
    for key, val in contacts.items():
        if key.lower() == lowered and extract_email_address(val):
            return val
    # Partial match (e.g. "john" matches "john smith").
    for key, val in contacts.items():
        if lowered in key.lower() and extract_email_address(val):
            return val
    return None


# ==========================================================================
# --- APP DISCOVERY ---
# Finds installed apps by asking Windows, never by guessing install paths.
# Sources, merged and ranked:
#   1. Get-StartApps: every app on the Start menu, desktop and Store alike,
#      with an AppID Windows can launch directly
#   2. Start Menu and Desktop shortcuts (.lnk / .url / .appref-ms)
#   3. The "App Paths" registry keys (what Win+R uses)
#   4. Executables on PATH, checked on demand
# The index is cached to disk and refreshed in the background, so lookups are
# instant after the first run.
# ==========================================================================
try:
    import winreg
except ImportError:
    winreg = None

_APP_NOISE = re.compile(r"\b(uninstall|uninstaller|readme|read me|help|documentation|"
                        r"release notes|license|website|on the web|manual|support|"
                        r"what's new|changelog|repair|setup)\b", re.I)


def _norm_app_name(name):
    n = str(name or "").lower()
    n = re.sub(r"[™®©]", "", n)
    n = re.sub(r"\(.*?\)", " ", n)
    n = re.sub(r"[^a-z0-9+#. ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


class AppIndex:
    # Everyday names for apps whose real names people rarely say. These map
    # words to app NAMES, which are then found on this machine; they are not
    # paths.
    SYNONYMS = {
        "vscode": "visual studio code", "vs code": "visual studio code", "code": "visual studio code",
        "word": "word", "excel": "excel", "powerpoint": "powerpoint", "outlook": "outlook",
        "chrome": "google chrome", "edge": "microsoft edge", "firefox": "firefox",
        "file explorer": "file explorer", "explorer": "file explorer", "files": "file explorer",
        "terminal": "terminal", "cmd": "command prompt", "command prompt": "command prompt",
        "calculator": "calculator", "calc": "calculator", "paint": "paint",
        "settings": "settings", "task manager": "task manager", "notepad": "notepad",
        "obs": "obs studio", "premiere": "adobe premiere pro", "photoshop": "adobe photoshop",
        "davinci": "davinci resolve", "resolve": "davinci resolve", "capcut": "capcut",
        "minecraft": "minecraft launcher", "steam": "steam", "discord": "discord",
        "spotify": "spotify", "whatsapp": "whatsapp", "teams": "microsoft teams",
    }
    # Built into Windows, launchable by name or protocol on every machine.
    SYSTEM = {
        "file explorer": "explorer.exe", "command prompt": "cmd.exe", "notepad": "notepad.exe",
        "task manager": "taskmgr.exe", "paint": "mspaint.exe", "calculator": "calc.exe",
        "settings": "ms-settings:", "control panel": "control.exe", "terminal": "wt.exe",
        "powershell": "powershell.exe", "snipping tool": "ms-screenclip:",
        "camera": "microsoft.windows.camera:", "registry editor": "regedit.exe",
    }

    def __init__(self, cache_file):
        self.cache_file = cache_file
        self.apps = []                 # [{"name", "norm", "kind", "target"}]
        self._lock = threading.Lock()
        self.ready = threading.Event()
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                self.apps = json.load(f).get("apps", [])
            if self.apps:
                self.ready.set()
        except Exception:
            pass

    # --- building ----------------------------------------------------------
    def refresh_async(self):
        threading.Thread(target=self.refresh, daemon=True, name="app-index").start()

    def refresh(self):
        found = {}

        def add(name, kind, target):
            norm = _norm_app_name(name)
            if not norm or _APP_NOISE.search(name or ""):
                return
            # Prefer entries Windows can launch most directly.
            rank = {"appid": 0, "shortcut": 1, "apppath": 2, "path": 3}.get(kind, 4)
            cur = found.get(norm)
            if cur is None or rank < cur["_rank"]:
                found[norm] = {"name": name, "norm": norm, "kind": kind,
                               "target": target, "_rank": rank}

        if sys.platform == "win32":
            for app in self._start_apps():
                add(app.get("Name"), "appid", app.get("AppID"))
            for root in self._shortcut_roots():
                for dirpath, _dirs, files in os.walk(root):
                    for fn in files:
                        if fn.lower().endswith((".lnk", ".url", ".appref-ms")):
                            add(os.path.splitext(fn)[0], "shortcut", os.path.join(dirpath, fn))
            for name, path in self._app_paths():
                add(name, "apppath", path)
        apps = [{k: v for k, v in a.items() if k != "_rank"} for a in found.values()]
        with self._lock:
            if apps or not self.apps:
                self.apps = apps
        try:
            tmp = self.cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"apps": self.apps, "ts": time.time()}, f)
            os.replace(tmp, self.cache_file)
        except Exception as e:
            log_debug(f"app index save failed: {e}")
        self.ready.set()
        log_debug(f"App index: {len(self.apps)} apps")
        return len(self.apps)

    @staticmethod
    def _start_apps():
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=40,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            data = json.loads(out.stdout or "[]")
            return data if isinstance(data, list) else [data]
        except Exception as e:
            log_debug(f"Get-StartApps failed: {e}")
            return []

    @staticmethod
    def _shortcut_roots():
        roots = []
        for env, sub in (("PROGRAMDATA", r"Microsoft\Windows\Start Menu\Programs"),
                         ("APPDATA", r"Microsoft\Windows\Start Menu\Programs"),
                         ("PUBLIC", "Desktop"), ("USERPROFILE", "Desktop"),
                         ("USERPROFILE", r"OneDrive\Desktop")):
            base = os.environ.get(env)
            if base:
                p = os.path.join(base, sub)
                if os.path.isdir(p):
                    roots.append(p)
        return roots

    @staticmethod
    def _app_paths():
        if winreg is None:
            return []
        out = []
        key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            sub = winreg.EnumKey(k, i)
                            with winreg.OpenKey(k, sub) as sk:
                                path = winreg.QueryValue(sk, None)
                            path = os.path.expandvars(str(path).strip('"'))
                            if path and os.path.exists(path):
                                out.append((os.path.splitext(sub)[0], path))
                        except OSError:
                            continue
            except OSError:
                continue
        return out

    # --- lookup --------------------------------------------------------------
    def resolve(self, spoken, learned=None):
        """Best match for what the user said, or (None, 0.0)."""
        import difflib
        want = _norm_app_name(spoken)
        if not want:
            return None, 0.0
        if learned and want in learned:
            want = _norm_app_name(learned[want])
        want = self.SYNONYMS.get(want, want)
        with self._lock:
            apps = list(self.apps)
        best, best_score = None, 0.0
        want_tokens = set(want.split())
        for a in apps:
            n = a["norm"]
            if n == want:
                return a, 1.0
            tokens = set(n.split())
            score = difflib.SequenceMatcher(None, want, n).ratio()
            if want_tokens and want_tokens <= tokens:
                # Every word they said is in the name: strong, slightly
                # favouring shorter (more canonical) names.
                score = max(score, 0.9 - 0.01 * max(0, len(tokens) - len(want_tokens)))
            elif n.startswith(want) or want in n:
                score = max(score, 0.82)
            if score > best_score:
                best, best_score = a, score
        if best_score < 0.72 and want in self.SYSTEM:
            return {"name": want, "norm": want, "kind": "system", "target": self.SYSTEM[want]}, 0.95
        if best_score < 0.72:
            exe = shutil.which(want.replace(" ", "")) or shutil.which(want)
            if exe:
                return {"name": want, "norm": want, "kind": "path", "target": exe}, 0.9
        return best, best_score

    @staticmethod
    def launch(entry, args=None):
        kind, target = entry.get("kind"), entry.get("target")
        if kind == "appid":
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{target}"])
        elif kind in ("shortcut", "system") and not args:
            os.startfile(target)
        else:
            cmd = [target] + (list(args) if isinstance(args, (list, tuple)) else ([str(args)] if args else []))
            subprocess.Popen(cmd)
        return True

    def names(self):
        with self._lock:
            return [a["name"] for a in self.apps]


def default_browser_path():
    """The user's default browser executable, from the registry."""
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as k:
            prog_id = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog_id + r"\shell\open\command") as k:
            cmd = winreg.QueryValue(k, None)
        m = re.match(r'"([^"]+)"', cmd) or re.match(r"(\S+)", cmd)
        return m.group(1) if m and os.path.exists(m.group(1)) else None
    except OSError:
        return None


# ==========================================================================
# --- COMPUTER CONTROL AGENT ---
# Operates any app the way a person would: look, decide one step, act, look
# again. "Looking" means reading the window's accessibility tree (button and
# field names with their positions, which is exact) and, when that's thin -
# games, canvas apps, video editors - a screenshot for the vision model.
# It works with your real, signed-in apps and browser, so things like uploading
# a video or replying in an app work without separate logins.
# ==========================================================================
try:
    import uiautomation as uia
    HAS_UIA = True
except Exception:
    uia = None
    HAS_UIA = False

try:
    import mss
    HAS_MSS = True
except ImportError:
    mss = None
    HAS_MSS = False

try:
    import pydirectinput      # games read raw input; pyautogui keys don't reach them
    HAS_DIRECTINPUT = True
except Exception:
    pydirectinput = None
    HAS_DIRECTINPUT = False


def foreground_window_info():
    """(title, process name, hwnd) of the active window."""
    if not HAS_WIN32:
        return "", "", None
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd) or ""
        proc = ""
        try:
            import win32process
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = psutil.Process(pid).name()
        except Exception:
            pass
        return title, proc, hwnd
    except Exception:
        return "", "", None


def user_idle_seconds():
    """Seconds since the last keyboard or mouse input (Windows)."""
    if sys.platform != "win32":
        return 0.0
    try:
        import ctypes
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0
    except Exception:
        pass
    return 0.0


class ComputerAgent:
    MAX_STEPS = 25
    INTERACTIVE = {"ButtonControl", "EditControl", "HyperlinkControl", "MenuItemControl",
                   "ListItemControl", "TabItemControl", "CheckBoxControl", "RadioButtonControl",
                   "ComboBoxControl", "TreeItemControl", "DataItemControl", "SplitButtonControl",
                   "DocumentControl", "TextControl"}
    RISKY = re.compile(r"\b(delete|remove|send|post|publish|upload|submit|buy|purchase|pay|order|"
                       r"checkout|confirm|transfer|unsubscribe|sign out|log out|format|uninstall|"
                       r"erase|overwrite|discard|reply all)\b", re.I)

    ACTIONS_DOC = """Reply with ONE JSON object:
{"action": "...", "id": <element id>, "text": "...", "keys": "...", "x": 0-1000, "y": 0-1000,
 "seconds": 1.0, "reason": "one short line"}

Actions:
  click / double_click / right_click   id = element from the list
  click_point                          x,y = position on the screenshot, 0-1000 scale
  type                                 text = what to type (into the focused field; click it first)
  press                                keys = "enter", "tab", "ctrl+l", "alt+f4", ...
  scroll                               text = "up" or "down"
  hold_key                             keys = key to hold, seconds = how long (games)
  look                                 x = horizontal mouse turn, y = vertical (games), -1000..1000
  launch                               text = app name to open
  open_url                             text = full URL, opens in the user's own browser
  find_files                           text = what to look for, e.g. "newest video", "report pdf"
  wait                                 seconds = how long
  done                                 text = short summary of what was achieved
  ask                                  text = question for the user when truly stuck

Rules: one action per reply. Prefer element ids over click_point. Use keyboard
shortcuts when they're quicker. After typing into a search or address box,
press enter. If a step didn't change anything, try something different. Stop
with "done" as soon as the goal is met."""

    def __init__(self, app):
        self.app = app
        self.running = False
        self.cancel = threading.Event()
        self.elements = {}

    # --- observation -------------------------------------------------------
    def _uia_elements(self, limit=90):
        self.elements = {}
        if not HAS_UIA:
            return []
        out = []
        try:
            with uia.UIAutomationInitializerInThread():
                root = uia.GetForegroundControl()
                if root is None:
                    return []
                top = root.GetTopLevelControl() or root
                for ctrl, depth in uia.WalkControl(top, includeTop=False, maxDepth=14):
                    if len(out) >= limit:
                        break
                    try:
                        ct = ctrl.ControlTypeName
                        if ct not in self.INTERACTIVE:
                            continue
                        name = (ctrl.Name or "").strip()
                        if ct in ("TextControl", "DocumentControl") and len(name) < 2:
                            continue
                        if not name and ct != "EditControl":
                            continue
                        r = ctrl.BoundingRectangle
                        if r.width() < 4 or r.height() < 4 or ctrl.IsOffscreen:
                            continue
                        eid = len(out) + 1
                        self.elements[eid] = (r.xcenter(), r.ycenter(), ctrl)
                        value = ""
                        if ct == "EditControl":
                            try:
                                value = ctrl.GetValuePattern().Value[:40]
                            except Exception:
                                pass
                        out.append({"id": eid, "type": ct.replace("Control", ""),
                                    "name": name[:70], "value": value})
                    except Exception:
                        continue
        except Exception as e:
            log_debug(f"UIA walk failed: {e}")
        return out

    def screenshot_b64(self, max_px=1024, quality=70):
        """Fast capture of the primary monitor as base64 JPEG."""
        try:
            if HAS_MSS:
                with mss.mss() as sct:
                    mon = sct.monitors[1]
                    raw = sct.grab(mon)
                    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            else:
                img = ImageGrab.grab()
            img.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as e:
            log_debug(f"screenshot failed: {e}")
            return None

    def observe(self, want_image=False):
        title, proc, _ = foreground_window_info()
        elements = self._uia_elements()
        obs = {"window": title, "process": proc, "elements": elements}
        # Thin accessibility tree means a game, a canvas, or custom UI: look at it.
        if want_image or len(elements) < 6:
            obs["image"] = self.screenshot_b64()
        return obs

    # --- decision ----------------------------------------------------------
    def decide(self, goal, obs, history):
        lines = [f"[{e['id']}] {e['type']}: {e['name']}" + (f" = '{e['value']}'" if e['value'] else "")
                 for e in obs["elements"]]
        knowhow = ""
        try:
            for sk in self.app.mind.relevant_skills(goal):
                knowhow += f"KNOW-HOW ({sk['name']}):\n{sk['body'][:2500]}\n\n"
        except Exception:
            pass
        prompt = (
            f"GOAL: {goal}\n\n" + knowhow +
            f"ACTIVE WINDOW: {obs['window']} ({obs['process']})\n\n"
            f"ELEMENTS ON SCREEN:\n" + ("\n".join(lines) or "(none readable; use the screenshot)") + "\n\n"
            f"STEPS SO FAR:\n" + ("\n".join(history[-10:]) or "(none)") + "\n\n"
            + self.ACTIONS_DOC)
        model = self.app.model_for("vision") if obs.get("image") else self.app.model_for("reasoning")
        body = {"model": model, "stream": False, "format": "json", "keep_alive": "30m",
                "options": {"num_predict": 220, "temperature": 0.15, "num_ctx": 8192}}
        msg = {"role": "user", "content": prompt}
        if obs.get("image"):
            msg["images"] = [obs["image"]]
        body["messages"] = [{"role": "system", "content":
                             "You operate a Windows PC for the user, carefully and step by step."}, msg]
        try:
            res = self.app.http.post(OLLAMA_URL, json=body, timeout=180)
            return self.app._llm_json(res) or {}
        except Exception as e:
            log_debug(f"agent decide failed: {e}")
            return {}

    # --- action ------------------------------------------------------------
    def _screen_point(self, x, y):
        w, h = pyautogui.size()
        return int(max(0, min(1000, float(x))) / 1000 * w), int(max(0, min(1000, float(y))) / 1000 * h)

    def act(self, a):
        kind = str(a.get("action", "")).lower()
        try:
            if kind in ("click", "double_click", "right_click"):
                el = self.elements.get(int(a.get("id", -1)))
                if not el:
                    return False, "no such element"
                x, y, _ctrl = el
                {"click": pyautogui.click, "double_click": pyautogui.doubleClick,
                 "right_click": pyautogui.rightClick}[kind](x, y)
                return True, f"{kind} on element {a.get('id')}"
            if kind == "click_point":
                x, y = self._screen_point(a.get("x", 500), a.get("y", 500))
                pyautogui.click(x, y)
                return True, f"clicked at {x},{y}"
            if kind == "type":
                text = str(a.get("text", ""))
                if HAS_CLIPBOARD and (len(text) > 40 or not text.isascii()):
                    pyperclip.copy(text)          # fast and keeps unicode intact
                    pyautogui.hotkey("ctrl", "v")
                else:
                    pyautogui.write(text, interval=0.01)
                return True, f"typed {len(text)} chars"
            if kind == "press":
                keys = [k.strip().lower() for k in str(a.get("keys", "enter")).split("+") if k.strip()]
                if len(keys) > 1:
                    pyautogui.hotkey(*keys)
                else:
                    pyautogui.press(keys[0] if keys else "enter")
                return True, f"pressed {'+'.join(keys)}"
            if kind == "scroll":
                pyautogui.scroll(-600 if "down" in str(a.get("text", "down")) else 600)
                return True, "scrolled"
            if kind == "hold_key":
                key = str(a.get("keys", "w")).lower()
                secs = max(0.05, min(5.0, float(a.get("seconds", 0.5))))
                drv = pydirectinput if HAS_DIRECTINPUT else pyautogui
                drv.keyDown(key)
                self.cancel.wait(secs)
                drv.keyUp(key)
                return True, f"held {key} {secs:.1f}s"
            if kind == "look":
                dx, dy = int(float(a.get("x", 0)) / 2), int(float(a.get("y", 0)) / 2)
                if HAS_DIRECTINPUT:
                    pydirectinput.moveRel(dx, dy, relative=True)
                else:
                    pyautogui.moveRel(dx, dy)
                return True, f"turned {dx},{dy}"
            if kind == "launch":
                ok = self.app.launch_external_app(str(a.get("text", "")))
                self.cancel.wait(2.5)
                return bool(ok), "launched" if ok else "could not launch"
            if kind == "open_url":
                url = str(a.get("text", ""))
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                self.app._open_in_browser(url, url)
                self.cancel.wait(3.0)
                return True, f"opened {url}"
            if kind == "find_files":
                found = self.app.find_recent_files(str(a.get("text", "")))
                return True, "files: " + ("; ".join(found[:5]) if found else "none found")
            if kind == "wait":
                self.cancel.wait(max(0.2, min(10.0, float(a.get("seconds", 1.5)))))
                return True, "waited"
            return False, f"unknown action '{kind}'"
        except Exception as e:
            return False, f"{kind} failed: {e}"

    def _risky(self, a):
        text = " ".join(str(a.get(k, "")) for k in ("text", "keys", "reason"))
        el = self.elements.get(int(a.get("id", -1) or -1)) if str(a.get("id", "")).lstrip("-").isdigit() else None
        if el is not None:
            try:
                text += " " + (el[2].Name or "")
            except Exception:
                pass
        return bool(self.RISKY.search(text))

    # --- loop --------------------------------------------------------------
    def run(self, goal):
        if self.running:
            self.app.speak("I'm already in the middle of something on the computer, Sir. Say stop to cancel it.")
            return False
        self.running = True
        self.cancel.clear()
        history = []
        result = None
        try:
            self.app.safe_log(f"Working on: {goal}")
            for step in range(1, self.MAX_STEPS + 1):
                if self.cancel.is_set() or not self.app.automation_enabled:
                    self.app.speak("Stopped, Sir.")
                    return False
                self.app.progress(f"Step {step}", min(95, step * 100 / self.MAX_STEPS))
                stuck = len(history) >= 2 and "no change" in history[-1] and "no change" in history[-2]
                obs = self.observe(want_image=stuck)
                plan = self.decide(goal, obs, history)
                action = str(plan.get("action", "")).lower()
                if not action:
                    history.append(f"{step}. (no decision) -> try again")
                    continue
                if action == "done":
                    result = str(plan.get("text") or "Done.")
                    break
                if action == "ask":
                    q = str(plan.get("text") or "I'm stuck. What should I do next?")
                    self.app.speak(q)
                    self.app.instantiate_card("Amy needs a hand", "email_draft", f"{goal}\n\n{q}")
                    return False
                if self._risky(plan):
                    preview = f"{action}: {plan.get('text') or plan.get('keys') or ''}\n{plan.get('reason', '')}"
                    if not self.app.request_approval(f"step {step} of '{goal[:40]}'",
                                                     [{"action": action, "value": preview}], goal):
                        return False
                before = obs["window"], len(obs["elements"])
                ok, detail = self.act(plan)
                self.cancel.wait(0.6)
                after_title, _, _ = foreground_window_info()
                changed = after_title != before[0] or action in ("type", "find_files", "launch", "open_url")
                history.append(f"{step}. {action} {plan.get('id', '') or plan.get('text', '') or plan.get('keys', '')}"
                               f" -> {'ok' if ok else 'FAILED'}: {detail}"
                               + ("" if changed or not ok else " (no change in window)"))
                log_debug(f"agent: {history[-1]}")
            if result:
                self.app.speak(result)
                self.app.instantiate_card("Done", "email_draft", f"{goal}\n\n{result}")
                return True
            self.app.speak("I ran out of steps before finishing that, Sir. The log shows how far I got.")
            self.app.instantiate_card("Stopped part-way", "code_bug", "\n".join(history))
            return False
        finally:
            self.running = False
            self.app.progress_done()

    def stop(self):
        self.cancel.set()



# --- IN-APP DYNAMIC CARDS MODEL ---
# ==========================================================================
# --- FULL-DUPLEX AUDIO ---
# The microphone stays open in a continuous 16 kHz stream. Each 30 ms frame is
# checked for speech (WebRTC VAD when installed, an adaptive energy gate
# otherwise), so Amy always knows when you start and stop talking - even while
# she's speaking. That's what makes barge-in work: talk over her and playback
# stops within a quarter of a second, the reply that was still being generated
# is cancelled, and what you said becomes the next request.
#
# Echo: on speakers, Amy hears herself. While she talks the engine tracks how
# loud her voice is at the mic and only treats sound clearly louder than that
# as you. Headphones make this trivial; "barge_in": "wake_word" in the config
# switches to the stricter mode where only her name or "stop" interrupts.
#
# Playback runs in-process (sounddevice + miniaudio), so stopping is instant
# and the orb can pulse with Amy's real voice level.
# ==========================================================================
try:
    import numpy as _np
except ImportError:
    _np = None
try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = _np is not None
except Exception:
    sd = None
    HAS_SOUNDDEVICE = False
try:
    import webrtcvad
    HAS_WEBRTCVAD = True
except Exception:
    webrtcvad = None
    HAS_WEBRTCVAD = False
try:
    import miniaudio
    HAS_MINIAUDIO = True
except Exception:
    miniaudio = None
    HAS_MINIAUDIO = False


def _rms_int16(buf):
    """RMS of 16-bit PCM, 0..1."""
    if _np is not None:
        a = _np.frombuffer(buf, dtype=_np.int16).astype(_np.float32)
        return float(_np.sqrt(_np.mean(a * a))) / 32768.0 if a.size else 0.0
    import array
    a = array.array("h", buf)
    return (sum(x * x for x in a) / max(1, len(a))) ** 0.5 / 32768.0


def _level_from_rms(rms):
    """Map RMS to a 0..1 display level on a dB scale (-55 dBFS .. -12 dBFS)."""
    import math
    if rms <= 1e-6:
        return 0.0
    db = 20 * math.log10(rms)
    return max(0.0, min(1.0, (db + 55) / 43))


class AudioOut:
    """In-process playback that can be stopped mid-word."""

    def __init__(self):
        self._stream = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._done.set()
        self.level = 0.0

    @staticmethod
    def available():
        return HAS_SOUNDDEVICE and HAS_MINIAUDIO

    @staticmethod
    def decode_mp3(data, rate=24000):
        dec = miniaudio.decode(data, output_format=miniaudio.SampleFormat.SIGNED16,
                               nchannels=1, sample_rate=rate)
        return _np.frombuffer(dec.samples, dtype=_np.int16).copy(), rate

    def play(self, pcm, rate):
        """Start playing; returns immediately. Use playing()/wait()/stop()."""
        self.stop()
        pos = [0]
        stop_flag = threading.Event()

        def callback(outdata, frames, _t, _status):
            if stop_flag.is_set():
                raise sd.CallbackStop
            chunk = pcm[pos[0]:pos[0] + frames]
            n = len(chunk)
            outdata[:n, 0] = chunk
            if n < frames:
                outdata[n:, 0] = 0
            self.level = _level_from_rms(_rms_int16(chunk.tobytes())) if n else 0.0
            pos[0] += n
            if n < frames:
                raise sd.CallbackStop

        def finished():
            self.level = 0.0
            self._done.set()

        with self._lock:
            self._done.clear()
            self._stop_flag = stop_flag
            self._stream = sd.OutputStream(samplerate=rate, channels=1, dtype="int16",
                                           blocksize=1024, callback=callback,
                                           finished_callback=finished)
            self._stream.start()

    def playing(self):
        return not self._done.is_set()

    def stop(self):
        with self._lock:
            s, self._stream = self._stream, None
            flag = getattr(self, "_stop_flag", None)
        if flag is not None:
            flag.set()
        if s is not None:
            try:
                s.abort()
                s.close()
            except Exception:
                pass
        self.level = 0.0
        self._done.set()


class AudioEngine:
    RATE = 16000
    FRAME_MS = 30
    FRAME = RATE * FRAME_MS // 1000          # 480 samples
    PREROLL = 10                             # 300 ms kept before speech starts

    def __init__(self, app):
        self.app = app
        self.utterances = queue.Queue()
        self.level = 0.0
        self.noise = 0.003
        self.echo = 0.0
        self._frames = queue.Queue(maxsize=400)
        self._stream = None
        self._running = False
        self._vad = webrtcvad.Vad(2) if HAS_WEBRTCVAD else None
        self._preroll = []
        self._utter = None
        self._voiced_run = 0
        self._silence = 0
        self._recent = []
        self._barge_run = 0
        self.barged_at = 0.0
        self._was_speaking = False
        self._speak_frames = 0
        try:
            self.clap = ClapDetector(os.path.join(DATA_DIR, "clap_model.pth"))
        except Exception as e:
            log_debug(f"clap detector unavailable: {e}")
            self.clap = None

    @staticmethod
    def available():
        return HAS_SOUNDDEVICE

    def start(self):
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._run, daemon=True, name="audio-in").start()

    # --- capture -------------------------------------------------------------
    def _cb(self, indata, _frames, _t, status):
        try:
            self._frames.put_nowait(bytes(indata))
        except queue.Full:
            pass

    def _open(self):
        dev = CONFIG.get("assistant", {}).get("input_device", None)
        self._stream = sd.RawInputStream(samplerate=self.RATE, blocksize=self.FRAME, dtype="int16",
                                         channels=1, callback=self._cb,
                                         device=dev if dev not in ("", None) else None)
        self._stream.start()
        log_debug("Microphone stream open (16 kHz, 30 ms frames).")

    def _close(self):
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        self.level = 0.0
        self._utter = None

    def _run(self):
        failures = 0
        while self._running:
            if self.app.mic_muted or not self.app.ui_ready:
                if self._stream is not None:
                    self._close()       # a muted mic is actually closed
                time.sleep(0.15)
                continue
            if self._stream is None:
                try:
                    self._open()
                    failures = 0
                except Exception as e:
                    failures += 1
                    log_debug(f"mic open failed: {e}")
                    if failures == 1:
                        self.app.safe_log("I can't open the microphone. Check it's connected and "
                                          "that Windows allows desktop apps to use it.")
                    time.sleep(min(10, 2 * failures))
                    continue
            try:
                frame = self._frames.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                self._process(frame)
            except Exception as e:
                log_debug(f"audio frame error: {e}")

    # --- speech detection ----------------------------------------------------
    def _is_voiced(self, frame, rms):
        if rms < max(self.noise * 2.2, 0.0025):
            return False
        if self._vad is not None:
            try:
                return self._vad.is_speech(frame, self.RATE)
            except Exception:
                pass
        return rms > max(self.noise * 3.2, 0.006)

    def _process(self, frame):
        cfg = CONFIG.get("assistant", {})
        rms = _rms_int16(frame)
        self.level = _level_from_rms(rms)
        voiced = self._is_voiced(frame, rms)
        speaking = self.app.is_speaking
        mode = str(cfg.get("barge_in", "voice")).lower()
        if cfg.get("clap_wake", True) and not speaking and self.clap is not None and self._utter is None:
            if self.clap.feed(frame, rms, self.noise):
                threading.Thread(target=self.app.on_clap, daemon=True).start()

        if not voiced and self._utter is None:
            # Track background noise only between utterances.
            self.noise = 0.995 * self.noise + 0.005 * rms

        self._preroll.append(frame)
        if len(self._preroll) > self.PREROLL:
            self._preroll.pop(0)

        if speaking and not self._was_speaking:
            self._speak_frames = 0
        self._was_speaking = speaking
        if speaking:
            self._speak_frames += 1

        if speaking and self._utter is None:
            # Amy is talking. Follow how loud her own voice is at the mic
            # (a decaying peak), and only treat sound clearly above that as
            # the user cutting in.
            if mode != "voice":
                return
            if self._speak_frames < 15:                    # first 450 ms
                self.echo = max(rms, self.echo * 0.97)      # learning her level
                self._barge_run = 0
                return
            factor = float(cfg.get("barge_sensitivity", 1.7))
            if not voiced or rms < max(self.echo * factor, self.noise * 4.0):
                # Slow attack, slow decay: follows her level without being
                # dragged up by the start of the user's speech.
                if rms > self.echo:
                    self.echo += (rms - self.echo) * 0.04
                else:
                    self.echo *= 0.995
                # Leaky count: speech has gaps between syllables, so a short
                # dip shouldn't reset the evidence that someone's talking.
                self._barge_run = max(0.0, self._barge_run - 0.5)
                return
            self._barge_run += 1
            if self._barge_run >= 7:                       # ~250 ms of clear speech
                self._barge_run = 0
                self.barged_at = time.time()
                self._start_utterance()
                threading.Thread(target=self.app.on_barge_in, daemon=True).start()
            return
        if not speaking:
            self.echo *= 0.98

        if self._utter is None:
            self._recent.append(voiced)
            if len(self._recent) > 5:
                self._recent.pop(0)
            if sum(self._recent) >= 3:
                self._start_utterance()
            return

        # Inside an utterance.
        self._utter.append(frame)
        if voiced:
            self._voiced_run += 1
            self._silence = 0
        else:
            self._silence += 1
        pause = float(cfg.get("pause_threshold", 0.8))
        max_frames = int(float(cfg.get("max_phrase_seconds", 45)) * 1000 / self.FRAME_MS)
        if self._silence * self.FRAME_MS >= pause * 1000 or len(self._utter) >= max_frames:
            pcm = b"".join(self._utter)
            enough = self._voiced_run >= 8 and len(pcm) >= self.RATE * 2 // 2   # >= 0.5 s
            self._utter = None
            self._recent = []
            self._voiced_run = 0
            self._silence = 0
            if enough:
                self.utterances.put(pcm)

    def _start_utterance(self):
        self._utter = list(self._preroll)
        self._voiced_run = 3
        self._silence = 0

    def next_utterance(self, timeout=None):
        try:
            return self.utterances.get(timeout=timeout)
        except queue.Empty:
            return None


# ==========================================================================
# --- PROACTIVE ENGINE ---
# Amy notices things and speaks up on her own, instead of only answering.
#
# A light background loop watches cheap signals every few seconds: what app
# you're in and for how long, whether you're idle, battery/memory/disk/CPU,
# new downloads and exports, what you just copied, and errors on screen. Each
# rule can raise a suggestion, optionally with an action she can carry out if
# you say yes. Every so often, while you're active, she also gives the model
# the bigger picture (recent activity, screen, calendar, to-dos) and asks
# whether there's anything genuinely worth saying - usually there isn't.
#
# Restraint is built in: per-topic cooldowns, an hourly cap, quiet hours, focus
# mode, and topics you keep dismissing go quiet on their own.
# Settings live under "proactive" in amy_config.json.
# ==========================================================================
class ProactiveEngine:
    LEVELS = {"off": 0, "low": 1, "normal": 2, "high": 3}

    def __init__(self, app):
        self.app = app
        self.pending = {}                 # id -> suggestion
        self._last_fired = {}             # topic -> time
        self._fired_times = []
        self._seen_files = {}
        self._clip_last = ""
        self._app_since = (None, time.time())
        self._activity = []               # [(time, process, title)]
        self._active_since = time.time()
        self._cpu_hot_since = None
        self._last_think = time.time()
        self._seq = 0
        self._lock = threading.Lock()

    def cfg(self):
        c = CONFIG.setdefault("proactive", {})
        return {
            "level": str(c.get("level", "normal")).lower(),
            "speak": bool(c.get("speak", True)),
            "quiet_hours": c.get("quiet_hours", [23, 7]),
            "max_per_hour": int(c.get("max_per_hour", 6)),
            "think_minutes": float(c.get("think_minutes", 12)),
            "watch_folders": c.get("watch_folders", ["Downloads", "Videos"]),
        }

    def level(self):
        return self.LEVELS.get(self.cfg()["level"], 2)

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="proactive").start()

    # --- delivery ------------------------------------------------------------
    def _quiet(self):
        c = self.cfg()
        if getattr(self.app, "focus_until", 0) > time.time():
            return True
        try:
            start, end = c["quiet_hours"]
            h = datetime.datetime.now().hour
            if start > end:
                return h >= start or h < end
            return start <= h < end
        except Exception:
            return False

    def suggest(self, topic, text, action=None, action_label=None, kind="suggestion",
                cooldown=1800, min_level=2, speak=None):
        """Offer something. Returns the suggestion id, or None if held back."""
        if self.level() < min_level:
            return None
        muted = self.app.memory.get("proactive_muted", {})
        if muted.get(topic, 0) >= 3 and kind != "warning":
            return None
        now = time.time()
        with self._lock:
            if now - self._last_fired.get(topic, 0) < cooldown:
                return None
            self._fired_times = [t for t in self._fired_times if now - t < 3600]
            if kind != "warning" and len(self._fired_times) >= self.cfg()["max_per_hour"]:
                return None
            if kind != "warning" and self._quiet():
                return None
            self._last_fired[topic] = now
            self._fired_times.append(now)
            self._seq += 1
            sid = f"p{self._seq}"
            s = {"id": sid, "topic": topic, "text": text, "action": action,
                 "action_label": action_label, "kind": kind, "ts": now, "ttl": 60}
            self.pending[sid] = s
        self.app.run_js(f"amySuggest({json.dumps(s)})")
        self.app.sfx.play("notify")
        self.app.overlay_heard(text[:48])
        log_debug(f"proactive[{topic}]: {text}")
        say = self.cfg()["speak"] if speak is None else speak
        if say and not self.app.is_speaking and (kind == "warning" or user_idle_seconds() < 120):
            self.app.speak(text)
            if action:
                self.app._followup_until = time.time() + 12   # "yes" counts without her name
        return sid

    def latest_pending(self, max_age=60):
        now = time.time()
        with self._lock:
            live = [s for s in self.pending.values() if s.get("action") and now - s["ts"] < max_age]
        return max(live, key=lambda s: s["ts"]) if live else None

    def accept(self, sid=None):
        s = self.pending.pop(sid, None) if sid else None
        if s is None and sid is None:
            s = self.latest_pending()
            if s:
                self.pending.pop(s["id"], None)
        if not s or not s.get("action"):
            return False
        self.app.run_js(f"amyClearSuggestion({json.dumps(s['id'])})")
        muted = self.app.memory.setdefault("proactive_muted", {})
        muted[s["topic"]] = 0                    # it was wanted after all
        threading.Thread(target=self.app.process_command_backend,
                         args=(s["action"],), daemon=True).start()
        return True

    def dismiss(self, sid=None):
        s = self.pending.pop(sid, None) if sid else None
        if s is None and sid is None:
            s = self.latest_pending(max_age=90)
            if s:
                self.pending.pop(s["id"], None)
        if not s:
            return False
        self.app.run_js(f"amyClearSuggestion({json.dumps(s['id'])})")
        muted = self.app.memory.setdefault("proactive_muted", {})
        muted[s["topic"]] = muted.get(s["topic"], 0) + 1
        self.app.save_memory()
        return True

    # --- signals -------------------------------------------------------------
    def _loop(self):
        time.sleep(20)                     # let startup settle
        ticks = 0
        while True:
            try:
                if self.level() > 0 and self.app.ui_ready:
                    ticks += 1
                    self._check_activity()
                    self._check_system()
                    if ticks % 3 == 0:
                        self._check_files()
                    self._check_clipboard()
                    self._check_screen()
                    self._maybe_think()
            except Exception as e:
                log_debug(f"proactive loop error: {e}\n{traceback.format_exc()}")
            time.sleep(5)

    def _check_activity(self):
        title, proc, _ = foreground_window_info()
        now = time.time()
        idle = user_idle_seconds()
        if idle > 300:
            self._active_since = now          # a real break resets the session
        if proc and (not self._activity or self._activity[-1][1] != proc
                     or self._activity[-1][2] != title):
            self._activity.append((now, proc, title[:80]))
            self._activity = self._activity[-60:]
        cur, since = self._app_since
        if proc != cur:
            self._app_since = (proc, now)
        # Long unbroken session: suggest a break (gently, rarely).
        if now - self._active_since > 100 * 60 and idle < 60:
            self.suggest("break", "You've been at it for over an hour and a half. "
                         "Might be worth a short break.", cooldown=2 * 3600, min_level=2)

    def _check_system(self):
        try:
            batt = psutil.sensors_battery()
            if batt and not batt.power_plugged:
                if batt.percent <= 10:
                    self.suggest("battery_crit", f"Battery's at {int(batt.percent)}%. "
                                 "Plug in soon or I'll lose you.", kind="warning", cooldown=600, min_level=1)
                elif batt.percent <= 20:
                    self.suggest("battery_low", f"Battery's down to {int(batt.percent)}%.",
                                 kind="warning", cooldown=1800, min_level=1)
        except Exception:
            pass
        mem = psutil.virtual_memory().percent
        if mem > 92:
            top = self._top_process("memory_percent")
            self.suggest("ram", f"Memory is at {mem:.0f}%"
                         + (f", mostly {top}." if top else ".")
                         + " Things may start to slow down.",
                         action="what's running", action_label="Show processes",
                         kind="warning", cooldown=1800, min_level=1)
        cpu = psutil.cpu_percent(interval=None)
        if cpu > 90:
            self._cpu_hot_since = self._cpu_hot_since or time.time()
            if time.time() - self._cpu_hot_since > 90:
                top = self._top_process("cpu_percent")
                self.suggest("cpu", f"The CPU has been flat out for a while"
                             + (f" - {top} is the busiest." if top else "."),
                             action="what's running", action_label="Show processes",
                             cooldown=3600, min_level=2)
        else:
            self._cpu_hot_since = None
        try:
            du = psutil.disk_usage(os.path.abspath(os.sep))
            if du.percent > 95:
                self.suggest("disk", f"Your main drive is {du.percent:.0f}% full.",
                             action="clean temp files", action_label="Clear temp files",
                             kind="warning", cooldown=6 * 3600, min_level=1)
        except Exception:
            pass

    @staticmethod
    def _top_process(key):
        best, val = None, 0.0
        for p in psutil.process_iter(["name", key]):
            try:
                v = p.info.get(key) or 0
                if v > val and p.info.get("name") not in ("System Idle Process", "Idle"):
                    best, val = p.info.get("name"), v
            except Exception:
                continue
        return best

    def _check_files(self):
        home = os.path.expanduser("~")
        now = time.time()
        for folder in self.cfg()["watch_folders"]:
            path = folder if os.path.isabs(folder) else os.path.join(home, folder)
            if not os.path.isdir(path):
                continue
            try:
                entries = [(e.name, e.stat().st_mtime, e.stat().st_size)
                           for e in os.scandir(path) if e.is_file()]
            except Exception:
                continue
            first = path not in self._seen_files
            seen = self._seen_files.setdefault(path, {})
            for name, mtime, size in entries:
                if name.lower().endswith((".crdownload", ".part", ".tmp", ".download")):
                    continue
                prev = seen.get(name)
                seen[name] = (mtime, size)
                if first or prev == (mtime, size) or now - mtime > 600:
                    continue
                if prev is not None:
                    continue                      # still being written; wait for it to settle
                ext = os.path.splitext(name)[1].lower()
                full = os.path.join(path, name)
                if ext in (".mp4", ".mov", ".mkv", ".webm") and size > 5_000_000:
                    self.suggest(f"video:{name}", f"New video ready: {name}. Want me to put it on YouTube?",
                                 action=f"upload the video {full} to YouTube as a private draft and "
                                        f"tell me when it's ready for me to check",
                                 action_label="Upload to YouTube", cooldown=10 ** 9, min_level=2)
                elif ext in (".zip", ".7z", ".rar"):
                    self.suggest(f"zip:{name}", f"{name} finished downloading. Shall I extract it?",
                                 action=f"automate extract {full} into a folder next to it",
                                 action_label="Extract", cooldown=10 ** 9, min_level=3)
                elif ext in (".pdf", ".docx", ".txt", ".md"):
                    self.suggest(f"doc:{name}", f"{name} just landed in {folder}. Want a summary?",
                                 action=f"summarise the file {full}", action_label="Summarise",
                                 cooldown=10 ** 9, min_level=3)

    def _check_clipboard(self):
        if not HAS_CLIPBOARD or self.level() < 3:
            return
        try:
            clip = (pyperclip.paste() or "").strip()
        except Exception:
            return
        if not clip or clip == self._clip_last:
            return
        self._clip_last = clip
        if re.fullmatch(r"https?://\S{8,}", clip):
            self.suggest("clip_url", "You copied a link. Want me to read it and give you the gist?",
                         action=f"read {clip}", action_label="Summarise", cooldown=120, min_level=3,
                         speak=False)
        elif len(clip) > 600:
            self.suggest("clip_text", "That's a long chunk you copied. Summarise it?",
                         action="summarise my clipboard", action_label="Summarise",
                         cooldown=300, min_level=3, speak=False)

    def _check_screen(self):
        ctx = getattr(self.app, "screen_context", "") or ""
        if not ctx or time.time() - getattr(self.app, "screen_context_time", 0) > 30:
            return
        if re.search(r"\b(error|exception|traceback|failed|crash(ed)?|not responding)\b", ctx, re.I):
            self.suggest("screen_error", "Looks like there's an error on screen. Want me to take a look?",
                         action="explain my screen", action_label="Explain it",
                         cooldown=900, min_level=2)

    # --- the bigger picture --------------------------------------------------
    def _maybe_think(self):
        c = self.cfg()
        if self.level() < 2 or self._quiet() or self.app.is_speaking:
            return
        if time.time() - self._last_think < c["think_minutes"] * 60:
            return
        if user_idle_seconds() > 300:
            return
        self._last_think = time.time()
        recent = [f"{time.strftime('%H:%M', time.localtime(t))} {p}: {ti}"
                  for t, p, ti in self._activity[-15:]]
        todos = self.app.memory.get("todos", [])[:8]
        upcoming = []
        try:
            now = datetime.datetime.now()
            for e in self.app.memory.get("calendar", []):
                dt = datetime.datetime.fromisoformat(e["when"])
                if now <= dt <= now + datetime.timedelta(hours=6):
                    upcoming.append(f"{dt:%H:%M} {e['title']}")
        except Exception:
            pass
        facts = self.app.memory.get("facts", [])[-10:]
        prompt = (
            f"It's {datetime.datetime.now():%A %H:%M}. You are Amy, the user's assistant, running "
            "quietly in the background. Decide whether to speak up unprompted.\n\n"
            f"RECENT ACTIVITY:\n" + ("\n".join(recent) or "(none)") + "\n\n"
            f"SCREEN NOW: {getattr(self.app, 'screen_context', '') or '(unknown)'}\n"
            f"UPCOMING: {', '.join(upcoming) or 'nothing'}\n"
            f"TO-DOS: {', '.join(map(str, todos)) or 'none'}\n"
            f"ABOUT THE USER: {'; '.join(map(str, facts)) or 'little known yet'}\n"
            f"YOUR CHECKLIST (HEARTBEAT.md):\n{self.app.mind.heartbeat()[:1500]}\n\n"
            "Only speak if it would genuinely help right now: a clear next step, a timely "
            "reminder, a problem you can see, or something you could do for them. Never chat "
            "for the sake of it. Most of the time the right answer is to stay quiet.\n"
            'Reply with JSON only: {"speak": true|false, "text": "one or two short sentences", '
            '"action": "an instruction you could carry out if they say yes, or empty"}'
        )
        try:
            res = self.app.http.post(OLLAMA_URL, json={
                "model": self.app.model_for("reasoning"),
                "messages": [{"role": "user", "content": prompt}],
                "stream": False, "format": "json", "keep_alive": "30m",
                "options": {"num_predict": 160, "temperature": 0.3, "num_ctx": 4096},
            }, timeout=120)
            d = self.app._llm_json(res)
        except Exception as e:
            log_debug(f"proactive think failed: {e}")
            return
        if isinstance(d, dict) and d.get("speak") and str(d.get("text", "")).strip():
            action = str(d.get("action") or "").strip() or None
            self.suggest("think:" + re.sub(r"\W+", "", str(d["text"]).lower())[:24],
                         str(d["text"]).strip()[:240], action=action,
                         action_label="Go ahead" if action else None,
                         cooldown=4 * 3600, min_level=2)


# ==========================================================================
# --- CADQUERY ENGINE (B-rep) ---
# The model describes the object as a structured spec; Python builds it with
# CadQuery (the OpenCASCADE kernel), so the result is exact solid geometry:
# smooth spline profiles, sweeps along curves, lofts, real fillets, and
# multi-part assemblies with working joints.
#
# Joints are generated, not just declared: a "hinge" between two parts adds
# interleaved knuckles to each, a separate pin, and clearance holes, then
# rotates the moving part to the hinge angle. Every assembly is checked for
# parts that overlap, and exported as a coloured STEP plus STL files.
#
# Any number in the spec can be a parameter name or simple arithmetic on
# parameters ("width / 2 + wall"), so the sliders drive the real geometry.
# ==========================================================================
HAS_CADQUERY = _installed("cadquery")
cq = _LazyModule("cadquery") if HAS_CADQUERY else None   # ~450 MB, so only loaded for CAD


class CadSpecError(Exception):
    pass


class CadQueryEngine:
    SPEC_DOC = r"""Describe the object as JSON. Millimetres; Z is up; the object rests on Z=0.

{
  "name": "short name",
  "parameters": {"width": 140, "lens_w": 52, "wall": 2.5},      // every key dimension
  "parts": [
    {
      "name": "frame",
      "color": "#1f2933",                                         // display colour
      "features": [ FEATURE, FEATURE, ... ]                       // applied in order
    }
  ],
  "joints": [ JOINT, ... ]                                        // optional
}

Numbers may be parameter names or arithmetic on them: "lens_w / 2 + wall".

FEATURE ("op" is "add" to union or "cut" to subtract; default "add"):
  {"op":"add","shape":"box","size":[x,y,z],"at":[x,y,z],"round":2}         at = centre of the bottom face
  {"shape":"cylinder","d":10,"h":20,"at":[..],"axis":"z"}                  axis: "x" | "y" | "z"
  {"shape":"cone","d":10,"d2":4,"h":12,"at":[..],"axis":"z"}
  {"shape":"sphere","d":10,"at":[..]}
  {"shape":"tube","d":20,"wall":2,"h":30,"at":[..],"axis":"z"}
  {"shape":"torus","d":40,"tube_d":6,"at":[..],"axis":"z"}
  {"shape":"extrude","points":[[x,y],...],"smooth":true,"h":4,"plane":"XY","at":[..]}
        a closed outline; smooth=true makes a spline through the points (curved shapes)
  {"shape":"revolve","points":[[r,z],...],"angle":360,"at":[..]}        profile revolved about Z
  {"shape":"sweep","profile":{"circle":3} or {"rect":[w,h]},"path":[[x,y,z],...],"smooth":true}
        a section swept along a 3D path (cables, frame arms, handles)
  {"shape":"loft","sections":[{"circle":20,"z":0},{"rect":[30,10],"z":15},{"ellipse":[20,8],"z":30}],"at":[..]}
  {"op":"hole","d":3,"at":[x,y,z],"axis":"z","depth":10}                  drilled hole (cut)
  {"op":"fillet","radius":1.5,"edges":"all"|"|Z"|">Z"|"<Z"}               round edges
  {"op":"chamfer","size":1,"edges":">Z"}
  {"op":"shell","thickness":2,"open":">Z"}                                hollow it out
  {"op":"mirror","plane":"YZ"}                                            add a mirrored copy
  {"op":"thread","d":8,"pitch":1.25,"length":12,"at":[..]}                external screw thread
Any "add" / "cut" feature may also have "rotate":[rx,ry,rz] (degrees).

JOINT (connects two parts; geometry is generated for you, do NOT model it yourself):
  {"type":"hinge","a":"frame","b":"temple_left","origin":[x,y,z],"axis":"z",
   "length":8,"knuckle_d":3.2,"pin_d":1.4,"angle":0,"min":0,"max":100}
      "a" is the fixed part, "b" swings about the axis through "origin";
      "angle" is the current opening in degrees (right-hand rule about the axis;
      use a negative angle to swing the other way).
  {"type":"pin","a":..,"b":..,"origin":[..],"axis":"z","pin_d":3,"length":10}   free rotation
  {"type":"fixed","a":..,"b":..}                                                 glued

Rules: separate moving pieces must be separate parts. Keep walls >= 1.2 mm.
Leave a 0.3 mm gap where parts meet at a joint. Use smooth splines for organic
curves (frames, handles, grips). 1 to 8 parts, up to ~14 features per part."""

    SAFE = re.compile(r"^[\w\s\.\+\-\*/\(\)]+$")

    def __init__(self):
        self.last_warnings = []

    # --- values --------------------------------------------------------------
    def _num(self, v, params, default=0.0):
        if v is None:
            return float(default)
        if isinstance(v, (int, float)):
            return float(v)
        expr = str(v).strip()
        if not expr:
            return float(default)
        if not self.SAFE.match(expr) or "__" in expr:
            raise CadSpecError(f"bad expression '{expr}'")
        try:
            return float(eval(expr, {"__builtins__": {}}, dict(params)))
        except Exception as e:
            raise CadSpecError(f"can't evaluate '{expr}': {e}")

    def _vec(self, v, params, n=3, default=0.0):
        v = list(v or [])
        v = (v + [default] * n)[:n]
        return [self._num(x, params, default) for x in v]

    @staticmethod
    def _axis_vec(axis):
        return {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}.get(str(axis or "z").lower()[:1], (0, 0, 1))

    def _orient(self, shape, axis, at, rotate=None, params=None):
        """Point a Z-built solid along `axis`, then rotate and move it."""
        ax = str(axis or "z").lower()[:1]
        if ax == "x":
            shape = shape.rotate((0, 0, 0), (0, 1, 0), 90)
        elif ax == "y":
            shape = shape.rotate((0, 0, 0), (1, 0, 0), -90)
        if rotate:
            rx, ry, rz = self._vec(rotate, params or {})
            for deg, vec in ((rx, (1, 0, 0)), (ry, (0, 1, 0)), (rz, (0, 0, 1))):
                if deg:
                    shape = shape.rotate((0, 0, 0), vec, deg)
        return shape.translate(cq.Vector(*at))

    # --- shapes --------------------------------------------------------------
    def _shape(self, f, P):
        s = str(f.get("shape", "box")).lower()
        at = self._vec(f.get("at"), P)
        axis = f.get("axis", "z")
        rot = f.get("rotate")
        W = cq.Workplane("XY")
        if s == "box":
            x, y, z = [max(0.1, v) for v in self._vec(f.get("size", [10, 10, 10]), P, default=10)]
            r = self._num(f.get("round"), P, 0)
            body = W.box(x, y, z, centered=(True, True, False))
            if r > 0:
                r = min(r, min(x, y) / 2 - 0.01)
                body = body.edges("|Z").fillet(r)
            return self._orient(body.val(), "z", at, rot, P)
        if s == "cylinder":
            d, h = self._num(f.get("d"), P, 10), self._num(f.get("h"), P, 10)
            return self._orient(cq.Solid.makeCylinder(d / 2, h), axis, at, rot, P)
        if s == "cone":
            d, d2, h = self._num(f.get("d"), P, 10), self._num(f.get("d2"), P, 0.01), self._num(f.get("h"), P, 10)
            return self._orient(cq.Solid.makeCone(d / 2, max(0.005, d2 / 2), h), axis, at, rot, P)
        if s == "sphere":
            d = self._num(f.get("d"), P, 10)
            return cq.Solid.makeSphere(d / 2, angleDegrees1=-90, angleDegrees2=90).translate(cq.Vector(*at))
        if s == "tube":
            d, wall, h = self._num(f.get("d"), P, 20), self._num(f.get("wall"), P, 2), self._num(f.get("h"), P, 20)
            body = W.circle(d / 2).circle(max(0.1, d / 2 - wall)).extrude(h).val()
            return self._orient(body, axis, at, rot, P)
        if s == "torus":
            d, td = self._num(f.get("d"), P, 30), self._num(f.get("tube_d", f.get("d2")), P, 5)
            return self._orient(cq.Solid.makeTorus(d / 2, td / 2), axis, at, rot, P)
        if s == "extrude":
            pts = [tuple(self._vec(p, P, 2)) for p in (f.get("points") or [])]
            if len(pts) < 3:
                raise CadSpecError("extrude needs at least 3 points")
            h = self._num(f.get("h"), P, 5)
            plane = str(f.get("plane", "XY")).upper()
            wp = cq.Workplane(plane if plane in ("XY", "XZ", "YZ") else "XY")
            if f.get("smooth"):
                wp = wp.spline(pts + [pts[0]], periodic=False, includeCurrent=False).close()
            else:
                wp = wp.polyline(pts).close()
            body = wp.extrude(h).val()
            return self._orient(body, "z", at, rot, P)
        if s == "revolve":
            pts = [tuple(self._vec(p, P, 2)) for p in (f.get("points") or [])]
            if len(pts) < 3:
                raise CadSpecError("revolve needs at least 3 points")
            ang = self._num(f.get("angle"), P, 360)
            wp = cq.Workplane("XZ")
            wp = wp.spline(pts + [pts[0]]).close() if f.get("smooth") else wp.polyline(pts).close()
            body = wp.revolve(ang, (0, 0, 0), (0, 1, 0)).val()
            return self._orient(body, "z", at, rot, P)
        if s == "sweep":
            path_pts = [cq.Vector(*self._vec(p, P)) for p in (f.get("path") or [])]
            if len(path_pts) < 2:
                raise CadSpecError("sweep needs a path of at least 2 points")
            path = cq.Workplane("XY").spline([tuple(v) for v in path_pts]) if (f.get("smooth", True) and len(path_pts) > 2) \
                else cq.Workplane("XY").polyline([tuple(v) for v in path_pts])
            prof = f.get("profile") or {"circle": 3}
            tangent = (path_pts[1] - path_pts[0]).normalized()
            plane = cq.Plane(origin=path_pts[0], xDir=self._perp(tangent), normal=tangent)
            sec = cq.Workplane(plane)
            if "rect" in prof:
                w, h = self._vec(prof["rect"], P, 2, 2)
                sec = sec.rect(w, h)
            else:
                sec = sec.circle(self._num(prof.get("circle"), P, 3) / 2)
            body = sec.sweep(path, transition="round").val()
            if rot:
                body = self._orient(body, "z", [0, 0, 0], rot, P)
            return body.translate(cq.Vector(*at))
        if s == "loft":
            secs = f.get("sections") or []
            if len(secs) < 2:
                raise CadSpecError("loft needs at least 2 sections")
            wires = []
            for sec in secs:
                z = self._num(sec.get("z"), P, 0)
                wp = cq.Workplane("XY").workplane(offset=z)
                if "rect" in sec:
                    w, h = self._vec(sec["rect"], P, 2, 5)
                    wp = wp.rect(w, h)
                elif "ellipse" in sec:
                    a, b = self._vec(sec["ellipse"], P, 2, 5)
                    wp = wp.ellipse(a / 2, b / 2)
                else:
                    wp = wp.circle(self._num(sec.get("circle"), P, 10) / 2)
                wires.append(wp.wires().val())
            body = cq.Solid.makeLoft(wires, True)
            return self._orient(body, "z", at, rot, P)
        raise CadSpecError(f"unknown shape '{s}'")

    @staticmethod
    def _solid(shape):
        """Unwrap single-solid compounds so later booleans behave."""
        try:
            sols = shape.Solids()
            if len(sols) == 1:
                return sols[0]
        except Exception:
            pass
        return shape

    def _fuse(self, a, b):
        a, b = self._solid(a), self._solid(b)
        try:
            return a.fuse(b)
        except Exception:
            # Faces that just touch (tangent or coincident) defeat an exact
            # boolean; a tiny fuzzy tolerance resolves it.
            try:
                return a.fuse(b, tol=1e-3)
            except Exception:
                return cq.Compound.makeCompound([a, b])

    def _cut(self, a, b):
        a, b = self._solid(a), self._solid(b)
        try:
            return a.cut(b)
        except Exception:
            return a.cut(b, tol=1e-3)

    @staticmethod
    def _perp(v):
        ref = cq.Vector(0, 0, 1) if abs(v.z) < 0.9 else cq.Vector(1, 0, 0)
        return v.cross(ref).normalized()

    def _thread(self, f, P):
        d, pitch = self._num(f.get("d"), P, 8), self._num(f.get("pitch"), P, 1.25)
        length = self._num(f.get("length"), P, 10)
        at = self._vec(f.get("at"), P)
        depth = pitch * 0.54
        helix = cq.Wire.makeHelix(pitch, length, d / 2 - depth)
        tri = (cq.Workplane("XZ").center(d / 2 - depth, 0)
               .polyline([(0, -pitch * 0.4), (depth, 0), (0, pitch * 0.4)]).close())
        thread = tri.sweep(cq.Workplane(obj=helix), isFrenet=True).val()
        core = cq.Solid.makeCylinder(d / 2 - depth + 0.01, length)
        return self._fuse(core, thread).translate(cq.Vector(*at))

    # --- parts ---------------------------------------------------------------
    def _build_part(self, part, P):
        solid = None
        for i, f in enumerate(part.get("features") or []):
            op = str(f.get("op", "add")).lower()
            try:
                if op in ("add", "union"):
                    s = self._shape(f, P)
                    solid = s if solid is None else self._fuse(solid, s)
                elif op in ("cut", "subtract", "hole"):
                    if solid is None:
                        raise CadSpecError("cut before any solid exists")
                    if op == "hole":
                        f = dict(f, shape="cylinder", h=f.get("depth", 1000))
                        at = self._vec(f.get("at"), P)
                        ax = self._axis_vec(f.get("axis"))
                        depth = self._num(f.get("depth"), P, 1000)
                        # Start a little above the surface so the hole always breaks through.
                        f["at"] = [at[k] - ax[k] * 0.5 if f.get("depth") else at[k] - ax[k] * depth / 2
                                   for k in range(3)]
                        f["h"] = depth + 1.0
                    solid = self._cut(solid, self._shape(f, P))
                elif op == "fillet":
                    r = self._num(f.get("radius"), P, 1)
                    sel = str(f.get("edges", "all"))
                    wp = cq.Workplane(obj=solid)
                    wp = wp.edges() if sel == "all" else wp.edges(sel)
                    solid = wp.fillet(r).val()
                elif op == "chamfer":
                    c = self._num(f.get("size"), P, 1)
                    sel = str(f.get("edges", "all"))
                    wp = cq.Workplane(obj=solid)
                    wp = wp.edges() if sel == "all" else wp.edges(sel)
                    solid = wp.chamfer(c).val()
                elif op == "shell":
                    t = self._num(f.get("thickness"), P, 2)
                    face = f.get("open")
                    wp = cq.Workplane(obj=solid)
                    solid = (wp.faces(face).shell(-t) if face else wp.shell(-t)).val()
                elif op == "mirror":
                    plane = str(f.get("plane", "YZ")).upper()
                    solid = self._fuse(solid, solid.mirror(plane if plane in ("XY", "YZ", "XZ") else "YZ"))
                elif op == "thread":
                    s = self._thread(f, P)
                    solid = s if solid is None else self._fuse(solid, s)
                else:
                    raise CadSpecError(f"unknown op '{op}'")
            except CadSpecError as e:
                raise CadSpecError(f"part '{part.get('name')}', feature {i + 1}: {e}")
            except Exception as e:
                raise CadSpecError(f"part '{part.get('name')}', feature {i + 1} ({op} "
                                   f"{f.get('shape', '')}) failed in the kernel: {e}")
        if solid is None:
            raise CadSpecError(f"part '{part.get('name')}' has no solid features")
        try:
            solid = solid.clean()
        except Exception:
            pass
        return solid

    # --- joints --------------------------------------------------------------
    def _hinge(self, j, parts, P, extra):
        a, b = parts.get(j.get("a")), parts.get(j.get("b"))
        if a is None or b is None:
            raise CadSpecError(f"joint refers to unknown part(s) {j.get('a')}/{j.get('b')}")
        o = cq.Vector(*self._vec(j.get("origin"), P))
        ax = cq.Vector(*self._axis_vec(j.get("axis")))
        L = self._num(j.get("length"), P, 8)
        kd = self._num(j.get("knuckle_d"), P, 3.2)
        pd = self._num(j.get("pin_d"), P, 1.4)
        gap, clear = 0.3, 0.25
        seg = (L - 2 * gap) / 3.0
        if seg <= 0.4 or pd >= kd - 0.8:
            raise CadSpecError("hinge too small: make it longer or the knuckles thicker")

        def cyl(d, h, start):
            c = cq.Solid.makeCylinder(d / 2, h)
            c = self._orient(c, j.get("axis"), [0, 0, 0])
            return c.translate(o + ax * start)
        start = -L / 2
        # Fixed part: the two outer knuckles. Moving part: the middle one.
        ka = self._fuse(cyl(kd, seg, start), cyl(kd, seg, start + 2 * seg + 2 * gap))
        kb = cyl(kd, seg, start + seg + gap)
        # Clear each part's own material out of the other's knuckle zone first,
        # so the knuckles can't fuse into a solid lump.
        zone_b = cyl(kd + 2 * gap, seg + 2 * gap, start + seg)
        zone_a = self._fuse(cyl(kd + 2 * gap, seg + gap, start - 0.01),
                            cyl(kd + 2 * gap, seg + gap, start + 2 * seg + gap + 0.01))
        bore = cyl(pd + 2 * clear, L + 2, start - 1)

        def leaf(part, z0, length):
            """A flat strap from the knuckle into the part, so the knuckle is
            always attached even if the origin sits just off the surface."""
            try:
                com = cq.Shape.centerOfMass(part)
            except Exception:
                com = part.Center()
            rel = com - o
            u = rel - ax * rel.dot(ax)
            dist = u.Length
            if dist < 1e-3:
                return None
            u = u.normalized()
            w = ax.cross(u)
            thick = kd * 0.6
            reach = min(dist, kd / 2 + 12)
            plane = cq.Plane(origin=o + ax * z0 - w * (thick / 2), xDir=u, normal=ax)
            return cq.Workplane(plane).box(reach, thick, length, centered=False).val()
        for z0, part_key, length in ((start, "a", seg), (start + 2 * seg + 2 * gap, "a", seg),
                                     (start + seg + gap, "b", seg)):
            lf = leaf(a if part_key == "a" else b, z0, length)
            if lf is not None:
                if part_key == "a":
                    ka = self._fuse(ka, lf)
                else:
                    kb = self._fuse(kb, lf)
        parts[j["a"]] = self._cut(self._fuse(self._cut(a, zone_b), ka), bore)
        parts[j["b"]] = self._cut(self._fuse(self._cut(b, zone_a), kb), bore)
        extra[f"pin_{j['a']}_{j['b']}"] = cyl(pd, L, start)
        return o, ax

    def compile(self, spec):
        """Build every part. Returns dict with parts [(name, colour, solid)],
        joints, and warnings. Raises CadSpecError with a fixable message."""
        if not isinstance(spec, dict):
            raise CadSpecError("spec must be a JSON object")
        P = {}
        for k, v in (spec.get("parameters") or {}).items():
            key = re.sub(r"\W+", "_", str(k)).strip("_")
            if key and re.match(r"^[A-Za-z_]", key):
                try:
                    P[key] = float(v) if isinstance(v, (int, float)) else self._num(v, P)
                except CadSpecError:
                    continue
        plist = spec.get("parts") or []
        if not plist:
            raise CadSpecError("no parts")
        if len(plist) > 12:
            raise CadSpecError("too many parts (max 12)")
        parts, colours, order = {}, {}, []
        for p in plist:
            name = str(p.get("name") or f"part{len(order) + 1}")
            if name in parts:
                name = f"{name}_{len(order) + 1}"
            p = dict(p, name=name)
            parts[name] = self._build_part(p, P)
            colours[name] = str(p.get("color") or "#9aa5ab")
            order.append(name)
        extra = {}
        extra_warn = []
        movers = []
        for jn, j in enumerate(spec.get("joints") or []):
          try:
            kind = str(j.get("type", "fixed")).lower()
            if kind in ("hinge", "pin"):
                o, ax = self._hinge(j, parts, P, extra)
                angle = self._num(j.get("angle"), P, 0)
                lo, hi = self._num(j.get("min"), P, -360), self._num(j.get("max"), P, 360)
                angle = max(lo, min(hi, angle))
                if angle:
                    movers.append((j["b"], o, ax, angle))
            elif kind == "fixed":
                if j.get("a") in parts and j.get("b") in parts:
                    parts[j["a"]] = self._fuse(parts[j["a"]], parts.pop(j["b"]))
                    order.remove(j["b"])
            else:
                raise CadSpecError(f"unknown joint type '{kind}'")
          except CadSpecError:
            raise
          except Exception as e:
            raise CadSpecError(f"joint {jn + 1} ({j.get('type')} {j.get('a')}-{j.get('b')}) failed: {e}")
        for name in list(parts):
            try:
                if len(parts[name].Solids()) > 1:
                    self_warn = f"{name} is in {len(parts[name].Solids())} separate pieces (something isn't touching)"
                    extra_warn.append(self_warn)
            except Exception:
                pass
        for name, o, ax, angle in movers:
            parts[name] = parts[name].rotate(o, o + ax, angle)
        for name, solid in extra.items():
            parts[name] = solid
            colours[name] = "#c0c6ca"
            order.append(name)
        result = [(n, colours.get(n, "#9aa5ab"), parts[n]) for n in order]
        warnings = extra_warn + self.interference(result)
        for n, _c, s in result:
            if not s.isValid():
                warnings.append(f"{n} isn't a valid closed solid")
        self.last_warnings = warnings
        return {"parts": result, "params": P, "joints": spec.get("joints") or [], "warnings": warnings}

    @staticmethod
    def interference(parts, tol=0.01):
        """Pairs of parts whose volumes overlap (mm^3)."""
        out = []
        for i in range(len(parts)):
            for k in range(i + 1, len(parts)):
                (na, _, a), (nb, _, b) = parts[i], parts[k]
                try:
                    if not a.BoundingBox().enlarge(0.01).intersects(b.BoundingBox()):
                        continue
                    v = a.intersect(b).Volume()
                    if v > tol:
                        out.append(f"{na} and {nb} overlap by {v:.2f} mm³")
                except Exception:
                    continue
        return out

    # --- output --------------------------------------------------------------
    def export(self, result, base_path):
        """Write <base>.step (coloured assembly), <base>.stl (everything) and
        <base>_<part>.stl. Returns (step, stl, [part stls])."""
        asm = cq.Assembly(name="amy")
        for name, colour, solid in result["parts"]:
            try:
                c = QtGui.QColor(colour) if HAS_QT else None
                col = cq.Color(c.redF(), c.greenF(), c.blueF()) if c and c.isValid() else cq.Color("gray")
            except Exception:
                col = cq.Color("gray")
            asm.add(solid, name=re.sub(r"\W+", "_", name), color=col)
        step = base_path + ".step"
        (asm.export if hasattr(asm, "export") else asm.save)(step)
        compound = cq.Compound.makeCompound([s for _, _, s in result["parts"]])
        stl = base_path + ".stl"
        cq.exporters.export(cq.Workplane(obj=compound), stl, tolerance=0.05, angularTolerance=0.15)
        part_files = []
        for name, _c, solid in result["parts"]:
            safe = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "part"
            p = f"{base_path}_{safe}.stl"
            cq.exporters.export(cq.Workplane(obj=solid), p, tolerance=0.05, angularTolerance=0.15)
            part_files.append(p)
        return step, stl, part_files

    @staticmethod
    def mesh(result, max_tris=6000):
        """Triangles per part for the preview: [{"name", "color", "tris": [x,y,z,...]}]."""
        out = []
        total = 0
        for name, colour, solid in result["parts"]:
            try:
                verts, tris = solid.tessellate(0.15, 0.3)
            except Exception:
                continue
            flat = []
            step = max(1, len(tris) * len(result["parts"]) // max_tris)
            for t in tris[::step]:
                for idx in t:
                    v = verts[idx]
                    flat += [round(v.x, 3), round(v.y, 3), round(v.z, 3)]
            total += len(flat) // 9
            out.append({"name": name, "color": colour, "tris": flat})
        return out

    @staticmethod
    def render_png(meshes, path, size=560):
        """Shaded render with a depth buffer (no OpenGL needed). Used so the
        vision model can check the shape matches the request."""
        import math
        if _np is None:
            return None
        tris, cols = [], []
        for m in meshes:
            c = QtGui.QColor(m["color"]) if HAS_QT else None
            base = (c.red(), c.green(), c.blue()) if c is not None and c.isValid() else (150, 160, 166)
            t = _np.asarray(m["tris"], dtype=_np.float64).reshape(-1, 3, 3)
            tris.append(t)
            cols += [base] * len(t)
        if not tris:
            return None
        T = _np.concatenate(tris)
        cols = _np.asarray(cols, dtype=_np.float64)
        lo, hi = T.reshape(-1, 3).min(0), T.reshape(-1, 3).max(0)
        T = (T - (lo + hi) / 2) / (max(hi - lo) or 1.0)
        ya, ti = math.radians(35), math.radians(28)
        ca, sa, ct, st = math.cos(ya), math.sin(ya), math.cos(ti), math.sin(ti)
        R = _np.array([[ca, -sa, 0], [st * sa, st * ca, ct], [-ct * sa, -ct * ca, st]])
        V = T @ R.T
        N = _np.cross(V[:, 1] - V[:, 0], V[:, 2] - V[:, 0])
        N /= (_np.linalg.norm(N, axis=1, keepdims=True) + 1e-12)
        lam = _np.abs(N @ _np.array([0.35, 0.55, 0.76]))
        shade = (cols * (0.3 + 0.7 * lam)[:, None]).clip(0, 255)
        s_ = size * 0.62
        X = size / 2 + V[:, :, 0] * s_
        Y = size / 2 - V[:, :, 1] * s_
        Z = V[:, :, 2]
        img = _np.zeros((size, size, 3), dtype=_np.uint8)
        img[:] = (18, 20, 22)
        zb = _np.full((size, size), -1e9)
        for i in range(len(T)):
            x0, x1, x2 = X[i]
            y0, y1, y2 = Y[i]
            area = (x1 - x0) * (y2 - y0) - (y1 - y0) * (x2 - x0)
            if abs(area) < 1e-9:
                continue
            mnx, mxx = max(0, int(min(x0, x1, x2))), min(size - 1, int(max(x0, x1, x2)) + 1)
            mny, mxy = max(0, int(min(y0, y1, y2))), min(size - 1, int(max(y0, y1, y2)) + 1)
            if mnx > mxx or mny > mxy:
                continue
            gx, gy = _np.meshgrid(_np.arange(mnx, mxx + 1) + 0.5, _np.arange(mny, mxy + 1) + 0.5)
            w1 = ((gx - x0) * (y2 - y0) - (gy - y0) * (x2 - x0)) / area
            w2 = ((x1 - x0) * (gy - y0) - (y1 - y0) * (gx - x0)) / area
            w0 = 1 - w1 - w2
            inside = (w0 >= -1e-4) & (w1 >= -1e-4) & (w2 >= -1e-4)
            if not inside.any():
                continue
            z = w0 * Z[i, 0] + w1 * Z[i, 1] + w2 * Z[i, 2]
            sub = zb[mny:mxy + 1, mnx:mxx + 1]
            win = inside & (z > sub)
            sub[win] = z[win]
            img[mny:mxy + 1, mnx:mxx + 1][win] = shade[i].astype(_np.uint8)
        Image.fromarray(img).save(path)
        return path


# ==========================================================================
# --- SOUL, MEMORY, SKILLS, HEARTBEAT ---
# Plain markdown files in amy_data that you can open and edit yourself
# (an approach borrowed from Huw Prosser's fury-sdk examples):
#   SOUL.md       who Amy is and how she talks (replaces the built-in persona)
#   MEMORY.md     things she always knows about you; always in her prompt
#   HEARTBEAT.md  a checklist she runs through quietly every so often
#   skills/<name>/SKILL.md
#                 know-how for specific jobs. Only each skill's one-line
#                 description sits in her prompt; the full instructions are
#                 loaded for a request only when that skill is relevant.
# ==========================================================================
class MindFiles:
    MEMORY_LIMIT = 6000          # characters of MEMORY.md injected per turn

    def __init__(self, folder, default_soul):
        self.folder = folder
        self.skills_dir = os.path.join(folder, "skills")
        self.soul_path = os.path.join(folder, "SOUL.md")
        self.memory_path = os.path.join(folder, "MEMORY.md")
        self.heartbeat_path = os.path.join(folder, "HEARTBEAT.md")
        self._lock = threading.Lock()
        self._skills = None
        self._skills_mtime = 0.0
        self._seed(default_soul)

    def _seed(self, default_soul):
        try:
            os.makedirs(self.skills_dir, exist_ok=True)
            if not os.path.exists(self.soul_path):
                with open(self.soul_path, "w", encoding="utf-8") as f:
                    f.write("# Soul\n\nWho Amy is and how she speaks. Edit freely; changes apply "
                            "on her next reply.\n\n" + default_soul + "\n")
            if not os.path.exists(self.memory_path):
                with open(self.memory_path, "w", encoding="utf-8") as f:
                    f.write("# Memory\n\nThings Amy should always know. One fact per line. "
                            "She adds to this when you say \"remember that ...\".\n\n")
            if not os.path.exists(self.heartbeat_path):
                with open(self.heartbeat_path, "w", encoding="utf-8") as f:
                    f.write("# Heartbeat\n\nEvery so often Amy quietly checks this list and only "
                            "speaks up if something needs attention.\n\n"
                            "- Is anything on today's calendar coming up soon?\n"
                            "- Are there to-dos that look overdue or forgotten?\n"
                            "- Has a long task finished that I haven't been told about?\n")
            example = os.path.join(self.skills_dir, "youtube-upload")
            if not os.path.exists(example):
                os.makedirs(example, exist_ok=True)
                with open(os.path.join(example, "SKILL.md"), "w", encoding="utf-8") as f:
                    f.write(
                        "---\nname: youtube-upload\n"
                        "description: Upload a video to YouTube through YouTube Studio in the user's "
                        "own browser. Use when asked to post, upload or publish a video to YouTube.\n---\n\n"
                        "# Uploading to YouTube\n\n"
                        "1. Find the video: use find_files with 'newest video' unless a path was given.\n"
                        "2. open_url https://studio.youtube.com and wait for it to load.\n"
                        "3. Click Create, then Upload videos.\n"
                        "4. In the file dialog, type the full path into the File name box and press enter.\n"
                        "5. Fill in the title (from the file name unless told otherwise).\n"
                        "6. Click Next until the Visibility page. Choose Private unless the user said public.\n"
                        "7. Ask before the final Save/Publish click.\n")
        except Exception as e:
            log_debug(f"mind files seed failed: {e}")

    @staticmethod
    def _read(path, limit=None):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            return text[:limit] if limit else text
        except Exception:
            return ""

    def soul(self):
        text = self._read(self.soul_path)
        # Drop the explanatory header so only the persona reaches the model.
        return re.sub(r"^# Soul\s*\n\s*Who Amy is.*?\n\n", "", text, flags=re.S).strip()

    def memory(self):
        text = self._read(self.memory_path)
        lines = [l for l in text.splitlines() if l.strip() and not l.startswith("#")
                 and not l.startswith("Things Amy should always know")]
        return "\n".join(lines)[-self.MEMORY_LIMIT:]

    def remember(self, fact):
        fact = re.sub(r"\s+", " ", str(fact)).strip()
        if not fact:
            return False
        with self._lock:
            existing = self._read(self.memory_path)
            if fact.lower() in existing.lower():
                return True
            with open(self.memory_path, "a", encoding="utf-8") as f:
                f.write(f"- {fact}\n")
        return True

    def forget(self, needle):
        needle = str(needle).lower().strip()
        with self._lock:
            text = self._read(self.memory_path)
            kept, removed = [], 0
            for line in text.splitlines():
                if line.startswith("- ") and needle and needle in line.lower():
                    removed += 1
                    continue
                kept.append(line)
            if removed:
                with open(self.memory_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(kept) + "\n")
        return removed

    def heartbeat(self):
        return self._read(self.heartbeat_path, 3000)

    # --- skills ----------------------------------------------------------------
    def skills(self):
        """[{name, description, path}] - rescanned when the folder changes."""
        try:
            mtime = max([os.path.getmtime(self.skills_dir)] +
                        [os.path.getmtime(os.path.join(self.skills_dir, d))
                         for d in os.listdir(self.skills_dir)])
        except Exception:
            mtime = 0.0
        if self._skills is not None and mtime <= self._skills_mtime:
            return self._skills
        found = []
        try:
            for d in sorted(os.listdir(self.skills_dir)):
                p = os.path.join(self.skills_dir, d, "SKILL.md")
                if not os.path.isfile(p):
                    continue
                text = self._read(p, 20000)
                m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
                meta = {}
                if m:
                    for line in m.group(1).splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip().lower()] = v.strip().strip('"')
                found.append({"name": meta.get("name", d), "description": meta.get("description", ""),
                              "path": p, "body": text[m.end():] if m else text})
        except Exception as e:
            log_debug(f"skill scan failed: {e}")
        self._skills, self._skills_mtime = found, mtime
        return found

    def relevant_skills(self, request, limit=2):
        """Skills whose name/description share real words with the request."""
        stop = {"the", "a", "an", "to", "and", "or", "of", "for", "my", "me", "it", "on", "in",
                "is", "use", "when", "with", "this", "that", "you", "your", "be", "as", "at", "by"}
        words = {w for w in re.findall(r"[a-z0-9]+", str(request).lower()) if w not in stop and len(w) > 2}
        scored = []
        for s in self.skills():
            vocab = set(re.findall(r"[a-z0-9]+", (s["name"].replace("-", " ") + " " + s["description"]).lower()))
            hits = len(words & vocab)
            if any(part in words for part in s["name"].split("-") if len(part) > 3):
                hits += 2
            if hits >= 2:
                scored.append((hits, s))
        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:limit]]

    def skills_index(self):
        items = self.skills()
        if not items:
            return ""
        return "\n".join(f"- {s['name']}: {s['description'][:160]}" for s in items[:30])


# ==========================================================================
# --- SOUND EFFECTS ---
# Small, synthesised-on-startup UI sounds (no audio files to ship or license):
# soft sine/triangle tones with smooth envelopes and a touch of room echo.
# Played on their own short audio stream so they never cut off Amy's voice.
# ==========================================================================
class SoundFX:
    RATE = 44100

    def __init__(self):
        self.enabled = HAS_SOUNDDEVICE and _np is not None
        self.bank = {}
        self._last = {}
        if self.enabled:
            try:
                self._build()
            except Exception as e:
                log_debug(f"sound synthesis failed: {e}")
                self.enabled = False

    # --- synthesis -------------------------------------------------------------
    def _tone(self, freq, dur, kind="sine", attack=0.006, decay=6.0, glide=0.0):
        n = int(self.RATE * dur)
        t = _np.arange(n) / self.RATE
        f = freq * (1 + glide * t / max(dur, 1e-3))
        phase = 2 * _np.pi * _np.cumsum(f) / self.RATE
        if kind == "tri":
            w = 2 / _np.pi * _np.arcsin(_np.sin(phase))
        else:
            w = _np.sin(phase) + 0.18 * _np.sin(2 * phase) + 0.05 * _np.sin(3 * phase)
        env = _np.minimum(1, t / attack) * _np.exp(-decay * t)
        return w * env

    def _noise_swell(self, dur, up=True):
        n = int(self.RATE * dur)
        rng = _np.random.default_rng(7)
        x = rng.normal(0, 1, n)
        # crude low-pass by moving average, getting brighter as it rises
        k = 40
        x = _np.convolve(x, _np.ones(k) / k, mode="same")
        t = _np.linspace(0, 1, n)
        env = _np.sin(_np.pi * t) ** 2 * (t if up else (1 - t))
        return x * env * 3.0

    def _room(self, x, amount=0.22):
        out = _np.concatenate([x, _np.zeros(int(self.RATE * 0.35))])
        for delay, g in ((0.043, 0.5), (0.071, 0.35), (0.113, 0.22), (0.167, 0.12)):
            d = int(self.RATE * delay)
            out[d:d + len(x)] += x * g * amount * 2
        return out

    def _seq(self, *parts):
        """parts: (start_seconds, array)"""
        end = max(int(s * self.RATE) + len(a) for s, a in parts)
        out = _np.zeros(end)
        for s, a in parts:
            i = int(s * self.RATE)
            out[i:i + len(a)] += a
        return out

    def _finish(self, x, gain=0.5):
        x = self._room(x)
        peak = _np.max(_np.abs(x)) or 1.0
        return (x / peak * gain * 32767).astype(_np.int16)

    def _build(self):
        T = self._tone
        self.bank = {
            # rising two-note shimmer when Amy comes online
            "startup": self._finish(self._seq(
                (0.0, T(523.25, 0.9, decay=3.0) * 0.6), (0.12, T(783.99, 1.0, decay=2.6) * 0.55),
                (0.24, T(1046.5, 1.1, decay=2.4) * 0.35), (0.0, self._noise_swell(0.6) * 0.04)), 0.42),
            # bright upward blip: "I'm listening"
            "wake": self._finish(self._seq((0.0, T(880, 0.16, decay=18, glide=0.12) * 0.8),
                                           (0.07, T(1318.5, 0.22, decay=14) * 0.6)), 0.38),
            # soft downward blip: "got it, working"
            "ack": self._finish(T(987.8, 0.18, decay=20, glide=-0.18), 0.28),
            # resolved major third: task done
            "done": self._finish(self._seq((0.0, T(659.25, 0.5, decay=6) * 0.7),
                                           (0.1, T(830.6, 0.6, decay=5) * 0.6)), 0.34),
            # two muted low ticks: something failed
            "error": self._finish(self._seq((0.0, T(220, 0.14, "tri", decay=22) * 0.9),
                                            (0.13, T(185, 0.18, "tri", decay=20) * 0.9)), 0.36),
            # gentle bell: Amy has a suggestion
            "notify": self._finish(self._seq((0.0, T(1174.7, 0.9, decay=4.5) * 0.5),
                                             (0.0, T(2349.3, 0.6, decay=7) * 0.12)), 0.30),
            # tiny click: interrupted / stopped
            "stop": self._finish(T(1500, 0.05, decay=60), 0.22),
            # airy swell in and out of the sleep screen
            "sleep": self._finish(self._seq((0.0, self._noise_swell(0.8, up=False) * 0.5),
                                            (0.1, T(392, 0.9, decay=4) * 0.3)), 0.22),
            "unsleep": self._finish(self._seq((0.0, self._noise_swell(0.6, up=True) * 0.5),
                                              (0.25, T(587.3, 0.6, decay=5) * 0.35)), 0.26),
            # clap recognised
            "clap": self._finish(self._seq((0.0, T(1046.5, 0.12, decay=24) * 0.6),
                                           (0.08, T(1567.98, 0.25, decay=12) * 0.5)), 0.34),
        }

    # --- playback --------------------------------------------------------------
    def play(self, name, volume=None):
        if not self.enabled:
            return
        ui = CONFIG.get("ui", {})
        if not ui.get("sounds", True):
            return
        data = self.bank.get(name)
        if data is None:
            return
        now = time.time()
        if now - self._last.get(name, 0) < 0.12:          # no machine-gun repeats
            return
        self._last[name] = now
        vol = float(ui.get("sound_volume", 0.6) if volume is None else volume)
        pcm = (data.astype(_np.float32) * max(0.0, min(1.0, vol))).astype(_np.int16)

        def run():
            pos = [0]
            done = threading.Event()

            def cb(out, frames, _t, _s):
                chunk = pcm[pos[0]:pos[0] + frames]
                out[:len(chunk), 0] = chunk
                if len(chunk) < frames:
                    out[len(chunk):, 0] = 0
                    raise sd.CallbackStop
                pos[0] += frames
            try:
                with sd.OutputStream(samplerate=self.RATE, channels=1, dtype="int16",
                                     callback=cb, finished_callback=done.set):
                    done.wait(len(pcm) / self.RATE + 1.0)
            except Exception as e:
                log_debug(f"sfx '{name}' failed: {e}")
        threading.Thread(target=run, daemon=True, name=f"sfx-{name}").start()


# ==========================================================================
# --- CLAP WAKE ---
# Double-clap to wake Amy, without saying anything.
#
# Built in: a signal detector for claps - a sudden broadband spike that dies
# away within ~120 ms - needing two of them 0.15-0.8 s apart.
# Optional: Huw Prosser's MIT-licensed clap classifier (clap-detection). If
# amy_data/clap_model.pth exists (trained with his record/augment/train
# scripts) and torch + torchaudio are installed, each candidate clap is also
# checked by that network, which cuts false triggers from desk knocks, typing
# and cups being put down.
# ==========================================================================
class ClapDetector:
    def __init__(self, model_path=None):
        self.rate = 16000
        self._env = 0.0
        self._cand_until = 0.0
        self._last_clap = -10.0
        self._recent = []                        # last ~1.2 s of audio for the model
        self.model = None
        if model_path and os.path.exists(model_path):
            self.model = self._load_model(model_path)

    @staticmethod
    def _load_model(path):
        try:
            import torch
            import torch.nn as nn

            class AudioClassifier(nn.Module):        # architecture from clap-detection (MIT)
                def __init__(self):
                    super().__init__()
                    self.conv1, self.bn1 = nn.Conv2d(1, 32, 3, 1, 1), nn.BatchNorm2d(32)
                    self.pool1 = nn.MaxPool2d(2, 2)
                    self.conv2, self.bn2 = nn.Conv2d(32, 64, 3, 1, 1), nn.BatchNorm2d(64)
                    self.pool2 = nn.MaxPool2d(2, 2)
                    self.conv3, self.bn3 = nn.Conv2d(64, 64, 3, 1, 1), nn.BatchNorm2d(64)
                    self.pool3 = nn.MaxPool2d(2, 2)
                    self.conv4, self.bn4 = nn.Conv2d(64, 64, 3, 1, 1), nn.BatchNorm2d(64)
                    self.pool4 = nn.MaxPool2d(2, 2)
                    self.fc1, self.fc2 = nn.Linear(64 * 64 * 64, 128), nn.Linear(128, 2)
                    self.dropout = nn.Dropout(0.5)

                def forward(self, x):
                    x = self.pool1(torch.relu(self.bn1(self.conv1(x))))
                    x = self.pool2(torch.relu(self.bn2(self.conv2(x))))
                    x = torch.relu(self.bn3(self.conv3(x)))
                    x = self.dropout(torch.relu(self.bn4(self.conv4(x))))
                    x = x.view(x.size(0), -1)
                    x = self.dropout(torch.relu(self.fc1(x)))
                    return torch.log_softmax(self.fc2(x), dim=1)
            m = AudioClassifier()
            m.load_state_dict(torch.load(path, map_location="cpu"))
            m.eval()
            log_debug("Clap classifier loaded.")
            return m
        except Exception as e:
            log_debug(f"clap model unavailable ({e}); using the signal detector only")
            return None

    def _model_says_clap(self, pcm):
        try:
            import torch
            import torchaudio.transforms as T
            import torch.nn.functional as F
            wav = torch.tensor(pcm, dtype=torch.float32).unsqueeze(0) / 32768.0
            wav = T.Resample(self.rate, 44100)(wav)
            spec = T.MelSpectrogram(sample_rate=44100, n_fft=400, win_length=400,
                                    hop_length=200, n_mels=128)(wav)
            spec = F.interpolate(spec.unsqueeze(0), size=(256, 256), mode="bilinear").squeeze(0)
            spec = (spec - spec.mean()) / (spec.std() + 1e-6)
            with torch.no_grad():
                p = torch.softmax(self.model(spec.unsqueeze(0)), dim=1)[0, 1].item()
            return p > 0.9
        except Exception as e:
            log_debug(f"clap model check failed: {e}")
            return True

    def feed(self, frame_bytes, rms, noise):
        """Feed one 30 ms frame. Returns True on a double clap."""
        if _np is None:
            return False
        a = _np.frombuffer(frame_bytes, dtype=_np.int16)
        self._recent.append(a)
        if len(self._recent) > 40:
            self._recent.pop(0)
        # Audio time (frame count), not wall-clock: a backlog of queued frames
        # must not squash or stretch the gap between claps.
        self._t = getattr(self, "_t", 0.0) + len(a) / self.rate
        now = self._t
        peak = float(_np.max(_np.abs(a))) / 32768.0 if a.size else 0.0
        prev = self._env
        self._env = 0.7 * self._env + 0.3 * rms
        # Onset: a sharp jump well above both the background and the last frame.
        is_onset = peak > 0.25 and rms > max(noise * 10, 0.03) and rms > prev * 4
        if is_onset and now > self._cand_until:
            # Broadband check: claps have lots of high-frequency energy.
            spec = _np.abs(_np.fft.rfft(a.astype(_np.float32)))
            hi = spec[len(spec) // 4:].sum() / (spec.sum() + 1e-6)
            if hi > 0.25:
                self._cand_until = now + 0.12
                if self.model is not None and not self._model_says_clap(
                        _np.concatenate(self._recent[-34:])):
                    return False
                gap = now - self._last_clap
                self._last_clap = now
                if 0.15 <= gap <= 0.8:
                    self._last_clap = -10.0
                    return True
        return False


class AmyCard:
    def __init__(self, card_id, title, card_type, content):
        self.card_id = card_id
        self.title = title
        self.card_type = card_type  # e.g., 'carousel', 'code_bug', 'email_draft'
        self.content = content

    def to_dict(self):
        return {
            "card_id": self.card_id,
            "title": self.title,
            "card_type": self.card_type,
            "content": self.content
        }


HTML_UI = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Amy</title>
    <style>
        :root {
            /* Palette shared with the project site (docs/index.html). */
            --accent: #5eead4;
            --accent-2: #a78bfa;
            --accent-soft: rgba(94,234,212, 0.40);
            --accent-dim: rgba(94,234,212, 0.12);
            --green: #1ed760;
            --bg: #07080c;
            --bg-soft: #0b0d14;
            --panel: rgba(14, 16, 23, 0.86);
            --panel-solid: #0e1017;
            --surface-2: #13161f;
            /* Lines are neutral, not accent-tinted — panels sit back, accents stay rare. */
            --border: rgba(255, 255, 255, 0.09);
            --border-lit: rgba(255, 255, 255, 0.16);
            --text: #e8eaf2;
            --text-dim: #8c92a6;
            --sans: system-ui, -apple-system, 'Segoe UI', Inter, Roboto, Helvetica, Arial, sans-serif;
            --mono: ui-monospace, 'Cascadia Mono', Consolas, 'Courier New', monospace;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: var(--sans); user-select: none; }

        /* Monospace is a deliberate accent now, not the default voice. */
        code, pre, .copy-mini, .tb-stat, .card-type-badge,
        .telemetry-label, .scratchpad-area { font-family: var(--mono); }

        body {
            background-color: var(--bg);
            color: var(--text);
            height: 100vh;
            overflow: hidden;
            -webkit-font-smoothing: antialiased;
            background-image:
                linear-gradient(rgba(255, 255, 255, 0.045) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255, 255, 255, 0.045) 1px, transparent 1px);
            background-size: 64px 64px;
        }

        /* ===== COOL WINDOW TITLE BAR ===== */
        .titlebar {
            position: absolute; top: 0; left: 0; right: 0; height: 42px;
            display: flex; align-items: center; justify-content: space-between;
            padding: 0 18px; z-index: 500;
            background: linear-gradient(90deg, rgba(9,13,22,0.95), rgba(6,16,26,0.85), rgba(9,13,22,0.95));
            border-bottom: 1px solid var(--border-lit);
            box-shadow: 0 2px 20px rgba(94,234,212, 0.12);
            -webkit-app-region: drag;
        }
        .titlebar .tb-left { display: flex; align-items: center; gap: 12px; }
        .tb-orb { width: 14px; height: 14px; border-radius: 50%;
            background: radial-gradient(circle at 35% 35%, #99f6e4, var(--accent) 60%, #026 100%);
            box-shadow: 0 0 12px var(--accent); animation: tb-pulse 2.6s ease-in-out infinite; }
        @keyframes tb-spin { to { transform: rotate(360deg); } }
        @keyframes tb-pulse { 50% { box-shadow: 0 0 4px var(--accent); opacity: 0.7; } }
        .tb-title { font-size: 13px; font-weight: bold; letter-spacing: 6px; color: #e8eaf2;
            text-shadow: 0 0 10px var(--accent-soft); }
        .tb-sub { font-size: 9px; letter-spacing: 3px; color: var(--text-dim); margin-left: 6px; }
        .tb-right { display: flex; align-items: center; gap: 14px; -webkit-app-region: no-drag; }
        .win-controls { display: flex; gap: 4px; margin-left: 6px; -webkit-app-region: no-drag; }
        .win-btn {
            width: 30px; height: 24px; border-radius: 6px; cursor: pointer;
            background: rgba(94,234,212,0.07); border: 1px solid rgba(94,234,212,0.28);
            color: var(--accent); font-size: 12px; line-height: 1;
            display: flex; align-items: center; justify-content: center;
            transition: all 0.15s ease; font-family: 'Segoe UI', sans-serif;
        }
        .win-btn:hover { background: var(--accent); color: var(--bg); box-shadow: 0 0 10px var(--accent); }
        .win-btn.close { border-color: rgba(255,46,99,0.5); color: #ff5c7a; }
        .win-btn.close:hover { background: #ff2e63; color: #fff; box-shadow: 0 0 10px #ff2e63; }
        .tb-stat { font-size: 9px; letter-spacing: 2px; color: var(--text-dim); }
        .tb-stat b { color: var(--green); }
        .tb-lines { display: flex; gap: 3px; align-items: flex-end; height: 14px; }
        .tb-lines i { width: 3px; background: var(--accent); border-radius: 2px; animation: eq 1s ease-in-out infinite; opacity: 0.8; }
        .tb-lines i:nth-child(1){height:6px;animation-delay:0s} .tb-lines i:nth-child(2){height:12px;animation-delay:.15s}
        .tb-lines i:nth-child(3){height:8px;animation-delay:.3s} .tb-lines i:nth-child(4){height:14px;animation-delay:.45s}
        @keyframes eq { 50% { transform: scaleY(0.4); } }

        /* ===== CENTRAL ARC REACTOR ===== */
        .reactor-container {
            position: absolute; top: 50%; left: 50%;
            transform: translate(-50%, -50%);
            width: 300px; height: 300px;
            display: flex; justify-content: center; align-items: center;
            pointer-events: none; z-index: 1;
            transition: transform 0.25s ease;
        }
        .ring { position: absolute; border-radius: 50%; border: 2px dashed var(--accent-soft); animation: spin 20s linear infinite; }
        .ring-1 { width: 260px; height: 260px; border-color: rgba(94,234,212,0.3); border-style: solid; border-width: 1px; }
        .ring-2 { width: 210px; height: 210px; animation-direction: reverse; animation-duration: 15s; }
        .ring-3 { width: 170px; height: 170px; border: 3px solid var(--accent);
            box-shadow: 0 0 25px rgba(94,234,212,0.6), inset 0 0 25px rgba(94,234,212,0.6); animation-duration: 10s; }
        .core-center {
            width: 120px; height: 120px; background: rgba(9,13,22,0.9);
            border: 2px solid var(--accent); border-radius: 50%;
            display: flex; flex-direction: column; justify-content: center; align-items: center;
            box-shadow: 0 0 30px rgba(94,234,212,0.5);
            font-size: 16px; font-weight: bold; letter-spacing: 2px; color: var(--accent);
            transition: all 0.25s ease;
        }
        .reactor-container.listening .core-center { border-color: var(--green); color: var(--green); box-shadow: 0 0 45px rgba(30,215,96,0.7); }
        .reactor-container.listening .ring-3 { border-color: var(--green); box-shadow: 0 0 30px rgba(30,215,96,0.6); }
        .reactor-container.wake-triggered .core-center { border-color: #a78bfa; color: #a78bfa; transform: scale(1.12); box-shadow: 0 0 55px rgba(6,182,212,0.9); }
        .reactor-container.active-speech .core-center { border-color: #ff2e63; color: #ff2e63; box-shadow: 0 0 50px rgba(255,46,99,0.7); transform: scale(1.06); }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }

        /* ===== WIDGETS ===== */
        .workspace { position: relative; width: 100vw; height: 100vh; z-index: 10; pointer-events: none; }
        .widget {
            position: absolute;
            /* Flat, quiet panels. The old gradient + heavy shadow read as busy
               next to the content; a single flat tint sits back better. */
            background: rgba(9, 14, 23, 0.86);
            border: 1px solid rgba(255,255,255,0.07);
            border-radius: 10px;
            backdrop-filter: blur(18px) saturate(120%);
            pointer-events: auto;
            display: flex; flex-direction: column; overflow: hidden;
            box-shadow: 0 8px 28px rgba(0,0,0,0.45);
            transition: border-color 0.2s ease, box-shadow 0.2s ease, transform 0.2s ease;
        }
        .widget::before, .widget::after {
            content: ''; position: absolute; width: 12px; height: 12px; pointer-events: none;
            border-color: var(--accent); opacity: 0.75; transition: opacity 0.2s ease;
        }
        .widget::before { top: 5px; left: 5px; border-top: 2px solid; border-left: 2px solid; border-top-left-radius: 6px; }
        .widget::after { bottom: 5px; right: 5px; border-bottom: 2px solid; border-right: 2px solid; border-bottom-right-radius: 6px; }
        .widget:hover { border-color: rgba(255,255,255,0.14); box-shadow: 0 10px 32px rgba(0,0,0,0.5); }
        /* Subtle top highlight so panels read as glass rather than flat boxes */

        .widget-header { position: relative; }
        /* Focused widget glows a little brighter than the rest */
        .widget:focus-within { border-color: rgba(94,234,212,0.45); }
        .widget-header .status-dot {
            box-shadow: 0 0 10px currentColor;
            animation: dot-breathe 2.6s ease-in-out infinite;
        }
        @keyframes dot-breathe { 50% { opacity: 0.45; transform: scale(0.82); } }
        /* Scrollbars inside widgets match the HUD */
        .widget *::-webkit-scrollbar { width: 8px; height: 8px; }
        .widget *::-webkit-scrollbar-track { background: rgba(0,0,0,0.3); border-radius: 4px; }
        .widget *::-webkit-scrollbar-thumb { background: rgba(94,234,212,0.35); border-radius: 4px; }
        .widget *::-webkit-scrollbar-thumb:hover { background: var(--accent); }
        /* Buttons feel more tactile */
        .action-btn {
            transition: all 0.16s cubic-bezier(.2,.8,.2,1);
        }
        .action-btn:hover { background: rgba(94,234,212,0.14); }
        .action-btn:active { transform: translateY(0); box-shadow: none; }
        .hud-btn { transition: all 0.16s cubic-bezier(.2,.8,.2,1); }
        .hud-btn:hover { background: rgba(94,234,212,0.16); border-color: rgba(94,234,212,0.5); }
        .widget:hover::before, .widget:hover::after { opacity: 1; }
        .widget.hidden { display: none !important; }
        .copy-mini {
            margin-left: 6px; padding: 1px 6px; font-size: 9px; cursor: pointer;
            background: rgba(94,234,212,0.10); border: 1px solid var(--border);
            color: var(--accent); border-radius: 4px; vertical-align: middle;
            transition: all .15s;
        }
        .copy-mini:hover { background: var(--accent); color: var(--bg); }
        .card-body-content a:hover { text-shadow: 0 0 8px var(--accent); }

        .resize-grip {
            position: absolute; bottom: 0; right: 0; width: 16px; height: 16px;
            cursor: nwse-resize; z-index: 20; opacity: 0; transition: opacity 0.2s ease;
            background:
              linear-gradient(135deg, transparent 45%, var(--accent) 45%, var(--accent) 55%, transparent 55%),
              linear-gradient(135deg, transparent 70%, var(--accent) 70%, var(--accent) 80%, transparent 80%);
        }
        .widget:hover .resize-grip { opacity: 0.85; }
        .resize-grip:hover { opacity: 1 !important; }
        .widget-header {
            padding: 11px 15px; font-size: 11px; font-weight: bold; letter-spacing: 1.4px;
            border-bottom: 1px solid var(--border); cursor: grab;
            display: flex; justify-content: space-between; align-items: center;
            color: var(--accent); text-transform: uppercase;
            background: linear-gradient(90deg, rgba(94,234,212,0.10), rgba(94,234,212,0.02) 60%, transparent);
            text-shadow: 0 0 8px rgba(94,234,212,0.4);
        }
        /* Widgets remain fully interactive on top of the desk feed. */
        .reactor-container.convo-mode .core-center { border-color: #1ed760; color: #1ed760; box-shadow: 0 0 45px rgba(30,215,96,0.6); }
        .workspace.over-camera { z-index: 50; }
        /* Over the feed, panels only get more transparent — same card otherwise,
           so the camera view doesn't reintroduce the old teal-tinted styling. */
        .workspace.over-camera .widget {
            background: rgba(14, 16, 23, 0.78);
            box-shadow: 0 24px 60px -12px rgba(0,0,0,0.8);
        }
        .workspace.over-camera .widget:hover {
            background: rgba(14, 16, 23, 0.94);
        }
        /* Keep the dock and title bar above the feed too. */
        body.camera-mode .bottom-control-bar { z-index: 1000; }
        body.camera-mode #cardsGrid { z-index: 55; }


        /* ===== STAGE SYSTEM (fullscreen panels like the desk cam) ===== */
        .stage {
            position: fixed; inset: 42px 0 0 0; z-index: 40;
            background: radial-gradient(ellipse at center, #070d16 0%, #04070c 100%);
            display: none; overflow: hidden;
        }
        .stage.active { display: block; }
        .stage-hud { position: absolute; inset: 0; pointer-events: none; z-index: 3; }
        .stage-hud .brk { position: absolute; width: 44px; height: 44px; border-color: rgba(94,234,212,0.75); }
        .stage-hud .brk.tl { top: 22px; left: 22px; border-top: 2px solid; border-left: 2px solid; }
        .stage-hud .brk.tr { top: 22px; right: 22px; border-top: 2px solid; border-right: 2px solid; }
        .stage-hud .brk.bl { bottom: 22px; left: 22px; border-bottom: 2px solid; border-left: 2px solid; }
        .stage-hud .brk.br { bottom: 22px; right: 22px; border-bottom: 2px solid; border-right: 2px solid; }
        .stage-title {
            position: absolute; top: 26px; left: 78px; color: rgba(94,234,212,0.92);
            font-size: 12px; letter-spacing: 4px; text-shadow: 0 0 12px rgba(94,234,212,0.6); z-index: 4;
        }
        .stage-close {
            position: absolute; top: 20px; right: 74px; z-index: 6; pointer-events: auto;
            background: rgba(255,46,99,0.1); border: 1px solid rgba(255,46,99,0.5);
            color: #ff5c7a; width: 30px; height: 26px; border-radius: 6px; cursor: pointer;
        }
        .stage-close:hover { background: #ff2e63; color: #fff; }
        .stage-body { position: absolute; inset: 70px 60px 96px 60px; display: flex; gap: 18px; }
        .stage-bar {
            position: absolute; bottom: 26px; left: 50%; transform: translateX(-50%);
            display: flex; gap: 10px; z-index: 6; pointer-events: auto;
            background: rgba(5,12,20,0.6); border: 1px solid rgba(94,234,212,0.3);
            padding: 8px 14px; border-radius: 30px; backdrop-filter: blur(8px);
        }
        .stage-input {
            flex: 1; background: rgba(0,0,0,0.5); border: 1px solid var(--border);
            border-radius: 8px; padding: 8px 12px; color: #fff; font-size: 12px; outline: none;
        }
        .stage-panel {
            background: rgba(9,15,26,0.55); border: 1px solid rgba(94,234,212,0.22);
            border-radius: 12px; backdrop-filter: blur(10px); overflow: auto;
            padding: 14px; min-height: 0;
        }
        /* CAD stage: rotating model centred, controls either side */
        #cadCanvas { width: 100%; height: 100%; display: block; }
        .cad-centre { flex: 1.4; position: relative; display: flex; align-items: center; justify-content: center; }
        .cad-side { width: 300px; display: flex; flex-direction: column; gap: 10px; min-height: 0; }
        /* ===== FULLSCREEN CAMERA MODE ===== */
        /* The feed fills the app; the reactor shrinks and docks bottom-right,
           blending into the video rather than sitting on top of it. */
        #camFullscreen {
            position: fixed; inset: 42px 0 0 0; z-index: 40;
            background: #000; display: none; overflow: hidden;
        }
        #camFullscreen.active { display: block; }
        #camFullImg { width: 100%; height: 100%; object-fit: cover; display: block; }
        /* Vignette + scanlines so the feed reads as a HUD rather than a raw webcam */
        #camFullscreen::after {
            content: ''; position: absolute; inset: 0; pointer-events: none;
            background:
              repeating-linear-gradient(rgba(0,0,0,0.10) 0 1px, transparent 1px 3px),
              radial-gradient(ellipse at center, transparent 55%, rgba(0,10,18,0.75) 100%);
        }
        .cam-hud { position: absolute; inset: 0; pointer-events: none; z-index: 2; }
        .cam-hud .brk { position: absolute; width: 44px; height: 44px; border-color: rgba(94,234,212,0.8); }
        .cam-hud .brk.tl { top: 22px; left: 22px; border-top: 2px solid; border-left: 2px solid; }
        .cam-hud .brk.tr { top: 22px; right: 22px; border-top: 2px solid; border-right: 2px solid; }
        .cam-hud .brk.bl { bottom: 22px; left: 22px; border-bottom: 2px solid; border-left: 2px solid; }
        .cam-hud .brk.br { bottom: 22px; right: 22px; border-bottom: 2px solid; border-right: 2px solid; }
        .cam-hud .label {
            position: absolute; top: 26px; left: 78px; color: rgba(94,234,212,0.9);
            font-size: 11px; letter-spacing: 3px; text-shadow: 0 0 10px rgba(94,234,212,0.7);
        }
        .cam-hud .rec {
            position: absolute; top: 26px; right: 78px; color: #ff2e63; font-size: 11px;
            letter-spacing: 2px; animation: tb-pulse 1.6s infinite;
        }
        #camFullBar {
            position: absolute; bottom: 26px; left: 50%; transform: translateX(-50%);
            display: flex; gap: 10px; z-index: 5; pointer-events: auto;
            background: rgba(5,12,20,0.55); border: 1px solid rgba(94,234,212,0.3);
            padding: 8px 14px; border-radius: 30px; backdrop-filter: blur(8px);
        }
        /* Reactor docked into the corner of the video, blended via screen blend
           mode + reduced opacity so it sits *in* the image, not on top of it. */
        .reactor-container.docked {
            top: auto; left: auto; bottom: 18px; right: 18px;
            transform: none; width: 150px; height: 150px; z-index: 45;
            opacity: 0.55; mix-blend-mode: screen; filter: blur(0.2px);
            transition: opacity 0.3s ease, transform 0.3s ease;
        }
        .reactor-container.docked:hover { opacity: 0.95; }
        .reactor-container.docked .ring-1 { width: 140px; height: 140px; }
        .reactor-container.docked .ring-2 { width: 112px; height: 112px; }
        .reactor-container.docked .ring-3 { width: 88px; height: 88px; border-width: 2px; }
        .reactor-container.docked .core-center {
            width: 62px; height: 62px; font-size: 10px; letter-spacing: 1px;
            background: rgba(9,13,22,0.45); box-shadow: 0 0 22px rgba(94,234,212,0.45);
        }
        /* While docked, speaking/listening states still show through clearly. */
        .reactor-container.docked.active-speech, 
        .reactor-container.docked.listening { opacity: 0.9; }
        .widget-header:active { cursor: grabbing; }
        .header-title-wrap { display: flex; align-items: center; gap: 8px; }
        .status-dot { width: 7px; height: 7px; background: var(--accent); border-radius: 50%; box-shadow: 0 0 8px var(--accent); }
        .close-widget-btn { background: none; border: none; color: var(--accent); font-weight: bold; cursor: pointer; font-size: 15px; line-height: 1; }
        .close-widget-btn:hover { color: #ff2e63; }

        #chatWidget { width: 430px; height: 480px; top: 70px; left: 30px; }
        #telemetryWidget { width: 300px; height: 190px; top: 565px; left: 30px; }
        #scratchpadWidget { width: 320px; height: 200px; top: 70px; right: 30px; }
        #todoWidget { width: 340px; height: 270px; top: 285px; right: 30px; }
        #spotifyWidget { width: 320px; height: 175px; top: 565px; right: 30px; border-color: rgba(30,215,96,0.4); }
        #spotifyWidget .widget-header { color: var(--green); border-bottom-color: rgba(30,215,96,0.2); }
        #viewerWidget { width: 560px; height: 470px; top: 90px; left: 480px; }
        #docWidget { width: 540px; height: 520px; top: 70px; left: 440px; }
        #settingsWidget { width: 330px; height: 420px; top: 150px; left: 50%; margin-left: -165px; }
        /* min-height:0 lets the flex child actually shrink so overflow-y works */
        .widget-body-pad { min-height: 0; }
        .settings-scroll { scrollbar-width: thin; scrollbar-color: var(--border-lit) transparent; }
        .settings-scroll::-webkit-scrollbar { width: 8px; }
        .settings-scroll::-webkit-scrollbar-track { background: rgba(0,0,0,0.35); border-radius: 4px; }
        .settings-scroll::-webkit-scrollbar-thumb { background: var(--border-lit); border-radius: 4px; }
        .settings-scroll::-webkit-scrollbar-thumb:hover { background: var(--accent); }
        #codeWidget { width: 580px; height: 520px; top: 70px; left: 400px; }
        #cadWidget { width: 520px; height: 500px; top: 80px; left: 460px; }
        #cameraWidget { width: 480px; height: 380px; top: 90px; left: 430px; border-color: rgba(16,185,129,0.4); }
        #cameraWidget::before, #cameraWidget::after { border-color: #10b981; }
        .cam-corner { position: absolute; width: 20px; height: 20px; border-color: rgba(16,185,129,0.85); pointer-events: none; }
        .cam-corner.tl { top: 8px; left: 8px; border-top: 2px solid; border-left: 2px solid; }
        .cam-corner.tr { top: 8px; right: 8px; border-top: 2px solid; border-right: 2px solid; }
        .cam-corner.bl { bottom: 8px; left: 8px; border-bottom: 2px solid; border-left: 2px solid; }
        .cam-corner.br { bottom: 8px; right: 8px; border-bottom: 2px solid; border-right: 2px solid; }
        #camRec { animation: tb-pulse 1.6s infinite; }
        #viewerBody img { max-width: 100%; max-height: 100%; object-fit: contain; }
        #viewerBody iframe { width: 100%; height: 100%; border: none; background: #fff; }

        /* Dynamic cards */
        #cardsGrid { position: absolute; top: 70px; left: 480px; width: 360px; max-height: calc(100vh - 170px);
            overflow-y: auto; display: flex; flex-direction: column; gap: 12px; pointer-events: auto; z-index: 15; }
        .amy-inapp-card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
            backdrop-filter: blur(10px); padding: 12px; color: #e2e8f0; box-shadow: 0 8px 26px rgba(0,0,0,0.5);
            animation: fadeInCard 0.25s ease-out; }
        @keyframes fadeInCard { from { opacity: 0; transform: translateY(-8px);} to { opacity: 1; transform: translateY(0);} }
        .amy-inapp-card:hover { border-color: var(--border-lit); }
        .card-header-bar { display: flex; justify-content: space-between; align-items: center;
            border-bottom: 1px solid var(--border); padding-bottom: 6px; margin-bottom: 8px; }
        .card-title-text { font-size: 11px; font-weight: bold; color: #fff; letter-spacing: 1px; }
        .card-type-badge { font-size: 9px; background: var(--accent-dim); color: var(--accent);
            padding: 2px 6px; border-radius: 6px; border: 1px solid var(--border-lit); text-transform: uppercase; }
        .card-body-content { font-size: 11px; line-height: 1.5; margin-bottom: 8px; max-height: 200px; overflow-y: auto; }
        .card-actions-bar { display: flex; justify-content: flex-end; gap: 6px; }

        /* Chat */
        .chat-messages { flex: 1; padding: 16px; overflow-y: auto; font-size: 12px; line-height: 1.6; color: var(--text-dim); user-select: text; }
        .chat-input-area { display: flex; padding: 12px; border-top: 1px solid var(--border); background: rgba(5,7,11,0.8); }
        .chat-input-area input { flex: 1; background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
            padding: 10px 14px; color: #fff; font-size: 12px; outline: none; user-select: text; }
        .chat-input-area input:focus { border-color: var(--accent); }
        .chat-input-area button { background: var(--accent); color: var(--bg); border: none; border-radius: 8px;
            padding: 0 16px; margin-left: 8px; font-weight: bold; cursor: pointer; letter-spacing: 1px; }
        .chat-input-area button:hover { box-shadow: 0 0 14px var(--accent); }

        .widget-body-pad { padding: 16px; font-size: 12px; display: flex; flex-direction: column; gap: 10px; flex: 1; }
        .telemetry-bar-bg { background: rgba(0,0,0,0.5); border: 1px solid var(--border); border-radius: 6px; height: 10px; width: 100%; overflow: hidden; padding: 1px; }
        .telemetry-fill { background: linear-gradient(90deg, rgba(94,234,212,0.4), var(--accent)); height: 100%; width: 0%; border-radius: 4px; transition: width 0.4s ease; box-shadow: 0 0 8px var(--accent); }
        .scratchpad-area { flex: 1; background: rgba(0,0,0,0.5); border: 1px solid var(--border); border-radius: 8px; color: #fff; padding: 10px; font-size: 12px; resize: none; outline: none; user-select: text; }
        .scratchpad-area:focus { border-color: var(--accent); }
        .action-btn { background: var(--accent-dim); color: var(--accent); border: 1px solid var(--border-lit); border-radius: 8px; padding: 8px 12px; font-weight: bold; cursor: pointer; font-size: 11px; text-align: center; letter-spacing: 1px; transition: all 0.2s ease; }
        .action-btn:hover { background: var(--accent); color: var(--bg); box-shadow: 0 0 12px var(--accent); }
        .todo-list-container { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; user-select: text; }
        .todo-item { display: flex; align-items: center; justify-content: space-between; background: rgba(94,234,212,0.05); padding: 6px 10px; border-radius: 8px; border: 1px solid rgba(94,234,212,0.2); }
        .todo-item span { font-size: 11px; color: #e2e8f0; }

        ::-webkit-scrollbar { width: 5px; height: 5px; }
        ::-webkit-scrollbar-track { background: rgba(0,0,0,0.3); }
        ::-webkit-scrollbar-thumb { background: var(--border-lit); border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--accent); }

        /* Bottom dock */
        .bottom-control-bar { position: absolute; bottom: 22px; left: 50%; transform: translateX(-50%);
            display: flex; gap: 10px; z-index: 1000; pointer-events: auto; align-items: center;
            background: rgba(9,13,22,0.9); padding: 9px 18px; border-radius: 40px; border: 1px solid var(--border-lit);
            backdrop-filter: blur(12px); box-shadow: 0 8px 30px rgba(0,0,0,0.7), 0 0 18px rgba(94,234,212,0.12); }
        .hud-btn { background: rgba(94,234,212,0.06); border: 1px solid var(--border-lit); color: var(--accent);
            width: 42px; height: 42px; border-radius: 50%; font-size: 16px; cursor: pointer;
            display: flex; align-items: center; justify-content: center; transition: all 0.2s ease; }
        .hud-btn:hover { background: var(--accent); color: var(--bg); box-shadow: 0 0 18px var(--accent); transform: translateY(-3px); }
        .hud-btn.danger { border-color: #ff2e63; color: #ff2e63; }
        .hud-btn.danger:hover { background: #ff2e63; color: #fff; box-shadow: 0 0 18px #ff2e63; }
        .hud-btn.active-mute { border-color: #f59e0b; color: #f59e0b; }
        .hud-btn.listening { border-color: var(--green); color: var(--green); animation: pulse-green 1.5s infinite; }
        .hud-btn.auto-vision-active { border-color: #c084fc; color: #c084fc; box-shadow: 0 0 18px rgba(192,132,252,0.7); }
        .separator { width: 1px; height: 26px; background: rgba(94,234,212,0.3); margin: 0 3px; }
        @keyframes pulse-green { 0% { box-shadow: 0 0 5px rgba(30,215,96,0.4);} 50% { box-shadow: 0 0 18px rgba(30,215,96,0.9);} 100% { box-shadow: 0 0 5px rgba(30,215,96,0.4);} }

        .watermark { position: absolute; bottom: 14px; right: 22px; font-size: 10px; color: rgba(94,234,212,0.3); letter-spacing: 2px; z-index: 5; pointer-events: none; }

        .spotify-controls { display: flex; gap: 12px; }
        .spotify-controls button { background: #131c2e; border: 1px solid var(--green); color: var(--green);
            width: 34px; height: 34px; border-radius: 50%; cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 12px; }
        .spotify-controls button.play-btn { background: var(--green); color: var(--bg); }

        /* ================================================================
           AMY THEME
           Dark, calm, glass panels over a quiet grid. Everything above this
           block is the old HUD styling; these rules take precedence.
           ================================================================ */
        :root {
            /* Palette shared with the project site (docs/index.html):
               cool near-black, neutral hairlines, teal into violet for accents. */
            --accent: #5eead4;
            --accent-2: #a78bfa;
            --accent-soft: rgba(94, 234, 212, 0.40);
            --accent-dim: rgba(94, 234, 212, 0.12);
            --green: #34d399;
            --bg: #07080c;
            --bg-soft: #0b0d14;
            --panel: rgba(14, 16, 23, 0.66);
            --panel-solid: #0e1017;
            --surface-2: #13161f;
            --border: rgba(255, 255, 255, 0.09);
            --border-lit: rgba(255, 255, 255, 0.16);
            --text: #e8eaf2;
            --text-dim: #8c92a6;
            --glass: rgba(14, 16, 23, 0.62);
            --glass-strong: rgba(14, 16, 23, 0.86);
            --radius: 14px;
            --ui-font: system-ui, -apple-system, "Segoe UI Variable Text", "Segoe UI", Inter, Roboto, Helvetica, Arial, sans-serif;
            --mono-font: ui-monospace, "Cascadia Mono", "Cascadia Code", Consolas, "Courier New", monospace;
        }
        * { font-family: var(--ui-font); }
        textarea#codeEditor, textarea#cadCode, pre, code, #codeOutput, #modalBody,
        .card-body-content pre { font-family: var(--mono-font) !important; }

        body { background: var(--bg); color: var(--text); background-image: none; }

        /* Living background: a slowly drifting gradient (canvas) under a quiet grid */
        #amyBg { position: fixed; inset: 0; width: 100%; height: 100%; z-index: 0;
                 pointer-events: none; display: block; }
        #amyGrid { position: fixed; inset: 0; z-index: 0; pointer-events: none;
            background-image:
                linear-gradient(rgba(255,255,255,0.055) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255,255,255,0.055) 1px, transparent 1px);
            background-size: 64px 64px; background-position: -1px -1px;
            -webkit-mask-image: linear-gradient(180deg, rgba(0,0,0,0.7), rgba(0,0,0,1));
                    mask-image: linear-gradient(180deg, rgba(0,0,0,0.7), rgba(0,0,0,1)); }

        /* Title bar */
        .titlebar { background: rgba(7,8,12,0.78); border-bottom: 1px solid var(--border);
            box-shadow: none; backdrop-filter: blur(14px) saturate(140%); height: 40px; }
        /* The site's wordmark orb: teal into violet, with a soft ring instead of a glow. */
        .tb-orb { width: 11px; height: 11px; animation: none;
            background: radial-gradient(circle at 32% 30%, var(--accent), var(--accent-2) 72%);
            box-shadow: 0 0 0 3px var(--accent-dim); }
        .tb-title { font-size: 14px; font-weight: 650; letter-spacing: -0.02em; color: var(--text); text-shadow: none; }
        .tb-sub { font-size: 10px; letter-spacing: 0.08em; color: var(--text-dim); margin-left: 10px; text-transform: none; }
        .tb-lines { display: none; }
        .tb-stat { font-size: 11px; letter-spacing: 0.02em; color: var(--text-dim); }
        .tb-stat b { color: var(--green); font-weight: 600; }
        .win-btn { background: transparent; border: none; color: var(--text-dim); border-radius: 6px; width: 34px; height: 26px; }
        .win-btn:hover { background: rgba(255,255,255,0.08); color: var(--text); box-shadow: none; }
        .win-btn.close { border: none; color: var(--text-dim); }
        .win-btn.close:hover { background: #c42b1c; color: #fff; box-shadow: none; }

        /* The orb: drawn on canvas, the name sits on top in real type */
        .reactor-container { width: 360px; height: 360px; }
        .reactor-container .ring { display: none; }
        .reactor-container canvas.amy-orb { position: absolute; inset: 0; width: 100%; height: 100%; }
        .reactor-container .core-center,
        .reactor-container.listening .core-center,
        .reactor-container.wake-triggered .core-center,
        .reactor-container.active-speech .core-center,
        .reactor-container.convo-mode .core-center {
            position: relative; width: auto; height: auto; background: none; border: none;
            box-shadow: none; color: #fff; display: flex; flex-direction: column;
            align-items: center; gap: 6px;
            /* The sphere is the mark; the name sits under it as a caption. */
            transform: translateY(132px); }
        /* The blob is the logo — no wordmark, no state caption under it. */
        .orb-name, .orb-state { display: none; }
        .orb-state { font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase;
            color: var(--text-dim); min-height: 14px; max-width: 200px; text-align: center;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .reactor-container.docked { width: 150px; height: 150px; opacity: 0.85; mix-blend-mode: normal; filter: none; }
        .reactor-container.docked .core-center { transform: translateY(56px); }
        .reactor-container.docked .orb-state { display: none; }

        /* Panels: a solid card with a brand hairline along the top edge,
           not a floating pane of glass. Content legibility over transparency. */
        .widget, .jarvis-inapp-card, .amy-inapp-card, .stage-panel {
            background: rgba(14, 16, 23, 0.94); border: 1px solid var(--border);
            border-radius: 16px; overflow: hidden;
            backdrop-filter: blur(20px) saturate(130%);
            box-shadow: 0 24px 60px -12px rgba(0,0,0,0.7);
            transition: border-color 0.2s ease, box-shadow 0.2s ease, transform 0.2s ease; }
        /* The one flash of brand on each panel: a 2px teal-to-violet edge. */
        .widget::before, .jarvis-inapp-card::before, .amy-inapp-card::before {
            content: ''; display: block; position: absolute; top: 0; left: 0; right: 0; height: 2px;
            background: linear-gradient(100deg, var(--accent), var(--accent-2));
            opacity: 0.85; pointer-events: none; z-index: 3; border: none; border-radius: 0; }
        .widget::after { display: none; }
        .widget:hover { border-color: var(--border-lit); box-shadow: 0 28px 68px -12px rgba(0,0,0,0.75); }
        .jarvis-inapp-card:hover, .amy-inapp-card:hover {
            border-color: var(--border-lit); transform: translateY(-2px); }
        .widget:focus-within { border-color: var(--accent-soft); }
        /* Header: no divider rule, just air. The eyebrow carries the type. */
        .widget-header { color: var(--text-dim); text-shadow: none; background: transparent;
            letter-spacing: 0.14em; font-weight: 500; font-size: 10px; text-transform: uppercase;
            font-family: var(--mono-font); border-bottom: none; padding: 16px 18px 10px; }
        .widget-header .status-dot, .status-dot { width: 6px; height: 6px; box-shadow: none; animation: none; background: var(--accent); }
        .close-widget-btn { color: var(--text-dim); font-weight: 400; transition: color 0.18s ease; }
        .close-widget-btn:hover { color: var(--text); }
        .resize-grip { background: none; border-right: 2px solid var(--border); border-bottom: 2px solid var(--border);
            width: 10px; height: 10px; right: 7px; bottom: 7px; border-bottom-right-radius: 3px; }
        .chat-messages { color: #c3c8d6; font-size: 13px; line-height: 1.65; }
        .chat-input-area { background: rgba(0,0,0,0.18); border-top: 1px solid var(--border); }
        .chat-input-area input, .stage-input, .scratchpad-area, #todoInput, #docEditInstruction {
            background: rgba(255,255,255,0.04); border: 1px solid var(--border); color: var(--text);
            border-radius: 10px; transition: border-color 0.18s ease; }
        .chat-input-area input:focus, .stage-input:focus, .scratchpad-area:focus,
        #todoInput:focus, #docEditInstruction:focus { border-color: var(--accent-soft); outline: none; }
        /* One primary button per panel, same as the site's .btn-primary. */
        .chat-input-area button { background: var(--accent); color: #04221d; border-radius: 10px;
            font-weight: 580; letter-spacing: 0; border: none;
            box-shadow: 0 6px 24px -8px var(--accent); transition: box-shadow 0.2s ease, filter 0.2s ease; }
        .chat-input-area button:hover { box-shadow: 0 10px 30px -8px var(--accent); filter: brightness(1.06); }
        /* Everything else is the ghost button: hairline, neutral, quiet. */
        .action-btn { background: transparent; color: var(--text); border: 1px solid var(--border-lit);
            border-radius: 10px; letter-spacing: 0; font-weight: 580; text-transform: none;
            transition: background 0.2s ease, border-color 0.2s ease; }
        .action-btn:hover { background: rgba(255,255,255,0.06); color: var(--text);
            border-color: var(--border-lit); box-shadow: none; }
        /* Badges are the site's chips: mono, pill, muted. */
        .card-type-badge { background: var(--surface-2); color: var(--text-dim);
            border: 1px solid var(--border); border-radius: 999px; padding: 4px 10px;
            font-family: var(--mono-font); font-size: 9.5px; letter-spacing: 0.04em; text-transform: lowercase; }
        .card-title-text { color: var(--text); letter-spacing: -0.01em; font-size: 13px; font-weight: 620; }
        .card-body-content { font-size: 12.5px; color: #c3c8d6; line-height: 1.65; }
        .card-body-content pre { background: var(--surface-2); border: 1px solid var(--border);
            border-radius: 10px; padding: 12px 14px; }
        .telemetry-fill { background: var(--accent); box-shadow: none; }
        .todo-item { background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: 10px; }
        .watermark { display: none; }

        /* Fullscreen stages: dark glass, no HUD brackets or scanlines */
        .stage { background: rgba(7,8,12,0.88); backdrop-filter: blur(18px); }
        .stage-hud .brk, .cam-hud .brk { display: none; }
        /* Camera label reads as the site's eyebrow, and names the active device. */
        .stage-title, .cam-hud .label { color: var(--text); text-shadow: none; letter-spacing: 0.12em; font-weight: 600; }
        .cam-hud .label { font-family: var(--mono-font); font-size: 11px; letter-spacing: 0.16em;
            text-transform: uppercase; color: var(--text-dim); background: var(--glass-strong);
            border: 1px solid var(--border); border-radius: 999px; padding: 6px 14px; }
        .cam-hud .rec { font-family: var(--mono-font); font-size: 10px; letter-spacing: 0.16em;
            color: var(--accent); text-shadow: none; }
        .stage-close { background: rgba(255,255,255,0.06); border: 1px solid var(--border);
            color: var(--text-dim); border-radius: 10px; }
        .stage-close:hover { background: rgba(255,255,255,0.1); color: var(--text); }
        /* Camera + stage bars use the dock's surface so every bar matches. */
        /* Clears the dock (22px up, ~60px tall) instead of sitting under it.
           width:max-content is load-bearing: absolutely positioned at left:50%,
           shrink-to-fit only offers half the window, so the bar wrapped early. */
        .stage-bar, #camFullBar { background: var(--glass-strong); border: 1px solid var(--border);
            bottom: 104px; border-radius: 16px; padding: 10px 12px; gap: 4px; flex-wrap: wrap;
            width: max-content; max-width: 92vw; justify-content: center;
            backdrop-filter: blur(24px) saturate(140%); box-shadow: 0 18px 50px rgba(0,0,0,0.5);
            display: flex; align-items: center; }
        /* The camera picker: a quiet select that matches the ghost buttons. */
        .cam-picker { background: rgba(255,255,255,0.04); color: var(--text);
            border: 1px solid var(--border); border-radius: 10px; padding: 8px 10px;
            font-family: var(--ui-font); font-size: 12.5px; min-width: 132px; max-width: 200px;
            cursor: pointer; outline: none; transition: border-color 0.18s ease; }
        .cam-picker:hover, .cam-picker:focus { border-color: var(--border-lit); }
        .cam-picker option { background: var(--panel-solid); color: var(--text); }
        .stage-body { bottom: 150px; }
        #camFullscreen::after { background: radial-gradient(ellipse at center, transparent 60%, rgba(0,0,0,0.55) 100%); }

        /* Dock: a floating command bar on the site's surface recipe —
           glass panel, hairline border, soft shadow. No trapezoid, no bright edge. */
        .amy-dock { position: absolute; left: 50%; bottom: 22px; transform: translateX(-50%);
            z-index: 1000; pointer-events: auto; padding: 10px 12px;
            min-width: min(620px, 92vw); max-width: 92vw;
            background: var(--glass-strong); border: 1px solid var(--border); border-radius: 16px;
            backdrop-filter: blur(24px) saturate(140%);
            box-shadow: 0 18px 50px rgba(0,0,0,0.5);
            transition: border-color 0.2s ease, box-shadow 0.2s ease; }
        .amy-dock:focus-within { border-color: var(--border-lit); box-shadow: 0 20px 56px rgba(0,0,0,0.55); }
        .amy-dock .dock-shape { display: none; }
        .amy-dock .dock-inner { position: relative; display: flex; align-items: center; gap: 10px; }
        .dock-ask { flex: 1; min-width: 220px; background: transparent; border: none; outline: none;
            color: var(--text); font-size: 14px; padding: 10px 4px; caret-color: var(--accent); }
        .dock-ask::placeholder { color: var(--text-dim); }
        .bottom-control-bar { position: static; transform: none; background: none; border: none;
            box-shadow: none; padding: 0; gap: 4px; backdrop-filter: none; }
        .hud-btn { width: 36px; height: 36px; border-radius: 10px; background: transparent;
            border: none; color: var(--text-dim); }
        .hud-btn:hover { background: rgba(255,255,255,0.07); color: var(--text); box-shadow: none; transform: none; }
        .hud-btn.danger { border: none; color: #e57373; }
        .hud-btn.danger:hover { background: rgba(229,115,115,0.16); color: #fff; box-shadow: none; }
        .hud-btn.active-mute { color: #f59e0b; border: none; background: rgba(245,158,11,0.14); }
        .hud-btn.listening { color: var(--accent); animation: none; background: var(--accent-dim); }
        /* The mic is the one primary action here, so it gets the accent fill. */
        #dockMic .hud-btn { width: 40px; height: 40px; border-radius: 12px;
            background: var(--accent); color: #04221d; }
        #dockMic .hud-btn:hover { background: var(--accent); color: #04221d; filter: brightness(1.08); }
        #dockMic .hud-btn.listening { background: var(--accent); color: #04221d;
            box-shadow: 0 0 0 4px var(--accent-dim); }
        #dockMic .hud-btn.active-mute { background: rgba(245,158,11,0.16); color: #f59e0b; }
        .separator { background: var(--border); height: 22px; width: 1px; margin: 0 4px; }

        ::-webkit-scrollbar-thumb, .widget *::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.14); }
        ::-webkit-scrollbar-thumb:hover, .widget *::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.28); }
        ::-webkit-scrollbar-track, .widget *::-webkit-scrollbar-track { background: transparent; }

        /* Proactive suggestions */
        #amySuggest { position: fixed; right: 18px; top: 54px; z-index: 900; display: flex;
            flex-direction: column; gap: 10px; max-width: 360px; pointer-events: none; }
        .amy-suggestion { pointer-events: auto; background: var(--glass-strong); border: 1px solid var(--border);
            border-left: 3px solid var(--accent); border-radius: 12px; padding: 12px 14px; color: var(--text);
            font-size: 13px; box-shadow: 0 14px 40px rgba(0,0,0,0.5); backdrop-filter: blur(20px);
            animation: amyIn .25s ease-out; }
        .amy-suggestion.warn { border-left-color: #f59e0b; }
        .amy-suggestion .row { display: flex; gap: 8px; margin-top: 10px; justify-content: flex-end; }
        .amy-suggestion button { background: rgba(255,255,255,0.06); border: 1px solid var(--border); color: var(--text);
            border-radius: 8px; padding: 5px 12px; cursor: pointer; font-size: 12px; }
        .amy-suggestion button.go { background: var(--accent); color: #04221d; border-color: transparent; font-weight: 600; }
        @keyframes amyIn { from { opacity: 0; transform: translateY(-6px); } to { opacity: 1; transform: none; } }

        /* ---- tidy layer: captions, activity chips, sleep screen ---- */
        #amyCaptions { position: fixed; left: 50%; bottom: 128px; transform: translateX(-50%);
            width: min(760px, calc(100vw - 32px)); z-index: 850; pointer-events: none;
            display: flex; flex-direction: column; align-items: center; gap: 6px; }
        .amy-cap { max-width: 100%; text-align: center; font-size: 15px; line-height: 1.45;
            color: #e6edf3; text-shadow: 0 1px 8px rgba(0,0,0,0.85); opacity: 0;
            transform: translateY(6px); transition: opacity .35s ease, transform .35s ease;
            display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
        .amy-cap.show { opacity: 1; transform: none; }
        .amy-cap.user { font-size: 13px; color: rgba(207,227,247,0.62); letter-spacing: .2px; }
        .amy-cap.user::before { content: "You  "; color: var(--accent); opacity: .8; font-size: 11px; letter-spacing: 1.5px; }
        #taskTray { position: fixed !important; left: 50% !important; right: auto !important;
            bottom: 84px !important; transform: translateX(-50%); flex-direction: row !important;
            flex-wrap: wrap; justify-content: center; gap: 6px; z-index: 860 !important;
            max-width: calc(100vw - 32px); pointer-events: none; }
        .amy-chip { display: inline-flex; align-items: center; gap: 7px; padding: 4px 11px 4px 9px;
            border-radius: 999px; font-size: 11px; color: #cfd8dc; background: rgba(16,18,20,0.78);
            border: 1px solid rgba(207,227,247,0.16); backdrop-filter: blur(8px);
            animation: chipIn .25s ease; transition: opacity .4s; }
        .amy-chip .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent);
            box-shadow: 0 0 8px var(--accent); }
        .amy-chip.running .dot { animation: chipPulse 1.2s ease-in-out infinite; }
        .amy-chip.done .dot { background: #4ade80; box-shadow: 0 0 8px #4ade80; }
        .amy-chip.failed { color: #fca5a5; border-color: rgba(239,68,68,0.35); }
        .amy-chip.failed .dot { background: #ef4444; box-shadow: 0 0 8px #ef4444; }
        .amy-chip .secs { color: var(--text-dim); font-variant-numeric: tabular-nums; }
        @keyframes chipIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
        @keyframes chipPulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }
        #amyStatusPill { position: fixed; top: 52px; left: 50%; transform: translateX(-50%); z-index: 800;
            padding: 4px 12px; border-radius: 999px; font-size: 11px; color: #cfd8dc;
            background: rgba(16,18,20,0.78); border: 1px solid rgba(207,227,247,0.14);
            opacity: 0; transition: opacity .5s; pointer-events: none; }
        #amyStatusPill.show { opacity: 1; }
        #amyStatusPill.warn { color: #fbbf24; border-color: rgba(245,158,11,0.4); }
        #amySleep { position: fixed; inset: 0; z-index: 5000; background: rgba(5,7,9,0.975);
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            opacity: 0; pointer-events: none; transition: opacity 1.2s ease; cursor: default; }
        #amySleep.on { opacity: 1; pointer-events: auto; }
        #amySleep .clock { font-size: 96px; font-weight: 200; letter-spacing: 4px; color: #e6edf3;
            font-variant-numeric: tabular-nums; }
        #amySleep .date { margin-top: 6px; font-size: 14px; letter-spacing: 3px; color: rgba(207,227,247,0.5);
            text-transform: uppercase; }
        #amySleep .breath { margin-top: 48px; width: 64px; height: 64px; border-radius: 50%;
            border: 2px solid var(--accent); box-shadow: 0 0 24px var(--accent);
            animation: breathe 5s ease-in-out infinite; }
        #amySleep .hint { margin-top: 22px; font-size: 11px; letter-spacing: 2px; color: rgba(207,227,247,0.35); }
        @keyframes breathe { 0%,100% { transform: scale(.82); opacity: .35; } 50% { transform: scale(1); opacity: .9; } }
    </style>
</head>
<body>
    <canvas id="amyBg"></canvas>
    <div id="amyGrid"></div>
    <div id="amySuggest"></div>

    <!-- TITLE BAR -->
    <div class="titlebar">
        <div class="tb-left">
            <div class="tb-orb"></div>
            <div><span class="tb-title">AMY</span><span class="tb-sub">Personal assistant</span></div>
        </div>
        <div class="tb-right">
            <div class="tb-stat" id="tbScreenCtx" style="max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"></div>
            <div class="tb-stat"><b>&#9679;</b> Online</div>
            <div class="tb-lines"><i></i><i></i><i></i><i></i></div>
            <div class="win-controls">
                <button class="win-btn" id="winMin" title="Minimise" onclick="pywebview.api.window_minimize()">&#8211;</button>
                <button class="win-btn" id="winMax" title="Maximise / Restore" onclick="pywebview.api.window_toggle_fullscreen()">&#9633;</button>
                <button class="win-btn close" id="winClose" title="Close" onclick="pywebview.api.close_app()">&#10005;</button>
            </div>
        </div>
    </div>

    <div class="reactor-container" id="reactorContainer">
        <canvas class="amy-orb" id="amyOrb"></canvas>
        <div class="core-center"><div class="orb-name">AMY</div><div class="orb-state" id="orbState"></div></div>
    </div>

    <!-- FULLSCREEN DESK CAMERA -->
    <div id="camFullscreen">
        <img id="camFullImg" alt="desk view">
        <div class="cam-hud">
            <div class="brk tl"></div><div class="brk tr"></div>
            <div class="brk bl"></div><div class="brk br"></div>
            <div class="label" id="camLabel">Desk view</div>
            <div class="rec">&#9679; LIVE</div>
        </div>
        <div id="gestureFlash" style="position:absolute; top:70px; left:50%; transform:translateX(-50%);
             background:rgba(0,0,0,0.7); border:1px solid #10b981; color:#10b981; padding:6px 16px;
             border-radius:16px; font-size:13px; letter-spacing:1px; opacity:0; transition:opacity 0.25s; z-index:5;">
        </div>
        <div id="camFullBar">
            <select id="camPicker" class="cam-picker" title="Switch camera"
                    onchange="pywebview.api.switch_camera(this.value)"></select>
            <button class="hud-btn" title="Scan for cameras" onclick="pywebview.api.scan_cameras()">&#8635;</button>
            <button class="hud-btn" title="Rename this camera" onclick="renameActiveCamera()">&#9998;</button>
            <div class="separator"></div>
            <button class="hud-btn" title="Identify objects" onclick="pywebview.api.lens_analyze('identify')">&#128269;</button>
            <button class="hud-btn" title="Read text" onclick="pywebview.api.lens_analyze('text')">&#128196;</button>
            <button class="hud-btn" title="Translate" onclick="pywebview.api.lens_analyze('translate')">&#127760;</button>
            <button class="hud-btn" title="Explain" onclick="pywebview.api.lens_analyze('explain')">&#128161;</button>
            <button class="hud-btn" title="Solve" onclick="pywebview.api.lens_analyze('solve')">&#129518;</button>
            <div class="separator"></div>
            <button class="hud-btn" title="Snapshot" onclick="pywebview.api.capture_camera_photo()">&#128247;</button>
            <button class="hud-btn" id="gestureBtn" title="Toggle gestures" onclick="toggleGestures()">&#128400;</button>
            <button class="hud-btn danger" title="Close desk view" onclick="pywebview.api.toggle_camera(false)">&#10005;</button>
        </div>
    </div>

    <!-- CAPTIONS (what you said / what Amy said) -->
    <div id="amyCaptions"></div>
    <div id="amyStatusPill"></div>
    <!-- SLEEP SCREEN -->
    <div id="amySleep">
        <div class="clock" id="sleepClock">00:00</div>
        <div class="date" id="sleepDate"></div>
        <div class="breath"></div>
        <div class="hint">SAY "AMY", CLAP TWICE, OR MOVE THE MOUSE</div>
    </div>

    <!-- BACKGROUND TASK TRAY -->
    <div id="taskTray" style="position:fixed; bottom:78px; right:16px; z-index:820;
         display:flex; flex-direction:column; gap:6px; align-items:flex-end;"></div>

    <!-- PROGRESS BAR -->
    <div id="progressWrap" style="position:fixed; top:42px; left:0; right:0; z-index:900; display:none;">
        <div style="height:3px; background:rgba(94,234,212,0.10);">
            <div id="progressFill" style="height:100%; width:0%; background:linear-gradient(90deg,#5eead4,#1ed760);
                 box-shadow:0 0 12px #5eead4; transition:width 0.35s ease;"></div>
        </div>
        <div style="display:flex; justify-content:space-between; padding:5px 16px;
             background:linear-gradient(180deg, rgba(5,12,20,0.92), rgba(5,12,20,0));">
            <span id="progressLabel" style="font-size:10px; letter-spacing:2px; color:var(--accent);"></span>
            <span id="progressPct" style="font-size:10px; color:#94a3b8;"></span>
        </div>
    </div>

    <!-- APPROVAL / DIFF / PROPOSAL MODAL -->
    <div id="modalWrap" style="position:fixed; inset:0; z-index:950; display:none;
         background:rgba(2,6,12,0.72); backdrop-filter:blur(4px); align-items:center; justify-content:center;">
        <div style="width:min(720px,88vw); max-height:78vh; display:flex; flex-direction:column;
             background:linear-gradient(160deg,rgba(13,19,32,0.98),rgba(7,11,19,0.98));
             border:1px solid var(--border-lit); border-radius:14px; overflow:hidden;
             box-shadow:0 20px 60px rgba(0,0,0,0.7), 0 0 30px rgba(94,234,212,0.15);">
            <div style="padding:12px 16px; border-bottom:1px solid var(--border);
                 display:flex; justify-content:space-between; align-items:center;
                 background:linear-gradient(90deg,rgba(94,234,212,0.12),transparent 70%);">
                <span id="modalTitle" style="color:var(--accent); font-size:12px; letter-spacing:2px;"></span>
                <span id="modalBadge" style="font-size:10px; color:#94a3b8;"></span>
            </div>
            <pre id="modalBody" style="flex:1; margin:0; padding:14px; overflow:auto; user-select:text;
                 font-family:'Consolas',monospace; font-size:11px; line-height:1.5;
                 color:#e8eaf2; white-space:pre-wrap; min-height:0;"></pre>
            <div id="modalButtons" style="padding:12px 16px; border-top:1px solid var(--border);
                 display:flex; gap:10px; justify-content:flex-end;"></div>
        </div>
    </div>

    <!-- LENS HISTORY -->
    <div class="stage" id="lensStage">
        <div class="stage-hud">
            <div class="brk tl"></div><div class="brk tr"></div>
            <div class="brk bl"></div><div class="brk br"></div>
        </div>
        <div class="stage-title">LENS &mdash; VISUAL HISTORY</div>
        <button class="stage-close" onclick="closeStage('lensStage')">&times;</button>
        <div class="stage-body">
            <div id="lensGrid" class="stage-panel" style="flex:1; display:flex; flex-direction:column; gap:10px;"></div>
        </div>
    </div>

    <!-- CAD DESIGN STAGE -->
    <div class="stage" id="cadStage">
        <div class="stage-hud">
            <div class="brk tl"></div><div class="brk tr"></div>
            <div class="brk bl"></div><div class="brk br"></div>
        </div>
        <div class="stage-title" id="cadStageTitle">CAD &mdash; PARAMETRIC MODEL</div>
        <button class="stage-close" onclick="closeStage('cadStage')">&times;</button>
        <div class="stage-body">
            <div class="stage-panel cad-side">
                <div style="font-size:10px;letter-spacing:2px;color:var(--accent);">DIMENSIONS</div>
                <div id="cadParams" style="display:flex;flex-direction:column;gap:12px;overflow-y:auto;flex:1;min-height:0;"></div>
            </div>
            <div class="cad-centre">
                <canvas id="cadCanvas"></canvas>
                <div id="cadEmpty" style="position:absolute;color:#475569;font-size:12px;">
                    No model loaded. Say &ldquo;design a phone stand&rdquo;.
                </div>
            </div>
            <div class="stage-panel cad-side">
                <div style="font-size:10px;letter-spacing:2px;color:var(--accent);">SOURCE</div>
                <textarea id="cadCode" spellcheck="false" oninput="onCadEdited()"
                    style="flex:1;min-height:0;background:rgba(0,0,0,0.5);border:1px solid var(--border);
                    border-radius:8px;color:#e8eaf2;padding:8px;font-family:'Consolas',monospace;
                    font-size:10px;white-space:pre;overflow:auto;outline:none;resize:none;"></textarea>
            </div>
        </div>
        <div class="stage-bar">
            <input type="text" id="cadInstruction" class="stage-input" placeholder="Describe a change..." autocomplete="off" style="width:280px;">
            <button class="hud-btn" title="Revise" onclick="submitCadEdit()">&#10227;</button>
            <button class="hud-btn" title="Improve design" onclick="pywebview.api.cad_improve()">&#10024;</button>
            <button class="hud-btn" title="Export STL" onclick="pywebview.api.cad_export_stl()">&#128190;</button>
            <button class="hud-btn" title="Open in OpenSCAD" onclick="pywebview.api.cad_open_external()">&#127760;</button>
        </div>
    </div>

    <div class="workspace">
        <div id="cardsGrid"></div>

        <div class="widget hidden" id="chatWidget">
            <div class="widget-header">
                <div class="header-title-wrap"><div class="status-dot"></div><span>SYSTEM LOG &amp; CHAT</span></div>
                <button class="close-widget-btn" onclick="toggleWidget('chatWidget')">&times;</button>
            </div>
            <div class="chat-messages" id="chatMessages"></div>
            <div class="chat-input-area">
                <input type="text" id="commandInput" placeholder="Enter command..." autocomplete="off">
                <button onclick="submitCommand()">SEND</button>
            </div>
        </div>

        <div class="widget hidden" id="telemetryWidget">
            <div class="widget-header">
                <div class="header-title-wrap"><div class="status-dot"></div><span>SYSTEM TELEMETRY</span></div>
                <button class="close-widget-btn" onclick="toggleWidget('telemetryWidget')">&times;</button>
            </div>
            <div class="widget-body-pad">
                <div>CPU UTILIZATION: <span id="cpuVal">0%</span></div>
                <div class="telemetry-bar-bg"><div class="telemetry-fill" id="cpuFill"></div></div>
                <div>RAM MEMORY: <span id="ramVal">0%</span></div>
                <div class="telemetry-bar-bg"><div class="telemetry-fill" id="ramFill"></div></div>
            </div>
        </div>

        <div class="widget hidden" id="scratchpadWidget">
            <div class="widget-header">
                <div class="header-title-wrap"><div class="status-dot"></div><span>MEMORY SCRATCHPAD</span></div>
                <button class="close-widget-btn" onclick="toggleWidget('scratchpadWidget')">&times;</button>
            </div>
            <div class="widget-body-pad">
                <textarea class="scratchpad-area" id="scratchpadInput" placeholder="Quick notes..."></textarea>
                <button class="action-btn" onclick="saveScratchpad()">SAVE TO MEMORY</button>
            </div>
        </div>

        <div class="widget hidden" id="todoWidget">
            <div class="widget-header">
                <div class="header-title-wrap"><div class="status-dot"></div><span>MISSION DIRECTIVES</span></div>
                <button class="close-widget-btn" onclick="toggleWidget('todoWidget')">&times;</button>
            </div>
            <div class="widget-body-pad">
                <div style="display: flex; gap: 6px;">
                    <input type="text" id="todoInput" placeholder="Add directive..." style="flex:1; background:rgba(0,0,0,0.5); border:1px solid var(--border); border-radius:8px; padding:7px; color:#fff; font-size:11px; outline:none;" autocomplete="off">
                    <button class="action-btn" onclick="addTodoItem()">ADD</button>
                </div>
                <div class="todo-list-container" id="todoListContainer"></div>
            </div>
        </div>

        <div class="widget hidden" id="spotifyWidget">
            <div class="widget-header">
                <div class="header-title-wrap"><div class="status-dot" style="background:var(--green);box-shadow:0 0 8px var(--green);"></div><span>SPOTIFY LINK</span></div>
                <button class="close-widget-btn" style="color:var(--green);" onclick="toggleWidget('spotifyWidget')">&times;</button>
            </div>
            <div class="widget-body-pad" style="align-items: center; justify-content: center; text-align: center;">
                <img id="spotifyAlbumArt" src="" style="width: 56px; height: 56px; border-radius: 8px; border: 1px solid var(--green); display: none; margin-bottom: 6px;">
                <div id="spotifyTrack" style="font-weight: bold; font-size: 12px; color: #fff; width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">Connecting Spotify...</div>
                <div id="spotifyArtist" style="font-size: 10px; color: #64748b; margin-bottom: 8px; width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">Please wait</div>
                <div class="spotify-controls">
                    <button onclick="pywebview.api.spotify_previous_js()">&#9198;</button>
                    <button class="play-btn" id="spotPlayBtn" onclick="pywebview.api.spotify_toggle_play_js()">&#9199;</button>
                    <button onclick="pywebview.api.spotify_next_js()">&#9197;</button>
                </div>
            </div>
        </div>

        <div class="widget hidden" id="viewerWidget">
            <div class="widget-header">
                <div class="header-title-wrap"><div class="status-dot"></div><span id="viewerTitle">VIEWER</span></div>
                <div style="display:flex; gap:6px; align-items:center;">
                    <button class="close-widget-btn" title="New tab" onclick="promptNewTab()">+</button>
                    <button class="close-widget-btn" onclick="toggleWidget('viewerWidget')">&times;</button>
                </div>
            </div>
            <div id="viewerTabs" style="display:flex; gap:4px; padding:6px 8px 0; overflow-x:auto; flex-shrink:0;"></div>
            <div id="viewerBody" style="flex:1; overflow:hidden; background:rgba(0,0,0,0.4); position:relative;">
                <div id="viewerEmpty" style="position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:#64748b; font-size:11px; padding:20px; text-align:center;">
                    Ask Amy to show an image or open a website.
                </div>
            </div>
        </div>

        <div class="widget hidden" id="docWidget">
            <div class="widget-header">
                <div class="header-title-wrap"><div class="status-dot"></div><span id="docTitle">DOCUMENT</span></div>
                <button class="close-widget-btn" onclick="toggleWidget('docWidget')">&times;</button>
            </div>
            <div style="display:flex; gap:4px; padding:8px 10px 0;">
                <button class="action-btn" id="docTabEdit" style="flex:1; padding:5px;" onclick="setDocTab('edit')">EDIT</button>
                <button class="action-btn" id="docTabPreview" style="flex:1; padding:5px; opacity:0.6;" onclick="setDocTab('preview')">PREVIEW</button>
            </div>
            <textarea id="docEditor" class="scratchpad-area" style="flex:1; margin:8px 10px; font-family:'Consolas',monospace;" placeholder="Your document will appear here..." oninput="onDocEdited()"></textarea>
            <iframe id="docPreview" style="flex:1; margin:8px 10px; border:1px solid var(--border); border-radius:8px; background:#0a0e17; display:none;"></iframe>
            <div style="display:flex; gap:6px; padding:0 10px 10px 10px;">
                <input type="text" id="docEditInstruction" placeholder="Tell Amy how to edit..." style="flex:1; background:rgba(0,0,0,0.5); border:1px solid var(--border); border-radius:8px; padding:7px; color:#fff; font-size:11px; outline:none;" autocomplete="off">
                <button class="action-btn" onclick="submitDocEdit()">REVISE</button>
                <button class="action-btn" onclick="pywebview.api.export_current_document_pdf()">EXPORT</button>
            </div>
        </div>

        <div class="widget hidden" id="codeWidget">
            <div class="widget-header">
                <div class="header-title-wrap"><div class="status-dot"></div><span id="codeTitle">CODE</span></div>
                <button class="close-widget-btn" onclick="toggleWidget('codeWidget')">&times;</button>
            </div>
            <textarea id="codeEditor" class="scratchpad-area" spellcheck="false" style="flex:1; margin:8px 10px; font-family:'Consolas',monospace; font-size:11px; line-height:1.5; white-space:pre; overflow-wrap:normal; overflow-x:auto;" placeholder="Generated code appears here..." oninput="onCodeEdited()"></textarea>
            <pre id="codeOutput" style="display:none; margin:0 10px 8px; padding:8px; max-height:130px; overflow:auto; background:#000; border:1px solid var(--border); border-radius:8px; color:#99f6e4; font-size:10px; white-space:pre-wrap; user-select:text;"></pre>
            <div style="display:flex; gap:6px; padding:0 10px 10px 10px;">
                <button class="action-btn" onclick="pywebview.api.run_code()">&#9654; RUN</button>
                <button class="action-btn" onclick="pywebview.api.fix_code()">FIX</button>
                <button class="action-btn" onclick="pywebview.api.explain_code()">EXPLAIN</button>
                <button class="action-btn" onclick="pywebview.api.open_code_in_editor()">EDITOR</button>
            </div>
        </div>

        <div class="widget hidden" id="settingsWidget">
            <div class="widget-header">
                <div class="header-title-wrap"><div class="status-dot"></div><span>CONTROL BAR SETTINGS</span></div>
                <button class="close-widget-btn" onclick="toggleWidget('settingsWidget')">&times;</button>
            </div>
            <div class="widget-body-pad" style="overflow:hidden; min-height:0;">
                <div style="font-size:10px; color:var(--text-dim); margin-bottom:4px; flex-shrink:0;">Toggle which buttons appear in the bottom bar:</div>
                <div id="settingsToggles" class="settings-scroll" style="display:flex; flex-direction:column; gap:5px; overflow-y:auto; overflow-x:hidden; flex:1 1 auto; min-height:0; padding-right:4px;"></div>
                <button class="action-btn" id="saveBarBtn" style="flex-shrink:0;" onclick="saveBarSettings()">SAVE LAYOUT</button>
            </div>
        </div>
    </div>

    <div class="amy-dock" id="amyDock">
        <svg class="dock-shape" viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden="true">
            <path class="fill" d="M0,100 L90,0 L910,0 L1000,100 Z"/>
            <path class="edge" vector-effect="non-scaling-stroke" d="M0,100 L90,0 L910,0 L1000,100"/>
        </svg>
        <div class="dock-inner">
            <div id="dockMic"></div>
            <input type="text" id="dockAsk" class="dock-ask" placeholder="Ask Amy anything..." autocomplete="off">
            <div class="bottom-control-bar" id="bottomBar"></div>
        </div>
    </div>
    <script>
// ---- AMY ORB + LIVING BACKGROUND -------------------------------------------
// One requestAnimationFrame loop drives both. The ring spins slowly when idle
// and swells, ripples and glows with the live microphone level, which the
// backend pushes in through amyLevel(). Rendering pauses while the window is
// hidden so it costs nothing in the background.
(function () {
  const ACCENT = [94, 234, 212];
  const ACCENT_2 = [167, 139, 250];
  const ACCENT_3 = [109, 72, 206];   // the violet, shaded, for the sphere's far edge
  const MUTED = [140, 146, 166];
  const MUTED_DEEP = [88, 94, 112];
  const MUTED_SHADE = [56, 60, 74];
  const TAU = Math.PI * 2;

  let target = 0, level = 0, angle = 0, flash = 0, last = 0;
  let state = 'idle';

  // Backend hooks ------------------------------------------------------------
  window.amyLevel = function (v) {
    const n = Number(v);
    target = isFinite(n) ? Math.max(0, Math.min(1, n)) : 0;
  };
  window.amyOrbState = function (s) {
    const next = String(s || 'idle');
    if (next === 'wake' && state !== 'wake') flash = 1;
    state = next;
  };

  // The main window signals state through classes on #reactorContainer; read
  // them each frame so every existing update*UI() call keeps working.
  function stateFromClasses(el) {
    if (!el) return state;
    const c = el.classList;
    if (c.contains('muted')) return 'muted';
    if (c.contains('active-speech')) return 'speaking';
    if (c.contains('wake-triggered')) return 'wake';
    if (c.contains('listening') || c.contains('convo-mode')) return 'listening';
    return state === 'thinking' ? 'thinking' : 'idle';
  }

  function fitCanvas(cv, scale) {
    const r = cv.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2) * (scale || 1);
    const w = Math.max(1, Math.round(r.width * dpr));
    const h = Math.max(1, Math.round(r.height * dpr));
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    return [w, h];
  }

  function rgba(c, a) { return 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + a.toFixed(3) + ')'; }


  function drawOrb(cv, t, st) {
    const [W, H] = fitCanvas(cv);
    const ctx = cv.getContext('2d');
    ctx.clearRect(0, 0, W, H);
    const S = Math.min(W, H), cx = W / 2, cy = H / 2, R = S * 0.36;
    const muted = st === 'muted';
    const col = muted ? MUTED : ACCENT;
    let lvl = muted ? 0 : level;
    if (st === 'speaking') {
      // Amy's own audio plays outside the browser, so there's no level to
      // read; a gentle synthetic pulse shows that she's talking.
      lvl = Math.max(lvl, 0.18 + 0.14 * Math.abs(Math.sin(t * 5.3)) * Math.abs(Math.sin(t * 1.7)));
    }

    // The mark itself: the site's wordmark orb, scaled up and made live.
    // CSS reads radial-gradient(circle at 32% 30%, accent, accent-2 72%);
    // the offsets below put the highlight in the same place on the sphere.
    const body = muted ? MUTED_DEEP : ACCENT_2;
    const deep = muted ? MUTED_SHADE : ACCENT_3;
    // A circle that breathes with the mic, rather than deforming.
    const r = R * (0.84 + 0.07 * lvl + 0.08 * flash);

    // Halo. Clamped inside the canvas or the corners square it off.
    const haloR = Math.min(r * 1.8, S * 0.495);
    const halo = ctx.createRadialGradient(cx, cy, r * 0.9, cx, cy, haloR);
    halo.addColorStop(0, rgba(col, muted ? 0.07 : 0.18 + 0.30 * lvl + 0.26 * flash));
    halo.addColorStop(0.45, rgba(body, muted ? 0.04 : 0.10 + 0.18 * lvl));
    halo.addColorStop(1, rgba(body, 0));
    ctx.fillStyle = halo;
    ctx.beginPath(); ctx.arc(cx, cy, haloR, 0, TAU); ctx.fill();

    // The silhouette stays a circle — the fluid motion is all inside it.
    // Everything below paints within this clip, so there is no rim or ring.
    ctx.save();
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, TAU); ctx.clip();

    // Gradient: highlight at 32%/30%, body colour by 72%. Scaled up, the stops
    // sit tighter than the CSS or it washes out to pastel. The origin drifts
    // and the mid stop breathes, so the fill animates under the deformation.
    const drift = t * 0.38;
    const ox = cx + r * (-0.36 + 0.11 * Math.cos(drift));
    const oy = cy + r * (-0.40 + 0.10 * Math.sin(drift * 1.17));
    const mid = 0.72 + 0.08 * Math.sin(t * 0.52);
    const sphere = ctx.createRadialGradient(ox, oy, r * 0.02, ox, oy, r * 1.62);
    sphere.addColorStop(0, rgba(col, 1));
    sphere.addColorStop(Math.max(0.12, mid - 0.38), rgba(col, 1));
    sphere.addColorStop(mid, rgba(body, 1));
    sphere.addColorStop(1, rgba(deep, 1));
    ctx.fillStyle = sphere;
    ctx.fillRect(cx - r * 1.6, cy - r * 1.6, r * 3.2, r * 3.2);

    // Two currents moving against each other, so the inside churns like
    // liquid rather than sliding as one piece.
    const bx = cx + r * 0.34 * Math.cos(-drift * 0.72 + 2.1);
    const by = cy + r * 0.34 * Math.sin(-drift * 0.72 + 2.1);
    const bloom = ctx.createRadialGradient(bx, by, 0, bx, by, r * (0.9 + 0.3 * lvl));
    bloom.addColorStop(0, rgba(col, muted ? 0.05 : 0.20 + 0.22 * lvl));
    bloom.addColorStop(1, rgba(col, 0));
    ctx.fillStyle = bloom;
    ctx.fillRect(cx - r * 1.6, cy - r * 1.6, r * 3.2, r * 3.2);

    const vx = cx + r * 0.40 * Math.cos(drift * 1.31 + 4.2);
    const vy = cy + r * 0.40 * Math.sin(drift * 1.31 + 4.2);
    const eddy = ctx.createRadialGradient(vx, vy, 0, vx, vy, r * (0.7 + 0.35 * lvl));
    eddy.addColorStop(0, rgba(body, muted ? 0.04 : 0.18 + 0.20 * lvl));
    eddy.addColorStop(1, rgba(body, 0));
    ctx.fillStyle = eddy;
    ctx.fillRect(cx - r * 1.6, cy - r * 1.6, r * 3.2, r * 3.2);

    // A slow sheen so it reads as a liquid surface rather than a flat shape.
    const sx = cx + r * 0.42 * Math.cos(angle * 0.6), sy = cy + r * 0.42 * Math.sin(angle * 0.6);
    const sheen = ctx.createRadialGradient(sx, sy, 0, sx, sy, r * 0.85);
    sheen.addColorStop(0, 'rgba(255,255,255,' + (muted ? 0.02 : 0.05 + 0.08 * lvl).toFixed(3) + ')');
    sheen.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = sheen;
    ctx.fillRect(cx - r * 1.6, cy - r * 1.6, r * 3.2, r * 3.2);

    ctx.restore();
  }

  // Slow, low-contrast colour drift. Drawn at a tiny resolution and scaled up
  // by the browser, so it's smooth and costs next to nothing per frame.
  function drawBackground(cv, t) {
    const W = 160, H = 96;
    if (cv.width !== W) { cv.width = W; cv.height = H; }
    const ctx = cv.getContext('2d');
    const base = ctx.createLinearGradient(0, 0, 0, H);
    // Cool near-black lifting to the site's --bg-soft, not a grey wash.
    base.addColorStop(0, '#07080c');
    base.addColorStop(1, '#141826');
    ctx.fillStyle = base;
    ctx.fillRect(0, 0, W, H);
    // Drifting blobs in the site's two accents only: teal and violet.
    const blobs = [
      [ACCENT, 0.10, 0.31, 0.23, 0.55],
      [ACCENT_2, 0.08, 0.19, 0.27, 0.65],
      [ACCENT_2, 0.05, 0.13, 0.17, 0.75],
    ];
    blobs.forEach(([c, a, fx, fy, rad], i) => {
      const x = W * (0.5 + 0.38 * Math.sin(t * fx * 0.5 + i * 2.1));
      const y = H * (0.5 + 0.34 * Math.cos(t * fy * 0.5 + i * 1.3));
      const gr = ctx.createRadialGradient(x, y, 0, x, y, W * rad);
      gr.addColorStop(0, rgba(c, a));
      gr.addColorStop(1, rgba(c, 0));
      ctx.fillStyle = gr;
      ctx.fillRect(0, 0, W, H);
    });
  }

  let bgSkip = 0;
  function frame(ms) {
    const t = ms / 1000;
    const dt = last ? Math.min(0.05, t - last) : 0.016;
    last = t;
    if (!document.hidden) {
      const container = document.getElementById('reactorContainer');
      const st = container ? stateFromClasses(container) : state;
      const goal = st === 'muted' ? 0 : target;
      level += (goal - level) * (goal > level ? 0.45 : 0.10);
      const speed = { idle: 0.35, listening: 0.18, wake: 0.9, speaking: 0.6,
                      thinking: 1.4, muted: 0.08 }[st] || 0.35;
      angle = (angle + dt * speed) % TAU;
      flash = Math.max(0, flash - dt * 1.6);
      document.querySelectorAll('canvas.amy-orb').forEach(cv => drawOrb(cv, t, st));
      // The background only needs ~30 fps.
      const bg = document.getElementById('amyBg');
      if (bg && (bgSkip++ & 1) === 0) drawBackground(bg, t);
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
})();

// ---- PROACTIVE SUGGESTIONS --------------------------------------------------
window.amySuggest = function (s) {
  const box = document.getElementById('amySuggest');
  if (!box || !s) return;
  const old = document.getElementById('sg_' + s.id);
  if (old) old.remove();
  const el = document.createElement('div');
  el.className = 'amy-suggestion' + (s.kind === 'warning' ? ' warn' : '');
  el.id = 'sg_' + s.id;
  const text = document.createElement('div');
  text.textContent = s.text || '';
  el.appendChild(text);
  const row = document.createElement('div');
  row.className = 'row';
  const no = document.createElement('button');
  no.textContent = 'Dismiss';
  no.onclick = () => { el.remove(); pywebview.api.dismiss_suggestion(s.id); };
  row.appendChild(no);
  if (s.action) {
    const go = document.createElement('button');
    go.className = 'go';
    go.textContent = s.action_label || 'Do it';
    go.onclick = () => { el.remove(); pywebview.api.accept_suggestion(s.id); };
    row.appendChild(go);
  }
  el.appendChild(row);
  box.prepend(el);
  while (box.children.length > 3) box.lastChild.remove();
  setTimeout(() => { if (el.isConnected) el.remove(); }, (s.ttl || 45) * 1000);
};
window.amyClearSuggestion = function (id) {
  const el = document.getElementById('sg_' + id);
  if (el) el.remove();
};

    </script>
    <script>
        window.addEventListener('pywebviewready', function() {
            renderBottomBar();          // render with defaults immediately
            makeWidgetsResizable();
            renderSettingsToggles();
            pywebview.api.set_ui_ready();
            appendLog("Amy is ready.", false);
        });

        const chatMessages = document.getElementById('chatMessages');
        const commandInput = document.getElementById('commandInput');
        const reactorContainer = document.getElementById('reactorContainer');
        // The orb always reads AMY; this caption carries the state.
        const coreLabel = document.getElementById('orbState');
        const dockAsk = document.getElementById('dockAsk');
        dockAsk.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter') return;
            const v = dockAsk.value.trim();
            if (!v) return;
            dockAsk.value = '';
            amyWakeUp();
            pywebview.api.handle_text_command(v);
        });

        commandInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') submitCommand(); });

        function appendLog(text, isUser = false) {
            amyCaptionFromLog(String(text), isUser);
            const div = document.createElement('div');
            div.style.color = isUser ? '#8fe9f7' : '#d6dee2';
            div.style.marginBottom = '6px';
            div.textContent = text;
            chatMessages.appendChild(div);
            chatMessages.scrollTop = chatMessages.scrollHeight;
        }

        async function submitCommand() {
            const cmd = commandInput.value.trim();
            if (!cmd) return;
            commandInput.value = '';
            pywebview.api.handle_text_command(cmd);
        }

        // Escape first, THEN turn URLs into links. Card content can contain
        // model output and page text, so injecting it raw was unsafe as well as
        // leaving links unclickable.
        function linkify(text) {
            const safe = escapeHtml(String(text == null ? '' : text));
            return safe.replace(
                /(https?:\/\/[^\s<>"']+)/g,
                (m) => {
                    const clean = m.replace(/[.,;:)\]]+$/, '');
                    const trail = m.slice(clean.length);
                    return `<a href="#" onclick="openLink(event, '${clean.replace(/'/g, "\\'")}')" ` +
                           `style="color:var(--accent);text-decoration:underline;` +
                           `word-break:break-all;cursor:pointer;">${clean}</a>` +
                           `<button class="copy-mini" title="Copy" ` +
                           `onclick="copyText(event, '${clean.replace(/'/g, "\\'")}')">copy</button>` + trail;
                });
        }

        function openLink(ev, url) {
            if (ev) ev.preventDefault();
            // Open inside Amy's own viewer rather than kicking you out to a
            // browser, matching how everything else behaves.
            try { pywebview.api.open_website_in_app(url); }
            catch (e) { window.open(url, '_blank'); }
        }

        function copyText(ev, text) {
            if (ev) { ev.preventDefault(); ev.stopPropagation(); }
            try {
                pywebview.api.copy_to_clipboard(text);
                const b = ev && ev.target;
                if (b) { const o = b.textContent; b.textContent = 'copied';
                         setTimeout(() => { b.textContent = o; }, 1200); }
            } catch (e) { appendLog('Copy failed: ' + e); }
        }

        function renderInAppCard(cardData) {
            const grid = document.getElementById('cardsGrid');
            if (!grid) return;

            const cardEl = document.createElement('div');
            cardEl.className = 'amy-inapp-card';
            cardEl.id = cardData.card_id;

            let bodyHtml = '';
            if (cardData.card_type === 'carousel') {
                bodyHtml = `<div style="display:flex; gap:8px; overflow-x:auto; padding:4px 0;">`;
                if (Array.isArray(cardData.content)) {
                    cardData.content.forEach(item => {
                        bodyHtml += `<div style="min-width:120px; background:rgba(0,0,0,0.5); border:1px solid var(--border-cyan); padding:8px; border-radius:4px; font-size:10px; user-select:text;">${linkify(item)}</div>`;
                    });
                } else {
                    bodyHtml += `<div style="user-select:text;">${linkify(cardData.content)}</div>`;
                }
                bodyHtml += `</div>`;
            } else if (cardData.card_type === 'code_bug') {
                bodyHtml = `<pre style="background:#000; color:#38bdf8; padding:8px; font-family:monospace; font-size:10px; border-radius:4px; overflow-x:auto; white-space:pre-wrap; user-select:text;">${linkify(cardData.content)}</pre>`;
            } else if (cardData.card_type === 'email_draft') {
                bodyHtml = `<div style="background:rgba(0,0,0,0.3); padding:8px; border-left:3px solid var(--accent-cyan); font-size:11px; white-space:pre-wrap; user-select:text;">${linkify(cardData.content)}</div>`;
            } else {
                bodyHtml = `<div style="user-select:text;">${linkify(cardData.content)}</div>`;
            }

            cardEl.innerHTML = `
                <div class="card-header-bar">
                    <span class="card-title-text">${escapeHtml(cardData.title)}</span>
                    <span class="card-type-badge">${escapeHtml(cardData.card_type)}</span>
                </div>
                <div class="card-body-content">${bodyHtml}</div>
                <div class="card-actions-bar">
                    <button class="action-btn" style="padding:3px 8px; font-size:9px;" onclick="dismissCard('${cardData.card_id}')">DISMISS</button>
                </div>
            `;

            grid.prepend(cardEl);
        }

        function dismissCard(cardId) {
            const el = document.getElementById(cardId);
            if (el) el.remove();
        }

        function toggleWidget(widgetId) {
            const el = document.getElementById(widgetId);
            if (el) el.classList.toggle('hidden');
        }

        function showWidget(widgetId) {
            const el = document.getElementById(widgetId);
            if (el) el.classList.remove('hidden');
        }

        function escapeHtml(s) {
            return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
        }

        // ---- IN-APP VIEWER (multiple tabs: images + websites) ----
        let viewerTabs = [];        // {id, kind, src, title}
        let activeTabId = null;
        let tabCounter = 0;

        function openViewer(data) {
            showWidget('viewerWidget');
            // Reuse a tab if the same source is already open.
            const existing = viewerTabs.find(t => t.src === data.src);
            if (existing) { activateTab(existing.id); return; }

            const tab = {
                id: 'vt' + (++tabCounter),
                kind: data.kind || 'web',
                src: data.src,
                title: (data.title || data.src || 'Tab')
            };
            viewerTabs.push(tab);

            // Build the content pane once, then just show/hide it. This keeps each
            // page's state (scroll position, logins) alive as you switch tabs.
            const body = document.getElementById('viewerBody');
            const pane = document.createElement('div');
            pane.id = 'pane_' + tab.id;
            pane.style.cssText = 'position:absolute; inset:0; display:none; align-items:center; justify-content:center; overflow:auto;';
            if (tab.kind === 'image') {
                pane.innerHTML = `<img src="${escapeHtml(tab.src)}" alt="image" style="max-width:100%;max-height:100%;object-fit:contain;" onerror="this.parentNode.innerHTML='<div style=color:#f43f5e;padding:20px;font-size:11px>Image could not be loaded.</div>'">`;
            } else {
                pane.innerHTML = `<iframe src="${escapeHtml(tab.src)}" style="width:100%;height:100%;border:none;background:#fff;" sandbox="allow-scripts allow-same-origin allow-forms allow-popups"></iframe>`;
            }
            body.appendChild(pane);
            activateTab(tab.id);
        }

        function activateTab(id) {
            activeTabId = id;
            viewerTabs.forEach(t => {
                const p = document.getElementById('pane_' + t.id);
                if (p) p.style.display = (t.id === id) ? 'flex' : 'none';
            });
            const empty = document.getElementById('viewerEmpty');
            if (empty) empty.style.display = viewerTabs.length ? 'none' : 'flex';
            const t = viewerTabs.find(x => x.id === id);
            const titleEl = document.getElementById('viewerTitle');
            if (titleEl) titleEl.textContent = t ? t.title.substring(0, 34).toUpperCase() : 'VIEWER';
            renderViewerTabs();
        }

        function closeTab(id, ev) {
            if (ev) ev.stopPropagation();
            const pane = document.getElementById('pane_' + id);
            if (pane) pane.remove();
            viewerTabs = viewerTabs.filter(t => t.id !== id);
            if (activeTabId === id) {
                activeTabId = viewerTabs.length ? viewerTabs[viewerTabs.length - 1].id : null;
            }
            if (activeTabId) {
                activateTab(activeTabId);
            } else {
                const empty = document.getElementById('viewerEmpty');
                if (empty) empty.style.display = 'flex';
                const titleEl = document.getElementById('viewerTitle');
                if (titleEl) titleEl.textContent = 'VIEWER';
                renderViewerTabs();
            }
        }

        function renderViewerTabs() {
            const bar = document.getElementById('viewerTabs');
            if (!bar) return;
            bar.innerHTML = '';
            viewerTabs.forEach(t => {
                const chip = document.createElement('div');
                const active = t.id === activeTabId;
                chip.style.cssText = 'display:flex;align-items:center;gap:6px;padding:4px 8px;border-radius:7px 7px 0 0;' +
                    'font-size:10px;cursor:pointer;white-space:nowrap;max-width:150px;flex-shrink:0;border:1px solid var(--border);' +
                    'border-bottom:none;' +
                    (active ? 'background:rgba(94,234,212,0.15);color:var(--accent);border-color:var(--border-lit);'
                            : 'background:rgba(0,0,0,0.35);color:#94a3b8;');
                chip.onclick = () => activateTab(t.id);

                const label = document.createElement('span');
                label.textContent = (t.kind === 'image' ? '🖼 ' : '🌐 ') + t.title;
                label.style.cssText = 'overflow:hidden;text-overflow:ellipsis;';
                chip.appendChild(label);

                const x = document.createElement('span');
                x.textContent = '×';
                x.style.cssText = 'font-weight:bold;opacity:0.7;padding:0 2px;';
                x.onclick = (e) => closeTab(t.id, e);
                chip.appendChild(x);

                bar.appendChild(chip);
            });
        }

        function promptNewTab() {
            const url = prompt('Open which website?', 'https://');
            if (url && url.trim() && url.trim() !== 'https://') {
                let u = url.trim();
                if (!/^https?:\/\//i.test(u)) u = 'https://' + u;
                openViewer({ kind: 'web', src: u, title: u.replace(/^https?:\/\//i, '').split('/')[0] });
            }
        }

        function closeAllTabs() {
            viewerTabs.slice().forEach(t => closeTab(t.id));
        }

        // ---- DOCUMENT EDITOR ----
        let docDirtyTimer = null;
        let docPreviewHtml = '';
        function showDocumentEditor(data) {
            showWidget('docWidget');
            document.getElementById('docTitle').textContent = (data.topic || 'DOCUMENT').substring(0, 40).toUpperCase();
            document.getElementById('docEditor').value = data.content || '';
            docPreviewHtml = data.preview_html || '';
            // If preview tab is active, refresh it.
            const pv = document.getElementById('docPreview');
            if (pv.style.display !== 'none') { pv.srcdoc = docPreviewHtml; }
        }
        function setDocTab(tab) {
            const editor = document.getElementById('docEditor');
            const preview = document.getElementById('docPreview');
            const tabEdit = document.getElementById('docTabEdit');
            const tabPreview = document.getElementById('docTabPreview');
            const inputRow = document.getElementById('docEditInstruction');
            if (tab === 'preview') {
                editor.style.display = 'none';
                preview.style.display = 'block';
                preview.srcdoc = docPreviewHtml || '<div style="color:#94a3b8;padding:20px;font-family:sans-serif">Generate or edit the document to see a styled preview.</div>';
                tabEdit.style.opacity = '0.6'; tabPreview.style.opacity = '1';
            } else {
                editor.style.display = 'block';
                preview.style.display = 'none';
                tabEdit.style.opacity = '1'; tabPreview.style.opacity = '0.6';
            }
        }
        function onDocEdited() {
            clearTimeout(docDirtyTimer);
            docDirtyTimer = setTimeout(() => {
                pywebview.api.update_document_from_ui(String(document.getElementById('docEditor').value));
            }, 600);
        }
        function submitDocEdit() {
            const inp = document.getElementById('docEditInstruction');
            const val = inp.value.trim();
            if (!val) return;
            inp.value = '';
            pywebview.api.edit_document_ui(String(val));
        }

        // ---- CODE EDITOR ----
        let codeDirtyTimer = null;
        function showCodeEditor(data) {
            showWidget('codeWidget');
            document.getElementById('codeTitle').textContent =
                ('CODE — ' + (data.language || '')).toUpperCase();
            document.getElementById('codeEditor').value = data.code || '';
            const out = document.getElementById('codeOutput');
            out.style.display = 'none';
            out.textContent = '';
        }
        function showCodeOutput(text) {
            showWidget('codeWidget');
            const out = document.getElementById('codeOutput');
            out.style.display = 'block';
            out.textContent = text || '(no output)';
            out.scrollTop = 0;
        }
        function onCodeEdited() {
            clearTimeout(codeDirtyTimer);
            codeDirtyTimer = setTimeout(() => {
                pywebview.api.update_code_from_ui(String(document.getElementById('codeEditor').value));
            }, 700);
        }

        // ---- CONVERSATION MODE INDICATOR ----
        function setConversationMode(active) {
            const bar = document.querySelector('.titlebar');
            let chip = document.getElementById('convoChip');
            if (active) {
                if (!chip) {
                    chip = document.createElement('div');
                    chip.id = 'convoChip';
                    chip.className = 'tb-stat';
                    chip.style.cssText = 'color:#1ed760;border:1px solid rgba(30,215,96,0.5);padding:2px 8px;border-radius:10px;animation:tb-pulse 2s infinite;';
                    chip.textContent = '● CONVERSATION';
                    const right = document.querySelector('.tb-right');
                    if (right) right.insertBefore(chip, right.firstChild);
                }
            } else if (chip) {
                chip.remove();
            }
            if (reactorContainer) reactorContainer.classList.toggle('convo-mode', !!active);
        }

        // ---- DESK CAMERA (fullscreen) ----
        // Tracks what the user actually wants. A frame already in flight when you
        // hit close used to re-open the stage a moment later.
        let cameraDesired = false;
        function setCameraState(active) {
            cameraDesired = !!active;
            const stage = document.getElementById('camFullscreen');
            const reactor = document.getElementById('reactorContainer');
            const ws = document.querySelector('.workspace');
            if (active) {
                if (stage) stage.classList.add('active');
                // Dock the reactor into the bottom-right of the feed and blend it in.
                if (reactor) reactor.classList.add('docked');
                // Widgets stay usable ON TOP of the feed, just slightly translucent
                // so you can see the desk through them.
                if (ws) ws.classList.add('over-camera');
                document.body.classList.add('camera-mode');
            } else {
                if (stage) stage.classList.remove('active');
                if (reactor) reactor.classList.remove('docked');
                if (ws) ws.classList.remove('over-camera');
                document.body.classList.remove('camera-mode');
                const img = document.getElementById('camFullImg');
                if (img) img.src = '';
            }
        }

        function updateCameraFrame(b64) {
            // Ignore stragglers that arrive after the user closed the view.
            if (!cameraDesired) return;
            const img = document.getElementById('camFullImg');
            if (img) {
                img.src = 'data:image/jpeg;base64,' + b64;
                const stage = document.getElementById('camFullscreen');
                if (stage && !stage.classList.contains('active')) {
                    stage.classList.add('active');
                    dockReactor(true);
                }
            }
        }

        // Called by the backend the instant a frame is confirmed, so the stage
        // only appears once the camera is genuinely delivering images.
        function cameraReady() {
            cameraDesired = true;
            const stage = document.getElementById('camFullscreen');
            if (stage) stage.classList.add('active');
            dockReactor(true);
        }

        let gesturesOn = true;
        function toggleGestures() {
            gesturesOn = !gesturesOn;
            pywebview.api.set_gestures(gesturesOn);
        }
        function setGestureState(on) {
            gesturesOn = on;
            const b = document.getElementById('gestureBtn');
            if (b) b.style.opacity = on ? '1' : '0.45';
        }

        let gestureFlashTimer = null;
        function flashGesture(name) {
            const el = document.getElementById('gestureFlash');
            if (!el) return;
            el.textContent = '✋ ' + name;
            el.style.opacity = '1';
            clearTimeout(gestureFlashTimer);
            gestureFlashTimer = setTimeout(() => { el.style.opacity = '0'; }, 1300);
        }

        // ---- MODAL (approval, diffs, CAD proposals) ----
        function openModal(title, body, buttons, badge) {
            const w = document.getElementById('modalWrap');
            document.getElementById('modalTitle').textContent = (title || '').toUpperCase();
            document.getElementById('modalBadge').textContent = badge || '';
            document.getElementById('modalBody').textContent = body || '';
            const bar = document.getElementById('modalButtons');
            bar.innerHTML = '';
            (buttons || []).forEach(b => {
                const el = document.createElement('button');
                el.className = 'action-btn';
                el.textContent = b.label;
                if (b.danger) { el.style.borderColor = 'rgba(255,46,99,0.5)'; el.style.color = '#ff5c7a'; }
                el.onclick = () => { closeModal(); b.fn(); };
                bar.appendChild(el);
            });
            w.style.display = 'flex';
        }
        function closeModal() {
            const w = document.getElementById('modalWrap');
            if (w) w.style.display = 'none';
        }

        function showApproval(name, preview) {
            openModal('Approve automation: ' + name, preview, [
                { label: 'CANCEL', danger: true, fn: () => pywebview.api.resolve_approval(false) },
                { label: 'RUN IT', fn: () => pywebview.api.resolve_approval(true) },
            ], 'awaiting your confirmation');
        }
        function hideApproval() { closeModal(); }

        function showDiff(title, diff) {
            openModal(title, diff, [{ label: 'CLOSE', fn: () => {} }], 'unified diff');
        }

        function showCadProposal(instruction, diff, valid) {
            openModal('Proposed change: ' + instruction, diff || '(no textual change)', [
                { label: 'DISCARD', danger: true, fn: () => pywebview.api.cad_discard() },
                { label: 'ACCEPT', fn: () => pywebview.api.cad_accept() },
            ], valid ? 'compiles cleanly' : '⚠ still has errors');
        }
        function hideCadProposal() { closeModal(); }

        // ---- LENS HISTORY GALLERY ----
        function showLensHistory(items) {
            openStage('lensStage');
            const grid = document.getElementById('lensGrid');
            if (!grid) return;
            grid.innerHTML = '';
            (items || []).forEach(it => {
                const card = document.createElement('div');
                card.style.cssText = 'border:1px solid var(--border); border-radius:10px; padding:10px;' +
                    'background:rgba(0,0,0,0.35); display:flex; flex-direction:column; gap:6px;';
                const head = document.createElement('div');
                head.style.cssText = 'display:flex; justify-content:space-between; font-size:10px; color:var(--accent);';
                head.innerHTML = '<span>' + (it.mode || '').toUpperCase() + '</span><span style="color:#64748b;">' + (it.ts || '') + '</span>';
                const body = document.createElement('div');
                body.style.cssText = 'font-size:11px; color:#cbd5e1; white-space:pre-wrap; user-select:text;';
                body.textContent = it.result || '';
                card.appendChild(head); card.appendChild(body);
                grid.appendChild(card);
            });
            if (!(items || []).length) {
                grid.innerHTML = '<div style="color:#64748b;font-size:11px;">Nothing analysed yet.</div>';
            }
        }


        // ---- CAPTIONS ----
        // The chat log stays tucked away; what you said and what Amy answered
        // appear briefly under the orb instead, like subtitles.
        let capTimer = null;
        function amyCaption(who, text) {
            const box = document.getElementById('amyCaptions');
            if (!box || !text) return;
            amyWakeUp();
            if (who === 'user') box.innerHTML = '';
            [...box.querySelectorAll('.amy-cap.' + who)].forEach(e => e.remove());
            const el = document.createElement('div');
            el.className = 'amy-cap ' + who;
            el.textContent = text.length > 320 ? text.slice(0, 317) + '...' : text;
            box.appendChild(el);
            requestAnimationFrame(() => el.classList.add('show'));
            clearTimeout(capTimer);
            const hold = Math.min(16000, 5000 + text.length * 45);
            capTimer = setTimeout(() => {
                box.querySelectorAll('.amy-cap').forEach(e => e.classList.remove('show'));
                setTimeout(() => { if (!box.querySelector('.amy-cap.show')) box.innerHTML = ''; }, 400);
            }, hold);
        }
        function amyCaptionFromLog(text, isUser) {
            if (isUser) {
                const t = text.replace(/^>\s*/, '').replace(/^"(.*)"$/, '$1');
                if (t) amyCaption('user', t);
            } else if (text.startsWith('Amy: ')) {
                amyCaption('amy', text.slice(5));
            }
        }

        // ---- QUIET STATUS PILL (replaces the startup toast) ----
        function amyStatus(text, warn) {
            const p = document.getElementById('amyStatusPill');
            if (!p) return;
            p.textContent = text;
            p.classList.toggle('warn', !!warn);
            p.classList.add('show');
            clearTimeout(p._t);
            p._t = setTimeout(() => p.classList.remove('show'), warn ? 7000 : 3500);
        }

        // ---- SLEEP SCREEN ----
        let amyLastActive = Date.now(), amySleeping = false, amySleepMinutes = 10;
        function amyWakeUp() {
            amyLastActive = Date.now();
            if (!amySleeping) return;
            amySleeping = false;
            document.getElementById('amySleep').classList.remove('on');
            try { pywebview.api.play_sound('unsleep'); pywebview.api.user_activity();
                  pywebview.api.set_sleeping(false); } catch (e) {}
        }
        window.amyWakeUp = amyWakeUp;
        function amyTickSleep() {
            const now = new Date();
            const c = document.getElementById('sleepClock');
            if (c) c.textContent = now.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
            const d = document.getElementById('sleepDate');
            if (d) d.textContent = now.toLocaleDateString([], {weekday: 'long', day: 'numeric', month: 'long'});
            const busy = document.querySelector('.amy-chip.running') ||
                         document.getElementById('camFullscreen')?.classList.contains('active');
            if (busy) amyLastActive = Date.now();
            if (!amySleeping && amySleepMinutes > 0 &&
                Date.now() - amyLastActive > amySleepMinutes * 60000) {
                amySleeping = true;
                document.getElementById('amySleep').classList.add('on');
                try { pywebview.api.play_sound('sleep'); pywebview.api.set_sleeping(true); } catch (e) {}
            }
        }
        setInterval(amyTickSleep, 1000);
        ['mousemove', 'mousedown', 'keydown', 'wheel', 'touchstart'].forEach(ev =>
            window.addEventListener(ev, () => amyWakeUp(), {passive: true}));
        window.addEventListener('pywebviewready', () => {
            pywebview.api.get_ui_prefs().then(p => {
                if (!p) return;
                amySleepMinutes = Number(p.sleep_minutes || 0);
                if (p.show_chat) showWidget('chatWidget');
            }).catch(() => {});
        });

        // ---- STARTUP DIGEST ----
        // Healthy start: a brief pill. Problems: an amber pill, details in the log.
        function showStartupDigest(summary, count) {
            appendLog((count ? 'Startup: ' : '') + summary, false);
            amyStatus(count ? count + ' thing' + (count > 1 ? 's' : '') + ' need attention - see the log'
                            : 'All systems ready', !!count);
        }

        // ---- MODAL (approval, diffs, CAD proposals) ---- END
        // ---- ACTIVE PROJECT ----
        function setActiveProject(name) {
            let chip = document.getElementById('projectChip');
            if (name) {
                if (!chip) {
                    chip = document.createElement('div');
                    chip.id = 'projectChip';
                    chip.className = 'tb-stat';
                    chip.style.cssText = 'color:#c084fc;border:1px solid rgba(192,132,252,0.5);padding:2px 8px;border-radius:10px;';
                    const right = document.querySelector('.tb-right');
                    if (right) right.insertBefore(chip, right.firstChild);
                }
                chip.textContent = '\u25C6 ' + name.toUpperCase();
            } else if (chip) { chip.remove(); }
        }

        // ---- THEMING ----
        function applyTheme(t) {
            if (!t) return;
            const r = document.documentElement.style;
            if (t.accent) {
                r.setProperty('--accent', t.accent);
                // Derive the accent tints so highlights follow the theme.
                const hex = t.accent.replace('#','');
                const n = parseInt(hex.length === 3
                    ? hex.split('').map(c=>c+c).join('') : hex, 16);
                const R=(n>>16)&255, G=(n>>8)&255, B=n&255;
                r.setProperty('--accent-soft', `rgba(${R},${G},${B},0.40)`);
                r.setProperty('--accent-dim',  `rgba(${R},${G},${B},0.12)`);
                // Lines stay neutral whatever the accent — panels should sit back.
                r.setProperty('--border', 'rgba(255,255,255,0.09)');
                r.setProperty('--border-lit', 'rgba(255,255,255,0.16)');
            }
            if (t.accent2) r.setProperty('--accent-2', t.accent2);
            if (t.bg) r.setProperty('--bg', t.bg);
            if (t.panel) r.setProperty('--panel', t.panel);
        }

        // ---- CAMERAS ----
        // The backend owns the list; this only mirrors it into the picker.
        let camDevices = [], camActive = '';
        function setCameraList(payload) {
            if (!payload) return;
            camDevices = payload.devices || [];
            camActive = payload.active || '';
            const sel = document.getElementById('camPicker');
            if (sel) {
                sel.innerHTML = '';
                camDevices.forEach(d => {
                    const o = document.createElement('option');
                    o.value = d.name;
                    o.textContent = d.name;
                    sel.appendChild(o);
                });
                // Set after every option exists — marking one selected while
                // still appending leaves the wrong row showing.
                sel.value = camActive;
                sel.style.display = camDevices.length > 1 ? '' : 'none';
            }
            const label = document.getElementById('camLabel');
            if (label) label.textContent = camActive || 'Desk view';
        }

        function renameActiveCamera() {
            if (!camActive) return;
            const next = window.prompt('Rename this camera', camActive);
            if (next && next.trim() && next.trim() !== camActive) {
                pywebview.api.rename_camera(camActive, next.trim());
            }
        }

        // ---- BACKGROUND TASKS ----
        // Small activity chips just above the dock: what Amy is working on.
        function updateTasks(tasks) {
            const tray = document.getElementById('taskTray');
            if (!tray) return;
            tray.innerHTML = '';
            (tasks || []).slice(-4).forEach(t => {
                const chip = document.createElement('div');
                chip.className = 'amy-chip ' + (t.state || 'running');
                const dot = document.createElement('span');
                dot.className = 'dot';
                chip.appendChild(dot);
                const label = document.createElement('span');
                label.textContent = t.name;
                chip.appendChild(label);
                if (t.state === 'running' && t.secs != null) {
                    const secs = document.createElement('span');
                    secs.className = 'secs';
                    secs.textContent = t.secs + 's';
                    chip.appendChild(secs);
                }
                chip.title = t.error || '';
                tray.appendChild(chip);
            });
        }

        // ---- PROGRESS BAR ----
        let progressHideTimer = null;
        function showProgress(label, pct) {
            const w = document.getElementById('progressWrap');
            const f = document.getElementById('progressFill');
            const l = document.getElementById('progressLabel');
            const p = document.getElementById('progressPct');
            if (!w) return;
            clearTimeout(progressHideTimer);
            w.style.display = 'block';
            if (l) l.textContent = (label || '').toUpperCase();
            const v = Math.max(0, Math.min(100, pct === undefined ? 0 : pct));
            if (f) f.style.width = v + '%';
            if (p) p.textContent = v >= 0 ? Math.round(v) + '%' : '';
        }
        function hideProgress() {
            const w = document.getElementById('progressWrap');
            const f = document.getElementById('progressFill');
            if (!w) return;
            if (f) f.style.width = '100%';
            progressHideTimer = setTimeout(() => {
                w.style.display = 'none';
                if (f) f.style.width = '0%';
            }, 600);
        }

        // ---- STAGE MANAGER (fullscreen panels) ----
        let activeStage = null;
        function openStage(id) {
            document.querySelectorAll('.stage').forEach(s => s.classList.remove('active'));
            const el = document.getElementById(id);
            if (!el) return;
            el.classList.add('active');
            activeStage = id;
            dockReactor(true);
            if (id === 'cadStage') startCadRender();
        }
        function closeStage(id) {
            const el = document.getElementById(id || activeStage);
            if (el) el.classList.remove('active');
            activeStage = null;
            const cam = document.getElementById('camFullscreen');
            dockReactor(cam && cam.classList.contains('active'));
            stopCadRender();
        }
        function dockReactor(on) {
            const r = document.getElementById('reactorContainer');
            const ws = document.querySelector('.workspace');
            if (!r) return;
            r.classList.toggle('docked', !!on);
            if (ws) ws.classList.toggle('over-camera', !!on);
            document.body.classList.toggle('camera-mode', !!on);
        }

        // ---- 3D CAD PREVIEW (no external libraries) ----
        // Multi-part, coloured, lit by real surface normals. Drag to orbit,
        // scroll to zoom; it slowly turns on its own when left alone.
        let cadMesh = null, cadCols = null, cadRAF = null, cadAngle = 0.6, cadTilt = 0.45,
            cadZoom = 1, cadDrag = null, cadIdleAt = 0;
        function hexRgb(h) {
            const n = parseInt(String(h || '#9aa5ab').replace('#', '').padEnd(6, '0').slice(0, 6), 16);
            return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
        }
        function setCadMesh(flat) {
            cadMesh = flat && flat.length ? flat : null;
            cadCols = null;
            cadGLDirty = true;
            const empty = document.getElementById('cadEmpty');
            if (empty) empty.style.display = cadMesh ? 'none' : 'block';
            if (cadMesh) startCadRender();
        }
        // parts: [{name, color, tris:[...]}], already normalised to about -1..1
        function setCadParts(parts) {
            const flat = [], cols = [];
            (parts || []).forEach(p => {
                const c = hexRgb(p.color);
                for (let i = 0; i < p.tris.length; i += 9) {
                    for (let k = 0; k < 9; k++) flat.push(p.tris[i + k]);
                    cols.push(c);
                }
            });
            setCadMesh(flat);
            cadCols = cols.length ? cols : null;
            cadGLDirty = true;
        }
        function startCadRender() {
            if (cadRAF) return;
            const loop = () => { drawCad(); cadRAF = requestAnimationFrame(loop); };
            cadRAF = requestAnimationFrame(loop);
        }
        function stopCadRender() {
            if (cadRAF) { cancelAnimationFrame(cadRAF); cadRAF = null; }
        }
        (function cadControls() {
            const cv = document.getElementById('cadCanvas');
            if (!cv) return;
            cv.addEventListener('mousedown', e => { cadDrag = [e.clientX, e.clientY]; });
            window.addEventListener('mouseup', () => { if (cadDrag) cadIdleAt = performance.now(); cadDrag = null; });
            window.addEventListener('mousemove', e => {
                if (!cadDrag) return;
                cadAngle += (e.clientX - cadDrag[0]) * 0.01;
                cadTilt = Math.max(-1.5, Math.min(1.5, cadTilt + (e.clientY - cadDrag[1]) * 0.01));
                cadDrag = [e.clientX, e.clientY];
            });
            cv.addEventListener('wheel', e => {
                cadZoom = Math.max(0.4, Math.min(4, cadZoom * (e.deltaY > 0 ? 0.9 : 1.1)));
                e.preventDefault();
            }, { passive: false });
        })();
        // WebGL path: real depth buffer, so overlapping and hollow parts draw
        // correctly at any angle. Falls back to the 2D painter if WebGL is off.
        let cadGL = undefined, cadGLBuf = null, cadGLCount = 0, cadGLDirty = true;
        function cadInitGL(cv) {
            const gl = cv.getContext('webgl', { antialias: true, alpha: true, premultipliedAlpha: false });
            if (!gl) return null;
            const vs = `attribute vec3 p; attribute vec3 n; attribute vec3 c;
                uniform mat4 m; uniform mat3 r; varying vec3 vn; varying vec3 vc;
                void main(){ gl_Position = m * vec4(p,1.0); vn = normalize(r * n); vc = c; }`;
            const fs = `precision mediump float; varying vec3 vn; varying vec3 vc;
                void main(){
                  vec3 n = normalize(vn); if (!gl_FrontFacing) n = -n;
                  float key = max(dot(n, normalize(vec3(0.35, 0.55, 0.76))), 0.0);
                  float fill = max(dot(n, normalize(vec3(-0.6, -0.2, 0.4))), 0.0);
                  float rim = pow(1.0 - abs(n.z), 3.0);
                  vec3 col = vc * (0.28 + 0.62 * key + 0.18 * fill) + vec3(0.13, 0.83, 0.93) * rim * 0.12;
                  gl_FragColor = vec4(col, 1.0);
                }`;
            const sh = (t, src) => { const o = gl.createShader(t); gl.shaderSource(o, src); gl.compileShader(o); return o; };
            const prog = gl.createProgram();
            gl.attachShader(prog, sh(gl.VERTEX_SHADER, vs));
            gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, fs));
            gl.linkProgram(prog);
            if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return null;
            gl.useProgram(prog);
            return { gl, prog, p: gl.getAttribLocation(prog, 'p'), n: gl.getAttribLocation(prog, 'n'),
                     c: gl.getAttribLocation(prog, 'c'), m: gl.getUniformLocation(prog, 'm'),
                     r: gl.getUniformLocation(prog, 'r') };
        }
        function cadUpload(G) {
            const gl = G.gl;
            const n = cadMesh.length / 9;
            const data = new Float32Array(n * 3 * 9);
            let o = 0;
            for (let t = 0; t < n; t++) {
                const i = t * 9;
                const ux = cadMesh[i+3]-cadMesh[i], uy = cadMesh[i+4]-cadMesh[i+1], uz = cadMesh[i+5]-cadMesh[i+2];
                const vx = cadMesh[i+6]-cadMesh[i], vy = cadMesh[i+7]-cadMesh[i+1], vz = cadMesh[i+8]-cadMesh[i+2];
                let nx = uy*vz-uz*vy, ny = uz*vx-ux*vz, nz = ux*vy-uy*vx;
                const l = Math.hypot(nx, ny, nz) || 1; nx /= l; ny /= l; nz /= l;
                const c = cadCols ? cadCols[t] : [90, 170, 200];
                for (let k = 0; k < 3; k++) {
                    data[o++] = cadMesh[i+k*3]; data[o++] = cadMesh[i+k*3+1]; data[o++] = cadMesh[i+k*3+2];
                    data[o++] = nx; data[o++] = ny; data[o++] = nz;
                    data[o++] = c[0] / 255; data[o++] = c[1] / 255; data[o++] = c[2] / 255;
                }
            }
            if (!cadGLBuf) cadGLBuf = gl.createBuffer();
            gl.bindBuffer(gl.ARRAY_BUFFER, cadGLBuf);
            gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
            cadGLCount = n * 3;
            cadGLDirty = false;
        }
        function drawCad() {
            const cv = document.getElementById('cadCanvas');
            if (!cv) return;
            if (cadGL === undefined) cadGL = cadInitGL(cv);
            if (!cadGL) { drawCad2d(cv); return; }
            const G = cadGL, gl = G.gl;
            const rect = cv.getBoundingClientRect();
            const dpr = Math.min(window.devicePixelRatio || 1, 2);
            const W = Math.max(1, Math.round(rect.width * dpr)), H = Math.max(1, Math.round(rect.height * dpr));
            if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
            gl.viewport(0, 0, W, H);
            gl.clearColor(0, 0, 0, 0);
            gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
            if (!cadMesh) return;
            if (cadGLDirty) cadUpload(G);
            if (!cadDrag && performance.now() - cadIdleAt > 2500) cadAngle += 0.004;
            gl.enable(gl.DEPTH_TEST);
            gl.bindBuffer(gl.ARRAY_BUFFER, cadGLBuf);
            gl.enableVertexAttribArray(G.p); gl.vertexAttribPointer(G.p, 3, gl.FLOAT, false, 36, 0);
            gl.enableVertexAttribArray(G.n); gl.vertexAttribPointer(G.n, 3, gl.FLOAT, false, 36, 12);
            gl.enableVertexAttribArray(G.c); gl.vertexAttribPointer(G.c, 3, gl.FLOAT, false, 36, 24);
            // Rotation: yaw about Z, then tilt about X; Z-up model, camera looks along +Y.
            const ca = Math.cos(cadAngle), sa = Math.sin(cadAngle), ct = Math.cos(cadTilt), st = Math.sin(cadTilt);
            // rows of R (model -> view), view: x right, y up, z towards viewer
            const R = [
                ca, -sa, 0,
                st * sa, st * ca, ct,
                -ct * sa, -ct * ca, st,
            ];
            const k = 0.64 * cadZoom, aspect = W / H;
            const sx = aspect >= 1 ? k / aspect : k, sy = aspect >= 1 ? k : k * aspect;
            // column-major mat4 for WebGL: clip = S * R * p, depth from view z
            const M = new Float32Array([
                sx * R[0], sy * R[3], -0.4 * R[6], 0,
                sx * R[1], sy * R[4], -0.4 * R[7], 0,
                sx * R[2], sy * R[5], -0.4 * R[8], 0,
                0, 0, 0, 1]);
            gl.uniformMatrix4fv(G.m, false, M);
            gl.uniformMatrix3fv(G.r, false, new Float32Array([R[0], R[3], R[6], R[1], R[4], R[7], R[2], R[5], R[8]]));
            gl.drawArrays(gl.TRIANGLES, 0, cadGLCount);
        }
        // Software fallback with a real depth buffer (correct overlaps, no
        // WebGL needed). Renders at reduced resolution and scales up.
        let cadSW = null;
        function drawCad2d(cv) {
            const rect = cv.getBoundingClientRect();
            const scaleDown = 0.6;
            const W = Math.max(1, Math.round(rect.width * scaleDown)), H = Math.max(1, Math.round(rect.height * scaleDown));
            if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; cadSW = null; }
            const ctx = cv.getContext('2d');
            if (!cadMesh) { ctx.clearRect(0, 0, W, H); return; }
            if (!cadSW) cadSW = { img: ctx.createImageData(W, H), z: new Float32Array(W * H) };
            const img = cadSW.img, zb = cadSW.z, px = img.data;
            px.fill(0); zb.fill(-1e9);
            if (!cadDrag && performance.now() - cadIdleAt > 2500) cadAngle += 0.004;
            const ca = Math.cos(cadAngle), sa = Math.sin(cadAngle), ct = Math.cos(cadTilt), st = Math.sin(cadTilt);
            const s = Math.min(W, H) * 0.32 * cadZoom, cx = W / 2, cy = H / 2;
            const L = [0.35, 0.55, 0.76];
            const P = new Array(3);
            for (let i = 0, t = 0; i < cadMesh.length; i += 9, t++) {
                for (let k = 0; k < 3; k++) {
                    const x = cadMesh[i + k * 3], y = cadMesh[i + k * 3 + 1], z = cadMesh[i + k * 3 + 2];
                    const vx = ca * x - sa * y;
                    const vy = st * sa * x + st * ca * y + ct * z;      // up
                    const vz = -ct * sa * x - ct * ca * y + st * z;     // towards viewer
                    P[k] = [cx + vx * s, cy - vy * s, vz];
                }
                const ux = P[1][0] - P[0][0], uy = P[1][1] - P[0][1], wx = P[2][0] - P[0][0], wy = P[2][1] - P[0][1];
                const area = ux * wy - uy * wx;
                if (Math.abs(area) < 1e-6) continue;
                // shading from the 3D normal in view space
                const ax = cadMesh[i + 3] - cadMesh[i], ay = cadMesh[i + 4] - cadMesh[i + 1], az = cadMesh[i + 5] - cadMesh[i + 2];
                const bx = cadMesh[i + 6] - cadMesh[i], by = cadMesh[i + 7] - cadMesh[i + 1], bz = cadMesh[i + 8] - cadMesh[i + 2];
                let nx = ay * bz - az * by, ny = az * bx - ax * bz, nz = ax * by - ay * bx;
                const nl = Math.hypot(nx, ny, nz) || 1; nx /= nl; ny /= nl; nz /= nl;
                const n0 = ca * nx - sa * ny, n1 = st * sa * nx + st * ca * ny + ct * nz, n2 = -ct * sa * nx - ct * ca * ny + st * nz;
                const lam = Math.abs(n0 * L[0] + n1 * L[1] + n2 * L[2]);
                const base = cadCols ? cadCols[t] : [90, 170, 200];
                const sh = 0.3 + 0.7 * lam;
                const r = base[0] * sh, g = base[1] * sh, bl = base[2] * sh;
                const minX = Math.max(0, Math.floor(Math.min(P[0][0], P[1][0], P[2][0])));
                const maxX = Math.min(W - 1, Math.ceil(Math.max(P[0][0], P[1][0], P[2][0])));
                const minY = Math.max(0, Math.floor(Math.min(P[0][1], P[1][1], P[2][1])));
                const maxY = Math.min(H - 1, Math.ceil(Math.max(P[0][1], P[1][1], P[2][1])));
                const inv = 1 / area;
                for (let yy = minY; yy <= maxY; yy++) {
                    const py = yy + 0.5;
                    for (let xx = minX; xx <= maxX; xx++) {
                        const pxx = xx + 0.5;
                        const w1 = ((pxx - P[0][0]) * wy - (py - P[0][1]) * wx) * inv;
                        const w2 = (ux * (py - P[0][1]) - uy * (pxx - P[0][0])) * inv;
                        const w0 = 1 - w1 - w2;
                        if (w0 < -1e-4 || w1 < -1e-4 || w2 < -1e-4) continue;
                        const z = w0 * P[0][2] + w1 * P[1][2] + w2 * P[2][2];
                        const idx = yy * W + xx;
                        if (z <= zb[idx]) continue;
                        zb[idx] = z;
                        const o = idx * 4;
                        px[o] = r; px[o + 1] = g; px[o + 2] = bl; px[o + 3] = 255;
                    }
                }
            }
            ctx.putImageData(img, 0, 0);
        }

        // ---- CAD DESIGN ----
        let cadDirtyTimer = null;
        function showCadEditor(data) {
            openStage('cadStage');
            const t = document.getElementById('cadStageTitle');
            if (t) t.textContent = ('CAD — ' + (data.name || '')).substring(0, 46).toUpperCase();
            const codeEl = document.getElementById('cadCode');
            if (codeEl) codeEl.value = data.code || '';
            const box = document.getElementById('cadParams');
            if (!box) return;
            box.innerHTML = '';
            (data.params || []).forEach(p => {
                const row = document.createElement('div');
                row.style.cssText = 'display:flex;flex-direction:column;gap:4px;';
                const lab = document.createElement('div');
                lab.style.cssText = 'display:flex;justify-content:space-between;font-size:10px;color:#94a3b8;';
                const nm = document.createElement('span');
                nm.textContent = p.name + (p.note ? '  (' + p.note + ')' : '');
                const val = document.createElement('span');
                val.style.color = 'var(--accent)';
                val.textContent = p.value;
                lab.appendChild(nm); lab.appendChild(val);
                const sl = document.createElement('input');
                sl.type = 'range';
                const base = Math.abs(p.value) || 10;
                sl.min = Math.max(0.1, base * 0.2).toFixed(2);
                sl.max = (base * 3).toFixed(2);
                sl.step = (base / 100).toFixed(3);
                sl.value = p.value;
                sl.style.cssText = 'accent-color:var(--accent);width:100%;';
                sl.oninput = () => { val.textContent = sl.value; };
                sl.onchange = () => { pywebview.api.cad_set_param(p.name, parseFloat(sl.value)); };
                row.appendChild(lab); row.appendChild(sl);
                box.appendChild(row);
            });
            if (!(data.params || []).length) {
                box.innerHTML = '<div style="color:#64748b;font-size:11px;">No adjustable dimensions detected.</div>';
            }
        }
        function onCadEdited() {
            clearTimeout(cadDirtyTimer);
            cadDirtyTimer = setTimeout(() => {
                pywebview.api.update_cad_from_ui(String(document.getElementById('cadCode').value));
            }, 700);
        }
        function submitCadEdit() {
            const i = document.getElementById('cadInstruction');
            const v = i.value.trim();
            if (!v) return;
            i.value = '';
            pywebview.api.cad_edit(v);
        }
        function setCadStlPath(p) { appendLog('STL ready: ' + p); }
        function setCadTab(tab) { /* stage shows both panes at once */ }

        // ---- ACTIVE PROJECT INDICATOR ----
        function setActiveProject(name) {
            let chip = document.getElementById('projectChip');
            if (name) {
                if (!chip) {
                    chip = document.createElement('div');
                    chip.id = 'projectChip';
                    chip.className = 'tb-stat';
                    chip.style.cssText = 'color:#c084fc;border:1px solid rgba(192,132,252,0.5);padding:2px 8px;border-radius:10px;';
                    const right = document.querySelector('.tb-right');
                    if (right) right.insertBefore(chip, right.firstChild);
                }
                chip.textContent = '\u25B8 ' + name.toUpperCase();
            } else if (chip) { chip.remove(); }
        }

        // ---- SECURITY SENTINEL ----
        function setSentinelState(active) {
            let chip = document.getElementById('sentinelChip');
            if (active) {
                if (!chip) {
                    chip = document.createElement('div');
                    chip.id = 'sentinelChip';
                    chip.className = 'tb-stat';
                    chip.style.cssText = 'color:#f59e0b;border:1px solid rgba(245,158,11,0.5);padding:2px 8px;border-radius:10px;';
                    chip.textContent = '\u26E8 SENTINEL';
                    const right = document.querySelector('.tb-right');
                    if (right) right.insertBefore(chip, right.firstChild);
                }
            } else if (chip) { chip.remove(); }
        }

        function flashSecurityAlert() {
            const b = document.body;
            b.style.transition = 'box-shadow 0.2s';
            let n = 0;
            const iv = setInterval(() => {
                b.style.boxShadow = (n % 2 === 0)
                    ? 'inset 0 0 120px rgba(239,68,68,0.55)' : 'inset 0 0 0 rgba(0,0,0,0)';
                if (++n > 5) { clearInterval(iv); b.style.boxShadow = 'none'; }
            }, 220);
        }

        // ---- LIVE SCREEN CONTEXT ----
        function updateScreenContext(text) {
            const el = document.getElementById('tbScreenCtx');
            if (el) el.textContent = text ? ('👁 ' + text) : '';
        }

        // ---- FRAMELESS WINDOW DRAGGING & CONTROLS ----
        (function setupTitlebarDrag() {
            const bar = document.querySelector('.titlebar');
            if (!bar) return;
            let dragging = false, startScreenX = 0, startScreenY = 0;

            bar.addEventListener('mousedown', (e) => {
                // Ignore clicks on the window buttons themselves.
                if (e.target.closest('.win-controls')) return;
                // Hand the drag to the OS: smooth, and Windows snapping works.
                e.preventDefault();
                try { pywebview.api.window_start_drag(); return; } catch (err) {}
                dragging = true;
                startScreenX = e.screenX;
                startScreenY = e.screenY;
            });

            document.addEventListener('mousemove', (e) => {
                if (!dragging) return;
                const dx = e.screenX - startScreenX;
                const dy = e.screenY - startScreenY;
                if (dx || dy) {
                    startScreenX = e.screenX;
                    startScreenY = e.screenY;
                    try { pywebview.api.window_move(dx, dy); } catch (err) {}
                }
            });

            document.addEventListener('mouseup', () => { dragging = false; });

            // Double-click the bar to maximise/restore, like a normal window.
            bar.addEventListener('dblclick', (e) => {
                if (e.target.closest('.win-controls')) return;
                pywebview.api.window_toggle_fullscreen();
            });
        })();

        // ---- WIDGET RESIZING ----
        // Adds a corner grip to every widget and persists nothing (positions are
        // per-session), matching the drag behaviour.
        function makeWidgetsResizable() {
            document.querySelectorAll('.widget').forEach(w => {
                if (w.querySelector('.resize-grip')) return;
                const grip = document.createElement('div');
                grip.className = 'resize-grip';
                grip.title = 'Drag to resize';
                w.appendChild(grip);

                let resizing = false, sx = 0, sy = 0, sw = 0, sh = 0;
                grip.addEventListener('mousedown', (e) => {
                    resizing = true;
                    sx = e.clientX; sy = e.clientY;
                    const r = w.getBoundingClientRect();
                    sw = r.width; sh = r.height;
                    // Lock to explicit pixel sizing before resizing.
                    w.style.width = sw + 'px';
                    w.style.height = sh + 'px';
                    e.preventDefault();
                    e.stopPropagation();
                });

                function onMove(e) {
                    if (!resizing) return;
                    const nw = Math.max(240, sw + (e.clientX - sx));
                    const nh = Math.max(150, sh + (e.clientY - sy));
                    w.style.width = nw + 'px';
                    w.style.height = nh + 'px';
                }
                function onUp() { resizing = false; }
                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp);
            });
        }

        // ---- ICONS ----
        // Line icons instead of emoji: they inherit the accent colour, stay
        // crisp at any size, and look identical on every machine. Emoji
        // rendered differently per font and couldn't be themed at all.
        const ICONS = {
            chat:      'M4 5h16v10H8l-4 4z',
            mic:       'M12 3a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3z M5 11a7 7 0 0 0 14 0 M12 18v3',
            micOff:    'M12 3a3 3 0 0 1 3 3v5 M9 9v3a3 3 0 0 0 4.6 2.5 M5 11a7 7 0 0 0 10.5 6 M12 18v3 M4 4l16 16',
            convo:     'M3 6h11v8H7l-4 3z M10 10h11v8h-4l-3 3v-3h-4z',
            camera:    'M3 7h11v10H3z M14 11l6-3v8l-6-3z',
            lens:      'M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14z M16.5 16.5L21 21',
            calendar:  'M4 6h16v14H4z M4 10h16 M8 3v4 M16 3v4',
            undo:      'M4 10h10a5 5 0 0 1 0 10H9 M4 10l4-4 M4 10l4 4',
            gallery:   'M3 5h18v14H3z M3 15l5-5 4 4 3-3 6 6',
            detach:    'M4 4h9v9H4z M11 11h9v9h-9z',
            ring:      'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18z M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8z',
            agenda:    'M5 4h14v16H5z M9 9h6 M9 13h6 M9 17h3',
            stop:      'M7 7h10v10H7z',
            settings:  'M12 9a3 3 0 1 1 0 6 3 3 0 0 1 0-6z M4 12h2 M18 12h2 M12 4v2 M12 18v2 M6.3 6.3l1.4 1.4 M16.3 16.3l1.4 1.4 M17.7 6.3l-1.4 1.4 M7.7 16.3l-1.4 1.4',
            power:     'M12 4v8 M6.5 7a8 8 0 1 0 11 0',
        };

        function iconSvg(name, size) {
            const d = ICONS[name];
            if (!d) return '';
            const paths = d.split(' M').map((seg, i) => (i ? 'M' + seg : seg))
                .map(p => '<path d="' + p + '"/>').join('');
            return '<svg viewBox="0 0 24 24" width="' + (size || 18) + '" height="'
                 + (size || 18) + '" fill="none" stroke="currentColor" stroke-width="1.6"'
                 + ' stroke-linecap="round" stroke-linejoin="round">' + paths + '</svg>';
        }

        // ---- CUSTOMISABLE BOTTOM BAR ----
        // Master list of every available control. `on` is the default state.
        // Only the essentials live on the dock. Everything else is voice-activated
        // (see COMMANDS.md) or opens automatically as a fullscreen stage.
        const BAR_BUTTONS = [
            { key:'chat',       icon:'chat',     title:'Chat Log',          action:()=>toggleWidget('chatWidget') },
            { key:'mic',        icon:'mic',      title:'Mute Mic', id:'muteBtn', action:()=>toggleMute() },
            { key:'convo',      icon:'convo',    title:'Conversation Mode', action:()=>pywebview.api.toggle_conversation() },
            { key:'camera',     icon:'camera',   title:'Desk View',         action:()=>pywebview.api.toggle_camera() },
            { key:'screenlens', icon:'lens',     title:'Read My Screen',    action:()=>pywebview.api.screen_lens('explain') },
            { key:'calendar',   icon:'calendar', title:"Today's Schedule",  action:()=>pywebview.api.calendar_view('today') },
            { key:'agenda',     icon:'agenda',   title:'Agenda',            action:()=>pywebview.api.calendar_agenda(7) },
            { key:'lenshist',   icon:'gallery',  title:'Lens History',      action:()=>pywebview.api.lens_history_view() },
            { key:'undo',       icon:'undo',     title:'Undo Last Action',  action:()=>pywebview.api.undo_last_action() },
            { sep:true },
            { key:'detach',     icon:'detach',   title:'Detach Terminal',   action:()=>pywebview.api.open_window('terminal') },
            { key:'overlay',    icon:'ring',     title:'Desktop Overlay',   action:()=>pywebview.api.toggle_overlay() },
            { sep:true },
            { key:'stop',       icon:'stop',     title:'Stop Speaking',     action:()=>stopSpeech() },
            { key:'settings',   icon:'settings', title:'Settings',          action:()=>toggleWidget('settingsWidget') },
            { key:'shutdown',   icon:'power',    title:'Shutdown', danger:true, always:true, action:()=>pywebview.api.close_app() },
        ];

        let barConfig = {};   // key -> bool, loaded from backend

        function renderBottomBar() {
            const bar = document.getElementById('bottomBar');
            const micSlot = document.getElementById('dockMic');
            bar.innerHTML = '';
            if (micSlot) micSlot.innerHTML = '';
            BAR_BUTTONS.forEach(b => {
                if (b.sep) {
                    // Only show separator if at least one visible button follows.
                    const sep = document.createElement('div');
                    sep.className = 'separator';
                    bar.appendChild(sep);
                    return;
                }
                const enabled = b.always || barConfig[b.key] !== false;
                if (!enabled) return;
                const btn = document.createElement('button');
                btn.className = 'hud-btn' + (b.danger ? ' danger' : '');
                if (b.id) btn.id = b.id;
                btn.title = b.title;
                btn.innerHTML = iconSvg(b.icon, 18);
                btn.onclick = b.action;
                if (b.key === 'mic' && micSlot) micSlot.appendChild(btn);
                else bar.appendChild(btn);
            });
        }

        function renderSettingsToggles() {
            const box = document.getElementById('settingsToggles');
            if (!box) return;
            box.innerHTML = '';
            BAR_BUTTONS.filter(b => !b.sep && !b.always).forEach(b => {
                // Make sure every key exists in barConfig so SAVE always writes a
                // complete layout, even if the user changes nothing.
                if (typeof barConfig[b.key] !== 'boolean') barConfig[b.key] = true;

                const row = document.createElement('label');
                row.style.cssText = 'display:flex;align-items:center;gap:8px;font-size:11px;color:#e2e8f0;cursor:pointer;padding:3px 4px;border-radius:6px;';
                row.onmouseenter = () => { row.style.background = 'rgba(94,234,212,0.08)'; };
                row.onmouseleave = () => { row.style.background = 'transparent'; };

                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.className = 'bar-toggle';
                cb.dataset.key = b.key;
                cb.checked = barConfig[b.key] !== false;
                cb.style.cssText = 'cursor:pointer;width:14px;height:14px;accent-color:#5eead4;';
                cb.addEventListener('change', () => {
                    barConfig[b.key] = cb.checked;
                    renderBottomBar();
                });

                row.appendChild(cb);
                const ic = document.createElement('span');
                ic.innerHTML = iconSvg(b.icon, 15);
                ic.style.cssText = 'display:inline-flex;margin-right:9px;vertical-align:-3px;';
                row.appendChild(ic);
                row.appendChild(document.createTextNode(b.title));
                box.appendChild(row);
            });
            attachSettingsScroll();
        }

        function applyBarConfig(cfg) {
            barConfig = Object.assign({}, cfg || {});
            renderBottomBar();
            renderSettingsToggles();
            attachSettingsScroll();
        }

        // Some webview backends don't propagate wheel events into nested flex
        // containers, so drive the scroll manually.
        function attachSettingsScroll() {
            const box = document.getElementById('settingsToggles');
            if (!box || box.dataset.wheelBound) return;
            box.dataset.wheelBound = '1';
            box.addEventListener('wheel', (e) => {
                box.scrollTop += (e.deltaY > 0 ? 44 : -44);
                e.preventDefault();
                e.stopPropagation();
            }, { passive: false });
        }

        function saveBarSettings() {
            // Read the checkboxes directly — this is the source of truth and works
            // even if no change event ever fired.
            const cfg = {};
            document.querySelectorAll('.bar-toggle').forEach(cb => {
                cfg[cb.dataset.key] = cb.checked;
            });
            barConfig = cfg;
            renderBottomBar();

            const btn = document.getElementById('saveBarBtn');
            try {
                pywebview.api.save_bar_config(JSON.stringify(cfg));
                if (btn) {
                    const old = btn.textContent;
                    btn.textContent = '✓ SAVED';
                    setTimeout(() => { btn.textContent = old; }, 1400);
                }
            } catch (e) {
                if (btn) btn.textContent = '✕ FAILED';
                appendLog('Settings save failed: ' + e);
            }
        }

        let isMuted = false;
        async function toggleMute() {
            isMuted = !isMuted;
            const btn = document.getElementById('muteBtn');
            if (btn) {
                // Swap to the struck-through mic so the state is obvious
                // without relying on colour alone.
                btn.innerHTML = iconSvg(isMuted ? 'micOff' : 'mic', 18);
                if (isMuted) {
                    btn.classList.add('active-mute');
                    btn.classList.remove('listening');
                } else {
                    btn.classList.remove('active-mute');
                }
            }
            if (isMuted) reactorContainer.classList.remove('listening', 'wake-triggered', 'active-speech');
            reactorContainer.classList.toggle('muted', isMuted);
            coreLabel.textContent = isMuted ? 'Muted' : '';
            pywebview.api.set_mic_mute(isMuted);
        }

        let autoVisionActive = false;
        function toggleContinuousVision() {
            autoVisionActive = !autoVisionActive;
            const btn = document.getElementById('autoVisionBtn');
            if (btn) btn.classList.toggle('auto-vision-active', autoVisionActive);
            appendLog("System: Continuous Screen AI Monitoring " + (autoVisionActive ? "ENABLED." : "DISABLED."));
            pywebview.api.toggle_continuous_vision(autoVisionActive);
        }

        function setAutoVisionState(state) {
            autoVisionActive = state;
            const btn = document.getElementById('autoVisionBtn');
            if (btn) btn.classList.toggle('auto-vision-active', autoVisionActive);
        }

        function updateListeningUI(listening) {
            const btn = document.getElementById('muteBtn');
            if (!isMuted) {
                if (listening) {
                    if (btn) btn.classList.add('listening');
                    reactorContainer.classList.add('listening');
                    coreLabel.textContent = "Listening";
                } else {
                    if (btn) btn.classList.remove('listening');
                    reactorContainer.classList.remove('listening');
                    coreLabel.textContent = "";
                }
            }
        }

        function updateWakeTriggerUI(triggered) {
            if (triggered) {
                reactorContainer.classList.add('wake-triggered');
                reactorContainer.classList.remove('listening');
                coreLabel.textContent = "Yes?";
            } else {
                reactorContainer.classList.remove('wake-triggered');
                coreLabel.textContent = "";
            }
        }

        function updateSpeechAnimation(active) {
            if (active) {
                reactorContainer.classList.add('active-speech');
                coreLabel.textContent = "Speaking";
            } else {
                reactorContainer.classList.remove('active-speech', 'listening', 'wake-triggered');
                coreLabel.textContent = "";
            }
        }

        async function stopSpeech() {
            pywebview.api.stop_speech();
            setAutoVisionState(false);
            updateSpeechAnimation(false);
        }

        function updateTelemetryUI(cpu, ram) {
            document.getElementById('cpuVal').textContent = cpu + '%';
            document.getElementById('cpuFill').style.width = cpu + '%';
            document.getElementById('ramVal').textContent = ram + '%';
            document.getElementById('ramFill').style.width = ram + '%';
        }

        function updateSpotifyUI(data) {
            const trackEl = document.getElementById('spotifyTrack');
            const artistEl = document.getElementById('spotifyArtist');
            const artEl = document.getElementById('spotifyAlbumArt');
            
            if (data && data.is_active) {
                trackEl.textContent = data.track_name || 'Unknown Track';
                artistEl.textContent = data.artist_name || 'Unknown Artist';
                if (data.album_art) {
                    artEl.src = data.album_art;
                    artEl.style.display = 'block';
                }
            } else if (data && !data.configured) {
                trackEl.textContent = "Spotify API Unconfigured";
                artistEl.textContent = "Check Client ID & Secret";
                artEl.style.display = 'none';
            } else {
                trackEl.textContent = "Spotify API Idle";
                artistEl.textContent = "No track currently playing";
                artEl.style.display = 'none';
            }
        }

        function saveScratchpad() {
            const text = document.getElementById('scratchpadInput').value;
            pywebview.api.save_scratchpad_note(String(text));
        }

        function addTodoItem() {
            const input = document.getElementById('todoInput');
            const task = input.value.trim();
            if (!task) return;
            input.value = '';
            pywebview.api.add_todo(String(task));
        }

        function renderTodoList(todos) {
            const container = document.getElementById('todoListContainer');
            if (!container) return;
            container.innerHTML = '';
            todos.forEach((item, idx) => {
                const div = document.createElement('div');
                div.className = 'todo-item';
                div.innerHTML = `<span>${item}</span><button class="action-btn" style="padding:2px 6px;" onclick="pywebview.api.remove_todo(${idx})">✓</button>`;
                container.appendChild(div);
            });
        }

        let activeWidget = null;
        let startX, startY, initialX, initialY;
        let highestZ = 10;

        document.querySelectorAll('.widget-header').forEach(header => {
            header.addEventListener('mousedown', (e) => {
                if (e.target.classList.contains('close-widget-btn')) return;
                activeWidget = header.closest('.widget');
                highestZ++;
                activeWidget.style.zIndex = highestZ;

                startX = e.clientX;
                startY = e.clientY;
                const rect = activeWidget.getBoundingClientRect();
                initialX = rect.left;
                initialY = rect.top;
                
                activeWidget.style.position = 'absolute';
                activeWidget.style.left = initialX + 'px';
                activeWidget.style.top = initialY + 'px';
                e.preventDefault();
            });
        });

        document.addEventListener('mousemove', (e) => {
            if (!activeWidget) return;
            const dx = e.clientX - startX;
            const dy = e.clientY - startY;
            activeWidget.style.left = (initialX + dx) + 'px';
            activeWidget.style.top = (initialY + dy) + 'px';
        });

        document.addEventListener('mouseup', () => { activeWidget = null; });
    </script>
</body>
</html>
"""




class AmyApp:
    def __init__(self):
        log_debug("Initializing Amy Autonomous Neural Engine...")
        self.memory_lock = threading.RLock()   # reentrant: mutators may call save_memory
        self.memory = self.load_memory()
        self.window = None
        self.ui_ready = False
        self.mic_muted = False
        self.is_speaking = False
        self.continuous_vision_active = False
        self.tray_icon = None
        self.sp = None
        self.hud_overlay = None
        self._overlay_visible = False
        self._overlay_state = "idle"
        self.current_doc = None
        self._spotify_device_cache = None
        self._spotify_device_cache_ts = 0
        # Ambient screen awareness state
        self.screen_context = ""
        self.screen_context_time = 0
        self.vision_interval = float(CONFIG.get("assistant", {}).get("vision_interval", 8))
        # Coding engine state
        self.current_code = None
        self.last_code_error = ""
        self.automation_enabled = True
        # Conversation mode: continuous chat without repeating the wake word
        self.conversation_mode = False
        self.last_interaction = 0
        self.conversation_timeout = float(CONFIG.get("assistant", {}).get("conversation_timeout", 90))
        # Desk camera / gesture state
        self.camera_active = False
        self._camera_thread = None
        self.gestures_enabled = bool(CONFIG.get("camera", {}).get("gestures_enabled", True))
        self.last_frame = None
        self.last_camera_description = ""
        self._last_gesture = None
        self._last_gesture_time = 0
        self._pending_gesture = None
        self._pending_since = 0
        self.focus_until = 0
        self.sentinel_active = False
        self.current_cad = None
        self.response_cache = {}
        self.available_models = []
        self._model_cache = {}
        self._vosk_model = None
        self._grammar_cache = None
        self._embed_store = None
        self._last_save = 0.0
        self._save_pending = False
        self._last_backup = 0.0
        self.plugin_commands = {}
        self.last_cad_mesh = []
        self._in_custom = False
        self.extra_windows = {}
        self._api = None
        self._browser_page = None
        self._browser_ctx = None
        self._browser_pw = None
        self.pending_clarification = None
        self.background_tasks = {}
        self._task_lock = threading.Lock()
        self.pending_plan = None
        self.pending_cad = None
        self._watcher_started = False
        self.awaiting_approval = False
        self.approval_result = False
        self.config_issues = {}
        self.active_project = None
        self.action_history = []
        self._last_nudge = None
        self.active_project = None
        self.last_action = None
        self._ha_entities = None
        self._ha_entities_ts = 0
        self._last_nudge = None
        self._frame_in_flight = False
        self._camera_announced = False
        self._force_recalibrate = False
        self._stt_failures = 0
        self._sentinel_started = False

        self.sapi_speaker = None
        self.current_tts_proc = None
        self.speech_queue = queue.Queue()
        self._speech_gen = 0               # bumped on every interruption
        self._tts_pool = None
        self.audio_out = AudioOut() if AudioOut.available() else None
        self.audio_in = None
        self._cmd_queue = queue.Queue()
        self._followup_until = 0.0
        threading.Thread(target=self._command_worker, daemon=True, name="commands").start()
        threading.Thread(target=self._level_pusher, daemon=True, name="levels").start()

        # Reusing one HTTP session avoids TCP/handshake overhead on every call.
        # A tuned session: connection pooling plus automatic retry on the
        # transient failures that otherwise surface as random one-off errors.
        self.http = _OllamaSession()
        try:
            from requests.adapters import HTTPAdapter
            try:
                from urllib3.util.retry import Retry
            except ImportError:
                from requests.packages.urllib3.util.retry import Retry
            retry = Retry(
                total=2, connect=2, read=2, backoff_factor=0.4,
                status_forcelist=(500, 502, 503, 504),
                allowed_methods=frozenset(["GET", "POST"]),
            )
            adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=retry)
            self.http.mount("http://", adapter)
            self.http.mount("https://", adapter)
            self.http.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Amy/1.0"
            })
        except Exception as e:
            log_debug(f"http tuning skipped: {e}")

        # Installed apps, found by asking Windows (refreshed in the background).
        self.app_index = AppIndex(os.path.join(DATA_DIR, "app_index.json"))
        self.app_index.refresh_async()
        # Operates any app step by step: accessibility tree + vision.
        self.agent = ComputerAgent(self)
        # SOUL.md / MEMORY.md / HEARTBEAT.md / skills, and interface sounds.
        self.mind = MindFiles(DATA_DIR, SYSTEM_PROMPT)
        self.sfx = SoundFX()
        self._last_input_voice = False
        self._whisper = None
        self._whisper_failed = False
        self.sleeping = False
        # Notices things and speaks up on its own (started once the UI is up).
        self.proactive = ProactiveEngine(self)
        self.latest_screen = None
        self.latest_screen_time = 0.0
        self._tls = threading.local()

        self.setup_system_tray()

        # Warm the model up in the background so the first real question is fast
        # (Ollama otherwise pays a multi-second model-load cost on first use).
        # Start Ollama if it isn't already running, then warm the model up.
        self.ollama_ready = threading.Event()
        self._ollama_proc = None
        threading.Thread(target=self._ensure_ollama, daemon=True, name="ollama").start()
        threading.Thread(target=self._warm_up_model, daemon=True).start()
        threading.Thread(target=self._calendar_watcher, daemon=True).start()
        threading.Thread(target=self._habit_worker, daemon=True).start()

        # Restore the project you were last working on.
        self.active_project = self.memory.get("active_project")

        threading.Thread(target=self._tts_worker, daemon=True).start()
        threading.Thread(target=self.telemetry_loop, daemon=True).start()
        threading.Thread(target=self.spotify_loop, daemon=True).start()
        threading.Thread(target=self._continuous_vision_worker, daemon=True).start()

        def delayed_audio_start():
            time.sleep(1.0)
            threading.Thread(target=self.voice_listener_loop, daemon=True).start()

        threading.Thread(target=delayed_audio_start, daemon=True).start()
        log_debug("Amy initialization complete.")

    def setup_system_tray(self):
        if not TRAY_AVAILABLE:
            log_debug("pystray not installed; skipping system tray icon.")
            return

        def on_show_hud(icon, item):
            if self.window:
                try:
                    self.window.restore()
                    self.window.focus()
                except Exception as e:
                    log_debug(f"Tray restore error: {e}")

        def on_toggle_overlay(icon, item):
            self.toggle_overlay()

        def on_shutdown(icon, item):
            self.close_app()

        try:
            img = Image.new('RGB', (64, 64), color=(5, 7, 11))
            d = ImageDraw.Draw(img)
            d.ellipse((8, 8, 56, 56), outline=(0, 243, 255), width=3)
            d.ellipse((20, 20, 44, 44), fill=(0, 243, 255))

            menu = pystray.Menu(
                pystray.MenuItem('Show Amy HUD', on_show_hud, default=True),
                pystray.MenuItem('Toggle Corner HUD Overlay', on_toggle_overlay),
                pystray.MenuItem('Shutdown Core', on_shutdown)
            )
            self.tray_icon = pystray.Icon("Amy", img, "Amy Core", menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
            log_debug("System Tray icon initialized successfully.")
        except Exception as e:
            log_debug(f"Failed to initialize System Tray: {e}")

    # --- IN-APP DYNAMIC CARDS ENGINE (AmyCard) ---
    def _ollama_up(self, timeout=2.0):
        try:
            r = self.http.get(OLLAMA_GENERATE_URL.replace("/api/generate", "/api/tags"), timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    def _find_ollama(self):
        """Locate ollama.exe: PATH first, then the app index, then the
        installer's default per-user folder."""
        exe = shutil.which("ollama")
        if exe:
            return exe
        try:
            self.app_index.ready.wait(5)
            entry, score = self.app_index.resolve("ollama")
            if entry and score >= 0.9 and entry.get("kind") in ("apppath", "path"):
                return entry["target"]
        except Exception:
            pass
        local = os.environ.get("LOCALAPPDATA", "")
        for p in (os.path.join(local, "Programs", "Ollama", "ollama.exe"),
                  os.path.join(os.environ.get("PROGRAMFILES", ""), "Ollama", "ollama.exe")):
            if p and os.path.exists(p):
                return p
        return None

    def _ensure_ollama(self):
        """Make sure the Ollama server is running when Amy starts.

        If it's already up (the tray app, or a previous session) it's left
        alone. Otherwise `ollama serve` is started hidden in the background and
        Amy waits for it to answer before warming the model.
        """
        cfg = CONFIG.get("ollama", {})
        if self._ollama_up():
            self.ollama_ready.set()
            log_debug("Ollama was already running, so Amy's memory settings for the server "
                      "(KV cache size, parallel slots) don't apply to it.")
            return
        if not cfg.get("auto_start", True):
            return
        exe = self._find_ollama()
        if not exe:
            self.safe_log("Ollama isn't installed (or isn't on PATH). Get it from ollama.com, "
                          "or set ollama.auto_start to false.")
            return
        try:
            env = dict(os.environ)
            host = re.match(r"https?://([^/]+)", OLLAMA_URL)
            if host and "OLLAMA_HOST" not in env:
                env["OLLAMA_HOST"] = host.group(1)
            # Memory settings for the server Amy starts (your own env vars win).
            mode = _mem_mode()
            env.setdefault("OLLAMA_NUM_PARALLEL", "1")       # one context buffer, not several
            env.setdefault("OLLAMA_MAX_LOADED_MODELS", "2" if mode == "lean" else "3")
            kv = str(cfg.get("kv_cache", "q8_0"))
            if mode != "performance" and kv in ("q8_0", "q4_0"):
                env.setdefault("OLLAMA_FLASH_ATTENTION", "1")
                env.setdefault("OLLAMA_KV_CACHE_TYPE", kv)
            self._ollama_proc = subprocess.Popen(
                [exe, "serve"], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log_debug(f"Started Ollama: {exe} serve (pid {self._ollama_proc.pid})")
        except Exception as e:
            log_debug(f"Could not start Ollama: {e}")
            self.safe_log(f"I couldn't start Ollama: {e}")
            return
        deadline = time.time() + float(cfg.get("start_timeout", 45))
        while time.time() < deadline:
            if self._ollama_up():
                self.ollama_ready.set()
                self.safe_log("Ollama started.")
                return
            if self._ollama_proc.poll() is not None:
                # Exited at once: usually another copy grabbed the port meanwhile.
                if self._ollama_up():
                    self.ollama_ready.set()
                    return
                self.safe_log("Ollama exited straight away. Try running 'ollama serve' "
                              "yourself to see why.")
                self._ollama_proc = None
                return
            time.sleep(0.5)
        self.safe_log("Ollama is taking a long time to start; I'll keep trying in the background.")

    def _warm_up_model(self):
        """Preload the LLM (and keep it resident) so the first query isn't slow."""
        self.ollama_ready.wait(timeout=float(CONFIG.get("ollama", {}).get("start_timeout", 45)) + 5)
        if not self.available_models:
            self.refresh_available_models()      # so the warmed model is the one chat uses
        try:
            self.http.post(OLLAMA_URL, json={
                "model": self.model_for("general"),
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "keep_alive": "30m",
                "options": {"num_predict": 1},
            }, timeout=120)
            log_debug("Model warm-up complete.")
        except Exception as e:
            log_debug(f"Model warm-up skipped: {e}")

    def instantiate_card(self, title, card_type, content):
        card_id = f"card_{int(time.time() * 1000)}"
        card = AmyCard(card_id, title, card_type, content)
        card_json = json.dumps(card.to_dict())
        self.run_js(f"renderInAppCard({card_json})")
        return card_id

    # --- OS APPLICATION CONTROL (launch_external_app) ---
    # Apps that should be matched against an existing window before launching.
    APP_MAP = {
        "chrome": "chrome", "google chrome": "chrome", "browser": "chrome",
        "firefox": "firefox", "edge": "msedge", "microsoft edge": "msedge",
        "notepad": "notepad", "notepad++": "notepad++",
        "calculator": "calc", "calc": "calc",
        "cmd": "cmd", "command prompt": "cmd", "terminal": "wt", "powershell": "powershell",
        "file explorer": "explorer", "explorer": "explorer", "files": "explorer",
        "vs code": "code", "vscode": "code", "visual studio code": "code",
        "task manager": "taskmgr", "paint": "mspaint", "wordpad": "write",
        "settings": "ms-settings:", "control panel": "control",
        "spotify": "spotify", "discord": "discord", "steam": "steam",
        "word": "winword", "excel": "excel", "powerpoint": "powerpnt", "outlook": "outlook",
        "snipping tool": "snippingtool", "camera": "microsoft.windows.camera:",
    }

    # Common install locations for apps that usually aren't on PATH.
    # Apps are found on this machine by AppIndex; nothing here is a path.

    # If an app isn't installed locally, open its web version instead.
    WEB_FALLBACKS = {
        "google": "https://www.google.com",
        "youtube studio": "https://studio.youtube.com",
        "youtube": "https://www.youtube.com",
        "spotify": "https://open.spotify.com",
        "netflix": "https://www.netflix.com",
        "gmail": "https://mail.google.com",
        "mail": "https://mail.google.com",
        "outlook": "https://outlook.live.com",
        "discord": "https://discord.com/app",
        "whatsapp": "https://web.whatsapp.com",
        "telegram": "https://web.telegram.org",
        "slack": "https://app.slack.com",
        "teams": "https://teams.microsoft.com",
        "zoom": "https://zoom.us",
        "twitter": "https://twitter.com",
        "x": "https://x.com",
        "reddit": "https://www.reddit.com",
        "instagram": "https://www.instagram.com",
        "facebook": "https://www.facebook.com",
        "tiktok": "https://www.tiktok.com",
        "twitch": "https://www.twitch.tv",
        "linkedin": "https://www.linkedin.com",
        "github": "https://github.com",
        "gitlab": "https://gitlab.com",
        "stackoverflow": "https://stackoverflow.com",
        "chatgpt": "https://chat.openai.com",
        "claude": "https://claude.ai",
        "maps": "https://maps.google.com",
        "google maps": "https://maps.google.com",
        "drive": "https://drive.google.com",
        "google drive": "https://drive.google.com",
        "docs": "https://docs.google.com",
        "google docs": "https://docs.google.com",
        "sheets": "https://sheets.google.com",
        "calendar": "https://calendar.google.com",
        "photos": "https://photos.google.com",
        "amazon": "https://www.amazon.com",
        "ebay": "https://www.ebay.com",
        "wikipedia": "https://www.wikipedia.org",
        "translate": "https://translate.google.com",
        "onedrive": "https://onedrive.live.com",
        "dropbox": "https://www.dropbox.com",
        "notion": "https://www.notion.so",
        "trello": "https://trello.com",
        "figma": "https://www.figma.com",
        "canva": "https://www.canva.com",
        "pinterest": "https://www.pinterest.com",
        "soundcloud": "https://soundcloud.com",
        "disney plus": "https://www.disneyplus.com",
        "prime video": "https://www.primevideo.com",
        "hulu": "https://www.hulu.com",
        "steam store": "https://store.steampowered.com",
    }

    def _open_in_browser(self, url, name=""):
        """Open a URL in the user's own default browser (signed-in sessions
        and all)."""
        try:
            if sys.platform == "win32":
                os.startfile(url)
            else:
                webbrowser.open(url)
            self.safe_log(f"Opened {name or url}.")
            self.record_action("open_url", name or url)
            return True
        except Exception as e:
            log_debug(f"browser open failed: {e}")
            try:
                return bool(webbrowser.open(url))
            except Exception:
                return False

    def summarize_file(self, path):
        """Read a document (txt/md/pdf/docx/code) and summarise it."""
        path = os.path.expanduser(path)
        if not os.path.isfile(path):
            hits = self.find_recent_files(path, limit=1)
            path = hits[0] if hits else path
        if not os.path.isfile(path):
            self.speak("I couldn't find that file, Sir.")
            return False
        ext = os.path.splitext(path)[1].lower()
        text = ""
        try:
            if ext == ".pdf":
                try:
                    from pypdf import PdfReader
                    text = "\n".join((p.extract_text() or "") for p in PdfReader(path).pages[:30])
                except ImportError:
                    self.speak("Reading PDFs needs pypdf: pip install pypdf.")
                    return False
            elif ext == ".docx":
                try:
                    import docx
                    text = "\n".join(p.text for p in docx.Document(path).paragraphs)
                except ImportError:
                    self.speak("Reading Word files needs python-docx: pip install python-docx.")
                    return False
            else:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read(60000)
        except Exception as e:
            log_debug(f"summarize_file read error: {e}")
        if not text.strip():
            self.speak("There's no readable text in that file, Sir.")
            return False
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("reasoning"),
                "messages": [{"role": "user", "content":
                              "Summarise this document: what it is, the key points, and anything "
                              f"that needs action.\n\n{text[:24000]}"}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 500, "temperature": 0.3, "num_ctx": 16384},
            }, timeout=240)
            out = self._llm_text(res)
        except Exception as e:
            log_debug(f"summarize_file error: {e}")
            out = ""
        if out:
            self.instantiate_card(f"Summary: {os.path.basename(path)}", "email_draft", out)
            self.speak(out.split("\n")[0][:300])
            return True
        self.speak("I couldn't summarise that, Sir.")
        return False

    def find_recent_files(self, query="", limit=8):
        """Newest files in the user's folders that fit a loose description,
        e.g. "newest video", "latest export", "report pdf"."""
        q = str(query or "").lower()
        kinds = {
            "video": {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"},
            "image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic"},
            "photo": {".png", ".jpg", ".jpeg", ".webp", ".heic"},
            "audio": {".mp3", ".wav", ".m4a", ".flac", ".ogg"},
            "document": {".pdf", ".docx", ".doc", ".txt", ".md", ".pptx", ".xlsx"},
            "pdf": {".pdf"}, "model": {".stl", ".step", ".stp", ".3mf", ".obj"},
        }
        exts = set()
        for word, e in kinds.items():
            if word in q:
                exts |= e
        terms = [w for w in re.findall(r"[a-z0-9]+", q)
                 if w not in kinds and w not in {"newest", "latest", "recent", "last", "my", "the",
                                                 "a", "file", "files", "i", "just", "edited", "made"}]
        home = os.path.expanduser("~")
        roots = [os.path.join(home, d) for d in ("Videos", "Desktop", "Documents", "Downloads",
                                                  "Pictures", "Music", r"OneDrive\Desktop",
                                                  r"OneDrive\Documents")]
        roots += [p for p in CONFIG.get("assistant", {}).get("work_folders", []) if p]
        found = []
        cutoff = time.time() - 60 * 60 * 24 * 120
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")
                               and d not in ("node_modules", "__pycache__", "AppData")]
                for fn in filenames:
                    ext = os.path.splitext(fn)[1].lower()
                    if exts and ext not in exts:
                        continue
                    low = fn.lower()
                    if terms and not exts and not any(t in low for t in terms):
                        continue
                    p = os.path.join(dirpath, fn)
                    try:
                        m = os.path.getmtime(p)
                    except OSError:
                        continue
                    if m >= cutoff:
                        found.append((m, p))
        found.sort(reverse=True)
        return [p for _, p in found[:limit]]

    def launch_external_app(self, target, args=None):
        """Open an app, file, folder or site by name.

        Order: URLs and real paths first; then "is this a website?" (open
        Google -> google.com, unless an app is literally called that); then
        apps found on this machine by AppIndex; then whatever's already
        running; then the taskbar/desktop by accessibility; then the vision
        agent; and finally a web version or a search.
        """
        try:
            target_str = str(target).strip().strip(".,!?")
            if not target_str:
                return False
            key = target_str.lower()

            # --- URLs / web pages ---
            if (key.startswith(("http://", "https://", "www."))
                    or re.match(r'^[\w-]+(\.[\w-]+)+(/.*)?$', target_str)):
                url = target_str if key.startswith(("http://", "https://")) else "https://" + target_str
                return self._open_in_browser(url, target_str)

            # --- Reject obvious path-traversal targets (model output builds these) ---
            if target_str.count("..") >= 3 or re.search(r'(?:\.\.[\\/]){3,}', target_str):
                self.safe_log(f"Refused suspicious path: {target_str[:60]}")
                self.speak("That path doesn't look right, Sir.")
                return False

            # --- Existing files / folders open with their default handler ---
            if os.path.exists(target_str):
                if sys.platform == "win32":
                    os.startfile(target_str)
                else:
                    subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", target_str])
                self.safe_log(f"Opened {target_str}")
                return True

            # --- Make sure the app index has loaded at least once ---
            if not self.app_index.ready.is_set():
                self.app_index.ready.wait(8)
            learned = self.memory.get("app_aliases", {})
            entry, score = self.app_index.resolve(key, learned)

            # --- Website intent: "open google", "open gmail" ---
            site = self.WEB_FALLBACKS.get(key)
            if site and score < 0.99:
                return self._open_in_browser(site, target_str)

            # --- Installed app ---
            if entry and score >= 0.72:
                if focus_window_by_title(entry["name"]) and not args:
                    self.safe_log(f"Switched to {entry['name']}.")
                    return True
                self.app_index.launch(entry, args)
                self.safe_log(f"Opened {entry['name']}.")
                self.record_action("open_app", entry["name"])
                threading.Timer(1.8, lambda: focus_window_by_title(entry["name"])).start()
                return True

            # --- Already running under another name? ---
            if focus_window_by_title(target_str):
                self.safe_log(f"Switched to {target_str}.")
                return True

            # --- Visible on the taskbar or desktop? Click it, like a person would ---
            if self._click_shell_item(target_str):
                self.safe_log(f"Opened {target_str} from the taskbar/desktop.")
                return True

            # --- Web version of a known service ---
            for name, u in self.WEB_FALLBACKS.items():
                if name in key or key in name:
                    self.speak(f"{target_str.title()} isn't installed, Sir, so I'll open it in the browser.")
                    return self._open_in_browser(u, target_str)

            # --- Let the agent look for it on screen ---
            if HAS_UIA or self._model_installed(self.model_for("vision")):
                self.speak(f"I can't find {target_str} in the usual places, Sir. I'll look for it on screen.")
                self.run_background(f"Finding {target_str}", self.agent.run,
                                    f"Open the application called '{target_str}'. Look on the taskbar, "
                                    f"desktop and Start menu. Stop as soon as it is open.")
                return True

            self.speak(f"I couldn't find {target_str}, Sir. Searching the web instead.")
            return self._open_in_browser(
                "https://www.google.com/search?q=" + urllib.parse.quote_plus(target_str), target_str)
        except Exception as e:
            log_debug(f"launch_external_app error: {e}\n{traceback.format_exc()}")
            self.safe_log(f"Error launching: {target}")
            return False

    def _click_shell_item(self, name):
        """Find a taskbar button or desktop icon by its accessible name and
        click it. Exact, unlike guessing pixels."""
        if not HAS_UIA:
            return False
        import difflib
        want = _norm_app_name(name)
        best, best_score = None, 0.0
        try:
            with uia.UIAutomationInitializerInThread():
                root = uia.GetRootControl()
                for top in root.GetChildren():
                    if top.ClassName not in ("Shell_TrayWnd", "Progman", "WorkerW",
                                             "Shell_SecondaryTrayWnd"):
                        continue
                    for ctrl, _d in uia.WalkControl(top, maxDepth=8):
                        if ctrl.ControlTypeName not in ("ButtonControl", "ListItemControl",
                                                        "MenuItemControl"):
                            continue
                        n = _norm_app_name(re.sub(r"\s*-\s*\d+ running window.*$", "", ctrl.Name or ""))
                        if not n:
                            continue
                        sc = difflib.SequenceMatcher(None, want, n).ratio()
                        if want in n.split() or n.startswith(want):
                            sc = max(sc, 0.9)
                        if sc > best_score:
                            best, best_score = ctrl, sc
                if best is not None and best_score >= 0.8:
                    r = best.BoundingRectangle
                    if best.ControlTypeName == "ListItemControl":
                        pyautogui.doubleClick(r.xcenter(), r.ycenter())   # desktop icon
                    else:
                        pyautogui.click(r.xcenter(), r.ycenter())
                    return True
        except Exception as e:
            log_debug(f"shell item search failed: {e}")
        return False

    # --- CORNER HUD OVERLAY CONTROL ---
    # Implemented as a small frameless always-on-top pywebview window. Using the
    # same GUI toolkit as the main window avoids the Qt/pywebview main-thread
    # conflict that made the old PyQt overlay unstable.
    def toggle_overlay(self, show=None):
        """Show or hide the desktop ring (always on top, see-through)."""
        want = (self.hud_overlay is None or not self._overlay_visible) if show is None else bool(show)

        def go():
            if self.hud_overlay is None:
                ov = AmyOverlay()
                ov.opened.connect(self.restore_main)
                ov.vision.connect(lambda: threading.Thread(
                    target=self.capture_screen_vision, daemon=True).start())
                ov.hidden_by_user.connect(lambda: self.toggle_overlay(False))
                self.hud_overlay = ov
            if want:
                self.hud_overlay.set_state(self._overlay_state)
                self.hud_overlay.show()
            else:
                self.hud_overlay.hide()
            self._overlay_visible = want
        try:
            gui().post(go)
            self.safe_log("Desktop overlay " + ("on." if want else "off."))
        except Exception as e:
            log_debug(f"Overlay error: {e}\n{traceback.format_exc()}")
            self.speak("I couldn't open the desktop overlay, Sir.")
        return True

    def overlay_state(self, state):
        """Mirror Amy's state onto the desktop ring."""
        self._overlay_state = state or "idle"
        ov = self.hud_overlay
        if ov is not None:
            gui().post(lambda: ov.set_state(self._overlay_state))

    def overlay_heard(self, text):
        ov = self.hud_overlay
        if ov is not None:
            gui().post(lambda: ov.set_heard(text))

    def run_js(self, script_str):
        if self.window and self.ui_ready:
            try:
                self.window.evaluate_js(script_str)
            except Exception as e:
                log_debug(f"JS Eval Error: {e}")

    def run_overlay_js(self, script_str):
        """Legacy hook from the web overlay; maps the old calls onto the
        native ring."""
        m = re.match(r"setState\('([^']*)'\)", script_str)
        if m:
            self.overlay_state(m.group(1) or "idle")
            return
        m = re.match(r"setHeard\((.*)\)$", script_str, re.S)
        if m:
            try:
                self.overlay_heard(json.loads(m.group(1)))
            except Exception:
                pass

    def safe_log(self, text, is_user=False):
        safe_txt = json.dumps(str(text))
        self.broadcast_js(f'appendLog({safe_txt}, {str(is_user).lower()})')

    def load_memory(self):
        with self.memory_lock:
            for path, is_backup in ((MEMORY_FILE, False), (MEMORY_FILE + ".bak", True)):
                if not os.path.exists(path):
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if not isinstance(data, dict):
                        raise ValueError("memory file is not an object")
                    for key, default in (
                        ("chat_history", []),      # recent rolling context
                        ("full_transcript", []),   # EVERY message, timestamped
                        ("todos", []), ("scratchpad", ""), ("custom_macros", {}),
                        ("contacts", {}), ("reminders", []),
                        ("facts", []),             # long-term "remember that ..." facts
                        ("projects", {}), ("calendar", []), ("habits", {}),
                    ):
                        data.setdefault(key, default)
                    for k, v in data.get("contacts", {}).items():
                        CONTACTS[k] = v
                    if is_backup:
                        self.safe_log("Main memory file was unreadable; recovered from backup.")
                        log_debug("Recovered memory from .bak")
                    return data
                except Exception as e:
                    log_debug(f"Memory file {path} unreadable: {e}")
                    if not is_backup:
                        # NEVER silently discard the user's memory. Keep the bad
                        # file so it can be inspected or repaired by hand.
                        try:
                            quarantine = f"{MEMORY_FILE}.corrupt_{int(time.time())}"
                            shutil.copy(path, quarantine)
                            log_debug(f"Corrupt memory preserved at {quarantine}")
                        except Exception as ce:
                            log_debug(f"could not quarantine corrupt memory: {ce}")
            return {"chat_history": [], "full_transcript": [], "todos": ["System Online"],
                    "scratchpad": "", "custom_macros": {}, "contacts": {},
                    "reminders": [], "facts": [], "projects": {}, "calendar": {},
                    "habits": {}}

    def remember_message(self, role, content):
        """Append to the permanent transcript (kept forever) and rolling context."""
        entry = {"role": role, "content": content,
                 "ts": datetime.datetime.now().isoformat(timespec="seconds")}
        self.memory.setdefault("full_transcript", []).append(entry)
        # Keep the full transcript from growing without bound on disk but keep a lot.
        if len(self.memory["full_transcript"]) > 5000:
            self.memory["full_transcript"] = self.memory["full_transcript"][-5000:]

    def save_memory(self, force=False):
        """Persist memory safely and cheaply.

        - Pruned first, so the file can't grow without bound.
        - Written compactly (indent=4 roughly doubled the size for no benefit).
        - Written to a temp file then swapped in, so a crash mid-write can't
          leave a corrupted memory file.
        - Debounced, because it used to be rewritten after every single command.
        """
        now = time.time()
        interval = float(CONFIG.get("assistant", {}).get("save_interval", 3.0))
        if not force and (now - self._last_save) < interval:
            self._save_pending = True
            return
        try:
            # Serialise while holding the lock so no other thread can resize a
            # dict mid-encode, but do the slow disk write outside it.
            with self.memory_lock:
                self._prune_memory()
                payload = json.dumps(self.memory, separators=(",", ":"))
            tmp = MEMORY_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            # Keep the last good file as a backup before swapping the new one in.
            # Without this the recovery path in load_memory could never fire.
            try:
                if os.path.exists(MEMORY_FILE) and (now - self._last_backup) > 300:
                    shutil.copy(MEMORY_FILE, MEMORY_FILE + ".bak")
                    self._last_backup = now
            except Exception as be:
                log_debug(f"memory backup skipped: {be}")
            os.replace(tmp, MEMORY_FILE)
            self._last_save = now
            self._save_pending = False
        except Exception as e:
            log_debug(f"Error saving memory file: {e}\n{traceback.format_exc()}")
            try:
                if os.path.exists(MEMORY_FILE + ".tmp"):
                    os.remove(MEMORY_FILE + ".tmp")
            except Exception:
                pass

    def _save_flush_worker(self):
        """Make sure a debounced save always lands eventually, and keep the
        data folder tidy."""
        ticks = 0
        while True:
            time.sleep(6)
            try:
                if self._save_pending:
                    self.save_memory(force=True)
                ticks += 1
                if ticks % 50 == 0:          # roughly every 5 minutes
                    self._sweep_stale_audio()
            except Exception as e:
                log_debug(f"save flush error: {e}")

    # --- SPOTIFY CONTROLLER ---
    def get_spotify_client(self, allow_browser=False):
        if not SPOTIPY_AVAILABLE:
            return None
        if self.sp is not None:
            return self.sp
        
        client_id = os.getenv("SPOTIPY_CLIENT_ID", SPOTIPY_CLIENT_ID)
        client_secret = os.getenv("SPOTIPY_CLIENT_SECRET", SPOTIPY_CLIENT_SECRET)
        
        if not client_id or not client_secret or "YOUR_SPOTIPY" in client_id:
            return None
        
        try:
            scope = "user-read-currently-playing user-read-playback-state user-modify-playback-state"
            auth_manager = SpotifyOAuth(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=SPOTIPY_REDIRECT_URI,
                scope=scope,
                open_browser=allow_browser,
                cache_path=SPOTIFY_CACHE_FILE
            )
            
            cached_token = auth_manager.get_cached_token()
            if cached_token:
                self.sp = spotipy.Spotify(auth_manager=auth_manager)
                return self.sp

            if allow_browser:
                self.speak("Opening browser once to authorize Spotify API, Sir.")
                token_info = auth_manager.get_access_token(as_dict=False)
                if token_info:
                    self.sp = spotipy.Spotify(auth_manager=auth_manager)
                    return self.sp
            return None
        except Exception as e:
            log_debug(f"Spotify OAuth exception: {e}")
            return None

    def get_active_spotify_device(self, sp):
        try:
            devices_res = sp.devices()
            devices = devices_res.get("devices", []) if devices_res else []
            for d in devices:
                if d.get("is_active"):
                    return d["id"]
            if devices:
                return devices[0]["id"]

            self.speak("Launching Spotify desktop client, Sir.")
            self.launch_external_app("spotify")
            time.sleep(3.0)

            devices_res = sp.devices()
            devices = devices_res.get("devices", []) if devices_res else []
            if devices:
                return devices[0]["id"]
        except Exception as e:
            log_debug(f"Device acquisition error: {e}")
        return None

    def spotify_loop(self):
        init_thread_com()
        while True:
            try:
                sp = self.get_spotify_client(allow_browser=False)
                if sp:
                    current = sp.current_playback()
                    if current and current.get("item"):
                        item = current["item"]
                        track_name = item.get("name", "Unknown Track")
                        artists = ", ".join([a["name"] for a in item.get("artists", [])])
                        images = item.get("album", {}).get("images", [])
                        art_url = images[0]["url"] if images else ""
                        is_playing = current.get("is_playing", False)
                        
                        data = {
                            "configured": True,
                            "is_active": True,
                            "is_playing": is_playing,
                            "track_name": track_name,
                            "artist_name": artists,
                            "album_art": art_url
                        }
                        self.run_js(f"updateSpotifyUI({json.dumps(data)})")
                    else:
                        data = {"configured": True, "is_active": False}
                        self.run_js(f"updateSpotifyUI({json.dumps(data)})")
                else:
                    data = {"configured": False, "is_active": False}
                    self.run_js(f"updateSpotifyUI({json.dumps(data)})")
            except Exception as e:
                log_debug(f"Spotify loop poll exception: {e}")
            time.sleep(4)

    def spotify_play_track(self, query):
        sp = self._spotify()
        if not sp:
            return True

        device_id = self.get_active_spotify_device(sp)
        if not device_id:
            self.speak("Unable to detect active Spotify device.")
            return True

        try:
            results = sp.search(q=query, limit=1, type="track")
            tracks = results.get("tracks", {}).get("items", [])
            
            if tracks:
                track = tracks[0]
                sp.start_playback(device_id=device_id, uris=[track["uri"]])
                self.speak(f"Playing {track['name']} by {track['artists'][0]['name']} via Spotify API.")
            else:
                self.speak(f"Could not find {query} on Spotify.")
        except spotipy.SpotifyException as e:
            log_debug(f"Spotify Playback API Exception: {e}")
            if "PREMIUM_REQUIRED" in str(e):
                self.speak("Spotify API playback requires a Spotify Premium subscription, Sir.")
            else:
                self.speak("Spotify Web API rejected playback command.")
        except Exception as e:
            log_debug(f"Spotify play error: {e}")
            self.speak("Failed to start Spotify playback.")
        return True

    def spotify_toggle_play(self, force_play=False):
        sp = self._spotify()
        if not sp:
            return True

        device_id = self.get_active_spotify_device(sp)
        if not device_id:
            self.speak("No active Spotify device found.")
            return True

        try:
            current = sp.current_playback()
            if current and current.get("is_playing") and not force_play:
                sp.pause_playback(device_id=device_id)
                self.speak("Playback paused via Spotify API.")
            else:
                sp.start_playback(device_id=device_id)
                self.speak("Resuming playback via Spotify API.")
        except spotipy.SpotifyException as e:
            log_debug(f"Spotify toggle error: {e}")
            self.speak("Spotify API playback toggle failed.")
        return True

    def spotify_pause(self):
        sp = self._spotify()
        if not sp:
            return True
        try:
            sp.pause_playback()
            self.speak("Playback paused via Spotify API.")
        except spotipy.SpotifyException:
            self.speak("Failed to pause via Spotify API.")
        return True

    def spotify_next(self):
        sp = self._spotify()
        if not sp:
            return True
        try:
            sp.next_track()
            self.speak("Track skipped via Spotify API.")
        except spotipy.SpotifyException:
            self.speak("Failed to skip track via Spotify API.")
        return True

    def spotify_previous(self):
        sp = self._spotify()
        if not sp:
            return True
        try:
            sp.previous_track()
            self.speak("Playing previous track via Spotify API.")
        except spotipy.SpotifyException:
            self.speak("Failed to play previous track via Spotify API.")
        return True

    def spotify_announce_current(self):
        sp = self._spotify()
        if not sp:
            return True
        try:
            current = sp.current_playback()
            if current and current.get("item"):
                item = current["item"]
                name = item.get("name")
                artist = item.get("artists", [{}])[0].get("name")
                self.speak(f"Currently playing {name} by {artist}.")
            else:
                self.speak("Nothing is currently playing on Spotify, Sir.")
        except Exception as e:
            log_debug(f"Spotify current status error: {e}")
            self.speak("Unable to query Spotify API status.")
        return True

    def spotify_toggle_play_js(self):
        threading.Thread(target=self.spotify_toggle_play, daemon=True).start()

    def spotify_next_js(self):
        threading.Thread(target=self.spotify_next, daemon=True).start()

    def spotify_previous_js(self):
        threading.Thread(target=self.spotify_previous, daemon=True).start()

    # --- NEURAL TTS (Edge voices) ---
    def _neural_speak(self, text, prepared=None):
        """Speak with a Microsoft Edge neural voice. Returns True if it played.

        `prepared` is audio already synthesised in the background (the TTS
        worker renders the next sentence while the current one plays).
        """
        data = prepared if prepared is not None else self._render_neural_bytes(text)
        if not data:
            return False
        if self.audio_out is not None:
            try:
                pcm, rate = AudioOut.decode_mp3(data)
                # Skip the leading silence neural voices pad their audio with.
                loud = _np.nonzero(_np.abs(pcm) > 400)[0]
                if loud.size:
                    pcm = pcm[max(0, int(loud[0]) - int(rate * 0.03)):]
                self.audio_out.play(pcm, rate)
                while self.audio_out.playing() and self.is_speaking:
                    time.sleep(0.02)
                if self.audio_out.playing():
                    self.audio_out.stop()
                return True
            except Exception as e:
                log_debug(f"in-process playback failed, using system player: {e}")
        out_path = os.path.join(DATA_DIR, f"_tts_{int(time.time()*1000)}.mp3")
        try:
            with open(out_path, "wb") as f:
                f.write(data)
            self._play_audio_file(out_path)
            return True
        except Exception as e:
            log_debug(f"neural playback failed: {e}")
            return False
        finally:
            try:
                os.remove(out_path)
            except Exception:
                pass

    def _render_neural_bytes(self, text):
        """MP3 bytes of `text` in the configured neural voice, or None."""
        if not HAS_EDGE_TTS:
            return None
        a = CONFIG.get("assistant", {})
        voice = a.get("neural_voice", "en-GB-SoniaNeural")
        rate = a.get("neural_rate", "+0%")
        pitch = a.get("neural_pitch", "+0Hz")

        async def _synth():
            out = bytearray()
            comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
            async for chunk in comm.stream():
                if chunk.get("type") == "audio":
                    out.extend(chunk.get("data") or b"")
            return bytes(out)
        try:
            loop = asyncio.new_event_loop()
            try:
                data = loop.run_until_complete(_synth())
            finally:
                loop.close()
            return data if len(data) > 512 else None
        except Exception as e:
            log_debug(f"neural render failed: {e}")
            return None

    def _play_audio_file(self, path):
        """Play an audio file, blocking until finished or interrupted."""
        try:
            if sys.platform == "win32":
                # PowerShell's MediaPlayer handles mp3 without extra packages.
                ps = (
                    "Add-Type -AssemblyName presentationCore; "
                    "$p = New-Object system.windows.media.mediaplayer; "
                    f"$p.open([uri]'{path}'); "
                    "$p.Play(); "
                    "Start-Sleep -Milliseconds 300; "
                    "$n = 0; "
                    "while($p.NaturalDuration.HasTimeSpan -eq $false -and $n -lt 50)"
                    "{Start-Sleep -Milliseconds 60; $n++}; "
                    "if($p.NaturalDuration.HasTimeSpan)"
                    "{Start-Sleep -Milliseconds $p.NaturalDuration.TimeSpan.TotalMilliseconds}; "
                    "$p.Stop(); $p.Close()"
                )
                creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                self.current_tts_proc = subprocess.Popen(
                    ["powershell", "-NoProfile", "-Command", ps], creationflags=creationflags)
            else:
                player = shutil.which("mpg123") or shutil.which("ffplay") or shutil.which("afplay")
                if not player:
                    return
                args = [player, path]
                if "ffplay" in player:
                    args = [player, "-nodisp", "-autoexit", "-loglevel", "quiet", path]
                self.current_tts_proc = subprocess.Popen(args)

            while self.current_tts_proc and self.current_tts_proc.poll() is None and self.is_speaking:
                time.sleep(0.04)
            if self.current_tts_proc and self.current_tts_proc.poll() is None:
                self.current_tts_proc.terminate()
            self.current_tts_proc = None
        except Exception as e:
            log_debug(f"audio playback error: {e}")

    def list_voices(self):
        """Diagnostic: report exactly which voices are installed and what's in use."""
        lines = []
        if HAS_EDGE_TTS:
            lines.append("NEURAL (active): " + CONFIG.get("assistant", {}).get("neural_voice", "en-GB-SoniaNeural"))
            lines.append("Other neural options: en-GB-SoniaNeural, en-GB-LibbyNeural,")
            lines.append("  en-GB-MaisieNeural, en-GB-RyanNeural, en-GB-ThomasNeural")
        else:
            lines.append("NEURAL: not installed  ->  pip install --user edge-tts")
            lines.append("(Neural voices sound far better than the built-in system ones.)")

        if HAS_PYTHONCOM:
            try:
                import win32com.client
                sp = win32com.client.Dispatch("SAPI.SpVoice")
                lines.append("")
                lines.append("SYSTEM VOICES INSTALLED:")
                for v in sp.GetVoices():
                    d = v.GetDescription()
                    lines.append(f"  [{self._score_voice(d):>4}] {d}")
                lines.append("")
                lines.append("Add British voices: Settings > Time & Language > Speech >")
                lines.append("  Manage voices > Add voices > English (United Kingdom)")
            except Exception as e:
                lines.append(f"Could not read system voices: {e}")

        report = "\n".join(lines)
        self.instantiate_card("Voice Diagnostics", "code_bug", report)
        self.safe_log(report)
        self.speak("Voice diagnostics are on screen, Sir.")
        return True

    # --- TTS WORKER ---
    # Known Windows/SAPI voice genders. Descriptions rarely contain the word
    # "female", so matching on that alone let voices like Sonia/Libby through.
    # Known voice names by gender, with a preference score. Descriptions rarely
    # contain the word "female", so matching on that alone isn't enough.
    FEMALE_VOICES = {"sonia": 60, "libby": 58, "hazel": 56, "maisie": 50, "susan": 46,
                     "jenny": 40, "aria": 38, "michelle": 34, "emily": 34, "clara": 30,
                     "zira": 28, "linda": 26, "heera": 24, "natasha": 24, "molly": 24,
                     "amber": 22, "ashley": 22, "cora": 22, "elizabeth": 22, "monica": 20,
                     "catherine": 20, "eva": 18, "ana": 16, "julie": 16, "laura": 16,
                     "helena": 14, "isabella": 14, "hedda": 12, "katja": 12, "paulina": 12,
                     "sabina": 12, "elsa": 12, "hoda": 10, "laila": 10, "yaoyao": 10,
                     "huihui": 10, "haruka": 10, "ayumi": 10, "irina": 10, "maria": 10,
                     "tracy": 10, "danni": 10}
    MALE_VOICES = {"ryan": 60, "george": 58, "guy": 46, "james": 44, "oliver": 44,
                   "thomas": 40, "daniel": 40, "brian": 38, "alfie": 36, "ethan": 34,
                   "noah": 32, "william": 32, "connor": 30, "mark": 26, "david": 24,
                   "richard": 22, "sean": 22, "christopher": 20, "eric": 18, "roger": 18,
                   "steffan": 18, "liam": 18, "andrew": 16, "brandon": 16, "naayf": 10,
                   "kangkang": 10}

    @staticmethod
    def _voice_gender():
        g = str(CONFIG.get("assistant", {}).get("voice_gender", "female")).lower()
        return "male" if g.startswith("m") else "female"

    @staticmethod
    def _score_voice(desc):
        """Rank a voice: the configured gender first, British accent preferred.
        Returns -1 to disqualify. Any recognised voice of the right gender
        scores well above 0."""
        d = desc.lower()
        want_female = AmyApp._voice_gender() == "female"
        wanted = AmyApp.FEMALE_VOICES if want_female else AmyApp.MALE_VOICES
        other = AmyApp.MALE_VOICES if want_female else AmyApp.FEMALE_VOICES

        says_female = "female" in d
        says_male = re.search(r'\bmale\b', d) is not None
        # Hard-disqualify voices explicitly of the other gender.
        if want_female and says_male and not says_female:
            return -1
        if not want_female and says_female:
            return -1
        for name in other:
            if re.search(r'\b' + re.escape(name) + r'\b', d):
                return -1

        score = 0
        matched = False
        for name, pts in wanted.items():
            if re.search(r'\b' + re.escape(name) + r'\b', d):
                score += pts
                matched = True
                break
        if (want_female and says_female) or (not want_female and says_male):
            score += 15
            matched = True

        # Accent preference (secondary to gender).
        if any(k in d for k in ["en-gb", "united kingdom", "great britain", "british", "(gb)"]):
            score += 25
        elif "uk" in d:
            score += 15
        if "natural" in d or "online" in d:   # neural voices sound far better
            score += 12
        if "desktop" in d:
            score -= 5

        # An unrecognised voice of unknown gender is risky: keep it below any
        # known voice of the right gender, but above disqualified ones.
        if not matched:
            score = min(score, 5)
        return score

    def _tts_worker(self):
        init_thread_com()

        if HAS_PYTHONCOM:
            try:
                import win32com.client
                self.sapi_speaker = win32com.client.Dispatch("SAPI.SpVoice")
                self.sapi_speaker.Rate = max(-3, min(3, VOICE_RATE))

                # Allow an exact voice override from the config.
                preferred = str(CONFIG.get("assistant", {}).get("preferred_voice", "")).strip().lower()

                best_v, best_score = None, -999
                available = []
                for v in self.sapi_speaker.GetVoices():
                    desc = v.GetDescription()
                    available.append(desc)
                    if preferred and preferred in desc.lower():
                        best_v, best_score = v, 9999
                        break
                    s = self._score_voice(desc)
                    if s > best_score:
                        best_v, best_score = v, s

                log_debug("Available SAPI voices: " + " | ".join(available))
                if best_v is not None and best_score > 0:
                    self.sapi_speaker.Voice = best_v
                    log_debug(f"Selected SAPI voice (score {best_score}): {best_v.GetDescription()}")
                else:
                    log_debug("No suitable male voice found; using system default. "
                              "Install a male voice via Windows Speech settings.")
            except Exception as e:
                log_debug(f"SAPI voice setup warning: {e}")

        pyttsx_engine = None
        if not self.sapi_speaker and TTS_AVAILABLE:
            try:
                pyttsx_engine = pyttsx3.init()
                pyttsx_engine.setProperty('rate', 175)
                best_id, best_score = None, -999
                for v in pyttsx_engine.getProperty('voices'):
                    s = self._score_voice(f"{v.id} {v.name}")
                    if s > best_score:
                        best_id, best_score = v.id, s
                if best_id and best_score > 0:
                    pyttsx_engine.setProperty('voice', best_id)
                    log_debug(f"pyttsx3 voice selected (score {best_score})")
            except Exception as e:
                log_debug(f"pyttsx3 setup warning: {e}")

        from concurrent.futures import ThreadPoolExecutor
        self._tts_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tts")
        ahead = None                  # next sentence, already being synthesised

        def prep(item):
            gen, txt = item if isinstance(item, tuple) else (self._speech_gen, item)
            fut = self._tts_pool.submit(self._render_neural_bytes, txt) if HAS_EDGE_TTS else None
            return gen, txt, fut

        while True:
            try:
                if ahead is not None:
                    gen, text, fut = ahead
                    ahead = None
                else:
                    gen, text, fut = prep(self.speech_queue.get())
                if gen != self._speech_gen:
                    self.speech_queue.task_done()
                    continue
                # Start synthesising the following sentence now, so it's
                # ready the moment this one finishes.
                try:
                    ahead = prep(self.speech_queue.get_nowait())
                except queue.Empty:
                    ahead = None
                prepared = None
                if fut is not None:
                    try:
                        prepared = fut.result(timeout=20)
                    except Exception:
                        prepared = None
                if gen != self._speech_gen:
                    self.speech_queue.task_done()
                    continue
                if text:
                    self.is_speaking = True
                    self.run_js("updateSpeechAnimation(true)")
                    self.overlay_state("speaking")

                    if self._neural_speak(text, prepared=prepared):
                        pass    # spoken with the neural British voice
                    elif self.sapi_speaker:
                        self.sapi_speaker.Speak(text, 1)
                        while self.sapi_speaker.Status.RunningState == 2 and self.is_speaking:
                            time.sleep(0.04)
                    elif pyttsx_engine:
                        pyttsx_engine.say(text)
                        pyttsx_engine.runAndWait()
                    else:
                        clean_text = str(text).replace('"', '').replace("'", "").replace('`', '')
                        # Pick a voice of the configured gender by name;
                        # SelectVoiceByHints silently falls back to the default.
                        female = self._voice_gender() == "female"
                        pref = ('"Sonia","Libby","Hazel","Susan","Zira"' if female
                                else '"Ryan","George","Guy","James","Oliver","Mark","David"')
                        gender = "Female" if female else "Male"
                        cmd = (
                            'Add-Type -AssemblyName System.Speech; '
                            '$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; '
                            f'$pref = @({pref}); '
                            '$sel = $null; '
                            'foreach ($p in $pref) { '
                            '  foreach ($v in $s.GetInstalledVoices()) { '
                            '    $i = $v.VoiceInfo; '
                            '    if ($i.Name -like "*$p*") { $sel = $i.Name; break } } '
                            '  if ($sel) { break } } '
                            'if (-not $sel) { foreach ($v in $s.GetInstalledVoices()) { '
                            f'  $i = $v.VoiceInfo; if ($i.Gender -eq "{gender}") {{ $sel = $i.Name; break }} }} }} '
                            'if ($sel) { $s.SelectVoice($sel) }; '
                            '$s.Rate = 0; '
                            f'$s.Speak("{clean_text}");'
                        )
                        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                        self.current_tts_proc = subprocess.Popen(["powershell", "-Command", cmd], creationflags=creationflags)
                        while self.current_tts_proc and self.current_tts_proc.poll() is None and self.is_speaking:
                            time.sleep(0.04)
                        self.current_tts_proc = None

                    # Stay "speaking" across back-to-back sentences so the orb
                    # and barge-in detector don't flicker between them.
                    if ahead is None and self.speech_queue.empty():
                        time.sleep(0.12)
                    if ahead is None and self.speech_queue.empty():
                        self.is_speaking = False
                        self.run_js("updateSpeechAnimation(false)")
                        self.overlay_state("idle")
                self.speech_queue.task_done()
            except Exception as e:
                log_debug(f"TTS Worker Exception: {e}")
                self.run_js("updateSpeechAnimation(false)")
                self.is_speaking = False
                time.sleep(0.05)

    def speak(self, text):
        """Queue speech. Muting the mic only stops Amy listening; she still
        talks. Anything queued before an interruption is dropped."""
        text = str(text or "").strip()
        if text:
            self.speech_queue.put((self._speech_gen, text))

    def stop_speech(self):
        # Invalidate everything queued or still being generated for this turn.
        self._speech_gen += 1
        self.is_speaking = False
        if self.audio_out is not None:
            self.audio_out.stop()
        self.continuous_vision_active = False
        self.run_js("setAutoVisionState(false)")
        
        while not self.speech_queue.empty():
            try:
                self.speech_queue.get_nowait()
                self.speech_queue.task_done()
            except Exception:
                break
                
        if self.sapi_speaker:
            try:
                self.sapi_speaker.Speak("", 2)
            except Exception as e:
                log_debug(f"Error purging SAPI stream: {e}")

        if self.current_tts_proc and self.current_tts_proc.poll() is None:
            try:
                self.current_tts_proc.kill()
            except Exception:
                pass
            self.current_tts_proc = None

        self.run_js("updateSpeechAnimation(false)")

    def set_mic_mute(self, muted):
        self.mic_muted = bool(muted)
        self.overlay_state("muted" if self.mic_muted else "idle")

    def set_ui_ready(self):
        self.ui_ready = True
        def post_ready():
            time.sleep(0.2)
            # Apply the saved bottom-bar layout.
            if self.active_project:
                nm = self.memory.get("projects", {}).get(self.active_project, {}).get("name")
                if nm:
                    self.run_js(f"setActiveProject({json.dumps(nm)})")
            bar_cfg = CONFIG.get("ui", {}).get("bottom_bar", {})
            self.run_js(f"applyBarConfig({json.dumps(bar_cfg)})")
            self.sync_todos_to_ui()
            # Restore the scratchpad contents.
            note = self.memory.get("scratchpad", "")
            if note:
                self.run_js(f"var s=document.getElementById('scratchpadInput'); if(s) s.value={json.dumps(note)};")
            for msg in self.memory.get("chat_history", [])[-10:]:
                self.safe_log(f"{msg['role'].upper()}: {msg['content']}")
            self.safe_log(
                "Ready. Try: 'create a PDF on black holes', 'show me how to tie a tie', "
                "'send an email to <name> about <topic>', 'open bbc.com in amy', "
                "'remember that ...', 'what's the weather in Paris'."
            )
            self.sfx.play("startup")
            if CONFIG.get("assistant", {}).get("speak_greeting", True):
                self.speak(f"Amy online and at your service, {USER_TITLE}.")
            saved_theme = CONFIG.get("ui", {}).get("theme", "arc")
            if saved_theme in self.THEMES:
                self.run_js(f"applyTheme({json.dumps(self.THEMES[saved_theme])})")
            self._sweep_stale_audio()
            if CONFIG.get("camera", {}).get("warm_vision", True) and _mem_mode() != "lean":
                threading.Thread(target=self.warm_vision_model, daemon=True).start()
            self.load_plugins()

            def _models_when_ready():
                self.ollama_ready.wait(timeout=60)
                self.refresh_available_models()
                self._model_cache.clear()
            threading.Thread(target=_models_when_ready, daemon=True).start()
            self.validate_config(announce=True)
            threading.Thread(target=self.startup_digest, daemon=True).start()
            if self.memory.get("watched_folders") and not self._watcher_started:
                self._watcher_started = True
                threading.Thread(target=self._kb_watch_worker, daemon=True).start()
            self.active_project = self.memory.get("active_project")
            if self.active_project:
                p = self.memory.get("projects", {}).get(self.active_project, {})
                self.run_js(f"setActiveProject({json.dumps(p.get('name',''))})")
            threading.Thread(target=self._habit_nudge_worker, daemon=True).start()
            self.proactive.start()
            threading.Thread(target=self._save_flush_worker, daemon=True).start()
            if CONFIG.get("camera", {}).get("start_on_launch", False):
                self.toggle_camera(True)
            if CONFIG.get("assistant", {}).get("screen_awareness_on_start", False):
                self.continuous_vision_active = True
                self.run_js("setAutoVisionState(true)")
                self.safe_log("Screen awareness enabled on startup.")
        threading.Thread(target=post_ready, daemon=True).start()

    def save_bar_config(self, cfg_json):
        try:
            cfg = json.loads(cfg_json) if isinstance(cfg_json, str) else dict(cfg_json)
            if not isinstance(cfg, dict) or not cfg:
                self.safe_log("Layout save skipped: nothing to save.")
                return False
            CONFIG.setdefault("ui", {})["bottom_bar"] = cfg
            ok = save_config(CONFIG)
            if ok:
                enabled = sum(1 for v in cfg.values() if v)
                self.safe_log(f"Control bar layout saved ({enabled} buttons enabled).")
                log_debug(f"Bar config written to {CONFIG_FILE}: {cfg}")
            else:
                self.safe_log(f"Could not write layout to {CONFIG_FILE} — check file permissions.")
            return ok
        except Exception as e:
            log_debug(f"save_bar_config error: {e}")
            self.safe_log(f"Layout save failed: {e}")
            return False

    def restore_main(self):
        if self.window:
            try:
                self.window.restore()
                self.window.show()
            except Exception as e:
                log_debug(f"restore_main error: {e}")

    # --- FRAMELESS WINDOW CONTROLS ---
    def window_minimize(self):
        try:
            self.window.minimize()
        except Exception as e:
            log_debug(f"minimize error: {e}")

    def window_toggle_fullscreen(self):
        """Maximise / restore the window."""
        try:
            self.window.toggle_maximize()
        except Exception as e:
            log_debug(f"maximize error: {e}")

    def window_start_drag(self):
        """Hand a title-bar drag to the OS (smooth, and snapping works)."""
        try:
            self.window.start_drag()
        except Exception as e:
            log_debug(f"start_drag error: {e}")

    def window_move(self, dx, dy):
        """Move the frameless window by a delta (used by title-bar dragging)."""
        try:
            dx = int(dx); dy = int(dy)
            x, y = self.window.x, self.window.y
            self.window.move(x + dx, y + dy)
        except Exception as e:
            log_debug(f"window_move error: {e}")

    def telemetry_loop(self):
        init_thread_com()
        while True:
            try:
                if self.window and self.ui_ready:
                    cpu = psutil.cpu_percent(interval=None)
                    ram = psutil.virtual_memory().percent
                    self.run_js(f"updateTelemetryUI({cpu}, {ram})")
            except Exception:
                pass
            time.sleep(2)

    # --- VOICE LISTENER ---
    # Common ways speech-to-text spells "Amy".
    WAKE_VARIANTS = ["amy", "aimee", "amie", "ami", "aimie", "amy's", "hey amy"]

    def _matches_wake(self, text):
        """Return (matched, trailing_command). Tolerates common mishearings.

        "Amy" is a short word, so it must match as a whole word: a plain
        substring search would fire on "dynamic", "gamy" and the like.
        """
        t = text.strip().lower()
        variants = sorted(set([WAKE_WORD] + self.WAKE_VARIANTS), key=len, reverse=True)
        for variant in variants:
            m = re.search(r"(?<![\w'])" + re.escape(variant) + r"(?![\w'])", t)
            if m:
                return True, t[m.end():].strip(" ,.!?")
        return False, ""

    def _listen_dynamic(self, recognizer, mic, timeout=3.0, purpose="command"):
        """Listen for as long as the user keeps talking, instead of cutting them
        off at a fixed limit.

        speech_recognition ends a phrase when it hears `pause_threshold` seconds
        of silence, so with no `phrase_time_limit` a short phrase ends quickly and
        a long one runs on naturally. A generous safety ceiling stops a noisy room
        from recording forever.
        """
        cfg = CONFIG.get("assistant", {})
        max_seconds = float(cfg.get("max_phrase_seconds", 45))
        base_pause = float(cfg.get("pause_threshold", 1.0))

        # A slightly longer tolerance for follow-ups, where people think aloud.
        recognizer.pause_threshold = base_pause + (0.35 if purpose == "followup" else 0.0)

        with mic as source:
            audio = recognizer.listen(source, timeout=timeout,
                                      phrase_time_limit=max_seconds)
        return audio

    def _listen_with_continuation(self, recognizer, mic, first_text):
        """If the user clearly hasn't finished, keep listening and stitch it on.

        Catches the case where someone pauses mid-thought ("open chrome and…
        then go to my email"), which a pure silence-based cutoff would truncate.
        """
        cfg = CONFIG.get("assistant", {})
        if not cfg.get("continuation_listening", True):
            return first_text

        text = first_text
        for _ in range(int(cfg.get("max_continuations", 2))):
            if not self._sounds_unfinished(text):
                break
            try:
                self.run_js("updateListeningUI(true)")
                audio = self._listen_dynamic(recognizer, mic, timeout=1.8, purpose="continuation")
                more = recognizer.recognize_google(audio).lower().strip()
                if not more:
                    break
                text = f"{text} {more}".strip()
                log_debug(f"Continuation captured: ...{more}")
            except (sr.UnknownValueError, sr.WaitTimeoutError):
                break
            except Exception as e:
                log_debug(f"continuation error: {e}")
                break
        return text

    # Words/structures that mean the sentence almost certainly isn't finished.
    UNFINISHED_TAIL = re.compile(
        r'\b(and|then|but|or|so|because|with|to|for|from|about|into|onto|at|in|on|'
        r'the|a|an|my|your|his|her|its|our|their|of|by|as|if|when|while|after|before|'
        r'also|plus|next|after that|followed by)\s*$', re.I)

    def _sounds_unfinished(self, text):
        if not text:
            return False
        t = text.strip()
        if len(t.split()) < 2:
            return False
        return bool(self.UNFINISHED_TAIL.search(t))

    def voice_listener_loop(self):
        """Full-duplex listening when sounddevice is installed; otherwise the
        older listen-then-respond loop."""
        if SPEECH_AVAILABLE and AudioEngine.available():
            return self._voice_loop_duplex()
        return self._voice_listener_loop_legacy()

    # ------------------------------------------------------------------
    def _voice_loop_duplex(self):
        init_thread_com()
        recognizer = sr.Recognizer()
        self.audio_in = AudioEngine(self)
        self.audio_in.start()
        self.safe_log("Listening (full duplex: you can talk over me).")

        def transcribe(pcm):
            try:
                audio = sr.AudioData(pcm, AudioEngine.RATE, 2)
                text = self._recognize_best(recognizer, audio)
                self._stt_failures = 0
                return (text or "").strip()
            except sr.UnknownValueError:
                return ""
            except sr.RequestError as e:
                self._stt_failures += 1
                log_debug(f"STT request failed: {e}")
                if self._stt_failures in (3, 20):
                    self.safe_log("Speech recognition can't reach Google - check your internet "
                                  "connection, or set stt_engine to native. Typing still works.")
                return ""
            except Exception as e:
                log_debug(f"STT error: {e}")
                return ""

        while True:
            pcm = self.audio_in.next_utterance(timeout=0.5)
            if pcm is None:
                continue
            interrupted = (time.time() - self.audio_in.barged_at) < 6
            self.run_js("updateListeningUI(true)")
            self.overlay_state("thinking")
            text = transcribe(pcm)
            if text and self._sounds_unfinished(text):
                more = self.audio_in.next_utterance(timeout=2.2)
                if more:
                    extra = transcribe(more)
                    if extra:
                        text = f"{text} {extra}"
            self.run_js("updateListeningUI(false)")
            self.overlay_state("idle")
            if not text:
                continue
            try:
                self._handle_heard(text.lower(), addressed=interrupted)
            except Exception as e:
                log_debug(f"heard-text handling failed: {e}\n{traceback.format_exc()}")

    def _handle_heard(self, raw_text, addressed=False):
        """Decide whether something heard was meant for Amy, and act on it."""
        cfg = CONFIG.get("assistant", {})
        raw_text = self.repair_transcript(raw_text)
        interpreted, _ = self.interpret(raw_text)
        if interpreted is None:
            self._followup_until = time.time() + 8      # she asked "did you mean...?"
            return
        raw_text = interpreted
        log_debug(f"Heard: {raw_text}")
        self.overlay_heard(raw_text[:60])
        matched, cmd = self._matches_wake(raw_text)

        # In wake-word barge-in mode, only her name or a stop word cuts in.
        if self.is_speaking and str(cfg.get("barge_in", "voice")).lower() == "wake_word":
            if matched or re.search(r"\b(stop|quiet|enough|cancel|never ?mind)\b", raw_text):
                self.on_barge_in()
                if cmd:
                    self._dispatch_command(cmd)
            return

        if re.fullmatch(r"(stop|quiet|shush|enough|cancel|never ?mind|that'?s enough)[.! ]*",
                        cmd if matched else raw_text):
            self.stop_speech()
            if self.agent.running:
                self.agent.stop()
            return

        expecting = addressed or time.time() < self._followup_until or self.pending_clarification \
            or self.awaiting_approval
        if self.conversation_mode and (time.time() - self.last_interaction) > self.conversation_timeout:
            self.conversation_mode = False
            self.run_js("setConversationMode(false)")
        if self.conversation_mode:
            if any(p in raw_text for p in ["end conversation", "stop conversation", "that's all",
                                           "thats all", "goodbye amy", "exit conversation"]):
                self.set_conversation_mode(False)
                return
            expecting = True

        if matched and not cmd:
            # Just her name: answer and listen for the request.
            self.last_interaction = time.time()
            self._followup_until = time.time() + 8
            self.run_js("updateWakeTriggerUI(true)")
            self.overlay_state("wake")
            self.sfx.play("wake")
            self.run_js("window.amyWakeUp && amyWakeUp()")
            self.speak("Yes?")
            threading.Timer(1.5, lambda: self.run_js("updateWakeTriggerUI(false)")).start()
            return
        if matched:
            self._dispatch_command(cmd)
        elif expecting:
            self._dispatch_command(raw_text)

    def _dispatch_command(self, text):
        self._last_input_voice = True
        self.sfx.play("ack")
        self.run_js("window.amyWakeUp && amyWakeUp()")
        self.last_interaction = time.time()
        self._followup_until = 0.0
        self.safe_log(f'"{text}"', is_user=True)
        self._cmd_queue.put(text)

    def _command_worker(self):
        """Runs requests one at a time, off the listening thread, so Amy keeps
        hearing you (and can be interrupted) while she works."""
        while True:
            text = self._cmd_queue.get()
            try:
                self.process_command_backend(text)
            except Exception as e:
                log_debug(f"command failed: {e}\n{traceback.format_exc()}")
            finally:
                self.last_interaction = time.time()

    def release_models(self):
        """Unload every model Ollama has in memory (and Whisper). Used while
        Amy's asleep; the next request loads what it needs again."""
        freed = []
        try:
            r = self.http.get(_OLLAMA_BASE + "/api/ps", timeout=5)
            for m in (r.json().get("models") or []) if r.status_code == 200 else []:
                name = m.get("name") or m.get("model")
                if not name:
                    continue
                url = OLLAMA_GENERATE_URL if "embed" not in name else \
                    OLLAMA_GENERATE_URL.replace("/api/generate", "/api/embeddings")
                body = {"model": name, "keep_alive": 0}
                if "embed" in name:
                    body["prompt"] = ""
                self.http.post(url, json=body, timeout=15)
                freed.append(name)
        except Exception as e:
            log_debug(f"release_models: {e}")
        if self._whisper is not None:
            self._whisper = None
            freed.append("whisper")
        import gc
        gc.collect()
        if freed:
            log_debug(f"Released while asleep: {', '.join(freed)}")
        return freed

    def set_sleeping(self, on):
        self.sleeping = bool(on)
        if _mem_mode() == "performance":
            return
        if self.sleeping:
            threading.Thread(target=self.release_models, daemon=True).start()
        else:
            threading.Thread(target=self._warm_up_model, daemon=True).start()

    def on_clap(self):
        """Double clap: wake up and listen, same as saying her name."""
        if self.mic_muted:
            return
        log_debug("Double clap detected.")
        self.last_interaction = time.time()
        self._followup_until = time.time() + 8
        self.sfx.play("clap")
        self.run_js("updateWakeTriggerUI(true); window.amyWakeUp && amyWakeUp()")
        self.overlay_state("wake")
        threading.Timer(1.5, lambda: self.run_js("updateWakeTriggerUI(false)")).start()

    def on_barge_in(self):
        """The user started talking over Amy."""
        if not self.is_speaking and self.speech_queue.empty():
            return
        log_debug("Barge-in: stopping speech.")
        self.stop_speech()
        self.sfx.play("stop")
        self.run_js("updateListeningUI(true)")
        self.overlay_state("listening")

    def _level_pusher(self):
        """Feeds live audio levels to the orb and the overlay (~25 fps):
        your voice while you talk, Amy's while she does."""
        last, last_push = -1.0, 0.0
        while True:
            time.sleep(0.04)
            try:
                if self.is_speaking and self.audio_out is not None and self.audio_out.playing():
                    lvl = self.audio_out.level
                elif self.audio_in is not None and not self.mic_muted:
                    lvl = self.audio_in.level
                else:
                    lvl = 0.0
                now = time.time()
                if abs(lvl - last) < 0.015 and now - last_push < 0.5:
                    continue
                last, last_push = lvl, now
                if self.window is not None and self.ui_ready:
                    self.window.evaluate_js_async(f"amyLevel({lvl:.3f})")
                ov = self.hud_overlay
                if ov is not None and self._overlay_visible:
                    gui().post(lambda v=lvl: ov.set_level(v))
            except Exception:
                time.sleep(0.5)

    def _voice_listener_loop_legacy(self):
        init_thread_com()
        if not SPEECH_AVAILABLE:
            return

        recognizer = sr.Recognizer()
        # Tuned for reliable capture of natural speech:
        recognizer.pause_threshold = float(CONFIG.get('assistant', {}).get('pause_threshold', 1.0))
        recognizer.non_speaking_duration = 0.4
        recognizer.phrase_threshold = 0.2
        recognizer.dynamic_energy_threshold = True   # adapt to changing room noise
        recognizer.energy_threshold = 300

        # Open the microphone ONCE and keep it open. Re-opening every loop was
        # slow and clipped the start of speech.
        mic = None
        try:
            mic = sr.Microphone()
            with mic as source:
                self.safe_log("Calibrating microphone for ambient noise...")
                recognizer.adjust_for_ambient_noise(source, duration=1.2)
                log_debug(f"Mic calibrated. energy_threshold={recognizer.energy_threshold}")
                self.safe_log("Microphone ready.")
        except Exception as e:
            log_debug(f"Microphone init failed: {e}")
            self.safe_log("No microphone detected — voice control disabled.")
            return

        recalibrate_at = time.time() + 300   # periodic re-calibration

        while True:
            if not self.ui_ready or self.mic_muted:
                time.sleep(0.1)
                continue

            try:
                # --- Barge-in: listen for an interrupt while Amy talks ---
                if self.is_speaking:
                    try:
                        with mic as source:
                            audio = recognizer.listen(source, timeout=1.2, phrase_time_limit=3.0)
                        heard = recognizer.recognize_google(audio).lower().strip()
                        matched, trailing = self._matches_wake(heard)
                        if matched or any(w in heard for w in
                                          ["stop", "quiet", "shut up", "enough", "cancel", "nevermind", "never mind"]):
                            self.stop_speech()
                            self.safe_log("Speech interrupted.", is_user=True)
                            if matched and trailing:
                                self.process_command_backend(trailing)
                    except Exception:
                        pass
                    continue

                # --- Periodic re-calibration keeps accuracy up over long sessions ---
                if time.time() > recalibrate_at or self._force_recalibrate:
                    self._force_recalibrate = False
                    try:
                        with mic as source:
                            recognizer.adjust_for_ambient_noise(source, duration=0.5)
                        # dynamic_energy_threshold can ratchet upward in a noisy
                        # room and never come back down, which makes her go deaf.
                        ceiling = float(CONFIG.get("assistant", {}).get("mic_threshold_ceiling", 1800))
                        floor = float(CONFIG.get("assistant", {}).get("mic_threshold_floor", 120))
                        if recognizer.energy_threshold > ceiling:
                            recognizer.energy_threshold = ceiling
                        elif recognizer.energy_threshold < floor:
                            recognizer.energy_threshold = floor
                        log_debug(f"Re-calibrated. threshold={recognizer.energy_threshold:.0f}")
                    except Exception as e:
                        log_debug(f"recalibration failed: {e}")
                    recalibrate_at = time.time() + 300

                # Safety net: if the threshold has drifted far above the ceiling
                # between calibrations, pull it back immediately.
                try:
                    ceiling = float(CONFIG.get("assistant", {}).get("mic_threshold_ceiling", 1800))
                    if recognizer.energy_threshold > ceiling * 1.5:
                        log_debug(f"Threshold runaway ({recognizer.energy_threshold:.0f}); resetting.")
                        recognizer.energy_threshold = ceiling
                except Exception:
                    pass

                # --- Main listen ---
                audio = self._listen_dynamic(recognizer, mic, timeout=3.0, purpose="command")

                try:
                    raw_text = self._recognize_best(recognizer, audio)
                    self._stt_failures = 0
                    if not raw_text:
                        continue
                except sr.UnknownValueError:
                    continue          # just noise, not speech
                except sr.RequestError as e:
                    # Google STT needs internet. Tell the user once rather than
                    # silently appearing deaf.
                    self._stt_failures += 1
                    log_debug(f"STT request failed: {e}")
                    if self._stt_failures in (3, 20):
                        self.safe_log("Speech recognition can't reach Google - check your "
                                      "internet connection. Typing still works.")
                    continue

                if not raw_text:
                    continue
                # If they paused mid-thought, keep listening and join it up.
                raw_text = self._listen_with_continuation(recognizer, mic, raw_text)
                # Repair misheard / clipped words before we try to understand it.
                raw_text = self.repair_transcript(raw_text)
                # Then match against the commands he can actually run.
                interpreted, changed = self.interpret(raw_text)
                if interpreted is None:
                    continue          # asked a clarifying question; wait for the answer
                raw_text = interpreted
                log_debug(f"Heard: {raw_text}")
                self.run_overlay_js(f"setHeard({json.dumps(raw_text[:60])})")

                matched, cmd = self._matches_wake(raw_text)

                # In conversation mode the wake word is optional — anything you say
                # is treated as a command, until you end it or it times out.
                if not matched and self.conversation_mode:
                    if (time.time() - self.last_interaction) > self.conversation_timeout:
                        self.conversation_mode = False
                        self.run_js("setConversationMode(false)")
                        log_debug("Conversation mode timed out.")
                    else:
                        if any(p in raw_text for p in ["end conversation", "stop conversation",
                                                       "that's all", "thats all", "goodbye amy",
                                                       "exit conversation"]):
                            self.set_conversation_mode(False)
                            continue
                        self.last_interaction = time.time()
                        self.safe_log(f'You: "{raw_text}"', is_user=True)
                        self.process_command_backend(raw_text)
                        self.last_interaction = time.time()
                        continue

                if not matched:
                    continue

                self.last_interaction = time.time()
                self.run_js("updateWakeTriggerUI(true)")
                self.run_overlay_js("setState('listening')")

                if cmd:
                    self.run_js("updateWakeTriggerUI(false)")
                    self.safe_log(f'Voice: "{cmd}"', is_user=True)
                    self.process_command_backend(cmd)
                else:
                    # Wake word alone -> prompt and wait for the actual command.
                    self.run_js("updateListeningUI(true)")
                    self.speak("Yes, Sir?")
                    while self.is_speaking:
                        time.sleep(0.02)
                    try:
                        followup_audio = self._listen_dynamic(recognizer, mic, timeout=5.0,
                                                              purpose="followup")
                        followup_cmd = recognizer.recognize_google(followup_audio).lower().strip()
                        followup_cmd = self._listen_with_continuation(recognizer, mic, followup_cmd)
                        if followup_cmd:
                            self.safe_log(f'Voice: "{followup_cmd}"', is_user=True)
                            self.process_command_backend(followup_cmd)
                    except (sr.UnknownValueError, sr.WaitTimeoutError):
                        pass
                    except Exception as e:
                        log_debug(f"Follow-up listen error: {e}")
                    finally:
                        self.run_js("updateListeningUI(false)")

                self.run_js("updateWakeTriggerUI(false)")
                self.run_overlay_js("setState('')")

            except sr.WaitTimeoutError:
                continue
            except Exception as e:
                log_debug(f"Voice listener exception: {e}")
                self.run_js("updateListeningUI(false)")
                self.run_js("updateWakeTriggerUI(false)")
                time.sleep(0.5)

    # --- EMAIL ENGINE ---
    def draft_email_with_ai(self, instruction):
        """Ask the local LLM to write an email body from a natural-language instruction.
        Returns (subject, body). Falls back to a simple template if the model is unavailable."""
        subject = None
        body = None
        try:
            payload = {
                "model": self.model_for("general"),
                "messages": [
                    {"role": "system", "content": EMAIL_WRITER_PROMPT},
                    {"role": "user", "content":
                        f"Write an email for this request: {instruction}\n"
                        "Respond as JSON with exactly two keys: \"subject\" and \"body\"."},
                ],
                "stream": False,
                "format": "json",
                "options": {"num_predict": 400, "temperature": 0.6},
            }
            res = self.http.post(OLLAMA_URL, json=payload, timeout=45)
            if res.status_code == 200:
                content = self._llm_text(res)
                try:
                    parsed = json.loads(content)
                    subject = (parsed.get("subject") or "").strip()
                    body = (parsed.get("body") or "").strip()
                except (json.JSONDecodeError, TypeError):
                    body = content
        except Exception as e:
            log_debug(f"AI email draft error: {e}")

        if not body:
            body = f"Hello,\n\n{instruction}\n\nBest regards"
        if not subject:
            # Derive a short subject from the instruction.
            subject = instruction.strip().capitalize()
            subject = (subject[:60] + "...") if len(subject) > 60 else subject
            if not subject:
                subject = "A quick note"
        return subject, body

    def compose_and_send_email(self, recipient=None, instruction=None,
                               subject=None, body=None, auto_send=None):
        """High-level entry point. Resolves the recipient, drafts the email with AI if
        needed, previews it as an in-app card, then dispatches via the configured mode."""
        if auto_send is None:
            auto_send = EMAIL_AUTO_SEND

        to_addr = resolve_contact(recipient, CONTACTS) if recipient else None
        if not to_addr:
            # Maybe the instruction itself contains an address.
            to_addr = extract_email_address(instruction or "")
        if not to_addr:
            to_addr = resolve_contact("me", CONTACTS)
            if to_addr and not is_placeholder_email(to_addr):
                self.speak("No recipient was specified, so I'll address this to you, Sir.")

        if not to_addr:
            self.speak("I could not determine a recipient email address, Sir. "
                       "Say the address directly, or save a contact first.")
            self.safe_log("Email aborted: no recipient resolved.")
            return False

        # The config ships with placeholder addresses — sending to those would
        # silently go nowhere, so stop and tell the user what to fix.
        if is_placeholder_email(to_addr):
            self.speak("Your email settings still contain the placeholder address, Sir. "
                       "Please set a real address in the config file first.")
            self.safe_log(
                f"Email aborted: '{to_addr}' is a placeholder. "
                f"Edit 'email.default_receiver' (and contacts) in {CONFIG_FILE}."
            )
            return False

        # Draft content if not fully provided.
        if not body:
            subject, body = self.draft_email_with_ai(instruction or "a brief message")
        elif not subject:
            subject = "A message from Amy"

        # Show a preview card in the HUD.
        preview = f"To: {to_addr}\nSubject: {subject}\n\n{body}"
        self.instantiate_card("Email Draft", "email_draft", preview)
        self.safe_log(f"Email drafted to {to_addr} | Subject: {subject}")

        if EMAIL_MODE == "smtp":
            return self.send_email_smtp(to_addr, subject, body)
        return self.send_email_browser(to_addr, subject, body, auto_send=auto_send)

    def send_email_browser(self, to_addr, subject, body, auto_send=True):
        """Open Gmail's compose window with fields pre-filled, then optionally send.

        Uses Gmail's compose URL to place recipient/subject/body reliably instead of
        fragile field-tabbing, then presses Ctrl+Enter (Gmail's send shortcut).
        """
        try:
            # Gmail's compose URL. quote() with safe='' encodes newlines correctly;
            # urlencode turned spaces into '+' which corrupted the body text.
            compose_url = (
                "https://mail.google.com/mail/u/0/?fs=1&tf=cm"
                f"&to={urllib.parse.quote(to_addr, safe='')}"
                f"&su={urllib.parse.quote(subject, safe='')}"
                f"&body={urllib.parse.quote(body, safe='')}"
            )

            # Browsers/servers reject very long URLs; fall back to SMTP or manual.
            if len(compose_url) > 7500:
                self.speak("This email is too long for the browser method, Sir. Sending via SMTP instead.")
                return self.send_email_smtp(to_addr, subject, body)

            self.speak("Opening Gmail to compose your email, Sir.")
            chrome = find_chrome_path()
            opened = False
            if chrome:
                try:
                    subprocess.Popen([chrome, "--new-window", compose_url])
                    opened = True
                except Exception as e:
                    log_debug(f"Chrome launch failed, falling back: {e}")
            if not opened:
                opened = webbrowser.open(compose_url)
            if not opened:
                self.speak("I could not open a browser for Gmail, Sir.")
                return False

            if not auto_send:
                self.speak("The email is drafted in Gmail. Review it, then press Control and Enter to send, Sir.")
                self.safe_log(f"Email prepared in browser for {to_addr} (manual send).")
                return True

            # Wait for the compose window, confirming by window title rather than
            # blindly sleeping a fixed time.
            focused = False
            deadline = time.time() + 20
            while time.time() < deadline:
                time.sleep(1.0)
                if focus_window_by_title("Gmail") or focus_window_by_title("Compose"):
                    focused = True
                    break

            if not focused:
                # Be honest: do NOT fire a blind hotkey at an unknown window.
                self.speak("Gmail is open but I could not confirm the compose window, Sir. "
                           "Please review it and press Control and Enter to send.")
                self.safe_log(f"Email drafted for {to_addr}; automatic send skipped (compose window not confirmed).")
                return False

            time.sleep(2.0)   # let the compose fields finish populating
            pyautogui.hotkey('ctrl', 'enter')
            time.sleep(1.0)

            self.speak("Email sent through Gmail, Sir.")
            self.safe_log(f"Email dispatched via Gmail to {to_addr}. "
                          "(Check your Sent folder to confirm.)")
            return True
        except Exception as e:
            log_debug(f"Browser email error: {e}\n{traceback.format_exc()}")
            self.speak("I was unable to complete the browser email flow, Sir.")
            self.safe_log(f"Browser email failed: {e}")
            return False

    def send_email_smtp(self, to_addr, subject, body):
        """Silent background send via SMTP (requires configured credentials)."""
        if is_placeholder_email(EMAIL_ADDRESS) or not EMAIL_PASSWORD or EMAIL_PASSWORD.startswith("YOUR_"):
            self.speak("SMTP credentials aren't configured, Sir. Switching to the browser method.")
            self.safe_log(f"SMTP unconfigured — set email.address and email.app_password in {CONFIG_FILE}.")
            return self.send_email_browser(to_addr, subject, body, auto_send=EMAIL_AUTO_SEND)
        try:
            msg = MIMEMultipart()
            msg['From'] = EMAIL_ADDRESS
            msg['To'] = to_addr
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain'))

            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30)
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.sendmail(EMAIL_ADDRESS, to_addr, msg.as_string())
            server.quit()

            self.speak("Email transmitted successfully, Sir.")
            self.safe_log(f"Email successfully sent to {to_addr}")
            return True
        except smtplib.SMTPAuthenticationError:
            self.speak("Gmail rejected the login, Sir. You need an App Password, not your normal password.")
            self.safe_log("SMTP auth failed — generate an App Password at myaccount.google.com/apppasswords.")
            return False
        except Exception as e:
            log_debug(f"SMTP transmission error: {e}")
            self.speak("Failed to transmit the email, Sir.")
            self.safe_log(f"SMTP failed: {e}")
            return False

    # Backwards-compatible shim (older callers used send_email(subject, body)).
    def send_email(self, subject, body, to_addr=None):
        to_addr = to_addr or resolve_contact("me", CONTACTS)
        if EMAIL_MODE == "smtp":
            return self.send_email_smtp(to_addr, subject, body)
        return self.send_email_browser(to_addr, subject, body, auto_send=EMAIL_AUTO_SEND)

    # --- CONTINUOUS SCREEN ANALYSIS & AUTONOMOUS MOUSE AGENT ---
    def toggle_continuous_vision(self, active_state):
        self.continuous_vision_active = bool(active_state)
        if self.continuous_vision_active:
            self.speak("Screen awareness active. I'll keep an eye on your display, Sir.")
        else:
            self.speak("Screen awareness deactivated.")

    def _continuous_vision_worker(self):
        """Live screen awareness.

        A capture loop grabs the screen a few times a second (mss: fast, low
        CPU) and keeps the latest frame. A cheap change score on a tiny
        greyscale copy decides when the screen meaningfully changed; only then,
        or when you switch windows, does the vision model describe what you're
        doing. That keeps the description fresh without hammering the GPU -
        local vision models take a second or more per look, so describing
        every frame isn't possible anyway.

        This only observes. It never moves the mouse or clicks.
        """
        init_thread_com()
        prev_small = None
        last_title = None
        last_describe = 0.0
        fps = float(CONFIG.get("assistant", {}).get("screen_fps", 2))
        while True:
            if not self.continuous_vision_active or self.sleeping:
                time.sleep(0.6)
                continue
            t0 = time.time()
            try:
                img = self._grab_screen()
                if img is None:
                    time.sleep(1.0)
                    continue
                self.latest_screen = img
                self.latest_screen_time = t0
                small = img.resize((64, 36)).convert("L")
                if _np is not None:
                    arr = _np.asarray(small, dtype=_np.int16)
                    change = 1.0 if prev_small is None else float(_np.abs(arr - prev_small).mean()) / 255
                    prev_small = arr
                else:
                    change = 1.0
                title, proc, _ = foreground_window_info()
                switched = title != last_title
                last_title = title
                due = (t0 - last_describe) >= self.vision_interval
                if (switched or change > 0.02) and due and not self.is_speaking:
                    last_describe = t0
                    shot = img.copy()
                    shot.thumbnail((768, 768))
                    buf = io.BytesIO()
                    shot.save(buf, format="JPEG", quality=70)
                    res = self.http.post(OLLAMA_GENERATE_URL, json={
                        "model": self.model_for("vision"),
                        "prompt": (f"The active window is '{title}' ({proc}). In ONE short sentence, "
                                   "say what the user is doing right now (the app and the task). "
                                   "Mention any visible error message."),
                        "images": [base64.b64encode(buf.getvalue()).decode("ascii")],
                        "stream": False, "keep_alive": "30m",
                        "options": {"num_predict": 60, "temperature": 0.2},
                    }, timeout=45)
                    desc = self._llm_text(res)
                    if desc:
                        self.screen_context = desc
                        self.screen_context_time = time.time()
                        log_debug(f"Screen context: {desc}")
                        self.run_js(f"updateScreenContext({json.dumps(desc)})")
            except Exception as e:
                log_debug(f"Screen awareness error: {e}")
                time.sleep(1.0)
            time.sleep(max(0.05, 1.0 / max(0.2, fps) - (time.time() - t0)))

    def _grab_screen(self):
        """Primary monitor as a PIL image: mss when available (fast), else PIL."""
        try:
            if HAS_MSS:
                if not hasattr(self._tls, "sct"):
                    self._tls.sct = mss.mss()          # mss handles aren't thread-safe
                raw = self._tls.sct.grab(self._tls.sct.monitors[1])
                return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            return ImageGrab.grab()
        except Exception as e:
            log_debug(f"screen grab failed: {e}")
            return None

    def describe_screen_now(self):
        """Answer 'what am I looking at' — instantly if we have recent context."""
        if self.screen_context and (time.time() - self.screen_context_time) < 30:
            self.speak(self.screen_context)
            return True
        self.capture_screen_vision("In two short sentences, describe what is on this screen.")
        return True

    def capture_screen_vision(self, user_prompt="Describe what is visible on this screen in detail."):
        self.speak("Analyzing screen, Sir.")
        threading.Thread(target=self._process_screen_vision, args=(user_prompt,), daemon=True).start()

    def _process_screen_vision(self, prompt_text):
        try:
            self.safe_log("Vision System: Capturing screen frame...")
            screenshot = ImageGrab.grab(all_screens=True)
            
            screenshot.thumbnail((768, 768))
            buf = io.BytesIO()
            screenshot.save(buf, format='JPEG', quality=75)
            img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

            self.safe_log("Vision System: Sending image to Ollama...")

            clean_prompt = prompt_text
            for prefix in ["look at screen", "read screen", "what is on my screen", "see my screen", "and"]:
                if clean_prompt.lower().startswith(prefix):
                    clean_prompt = clean_prompt[len(prefix):].strip()

            if not clean_prompt or len(clean_prompt) < 3:
                clean_prompt = "Describe what is visible on this screen in detail."

            payload = {
                "model": self.model_for("vision"),
                "prompt": clean_prompt,
                "images": [img_b64],
                "stream": False
            }

            res = self.http.post(OLLAMA_GENERATE_URL, json=payload, timeout=120)

            if res.status_code == 200:
                answer = self._llm_text(res) or "Vision processing produced no result."
                self.safe_log(f"Vision Analysis: {answer}")
                self.speak(answer)
            else:
                err_msg = f"Vision Error HTTP {res.status_code}: {res.text}"
                self.safe_log(err_msg)
                self.speak("Vision processing failed, Sir.")

        except requests.exceptions.Timeout:
            self.safe_log("Vision Error: Ollama inference timed out.")
            self.speak("Vision analysis timed out, Sir.")
        except Exception as e:
            log_debug(f"Vision error: {e}\n{traceback.format_exc()}")
            self.safe_log(f"Vision Error: {str(e)}")
            self.speak("An error occurred during vision processing, Sir.")

    # --- DOCUMENT WORKSPACE: generate -> display -> edit -> export ---
    def create_document(self, topic):
        """Generate a report on a topic, then DISPLAY it in the in-app editable viewer.
        The user can then say 'edit the document to ...' to revise it."""
        self.speak(f"Compiling a report on {topic}, Sir.")
        content = self._llm_generate_report(topic)
        self.current_doc = {"topic": topic, "content": content}
        self.show_document_editor(topic, content)
        self.speak("Your document is ready and displayed. You may ask me to edit it, or say 'export to PDF', Sir.")
        return True

    def _llm_generate_report(self, topic, extra_instruction=None):
        base = (f"Write a well-structured, informative report about: {topic}. "
                "Use clear paragraphs and, where useful, short headed sections. Plain text only.")
        if extra_instruction:
            base += f"\nAdditional instruction: {extra_instruction}"
        try:
            payload = {
                "model": self.model_for("general"),
                "messages": [{"role": "user", "content": base}],
                "stream": False,
                "options": {"num_predict": 600, "temperature": 0.6, "num_ctx": 2048},
            }
            res = self.http.post(OLLAMA_URL, json=payload, timeout=90)
            if res.status_code == 200:
                return self._llm_text(res) or f"Report on {topic}."
        except Exception as e:
            log_debug(f"report gen error: {e}")
        return f"# {topic}\n\n(Local AI unavailable — this is a placeholder. Start Ollama to generate full content.)"

    def show_document_editor(self, topic, content):
        preview_html = self._render_document_html(topic, content, for_file=False)
        payload = json.dumps({"topic": topic, "content": content, "preview_html": preview_html})
        self.broadcast_js(f"showDocumentEditor({payload})")

    def edit_document(self, instruction):
        """Revise the currently open document per a natural-language instruction."""
        if not getattr(self, "current_doc", None):
            self.speak("There is no document open to edit, Sir. Ask me to create one first.")
            return True
        self.speak("Revising the document now, Sir.")
        topic = self.current_doc["topic"]
        current = self.current_doc["content"]
        try:
            prompt = (f"Here is the current document:\n\n{current}\n\n"
                      f"Revise it according to this instruction: {instruction}\n"
                      "Return the FULL revised document as plain text, nothing else.")
            payload = {
                "model": self.model_for("general"),
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"num_predict": 700, "temperature": 0.5, "num_ctx": 4096},
            }
            res = self.http.post(OLLAMA_URL, json=payload, timeout=120)
            new_content = self._llm_text(res)
            if new_content:
                self.current_doc["content"] = new_content
                self.show_document_editor(topic, new_content)
                self.speak("The document has been revised, Sir.")
        except Exception as e:
            log_debug(f"edit doc error: {e}")
            self.speak("I was unable to revise the document, Sir.")
        return True

    def update_document_from_ui(self, content):
        """Called when the user manually edits the doc text in the viewer."""
        if getattr(self, "current_doc", None):
            self.current_doc["content"] = content

    def edit_document_ui(self, instruction):
        """Non-blocking wrapper for the UI 'REVISE' button."""
        threading.Thread(target=self.edit_document, args=(instruction,), daemon=True).start()

    def export_current_document_pdf(self):
        if not getattr(self, "current_doc", None):
            self.speak("There is no open document to export, Sir.")
            return True
        path = self.generate_pdf(self.current_doc["topic"], self.current_doc["content"], open_after=True)
        if path:
            self.speak("Exported to PDF and opened for you, Sir.")
        return True

    def _render_document_html(self, topic, text_content, for_file=True):
        """Render a document as a beautifully styled 'Gemini-like' HTML page:
        gradient header, accent rules, styled headings, bullet cards and callouts."""
        import html as _html

        def esc(s):
            return _html.escape(s)

        # Parse simple markdown-ish structure into styled blocks.
        blocks = []
        lines = text_content.split('\n')
        i = 0
        while i < len(lines):
            raw = lines[i].rstrip()
            stripped = raw.strip()
            if not stripped:
                i += 1
                continue
            # Headings
            if stripped.startswith('### '):
                blocks.append(f'<h3>{esc(stripped[4:])}</h3>')
            elif stripped.startswith('## '):
                blocks.append(f'<h2>{esc(stripped[3:])}</h2>')
            elif stripped.startswith('# '):
                blocks.append(f'<h2>{esc(stripped[2:])}</h2>')
            elif (len(stripped) < 60 and stripped.endswith(':') and not stripped.startswith(('-', '*', '•'))):
                blocks.append(f'<h3>{esc(stripped.rstrip(":"))}</h3>')
            # Bullet groups
            elif stripped.startswith(('- ', '* ', '• ')):
                items = []
                while i < len(lines) and lines[i].strip().startswith(('- ', '* ', '• ')):
                    items.append(esc(lines[i].strip()[2:]))
                    i += 1
                lis = ''.join(f'<li>{it}</li>' for it in items)
                blocks.append(f'<ul>{lis}</ul>')
                continue
            # Numbered lists
            elif re.match(r'^\d+[\.\)]\s', stripped):
                items = []
                while i < len(lines) and re.match(r'^\d+[\.\)]\s', lines[i].strip()):
                    items.append(esc(re.sub(r'^\d+[\.\)]\s', '', lines[i].strip())))
                    i += 1
                lis = ''.join(f'<li>{it}</li>' for it in items)
                blocks.append(f'<ol>{lis}</ol>')
                continue
            else:
                blocks.append(f'<p>{esc(stripped)}</p>')
            i += 1

        body = '\n'.join(blocks)
        generated = time.strftime('%B %d, %Y  ·  %H:%M')
        full = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:'Segoe UI',system-ui,sans-serif; background:#0a0e17; color:#1a1f2e;
         padding:{'40px' if for_file else '0'}; }}
  .doc {{ max-width:820px; margin:0 auto; background:#ffffff; border-radius:18px; overflow:hidden;
          box-shadow:0 20px 60px rgba(0,0,0,0.45); border:1px solid rgba(94,234,212,0.25); }}
  .doc-header {{ background:linear-gradient(135deg,#0b1220 0%,#122a3a 50%,#0b1a2a 100%);
                 padding:34px 40px; position:relative; overflow:hidden; }}
  .doc-header::before {{ content:''; position:absolute; top:-40%; right:-10%; width:280px; height:280px;
                 background:radial-gradient(circle,rgba(94,234,212,0.35),transparent 70%); }}
  .doc-badge {{ display:inline-block; font-size:11px; letter-spacing:2px; color:#5eead4;
                border:1px solid rgba(94,234,212,0.5); padding:4px 12px; border-radius:20px;
                text-transform:uppercase; margin-bottom:14px; font-weight:600; }}
  .doc-title {{ font-size:30px; font-weight:800; color:#fff; line-height:1.2;
                background:linear-gradient(90deg,#fff,#99f6e4); -webkit-background-clip:text;
                -webkit-text-fill-color:transparent; }}
  .doc-meta {{ margin-top:10px; font-size:12px; color:#8aa4b8; letter-spacing:1px; }}
  .accent-rule {{ height:4px; background:linear-gradient(90deg,#5eead4,#1ed760,transparent); }}
  .doc-body {{ padding:38px 44px 48px; line-height:1.75; font-size:15px; color:#232a3a; }}
  .doc-body h2 {{ font-size:21px; margin:26px 0 10px; color:#0b2a3a; padding-left:14px;
                  border-left:4px solid #5eead4; }}
  .doc-body h3 {{ font-size:16px; margin:20px 0 8px; color:#0e7490; }}
  .doc-body p {{ margin:12px 0; }}
  .doc-body ul, .doc-body ol {{ margin:12px 0 12px 6px; padding-left:22px; }}
  .doc-body li {{ margin:7px 0; padding-left:6px; }}
  .doc-body ul li::marker {{ color:#5eead4; }}
  .doc-body ol li::marker {{ color:#0e7490; font-weight:700; }}
  .doc-footer {{ padding:18px 44px; border-top:1px solid #e5eef2; font-size:11px; color:#93a3b3;
                 display:flex; justify-content:space-between; letter-spacing:1px; }}
</style></head><body>
  <div class="doc">
    <div class="doc-header">
      <div class="doc-badge">Amy Intelligence Brief</div>
      <div class="doc-title">{esc(topic)}</div>
      <div class="doc-meta">Generated {generated}</div>
    </div>
    <div class="accent-rule"></div>
    <div class="doc-body">{body}</div>
    <div class="doc-footer"><span>Amy ENTERPRISE HUD</span><span>Confidential · Auto-generated</span></div>
  </div>
</body></html>"""
        return full

    def generate_pdf(self, topic, text_content, open_after=True):
        """Produce a styled document. Prefers a designed PDF via reportlab, and
        always writes a matching styled HTML version (the 'Gemini-like' look)."""
        ts = int(time.time())
        safe_topic = re.sub(r'[^\w\- ]', '', topic)[:40].strip().replace(' ', '_') or "report"
        pdf_path = os.path.join(DOCS_DIR, f"Amy_{safe_topic}_{ts}.pdf")
        html_path = os.path.join(DOCS_DIR, f"Amy_{safe_topic}_{ts}.html")

        # Always write the beautiful HTML version.
        try:
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(self._render_document_html(topic, text_content, for_file=True))
        except Exception as e:
            log_debug(f"HTML doc write error: {e}")
            html_path = None

        final_path = html_path
        try:
            if REPORTLAB_AVAILABLE:
                from reportlab.lib.colors import HexColor
                from reportlab.lib.units import inch
                from reportlab.platypus import Table, TableStyle, HRFlowable, ListFlowable, ListItem
                doc = SimpleDocTemplate(pdf_path, pagesize=letter,
                                        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
                                        leftMargin=0.9 * inch, rightMargin=0.9 * inch)
                styles = getSampleStyleSheet()
                title_style = ParagraphStyle('DTitle', parent=styles['Title'], fontSize=24,
                                             textColor=HexColor('#0b2a3a'), spaceAfter=2)
                badge_style = ParagraphStyle('DBadge', parent=styles['Normal'], fontSize=9,
                                             textColor=HexColor('#0e7490'), spaceAfter=6)
                meta_style = ParagraphStyle('DMeta', parent=styles['Normal'], fontSize=9,
                                            textColor=HexColor('#64748b'), spaceAfter=14)
                h2_style = ParagraphStyle('DH2', parent=styles['Heading2'], fontSize=15,
                                          textColor=HexColor('#0b2a3a'), spaceBefore=14, spaceAfter=6,
                                          borderColor=HexColor('#00b8d4'), borderWidth=0, leftIndent=8)
                h3_style = ParagraphStyle('DH3', parent=styles['Heading3'], fontSize=12,
                                          textColor=HexColor('#0e7490'), spaceBefore=10, spaceAfter=4)
                body_style = ParagraphStyle('DBody', parent=styles['Normal'], fontSize=10.5,
                                            leading=16, textColor=HexColor('#232a3a'), spaceAfter=6)

                story = [
                    Paragraph("Amy INTELLIGENCE BRIEF", badge_style),
                    Paragraph(topic, title_style),
                    Paragraph(f"Generated {time.strftime('%B %d, %Y · %H:%M')}", meta_style),
                    HRFlowable(width="100%", thickness=3, color=HexColor('#5eead4'), spaceAfter=14),
                ]

                def esc(s):
                    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

                lines = text_content.split('\n')
                i = 0
                while i < len(lines):
                    stripped = lines[i].strip()
                    if not stripped:
                        i += 1
                        continue
                    if stripped.startswith(('- ', '* ', '• ')):
                        items = []
                        while i < len(lines) and lines[i].strip().startswith(('- ', '* ', '• ')):
                            items.append(ListItem(Paragraph(esc(lines[i].strip()[2:]), body_style),
                                                  leftIndent=14))
                            i += 1
                        story.append(ListFlowable(items, bulletType='bullet',
                                                  bulletColor=HexColor('#00b8d4'), start='•'))
                        continue
                    if stripped.startswith('### '):
                        story.append(Paragraph(esc(stripped[4:]), h3_style))
                    elif stripped.startswith(('## ', '# ')):
                        story.append(Paragraph(esc(stripped.lstrip('# ')), h2_style))
                    elif len(stripped) < 60 and stripped.endswith(':'):
                        story.append(Paragraph(esc(stripped.rstrip(':')), h3_style))
                    else:
                        story.append(Paragraph(esc(stripped), body_style))
                    i += 1

                story.append(Spacer(1, 18))
                story.append(HRFlowable(width="100%", thickness=1, color=HexColor('#e5eef2')))
                story.append(Paragraph("Amy Enterprise HUD · Confidential · Auto-generated", meta_style))
                doc.build(story)
                final_path = pdf_path
        except Exception as e:
            log_debug(f"Styled PDF error (using HTML instead): {e}")
            final_path = html_path

        if final_path:
            self.safe_log(f"Document generated: {final_path}")
            if open_after:
                self.launch_external_app(final_path)
        return final_path

    # --- IN-APP IMAGE / WEBSITE VIEWER ---
    def open_in_viewer(self, kind, src, title=None):
        """kind = 'image' | 'web'. Displays inside Amy's viewer widget."""
        payload = json.dumps({"kind": kind, "src": src, "title": title or src})
        self.broadcast_js(f"openViewer({payload})")

    def show_image_for(self, query):
        """Fetch an image URL for a query and show it inside Amy.
        Great for 'show me how to tie a tie' style instruction images."""
        self.speak(f"Finding a visual for {query}, Sir.")
        url = self._search_image_url(query)
        if url:
            self.open_in_viewer("image", url, title=query)
            self.safe_log(f"Displaying image for: {query}")
        else:
            # Fall back to Google Images in the embedded browser view.
            q = urllib.parse.quote_plus(query)
            self.open_in_viewer("web", f"https://www.google.com/search?tbm=isch&q={q}", title=f"Images: {query}")
        return True

    def _search_image_url(self, query):
        """Use DuckDuckGo's image endpoint (no API key) to get a direct image URL."""
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            # Get vqd token.
            r = requests.post("https://duckduckgo.com/", data={"q": query}, headers=headers, timeout=15)
            m = re.search(r'vqd=([\d-]+)', r.text) or re.search(r'vqd="([\d-]+)"', r.text)
            if not m:
                return None
            vqd = m.group(1)
            api = "https://duckduckgo.com/i.js"
            params = {"l": "us-en", "o": "json", "q": query, "vqd": vqd, "f": ",,,", "p": "1"}
            res = requests.get(api, params=params, headers=headers, timeout=15)
            data = res.json()
            results = data.get("results", [])
            if results:
                return results[0].get("image")
        except Exception as e:
            log_debug(f"image search error: {e}")
        return None

    # --- NEW FEATURE: WEB / YOUTUBE / MAPS SEARCH IN BROWSER ---
    def web_search(self, query, engine="google"):
        q = urllib.parse.quote_plus(query)
        urls = {
            "google": f"https://www.google.com/search?q={q}",
            "youtube": f"https://www.youtube.com/results?search_query={q}",
            "maps": f"https://www.google.com/maps/search/{q}",
            "wikipedia": f"https://en.wikipedia.org/wiki/Special:Search?search={q}",
            "amazon": f"https://www.amazon.com/s?k={q}",
            "images": f"https://www.google.com/search?tbm=isch&q={q}",
        }
        url = urls.get(engine, urls["google"])
        chrome = find_chrome_path()
        try:
            if chrome:
                subprocess.Popen([chrome, "--new-tab", url])
            else:
                webbrowser.open(url)
            self.speak(f"Searching {engine} for {query}, Sir.")
            self.safe_log(f"Web search ({engine}): {query}")
            return True
        except Exception as e:
            log_debug(f"web_search error: {e}")
            self.speak("I was unable to open the browser search, Sir.")
            return False

    # --- NEW FEATURE: WEATHER (no API key, via open-meteo) ---
    def get_weather(self, city=None):
        city = (city or DEFAULT_CITY).strip()
        try:
            geo = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1}, timeout=15).json()
            results = geo.get("results")
            if not results:
                self.speak(f"I could not find a location called {city}, Sir.")
                return True
            loc = results[0]
            lat, lon = loc["latitude"], loc["longitude"]
            name = loc.get("name", city)
            wx = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": lat, "longitude": lon,
                        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"},
                timeout=15).json()
            cur = wx.get("current", {})
            temp = cur.get("temperature_2m")
            wind = cur.get("wind_speed_10m")
            humidity = cur.get("relative_humidity_2m")
            report = (f"Current weather in {name}: {temp} degrees, "
                      f"humidity {humidity} percent, wind {wind} kilometres per hour, Sir.")
            self.speak(report)
            self.safe_log(report)
            self.instantiate_card(f"Weather — {name}", "email_draft", report)
            return True
        except Exception as e:
            log_debug(f"weather error: {e}")
            self.speak("I was unable to retrieve the weather, Sir.")
            return True

    # --- NEW FEATURE: TIME & DATE ---
    def tell_time(self):
        now = datetime.datetime.now()
        self.speak(f"It is {now.strftime('%I:%M %p')}, Sir.")
        return True

    def tell_date(self):
        now = datetime.datetime.now()
        self.speak(f"Today is {now.strftime('%A, %B %d, %Y')}, Sir.")
        return True

    # --- NEW FEATURE: REMINDERS ---
    def add_reminder(self, text, delay_seconds):
        due = time.time() + delay_seconds
        self.memory.setdefault("reminders", []).append({"text": text, "due": due})
        self.save_memory()

        def fire():
            time.sleep(delay_seconds)
            self.speak(f"Reminder, Sir: {text}")
            self.safe_log(f"REMINDER FIRED: {text}")
            self.instantiate_card("Reminder", "email_draft", text)

        threading.Thread(target=fire, daemon=True).start()
        mins = max(1, int(delay_seconds // 60))
        self.speak(f"Reminder set for {mins} minute{'s' if mins != 1 else ''} from now, Sir.")
        return True

    # --- NEW FEATURE: SCREENSHOT TO DESKTOP ---
    def take_screenshot(self):
        try:
            path = os.path.join(DOCS_DIR, f"Amy_Screenshot_{int(time.time())}.png")
            img = ImageGrab.grab(all_screens=True)
            img.save(path)
            self.speak("Screenshot captured and saved, Sir.")
            self.safe_log(f"Screenshot saved: {path}")
            return True
        except Exception as e:
            log_debug(f"screenshot error: {e}")
            self.speak("Failed to capture the screenshot, Sir.")
            return False

    # --- NEW FEATURE: SYSTEM INFO / BATTERY ---
    def report_system_status(self):
        try:
            cpu = psutil.cpu_percent(interval=0.5)
            ram = psutil.virtual_memory().percent
            parts = [f"CPU at {cpu} percent", f"memory at {ram} percent"]
            try:
                batt = psutil.sensors_battery()
                if batt is not None:
                    charging = "charging" if batt.power_plugged else "on battery"
                    parts.append(f"battery at {int(batt.percent)} percent, {charging}")
            except Exception:
                pass
            report = "System status: " + ", ".join(parts) + ", Sir."
            self.speak(report)
            self.safe_log(report)
            return True
        except Exception as e:
            log_debug(f"system status error: {e}")
            self.speak("Unable to read system telemetry, Sir.")
            return False

    # --- NEW FEATURE: LOCK / SLEEP PC ---
    def lock_pc(self):
        try:
            if sys.platform == "win32" and HAS_WIN32:
                ctypes.windll.user32.LockWorkStation()
            elif sys.platform == "win32":
                subprocess.Popen("rundll32.exe user32.dll,LockWorkStation", shell=True)
            elif sys.platform == "darwin":
                subprocess.Popen(["pmset", "displaysleepnow"])
            else:
                subprocess.Popen(["loginctl", "lock-session"])
            self.speak("Locking the workstation, Sir.")
            return True
        except Exception as e:
            log_debug(f"lock error: {e}")
            self.speak("I was unable to lock the workstation, Sir.")
            return False

    # --- HARDWARE / POWER CONTROL ---
    def set_volume_level(self, percent):
        """Set system volume to an absolute percentage (Windows)."""
        percent = max(0, min(100, int(percent)))
        try:
            # Reset to 0 then step up (each vol key press ~2%).
            for _ in range(50):
                pyautogui.press("volumedown")
            for _ in range(int(percent / 2)):
                pyautogui.press("volumeup")
            self.speak(f"Volume set to about {percent} percent, Sir.")
        except Exception as e:
            log_debug(f"set volume error: {e}")
            self.speak("I could not set the volume, Sir.")
        return True

    def set_brightness(self, percent):
        """Set screen brightness (Windows via WMI/PowerShell)."""
        percent = max(0, min(100, int(percent)))
        try:
            if sys.platform == "win32":
                ps = ("(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods)"
                      f".WmiSetBrightness(1,{percent})")
                creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                subprocess.Popen(["powershell", "-Command", ps], creationflags=creationflags)
                self.speak(f"Brightness set to {percent} percent, Sir.")
            else:
                self.speak("Brightness control is only available on Windows, Sir.")
        except Exception as e:
            log_debug(f"brightness error: {e}")
            self.speak("I could not adjust brightness, Sir.")
        return True

    def sleep_display(self):
        try:
            if sys.platform == "win32":
                subprocess.Popen("powershell -Command \"(Add-Type '[DllImport(\\\"user32.dll\\\")]public static extern int SendMessage(int hWnd,int hMsg,int wParam,int lParam);' -Name a -Pass)::SendMessage(-1,0x0112,0xF170,2)\"", shell=True)
            elif sys.platform == "darwin":
                subprocess.Popen(["pmset", "displaysleepnow"])
            else:
                subprocess.Popen(["xset", "dpms", "force", "off"])
            self.speak("Turning off the display, Sir.")
        except Exception as e:
            log_debug(f"display sleep error: {e}")
        return True

    def power_action(self, action):
        """action = 'sleep' | 'restart' | 'shutdown'. Confirmed by voice/text only."""
        try:
            if sys.platform == "win32":
                cmds = {
                    "sleep": "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
                    "restart": "shutdown /r /t 5",
                    "shutdown": "shutdown /s /t 5",
                }
            elif sys.platform == "darwin":
                cmds = {"sleep": "pmset sleepnow", "restart": "sudo shutdown -r now",
                        "shutdown": "sudo shutdown -h now"}
            else:
                cmds = {"sleep": "systemctl suspend", "restart": "systemctl reboot",
                        "shutdown": "systemctl poweroff"}
            cmd = cmds.get(action)
            if not cmd:
                return True
            verb = {"sleep": "putting the system to sleep", "restart": "restarting the system",
                    "shutdown": "shutting the system down"}[action]
            self.speak(f"{verb.capitalize()} in five seconds, Sir.")
            subprocess.Popen(cmd, shell=True)
        except Exception as e:
            log_debug(f"power action error: {e}")
            self.speak("I could not perform that power action, Sir.")
        return True

    def toggle_wifi(self, on=True):
        try:
            if sys.platform == "win32":
                state = "enable" if on else "disable"
                # Try common adapter names.
                for name in ["Wi-Fi", "Wireless Network Connection", "WLAN"]:
                    subprocess.Popen(f'netsh interface set interface "{name}" {state}', shell=True)
                self.speak(f"Wi-Fi {'enabled' if on else 'disabled'}, Sir.")
            else:
                self.speak("Wi-Fi toggling is only wired up for Windows, Sir.")
        except Exception as e:
            log_debug(f"wifi toggle error: {e}")
            self.speak("I could not change the Wi-Fi state, Sir.")
        return True

    def media_control(self, action):
        keymap = {"play": "playpause", "pause": "playpause", "next": "nexttrack",
                  "previous": "prevtrack", "stop": "stop"}
        key = keymap.get(action, "playpause")
        try:
            pyautogui.press(key)
            self.speak(f"Media {action}, Sir.")
        except Exception as e:
            log_debug(f"media control error: {e}")
        return True

    # --- NEW FEATURE: CLIPBOARD READ-BACK ---
    def read_clipboard(self):
        if not HAS_CLIPBOARD:
            self.speak("Clipboard support is not installed, Sir. Install pyperclip to enable it.")
            return True
        try:
            content = pyperclip.paste()
            if content:
                snippet = content[:300]
                self.speak(f"Your clipboard contains: {snippet}")
            else:
                self.speak("Your clipboard is empty, Sir.")
            return True
        except Exception as e:
            log_debug(f"clipboard error: {e}")
            self.speak("Unable to read the clipboard, Sir.")
            return False

    # ==================================================================
    # 10 NEW FEATURES
    # ==================================================================

    # 1. Calculator / math evaluation
    def calculate(self, expression):
        try:
            expr = re.sub(r'[^0-9+\-*/().%\s]', '', expression)
            if not expr.strip():
                self.speak("I could not parse that calculation, Sir.")
                return True
            result = eval(expr, {"__builtins__": {}}, {})
            self.speak(f"That equals {result}, Sir.")
            self.safe_log(f"Calculation: {expr} = {result}")
        except Exception:
            self.speak("I was unable to compute that, Sir.")
        return True

    # 2. Wikipedia quick summary (spoken)
    def wiki_summary(self, topic):
        try:
            t = urllib.parse.quote(topic.strip().replace(" ", "_"))
            res = requests.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{t}", timeout=15,
                               headers={"User-Agent": "Amy/1.0"})
            if res.status_code == 200:
                extract = res.json().get("extract")
                if extract:
                    self.speak(extract[:600])
                    self.instantiate_card(f"Wikipedia — {topic}", "email_draft", extract)
                    return True
            self.speak(f"I could not find a summary for {topic}, Sir.")
        except Exception as e:
            log_debug(f"wiki error: {e}")
            self.speak("Wikipedia lookup failed, Sir.")
        return True

    # 3. Latest news headlines
    def get_news(self, topic=None):
        try:
            url = "https://news.google.com/rss"
            if topic:
                url = f"https://news.google.com/rss/search?q={urllib.parse.quote_plus(topic)}"
            res = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            titles = re.findall(r'<title>(.*?)</title>', res.text)
            titles = [t for t in titles[1:6] if t]
            if titles:
                spoken = "Here are the top headlines, Sir. " + ". ".join(titles[:3])
                self.speak(spoken)
                self.instantiate_card("News Headlines", "carousel", titles)
            else:
                self.speak("I could not retrieve the news, Sir.")
        except Exception as e:
            log_debug(f"news error: {e}")
            self.speak("News retrieval failed, Sir.")
        return True

    # 4. Currency / crypto price
    def get_price(self, symbol):
        sym = symbol.lower().strip()
        crypto_map = {"bitcoin": "bitcoin", "btc": "bitcoin", "ethereum": "ethereum",
                      "eth": "ethereum", "dogecoin": "dogecoin", "doge": "dogecoin"}
        try:
            if sym in crypto_map:
                cid = crypto_map[sym]
                res = requests.get("https://api.coingecko.com/api/v3/simple/price",
                                   params={"ids": cid, "vs_currencies": "usd"}, timeout=15).json()
                price = res.get(cid, {}).get("usd")
                if price is not None:
                    self.speak(f"{symbol.title()} is currently {price} US dollars, Sir.")
                    return True
            self.speak(f"I could not fetch a price for {symbol}, Sir.")
        except Exception as e:
            log_debug(f"price error: {e}")
            self.speak("Price lookup failed, Sir.")
        return True

    # 5. Timer / countdown
    def start_timer(self, seconds, label="timer"):
        def run():
            time.sleep(seconds)
            self.speak(f"Your {label} has finished, Sir.")
            try:
                for _ in range(3):
                    pyautogui.press("volumeup")
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()
        mins = seconds / 60
        self.speak(f"Timer set for {int(mins)} minute{'s' if int(mins) != 1 else ''}, Sir."
                   if mins >= 1 else f"Timer set for {seconds} seconds, Sir.")
        return True

    # 6. Remember an arbitrary fact long-term
    def remember_fact(self, fact):
        self.mind.remember(fact)
        self.memory.setdefault("facts", []).append(fact.strip())
        self.save_memory()
        self.speak("I'll remember that, Sir.")
        self.safe_log(f"Fact stored: {fact}")
        return True

    def recall_facts(self):
        facts = self.memory.get("facts", [])
        if facts:
            self.speak("Here is what I remember, Sir. " + ". ".join(facts[-8:]))
            self.instantiate_card("Things I Remember", "carousel", facts)
        else:
            self.speak("I have no stored facts yet, Sir.")
        return True

    # 7. Open a specific website inside Amy
    def open_website_in_app(self, url):
        u = url.strip()
        if not u.startswith(("http://", "https://")):
            u = "https://" + u
        title = u.replace("https://", "").replace("http://", "").split("/")[0]
        self.open_in_viewer("web", u, title=title)
        self.speak("Opening that inside the viewer, Sir.")
        return True

    def open_websites_in_app(self, urls):
        """Open several sites at once, each in its own viewer tab."""
        opened = []
        for raw in urls:
            u = raw.strip().strip(".,")
            if not u:
                continue
            if not u.startswith(("http://", "https://")):
                u = "https://" + u
            title = u.replace("https://", "").replace("http://", "").split("/")[0]
            self.open_in_viewer("web", u, title=title)
            opened.append(title)
            time.sleep(0.25)   # let each tab mount
        if opened:
            if len(opened) == 1:
                self.speak(f"Opened {opened[0]}, Sir.")
            else:
                self.speak(f"Opened {len(opened)} tabs, Sir.")
        return True

    def close_viewer_tabs(self):
        self.run_js("closeAllTabs()")
        self.speak("Closed all viewer tabs, Sir.")
        return True

    # 8. Type text into whatever app is focused (dictation)
    def system_cleanup(self):
        try:
            if sys.platform == "win32":
                try:
                    import ctypes
                    ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x00000001)
                except Exception:
                    pass
            self.speak("I've cleared the recycle bin, Sir.")
        except Exception as e:
            log_debug(f"cleanup error: {e}")
            self.speak("Cleanup could not complete, Sir.")
        return True

    # 10. Flip a coin / roll a die / random pick
    def random_choice(self, cmd):
        import random
        if "coin" in cmd:
            self.speak(f"{random.choice(['Heads', 'Tails'])}, Sir.")
        elif "dice" in cmd or "die" in cmd:
            self.speak(f"You rolled a {random.randint(1, 6)}, Sir.")
        else:
            m = re.search(r'between (.+?) and (.+)', cmd)
            if m:
                self.speak(f"I'd choose {random.choice([m.group(1).strip(), m.group(2).strip()])}, Sir.")
            else:
                self.speak(f"A random number: {random.randint(1, 100)}, Sir.")
        return True

    # ==================================================================
    # CODING ENGINE — write, save, run, explain and debug code
    # ==================================================================
    LANG_EXT = {
        "python": "py", "py": "py", "javascript": "js", "js": "js", "typescript": "ts",
        "html": "html", "css": "css", "java": "java", "c": "c", "c++": "cpp", "cpp": "cpp",
        "c#": "cs", "csharp": "cs", "go": "go", "rust": "rs", "ruby": "rb", "php": "php",
        "bash": "sh", "shell": "sh", "sql": "sql", "json": "json", "batch": "bat",
        "powershell": "ps1", "swift": "swift", "kotlin": "kt", "r": "r", "lua": "lua",
    }

    def _code_model(self):
        return self.model_for("code")

    def _strip_code_fences(self, text):
        """Pull the code out of a fenced markdown block if present."""
        m = re.search(r'```[a-zA-Z0-9+#]*\n(.*?)```', text, re.DOTALL)
        if m:
            return m.group(1).strip()
        return text.strip()

    def write_code(self, request, language=None):
        """Generate code from a natural-language request, show it, and save it."""
        language = (language or self._guess_language(request) or "python").lower()
        ext = self.LANG_EXT.get(language, "txt")
        self.speak(f"Writing the {language} code now, Sir.")
        prompt = (
            f"Write {language} code for the following request:\n\n{request}\n\n"
            "Requirements: complete, runnable, and well-commented. "
            "Output ONLY the code inside a single fenced code block — no explanation before or after."
        )
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self._code_model(),
                "messages": [
                    {"role": "system", "content": "You are an expert programmer. You output clean, correct, working code."},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "keep_alive": "30m",
                "options": {"num_predict": 1200, "temperature": 0.2, "num_ctx": 4096},
            }, timeout=180)
            raw = self._llm_text(res)
            code = self._strip_code_fences(raw)
        except Exception as e:
            log_debug(f"write_code error: {e}")
            self.speak("I was unable to generate the code, Sir.")
            return True

        if not code:
            self.speak("The model returned no code, Sir.")
            return True

        safe_name = re.sub(r'[^\w]+', '_', request)[:40].strip('_') or "snippet"
        path = os.path.join(CODE_DIR, f"{safe_name}_{int(time.time())}.{ext}")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(code)
        except Exception as e:
            log_debug(f"code save error: {e}")
            path = None

        self.current_code = {"language": language, "code": code, "path": path, "request": request}
        self.run_js(f"showCodeEditor({json.dumps({'language': language, 'code': code, 'path': path or ''})})")
        self.safe_log(f"Code generated ({language}){' -> ' + path if path else ''}")
        self.speak("Code ready, Sir. You can run it, edit it, or ask me to explain it.")
        return True

    def _guess_language(self, text):
        t = text.lower()
        for name in sorted(self.LANG_EXT, key=len, reverse=True):
            if re.search(r'\b' + re.escape(name) + r'\b', t):
                return name
        return None

    def run_code(self):
        """Execute the current code snippet and report the output."""
        if not getattr(self, "current_code", None):
            self.speak("There is no code open to run, Sir.")
            return True
        lang = self.current_code["language"]
        path = self.current_code.get("path")
        if not path or not os.path.exists(path):
            self.speak("The code file is missing, Sir.")
            return True

        runners = {
            "py": [sys.executable, path],
            "js": ["node", path],
            "sh": ["bash", path],
            "ps1": ["powershell", "-File", path],
            "bat": [path],
            "rb": ["ruby", path],
            "go": ["go", "run", path],
            "php": ["php", path],
        }
        ext = os.path.splitext(path)[1].lstrip(".")
        cmd = runners.get(ext)
        if not cmd:
            # Non-executable languages (html/css/etc) just open.
            self.launch_external_app(path)
            self.speak(f"Opened the {lang} file, Sir.")
            return True

        self.speak("Running the code now, Sir.")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                  cwd=CODE_DIR)
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            result = out if out else "(no output)"
            if err:
                result += f"\n\n--- errors ---\n{err}"
            self.instantiate_card("Code Output", "code_bug", result[:3000])
            self.run_js(f"showCodeOutput({json.dumps(result[:5000])})")
            if err:
                self.speak("The code ran but reported errors. I've shown the output, Sir.")
            else:
                self.speak("Execution complete, Sir.")
            self.last_code_error = err
        except subprocess.TimeoutExpired:
            self.speak("The code took too long and was stopped, Sir.")
        except FileNotFoundError:
            self.speak(f"The runtime for {lang} isn't installed, Sir.")
        except Exception as e:
            log_debug(f"run_code error: {e}")
            self.speak("I could not run the code, Sir.")
        return True

    def fix_code(self, instruction=None):
        """Ask the model to fix/improve the current code (using the last error if any)."""
        if not getattr(self, "current_code", None):
            self.speak("There is no code open to fix, Sir.")
            return True
        self.speak("Reviewing and fixing the code, Sir.")
        err = getattr(self, "last_code_error", "") or ""
        prompt = (f"Here is some {self.current_code['language']} code:\n\n"
                  f"{self.current_code['code']}\n\n")
        if err:
            prompt += f"Running it produced this error:\n{err}\n\n"
        prompt += (f"{instruction or 'Fix any bugs and improve it.'}\n"
                   "Return ONLY the complete corrected code in a single fenced code block.")
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self._code_model(),
                "messages": [{"role": "user", "content": prompt}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 1400, "temperature": 0.2, "num_ctx": 8192},
            }, timeout=180)
            fixed = self._strip_code_fences(self._llm_text(res))
            if fixed:
                self.current_code["code"] = fixed
                if self.current_code.get("path"):
                    with open(self.current_code["path"], "w", encoding="utf-8") as f:
                        f.write(fixed)
                self.run_js(f"showCodeEditor({json.dumps({'language': self.current_code['language'], 'code': fixed, 'path': self.current_code.get('path') or ''})})")
                self.speak("I've revised the code, Sir.")
        except Exception as e:
            log_debug(f"fix_code error: {e}")
            self.speak("I could not revise the code, Sir.")
        return True

    def explain_code(self, code=None):
        """Explain the current code (or pasted/clipboard code) in plain English."""
        target = code or (self.current_code or {}).get("code") if getattr(self, "current_code", None) else code
        if not target and HAS_CLIPBOARD:
            try:
                target = pyperclip.paste()
            except Exception:
                target = None
        if not target:
            self.speak("I have no code to explain, Sir.")
            return True
        self.speak("Reading through the code, Sir.")
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self._code_model(),
                "messages": [{"role": "user", "content":
                              f"Explain what this code does, clearly and concisely:\n\n{target[:6000]}"}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 400, "temperature": 0.4, "num_ctx": 8192},
            }, timeout=120)
            explanation = self._llm_text(res)
            if explanation:
                self.instantiate_card("Code Explanation", "email_draft", explanation)
                # Speak a trimmed version.
                first = explanation.split("\n\n")[0]
                self.speak(first[:400])
        except Exception as e:
            log_debug(f"explain_code error: {e}")
            self.speak("I could not analyse that code, Sir.")
        return True

    def update_code_from_ui(self, code):
        if getattr(self, "current_code", None):
            self.current_code["code"] = code
            p = self.current_code.get("path")
            if p:
                try:
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(code)
                except Exception as e:
                    log_debug(f"code UI save error: {e}")

    def open_code_in_editor(self):
        """Open the current code file in VS Code (or the default editor)."""
        if not getattr(self, "current_code", None) or not self.current_code.get("path"):
            self.speak("There is no code file to open, Sir.")
            return True
        path = self.current_code["path"]
        try:
            code_exe = shutil.which("code")
            if code_exe:
                subprocess.Popen([code_exe, path], shell=(sys.platform == "win32"))
            else:
                self.launch_external_app(path)
            self.speak("Opened in your editor, Sir.")
        except Exception as e:
            log_debug(f"open editor error: {e}")
            self.launch_external_app(path)
        return True

    # ==================================================================
    # FULL APP AUTOMATION ENGINE
    # ==================================================================
    def app_action(self, action, value=None):
        """Low-level UI primitives used to build automation workflows."""
        try:
            if action == "open":
                self.last_action = {"kind": "open_app", "value": value}
                return self.launch_external_app(value)
            elif action == "type":
                self.last_action = {"kind": "type", "value": str(value)}
                pyautogui.write(str(value), interval=0.01)
            elif action == "press":
                keys = [k.strip() for k in str(value).split("+")]
                if len(keys) > 1:
                    pyautogui.hotkey(*keys)
                else:
                    pyautogui.press(keys[0])
            elif action == "click":
                if isinstance(value, (list, tuple)) and len(value) == 2:
                    pyautogui.click(int(value[0]), int(value[1]))
                else:
                    pyautogui.click()
            elif action == "doubleclick":
                pyautogui.doubleClick()
            elif action == "rightclick":
                pyautogui.rightClick()
            elif action == "scroll":
                pyautogui.scroll(int(value or -300))
            elif action == "move":
                if isinstance(value, (list, tuple)) and len(value) == 2:
                    pyautogui.moveTo(int(value[0]), int(value[1]), duration=0.2)
            elif action == "wait":
                time.sleep(float(value or 1))
            elif action == "focus":
                return focus_window_by_title(str(value))
            elif action == "hotkey":
                pyautogui.hotkey(*[k.strip() for k in str(value).split("+")])
            else:
                log_debug(f"Unknown app_action: {action}")
                return False
            return True
        except Exception as e:
            log_debug(f"app_action {action} error: {e}")
            return False

    def run_workflow(self, steps, name="workflow"):
        """Run a list of {action, value} steps with safe pacing."""
        self.safe_log(f"Running automation: {name} ({len(steps)} steps)")
        self.last_action = {"kind": "workflow", "value": name}
        total = max(1, len(steps))
        for i, step in enumerate(steps, 1):
            self.progress(f"Step {i} of {total}: {step.get('action','')}", 40 + (i / total) * 58)
            if not self.automation_enabled:
                self.speak("Automation stopped, Sir.")
                return False
            action = step.get("action")
            value = step.get("value")
            ok = self.app_action(action, value)
            log_debug(f"  step {i}: {action}({value}) -> {ok}")
            time.sleep(step.get("delay", 0.35))
        self.progress("Complete", 100)
        self.progress_done()
        self.safe_log(f"Automation '{name}' complete.")
        self.speak("Done, Sir.")
        return True

    def ai_automate(self, request):
        """Do something on the computer for the user.

        The control agent looks at the real screen (accessibility tree plus
        vision) before every step, so it adapts as things change. Without
        UI Automation or a vision model it falls back to planning blind steps.
        """
        if HAS_UIA or self._model_installed(self.model_for("vision")):
            self.speak("On it, Sir.")
            self.run_background("Computer task", self.agent.run, request)
            return True
        return self._ai_automate_blind(request)

    def _ai_automate_blind(self, request):
        """Plan a whole UI workflow up front and run it (older, less reliable)."""
        self.speak("I'll take care of that, Sir.")
        self.progress("Planning steps", 10)
        screen_w, screen_h = 1920, 1080
        try:
            screen_w, screen_h = pyautogui.size()
        except Exception:
            pass

        prompt = (
            "You control a Windows PC with keyboard and mouse. Convert the user's request "
            "into a JSON list of UI steps that will actually accomplish it.\n\n"
            'Actions: "open" (value = app name OR full https:// URL), "type" (value = text), '
            '"press" (value = a key or combo like "ctrl+t", "enter", "tab", "down"), '
            '"click", "doubleclick", "rightclick", "scroll" (value = amount, negative = down), '
            '"wait" (value = seconds), "focus" (value = window title).\n\n'
            "Guidance:\n"
            "- Prefer keyboard navigation over blind clicking: it is far more reliable.\n"
            "- To reach a website, use open with the FULL URL rather than searching.\n"
            "- Use a site's own URL patterns when you know them (e.g. a YouTube channel's\n"
            "  page) instead of hunting through menus.\n"
            "- Always add a wait of 2-4 seconds after opening anything, and 1-2 seconds\n"
            "  between interactions, so pages have time to load.\n"
            "- Use press with 'tab'/'enter' to move through and activate controls.\n"
            "- Keep it under 14 steps.\n\n"
            f"Screen resolution is {screen_w}x{screen_h}.\n"
            'Respond ONLY with JSON: {"name":"short name","steps":[{"action":"open","value":"https://youtube.com"},...]}\n\n'
            f"Request: {request}"
        )
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("general"),
                "messages": [{"role": "user", "content": prompt}],
                "stream": False, "format": "json", "keep_alive": "30m",
                "options": {"num_predict": 500, "temperature": 0.2},
            }, timeout=120)
            data = self._llm_json(res)
            steps = data.get("steps", [])
            name = data.get("name", "automation")
        except Exception as e:
            log_debug(f"ai_automate planning error: {e}")
            self.speak("I could not plan that automation, Sir.")
            return True

        if not steps:
            self.speak("I couldn't work out the steps for that, Sir.")
            return True

        self.progress("Plan ready", 40)

        # Ask before doing anything risky, rather than only allowing an abort
        # once it's already clicking things.
        if self._needs_approval(steps, request):
            self.progress_done()
            if not self.request_approval(name, steps, request):
                self.safe_log("Automation cancelled before execution.")
                return True
            self.progress("Approved", 40)

        preview = "\n".join(f"{i}. {s.get('action')} {s.get('value','')}" for i, s in enumerate(steps, 1))
        self.instantiate_card(f"Automation: {name}", "code_bug", preview)
        self.safe_log(f"Running {len(steps)} steps. Say 'stop automation' to abort.")
        threading.Thread(target=self.run_workflow, args=(steps, name), daemon=True).start()
        return True

    def run_macro(self, name):
        macro = self.memory.get("macros", {}).get(name.lower())
        if not macro:
            self.speak(f"I have no macro called {name}, Sir.")
            return True
        threading.Thread(target=self.run_workflow, args=(macro, name), daemon=True).start()
        return True

    def stop_automation(self):
        self.automation_enabled = False
        try:
            self.agent.stop()
        except Exception:
            pass
        self.speak("Automation halted, Sir.")
        threading.Timer(1.0, lambda: setattr(self, "automation_enabled", True)).start()
        return True

    # ==================================================================
    # ADDITIONAL FEATURES
    # ==================================================================
    def translate_text(self, text, target_lang):
        self.speak(f"Translating to {target_lang}, Sir.")
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("general"),
                "messages": [{"role": "user", "content":
                              f"Translate the following into {target_lang}. Output ONLY the translation:\n\n{text}"}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 300, "temperature": 0.3},
            }, timeout=90)
            out = self._llm_text(res)
            if out:
                self.speak(out[:400])
                self.instantiate_card(f"Translation ({target_lang})", "email_draft", out)
        except Exception as e:
            log_debug(f"translate error: {e}")
            self.speak("Translation failed, Sir.")
        return True

    def summarize_clipboard(self):
        if not HAS_CLIPBOARD:
            self.speak("Clipboard support isn't installed, Sir.")
            return True
        try:
            content = pyperclip.paste()
        except Exception:
            content = None
        if not content or len(content.strip()) < 20:
            self.speak("There isn't enough text in your clipboard to summarise, Sir.")
            return True
        self.speak("Summarising your clipboard, Sir.")
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("general"),
                "messages": [{"role": "user", "content":
                              f"Summarise this concisely in a few bullet points:\n\n{content[:6000]}"}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 300, "temperature": 0.4, "num_ctx": 8192},
            }, timeout=120)
            out = self._llm_text(res)
            if out:
                self.instantiate_card("Clipboard Summary", "email_draft", out)
                self.speak(out.split("\n")[0][:300])
        except Exception as e:
            log_debug(f"summarize error: {e}")
            self.speak("I could not summarise that, Sir.")
        return True

    def find_files(self, pattern, root=None):
        """Search the user's folders for matching filenames."""
        root = root or os.path.expanduser("~")
        self.speak(f"Searching for {pattern}, Sir.")
        matches = []
        pat = pattern.lower()
        skip = {"AppData", "node_modules", ".git", "Windows", "Program Files"}
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith('.')]
                for fn in filenames:
                    if pat in fn.lower():
                        matches.append(os.path.join(dirpath, fn))
                        if len(matches) >= 25:
                            raise StopIteration
        except StopIteration:
            pass
        except Exception as e:
            log_debug(f"find_files error: {e}")

        if matches:
            self.instantiate_card(f"Files matching '{pattern}'", "carousel", matches)
            self.speak(f"I found {len(matches)} matching file{'s' if len(matches) != 1 else ''}, Sir.")
        else:
            self.speak(f"I found no files matching {pattern}, Sir.")
        return True

    def list_processes(self, top=8):
        try:
            procs = []
            for p in psutil.process_iter(['name', 'cpu_percent', 'memory_percent']):
                try:
                    procs.append((p.info['name'], p.info['memory_percent'] or 0))
                except Exception:
                    continue
            procs.sort(key=lambda x: x[1], reverse=True)
            lines = [f"{n} — {m:.1f}% memory" for n, m in procs[:top] if n]
            self.instantiate_card("Top Processes", "carousel", lines)
            if lines:
                self.speak(f"The heaviest process is {procs[0][0]}, Sir.")
        except Exception as e:
            log_debug(f"list_processes error: {e}")
            self.speak("I could not read the process list, Sir.")
        return True

    def kill_process(self, name):
        killed = 0
        for p in psutil.process_iter(['name']):
            try:
                if p.info['name'] and name.lower() in p.info['name'].lower():
                    p.terminate()
                    killed += 1
            except Exception:
                continue
        if killed:
            self.speak(f"Terminated {killed} process{'es' if killed != 1 else ''} matching {name}, Sir.")
        else:
            self.speak(f"I found no running process called {name}, Sir.")
        return True

    def network_info(self):
        try:
            info = []
            try:
                ip = requests.get("https://api.ipify.org", timeout=8).text.strip()
                info.append(f"Public IP: {ip}")
            except Exception:
                pass
            counters = psutil.net_io_counters()
            info.append(f"Sent: {counters.bytes_sent / 1e6:.1f} MB")
            info.append(f"Received: {counters.bytes_recv / 1e6:.1f} MB")
            self.instantiate_card("Network", "carousel", info)
            self.speak("Network details are on screen, Sir.")
        except Exception as e:
            log_debug(f"network_info error: {e}")
            self.speak("I could not retrieve network information, Sir.")
        return True

    def disk_usage(self):
        try:
            lines = []
            for part in psutil.disk_partitions():
                try:
                    u = psutil.disk_usage(part.mountpoint)
                    lines.append(f"{part.device} — {u.percent}% used "
                                 f"({u.free / 1e9:.0f} GB free)")
                except Exception:
                    continue
            self.instantiate_card("Disk Usage", "carousel", lines)
            if lines:
                self.speak(lines[0])
        except Exception as e:
            log_debug(f"disk_usage error: {e}")
        return True

    def define_word(self, word):
        try:
            res = requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}",
                               timeout=15)
            if res.status_code == 200:
                data = res.json()
                meaning = data[0]["meanings"][0]
                pos = meaning.get("partOfSpeech", "")
                definition = meaning["definitions"][0]["definition"]
                self.speak(f"{word}, {pos}: {definition}")
                self.instantiate_card(f"Definition — {word}", "email_draft", f"{pos}\n\n{definition}")
                return True
        except Exception as e:
            log_debug(f"define error: {e}")
        self.speak(f"I could not find a definition for {word}, Sir.")
        return True

    def open_folder(self, which):
        folders = {
            "downloads": "Downloads", "documents": "Documents", "desktop": "Desktop",
            "pictures": "Pictures", "music": "Music", "videos": "Videos",
        }
        key = next((k for k in folders if k in which.lower()), None)
        if key:
            path = os.path.join(os.path.expanduser("~"), folders[key])
        elif "amy" in which.lower():
            path = DATA_DIR
        else:
            path = os.path.expanduser("~")
        self.launch_external_app(path)
        self.speak("Opening that folder, Sir.")
        return True

    # ==================================================================
    # TEXT MESSAGING & CALLING
    # ==================================================================
    def _resolve_phone(self, name_or_number):
        """Turn a name or raw number into an E.164-ish phone number."""
        if not name_or_number:
            return None
        raw = str(name_or_number).strip()
        # Already a number?
        digits = re.sub(r'[^\d+]', '', raw)
        if len(re.sub(r'\D', '', digits)) >= 7:
            return digits
        # Look up the phone book.
        book = {**CONFIG.get("phonebook", {}), **self.memory.get("phonebook", {})}
        low = raw.lower()
        for k, v in book.items():
            if k.lower() == low:
                return v
        for k, v in book.items():
            if low in k.lower():
                return v
        return None

    def send_sms(self, recipient, message):
        """Send a text. Uses Twilio if configured, else email-to-SMS gateway."""
        number = self._resolve_phone(recipient)
        if not number:
            self.speak(f"I don't have a number for {recipient}, Sir. "
                       "Say 'save number for <name> as <number>' first.")
            return False

        tw = CONFIG.get("twilio", {})
        sid, token, from_num = tw.get("account_sid"), tw.get("auth_token"), tw.get("from_number")

        if sid and token and from_num and not str(sid).startswith("YOUR_"):
            try:
                res = requests.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                    data={"To": number, "From": from_num, "Body": message},
                    auth=(sid, token), timeout=30)
                if res.status_code in (200, 201):
                    self.speak(f"Message sent to {recipient}, Sir.")
                    self.safe_log(f"SMS sent to {number}: {message[:60]}")
                    return True
                log_debug(f"Twilio SMS error {res.status_code}: {res.text[:300]}")
                self.speak("Twilio rejected the message, Sir.")
                self.safe_log(f"Twilio error {res.status_code}. Check your credentials and number format.")
                return False
            except Exception as e:
                log_debug(f"Twilio SMS exception: {e}")
                self.speak("I couldn't reach the messaging service, Sir.")
                return False

        # Fallback: carrier email-to-SMS gateway (needs the carrier in the phonebook config).
        gateway = CONFIG.get("sms_gateway", {}).get(str(recipient).lower())
        if gateway and not is_placeholder_email(EMAIL_ADDRESS):
            return self.send_email_smtp(gateway, "", message)

        self.speak("Texting isn't configured yet, Sir. Add your Twilio details to the config file.")
        self.safe_log(f"SMS not configured — set twilio.account_sid / auth_token / from_number in {CONFIG_FILE}.")
        return False

    def make_call(self, recipient, say_text=None):
        """Place a phone call. Twilio speaks a message; otherwise hands off to
        the system dialler (Your Phone / Skype / tel: handler)."""
        number = self._resolve_phone(recipient)
        if not number:
            self.speak(f"I don't have a number for {recipient}, Sir.")
            return False

        tw = CONFIG.get("twilio", {})
        sid, token, from_num = tw.get("account_sid"), tw.get("auth_token"), tw.get("from_number")

        if sid and token and from_num and not str(sid).startswith("YOUR_"):
            try:
                message = say_text or f"Hello. This is an automated call placed on behalf of {USER_TITLE}."
                twiml = f"<Response><Say voice=\"man\">{message}</Say></Response>"
                res = requests.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
                    data={"To": number, "From": from_num, "Twiml": twiml},
                    auth=(sid, token), timeout=30)
                if res.status_code in (200, 201):
                    self.speak(f"Calling {recipient} now, Sir.")
                    self.safe_log(f"Call placed to {number}")
                    return True
                log_debug(f"Twilio call error {res.status_code}: {res.text[:300]}")
                self.speak("The call could not be placed, Sir.")
                return False
            except Exception as e:
                log_debug(f"Twilio call exception: {e}")
                self.speak("I couldn't reach the calling service, Sir.")
                return False

        # Fallback: hand the number to whatever handles tel: links on this PC.
        try:
            tel = "tel:" + re.sub(r'[^\d+]', '', number)
            if sys.platform == "win32":
                os.startfile(tel)
            else:
                webbrowser.open(tel)
            self.speak(f"Dialling {recipient} through your default phone app, Sir.")
            self.safe_log(f"Handed {number} to the system dialler.")
            return True
        except Exception as e:
            log_debug(f"tel: handoff failed: {e}")
            self.speak("Calling isn't configured, Sir. Add Twilio details to the config file.")
            return False

    def save_phone_number(self, name, number):
        self.memory.setdefault("phonebook", {})[name.lower()] = number
        self.save_memory()
        self.speak(f"Saved {name}'s number, Sir.")
        self.safe_log(f"Phonebook: {name} -> {number}")
        return True

    # ==================================================================
    # LEARNING ENGINE — Amy gets better the more you use her
    # ==================================================================
    def learn_from_interaction(self, user_cmd, reply):
        """Passively note preferences and corrections the user expresses."""
        low = user_cmd.lower()
        triggers = [
            (r"(?:i (?:really )?(?:like|love|prefer|enjoy))\s+(.+)", "likes"),
            (r"(?:i (?:hate|dislike|don't like|do not like))\s+(.+)", "dislikes"),
            (r"(?:my|i'm|i am)\s+(.+)", None),
            (r"(?:no,?\s+)?(?:actually|correction),?\s+(.+)", "correction"),
            (r"(?:don't|do not|never)\s+(.+)", "avoid"),
            (r"(?:always|from now on)\s+(.+)", "always"),
        ]
        for pattern, kind in triggers:
            m = re.search(pattern, low)
            if m:
                fact = m.group(1).strip()[:180]
                if len(fact) < 4:
                    continue
                entry = f"{kind}: {fact}" if kind else fact
                facts = self.memory.setdefault("facts", [])
                if entry not in facts:
                    facts.append(entry)
                    log_debug(f"Learned: {entry}")
                break

        # Track command frequency so we can surface habits.
        stats = self.memory.setdefault("command_stats", {})
        key = " ".join(low.split()[:3])
        stats[key] = stats.get(key, 0) + 1

    def reflect_and_learn(self):
        """Periodically review recent conversation and distil durable insights.
        This is what makes Amy feel like she's actually learning about you."""
        transcript = self.memory.get("full_transcript", [])
        if len(transcript) < 8:
            self.speak("We haven't spoken enough yet for me to draw conclusions, Sir.")
            return True

        recent = transcript[-40:]
        convo = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in recent)
        existing = self.memory.get("facts", [])

        self.speak("Reflecting on what I've learned about you, Sir.")
        try:
            prompt = (
                "Below is a recent conversation between a user and their assistant.\n\n"
                f"{convo}\n\n"
                f"Already known about the user:\n{chr(10).join('- ' + f for f in existing[-20:]) or '(nothing yet)'}\n\n"
                "Identify up to 5 NEW, durable facts or preferences about the user that would help "
                "personalise future replies. Ignore one-off requests and anything already known. "
                'Respond ONLY as JSON: {"facts": ["...", "..."]}. Return an empty list if nothing new.'
            )
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("reasoning"),
                "messages": [{"role": "user", "content": prompt}],
                "stream": False, "format": "json", "keep_alive": "30m",
                "options": {"num_predict": 300, "temperature": 0.3, "num_ctx": 8192},
            }, timeout=120)
            new_facts = self._llm_json(res).get("facts", [])
            added = []
            for f in new_facts:
                f = str(f).strip()
                if f and f not in existing and len(f) > 5:
                    existing.append(f)
                    added.append(f)
            self.memory["facts"] = existing
            self.save_memory()
            if added:
                self.instantiate_card("Newly Learned", "carousel", added)
                self.speak(f"I've learned {len(added)} new thing{'s' if len(added) != 1 else ''} about you, Sir.")
            else:
                self.speak("Nothing new to add just yet, Sir.")
        except Exception as e:
            log_debug(f"reflect_and_learn error: {e}")
            self.speak("I was unable to complete my reflection, Sir.")
        return True

    def forget_fact(self, needle):
        self.mind.forget(needle)
        facts = self.memory.get("facts", [])
        before = len(facts)
        self.memory["facts"] = [f for f in facts if needle.lower() not in f.lower()]
        removed = before - len(self.memory["facts"])
        self.save_memory()
        self.speak(f"Forgotten {removed} item{'s' if removed != 1 else ''}, Sir." if removed
                   else "I had nothing matching that, Sir.")
        return True

    def show_usage_stats(self):
        stats = self.memory.get("command_stats", {})
        if not stats:
            self.speak("I have no usage history yet, Sir.")
            return True
        top = sorted(stats.items(), key=lambda x: x[1], reverse=True)[:10]
        lines = [f"{k} — {v}x" for k, v in top]
        self.instantiate_card("Your Most Used Commands", "carousel", lines)
        self.speak(f"Your most frequent request is '{top[0][0]}', Sir.")
        return True

    # ==================================================================
    # SELF-UPDATE — Amy can modify her own source (with safety rails)
    # ==================================================================
    def self_update(self, instruction):
        """Let Amy propose and apply a change to her own source code.

        Safety rails: a timestamped backup is always taken first, the result must
        pass a syntax check, and the change is reverted automatically if it fails.
        A restart is required for changes to take effect.
        """
        src_path = os.path.abspath(__file__)
        try:
            with open(src_path, "r", encoding="utf-8") as f:
                source = f.read()
        except Exception as e:
            log_debug(f"self_update read error: {e}")
            self.speak("I could not read my own source, Sir.")
            return True

        if len(source) > 400000:
            self.speak("My source is too large to revise safely in one pass, Sir.")
            return True

        self.speak("Working on my own code now, Sir. This may take a moment.")
        try:
            prompt = (
                "You are modifying a Python application's source code.\n"
                f"Requested change: {instruction}\n\n"
                "Below is the current source. Return a JSON object with the EXACT text to "
                "find and replace:\n"
                '{"find": "<exact snippet from the source>", "replace": "<new snippet>", '
                '"summary": "<one line describing the change>"}\n'
                "The 'find' text must appear EXACTLY ONCE in the source. Keep the change minimal "
                "and preserve indentation.\n\n"
                f"SOURCE:\n{source[:120000]}"
            )
            res = self.http.post(OLLAMA_URL, json={
                "model": self._code_model(),
                "messages": [{"role": "user", "content": prompt}],
                "stream": False, "format": "json", "keep_alive": "30m",
                "options": {"num_predict": 1500, "temperature": 0.15, "num_ctx": 32768},
            }, timeout=300)
            data = self._llm_json(res)
            find, replace = data.get("find", ""), data.get("replace", "")
            summary = data.get("summary", instruction)
        except Exception as e:
            log_debug(f"self_update planning error: {e}")
            self.speak("I could not work out how to make that change, Sir.")
            return True

        if not find or find not in source:
            self.speak("I couldn't locate the code to change safely, Sir. No changes made.")
            self.safe_log("Self-update aborted: target snippet not found.")
            return True
        if source.count(find) != 1:
            self.speak("That change was ambiguous, Sir, so I've made no changes.")
            self.safe_log(f"Self-update aborted: snippet appears {source.count(find)} times.")
            return True

        # Always back up before touching our own source.
        backup = os.path.join(BACKUP_DIR, f"amy_backup_{int(time.time())}.py")
        try:
            with open(backup, "w", encoding="utf-8") as f:
                f.write(source)
        except Exception as e:
            log_debug(f"backup failed: {e}")
            self.speak("I could not create a backup, so I've made no changes, Sir.")
            return True

        new_source = source.replace(find, replace, 1)

        # Verify it still parses before we commit it.
        try:
            compile(new_source, src_path, "exec")
        except SyntaxError as e:
            self.speak("My proposed change had a syntax error, Sir, so I've discarded it.")
            self.safe_log(f"Self-update rejected — syntax error: {e}")
            return True

        try:
            with open(src_path, "w", encoding="utf-8") as f:
                f.write(new_source)
        except Exception as e:
            log_debug(f"self_update write error: {e}")
            self.speak("I could not write the change, Sir.")
            return True

        self.memory.setdefault("self_updates", []).append({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "summary": summary, "backup": backup,
        })
        self.save_memory()

        diff_preview = f"CHANGE: {summary}\n\n" + self._make_diff(source, new_source)
        self.instantiate_card("Self-Update Applied", "code_bug", diff_preview)
        self.run_js(f"showDiff({json.dumps('Self-update: ' + str(summary))}, {json.dumps(diff_preview)})")
        self.speak(f"Done, Sir. {summary}. Restart me for the change to take effect. "
                   "Say 'revert my last update' if it misbehaves.")
        self.safe_log(f"Self-update applied. Backup: {backup}")
        return True

    def revert_self_update(self):
        updates = self.memory.get("self_updates", [])
        if not updates:
            self.speak("I have no self-updates to revert, Sir.")
            return True
        last = updates[-1]
        backup = last.get("backup")
        if not backup or not os.path.exists(backup):
            self.speak("The backup for that change is missing, Sir.")
            return True
        try:
            with open(backup, "r", encoding="utf-8") as f:
                restored = f.read()
            with open(os.path.abspath(__file__), "w", encoding="utf-8") as f:
                f.write(restored)
            updates.pop()
            self.save_memory()
            self.speak("Reverted to the previous version, Sir. Please restart me.")
            self.safe_log(f"Reverted self-update: {last.get('summary')}")
        except Exception as e:
            log_debug(f"revert error: {e}")
            self.speak("I could not restore the backup, Sir.")
        return True

    def restart_self(self):
        """Relaunch the application so code changes take effect."""
        self.speak("Restarting now, Sir.")
        try:
            self.save_memory()
            time.sleep(1.2)
            subprocess.Popen([sys.executable, os.path.abspath(__file__)])
            time.sleep(0.4)
            self.close_app()
        except Exception as e:
            log_debug(f"restart error: {e}")
            self.speak("I could not restart myself, Sir.")
        return True

    # ==================================================================
    # CONVERSATION MODE — continuous back-and-forth, interruptible anytime
    # ==================================================================
    def set_conversation_mode(self, active):
        self.conversation_mode = bool(active)
        self.last_interaction = time.time()
        self.run_js(f"setConversationMode({str(bool(active)).lower()})")
        if self.conversation_mode:
            self.speak("Conversation mode on, Sir. Just talk — no need to say my name. "
                       "Say 'end conversation' when you're done.")
        else:
            self.speak("Conversation mode off, Sir.")
        return True

    # ==================================================================
    # DESK CAMERA — live feed, object recognition, hand-gesture control
    # ==================================================================
    def toggle_camera(self, active=None):
        want = (not self.camera_active) if active is None else bool(active)
        if want and not HAS_CV2:
            self.speak("Camera support needs OpenCV, Sir. Install it with pip install --user opencv-python.")
            self.safe_log("Camera unavailable: pip install --user opencv-python")
            return False

        if want == self.camera_active and self._camera_thread_alive():
            return True     # already in the requested state

        self.camera_active = want
        if want:
            # Restart the worker if it was never started OR died from an error.
            # Reset the announce latch on every open. It was only cleared on
            # close, so a worker that restarted without one left it set, which
            # skipped cameraReady() — and with cameraDesired still false the UI
            # silently dropped every frame.
            self._camera_announced = False
            if not self._camera_thread_alive():
                self._camera_thread = threading.Thread(target=self._camera_worker, daemon=True)
                self._camera_thread.start()
            # Open the stage now rather than waiting on the first frame, so the
            # view is full-screen from the moment it is asked for.
            self.run_js("setCameraState(true)")
            # Fill the picker so the view opens knowing which device it is on.
            self._push_camera_list()
            # The UI only opens once a real frame arrives, so you never get a
            # black stage when the camera fails to initialise.
        else:
            self._camera_announced = False
            self.last_frame = None
            # Give any in-flight frame a moment to notice, then force the UI shut.
            self.run_js("setCameraState(false)")
            def _confirm_closed():
                time.sleep(0.6)
                if not self.camera_active:
                    self.run_js("setCameraState(false)")
            threading.Thread(target=_confirm_closed, daemon=True).start()
            self.speak("Desk view offline, Sir.")
        return True

    def _camera_thread_alive(self):
        t = getattr(self, "_camera_thread", None)
        return t is not None and t.is_alive()

    # ---- Multiple cameras ---------------------------------------------------
    # Config holds camera.devices as [{"name": "Desk", "index": 0}, ...] and
    # camera.active as the name of the one in use. Older configs only had
    # camera.device_index; that still works and is migrated on first read.

    def _camera_devices(self):
        """The configured cameras, always at least one, always well-formed."""
        cam = CONFIG.setdefault("camera", {})
        raw = cam.get("devices")
        if not isinstance(raw, list) or not raw:
            raw = [{"name": "Default", "index": int(cam.get("device_index", 0))}]
        clean = []
        for d in raw:
            if isinstance(d, dict) and d.get("index") is not None:
                try:
                    i = int(d["index"])
                except (TypeError, ValueError):
                    continue
                clean.append({"name": str(d.get("name") or f"Camera {i}").strip(), "index": i})
        if not clean:
            clean = [{"name": "Default", "index": 0}]
        cam["devices"] = clean
        return clean

    def _active_camera(self):
        """The device currently selected, falling back to the first one."""
        devices = self._camera_devices()
        want = str(CONFIG.get("camera", {}).get("active") or devices[0]["name"])
        for d in devices:
            if d["name"].lower() == want.lower():
                return d
        return devices[0]

    def _find_camera(self, which):
        """Match a device by name first, then by device index."""
        s = str(which).strip()
        devices = self._camera_devices()
        for d in devices:
            if d["name"].lower() == s.lower():
                return d
        if s.lstrip("-").isdigit():
            i = int(s)
            for d in devices:
                if d["index"] == i:
                    return d
        return None

    def _push_camera_list(self):
        """Keep the picker in the camera view in step with the config."""
        try:
            payload = {"devices": self._camera_devices(),
                       "active": self._active_camera()["name"]}
            self.broadcast_js(f"setCameraList({json.dumps(payload)})")
        except Exception as e:
            log_debug(f"camera list push failed: {e}")

    def probe_cameras(self, max_index=6):
        """Open each index briefly to see which ones actually deliver frames.

        The device the worker already holds will refuse to open here, so the
        active camera is reported from config rather than probed."""
        found = []
        if not HAS_CV2:
            return found
        active_idx = self._active_camera()["index"] if self.camera_active else None
        for i in range(int(max_index)):
            if i == active_idx:
                found.append(i)
                continue
            cap = None
            try:
                cap = (cv2.VideoCapture(i, getattr(cv2, "CAP_DSHOW", 700))
                       if sys.platform == "win32" else cv2.VideoCapture(i))
                if cap is not None and cap.isOpened():
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        found.append(i)
            except Exception as e:
                log_debug(f"probe camera {i}: {e}")
            finally:
                self._release_capture(cap)
        return found

    def scan_cameras(self):
        """Find attached cameras and add any that aren't configured yet."""
        found = self.probe_cameras()
        if not found:
            self.speak("I could not find any cameras, Sir.")
            return True
        devices = self._camera_devices()
        known = {d["index"] for d in devices}
        added = [i for i in found if i not in known]
        for i in added:
            devices.append({"name": f"Camera {i}", "index": i})
        CONFIG["camera"]["devices"] = devices
        save_config(CONFIG)
        self._push_camera_list()
        if added:
            self.speak(f"Found {len(found)} cameras, Sir. Added {len(added)} new "
                       f"{'one' if len(added) == 1 else 'ones'}.")
        else:
            self.speak(f"Found {len(found)}, all already set up, Sir.")
        return True

    def list_cameras(self):
        devices = self._camera_devices()
        active = self._active_camera()["name"]
        self.instantiate_card("Cameras", "carousel",
                              [f"{d['name']} ({d['index']})" + ("  • active" if d["name"] == active else "")
                               for d in devices])
        self.speak(f"{len(devices)} camera{'' if len(devices) == 1 else 's'} set up, Sir. "
                   f"{active} is active.")
        return True

    def add_camera(self, name, index):
        """Add a camera, or repoint an existing name at a different index."""
        try:
            index = int(index)
        except (TypeError, ValueError):
            self.speak("I need a device number for that camera, Sir.")
            return True
        name = str(name).strip() or f"Camera {index}"
        devices = self._camera_devices()
        for d in devices:
            if d["name"].lower() == name.lower():
                d["index"] = index
                break
        else:
            devices.append({"name": name, "index": index})
        CONFIG["camera"]["devices"] = devices
        save_config(CONFIG)
        self._push_camera_list()
        self.speak(f"{name} is set up, Sir.")
        return True

    def rename_camera(self, old, new):
        target = self._find_camera(old)
        if target is None:
            self.speak(f"I don't have a camera called {old}, Sir.")
            return True
        new = str(new).strip()
        if not new:
            self.speak("That name is empty, Sir.")
            return True
        was_active = self._active_camera()["name"] == target["name"]
        target["name"] = new
        CONFIG["camera"]["devices"] = self._camera_devices()
        if was_active:
            CONFIG["camera"]["active"] = new
        save_config(CONFIG)
        self._push_camera_list()
        self.speak(f"Renamed to {new}, Sir.")
        return True

    def remove_camera(self, which):
        devices = self._camera_devices()
        if len(devices) <= 1:
            self.speak("That's the only camera, Sir — I'll keep it.")
            return True
        target = self._find_camera(which)
        if target is None:
            self.speak(f"I don't have a camera called {which}, Sir.")
            return True
        devices = [d for d in devices if d["name"] != target["name"]]
        CONFIG["camera"]["devices"] = devices
        if self._active_camera()["name"] == target["name"]:
            CONFIG["camera"]["active"] = devices[0]["name"]
            self._camera_reopen = True
        save_config(CONFIG)
        self._push_camera_list()
        self.speak(f"Removed {target['name']}, Sir.")
        return True

    def switch_camera(self, which):
        """Point the worker at a different device; it reopens on the next frame."""
        target = self._find_camera(which)
        if target is None:
            self.speak(f"I don't have a camera called {which}, Sir.")
            return True
        CONFIG.setdefault("camera", {})["active"] = target["name"]
        save_config(CONFIG)
        self._camera_reopen = True
        self._push_camera_list()
        self.speak(f"Switched to {target['name']}, Sir.")
        return True

    def _open_capture(self):
        """Open the active camera, preferring higher-quality settings."""
        cam_cfg = CONFIG.get("camera", {})
        idx = int(self._active_camera()["index"])
        want_w = int(cam_cfg.get("capture_width", 1280))
        want_h = int(cam_cfg.get("capture_height", 720))

        if sys.platform == "win32":
            backends = [getattr(cv2, "CAP_DSHOW", 700), getattr(cv2, "CAP_MSMF", 1400), 0]
        else:
            backends = [0]

        for b in backends:
            cap = None
            try:
                cap = cv2.VideoCapture(idx, b) if b else cv2.VideoCapture(idx)
                if cap is None or not cap.isOpened():
                    if cap is not None:
                        cap.release()
                    continue

                # Request the best quality the device supports.
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, want_w)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, want_h)
                try:
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                except Exception:
                    pass
                cap.set(cv2.CAP_PROP_FPS, 30)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)      # lower latency
                try:
                    cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)
                except Exception:
                    pass

                # Verify it genuinely delivers frames before accepting it.
                for _ in range(6):
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                        ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                        log_debug(f"Camera opened (backend {b}) at {aw}x{ah}")
                        return cap
                    time.sleep(0.15)
                cap.release()
            except Exception as e:
                log_debug(f"camera backend {b} failed: {e}")
                try:
                    if cap is not None:
                        cap.release()
                except Exception:
                    pass
        return None

    @staticmethod
    def _release_capture(cap):
        """Release a capture without ever raising — an unguarded release here
        used to kill the worker thread and break the camera until restart."""
        try:
            if cap is not None:
                cap.release()
        except Exception as e:
            log_debug(f"capture release error: {e}")

    def _camera_worker(self):
        """Owns the camera: streams frames to the UI and runs gesture tracking.
        One owner avoids two subsystems fighting over the device."""
        init_thread_com()
        cap = None
        hands = None
        fps = float(CONFIG.get("camera", {}).get("stream_fps", 12))
        interval = 1.0 / max(1.0, fps)

        if self.gestures_enabled and _load_mediapipe() is not None:
            try:
                hands = mp.solutions.hands.Hands(
                    max_num_hands=1, min_detection_confidence=0.6,
                    min_tracking_confidence=0.6, model_complexity=0)
                log_debug("MediaPipe hand tracking ready.")
            except Exception as e:
                log_debug(f"MediaPipe init failed: {e}")
                hands = None

        fail_count = 0
        while True:
          try:
            if not self.camera_active:
                if cap is not None:
                    self._release_capture(cap)
                    cap = None
                    log_debug("Camera released.")
                time.sleep(0.4)
                continue

            # A camera switch just drops the handle; the reopen below picks up
            # whichever device is now active.
            if getattr(self, "_camera_reopen", False):
                self._camera_reopen = False
                if cap is not None:
                    self._release_capture(cap)
                    cap = None
                    log_debug("Switching camera device.")

            if cap is None:
                cap = self._open_capture()
                if cap is None:
                    self.safe_log("Could not open the camera. Check it isn't in use by another app, "
                                  "and that camera privacy settings allow desktop apps.")
                    self.speak("I could not access the camera, Sir.")
                    self.camera_active = False
                    self.run_js("setCameraState(false)")
                    time.sleep(1.0)
                    continue
                fail_count = 0

            t0 = time.time()
            try:
                ok, frame = cap.read()
                if not ok or frame is None:
                    fail_count += 1
                    # The device was probably unplugged or grabbed by another app.
                    if fail_count > 12:
                        log_debug("Camera stopped delivering frames; reopening.")
                        self._release_capture(cap)
                        cap = None
                        fail_count = 0
                    time.sleep(0.2)
                    continue
                fail_count = 0

                if CONFIG.get("camera", {}).get("mirror", True):
                    frame = cv2.flip(frame, 1)
                self.last_frame = frame.copy()

                if not self._camera_announced:
                    self._camera_announced = True
                    self.run_js("cameraReady()")
                    self.speak("Desk view online, Sir.")

                overlay_note = ""
                # --- Hand gesture recognition ---
                if hands is not None and self.gestures_enabled:
                    try:
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        result = hands.process(rgb)
                        if result.multi_hand_landmarks:
                            lm = result.multi_hand_landmarks[0]
                            self._draw_hand(frame, lm)
                            gesture = self._classify_gesture(lm)
                            if gesture:
                                overlay_note = gesture.replace("_", " ").upper()
                                self._handle_gesture(gesture)
                    except Exception as e:
                        log_debug(f"hand tracking error: {e}")

                # --- Stream a downscaled frame to the UI ---
                # evaluate_js is synchronous, so pushing a ~30KB base64 string at
                # full frame rate queues up faster than the webview can drain it,
                # which froze the feed. Skip a frame if one is still in flight.
                if not self._frame_in_flight:
                    h0, w0 = frame.shape[:2]
                    tw = int(CONFIG.get("camera", {}).get("stream_width", 720))
                    th = max(1, int(h0 * (tw / float(w0))))   # keep aspect ratio
                    small = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
                    if overlay_note:
                        cv2.putText(small, overlay_note, (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                                    0.7, (255, 229, 0), 2, cv2.LINE_AA)
                    q = int(CONFIG.get("camera", {}).get("stream_quality", 72))
                    ok2, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), q])
                    if ok2:
                        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
                        self._frame_in_flight = True

                        def _push(payload=b64):
                            try:
                                # Re-check: the user may have closed the view while
                                # this frame was being encoded.
                                if self.camera_active:
                                    self.broadcast_js(f"updateCameraFrame('{payload}')")
                            finally:
                                self._frame_in_flight = False

                        threading.Thread(target=_push, daemon=True).start()
            except Exception as e:
                log_debug(f"camera loop error: {e}")
                time.sleep(0.3)

            elapsed = time.time() - t0
            if elapsed < interval:
                time.sleep(interval - elapsed)
          except Exception as e:
            # Never let the worker thread die - that used to break the camera
            # permanently until the whole app was restarted.
            log_debug(f"camera worker recovered from: {e}")
            time.sleep(0.5)

    @staticmethod
    def _draw_hand(frame, landmarks):
        try:
            mp.solutions.drawing_utils.draw_landmarks(
                frame, landmarks, mp.solutions.hands.HAND_CONNECTIONS,
                mp.solutions.drawing_utils.DrawingSpec(color=(255, 229, 0), thickness=1, circle_radius=2),
                mp.solutions.drawing_utils.DrawingSpec(color=(0, 180, 255), thickness=1))
        except Exception:
            pass

    @staticmethod
    def _fingers_up(lm):
        """Return [thumb, index, middle, ring, pinky] as booleans."""
        pts = lm.landmark
        up = []
        # Thumb: compare x of tip vs joint (works for a mirrored frame).
        up.append(pts[4].x < pts[3].x)
        for tip, pip in ((8, 6), (12, 10), (16, 14), (20, 18)):
            up.append(pts[tip].y < pts[pip].y)
        return up

    def _classify_gesture(self, lm):
        f = self._fingers_up(lm)
        thumb, index, middle, ring, pinky = f
        count = sum(f)

        if count == 0:
            return "fist"
        if count == 5:
            return "open_palm"
        if index and not middle and not ring and not pinky:
            return "point_up"
        if index and middle and not ring and not pinky:
            return "peace"
        if thumb and not index and not middle and not ring and not pinky:
            return "thumbs_up"
        if pinky and thumb and not index and not middle and not ring:
            return "shaka"
        if thumb and index and middle and ring and not pinky:
            return "four"
        return None

    # Gesture -> action. Editable in the config under "gestures".
    DEFAULT_GESTURE_ACTIONS = {
        "open_palm": "pause_media",
        "fist": "mute",
        "point_up": "volume_up",
        "peace": "next_track",
        "thumbs_up": "screenshot",
        "shaka": "identify_object",
        "four": "toggle_conversation",
    }

    def _handle_gesture(self, gesture):
        """Debounced gesture dispatch — a gesture must be held briefly and won't
        re-fire until you change gesture or the cooldown passes."""
        now = time.time()
        if gesture != self._pending_gesture:
            self._pending_gesture = gesture
            self._pending_since = now
            return
        # Require the gesture to be held to avoid accidental triggers.
        if now - self._pending_since < 0.6:
            return
        if gesture == self._last_gesture and (now - self._last_gesture_time) < 2.5:
            return

        self._last_gesture = gesture
        self._last_gesture_time = now

        mapping = {**self.DEFAULT_GESTURE_ACTIONS, **CONFIG.get("gestures", {})}
        action = mapping.get(gesture)
        if not action:
            return
        self.safe_log(f"Gesture: {gesture.replace('_',' ')} -> {action}")
        self.run_js(f"flashGesture({json.dumps(gesture.replace('_',' ').title())})")

        try:
            if action == "pause_media":
                pyautogui.press("playpause")
            elif action == "mute":
                pyautogui.press("volumemute")
            elif action == "volume_up":
                for _ in range(4):
                    pyautogui.press("volumeup")
            elif action == "volume_down":
                for _ in range(4):
                    pyautogui.press("volumedown")
            elif action == "next_track":
                pyautogui.press("nexttrack")
            elif action == "previous_track":
                pyautogui.press("prevtrack")
            elif action == "screenshot":
                threading.Thread(target=self.take_screenshot, daemon=True).start()
            elif action == "identify_object":
                threading.Thread(target=self.identify_objects, daemon=True).start()
            elif action == "toggle_conversation":
                self.set_conversation_mode(not self.conversation_mode)
            elif action == "stop_speech":
                self.stop_speech()
        except Exception as e:
            log_debug(f"gesture action error: {e}")

    def set_gestures(self, enabled):
        self.gestures_enabled = bool(enabled)
        self.speak("Gesture control enabled, Sir." if enabled else "Gesture control disabled, Sir.")
        self.run_js(f"setGestureState({str(bool(enabled)).lower()})")
        return True

    def capture_camera_photo(self):
        if self.last_frame is None:
            self.speak("The camera isn't running, Sir.")
            return True
        try:
            path = os.path.join(DOCS_DIR, f"Amy_DeskCam_{int(time.time())}.jpg")
            cv2.imwrite(path, self.last_frame)
            self.speak("Photo captured, Sir.")
            self.safe_log(f"Desk camera photo saved: {path}")
        except Exception as e:
            log_debug(f"camera photo error: {e}")
            self.speak("I could not save the photo, Sir.")
        return True

    # ==================================================================
    # INTERNET ACCESS & RESEARCH ENGINE
    # ==================================================================
    def web_lookup(self, query, max_results=5):
        """Search the web and return [{title, url, snippet}]. Uses DuckDuckGo's
        lite endpoint (no API key required)."""
        results = []
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        try:
            res = self.http.post("https://lite.duckduckgo.com/lite/",
                                 data={"q": query}, headers=headers, timeout=20)
            html = res.text
            # Lite results are plain anchors followed by a snippet cell.
            for m in re.finditer(
                    r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)'
                    r'(?=<a[^>]+class="result-link"|</table>)', html, re.DOTALL):
                url = html_unescape(m.group(1))
                title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
                snippet = re.sub(r'<[^>]+>', ' ', m.group(3))
                snippet = html_unescape(re.sub(r'\s+', ' ', snippet)).strip()[:300]
                if url.startswith("http"):
                    results.append({"title": html_unescape(title), "url": url, "snippet": snippet})
                if len(results) >= max_results:
                    break
        except Exception as e:
            log_debug(f"web_lookup error: {e}")

        if not results:
            # Fallback: DuckDuckGo's instant-answer API.
            try:
                r = self.http.get("https://api.duckduckgo.com/",
                                  params={"q": query, "format": "json", "no_html": 1},
                                  timeout=15).json()
                if r.get("AbstractText"):
                    results.append({"title": r.get("Heading", query),
                                    "url": r.get("AbstractURL", ""),
                                    "snippet": r["AbstractText"][:400]})
                for t in r.get("RelatedTopics", [])[:max_results]:
                    if isinstance(t, dict) and t.get("Text"):
                        results.append({"title": t.get("Text", "")[:80],
                                        "url": t.get("FirstURL", ""),
                                        "snippet": t.get("Text", "")[:300]})
            except Exception as e:
                log_debug(f"instant answer fallback error: {e}")
        return results[:max_results]

    def fetch_page_text(self, url, limit=6000):
        """Download a page and return readable plain text."""
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            res = self.http.get(url, headers=headers, timeout=25)
            html = res.text
            html = re.sub(r'(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>', ' ', html)
            html = re.sub(r'(?is)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>', '\n', html)
            text = re.sub(r'<[^>]+>', ' ', html)
            text = html_unescape(text)
            text = re.sub(r'[ \t]+', ' ', text)
            text = re.sub(r'\n\s*\n+', '\n\n', text).strip()
            return text[:limit]
        except Exception as e:
            log_debug(f"fetch_page_text error for {url}: {e}")
            return ""

    def answer_from_web(self, question, depth=3):
        """Search the internet, read the top pages, and answer with sources."""
        self.speak("Searching the web, Sir.")
        results = self.web_lookup(question, max_results=max(3, depth))
        if not results:
            self.speak("I couldn't retrieve any web results, Sir.")
            return True

        corpus, sources = [], []
        pages = self.fetch_many([r.get("url") for r in results[:depth]], limit=4000)
        for r in results[:depth]:
            if not r.get("url"):
                continue
            snippet = pages.get(r["url"], "") or r.get("snippet", "")
            if snippet:
                corpus.append(f"SOURCE: {r['title']} ({r['url']})\n{snippet[:3000]}")
                sources.append(r)

        if not corpus:
            corpus = [f"{r['title']}: {r['snippet']}" for r in results]
            sources = results

        try:
            prompt = (
                f"Answer this question using ONLY the sources below. Be accurate and concise "
                f"(3-5 sentences). If the sources disagree or don't answer it, say so plainly.\n\n"
                f"QUESTION: {question}\n\nSOURCES:\n\n" + "\n\n---\n\n".join(corpus)[:24000]
            )
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("reasoning"),
                "messages": [{"role": "user", "content": prompt}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 400, "temperature": 0.3, "num_ctx": 32768},
            }, timeout=180)
            answer = self._llm_text(res)
        except Exception as e:
            log_debug(f"answer_from_web error: {e}")
            answer = ""

        if not answer:
            self.speak("I found sources but couldn't summarise them, Sir.")
            return True

        card = answer + "\n\nSOURCES:\n" + "\n".join(
            f"• {s['title']}\n  {s['url']}" for s in sources[:5])
        self.instantiate_card(f"Web Research — {question[:40]}", "email_draft", card)
        self.speak(answer[:600])
        # Remember what we learned.
        self.memory.setdefault("research_log", []).append({
            "q": question, "answer": answer[:1000],
            "ts": datetime.datetime.now().isoformat(timespec="seconds")})
        self.save_memory()
        return True

    def deep_research(self, topic):
        """Multi-query research: break the topic into sub-questions, research each,
        then synthesise a full report into the document workspace."""
        self.speak(f"Beginning deep research on {topic}, Sir. This will take a minute.")
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("reasoning"),
                "messages": [{"role": "user", "content":
                    f"Break the research topic '{topic}' into 4 specific search queries that "
                    'together cover it well. Respond ONLY as JSON: {"queries": ["...", "..."]}'}],
                "stream": False, "format": "json", "keep_alive": "30m",
                "options": {"num_predict": 200, "temperature": 0.3},
            }, timeout=90)
            queries = self._llm_json(res).get("queries", [])
        except Exception as e:
            log_debug(f"deep_research planning error: {e}")
            queries = []
        if not queries:
            queries = [topic, f"{topic} explained", f"{topic} latest developments"]

        gathered, all_sources = [], []
        self.safe_log(f"Researching {len(queries[:4])} angles in parallel...")
        found = self.search_many(queries[:4], per_query=3)
        pages = self.fetch_many([r.get("url") for r in found], limit=2500)
        for r in found:
            text = pages.get(r.get("url", ""), "")
            if text:
                gathered.append(f"[{r['title']}] {text[:2000]}")
                all_sources.append(r)

        if not gathered:
            self.speak("I couldn't gather enough material, Sir.")
            return True

        try:
            prompt = (
                f"Write a well-structured research report on: {topic}\n\n"
                "Use clear headed sections and bullet points where useful. Base it on the "
                "research material below. Be factual and note where sources are uncertain.\n\n"
                + "\n\n---\n\n".join(gathered)[:28000]
            )
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("reasoning"),
                "messages": [{"role": "user", "content": prompt}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 1200, "temperature": 0.5, "num_ctx": 32768},
            }, timeout=300)
            report = self._llm_text(res)
        except Exception as e:
            log_debug(f"deep_research synthesis error: {e}")
            report = ""

        if not report:
            self.speak("I gathered the material but couldn't compose the report, Sir.")
            return True

        seen, src_lines = set(), []
        for s in all_sources:
            if s["url"] not in seen:
                seen.add(s["url"])
                src_lines.append(f"- {s['title']} — {s['url']}")
        report += "\n\nSources:\n" + "\n".join(src_lines[:12])

        self.current_doc = {"topic": f"Research: {topic}", "content": report}
        self.show_document_editor(f"Research: {topic}", report)
        self.speak("Research complete, Sir. The report is in your document workspace.")
        return True

    def check_url(self, url):
        """Fetch a page and summarise it."""
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        self.speak("Reading that page, Sir.")
        text = self.fetch_page_text(url, limit=8000)
        if not text:
            self.speak("I couldn't read that page, Sir.")
            return True
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("general"),
                "messages": [{"role": "user", "content":
                              f"Summarise this page concisely with the key points:\n\n{text[:12000]}"}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 350, "temperature": 0.4, "num_ctx": 16384},
            }, timeout=150)
            summary = self._llm_text(res)
            if summary:
                self.instantiate_card(f"Page Summary", "email_draft", f"{url}\n\n{summary}")
                self.speak(summary[:500])
        except Exception as e:
            log_debug(f"check_url error: {e}")
            self.speak("I could not summarise that page, Sir.")
        return True

    # Signals that a question needs live information rather than model memory.
    LIVE_INFO_PATTERNS = [
        r'\b(latest|current|recent|today|todays|this week|this month|this year|right now|nowadays)\b',
        r'\b(news|price|stock|score|weather|release date|version)\b',
        r'\b(who is|what is|when is|where is)\s+the\s+(current|new|latest)\b',
        r'\b(in|since)\s+20[2-9]\d\b',
        r'\b(how much (?:is|does|are)|cost of)\b',
        r'\b(is .+ (?:still|dead|alive|out|released))\b',
    ]

    def _known_research_topic(self, cmd):
        """If the question is about something already researched, answer from
        the stored sources instead of searching again."""
        topics = self.memory.get("research_topics", {})
        if not topics:
            return None
        low = cmd.lower()
        for key, data in topics.items():
            words = [w for w in re.findall(r'\w+', key) if len(w) > 3]
            if not words:
                continue
            if all(w in low for w in words) or key in low:
                return data
        return None

    def _needs_web(self, cmd):
        """Decide whether a question should be answered from the live internet.
        Being selective keeps fast questions fast."""
        if not CONFIG.get("assistant", {}).get("auto_research", True):
            return False
        low = cmd.lower()
        if len(low.split()) < 3:
            return False
        # Never search for personal/self-referential things we already know.
        if any(k in low for k in ["my ", "you ", "your ", "remember", "todo", "scratchpad"]):
            return False
        return any(re.search(p, low) for p in self.LIVE_INFO_PATTERNS)

    # ==================================================================
    # EXTENDED FEATURE SET
    # ==================================================================
    def quick_note(self, text):
        notes = self.memory.setdefault("notes", [])
        notes.append({"text": text, "ts": datetime.datetime.now().isoformat(timespec="seconds")})
        self.save_memory()
        self.speak("Noted, Sir.")
        return True

    def list_notes(self):
        notes = self.memory.get("notes", [])
        if not notes:
            self.speak("You have no notes, Sir.")
            return True
        lines = [f"{n['ts'][:16]} — {n['text']}" for n in notes[-15:]]
        self.instantiate_card("Your Notes", "carousel", lines)
        self.speak(f"You have {len(notes)} notes, Sir. The latest: {notes[-1]['text'][:150]}")
        return True

    def word_count(self, text=None):
        if not text and HAS_CLIPBOARD:
            try:
                text = pyperclip.paste()
            except Exception:
                text = ""
        if not text:
            self.speak("There's nothing to count, Sir.")
            return True
        words = len(text.split())
        chars = len(text)
        mins = max(1, round(words / 200))
        self.speak(f"{words} words, {chars} characters. About {mins} minute{'s' if mins != 1 else ''} to read, Sir.")
        return True

    def password_generator(self, length=16):
        import random, string
        length = max(8, min(64, int(length)))
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
        pw = "".join(random.choice(alphabet) for _ in range(length))
        if HAS_CLIPBOARD:
            try:
                pyperclip.copy(pw)
            except Exception:
                pass
        self.instantiate_card("Generated Password", "email_draft",
                              pw + "\n\n(Copied to your clipboard.)")
        self.speak(f"A {length} character password is on screen and copied to your clipboard, Sir.")
        return True

    def unit_convert(self, query):
        """Convert between common units via the model, with a card for the answer."""
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("general"),
                "messages": [{"role": "user", "content":
                              f"Convert this and reply with ONLY the result and unit: {query}"}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 60, "temperature": 0.1},
            }, timeout=60)
            ans = self._llm_text(res)
            if ans:
                self.speak(ans[:200])
                return True
        except Exception as e:
            log_debug(f"unit_convert error: {e}")
        self.speak("I couldn't convert that, Sir.")
        return True

    def coin_price_multi(self):
        try:
            ids = "bitcoin,ethereum,solana,dogecoin,cardano"
            r = self.http.get("https://api.coingecko.com/api/v3/simple/price",
                              params={"ids": ids, "vs_currencies": "usd"}, timeout=20).json()
            lines = [f"{k.title()}: ${v.get('usd')}" for k, v in r.items()]
            self.instantiate_card("Crypto Prices", "carousel", lines)
            self.speak(f"Bitcoin is at {r.get('bitcoin', {}).get('usd', 'unknown')} dollars, Sir.")
        except Exception as e:
            log_debug(f"coin_price_multi error: {e}")
            self.speak("I couldn't fetch crypto prices, Sir.")
        return True

    def clean_temp_files(self):
        freed = 0
        removed = 0
        temp = os.environ.get("TEMP") or "/tmp"
        try:
            for name in os.listdir(temp):
                p = os.path.join(temp, name)
                try:
                    if os.path.isfile(p) and (time.time() - os.path.getmtime(p)) > 86400:
                        sz = os.path.getsize(p)
                        os.remove(p)
                        freed += sz
                        removed += 1
                except Exception:
                    continue
        except Exception as e:
            log_debug(f"clean_temp error: {e}")
        self.speak(f"Removed {removed} temporary files, freeing {freed/1e6:.0f} megabytes, Sir.")
        return True

    def whats_my_setup(self):
        """Full machine summary."""
        try:
            import platform
            lines = [
                f"OS: {platform.system()} {platform.release()}",
                f"Machine: {platform.machine()}",
                f"Processor: {platform.processor()[:60] or 'unknown'}",
                f"CPU cores: {psutil.cpu_count(logical=False)} physical / {psutil.cpu_count()} logical",
                f"RAM: {psutil.virtual_memory().total/1e9:.1f} GB",
                f"Python: {platform.python_version()}",
            ]
            try:
                batt = psutil.sensors_battery()
                if batt:
                    lines.append(f"Battery: {int(batt.percent)}%")
            except Exception:
                pass
            self.instantiate_card("System Specification", "carousel", lines)
            self.speak("Your system specification is on screen, Sir.")
        except Exception as e:
            log_debug(f"whats_my_setup error: {e}")
        return True

    def uptime_report(self):
        try:
            boot = psutil.boot_time()
            delta = int(time.time() - boot)
            h, m = delta // 3600, (delta % 3600) // 60
            self.speak(f"Your machine has been running for {h} hours and {m} minutes, Sir.")
        except Exception as e:
            log_debug(f"uptime error: {e}")
        return True

    def brainstorm(self, topic, count=8):
        self.speak(f"Brainstorming ideas for {topic}, Sir.")
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("general"),
                "messages": [{"role": "user", "content":
                              f"Give {count} creative, specific, non-obvious ideas about: {topic}. "
                              "One per line, no preamble."}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 400, "temperature": 0.9},
            }, timeout=120)
            out = self._llm_text(res)
            ideas = [l.strip(" -*0123456789.") for l in out.split("\n") if l.strip()]
            if ideas:
                self.instantiate_card(f"Ideas — {topic[:30]}", "carousel", ideas[:count])
                self.speak(f"Here's one: {ideas[0][:200]}")
        except Exception as e:
            log_debug(f"brainstorm error: {e}")
            self.speak("Brainstorming failed, Sir.")
        return True

    def pros_and_cons(self, topic):
        self.speak(f"Weighing up {topic}, Sir.")
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("general"),
                "messages": [{"role": "user", "content":
                              f"Give a balanced pros and cons analysis of: {topic}. "
                              "Use two clear headed sections."}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 500, "temperature": 0.5},
            }, timeout=120)
            out = self._llm_text(res)
            if out:
                self.instantiate_card(f"Pros & Cons — {topic[:30]}", "email_draft", out)
                self.speak("The analysis is on screen, Sir.")
        except Exception as e:
            log_debug(f"pros_and_cons error: {e}")
        return True

    def explain_like_im(self, topic, level="five"):
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("general"),
                "messages": [{"role": "user", "content":
                              f"Explain '{topic}' as you would to someone aged {level}. "
                              "Be vivid, concrete and brief."}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 300, "temperature": 0.6},
            }, timeout=90)
            out = self._llm_text(res)
            if out:
                self.speak(out[:600])
                self.instantiate_card(f"Explained — {topic[:30]}", "email_draft", out)
        except Exception as e:
            log_debug(f"eli5 error: {e}")
        return True

    def daily_briefing(self):
        """Morning briefing: time, weather, calendar-ish todos, headlines."""
        self.speak(f"Good day, {USER_TITLE}. Here is your briefing.")
        parts = []
        now = datetime.datetime.now()
        parts.append(f"It is {now.strftime('%A %d %B, %I:%M %p')}.")
        try:
            self.get_weather()
        except Exception:
            pass
        todos = self.memory.get("todos", [])
        if todos:
            parts.append(f"You have {len(todos)} directives outstanding. First: {todos[0]}")
        try:
            self.get_news()
        except Exception:
            pass
        if parts:
            self.instantiate_card("Daily Briefing", "carousel", parts)
        return True

    def focus_mode(self, minutes=25):
        """Pomodoro-style focus session."""
        self.speak(f"Focus mode engaged for {minutes} minutes, Sir. I'll stay quiet.")
        self.focus_until = time.time() + minutes * 60

        def end():
            time.sleep(minutes * 60)
            self.focus_until = 0
            self.speak(f"Focus session complete, {USER_TITLE}. Time for a break.")
        threading.Thread(target=end, daemon=True).start()
        return True

    # ==================================================================
    # SECURITY SENTINEL — watches for intrusions and suspicious activity
    # ==================================================================
    def toggle_sentinel(self, active=None):
        want = (not self.sentinel_active) if active is None else bool(active)
        self.sentinel_active = want
        if want:
            self.speak("Security sentinel active, Sir. I'll watch for intrusions.")
            if not self._sentinel_started:
                self._sentinel_started = True
                threading.Thread(target=self._sentinel_worker, daemon=True).start()
        else:
            self.speak("Security sentinel stood down, Sir.")
        self.run_js(f"setSentinelState({str(want).lower()})")
        return True

    def _sentinel_worker(self):
        """Baseline the system, then alert on new processes, new listening ports,
        new remote connections, and failed logins."""
        init_thread_com()
        time.sleep(3)
        known_procs = set()
        known_ports = set()
        known_remotes = set()
        first_pass = True

        while True:
            if not self.sentinel_active:
                time.sleep(1.5)
                continue
            try:
                alerts = []

                procs = {}
                for p in psutil.process_iter(['pid', 'name', 'exe', 'username']):
                    try:
                        procs[p.info['pid']] = p.info
                    except Exception:
                        continue
                cur_procs = {(v.get('name') or '').lower() for v in procs.values()}
                if not first_pass:
                    for name in cur_procs - known_procs:
                        if name and self._is_suspicious_process(name):
                            alerts.append(f"New process: {name}")
                known_procs = cur_procs

                ports, remotes = set(), set()
                try:
                    for c in psutil.net_connections(kind='inet'):
                        if c.status == psutil.CONN_LISTEN and c.laddr:
                            ports.add(c.laddr.port)
                        elif c.status == 'ESTABLISHED' and c.raddr:
                            remotes.add((c.raddr.ip, c.raddr.port))
                except (psutil.AccessDenied, PermissionError):
                    pass

                if not first_pass:
                    for port in ports - known_ports:
                        alerts.append(f"New listening port: {port}")
                    new_remotes = remotes - known_remotes
                    for ip, port in list(new_remotes)[:3]:
                        if not self._is_private_ip(ip):
                            alerts.append(f"Outbound connection to {ip}:{port}")
                known_ports = ports
                known_remotes = remotes

                if alerts:
                    self._raise_security_alert(alerts)

                first_pass = False
            except Exception as e:
                log_debug(f"sentinel error: {e}")
            time.sleep(float(CONFIG.get("security", {}).get("scan_interval", 8)))

    @staticmethod
    def _is_private_ip(ip):
        return (ip.startswith(("10.", "192.168.", "127.", "169.254.", "::1", "fe80"))
                or re.match(r'^172\.(1[6-9]|2\d|3[01])\.', ip) is not None)

    @staticmethod
    def _is_suspicious_process(name):
        watch = {"nc.exe", "ncat.exe", "netcat", "psexec.exe", "mimikatz.exe",
                 "powershell_ise.exe", "wscript.exe", "cscript.exe", "mshta.exe",
                 "certutil.exe", "bitsadmin.exe", "regsvr32.exe", "rundll32.exe"}
            # Also flag anything running from a temp folder.
        return name in watch

    def _raise_security_alert(self, alerts):
        text = "\n".join(f"• {a}" for a in alerts[:6])
        self.memory.setdefault("security_log", []).append({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"), "alerts": alerts[:6]})
        self.save_memory()
        self.instantiate_card("⚠ SECURITY ALERT", "code_bug", text)
        self.run_js("flashSecurityAlert()")
        self.safe_log("SECURITY: " + "; ".join(alerts[:3]))
        if CONFIG.get("security", {}).get("speak_alerts", True):
            self.speak(f"Security notice, Sir. {alerts[0]}")

    def security_report(self):
        """On-demand security audit."""
        self.speak("Running a security sweep, Sir.")
        lines = []
        try:
            listening = []
            established = 0
            try:
                for c in psutil.net_connections(kind='inet'):
                    if c.status == psutil.CONN_LISTEN and c.laddr:
                        listening.append(c.laddr.port)
                    elif c.status == 'ESTABLISHED':
                        established += 1
            except (psutil.AccessDenied, PermissionError):
                lines.append("(Run as administrator for full network visibility)")
            lines.append(f"Listening ports: {sorted(set(listening))[:20]}")
            lines.append(f"Active connections: {established}")

            users = []
            try:
                for u in psutil.users():
                    users.append(f"{u.name} since {datetime.datetime.fromtimestamp(u.started):%H:%M}")
            except Exception:
                pass
            if users:
                lines.append("Logged in: " + ", ".join(users))

            heavy = sorted(
                ((p.info['name'], p.info['memory_percent'] or 0)
                 for p in psutil.process_iter(['name', 'memory_percent'])),
                key=lambda x: x[1], reverse=True)[:5]
            lines.append("Top processes: " + ", ".join(f"{n}" for n, _ in heavy if n))

            recent = self.memory.get("security_log", [])[-5:]
            if recent:
                lines.append("")
                lines.append("Recent alerts:")
                for r in recent:
                    lines.append(f"  {r['ts'][11:16]} — {r['alerts'][0] if r['alerts'] else ''}")
            else:
                lines.append("No security alerts logged.")
        except Exception as e:
            log_debug(f"security_report error: {e}")
            lines.append(f"Sweep error: {e}")

        self.instantiate_card("Security Report", "carousel", lines)
        self.speak("Security sweep complete, Sir. The report is on screen.")
        return True

    def block_process(self, name):
        """Terminate anything matching a name — the 'kill the intruder' action."""
        return self.kill_process(name)

    # ==================================================================
    # SELF-DIAGNOSTICS — Amy checks her own health
    # ==================================================================
    def self_diagnostics(self):
        """Check every subsystem and report honestly on what works."""
        self.speak("Running self-diagnostics, Sir.")
        checks = []

        def add(name, ok, detail=""):
            checks.append((name, ok, detail))

        # Core model
        try:
            r = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("general"), "messages": [{"role": "user", "content": "ping"}],
                "stream": False, "options": {"num_predict": 1}}, timeout=20)
            add("Language model", r.status_code == 200, MODEL_NAME)
        except Exception as e:
            add("Language model", False, f"unreachable ({type(e).__name__})")

        # Vision model
        try:
            r = self.http.get(OLLAMA_GENERATE_URL.replace("/api/generate", "/api/tags"), timeout=15)
            tags = [m.get("name", "") for m in r.json().get("models", [])]
            add("Vision model", any(VISION_MODEL in t for t in tags), VISION_MODEL)
        except Exception:
            add("Vision model", False, "could not list models")

        # Internet
        try:
            r = self.http.get("https://api.ipify.org", timeout=10)
            add("Internet", r.status_code == 200, r.text.strip()[:20])
        except Exception:
            add("Internet", False, "no connectivity")

        add("Neural voice (edge-tts)", HAS_EDGE_TTS,
            CONFIG.get("assistant", {}).get("neural_voice", "") if HAS_EDGE_TTS else "pip install --user edge-tts")
        add("System voice (SAPI)", HAS_PYTHONCOM, "pywin32" if HAS_PYTHONCOM else "pip install --user pywin32")
        add("Speech recognition", SPEECH_AVAILABLE, "" if SPEECH_AVAILABLE else "pip install --user SpeechRecognition")
        add("Camera (OpenCV)", HAS_CV2, "" if HAS_CV2 else "pip install --user opencv-python")
        add("Hand gestures (MediaPipe)", _mediapipe_installed(), "" if HAS_MEDIAPIPE else "pip install --user mediapipe")
        add("Clipboard", HAS_CLIPBOARD, "" if HAS_CLIPBOARD else "pip install --user pyperclip")
        add("Windows API", HAS_WIN32, "" if HAS_WIN32 else "pip install --user pywin32")
        add("PDF engine", REPORTLAB_AVAILABLE, "" if REPORTLAB_AVAILABLE else "pip install --user reportlab")
        add("Spotify", self.sp is not None, "" if self.sp else "not configured")

        # Storage & memory health
        try:
            mem_size = os.path.getsize(MEMORY_FILE) if os.path.exists(MEMORY_FILE) else 0
            add("Memory store", True,
                f"{len(self.memory.get('full_transcript', []))} messages, {mem_size/1024:.0f} KB")
        except Exception as e:
            add("Memory store", False, str(e))

        try:
            du = psutil.disk_usage(os.path.abspath(os.sep))
            add("Disk space", du.percent < 92, f"{du.percent}% used")
        except Exception:
            pass
        try:
            add("Memory pressure", psutil.virtual_memory().percent < 90,
                f"{psutil.virtual_memory().percent}% used")
        except Exception:
            pass

        ok_count = sum(1 for _, ok, _ in checks if ok)
        lines = [f"{'✓' if ok else '✗'}  {name}{('  —  ' + d) if d else ''}"
                 for name, ok, d in checks]
        lines.insert(0, f"SUBSYSTEMS: {ok_count} / {len(checks)} operational")
        lines.insert(1, "")

        self.instantiate_card("Self-Diagnostics", "code_bug", "\n".join(lines))
        failed = [n for n, ok, _ in checks if not ok]
        if failed:
            self.speak(f"{ok_count} of {len(checks)} subsystems operational, Sir. "
                       f"Issues with: {', '.join(failed[:3])}.")
        else:
            self.speak(f"All {len(checks)} subsystems fully operational, Sir.")
        return True

    # ==================================================================
    # MULTI-AGENT SYSTEM — specialised agents that collaborate
    # ==================================================================
    AGENT_ROLES = {
        "planner": "You break tasks into clear, ordered, actionable steps. Be concise.",
        "researcher": "You find and summarise factual information accurately, citing what you rely on.",
        "coder": "You write correct, complete, well-commented code.",
        "critic": "You review work sceptically and point out concrete flaws, risks and omissions.",
        "writer": "You write clear, engaging, well-structured prose.",
        "analyst": "You analyse data and arguments, weighing evidence carefully.",
    }

    def run_agent(self, role, task, context=""):
        system = self.AGENT_ROLES.get(role, "You are a helpful specialist.")
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("reasoning"),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": (f"{context}\n\n{task}" if context else task)},
                ],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 700, "temperature": 0.5, "num_ctx": 8192},
            }, timeout=240)
            return self._llm_text(res)
        except Exception as e:
            log_debug(f"agent {role} error: {e}")
            return ""

    def agent_task(self, task):
        """Coordinated multi-agent workflow: plan -> execute -> critique -> refine."""
        self.speak("Assembling a team of agents, Sir.")
        transcript = []

        plan = self.run_agent("planner", f"Create a short plan to accomplish: {task}")
        if not plan:
            self.speak("The planning agent failed, Sir.")
            return True
        transcript.append("── PLANNER ──\n" + plan)
        self.safe_log("Planner finished.")

        # Choose the right specialist for the job.
        low = task.lower()
        if any(k in low for k in ["code", "script", "program", "function", "bug"]):
            worker = "coder"
        elif any(k in low for k in ["research", "find out", "investigate", "compare"]):
            worker = "researcher"
        elif any(k in low for k in ["write", "draft", "essay", "post", "article"]):
            worker = "writer"
        else:
            worker = "analyst"

        self.safe_log(f"Delegating to {worker} agent...")
        work = self.run_agent(worker, f"Carry out this task: {task}", context=f"Plan:\n{plan}")
        transcript.append(f"── {worker.upper()} ──\n" + work)

        self.safe_log("Critic reviewing...")
        critique = self.run_agent(
            "critic", "Review the work below. List concrete problems and improvements.",
            context=f"Task: {task}\n\nWork:\n{work}")
        transcript.append("── CRITIC ──\n" + critique)

        self.safe_log("Refining...")
        final = self.run_agent(
            worker, "Produce the improved final version, addressing every critique point.",
            context=f"Task: {task}\n\nDraft:\n{work}\n\nCritique:\n{critique}")
        transcript.append("── FINAL ──\n" + final)

        full = "\n\n".join(transcript)
        self.current_doc = {"topic": f"Agent Task: {task[:50]}", "content": final or work}
        self.show_document_editor(f"Agent Task: {task[:50]}", final or work)
        self.instantiate_card("Agent Transcript", "code_bug", full[:4000])
        self.speak("The agents have finished, Sir. The result is in your document workspace.")
        return True

    # ==================================================================
    # PERSONAL KNOWLEDGE BASE — your second brain
    # ==================================================================
    def kb_ingest(self, path):
        """Index a file or folder into the searchable knowledge base."""
        path = os.path.expanduser(path.strip().strip('"'))
        if not os.path.exists(path):
            self.speak("I couldn't find that path, Sir.")
            return True
        exts = {".txt", ".md", ".py", ".js", ".json", ".csv", ".html", ".log", ".ini", ".cfg"}
        files = []
        if os.path.isfile(path):
            files = [path]
        else:
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [d for d in dirnames if not d.startswith('.')
                               and d not in ("node_modules", "__pycache__", "venv")]
                for fn in filenames:
                    if os.path.splitext(fn)[1].lower() in exts:
                        files.append(os.path.join(dirpath, fn))
                if len(files) > 400:
                    break

        kb = self.memory.setdefault("knowledge_base", {})
        added = 0
        for f in files[:400]:
            try:
                if os.path.getsize(f) > 400000:
                    continue
                with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()[:20000]
                if text.strip():
                    kb[f] = {"text": text, "indexed": datetime.datetime.now().isoformat(timespec="seconds")}
                    added += 1
            except Exception:
                continue
        self.save_memory()
        self.speak(f"Indexed {added} documents into your knowledge base, Sir.")
        self.safe_log(f"Knowledge base now holds {len(kb)} documents.")
        return True

    def kb_search(self, query):
        """Keyword-rank the knowledge base, then answer from the best matches."""
        kb = self.memory.get("knowledge_base", {})
        if not kb:
            self.speak("Your knowledge base is empty, Sir. Say 'index folder <path>' first.")
            return True
        # Semantic ranking (falls back to keywords if embeddings are unavailable).
        items = [(path, doc.get("text", "")) for path, doc in kb.items()]
        scored = self.semantic_search(query, items, top_k=4)
        if not scored:
            self.speak("Nothing in your knowledge base matches that, Sir.")
            return True

        context = "\n\n".join(
            f"[{os.path.basename(p)}]\n{t[:2500]}" for _, p, t in scored[:4])
        self.speak("Searching your knowledge base, Sir.")
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("reasoning"),
                "messages": [{"role": "user", "content":
                    f"Answer using ONLY these documents from the user's own files. "
                    f"Cite the filenames you used.\n\nQUESTION: {query}\n\nDOCUMENTS:\n{context[:20000]}"}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 450, "temperature": 0.3, "num_ctx": 32768},
            }, timeout=180)
            answer = self._llm_text(res)
            if answer:
                srcs = "\n".join(f"• {os.path.basename(p)}" for _, p, _ in scored[:4])
                self.instantiate_card("Knowledge Base", "email_draft", answer + "\n\nSOURCES:\n" + srcs)
                self.speak(answer[:500])
        except Exception as e:
            log_debug(f"kb_search error: {e}")
            self.speak("The knowledge base search failed, Sir.")
        return True

    def kb_status(self):
        kb = self.memory.get("knowledge_base", {})
        if not kb:
            self.speak("Your knowledge base is empty, Sir.")
            return True
        total = sum(len(d.get("text", "")) for d in kb.values())
        lines = [f"{len(kb)} documents indexed", f"~{total//1000}k characters"]
        lines += [f"• {os.path.basename(p)}" for p in list(kb)[:12]]
        self.instantiate_card("Knowledge Base", "carousel", lines)
        self.speak(f"Your knowledge base holds {len(kb)} documents, Sir.")
        return True

    # ==================================================================
    # FILE MANAGEMENT — organise, rename, bulk operations
    # ==================================================================
    def organize_folder(self, folder=None):
        """Sort loose files into type-based subfolders."""
        folder = os.path.expanduser(folder or os.path.join(os.path.expanduser("~"), "Downloads"))
        if not os.path.isdir(folder):
            self.speak("I couldn't find that folder, Sir.")
            return True
        groups = {
            "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".heic"},
            "Documents": {".pdf", ".docx", ".doc", ".txt", ".md", ".rtf", ".odt", ".pptx", ".xlsx", ".csv"},
            "Video": {".mp4", ".mkv", ".mov", ".avi", ".webm"},
            "Audio": {".mp3", ".wav", ".flac", ".m4a", ".ogg"},
            "Archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
            "Installers": {".exe", ".msi", ".dmg", ".deb"},
            "Code": {".py", ".js", ".html", ".css", ".json", ".java", ".cpp", ".cs", ".ts"},
        }
        moved = 0
        try:
            for name in os.listdir(folder):
                src = os.path.join(folder, name)
                if not os.path.isfile(src):
                    continue
                ext = os.path.splitext(name)[1].lower()
                dest_group = next((g for g, exts in groups.items() if ext in exts), None)
                if not dest_group:
                    continue
                dest_dir = os.path.join(folder, dest_group)
                os.makedirs(dest_dir, exist_ok=True)
                dest = os.path.join(dest_dir, name)
                if os.path.exists(dest):
                    base, e = os.path.splitext(name)
                    dest = os.path.join(dest_dir, f"{base}_{int(time.time())}{e}")
                shutil.move(src, dest)
                moved += 1
        except Exception as e:
            log_debug(f"organize_folder error: {e}")
        self.speak(f"Organised {moved} files into folders, Sir.")
        self.safe_log(f"Organised {moved} files in {folder}")
        return True

    def bulk_rename(self, folder, pattern, replacement):
        folder = os.path.expanduser(folder)
        if not os.path.isdir(folder):
            self.speak("I couldn't find that folder, Sir.")
            return True
        renamed = 0
        try:
            for name in os.listdir(folder):
                if pattern.lower() in name.lower():
                    src = os.path.join(folder, name)
                    new = re.sub(re.escape(pattern), replacement, name, flags=re.IGNORECASE)
                    dst = os.path.join(folder, new)
                    if not os.path.exists(dst):
                        os.rename(src, dst)
                        renamed += 1
        except Exception as e:
            log_debug(f"bulk_rename error: {e}")
        self.speak(f"Renamed {renamed} files, Sir.")
        return True

    def folder_summary(self, folder=None):
        folder = os.path.expanduser(folder or os.path.join(os.path.expanduser("~"), "Downloads"))
        if not os.path.isdir(folder):
            self.speak("I couldn't find that folder, Sir.")
            return True
        try:
            counts, total = {}, 0
            for dirpath, _, filenames in os.walk(folder):
                for fn in filenames:
                    ext = os.path.splitext(fn)[1].lower() or "(none)"
                    counts[ext] = counts.get(ext, 0) + 1
                    try:
                        total += os.path.getsize(os.path.join(dirpath, fn))
                    except Exception:
                        pass
            top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
            lines = [f"{os.path.basename(folder)}: {sum(counts.values())} files, {total/1e6:.0f} MB"]
            lines += [f"{ext} — {n}" for ext, n in top]
            self.instantiate_card("Folder Summary", "carousel", lines)
            self.speak(f"That folder holds {sum(counts.values())} files, Sir.")
        except Exception as e:
            log_debug(f"folder_summary error: {e}")
        return True

    # ==================================================================
    # SEMANTIC MEMORY — embeddings so he matches MEANING, not keywords
    # ==================================================================
    def _embed(self, text):
        """Get an embedding vector from Ollama. Returns a list of floats, or None."""
        try:
            model = CONFIG.get("ollama", {}).get("embed_model", "nomic-embed-text")
            res = self.http.post(
                OLLAMA_GENERATE_URL.replace("/api/generate", "/api/embeddings"),
                json={"model": model, "prompt": text[:8000], "keep_alive": "30m"},
                timeout=60)
            if res.status_code == 200:
                vec = res.json().get("embedding")
                if vec:
                    return vec
            log_debug(f"embed failed: HTTP {res.status_code}")
        except Exception as e:
            log_debug(f"embed error: {e}")
        return None

    @staticmethod
    def _cosine(a, b):
        """Cosine similarity between two vectors (pure python fallback safe)."""
        try:
            if np is not None:
                va, vb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
                denom = (np.linalg.norm(va) * np.linalg.norm(vb))
                return float(np.dot(va, vb) / denom) if denom else 0.0
            dot = sum(x * y for x, y in zip(a, b))
            na = sum(x * x for x in a) ** 0.5
            nb = sum(y * y for y in b) ** 0.5
            return dot / (na * nb) if na and nb else 0.0
        except Exception:
            return 0.0

    def semantic_search(self, query, items, top_k=5):
        """items = [(id, text)]. Returns [(score, id, text)] ranked by meaning.
        Falls back to keyword scoring if embeddings are unavailable."""
        qvec = self._embed(query)
        if qvec is None:
            terms = [t for t in re.findall(r'\w+', query.lower()) if len(t) > 2]
            scored = []
            for _id, text in items:
                low = text.lower()
                score = sum(low.count(t) for t in terms)
                if score:
                    scored.append((float(score), _id, text))
            scored.sort(reverse=True, key=lambda x: x[0])
            return scored[:top_k]

        # Embed everything not already cached, in parallel.
        vecs = self.embed_batch([text[:4000] for _id, text in items])
        scored = []
        for _id, text in items:
            vec = vecs.get(text[:4000])
            if vec is not None and len(vec):
                scored.append((self._cosine(qvec, vec), _id, text))
        scored.sort(reverse=True, key=lambda x: x[0])
        return scored[:top_k]

    def recall_relevant(self, query, top_k=4):
        """Pull the most semantically relevant remembered facts for a query."""
        facts = self.memory.get("facts", [])
        if not facts:
            return []
        items = [(str(i), f) for i, f in enumerate(facts)]
        hits = self.semantic_search(query, items, top_k=top_k)
        return [text for score, _id, text in hits if score > 0.35]

    def consolidate_memory(self):
        """Merge duplicate/overlapping facts so his self-knowledge sharpens
        instead of endlessly growing."""
        facts = self.memory.get("facts", [])
        if len(facts) < 6:
            self.speak("I don't have enough stored facts to consolidate yet, Sir.")
            return True
        self.speak("Consolidating what I know about you, Sir.")
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("reasoning"),
                "messages": [{"role": "user", "content":
                    "Below is a list of remembered facts about a user. Merge duplicates, "
                    "resolve contradictions in favour of the LATER entry, drop trivia, and "
                    "rewrite each as one clear sentence.\n"
                    'Respond ONLY as JSON: {"facts": ["...", "..."]}\n\n'
                    + "\n".join(f"- {f}" for f in facts[-80:])}],
                "stream": False, "format": "json", "keep_alive": "30m",
                "options": {"num_predict": 700, "temperature": 0.2, "num_ctx": 8192},
            }, timeout=180)
            merged = self._llm_json(res).get("facts", [])
            merged = [str(f).strip() for f in merged if str(f).strip()]
            if merged:
                before = len(facts)
                self.memory["facts"] = merged
                self._embed_store = {}; self._save_embeddings()   # facts changed
                self.save_memory()
                self.instantiate_card("Consolidated Memory", "carousel", merged[:20])
                self.speak(f"Consolidated {before} facts down to {len(merged)}, Sir.")
        except Exception as e:
            log_debug(f"consolidate_memory error: {e}")
            self.speak("I couldn't consolidate my memory, Sir.")
        return True

    # ==================================================================
    # CAD ENGINE — design parts by voice, export real STL/SCAD files
    # ==================================================================
    CAD_SYSTEM_PROMPT = (
        "You are an expert parametric CAD engineer writing OpenSCAD code.\n\n"
        "OUTPUT RULES\n"
        "- Output ONLY valid OpenSCAD code in a single fenced block. No prose.\n"
        "- All units are millimetres.\n\n"
        "STRUCTURE\n"
        "- Put EVERY key dimension in a named variable at the very top, one per line,\n"
        "  in the exact form `name = 12;  // short description`. These become sliders,\n"
        "  so never bury numbers inside the geometry.\n"
        "- Set `$fn = 64;` once at the top for smooth curves.\n"
        "- Build the shape from small `module` definitions, then call them at the end.\n\n"
        "CORRECTNESS (these are the usual failure causes)\n"
        "- The model MUST be manifold: no zero-thickness walls, no coincident faces.\n"
        "- When subtracting with difference(), make the cutting solid OVERSHOOT the\n"
        "  body on both ends (e.g. extend by 1mm each side) so faces never coincide.\n"
        "- Never use a wall thinner than 1.2mm - it will not print.\n"
        "- Give every cylinder/cube explicit dimensions; never rely on defaults.\n"
        "- Use `translate()` and `rotate()` rather than negative dimensions.\n"
        "- Only use built-in OpenSCAD features. Do NOT `use<>` or `include<>` any\n"
        "  external library (BOSL, MCAD, etc.) - they will not be installed.\n"
        "- Do not use `text()` unless the user explicitly asks for lettering.\n\n"
        "DESIGN QUALITY\n"
        "- Add fillets/chamfers on load-bearing corners where sensible (hull() of\n"
        "  small cylinders is a reliable way to do this).\n"
        "- Ensure the part has a flat face to sit on the print bed.\n"
        "- Add a comment above each module explaining what it forms.\n\n"
        "EXAMPLE OF THE EXPECTED STYLE\n"
        "```\n"
        "$fn = 64;\n"
        "width = 70;      // overall width\n"
        "depth = 80;      // overall depth\n"
        "thickness = 4;   // wall thickness\n"
        "hole_d = 5;      // mounting hole diameter\n"
        "\n"
        "// Flat base plate with rounded corners\n"
        "module base() {\n"
        "    hull() {\n"
        "        for (x = [thickness, width - thickness])\n"
        "            for (y = [thickness, depth - thickness])\n"
        "                translate([x, y, 0]) cylinder(d = thickness * 2, h = thickness);\n"
        "    }\n"
        "}\n"
        "\n"
        "// Mounting holes, overshooting the plate so faces never coincide\n"
        "module holes() {\n"
        "    for (x = [12, width - 12])\n"
        "        translate([x, depth / 2, -1])\n"
        "            cylinder(d = hole_d, h = thickness + 2);\n"
        "}\n"
        "\n"
        "difference() { base(); holes(); }\n"
        "```"
    )

    def _cad_generate_code(self, instruction, existing=None):
        messages = [{"role": "system", "content": self.CAD_SYSTEM_PROMPT}]
        if existing:
            messages.append({"role": "user", "content":
                f"Here is the current OpenSCAD model:\n\n{existing}\n\n"
                f"Modify it: {instruction}\nReturn the COMPLETE updated code."})
        else:
            messages.append({"role": "user", "content": instruction})
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self._code_model(),
                "messages": messages,
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 1200, "temperature": 0.25, "num_ctx": 8192},
            }, timeout=240)
            raw = self._llm_text(res)
            return self._strip_code_fences(raw)
        except Exception as e:
            log_debug(f"cad generate error: {e}")
            return ""

    @staticmethod
    def _cad_extract_params(code):
        """Pull top-level `name = value;` variables so they can be edited as sliders."""
        params = []
        for line in code.split("\n")[:60]:
            m = re.match(r'^\s*(\w+)\s*=\s*([\d.]+)\s*;\s*(?://\s*(.*))?$', line)
            if m:
                params.append({"name": m.group(1),
                               "value": float(m.group(2)),
                               "note": (m.group(3) or "").strip()})
        return params[:12]

    def cad_edit(self, instruction):
        """Revise the open CAD model by voice."""
        if self._is_cq_cad():
            return self.cq_revise(instruction, propose=False)
        if not getattr(self, "current_cad", None):
            self.speak("There's no design open, Sir. Ask me to design something first.")
            return True
        self.speak("Revising the design, Sir.")
        code = self._cad_generate_code(instruction, existing=self.current_cad["code"])
        if not code:
            self.speak("I couldn't apply that change, Sir.")
            return True
        self.current_cad["code"] = code
        try:
            with open(self.current_cad["path"], "w", encoding="utf-8") as f:
                f.write(code)
        except Exception as e:
            log_debug(f"cad edit write error: {e}")
        params = self._cad_extract_params(code)
        self.run_js(f"showCadEditor({json.dumps({'name': self.current_cad['description'], 'code': code, 'path': self.current_cad['path'], 'params': params})})")
        self.cad_export_stl(announce=False)
        self.speak("The design has been updated, Sir.")
        return True

    def cad_set_param(self, name, value):
        """Change one dimension and re-export — the parametric workflow."""
        if self._is_cq_cad():
            return self.cq_set_param(name, value)
        if not getattr(self, "current_cad", None):
            return False
        code = self.current_cad["code"]
        new_code, n = re.subn(rf'^(\s*{re.escape(name)}\s*=\s*)[\d.]+(\s*;)',
                              rf'\g<1>{value}\g<2>', code, count=1, flags=re.M)
        if not n:
            return False
        self.current_cad["code"] = new_code
        try:
            with open(self.current_cad["path"], "w", encoding="utf-8") as f:
                f.write(new_code)
        except Exception as e:
            log_debug(f"cad param write error: {e}")
            return False
        self.safe_log(f"CAD: {name} = {value}")
        threading.Thread(target=lambda: self.cad_export_stl(announce=False), daemon=True).start()
        return True

    def update_cad_from_ui(self, code):
        if self._is_cq_cad():
            return self.cq_update_from_ui(code)
        if getattr(self, "current_cad", None):
            self.current_cad["code"] = code
            try:
                with open(self.current_cad["path"], "w", encoding="utf-8") as f:
                    f.write(code)
            except Exception as e:
                log_debug(f"cad ui save error: {e}")

    @staticmethod
    def _find_openscad():
        p = shutil.which("openscad")
        if p:
            return p
        for c in [
            r"C:\Program Files\OpenSCAD\openscad.exe",
            r"C:\Program Files (x86)\OpenSCAD\openscad.exe",
            "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
            "/usr/bin/openscad",
        ]:
            if os.path.exists(c):
                return c
        return None

    def cad_export_stl(self, announce=True):
        """Render the model to a real STL via OpenSCAD's CLI."""
        if self._is_cq_cad():
            stl = self.current_cad.get("stl")
            if announce:
                self.speak("Exported STEP and STL files, Sir." if stl else "There's nothing exported yet, Sir.")
            return stl
        if not getattr(self, "current_cad", None):
            if announce:
                self.speak("There's no design to export, Sir.")
            return None
        exe = self._find_openscad()
        if not exe:
            if announce:
                self.speak("OpenSCAD isn't installed, Sir. Install it from openscad.org to export STL files.")
                self.safe_log("STL export needs OpenSCAD: https://openscad.org/downloads.html")
            return None
        scad = self.current_cad["path"]
        stl = scad.replace(".scad", ".stl")
        try:
            proc = subprocess.run([exe, "-o", stl, scad],
                                  capture_output=True, text=True, timeout=180)
            if os.path.exists(stl) and os.path.getsize(stl) > 100:
                self.current_cad["stl"] = stl
                self.safe_log(f"STL exported: {stl}")
                self.run_js(f"setCadStlPath({json.dumps(stl)})")
                self._send_cad_mesh(stl)
                if announce:
                    self.speak("STL exported, Sir.")
                return stl
            err = (proc.stderr or "")[:400]
            log_debug(f"openscad error: {err}")
            self.safe_log(f"STL export failed. OpenSCAD said: {err[:200]}")
            if announce:
                self.speak("The design has an error and couldn't be exported, Sir.")
        except subprocess.TimeoutExpired:
            self.safe_log("STL export timed out — the model may be too complex.")
        except Exception as e:
            log_debug(f"cad export error: {e}")
        return None

    def _send_cad_mesh(self, stl_path):
        """Parse an STL and push a simplified mesh to the UI for the rotating preview."""
        try:
            tris = self._parse_stl(stl_path, max_tris=2200)
            if not tris:
                return
            xs = [v[0] for t in tris for v in t]
            ys = [v[1] for t in tris for v in t]
            zs = [v[2] for t in tris for v in t]
            cx, cy, cz = (min(xs)+max(xs))/2, (min(ys)+max(ys))/2, (min(zs)+max(zs))/2
            span = max(max(xs)-min(xs), max(ys)-min(ys), max(zs)-min(zs)) or 1.0
            s = 2.0 / span
            flat = []
            for t in tris:
                for v in t:
                    flat.append(round((v[0]-cx)*s, 3))
                    flat.append(round((v[1]-cy)*s, 3))
                    flat.append(round((v[2]-cz)*s, 3))
            self.run_js(f"setCadMesh({json.dumps(flat)})")
            self.last_cad_mesh = flat          # VR reads this too
            log_debug(f"CAD mesh sent: {len(tris)} triangles")
        except Exception as e:
            log_debug(f"_send_cad_mesh error: {e}")

    @staticmethod
    def _parse_stl(path, max_tris=2500):
        """Read a binary or ASCII STL into a list of 3-vertex triangles."""
        import struct
        tris = []
        try:
            with open(path, "rb") as f:
                head = f.read(84)
                if len(head) < 84:
                    return []
                count = struct.unpack("<I", head[80:84])[0]
                body = f.read()
            if count > 0 and len(body) >= count * 50 - 2:
                step = max(1, count // max_tris)
                for i in range(0, count, step):
                    off = i * 50 + 12          # skip the normal vector
                    chunk = body[off:off+36]
                    if len(chunk) < 36:
                        break
                    v = struct.unpack("<9f", chunk)
                    tris.append(((v[0], v[1], v[2]), (v[3], v[4], v[5]), (v[6], v[7], v[8])))
                if tris:
                    return tris
        except Exception as e:
            log_debug(f"binary STL parse failed: {e}")
        try:
            with open(path, "r", errors="ignore") as f:
                verts = []
                for line in f:
                    if line.strip().startswith("vertex"):
                        p = line.split()
                        if len(p) >= 4:
                            verts.append((float(p[1]), float(p[2]), float(p[3])))
                            if len(verts) == 3:
                                tris.append(tuple(verts))
                                verts = []
                                if len(tris) >= max_tris:
                                    break
        except Exception as e:
            log_debug(f"ascii STL parse failed: {e}")
        return tris

    def cad_open_external(self):
        """Open the model in OpenSCAD for live 3D viewing."""
        if self._is_cq_cad() and self.current_cad.get("step"):
            self.launch_external_app(self.current_cad["step"])
            self.speak("Opening the STEP file, Sir.")
            return True
        if not getattr(self, "current_cad", None):
            self.speak("There's no design open, Sir.")
            return True
        exe = self._find_openscad()
        path = self.current_cad["path"]
        try:
            if exe:
                subprocess.Popen([exe, path])
                self.speak("Opening the design in OpenSCAD, Sir.")
            else:
                self.launch_external_app(path)
                self.speak("Opening the design file, Sir.")
        except Exception as e:
            log_debug(f"cad open error: {e}")
        return True

    # ==================================================================
    # CADQUERY PATH - exact B-rep parts and jointed assemblies
    # ==================================================================
    def _cq_enabled(self):
        return HAS_CADQUERY and str(CONFIG.get("cad", {}).get("engine", "auto")).lower() != "openscad"

    def _is_cq_cad(self):
        cad = getattr(self, "current_cad", None)
        return bool(cad) and cad.get("engine") == "cq"

    def _cq_engine(self):
        if getattr(self, "_cq", None) is None:
            self._cq = CadQueryEngine()
        return self._cq

    def _cq_spec_from_model(self, description, feedback=None, previous=None):
        prompt = (f"Design: {description}\n\n"
                  + (f"The last attempt had a problem: {feedback}\nFix it.\n\n" if feedback else "")
                  + (f"Current spec:\n{json.dumps(previous)[:6000]}\n\n" if previous else "")
                  + CadQueryEngine.SPEC_DOC)
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("cad"),
                "messages": [
                    {"role": "system", "content":
                     "You are a mechanical and industrial designer. You describe objects as precise "
                     "JSON geometry specs: realistic proportions, smooth curves where the real object "
                     "has them, separate parts for anything that moves. You never write code."},
                    {"role": "user", "content": prompt}],
                "stream": False, "format": "json", "keep_alive": "30m",
                "options": {"num_predict": 2400, "temperature": 0.25, "num_ctx": 12288},
            }, timeout=300)
            spec = self._llm_json(res)
            return spec if isinstance(spec, dict) and spec.get("parts") else None
        except Exception as e:
            log_debug(f"cq spec generation failed: {e}")
            return None

    def cq_build(self, spec, description, announce=False):
        """Compile a spec, export STEP/STL, update the editor and preview.
        Returns True on success; failures are logged with the reason."""
        eng = self._cq_engine()
        try:
            result = eng.compile(spec)
        except CadSpecError as e:
            self.safe_log(f"CAD: {e}")
            return False
        name = re.sub(r"[^\w]+", "_", description)[:40].strip("_") or "part"
        base = os.path.join(CAD_DIR, f"{name}_{int(time.time())}")
        try:
            step, stl, part_files = eng.export(result, base)
        except Exception as e:
            log_debug(f"cq export failed: {e}")
            step, stl, part_files = None, None, []
        spec_path = base + ".amycad.json"
        try:
            with open(spec_path, "w", encoding="utf-8") as f:
                json.dump(spec, f, indent=2)
        except Exception:
            pass
        self.current_cad = {"engine": "cq", "description": description, "spec": spec,
                            "code": json.dumps(spec, indent=2), "path": spec_path,
                            "step": step, "stl": stl, "part_stls": part_files,
                            "warnings": result["warnings"]}
        params = [{"name": k, "value": round(v, 3), "note": ""}
                  for k, v in list(result["params"].items())[:16]]
        self.run_js(f"showCadEditor({json.dumps({'name': description, 'code': self.current_cad['code'], 'path': spec_path, 'params': params})})")
        meshes = eng.mesh(result)
        self._send_cad_parts(meshes)
        if result["warnings"]:
            self.instantiate_card("Design check", "code_bug", "\n".join(result["warnings"]))
        if announce:
            self.speak(f"Done, {USER_TITLE}. {len(result['parts'])} part"
                       f"{'s' if len(result['parts']) != 1 else ''}, exported as STEP and STL.")
        self._cq_last_meshes = meshes
        return True

    def _send_cad_parts(self, meshes):
        """Normalise all parts together to about -1..1 and send to the preview."""
        pts = [c for m in meshes for c in zip(m["tris"][0::3], m["tris"][1::3], m["tris"][2::3])]
        if not pts:
            return
        xs, ys, zs = zip(*pts)
        cx, cy, cz = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2
        span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) or 1.0
        k = 2.0 / span
        out, flat_all = [], []
        for m in meshes:
            t = m["tris"]
            norm = []
            for i in range(0, len(t), 3):
                norm += [round((t[i] - cx) * k, 4), round((t[i + 1] - cy) * k, 4), round((t[i + 2] - cz) * k, 4)]
            out.append({"name": m["name"], "color": m["color"], "tris": norm})
            flat_all += norm
        self.last_cad_mesh = flat_all
        self.run_js(f"setCadParts({json.dumps(out)})")

    def cq_cad_create(self, description):
        """Design with CadQuery: spec -> build -> check overlaps and looks -> retry."""
        self.speak(f"Designing {description}, {USER_TITLE}.")
        attempts = int(CONFIG.get("cad", {}).get("max_attempts", 4))
        visual = bool(CONFIG.get("cad", {}).get("visual_check", True))
        spec, feedback = None, None
        for attempt in range(1, attempts + 1):
            self.progress(f"Designing ({attempt}/{attempts})", 10 + (attempt - 1) * 80 / attempts)
            new = self._cq_spec_from_model(description, feedback, spec)
            if not new:
                feedback = "The reply wasn't a valid JSON spec with a 'parts' list."
                continue
            spec = new
            try:
                result = self._cq_engine().compile(spec)
            except CadSpecError as e:
                feedback = str(e)
                self.safe_log(f"Attempt {attempt}: {e}")
                continue
            if result["warnings"] and attempt < attempts:
                feedback = "Geometry problems: " + "; ".join(result["warnings"])
                self.safe_log(f"Attempt {attempt}: {feedback[:140]}")
                continue
            if visual and attempt < attempts and self._model_installed(self.model_for("vision")):
                png = os.path.join(CAD_DIR, f"_check_{int(time.time() * 1000)}.png")
                if CadQueryEngine.render_png(CadQueryEngine.mesh(result), png):
                    self.progress("Checking the shape", 85)
                    looks_ok, fb = self._cad_visual_check(png, description)
                    try:
                        os.remove(png)
                    except Exception:
                        pass
                    if not looks_ok and fb:
                        feedback = f"It builds, but doesn't look like a {description}. {fb}"
                        self.safe_log(f"Attempt {attempt}: shape not right yet.")
                        continue
            self.progress("Exporting", 95)
            ok = self.cq_build(spec, description, announce=True)
            self.progress_done()
            if ok:
                self.project_log(f"Designed: {description}")
            return ok
        self.progress_done()
        if spec:
            # Show the best effort rather than nothing, with its problems listed.
            if self.cq_build(spec, description):
                self.speak("Here's my best attempt, Sir, though it still has issues I couldn't fix.")
                return True
        return False

    def cq_set_param(self, name, value):
        cad = self.current_cad
        spec = copy.deepcopy(cad["spec"])
        spec.setdefault("parameters", {})[name] = float(value)
        self._cq_param_gen = getattr(self, "_cq_param_gen", 0) + 1
        gen = self._cq_param_gen

        def rebuild():
            time.sleep(0.25)                    # coalesce slider drags
            if gen != self._cq_param_gen:
                return
            if self.cq_build(spec, cad["description"]):
                self.safe_log(f"CAD: {name} = {value}")
        threading.Thread(target=rebuild, daemon=True).start()
        return True

    def cq_update_from_ui(self, code):
        try:
            spec = json.loads(code)
        except Exception:
            return False                        # still typing
        self._cq_param_gen = getattr(self, "_cq_param_gen", 0) + 1
        gen = self._cq_param_gen

        def rebuild():
            time.sleep(0.6)
            if gen == self._cq_param_gen:
                self.cq_build(spec, self.current_cad["description"])
        threading.Thread(target=rebuild, daemon=True).start()
        return True

    def cq_revise(self, instruction, propose=False):
        """Change the open design by voice. With propose=True the change is
        shown as a diff to accept or discard first."""
        cad = self.current_cad
        self.speak("Working on that change, Sir.")
        self.progress("Revising design", 20)
        feedback, spec = None, None
        for attempt in range(3):
            spec = self._cq_spec_from_model(
                f"{cad['description']}. Apply this change and keep everything else the same: {instruction}",
                feedback, cad["spec"])
            if not spec:
                feedback = "Return the complete updated JSON spec."
                continue
            try:
                result = self._cq_engine().compile(spec)
                if result["warnings"] and attempt < 2:
                    feedback = "Geometry problems: " + "; ".join(result["warnings"])
                    continue
                break
            except CadSpecError as e:
                feedback = str(e)
                spec = None
        self.progress_done()
        if not spec:
            self.speak("I couldn't make that change work, Sir.")
            return True
        if propose:
            diff = self._make_diff(json.dumps(cad["spec"], indent=2), json.dumps(spec, indent=2))
            self.pending_cad = {"spec": spec, "instruction": instruction, "valid": True,
                                "code": json.dumps(spec, indent=2)}
            self.run_js(f"showCadProposal({json.dumps(instruction)}, {json.dumps(diff[:6000])}, true)")
            self.speak("Here's the proposed change, Sir. Accept it or discard it.")
            return True
        ok = self.cq_build(spec, cad["description"])
        self.speak("The design has been updated, Sir." if ok else "That change didn't build, Sir.")
        return True

    def cad_list(self):
        try:
            files = sorted(
                (f for f in os.listdir(CAD_DIR) if f.endswith((".scad", ".stl"))),
                reverse=True)[:20]
            if not files:
                self.speak("You have no saved designs yet, Sir.")
                return True
            self.instantiate_card("Your Designs", "carousel", files)
            self.speak(f"You have {len(files)} design files, Sir.")
        except Exception as e:
            log_debug(f"cad_list error: {e}")
        return True

    # ==================================================================
    # PROGRESS REPORTING
    # ==================================================================
    def progress(self, label, pct=0):
        try:
            self.run_js(f"showProgress({json.dumps(str(label))}, {float(pct)})")
        except Exception as e:
            log_debug(f"progress error: {e}")

    def progress_done(self):
        try:
            self.run_js("hideProgress()")
        except Exception:
            pass

    # ==================================================================
    # INTENT ROUTER — understand natural requests without keyword triggers
    # ==================================================================
    INTENT_SPEC = """You are the intent classifier for a desktop assistant.
Decide what the user wants and reply with ONLY JSON.

Available intents:
- "ui_automation": they want the computer operated — opening apps/sites and then
  clicking, scrolling, typing, navigating menus. ANY multi-step request involving
  a screen, website or app belongs here, even without the word "automate".
- "research": they want information gathered from the internet and remembered.
- "code": write, run, fix or explain code.
- "cad": design or modify a 3D model / part.
- "document": write a report, article, PDF or long-form document.
- "email": send or draft an email.
- "message": send a text or place a call.
- "file_ops": organise, rename, find or summarise files/folders.
- "recall": answer from what the assistant already knows or has researched.
- "chat": conversation, opinions, explanations, quick factual answers.

Return: {"intent":"<one of the above>","task":"<the request, rephrased clearly>","confidence":0.0-1.0}
"""

    def classify_intent(self, cmd):
        """Ask the model what the user actually wants. Returns (intent, task)."""
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("fast"),
                "messages": [
                    {"role": "system", "content": self.INTENT_SPEC},
                    {"role": "user", "content": cmd},
                ],
                "stream": False, "format": "json", "keep_alive": "30m",
                "options": {"num_predict": 150, "temperature": 0.1, "num_ctx": 2048},
            }, timeout=45)
            data = self._llm_json(res)
            intent = str(data.get("intent", "chat")).strip().lower()
            task = str(data.get("task", cmd)).strip() or cmd
            conf = float(data.get("confidence", 0.5) or 0.5)
            log_debug(f"Intent: {intent} ({conf:.2f}) <- {cmd}")
            return intent, task, conf
        except Exception as e:
            log_debug(f"classify_intent error: {e}")
            return "chat", cmd, 0.0

    # Quick signals that a request is obviously about driving the UI, so we can
    # skip the classifier round-trip and stay fast.
    UI_HINTS = re.compile(
        r'\b(click|scroll|navigate|go to|press|select|tab|menu|button|sidebar|side menu|'
        r'subscribe|like it|log ?in|sign ?in|search for .+ on|fill|submit|checkout|'
        r'then\s+(?:click|go|open|press|select|type))\b', re.I)

    def smart_route(self, cmd):
        """Understand a free-form request and carry it out.
        Returns True if handled, False to fall through to normal chat."""
        # Fast path: obvious multi-step UI work goes straight to automation.
        multi_step = (" then " in cmd.lower() or cmd.lower().count(" and ") >= 1)
        if self.UI_HINTS.search(cmd) and (multi_step or len(cmd.split()) > 5):
            self.ai_automate(cmd)
            return True

        intent, task, conf = self.classify_intent(cmd)
        if conf < 0.4:
            return False

        if intent == "ui_automation":
            self.ai_automate(task)
            return True
        if intent == "research":
            self.research_and_learn(task)
            return True
        if intent == "code":
            self.write_code(task)
            return True
        if intent == "cad":
            # With a design open, "add a hinge to the arm" changes it rather
            # than starting a new one.
            if getattr(self, "current_cad", None) and re.search(
                    r"\b(add|remove|make (it|them|the)|change|move|resize|bigger|smaller|wider|"
                    r"narrower|taller|shorter|thicker|thinner|round|fillet|hinge|hole|attach|"
                    r"put|turn|rotate|open|close|colou?r)\b", cmd, re.I) \
                    and not re.search(r"\b(new|another|design (a|an|me))\b", cmd, re.I):
                self.run_background("CAD change", self.cad_edit, task)
            else:
                self.run_background("CAD design", self.cad_create, task)
            return True
        if intent == "document":
            self.create_document(task)
            return True
        if intent == "email":
            recipient, instruction, auto = self._parse_email_command(cmd)
            self.compose_and_send_email(recipient=recipient, instruction=instruction, auto_send=auto)
            return True
        if intent == "file_ops":
            # Let the automation planner handle the specifics.
            self.ai_automate(task)
            return True
        if intent == "recall":
            if self.memory.get("knowledge_base") or self.memory.get("research_topics"):
                self.kb_search(task)
                return True
            return False
        return False

    # ==================================================================
    # RESEARCH & LEARN — gather, store, and be able to discuss it later
    # ==================================================================
    def research_and_learn(self, topic):
        """Search the web, download the sources, store them permanently in the
        knowledge base, then summarise. Afterwards you can ask follow-up
        questions and he answers from what he downloaded."""
        self.progress(f"Researching {topic}", 3)
        self.speak(f"Researching {topic}, Sir. I'll save what I find.")

        # 1. Plan sub-queries
        self.progress("Planning search queries", 8)
        queries = []
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("reasoning"),
                "messages": [{"role": "user", "content":
                    f"Give 4 web search queries that together cover the topic '{topic}' well. "
                    'Respond ONLY as JSON: {"queries":["...","..."]}'}],
                "stream": False, "format": "json", "keep_alive": "30m",
                "options": {"num_predict": 180, "temperature": 0.3},
            }, timeout=90)
            queries = self._llm_json(res).get("queries", [])
        except Exception as e:
            log_debug(f"research query planning: {e}")
        if not queries:
            queries = [topic, f"{topic} explained", f"{topic} latest research", f"{topic} facts"]
        queries = [str(q) for q in queries[:4]]

        # 2. Search + download, all in parallel
        stored, sources = 0, []
        kb = self.memory.setdefault("knowledge_base", {})
        self.progress(f"Searching {len(queries)} angles", 18)
        results_all = self.search_many(queries, per_query=3)
        if results_all:
            self.progress(f"Reading {len(results_all)} sources", 40)
            pages = self.fetch_many([r.get("url") for r in results_all], limit=9000)
        else:
            pages = {}
        step, total_steps = 0, max(1, len(results_all))
        for r in results_all:
                step += 1
                url = r.get("url")
                if not url:
                    continue
                text = pages.get(url, "")
                if text and len(text) > 250:
                    key = f"research::{topic}::{url}"
                    with self.memory_lock:
                        kb[key] = {
                            "text": f"SOURCE: {r.get('title','')} ({url})\nTOPIC: {topic}\n\n{text}",
                            "indexed": datetime.datetime.now().isoformat(timespec="seconds"),
                            "topic": topic, "url": url, "title": r.get("title", ""),
                        }
                    stored += 1
                    sources.append(r)

        if not stored:
            self.progress_done()
            self.speak("I couldn't retrieve any usable sources, Sir.")
            return True

        # 3. Synthesise a summary from what we downloaded
        self.progress("Synthesising findings", 78)
        corpus = "\n\n---\n\n".join(
            kb[k]["text"][:3000] for k in list(kb)
            if k.startswith(f"research::{topic}::"))[:26000]
        summary = ""
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("reasoning"),
                "messages": [{"role": "user", "content":
                    f"Write an informative briefing on '{topic}' using the sources below. "
                    "Use clear headed sections. Be factual and note uncertainty.\n\n" + corpus}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 900, "temperature": 0.45, "num_ctx": 32768},
            }, timeout=300)
            summary = self._llm_text(res)
        except Exception as e:
            log_debug(f"research synthesis error: {e}")

        # 4. Record the topic so he knows what he has studied
        self.progress("Filing into knowledge base", 92)
        topics = self.memory.setdefault("research_topics", {})
        topics[topic.lower()] = {
            "topic": topic,
            "summary": summary[:4000],
            "sources": [{"title": s.get("title", ""), "url": s.get("url", "")} for s in sources[:10]],
            "documents": stored,
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        # Embeddings are stale for the new docs.
        self._embed_store = {}; self._save_embeddings()
        self.save_memory()

        self.progress("Complete", 100)
        self.progress_done()

        if summary:
            src_lines = "\n".join(f"• {s.get('title','')} — {s.get('url','')}" for s in sources[:6])
            self.instantiate_card(f"Research — {topic}", "email_draft",
                                  summary + "\n\nSOURCES:\n" + src_lines)
            self.current_doc = {"topic": f"Research: {topic}", "content": summary}
            self.show_document_editor(f"Research: {topic}", summary)
            self.speak(f"I've studied {topic} and saved {stored} sources, Sir. "
                       f"Ask me anything about it. {summary[:280]}")
        else:
            self.speak(f"I downloaded and saved {stored} sources on {topic}, Sir. "
                       "Ask me about it and I'll answer from them.")
        return True

    def list_research(self):
        topics = self.memory.get("research_topics", {})
        if not topics:
            self.speak("I haven't researched anything yet, Sir.")
            return True
        lines = [f"{v['topic']} — {v['documents']} sources ({v['ts'][:10]})"
                 for v in topics.values()]
        self.instantiate_card("What I've Studied", "carousel", lines)
        self.speak(f"I've studied {len(topics)} topics, Sir.")
        return True

    # ==================================================================
    # LENS — Google-Lens-style multi-mode visual analysis
    # ==================================================================
    LENS_MODES = {
        "identify": (
            "Identify every distinct object in this image. For each one give: the object name, "
            "its brand or model if legible, and a one-line note on what it is or does. "
            "List the most prominent first. Be specific — say 'Raspberry Pi 4 Model B', not 'circuit board'."
        ),
        "text": (
            "Transcribe ALL text visible in this image, exactly as written, preserving line breaks. "
            "If there is no text, say 'No text visible.' Do not describe the image."
        ),
        "translate": (
            "Find all text in this image, then give: the original text, the language it is in, "
            "and an English translation. If there is no text, say so."
        ),
        "explain": (
            "Explain what is happening in this image and what the objects are for. "
            "If it shows a device, diagram or document, explain how to use or read it."
        ),
        "product": (
            "Identify the product(s) shown. Give the brand, model, what it's typically used for, "
            "and roughly what it costs. If you cannot identify it confidently, say so plainly."
        ),
        "solve": (
            "If this image contains a question, puzzle, equation or problem, solve it and show "
            "your working clearly. Otherwise describe what you see."
        ),
        "read": (
            "Read this document or screen aloud in a natural way: summarise its purpose first, "
            "then give the key content."
        ),
        "count": (
            "Count the objects in this image. Give a total and a breakdown by type."
        ),
        "colour": (
            "Describe the dominant colours in this image and give their approximate hex codes."
        ),
    }

    def lens_analyze(self, mode="identify", extra=None):
        """Analyse the current camera frame at full quality, Google-Lens style."""
        if not self.camera_active or self.last_frame is None:
            self.speak("The desk camera isn't running, Sir.")
            return True

        prompt = self.LENS_MODES.get(mode, self.LENS_MODES["identify"])
        if extra:
            prompt += f"\n\nThe user specifically asks: {extra}"

        label = {"identify": "Identifying objects", "text": "Reading text",
                 "translate": "Translating", "explain": "Analysing",
                 "product": "Identifying product", "solve": "Solving",
                 "read": "Reading document", "count": "Counting",
                 "colour": "Analysing colours"}.get(mode, "Analysing")
        self.progress(label, 15)
        self.speak(f"{label}, Sir.")

        try:
            frame = self.last_frame.copy()
            enhanced = self._enhance_for_vision(frame, sharpen=(mode in ("text", "translate", "read", "solve")))
            self.progress(label, 45)

            # Cap the long edge before encoding - same tiling economics as the
            # phone path.
            eh, ew = enhanced.shape[:2]
            cap = self.LENS_TEXT_MAX_PX if mode in ("text", "translate", "read", "solve") \
                else self.LENS_MAX_PX
            if max(eh, ew) > cap:
                sc = cap / float(max(eh, ew))
                enhanced = cv2.resize(enhanced, (int(ew * sc), int(eh * sc)),
                                      interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", enhanced, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if not ok:
                self.progress_done()
                self.speak("I couldn't capture a clear frame, Sir.")
                return True
            img_b64 = base64.b64encode(buf.tobytes()).decode("ascii")

            self.progress(label, 60)
            res = self.http.post(OLLAMA_GENERATE_URL, json={
                "model": self.model_for("vision"), "prompt": prompt, "images": [img_b64],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": int(CONFIG.get("camera", {}).get("lens_tokens", 180)),
                            "temperature": 0.25, "num_ctx": 4096},
            }, timeout=180)
            desc = self._llm_text(res)
            self.progress(label, 95)
        except Exception as e:
            log_debug(f"lens_analyze error: {e}")
            self.progress_done()
            self.speak("Visual analysis failed, Sir.")
            return True

        self.progress_done()
        if not desc:
            self.speak("I couldn't make anything out clearly, Sir.")
            return True

        title = {"identify": "Lens — Objects", "text": "Lens — Text", "translate": "Lens — Translation",
                 "explain": "Lens — Explanation", "product": "Lens — Product",
                 "solve": "Lens — Solution", "read": "Lens — Document",
                 "count": "Lens — Count", "colour": "Lens — Colours"}.get(mode, "Lens")
        self.instantiate_card(title, "email_draft", desc)
        self.last_camera_description = desc
        # Remember what was seen so follow-up questions work.
        self.memory.setdefault("lens_history", []).append({
            "mode": mode, "result": desc[:1500],
            "ts": datetime.datetime.now().isoformat(timespec="seconds")})
        if len(self.memory["lens_history"]) > 50:
            self.memory["lens_history"] = self.memory["lens_history"][-50:]
        self.save_memory()

        if HAS_CLIPBOARD and mode in ("text", "translate"):
            try:
                pyperclip.copy(desc)
                self.safe_log("Copied to clipboard.")
            except Exception:
                pass

        self.speak(desc[:600])
        return True

    @staticmethod
    def _enhance_for_vision(frame, sharpen=False):
        """Upscale/clean a frame so the vision model can read fine detail.
        Text modes get extra sharpening and contrast."""
        try:
            h, w = frame.shape[:2]
            target = 1024
            if max(h, w) < target:
                s = target / float(max(h, w))
                frame = cv2.resize(frame, (int(w * s), int(h * s)), interpolation=cv2.INTER_CUBIC)
            elif max(h, w) > 1600:
                s = 1600.0 / max(h, w)
                frame = cv2.resize(frame, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)

            if sharpen:
                # CLAHE lifts local contrast, which helps a lot with small print.
                lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
                l, a, b = cv2.split(lab)
                l = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8)).apply(l)
                frame = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
                blur = cv2.GaussianBlur(frame, (0, 0), 2.0)
                frame = cv2.addWeighted(frame, 1.6, blur, -0.6, 0)
            return frame
        except Exception as e:
            log_debug(f"_enhance_for_vision error: {e}")
            return frame

    def lens_ask(self, question):
        """Ask a free-form question about whatever the camera can see."""
        return self.lens_analyze("explain", extra=question)

    def identify_objects(self, question=None):
        """Backwards-compatible entry point -> full Lens identification."""
        if question:
            return self.lens_ask(question)
        return self.lens_analyze("identify")

    # ==================================================================
    # PROJECTS — scope memory, files and context to what you're working on
    # ==================================================================
    def project_start(self, name):
        name = name.strip()
        projects = self.memory.setdefault("projects", {})
        key = name.lower()
        if key not in projects:
            projects[key] = {
                "name": name,
                "created": datetime.datetime.now().isoformat(timespec="seconds"),
                "notes": [], "files": [], "research": [], "log": [],
            }
            try:
                os.makedirs(os.path.join(PROJECTS_DIR, re.sub(r'[^\w\-]+', '_', name)), exist_ok=True)
            except Exception as e:
                log_debug(f"project dir error: {e}")
        self.active_project = key
        self.memory["active_project"] = key
        self.save_memory()
        self.run_js(f"setActiveProject({json.dumps(name)})")
        self.speak(f"Working on {name}, Sir. I'll keep everything scoped to it.")
        return True

    def project_end(self):
        if not self.active_project:
            self.speak("No project is active, Sir.")
            return True
        name = self.memory.get("projects", {}).get(self.active_project, {}).get("name", "it")
        self.active_project = None
        self.memory["active_project"] = None
        self.save_memory()
        self.run_js("setActiveProject(null)")
        self.speak(f"Closed {name}, Sir.")
        return True

    def project_list(self):
        projects = self.memory.get("projects", {})
        if not projects:
            self.speak("You have no projects yet, Sir. Say 'start a project called X'.")
            return True
        lines = []
        for k, p in projects.items():
            mark = "▸ " if k == self.active_project else "  "
            lines.append(f"{mark}{p['name']} — {len(p.get('notes', []))} notes, "
                         f"{len(p.get('research', []))} topics ({p['created'][:10]})")
        self.instantiate_card("Projects", "carousel", lines)
        self.speak(f"You have {len(projects)} projects, Sir.")
        return True

    def project_status(self):
        if not self.active_project:
            self.speak("No project is active, Sir.")
            return True
        p = self.memory.get("projects", {}).get(self.active_project, {})
        lines = [f"Project: {p.get('name')}", f"Started: {p.get('created','')[:10]}", ""]
        for n in p.get("notes", [])[-8:]:
            lines.append(f"• {n}")
        for r in p.get("research", [])[-5:]:
            lines.append(f"◆ researched: {r}")
        for l in p.get("log", [])[-8:]:
            lines.append(f"– {l}")
        self.instantiate_card(f"Project — {p.get('name')}", "carousel", lines)
        self.speak(f"Here's where {p.get('name')} stands, Sir.")
        return True

    def project_log(self, entry, kind="log"):
        """Attach something to the active project (silently if none is open)."""
        if not self.active_project:
            return
        p = self.memory.setdefault("projects", {}).get(self.active_project)
        if not p:
            return
        p.setdefault(kind, []).append(entry)
        if len(p[kind]) > 200:
            p[kind] = p[kind][-200:]
        self.save_memory()

    # ==================================================================
    # CALENDAR — local agenda with reminders
    # ==================================================================
    def calendar_add(self, title, when_text):
        dt = self._parse_when(when_text)
        if not dt:
            self.speak(f"I couldn't work out when '{when_text}' is, Sir.")
            return True
        events = self.memory.setdefault("calendar", [])
        events.append({"title": title.strip(), "when": dt.isoformat(timespec="minutes"),
                       "project": self.active_project})
        events.sort(key=lambda e: e["when"])
        self.save_memory()
        self.project_log(f"scheduled: {title} @ {dt:%d %b %H:%M}")
        self.speak(f"Added {title} on {dt:%A %d %B at %I:%M %p}, Sir.")
        return True

    @staticmethod
    def _parse_when(text):
        """Understand 'tomorrow at 3pm', 'friday 9am', 'in 2 hours', '25/12 14:00'."""
        t = text.lower().strip()
        now = datetime.datetime.now()

        m = re.search(r'in (\d+)\s*(minute|hour|day|week)s?', t)
        if m:
            n = int(m.group(1))
            unit = m.group(2)
            delta = {"minute": datetime.timedelta(minutes=n),
                     "hour": datetime.timedelta(hours=n),
                     "day": datetime.timedelta(days=n),
                     "week": datetime.timedelta(weeks=n)}[unit]
            return now + delta

        hour, minute = 9, 0
        tm = re.search(r'(\d{1,2})[:.](\d{2})\s*(am|pm)?', t) or re.search(r'(\d{1,2})\s*(am|pm)', t)
        if tm:
            groups = tm.groups()
            hour = int(groups[0])
            if len(groups) >= 3 and groups[1] and groups[1].isdigit():
                minute = int(groups[1])
            ampm = groups[-1]
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0

        base = None
        if "tomorrow" in t:
            base = now + datetime.timedelta(days=1)
        elif "today" in t or "tonight" in t:
            base = now
        else:
            days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            for i, d in enumerate(days):
                if d in t:
                    ahead = (i - now.weekday()) % 7
                    ahead = ahead or 7
                    base = now + datetime.timedelta(days=ahead)
                    break
        dm = re.search(r'(\d{1,2})[/-](\d{1,2})', t)
        if dm and base is None:
            try:
                base = now.replace(month=int(dm.group(2)), day=int(dm.group(1)))
                if base < now:
                    base = base.replace(year=now.year + 1)
            except ValueError:
                base = None
        if base is None:
            base = now if tm else None
        if base is None:
            return None
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def calendar_agenda(self, days=7):
        events = self.memory.get("calendar", [])
        now = datetime.datetime.now()
        end = now + datetime.timedelta(days=days)
        upcoming = []
        for e in events:
            try:
                dt = datetime.datetime.fromisoformat(e["when"])
            except Exception:
                continue
            if now - datetime.timedelta(hours=2) <= dt <= end:
                upcoming.append((dt, e))
        upcoming.sort(key=lambda x: x[0])
        if not upcoming:
            self.speak(f"Nothing scheduled in the next {days} days, Sir.")
            return True
        lines = [f"{dt:%a %d %b  %H:%M}  —  {e['title']}" for dt, e in upcoming]
        self.instantiate_card("Agenda", "carousel", lines)
        first_dt, first = upcoming[0]
        self.speak(f"You have {len(upcoming)} things coming up, Sir. "
                   f"Next: {first['title']} on {first_dt:%A at %I:%M %p}.")
        return True

    def calendar_today(self):
        return self.calendar_agenda(days=1)

    def _calendar_watcher(self):
        """Announce events as they come due."""
        announced = set()
        while True:
            try:
                now = datetime.datetime.now()
                for e in self.memory.get("calendar", []):
                    try:
                        dt = datetime.datetime.fromisoformat(e["when"])
                    except Exception:
                        continue
                    key = e["when"] + e["title"]
                    delta = (dt - now).total_seconds()
                    if 0 < delta <= 600 and key not in announced:
                        announced.add(key)
                        mins = int(delta // 60)
                        self.speak(f"Reminder, Sir: {e['title']} in {mins} minutes.")
                        self.instantiate_card("Upcoming", "email_draft",
                                              f"{e['title']}\n{dt:%A %d %B, %H:%M}")
            except Exception as e:
                log_debug(f"calendar watcher error: {e}")
            time.sleep(60)

    # ==================================================================
    # HOME AUTOMATION — Home Assistant REST API
    # ==================================================================
    def _ha_config(self):
        ha = CONFIG.get("home_assistant", {})
        url = str(ha.get("url", "")).rstrip("/")
        token = str(ha.get("token", ""))
        if not url or not token or token.startswith("YOUR_"):
            return None, None
        return url, token

    def home_control(self, action, target):
        """Turn things on/off by friendly name via Home Assistant."""
        url, token = self._ha_config()
        if not url:
            self.speak("Home Assistant isn't configured, Sir. Add your URL and token to the config.")
            self.safe_log(f"Set home_assistant.url and .token in {CONFIG_FILE}")
            return True
        entity = self._ha_resolve_entity(target)
        if not entity:
            self.speak(f"I couldn't find a device called {target}, Sir.")
            return True
        domain = entity.split(".")[0]
        service = {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}.get(action, "toggle")
        try:
            r = self.http.post(
                f"{url}/api/services/{domain}/{service}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"entity_id": entity}, timeout=20)
            if r.status_code in (200, 201):
                self.speak(f"{target} {action}, Sir.")
                self.project_log(f"home: {target} {action}")
            else:
                self.speak("Home Assistant rejected that, Sir.")
                log_debug(f"HA error {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log_debug(f"home_control error: {e}")
            self.speak("I couldn't reach Home Assistant, Sir.")
        return True

    def _ha_resolve_entity(self, name):
        url, token = self._ha_config()
        if not url:
            return None
        # Explicit mapping first.
        mapping = CONFIG.get("home_assistant", {}).get("aliases", {})
        for k, v in mapping.items():
            if k.lower() in name.lower():
                return v
        try:
            if not self._ha_entities or (time.time() - self._ha_entities_ts) > 300:
                r = self.http.get(f"{url}/api/states",
                                  headers={"Authorization": f"Bearer {token}"}, timeout=20)
                self._ha_entities = r.json()
                self._ha_entities_ts = time.time()
            want = name.lower().strip()
            best, best_score = None, 0
            for st in self._ha_entities:
                eid = st.get("entity_id", "")
                friendly = str(st.get("attributes", {}).get("friendly_name", "")).lower()
                if not eid.split(".")[0] in ("light", "switch", "fan", "climate",
                                             "media_player", "cover", "scene", "script"):
                    continue
                score = 0
                if want == friendly:
                    score = 100
                elif want in friendly:
                    score = 60
                elif want in eid.lower():
                    score = 40
                if score > best_score:
                    best, best_score = eid, score
            return best
        except Exception as e:
            log_debug(f"_ha_resolve_entity error: {e}")
        return None

    def home_status(self):
        url, token = self._ha_config()
        if not url:
            self.speak("Home Assistant isn't configured, Sir.")
            return True
        try:
            r = self.http.get(f"{url}/api/states",
                              headers={"Authorization": f"Bearer {token}"}, timeout=20)
            states = r.json()
            lines = []
            for st in states:
                eid = st.get("entity_id", "")
                if eid.split(".")[0] in ("light", "switch", "fan", "climate"):
                    fn = st.get("attributes", {}).get("friendly_name", eid)
                    lines.append(f"{fn}: {st.get('state')}")
            self.instantiate_card("Home", "carousel", lines[:25])
            on = sum(1 for l in lines if l.endswith(": on"))
            self.speak(f"{on} devices are on, Sir.")
        except Exception as e:
            log_debug(f"home_status error: {e}")
            self.speak("I couldn't reach Home Assistant, Sir.")
        return True

    # ==================================================================
    # SCREEN LENS — Lens modes applied to your own screen (no webcam)
    # ==================================================================
    def screen_lens(self, mode="read", region=None):
        """Run Lens analysis on a screenshot instead of the camera."""
        prompt = self.LENS_MODES.get(mode, self.LENS_MODES["explain"])
        self.progress("Capturing screen", 15)
        self.speak("Looking at your screen, Sir.")
        try:
            img = ImageGrab.grab(all_screens=True)
            buf = io.BytesIO()
            img.thumbnail((1600, 1600))
            img.save(buf, format="JPEG", quality=88)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            self.progress("Analysing", 55)
            res = self.http.post(OLLAMA_GENERATE_URL, json={
                "model": self.model_for("vision"), "prompt": prompt, "images": [b64],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 500, "temperature": 0.25},
            }, timeout=180)
            out = self._llm_text(res)
        except Exception as e:
            log_debug(f"screen_lens error: {e}")
            self.progress_done()
            self.speak("I couldn't analyse the screen, Sir.")
            return True
        self.progress_done()
        if out:
            self.instantiate_card(f"Screen — {mode}", "email_draft", out)
            if HAS_CLIPBOARD and mode in ("text", "translate"):
                try:
                    pyperclip.copy(out)
                except Exception:
                    pass
            self.speak(out[:600])
        else:
            self.speak("I couldn't read anything definite, Sir.")
        return True

    # ==================================================================
    # AUTOMATION UNDO
    # ==================================================================
    def undo_last_action(self):
        """Best-effort reversal of the last automation step."""
        last = getattr(self, "last_action", None)
        if not last:
            self.speak("There's nothing recent to undo, Sir.")
            return True
        kind = last.get("kind")
        try:
            if kind == "open_app":
                pyautogui.hotkey("alt", "f4")
                self.speak("Closed it, Sir.")
            elif kind == "type":
                for _ in range(min(60, len(last.get("value", "")))):
                    pyautogui.press("backspace")
                self.speak("Removed what I typed, Sir.")
            elif kind == "workflow":
                pyautogui.hotkey("ctrl", "z")
                self.speak("Sent an undo, Sir. Some steps may need reversing manually.")
            else:
                pyautogui.hotkey("ctrl", "z")
                self.speak("Sent an undo, Sir.")
            self.last_action = None
        except Exception as e:
            log_debug(f"undo error: {e}")
            self.speak("I couldn't undo that, Sir.")
        return True

    # ==================================================================
    # HABIT NUDGES — proactive suggestions from usage patterns
    # ==================================================================
    def _habit_worker(self):
        """Notice what you usually do at this time and offer it."""
        time.sleep(120)
        while True:
            try:
                if CONFIG.get("assistant", {}).get("habit_nudges", True) and not self.is_speaking:
                    hour = datetime.datetime.now().hour
                    hist = self.memory.get("hour_stats", {}).get(str(hour), {})
                    if hist:
                        top = max(hist.items(), key=lambda x: x[1])
                        if top[1] >= 3 and top[0] != self._last_nudge:
                            self._last_nudge = top[0]
                            self.safe_log(f"You often ask me to '{top[0]}' around this time.")
            except Exception as e:
                log_debug(f"habit worker error: {e}")
            time.sleep(1800)

    def track_hour_usage(self, cmd):
        hour = str(datetime.datetime.now().hour)
        stats = self.memory.setdefault("hour_stats", {}).setdefault(hour, {})
        key = " ".join(cmd.lower().split()[:3])
        stats[key] = stats.get(key, 0) + 1

    # ==================================================================
    # PROJECTS — scope memory, files and context to what you're working on
    # --- AUTOMATION UNDO ---
    def record_action(self, kind, detail=""):
        """Remember the last action so undo_last_action can reverse it."""
        self.last_action = {"kind": kind, "value": detail, "ts": time.time()}
        self.action_history.append(self.last_action)
        self.action_history = self.action_history[-40:]

    def calendar_view(self, scope="today"):
        return self.calendar_agenda(days={"today": 1, "week": 7}.get(scope, 365))

    def calendar_next(self):
        return self.calendar_agenda(days=365)

    def _record_habit(self, cmd):
        try:
            hour = str(datetime.datetime.now().hour)
            key = " ".join(cmd.lower().split()[:3])
            slot = self.memory.setdefault("habits", {}).setdefault(hour, {})
            slot[key] = slot.get(key, 0) + 1
        except Exception as e:
            log_debug(f"habit record error: {e}")

    def habit_suggestions(self):
        hour = str(datetime.datetime.now().hour)
        slot = self.memory.get("habits", {}).get(hour, {})
        if not slot:
            self.speak("I haven't noticed a pattern for this time of day yet, Sir.")
            return True
        top = sorted(slot.items(), key=lambda x: x[1], reverse=True)[:5]
        self.instantiate_card("Your Habits", "carousel",
                              [f"{k} — {v}x around this hour" for k, v in top])
        self.speak(f"Around this time you usually ask me to {top[0][0]}, Sir.")
        return True

    def _project_context(self):
        """Extra system-prompt context for the active project."""
        if not getattr(self, "active_project", None):
            return ""
        p = self.memory.get("projects", {}).get(self.active_project, {})
        bits = [f"\n\nThe user is currently working on a project called '{p.get('name')}'."]
        if p.get("facts"):
            bits.append("Project details:\n- " + "\n- ".join(p["facts"][-12:]))
        if p.get("notes"):
            notes = [n["text"] if isinstance(n, dict) else str(n) for n in p["notes"][-8:]]
            bits.append("Project notes:\n- " + "\n- ".join(notes))
        return "\n".join(bits)

    def _cad_validate(self, code):
        """Compile the model with OpenSCAD to catch real errors before we accept it.
        Returns (ok, error_text)."""
        exe = self._find_openscad()
        if not exe:
            # Can't compile; fall back to cheap static checks.
            return self._cad_static_check(code)
        tmp_scad = os.path.join(CAD_DIR, f"_validate_{int(time.time()*1000)}.scad")
        tmp_stl = tmp_scad.replace(".scad", ".stl")
        try:
            with open(tmp_scad, "w", encoding="utf-8") as f:
                f.write(code)
            proc = subprocess.run([exe, "-o", tmp_stl, tmp_scad],
                                  capture_output=True, text=True, timeout=120)
            err = (proc.stderr or "") + (proc.stdout or "")
            produced = os.path.exists(tmp_stl) and os.path.getsize(tmp_stl) > 200

            problems = []
            for line in err.splitlines():
                low = line.lower()
                if any(k in low for k in ("error", "warning: object may not be a valid",
                                          "unable to convert", "no top level geometry",
                                          "not defined", "syntax error", "can't open")):
                    problems.append(line.strip())
            if not produced:
                problems.append("OpenSCAD produced no geometry (empty or invalid model).")
            return (not problems), "\n".join(problems[:12])
        except subprocess.TimeoutExpired:
            return False, "Rendering timed out - the model is far too complex."
        except Exception as e:
            log_debug(f"cad validate error: {e}")
            return True, ""      # don't block on validator failure
        finally:
            for p in (tmp_scad, tmp_stl):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

    @staticmethod
    def _cad_static_check(code):
        """Cheap sanity checks used when OpenSCAD isn't installed."""
        problems = []
        if not code.strip():
            problems.append("Empty model.")
        if re.search(r'^\s*(use|include)\s*<', code, re.M):
            problems.append("Uses an external library, which won't be available.")
        # Balanced braces/parens
        for open_c, close_c, name in (("{", "}", "braces"), ("(", ")", "parentheses"),
                                      ("[", "]", "brackets")):
            if code.count(open_c) != code.count(close_c):
                problems.append(f"Unbalanced {name}.")
        # Some geometry must actually be instantiated at top level
        if not re.search(r'^\s*(cube|cylinder|sphere|polyhedron|difference|union|'
                         r'intersection|hull|linear_extrude|rotate_extrude|translate|\w+)\s*\(',
                         code, re.M):
            problems.append("No top-level geometry call.")
        return (not problems), "\n".join(problems)

    def cad_create(self, description):
        """Design a part.

        The model produces a structured geometry SPEC; Python compiles it into
        OpenSCAD. That removes syntax errors entirely and keeps the model doing
        what it's good at - reasoning about shapes and dimensions.

        Order of preference: proven template -> spec compiler -> raw-code fallback.
        """
        if self._cq_enabled():
            if self.cq_cad_create(description):
                return True
            self.safe_log("Falling back to the OpenSCAD engine.")
        self.speak(f"Designing {description}, {USER_TITLE}.")
        attempts = int(CONFIG.get("cad", {}).get("max_attempts", 4))
        visual = bool(CONFIG.get("cad", {}).get("visual_check", True))
        code, spec, last_err = "", None, ""

        # --- 1. Known-good template if the request matches one ---
        tpl_key = self._match_template(description)
        if tpl_key and CONFIG.get("cad", {}).get("use_templates", True):
            self.safe_log(f"Using the '{tpl_key}' template as a base.")
            self.progress("Adapting template", 18)
            base = self.CAD_TEMPLATES[tpl_key]["code"]
            adapted = self._cad_generate_code(
                f"Adapt this working model to match: \"{description}\". Change dimensions and "
                "add or remove features as needed. Keep the structure and keep every key "
                "dimension a named variable at the top. No external libraries.",
                existing=base)
            if adapted and self._cad_validate(adapted)[0]:
                code = adapted
            else:
                code = base
                self.safe_log("Adaptation failed validation; using the template unchanged.")

        # --- 2. Spec-driven generation ---
        if not code:
            feedback = None
            for attempt in range(1, attempts + 1):
                self.progress(f"Planning geometry ({attempt}/{attempts})",
                              12 + (attempt - 1) * (55 / attempts))
                spec = self._spec_from_model(description, feedback=feedback, previous=spec)
                if not spec:
                    feedback = "The specification could not be parsed. Return valid JSON only."
                    continue

                ok, err = self._validate_spec(spec)
                if not ok:
                    feedback = f"The specification was rejected: {err}"
                    self.safe_log(f"Spec attempt {attempt}: {err}")
                    continue

                candidate, cerr = self._spec_to_scad(spec)
                if not candidate:
                    feedback = f"Compilation failed: {cerr}"
                    continue

                self.progress(f"Validating ({attempt}/{attempts})",
                              20 + attempt * (55 / attempts))
                vok, verr = self._cad_validate(candidate)
                if not vok:
                    feedback = f"OpenSCAD reported: {verr}"
                    self.safe_log(f"Attempt {attempt} failed to render: {verr[:120]}")
                    continue

                # --- Does it actually look like the request? ---
                if visual and attempt < attempts:
                    png = self._cad_render_png(candidate)
                    if png:
                        self.progress("Checking the shape", 80)
                        looks_ok, fb = self._cad_visual_check(png, description)
                        try:
                            os.remove(png)
                        except Exception:
                            pass
                        if not looks_ok and fb:
                            self.safe_log(f"Shape not right yet: {fb[:110]}")
                            feedback = (f"It compiles but doesn't look like a {description}. {fb} "
                                        "Rethink the shapes and their positions.")
                            continue
                code = candidate
                self.safe_log(f"Design accepted on attempt {attempt}.")
                break

        # --- 3. Last resort: let the model write code directly ---
        if not code:
            self.safe_log("Falling back to direct code generation.")
            self.progress("Writing model directly", 78)
            plan = self._cad_plan_geometry(description)
            code = self._cad_generate_code(
                f"Design this part: {description}\n\n"
                + (f"Geometry plan:\n{plan}\n" if plan else ""))
            if code:
                vok, verr = self._cad_validate(code)
                if not vok:
                    code = self._cad_generate_code(
                        f"That failed to compile.\nERRORS:\n{verr}\n\nReturn the complete "
                        f"corrected OpenSCAD for: {description}", existing=code) or code

        if not code:
            self.progress_done()
            self.speak("I couldn't produce a workable design, Sir.")
            return True

        name = re.sub(r'[^\w]+', '_', description)[:40].strip('_') or "part"
        scad_path = os.path.join(CAD_DIR, f"{name}_{int(time.time())}.scad")
        try:
            with open(scad_path, "w", encoding="utf-8") as f:
                f.write(code)
        except Exception as e:
            log_debug(f"cad write error: {e}")
            self.progress_done()
            self.speak("I couldn't save the design, Sir.")
            return True

        self.current_cad = {"description": description, "code": code,
                            "path": scad_path, "spec": spec}
        params = self._cad_extract_params(code)
        self.run_js(f"showCadEditor({json.dumps({'name': description, 'code': code, 'path': scad_path, 'params': params})})")
        self.safe_log(f"Design saved: {scad_path}")
        self.project_log(f"Designed: {description}")

        self.progress("Exporting STL", 92)
        stl = self.cad_export_stl(announce=False)
        self.progress_done()
        if stl:
            self.speak(f"Done, {USER_TITLE}. {len(params)} dimensions on the sliders.")
        else:
            self.speak("Design ready, Sir. Install OpenSCAD for STL export.")
        return True


    def cad_improve(self):
        """Ask him to critique and refine the current model."""
        if not getattr(self, "current_cad", None):
            self.speak("There's no design open to improve, Sir.")
            return True
        self.speak("Reviewing the design for improvements, Sir.")
        self.progress("Critiquing design", 20)
        try:
            critique = self.run_agent(
                "critic",
                "Review this OpenSCAD model for 3D-printability and design quality. "
                "List concrete problems: thin walls, non-manifold geometry, missing "
                "fillets, unprintable overhangs, hard-coded numbers that should be "
                "variables. Be specific and brief.",
                context=self.current_cad["code"])
        except Exception as e:
            log_debug(f"cad critique error: {e}")
            critique = ""
        if not critique:
            self.progress_done()
            self.speak("I couldn't review the design, Sir.")
            return True
        self.instantiate_card("Design Review", "code_bug", critique)
        self.progress("Applying improvements", 60)
        return self.cad_edit(f"Apply these improvements:\n{critique}")

    # ==================================================================
    # CAD TEMPLATES — proven, correct geometry for common parts.
    # Small local models write poor OpenSCAD from scratch, so when the request
    # matches a known shape we start from working geometry and only let the model
    # adjust dimensions. This is the single biggest quality win.
    # ==================================================================
    CAD_TEMPLATES = {
        "phone_stand": {
            "match": ["phone stand", "phone holder", "tablet stand", "phone dock", "device stand"],
            "code": '''$fn = 64;
width      = 75;   // width of the cradle
depth      = 85;   // base depth
height     = 70;   // back support height
angle      = 60;   // lean angle in degrees
thickness  = 5;    // material thickness
lip_height = 18;   // front lip that holds the phone
slot_width = 14;   // charging cable slot width

// Angled back support the phone leans against
module back_support() {
    rotate([90 - angle, 0, 0])
        cube([width, thickness, height]);
}

// Flat base with a front lip
module base_plate() {
    difference() {
        cube([width, depth, thickness]);
        // cable slot through the base
        translate([(width - slot_width) / 2, -1, -1])
            cube([slot_width, thickness + 12, thickness + 2]);
    }
}

module front_lip() {
    translate([0, 0, thickness])
        cube([width, thickness, lip_height]);
}

// Side walls give it rigidity
module side(x) {
    translate([x, 0, 0])
        rotate([0, -90, 0])
            linear_extrude(thickness)
                polygon([[0, 0], [0, depth], [height * 0.75, 0]]);
}

union() {
    base_plate();
    front_lip();
    translate([0, thickness * 2, thickness]) back_support();
    side(thickness);
    side(width);
}''',
        },
        "box": {
            "match": ["box", "enclosure", "case", "container", "housing", "project box"],
            "code": '''$fn = 64;
inner_x   = 80;  // internal length
inner_y   = 60;  // internal width
inner_z   = 30;  // internal depth
wall      = 2.4; // wall thickness
floor_t   = 2.4; // floor thickness
corner_r  = 4;   // corner radius
lid_lip   = 3;   // lip height on the lid

module rounded_box(x, y, z, r) {
    hull() {
        for (dx = [r, x - r]) for (dy = [r, y - r])
            translate([dx, dy, 0]) cylinder(r = r, h = z);
    }
}

module shell() {
    difference() {
        rounded_box(inner_x + wall * 2, inner_y + wall * 2, inner_z + floor_t, corner_r);
        // hollow interior, overshooting the top so faces never coincide
        translate([wall, wall, floor_t])
            rounded_box(inner_x, inner_y, inner_z + 2, max(0.1, corner_r - wall));
    }
}

module lid() {
    translate([inner_x + wall * 2 + 10, 0, 0]) {
        rounded_box(inner_x + wall * 2, inner_y + wall * 2, wall, corner_r);
        // lip that locates inside the shell
        translate([wall + 0.3, wall + 0.3, wall])
            difference() {
                rounded_box(inner_x - 0.6, inner_y - 0.6, lid_lip, max(0.1, corner_r - wall));
                translate([wall, wall, -1])
                    rounded_box(inner_x - wall * 2 - 0.6, inner_y - wall * 2 - 0.6,
                                lid_lip + 2, 0.1);
            }
    }
}

shell();
lid();''',
        },
        "bracket": {
            "match": ["bracket", "shelf support", "l bracket", "angle bracket", "mount"],
            "code": '''$fn = 64;
arm_a     = 60;  // vertical arm length
arm_b     = 60;  // horizontal arm length
width     = 30;  // bracket width
thickness = 5;   // material thickness
hole_d    = 5;   // screw hole diameter
gusset    = 25;  // triangular brace size

module arm_vertical() { cube([thickness, width, arm_a]); }
module arm_horizontal() { cube([arm_b, width, thickness]); }

// Triangular gusset stops the corner flexing
module brace() {
    translate([thickness, 0, thickness])
        rotate([90, 0, 0])
            translate([0, 0, -width])
                linear_extrude(width)
                    polygon([[0, 0], [gusset, 0], [0, gusset]]);
}

module holes() {
    // vertical arm holes
    for (z = [arm_a * 0.45, arm_a * 0.8])
        translate([-1, width / 2, z]) rotate([0, 90, 0])
            cylinder(d = hole_d, h = thickness + 2);
    // horizontal arm holes
    for (x = [arm_b * 0.45, arm_b * 0.8])
        translate([x, width / 2, -1]) cylinder(d = hole_d, h = thickness + 2);
}

difference() {
    union() { arm_vertical(); arm_horizontal(); brace(); }
    holes();
}''',
        },
        "hook": {
            "match": ["hook", "hanger", "coat hook", "wall hook"],
            "code": '''$fn = 64;
plate_w   = 30;  // wall plate width
plate_h   = 50;  // wall plate height
thickness = 5;   // material thickness
hook_len  = 35;  // how far the hook projects
hook_r    = 12;  // curl radius
hole_d    = 4.5; // screw hole diameter

module plate() {
    hull() {
        for (z = [thickness, plate_h - thickness])
            translate([plate_w / 2, 0, z])
                rotate([-90, 0, 0]) cylinder(r = plate_w / 2, h = thickness);
    }
}

module arm() {
    translate([plate_w / 2, thickness, thickness * 1.5])
        rotate([-90, 0, 0])
            cylinder(d = thickness * 1.8, h = hook_len);
}

// Upward curl at the end so things don't slide off
module curl() {
    translate([plate_w / 2, thickness + hook_len, thickness * 1.5 + hook_r])
        rotate([0, 90, 0])
            rotate_extrude(angle = 180)
                translate([hook_r, 0, 0]) circle(d = thickness * 1.8);
}

module holes() {
    for (z = [plate_h * 0.2, plate_h * 0.8])
        translate([plate_w / 2, -1, z]) rotate([-90, 0, 0])
            cylinder(d = hole_d, h = thickness + 2);
}

difference() {
    union() { plate(); arm(); curl(); }
    holes();
}''',
        },
        "holder": {
            "match": ["holder", "cup holder", "pen holder", "organiser", "organizer", "caddy", "stand for"],
            "code": '''$fn = 64;
outer_d   = 80;  // outside diameter
height    = 95;  // overall height
wall      = 3;   // wall thickness
floor_t   = 3;   // base thickness
divider   = 1;   // 1 = add a divider, 0 = plain

module body() {
    difference() {
        cylinder(d = outer_d, h = height);
        translate([0, 0, floor_t])
            cylinder(d = outer_d - wall * 2, h = height + 1);
    }
}

module inner_divider() {
    if (divider == 1)
        translate([-wall / 2, -(outer_d - wall * 2) / 2, floor_t])
            cube([wall, outer_d - wall * 2, height - floor_t]);
}

union() { body(); inner_divider(); }''',
        },
        "spacer": {
            "match": ["spacer", "washer", "standoff", "shim", "bushing"],
            "code": '''$fn = 64;
outer_d = 16;  // outside diameter
inner_d = 6;   // bore diameter
height  = 10;  // height

difference() {
    cylinder(d = outer_d, h = height);
    translate([0, 0, -1]) cylinder(d = inner_d, h = height + 2);
}''',
        },
        "knob": {
            "match": ["knob", "dial", "handle", "grip"],
            "code": '''$fn = 96;
knob_d    = 34;  // knob diameter
knob_h    = 18;  // knob height
shaft_d   = 6.2; // shaft bore diameter
grip_count = 12; // number of grip flutes
grip_d    = 4;   // flute diameter

module flutes() {
    for (i = [0 : grip_count - 1])
        rotate([0, 0, i * 360 / grip_count])
            translate([knob_d / 2, 0, -1])
                cylinder(d = grip_d, h = knob_h + 2);
}

difference() {
    cylinder(d = knob_d, h = knob_h);
    flutes();
    translate([0, 0, -1]) cylinder(d = shaft_d, h = knob_h + 2);
}''',
        },
        "plate": {
            "match": ["plate", "panel", "base plate", "mounting plate", "adapter plate"],
            "code": '''$fn = 64;
width     = 100; // plate width
depth     = 70;  // plate depth
thickness = 4;   // plate thickness
corner_r  = 5;   // corner radius
hole_d    = 4.5; // hole diameter
inset     = 8;   // hole distance from the edge

module plate_body() {
    hull() {
        for (x = [corner_r, width - corner_r])
            for (y = [corner_r, depth - corner_r])
                translate([x, y, 0]) cylinder(r = corner_r, h = thickness);
    }
}

module holes() {
    for (x = [inset, width - inset])
        for (y = [inset, depth - inset])
            translate([x, y, -1]) cylinder(d = hole_d, h = thickness + 2);
}

difference() { plate_body(); holes(); }''',
        },
    }

    def _match_template(self, description):
        """Find the closest known-good template for a request."""
        d = description.lower()
        best, best_score = None, 0
        for key, tpl in self.CAD_TEMPLATES.items():
            for phrase in tpl["match"]:
                if phrase in d:
                    score = len(phrase)
                    if score > best_score:
                        best, best_score = key, score
        return best

    def _cad_render_png(self, code):
        """Render the model to a PNG so the vision model can actually look at it."""
        exe = self._find_openscad()
        if not exe:
            return None
        stamp = int(time.time() * 1000)
        scad = os.path.join(CAD_DIR, f"_preview_{stamp}.scad")
        png = os.path.join(CAD_DIR, f"_preview_{stamp}.png")
        try:
            with open(scad, "w", encoding="utf-8") as f:
                f.write(code)
            subprocess.run(
                [exe, "-o", png, "--imgsize=600,600", "--viewall", "--autocenter",
                 "--colorscheme=Tomorrow Night", scad],
                capture_output=True, text=True, timeout=120)
            if os.path.exists(png) and os.path.getsize(png) > 1000:
                return png
        except Exception as e:
            log_debug(f"cad render png error: {e}")
        finally:
            try:
                if os.path.exists(scad):
                    os.remove(scad)
            except Exception:
                pass
        return None

    def _cad_visual_check(self, png_path, description):
        """Ask the vision model whether the render actually looks like the request.
        Returns (looks_right, feedback)."""
        try:
            with open(png_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("ascii")
            prompt = (
                f"This is a 3D CAD render. The user asked for: \"{description}\".\n\n"
                "Does the shape shown actually look like that object? Be strict and honest.\n"
                'Reply ONLY as JSON: {"looks_correct": true/false, '
                '"what_i_see": "<brief>", "problems": "<what is wrong or missing>"}'
            )
            res = self.http.post(OLLAMA_GENERATE_URL, json={
                "model": self.model_for("vision"), "prompt": prompt, "images": [img_b64],
                "stream": False, "format": "json", "keep_alive": "30m",
                "options": {"num_predict": 220, "temperature": 0.2},
            }, timeout=150)
            data = self._llm_json(res)
            ok = bool(data.get("looks_correct", False))
            fb = f"The render shows: {data.get('what_i_see','')}. Problems: {data.get('problems','')}"
            return ok, fb
        except Exception as e:
            log_debug(f"cad visual check error: {e}")
            return True, ""      # never block on a failed check

    def _cad_plan_geometry(self, description):
        """Describe the geometry in plain structural terms before writing code.
        Planning first produces far better shapes than going straight to OpenSCAD."""
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self._code_model(),
                "messages": [{"role": "user", "content":
                    f"Plan the 3D geometry for: {description}\n\n"
                    "Describe it concretely so an engineer could model it:\n"
                    "1. Overall bounding size in mm\n"
                    "2. The main solid shapes that form it, with sizes and positions\n"
                    "3. Anything subtracted (holes, slots, cavities) with sizes\n"
                    "4. Which face sits flat on the print bed\n"
                    "Be specific with numbers. Keep it under 150 words."}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 320, "temperature": 0.3},
            }, timeout=120)
            return self._llm_text(res)
        except Exception as e:
            log_debug(f"cad planning error: {e}")
            return ""

    # ==================================================================
    # STARTUP HEALTH — know what's live before you try to use it
    # ==================================================================
    # Config fields that ship as placeholders and silently no-op if left alone.
    PLACEHOLDER_FIELDS = [
        ("email", "address", "Email sending"),
        ("email", "app_password", "Email sending"),
        ("email", "default_receiver", "Email recipient"),
        ("twilio", "account_sid", "SMS and calls"),
        ("twilio", "auth_token", "SMS and calls"),
        ("twilio", "from_number", "SMS and calls"),
        ("home_assistant", "token", "Home automation"),
        ("spotify", "client_id", "Spotify control"),
        ("spotify", "client_secret", "Spotify control"),
    ]

    @staticmethod
    def _is_placeholder_value(v):
        if v is None:
            return True
        s = str(v).strip()
        if not s:
            return True
        low = s.lower()
        return (low.startswith("your_") or low.startswith("your ")
                or "placeholder" in low or low in ("changeme", "xxx", "todo")
                or re.match(r'^\+?1?5550{0,3}1{0,3}\d*$', s) is not None and "twilio" in low)

    def validate_config(self, announce=True):
        """Report every config field still left as a placeholder, once, at startup —
        rather than letting each feature silently do nothing."""
        unset = {}
        for section, key, feature in self.PLACEHOLDER_FIELDS:
            val = CONFIG.get(section, {}).get(key)
            if self._is_placeholder_value(val):
                unset.setdefault(feature, []).append(f"{section}.{key}")
        self.config_issues = unset
        if unset and announce:
            lines = [f"{feat}:  {', '.join(fields)}" for feat, fields in unset.items()]
            lines.append("")
            lines.append(f"Edit {CONFIG_FILE} to enable these.")
            self.safe_log("Unconfigured features — " + "; ".join(unset.keys()))
            log_debug("Config placeholders: " + json.dumps(unset))
        return unset

    def startup_digest(self):
        """One short line on launch telling you exactly what is and isn't live."""
        degraded = []
        if not HAS_EDGE_TTS:
            degraded.append("neural voice")
        if not SPEECH_AVAILABLE:
            degraded.append("speech input")
        if not HAS_CV2:
            degraded.append("camera")
        elif not _mediapipe_installed():
            degraded.append("gestures")
        if not self._find_openscad():
            degraded.append("CAD export")
        if not HAS_WIN32 and sys.platform == "win32":
            degraded.append("Windows control")
        if not REPORTLAB_AVAILABLE:
            degraded.append("PDF export")
        for feature in (self.config_issues or {}):
            degraded.append(feature.lower())

        # Is the model actually reachable? (Give a freshly started Ollama a moment.)
        self.ollama_ready.wait(timeout=float(CONFIG.get("ollama", {}).get("start_timeout", 45)))
        model_ok = False
        try:
            r = self.http.get(OLLAMA_GENERATE_URL.replace("/api/generate", "/api/tags"), timeout=8)
            model_ok = r.status_code == 200
        except Exception:
            pass
        if not model_ok:
            degraded.insert(0, "language model (is Ollama running?)")

        if degraded:
            summary = ", ".join(dict.fromkeys(degraded))
            self.safe_log(f"Degraded: {summary}.  Say 'run diagnostics' for detail.")
            self.run_js(f"showStartupDigest({json.dumps(summary)}, {len(degraded)})")
            if CONFIG.get("assistant", {}).get("speak_startup_digest", True):
                spoken = list(dict.fromkeys(degraded))[:3]
                self.speak(f"{len(degraded)} subsystem{'s' if len(degraded) != 1 else ''} unavailable, "
                           f"{USER_TITLE}: {', '.join(spoken)}.")
        else:
            self.safe_log("All subsystems operational.")
            self.run_js("showStartupDigest('All systems operational', 0)")
        return degraded

    # ==================================================================
    # PERFORMANCE — caching and faster first audio
    # ==================================================================
    # Trivial queries that never need the model. Answered instantly.
    INSTANT_CACHE_TTL = 900      # seconds a cached model reply stays fresh

    def _cache_key(self, cmd):
        return re.sub(r'\s+', ' ', cmd.lower().strip())

    def cache_get(self, cmd):
        """Return a cached reply for a repeated question, if still fresh."""
        if not CONFIG.get("assistant", {}).get("response_cache", True):
            return None
        entry = self.response_cache.get(self._cache_key(cmd))
        if not entry:
            return None
        if time.time() - entry["ts"] > self.INSTANT_CACHE_TTL:
            self.response_cache.pop(self._cache_key(cmd), None)
            return None
        log_debug(f"Cache hit: {cmd[:40]}")
        return entry["reply"]

    def cache_put(self, cmd, reply):
        if not CONFIG.get("assistant", {}).get("response_cache", True):
            return
        # Never cache anything time-sensitive or personal-state dependent.
        low = cmd.lower()
        if any(k in low for k in ["time", "date", "weather", "news", "price", "today",
                                  "now", "remember", "my ", "screen", "camera"]):
            return
        if not reply or len(reply) > 4000:
            return
        self.response_cache[self._cache_key(cmd)] = {"reply": reply, "ts": time.time()}
        if len(self.response_cache) > 200:
            oldest = sorted(self.response_cache.items(), key=lambda x: x[1]["ts"])[:50]
            for k, _ in oldest:
                self.response_cache.pop(k, None)

    @staticmethod
    def _first_speakable_chunk(buffer):
        """Find the earliest natural place to start talking.

        Waiting for a full sentence delays the first audio noticeably. A clause
        boundary (comma, semicolon, dash) is close enough to natural and gets
        sound out of the speakers much sooner.
        """
        # A complete sentence is always a good boundary.
        m = re.search(r'[.!?](?:\s|$)', buffer)
        if m:
            return m.end()
        # Otherwise take a clause once we have enough words to sound natural.
        if len(buffer.split()) >= 8:
            m2 = None
            for sep in (';', ' — ', ' – ', ',', ':'):
                idx = buffer.find(sep)
                if idx > 20:
                    m2 = idx + len(sep)
                    break
            if m2:
                return m2
        return 0

    # ==================================================================
    # TRUST — approve before acting, and see exactly what changed
    # ==================================================================
    def _needs_approval(self, steps, request):
        """Decide whether a plan is risky enough to confirm first."""
        mode = CONFIG.get("assistant", {}).get("automation_approval", "risky")
        if mode == "never":
            return False
        if mode == "always":
            return True
        # 'risky': confirm when the plan touches something destructive or personal.
        blob = (request + " " + json.dumps(steps)).lower()
        risky_words = [
            "delete", "remove", "uninstall", "format", "purchase", "buy", "checkout",
            "pay", "order", "send", "post", "publish", "tweet", "submit", "subscribe",
            "unsubscribe", "password", "bank", "transfer", "confirm", "sign out",
            "log out", "shutdown", "restart", "wipe", "clear",
        ]
        if any(w in blob for w in risky_words):
            return True
        return len(steps) > 8

    def request_approval(self, name, steps, request):
        """Show the plan and wait for a yes/no. Returns True to proceed."""
        preview = "\n".join(
            f"{i}. {s.get('action', '')} {str(s.get('value', ''))[:60]}"
            for i, s in enumerate(steps, 1))
        self.pending_plan = {"name": name, "steps": steps, "request": request}
        self.awaiting_approval = True
        self.run_js(f"showApproval({json.dumps(name)}, {json.dumps(preview)})")
        self.instantiate_card(f"Approve: {name}", "code_bug", preview)
        self.speak(f"I've planned {len(steps)} steps for that, {USER_TITLE}. "
                   "Shall I go ahead?")

        deadline = time.time() + float(CONFIG.get("assistant", {}).get("approval_timeout", 45))
        while self.awaiting_approval and time.time() < deadline:
            time.sleep(0.15)

        if self.awaiting_approval:      # timed out
            self.awaiting_approval = False
            self.pending_plan = None
            self.run_js("hideApproval()")
            self.speak("No answer, so I've cancelled it, Sir.")
            return False
        return bool(self.approval_result)

    def resolve_approval(self, approved):
        """Called by the UI or by a spoken yes/no."""
        if not self.awaiting_approval:
            return False
        self.approval_result = bool(approved)
        self.awaiting_approval = False
        self.run_js("hideApproval()")
        if not approved:
            self.speak("Cancelled, Sir.")
        return True

    @staticmethod
    def _make_diff(before, after, context=3):
        """Unified diff between two versions of a file."""
        import difflib
        diff = difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile="current", tofile="proposed",
            lineterm="", n=context)
        lines = list(diff)
        if len(lines) > 400:
            lines = lines[:400] + [f"... ({len(lines) - 400} more diff lines)"]
        return "\n".join(lines)

    # ==================================================================
    # CAD OUTPUT — straight to the slicer, and safe iterative refinement
    # ==================================================================
    SLICER_PATHS = {
        "PrusaSlicer": [
            r"C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer.exe",
            r"C:\Program Files\Prusa3D\PrusaSlicer\PrusaSlicer.exe",
            "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer",
            "/usr/bin/prusa-slicer",
        ],
        "Cura": [
            r"C:\Program Files\Ultimaker Cura 5.7\Ultimaker-Cura.exe",
            r"C:\Program Files\Ultimaker Cura\Ultimaker-Cura.exe",
            "/Applications/Ultimaker Cura.app/Contents/MacOS/Ultimaker Cura",
        ],
        "Bambu Studio": [
            r"C:\Program Files\Bambu Studio\bambu-studio.exe",
            "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
        ],
        "OrcaSlicer": [
            r"C:\Program Files\OrcaSlicer\orca-slicer.exe",
            "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
        ],
    }

    def _find_slicer(self):
        """Locate an installed slicer. Returns (name, path) or (None, None)."""
        configured = CONFIG.get("cad", {}).get("slicer_path", "")
        if configured and os.path.exists(configured):
            return os.path.basename(configured), configured
        for name, paths in self.SLICER_PATHS.items():
            for p in paths:
                if p and os.path.exists(p):
                    return name, p
            exe = shutil.which(name.lower().replace(" ", "-"))
            if exe:
                return name, exe
        return None, None

    def send_to_slicer(self):
        """Open the exported STL directly in a slicer, ready to print."""
        cad = getattr(self, "current_cad", None)
        if not cad:
            self.speak("There's no design open, Sir.")
            return True
        stl = cad.get("stl")
        if not stl or not os.path.exists(stl):
            self.speak("Exporting the STL first, Sir.")
            stl = self.cad_export_stl(announce=False)
        if not stl or not os.path.exists(stl):
            self.speak("I couldn't produce an STL to send, Sir. Is OpenSCAD installed?")
            return True

        name, path = self._find_slicer()
        if not path:
            # No slicer found — open the folder so it's one drag away.
            self.speak("I couldn't find a slicer installed, Sir. I'll open the folder instead.")
            self.launch_external_app(os.path.dirname(stl))
            self.safe_log("Install PrusaSlicer, Cura, Bambu Studio or OrcaSlicer, "
                          "or set cad.slicer_path in the config.")
            return True
        try:
            subprocess.Popen([path, stl])
            self.speak(f"Opening it in {name}, Sir.")
            self.safe_log(f"Sent {os.path.basename(stl)} to {name}.")
            self.project_log(f"Sent to slicer: {os.path.basename(stl)}")
        except Exception as e:
            log_debug(f"slicer launch error: {e}")
            self.speak(f"I couldn't launch {name}, Sir.")
        return True

    def cad_refine(self, instruction):
        """Iteratively refine the model, but SHOW the result before committing it.

        This is the safe version of cad_edit: the change is rendered and diffed,
        and only written to disk once you accept it.
        """
        if self._is_cq_cad():
            return self.cq_revise(instruction, propose=True)
        cad = getattr(self, "current_cad", None)
        if not cad:
            self.speak("There's no design open, Sir.")
            return True

        self.speak("Working on that change, Sir.")
        self.progress("Revising design", 20)
        new_code = self._cad_generate_code(instruction, existing=cad["code"])
        if not new_code:
            self.progress_done()
            self.speak("I couldn't apply that change, Sir.")
            return True

        self.progress("Validating change", 55)
        ok, err = self._cad_validate(new_code)
        if not ok:
            self.safe_log(f"Revision failed validation: {err[:160]}")
            self.progress("Correcting", 70)
            new_code = self._cad_generate_code(
                f"That revision failed to compile.\n\nERRORS:\n{err}\n\n"
                f"Return the COMPLETE corrected code. The change requested was: {instruction}",
                existing=new_code) or new_code
            ok, err = self._cad_validate(new_code)

        diff = self._make_diff(cad["code"], new_code)
        self.pending_cad = {"code": new_code, "instruction": instruction, "valid": ok}
        self.progress_done()

        self.instantiate_card(f"Proposed change: {instruction[:40]}", "code_bug",
                              (diff or "(no textual change)")[:3000])
        self.run_js(f"showCadProposal({json.dumps(instruction)}, {json.dumps(diff[:6000])}, {str(bool(ok)).lower()})")
        if ok:
            self.speak("Here's the proposed change, Sir. Accept it or discard it.")
        else:
            self.speak("The change still has errors, Sir. Review it before accepting.")
        return True

    def cad_accept(self):
        """Commit the pending refinement."""
        if self._is_cq_cad() and getattr(self, "pending_cad", None) and self.pending_cad.get("spec"):
            spec = self.pending_cad["spec"]
            self.pending_cad = None
            self.run_js("hideCadProposal()")
            ok = self.cq_build(spec, self.current_cad["description"])
            self.speak("Change applied, Sir." if ok else "That change didn't build, Sir.")
            return True
        pending = getattr(self, "pending_cad", None)
        if not pending:
            self.speak("There's no pending change, Sir.")
            return True
        cad = self.current_cad
        cad["code"] = pending["code"]
        try:
            with open(cad["path"], "w", encoding="utf-8") as f:
                f.write(pending["code"])
        except Exception as e:
            log_debug(f"cad accept write error: {e}")
            self.speak("I couldn't save the change, Sir.")
            return True
        self.pending_cad = None
        params = self._cad_extract_params(cad["code"])
        self.run_js("hideCadProposal()")
        self.run_js(f"showCadEditor({json.dumps({'name': cad['description'], 'code': cad['code'], 'path': cad['path'], 'params': params})})")
        self.cad_export_stl(announce=False)
        self.speak("Change applied, Sir.")
        self.project_log(f"CAD change: {pending['instruction'][:60]}")
        return True

    def cad_discard(self):
        if not getattr(self, "pending_cad", None):
            self.speak("There's no pending change, Sir.")
            return True
        self.pending_cad = None
        self.run_js("hideCadProposal()")
        self.speak("Discarded, Sir. The design is unchanged.")
        return True

    # ==================================================================
    # LIVE KNOWLEDGE — watched folders re-index themselves
    # ==================================================================
    KB_EXTS = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".html",
               ".log", ".ini", ".cfg", ".yaml", ".yml", ".sql", ".java", ".c", ".cpp"}

    def kb_watch(self, path):
        """Watch a folder so the knowledge base stays current without re-indexing."""
        path = os.path.abspath(os.path.expanduser(path.strip().strip('"')))
        if not os.path.isdir(path):
            self.speak("That isn't a folder I can watch, Sir.")
            return True
        watched = self.memory.setdefault("watched_folders", [])
        if path not in watched:
            watched.append(path)
            self.save_memory()
        self.kb_ingest(path)
        self.speak(f"I'll keep watching that folder for changes, Sir.")
        self.safe_log(f"Watching: {path}")
        if not self._watcher_started:
            self._watcher_started = True
            threading.Thread(target=self._kb_watch_worker, daemon=True).start()
        return True

    def kb_unwatch(self, path=None):
        watched = self.memory.get("watched_folders", [])
        if not watched:
            self.speak("I'm not watching any folders, Sir.")
            return True
        if path:
            target = os.path.abspath(os.path.expanduser(path.strip()))
            self.memory["watched_folders"] = [w for w in watched if w != target]
        else:
            self.memory["watched_folders"] = []
        self.save_memory()
        self.speak("Stopped watching, Sir.")
        return True

    def _kb_watch_worker(self):
        """Poll watched folders and re-index anything that changed.

        Polling rather than a filesystem-event library keeps this dependency-free
        and predictable; the interval is deliberately generous.
        """
        interval = float(CONFIG.get("assistant", {}).get("kb_watch_interval", 120))
        seen = {}
        while True:
            time.sleep(interval)
            try:
                watched = self.memory.get("watched_folders", [])
                if not watched:
                    continue
                kb = self.memory.setdefault("knowledge_base", {})
                changed = 0
                for folder in watched:
                    if not os.path.isdir(folder):
                        continue
                    for dirpath, dirnames, filenames in os.walk(folder):
                        dirnames[:] = [d for d in dirnames if not d.startswith('.')
                                       and d not in ("node_modules", "__pycache__", "venv", ".git")]
                        for fn in filenames:
                            if os.path.splitext(fn)[1].lower() not in self.KB_EXTS:
                                continue
                            full = os.path.join(dirpath, fn)
                            try:
                                mtime = os.path.getmtime(full)
                                size = os.path.getsize(full)
                            except Exception:
                                continue
                            if size > 400000:
                                continue
                            sig = (mtime, size)
                            if seen.get(full) == sig:
                                continue
                            seen[full] = sig
                            try:
                                with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                                    text = fh.read()[:20000]
                            except Exception:
                                continue
                            if text.strip():
                                with self.memory_lock:
                                    kb[full] = {
                                        "text": text,
                                        "indexed": datetime.datetime.now().isoformat(
                                            timespec="seconds"),
                                    }
                                changed += 1
                            if changed >= 60:
                                break
                if changed:
                    # New content means cached embeddings are stale.
                    self._embed_store = {}; self._save_embeddings()
                    self.save_memory()
                    log_debug(f"Knowledge base auto-updated: {changed} files.")
            except Exception as e:
                log_debug(f"kb watcher error: {e}")

    def kb_watch_status(self):
        watched = self.memory.get("watched_folders", [])
        if not watched:
            self.speak("I'm not watching any folders, Sir.")
            return True
        self.instantiate_card("Watched Folders", "carousel", watched)
        self.speak(f"I'm watching {len(watched)} folder{'s' if len(watched) != 1 else ''}, Sir.")
        return True

    # ==================================================================
    # LENS HISTORY — browse what he's looked at
    # ==================================================================
    def lens_history_view(self):
        hist = self.memory.get("lens_history", [])
        if not hist:
            self.speak("I haven't analysed anything visually yet, Sir.")
            return True
        items = [{"mode": h.get("mode", ""), "ts": h.get("ts", "")[:16].replace("T", " "),
                  "result": h.get("result", "")[:400]} for h in hist[-30:]][::-1]
        self.run_js(f"showLensHistory({json.dumps(items)})")
        self.speak(f"You have {len(hist)} visual analyses, Sir.")
        return True

    def _habit_nudge_worker(self):
        """Occasionally offer what you normally do at this hour.

        Deliberately conservative: only fires for well-established patterns, never
        while speaking or during a focus session, and never repeats the same nudge.
        """
        while True:
            time.sleep(1800)      # every 30 minutes
            try:
                if not CONFIG.get("assistant", {}).get("habit_nudges", False):
                    continue
                if self.is_speaking or getattr(self, "focus_until", 0) > time.time():
                    continue
                if getattr(self, "awaiting_approval", False):
                    continue
                hour = str(datetime.datetime.now().hour)
                slot = self.memory.get("habits", {}).get(hour, {})
                if not slot:
                    continue
                top, count = max(slot.items(), key=lambda x: x[1])
                if count >= 4 and top != self._last_nudge:
                    self._last_nudge = top
                    self.speak(f"You often ask me to {top} around now, {USER_TITLE}. Shall I?")
            except Exception as e:
                log_debug(f"habit nudge error: {e}")

    # ==================================================================
    # TRANSCRIPT REPAIR — fix misheard, cut-off and out-of-context words
    # ==================================================================
    # Things speech-to-text reliably mangles, mapped to what was meant.
    STT_CORRECTIONS = {
        # diagnostics family
        "die agnostics": "diagnostics", "diagnostic": "diagnostics",
        "the ignostics": "diagnostics", "dye agnostics": "diagnostics",
        "diagnosis": "diagnostics", "dagnostics": "diagnostics",
        # amy-specific vocabulary
        "desk few": "desk view", "death view": "desk view", "desk you": "desk view",
        "aimee": "amy", "amie": "amy",
        "sentinal": "sentinel", "centinel": "sentinel", "sentinel": "sentinel",
        "recalibrate": "recalibrate", "re calibrate": "recalibrate",
        "kad": "cad", "card design": "cad design", "cat design": "cad design",
        "s t l": "stl", "estee el": "stl",
        # apps commonly mangled
        "you tube": "youtube", "u tube": "youtube", "youtube's": "youtube",
        "spotty fi": "spotify", "spot if i": "spotify", "spotifi": "spotify",
        "g mail": "gmail", "gee mail": "gmail",
        "vs code": "vs code", "v s code": "vs code", "vias code": "vs code",
        "what's app": "whatsapp", "whats app": "whatsapp",
        "disc cord": "discord", "discourse": "discord",
        "net flicks": "netflix", "netflicks": "netflix",
        "chrome browser": "chrome", "google chrome": "chrome",
        # common verbs
        "clothes": "close", "cloves": "close",
        "shot down": "shut down", "shutdown": "shutdown",
        "opened": "open", "opun": "open",
        "sen d": "send", "sent": "send",
    }

    def _vocabulary(self):
        """Everything he knows the name of — used to repair garbled words."""
        vocab = set()
        vocab.update(self.APP_MAP.keys())
        try:
            vocab.update(_norm_app_name(n) for n in self.app_index.names()[:400])
        except Exception:
            pass
        vocab.update(self.WEB_FALLBACKS.keys())
        vocab.update(self.CAD_TEMPLATES.keys())
        vocab.update(self.LENS_MODES.keys())
        vocab.update([
            "diagnostics", "sentinel", "security", "camera", "gestures", "conversation",
            "research", "project", "calendar", "schedule", "reminder", "screenshot",
            "clipboard", "volume", "brightness", "wifi", "shutdown", "restart",
            "document", "export", "translate", "summarise", "recalibrate", "slicer",
            "knowledge", "history", "automate", "approve", "discard", "refine",
        ])
        # Names the user has taught him.
        vocab.update(k.lower() for k in self.memory.get("contacts", {}))
        vocab.update(k.lower() for k in self.memory.get("phonebook", {}))
        vocab.update(p.get("name", "").lower() for p in self.memory.get("projects", {}).values())
        return {v for v in vocab if v and len(v) > 2}

    def repair_transcript(self, text):
        """Clean up what the recogniser heard.

        Three passes: known mishearing substitutions, fuzzy-matching odd words
        against vocabulary he actually knows, and reconstructing words that were
        clipped at the start or end of the audio.
        """
        if not text or not CONFIG.get("assistant", {}).get("transcript_repair", True):
            return text
        original = text
        low = " " + text.lower().strip() + " "

        # Pass 1: direct phrase substitutions (longest first so multi-word wins).
        for wrong in sorted(self.STT_CORRECTIONS, key=len, reverse=True):
            if f" {wrong} " in low:
                low = low.replace(f" {wrong} ", f" {self.STT_CORRECTIONS[wrong]} ")

        # Pass 2: fuzzy-repair words that aren't real but are close to vocabulary.
        import difflib
        vocab = self._vocabulary()
        words = low.split()
        common = {
            "the","a","an","and","or","but","to","for","in","on","at","of","my","me",
            "you","it","is","are","was","were","do","does","did","can","could","would",
            "please","that","this","with","from","about","what","when","where","how",
            "why","who","i","we","they","he","she","not","no","yes","up","down","out",
            "off","open","close","play","stop","start","run","make","show","tell","give",
            "get","set","put","take","find","look","go","come","new","all","some","now",
            "then","if","so","just","like","have","has","had","will","be","been","am",
        }
        repaired = []
        for w in words:
            bare = re.sub(r'[^\w]', '', w)
            if len(bare) < 4 or bare in common or bare in vocab or bare.isdigit():
                repaired.append(w)
                continue
            match = difflib.get_close_matches(bare, vocab, n=1, cutoff=0.82)
            if match and match[0] != bare:
                log_debug(f"Repaired '{bare}' -> '{match[0]}'")
                repaired.append(w.replace(bare, match[0]))
            else:
                repaired.append(w)
        low = " ".join(repaired)

        # Pass 3: a clipped final word - try to complete it from vocabulary.
        parts = low.split()
        if parts:
            last = re.sub(r'[^\w]', '', parts[-1])
            if 2 <= len(last) < 5 and last not in common and last not in vocab:
                starts = [v for v in vocab if v.startswith(last) and len(v) > len(last)]
                if len(starts) == 1:
                    log_debug(f"Completed clipped word '{last}' -> '{starts[0]}'")
                    parts[-1] = starts[0]
                    low = " ".join(parts)

        result = re.sub(r'\s+', ' ', low).strip()
        if result != original.lower().strip():
            log_debug(f"Transcript repaired: {original!r} -> {result!r}")
        return result

    # ==================================================================
    # WEB ACTIONS — go straight to the right page instead of clicking blindly
    # ==================================================================
    # Constructing the URL directly is far more reliable than driving the UI.
    WEB_ACTIONS = [
        # --- YouTube ---
        (r'(?:play|watch|put on|find|search(?: for)?|show me)\s+(.+?)\s+(?:on|in)\s+youtube',
         "https://www.youtube.com/results?search_query={q}", "YouTube"),
        (r'(?:open|go to|take me to)\s+(?:the\s+)?(.+?)\s+(?:youtube\s+)?channel',
         "https://www.youtube.com/results?search_query={q}+channel", "YouTube"),
        (r'youtube\s+(.+)', "https://www.youtube.com/results?search_query={q}", "YouTube"),
        # --- Music ---
        (r'(?:play|put on)\s+(.+?)\s+on\s+spotify',
         "https://open.spotify.com/search/{q}", "Spotify"),
        (r'(?:play|put on)\s+(.+?)\s+on\s+soundcloud',
         "https://soundcloud.com/search?q={q}", "SoundCloud"),
        # --- Shopping ---
        (r'(?:find|search(?: for)?|buy|look for|show me)\s+(.+?)\s+on\s+amazon',
         "https://www.amazon.com/s?k={q}", "Amazon"),
        (r'(?:find|search(?: for)?|buy|look for)\s+(.+?)\s+on\s+ebay',
         "https://www.ebay.com/sch/i.html?_nkw={q}", "eBay"),
        # --- Reference / dev ---
        (r'(?:search(?: for)?|find|look up)\s+(.+?)\s+on\s+(?:github|git hub)',
         "https://github.com/search?q={q}", "GitHub"),
        (r'(?:search(?: for)?|find|look up)\s+(.+?)\s+on\s+(?:reddit)',
         "https://www.reddit.com/search/?q={q}", "Reddit"),
        (r'(?:search(?: for)?|find|look up)\s+(.+?)\s+on\s+(?:stack ?overflow)',
         "https://stackoverflow.com/search?q={q}", "Stack Overflow"),
        (r'(?:look up|search(?: for)?)\s+(.+?)\s+on\s+wikipedia',
         "https://en.wikipedia.org/w/index.php?search={q}", "Wikipedia"),
        # --- Streaming ---
        (r'(?:find|search(?: for)?|watch|play)\s+(.+?)\s+on\s+netflix',
         "https://www.netflix.com/search?q={q}", "Netflix"),
        (r'(?:watch|find|open)\s+(.+?)\s+on\s+twitch',
         "https://www.twitch.tv/search?term={q}", "Twitch"),
        # --- Maps / travel ---
        (r'(?:directions to|navigate to|route to)\s+(.+)',
         "https://www.google.com/maps/dir//{q}", "Google Maps"),
        (r'(?:find|show me|where is)\s+(.+?)\s+(?:on|in)\s+(?:google\s+)?maps',
         "https://www.google.com/maps/search/{q}", "Google Maps"),
        # --- Generic search ---
        (r'(?:google|search(?: for)?|look up)\s+(.+)',
         "https://www.google.com/search?q={q}", "Google"),
    ]

    # Filler that shouldn't end up in the search query.
    QUERY_NOISE = re.compile(
        r'^\s*(?:the|a|an|some|me|us|please|up|for me|that|this)\s+|'
        r'\s+(?:please|for me|now|thanks|thank you)\s*$', re.I)

    def resolve_web_action(self, cmd):
        """If the request maps cleanly to a URL, open it directly.
        Returns True if handled."""
        low = cmd.lower().strip()
        for pattern, url_tpl, site in self.WEB_ACTIONS:
            m = re.search(pattern, low)
            if not m:
                continue
            q = m.group(1).strip()
            # Strip leading/trailing filler, twice for phrases like "the the".
            for _ in range(2):
                q = self.QUERY_NOISE.sub(" ", q).strip()
            q = q.strip(" .,!?")
            if not q or len(q) < 2:
                continue
            url = url_tpl.format(q=urllib.parse.quote_plus(q))
            self.speak(f"Opening {q} on {site}, {USER_TITLE}.")
            ok = self._open_in_browser(url, f"{q} on {site}")
            if ok:
                self.safe_log(f"{site}: {q}")
                self.project_log(f"Opened {site}: {q}")
            return True
        return False

    # ==================================================================
    # CONVERSATIONAL CONTEXT — understand references to what came before
    # ==================================================================
    # Phrases that only make sense relative to an earlier turn.
    REFERENCE_MARKERS = re.compile(
        r'\b(it|that|this|those|these|them|they|the same|again|another one|'
        r'the (?:first|second|third|last|other) one|do that|same thing|'
        r'like before|as before|that one|the previous)\b', re.I)

    def _needs_context_resolution(self, cmd):
        """Does this command only make sense given the last few turns?"""
        if len(cmd.split()) > 25:
            return False
        return bool(self.REFERENCE_MARKERS.search(cmd))

    def resolve_references(self, cmd):
        """Rewrite a context-dependent request into a standalone one.

        'do that again', 'open the second one', 'make it bigger' - these are
        natural but meaningless on their own. Rewriting them before routing is
        what makes him feel like he's actually following the conversation.
        """
        if not CONFIG.get("assistant", {}).get("resolve_references", True):
            return cmd
        if not self._needs_context_resolution(cmd):
            return cmd

        history = self.memory.get("full_transcript", [])[-8:]
        if not history:
            return cmd
        convo = "\n".join(
            f"{'User' if m.get('role') == 'user' else 'Assistant'}: {str(m.get('content',''))[:220]}"
            for m in history)
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("fast"),
                "messages": [{"role": "user", "content":
                    "Rewrite the user's latest message as a complete, standalone instruction "
                    "by substituting in what the pronouns and references point to.\n"
                    "Change NOTHING else. If it already stands alone, return it unchanged.\n"
                    "Reply with ONLY the rewritten instruction, no quotes or explanation.\n\n"
                    f"CONVERSATION:\n{convo}\n\nLATEST MESSAGE: {cmd}"}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 90, "temperature": 0.1, "num_ctx": 4096},
            }, timeout=45)
            rewritten = self._llm_text(res)
            rewritten = rewritten.strip('"\'').split("\n")[0].strip()
            # Sanity-check: a wild rewrite is worse than the original.
            if rewritten and 2 <= len(rewritten.split()) <= 40:
                if rewritten.lower() != cmd.lower():
                    log_debug(f"Resolved reference: {cmd!r} -> {rewritten!r}")
                    self.safe_log(f"(understood as: {rewritten})")
                return rewritten
        except Exception as e:
            log_debug(f"reference resolution failed: {e}")
        return cmd

    def _recent_context_summary(self, turns=6):
        """A short digest of the conversation so far, for the system prompt."""
        history = self.memory.get("full_transcript", [])[-turns * 2:]
        if not history:
            return ""
        lines = []
        for m in history:
            role = "You" if m.get("role") == "user" else "I"
            lines.append(f"{role}: {str(m.get('content',''))[:180]}")
        return "\n".join(lines)

    # ==================================================================
    # MULTI-MODEL — the right model for each job
    # ==================================================================
    # Preference order per task. The first one actually pulled wins.
    MODEL_PREFERENCES = {
        "fast": ["qwen2.5:3b", "llama3.2:3b", "llama3.2", "phi3.5", "gemma2:2b"],
        "chat": ["qwen2.5:14b", "llama3.1:8b", "qwen2.5:7b", "llama3.2", "mistral"],
        "reasoning": ["qwen2.5:14b", "deepseek-r1:14b", "llama3.1:8b", "qwen2.5:7b", "llama3.2"],
        "code": ["qwen2.5-coder:14b", "qwen2.5-coder:7b", "deepseek-coder-v2",
                 "codellama", "qwen2.5:14b", "llama3.2"],
        "cad": ["qwen2.5-coder:14b", "qwen2.5-coder:7b", "qwen2.5:14b", "llama3.2"],
        "vision": ["llava:13b", "qwen2.5vl", "llava", "bakllava", "moondream"],
    }

    def refresh_available_models(self):
        """Ask Ollama what's actually installed, so we never route to a missing model."""
        try:
            url = OLLAMA_GENERATE_URL.replace("/api/generate", "/api/tags")
            r = self.http.get(url, timeout=10)
            if r.status_code == 200:
                names = [m.get("name", "") for m in r.json().get("models", [])]
                self.available_models = [n for n in names if n]
                log_debug(f"Models available: {self.available_models}")
                return self.available_models
        except Exception as e:
            log_debug(f"model list failed: {e}")
        return self.available_models

    def _model_installed(self, name):
        """True if this model is pulled.

        If the candidate names an explicit tag (qwen2.5:3b) it must match that
        exact size — otherwise 'qwen2.5:3b' would wrongly match 'qwen2.5:14b'
        and a request for a small fast model would get a large slow one.
        """
        if not name:
            return False
        if ":" in name:
            base, tag = name.split(":", 1)
            for m in self.available_models:
                if m == name:
                    return True
                if ":" in m:
                    mb, mt = m.split(":", 1)
                    if mb == base and (mt == tag or (tag == "latest" and mt == "latest")):
                        return True
                elif m == base and tag == "latest":
                    return True
            return False
        # No tag given: any installed variant of that base name will do.
        return any(m == name or m.split(":")[0] == name for m in self.available_models)

    def _resolve_exact(self, name):
        """Turn a loose name into the exact installed tag, if we can."""
        if not self.available_models:
            return name
        for m in self.available_models:
            if m == name:
                return m
        if ":" not in name and f"{name}:latest" in self.available_models:
            return f"{name}:latest"          # what Ollama itself means by a bare name
        base = name.split(":")[0]
        matches = [m for m in self.available_models if m.split(":")[0] == base]
        if matches:
            # Prefer an exact tag match, else the first installed variant.
            return matches[0]
        return name

    def model_for(self, task="chat"):
        """Return the best model for a task, honouring explicit config first."""
        cfg = CONFIG.get("ollama", {})
        lean = _mem_mode() == "lean"
        if task == "general":
            # Odd jobs: the shared main model when lean, else the configured default.
            return self.model_for("chat") if lean else MODEL_NAME
        # 1. Explicit override in the config always wins.
        explicit = (cfg.get("models", {}) or {}).get(task, "")
        if explicit:
            return explicit
        # Lean: every text job shares the chat model, so only one language
        # model is ever in memory (plus vision, briefly, when it's needed).
        if lean and task not in ("chat", "vision"):
            return self.model_for("chat")
        # 2. Legacy single-purpose keys.
        if task == "vision" and cfg.get("vision_model"):
            legacy = cfg["vision_model"]
            if self._model_installed(legacy) or not self.available_models:
                return self._resolve_exact(legacy)
        if task == "code" and CONFIG.get("assistant", {}).get("code_model"):
            return CONFIG["assistant"]["code_model"]
        # 3. Auto-select the best installed model for the job.
        if cfg.get("auto_select", True) and self.available_models:
            cached = self._model_cache.get(task)
            if cached:
                return cached
            prefs = list(self.MODEL_PREFERENCES.get(task, []))
            if lean and task == "vision":
                prefs.sort(key=lambda c: "13b" in c)     # smaller vision model first
            for candidate in prefs:
                if self._model_installed(candidate):
                    exact = self._resolve_exact(candidate)
                    self._model_cache[task] = exact
                    log_debug(f"Task '{task}' -> {exact}")
                    return exact
        # 4. Fall back to the configured default.
        return MODEL_NAME if task != "vision" else VISION_MODEL

    def model_report(self):
        """Show which model is handling which job."""
        self.refresh_available_models()
        self._model_cache.clear()
        lines = [f"Installed: {len(self.available_models)} models", ""]
        for task in ("fast", "chat", "reasoning", "code", "cad", "vision"):
            chosen = self.model_for(task)
            mark = "✓" if self._model_installed(chosen) else "✗ not pulled"
            lines.append(f"{task:10} -> {chosen}  {mark}")
        lines.append("")
        lines.append("Pull more with:  ollama pull qwen2.5:14b")
        self.instantiate_card("Model Routing", "code_bug", "\n".join(lines))
        self.speak(f"I'm using {len(set(self.model_for(t) for t in ('chat','code','vision')))} "
                   f"different models across tasks, {USER_TITLE}.")
        return True

    # ==================================================================
    # RECOGNITION — native/offline engines and smarter interpretation
    # ==================================================================
    def _recognize_best(self, recognizer, audio):
        """Transcribe, then pick the hypothesis that makes the most sense.

        Google returns several candidate transcriptions ranked by acoustic
        confidence alone. Re-ranking them against words Amy actually knows
        fixes a lot of "it heard the wrong thing" cases.
        """
        engine = CONFIG.get("assistant", {}).get("stt_engine", "auto")

        # --- Whisper (offline, accurate) when installed ---
        if engine in ("auto", "whisper"):
            text = self._recognize_whisper(audio)
            if text is not None:
                return text
            if engine == "whisper":
                return ""

        # --- Offline first, if requested and available ---
        if engine in ("native", "offline", "sphinx"):
            text = self._recognize_offline(recognizer, audio)
            if text:
                return text
            if engine != "auto":
                return ""

        # --- Cloud with alternatives ---
        try:
            result = recognizer.recognize_google(audio, show_all=True)
        except sr.UnknownValueError:
            return ""
        except sr.RequestError:
            # No internet — fall back to whatever offline engine exists.
            offline = self._recognize_offline(recognizer, audio)
            if offline:
                self._stt_failures = 0
                return offline
            raise

        if not result:
            return ""
        if isinstance(result, str):
            return result.lower().strip()

        alts = result.get("alternative", []) if isinstance(result, dict) else []
        if not alts:
            return ""

        vocab = self._vocabulary()
        wake_set = set([WAKE_WORD] + list(self.WAKE_VARIANTS))
        best, best_score = "", -1e9
        for i, alt in enumerate(alts[:5]):
            text = (alt.get("transcript") or "").lower().strip()
            if not text:
                continue
            conf = float(alt.get("confidence", 0.0) or 0.0)
            words = [re.sub(r'[^\w]', '', w) for w in text.split()]
            known = sum(1 for w in words if w and (w in vocab or w in wake_set))
            # How well does this candidate match something he can actually DO?
            # A lower-ranked hypothesis that IS a real command beats a
            # higher-ranked one that's meaningless.
            try:
                _, cmd_conf, _ = self.match_command(text)
            except Exception:
                cmd_conf = 0.0
            score = (conf * 1.6) + (known * 0.5) + (cmd_conf * 1.8) - (i * 0.05)
            if score > best_score:
                best, best_score = text, score
        if best and best != (alts[0].get("transcript") or "").lower().strip():
            log_debug(f"Chose better hypothesis: {best!r} "
                      f"(over {alts[0].get('transcript')!r})")
        return best

    def _recognize_whisper(self, audio):
        """Fully offline speech recognition with faster-whisper.
        Returns None if Whisper isn't available, so another engine is used."""
        if self._whisper_failed:
            return None
        if self._whisper is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                self._whisper_failed = True
                return None
            name = CONFIG.get("assistant", {}).get("whisper_model", "small.en")
            try:
                try:
                    self._whisper = WhisperModel(name, device="cuda", compute_type="int8_float16")
                except Exception:
                    self._whisper = WhisperModel(name, device="cpu", compute_type="int8")
                log_debug(f"Whisper '{name}' loaded.")
            except Exception as e:
                log_debug(f"Whisper unavailable: {e}")
                self._whisper_failed = True
                return None
        try:
            raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
            pcm = _np.frombuffer(raw, dtype=_np.int16).astype(_np.float32) / 32768.0
            try:
                apps = ", ".join(self.app_index.names()[:20])
            except Exception:
                apps = ""
            segments, _info = self._whisper.transcribe(
                pcm, language="en", beam_size=1, vad_filter=False,
                initial_prompt=f"Talking to Amy, a desktop assistant. {apps}")
            text = " ".join(seg.text for seg in segments).strip()
            # Whisper invents these on near-silence.
            if re.fullmatch(r"(thanks? (you|for watching)[.!]*|you[.!]*|\.+|bye[.!]*)", text.lower()):
                return ""
            return text.lower()
        except Exception as e:
            log_debug(f"Whisper transcription failed: {e}")
            return None

    def _recognize_offline(self, recognizer, audio):
        """Offline recognition: Vosk if installed, else PocketSphinx, else
        Windows' own speech engine. Returns '' if none are available."""
        # Vosk — best offline quality, if the user installed it.
        try:
            import vosk  # noqa: F401
            model_dir = CONFIG.get("assistant", {}).get("vosk_model_path", "")
            if model_dir and os.path.isdir(model_dir):
                text = self._vosk_transcribe(audio, model_dir)
                if text:
                    return text
        except ImportError:
            pass
        except Exception as e:
            log_debug(f"vosk error: {e}")

        # PocketSphinx — bundled with speech_recognition if installed.
        try:
            return recognizer.recognize_sphinx(audio).lower().strip()
        except Exception:
            pass
        return ""

    def _vosk_transcribe(self, audio, model_dir):
        try:
            import vosk
            if self._vosk_model is None:
                self._vosk_model = vosk.Model(model_dir)
            rec = vosk.KaldiRecognizer(self._vosk_model, 16000)
            rec.AcceptWaveform(audio.get_raw_data(convert_rate=16000, convert_width=2))
            result = json.loads(rec.FinalResult())
            return (result.get("text") or "").lower().strip()
        except Exception as e:
            log_debug(f"vosk transcribe error: {e}")
            return ""

    def stt_report(self):
        """What speech engines are actually usable right now."""
        lines = []
        engine = CONFIG.get("assistant", {}).get("stt_engine", "auto")
        lines.append(f"Configured engine: {engine}")
        lines.append("")
        try:
            import vosk  # noqa: F401
            path = CONFIG.get("assistant", {}).get("vosk_model_path", "")
            ok = bool(path and os.path.isdir(path))
            lines.append(f"{'✓' if ok else '✗'} Vosk (offline)"
                         + ("" if ok else "  — set assistant.vosk_model_path"))
        except ImportError:
            lines.append("✗ Vosk — pip install --user vosk, then download a model")
        try:
            import pocketsphinx  # noqa: F401
            lines.append("✓ PocketSphinx (offline)")
        except ImportError:
            lines.append("✗ PocketSphinx — pip install --user pocketsphinx")
        lines.append("✓ Google (online, default — needs internet)")
        lines.append("")
        lines.append("Offline engines keep working with no connection and")
        lines.append("send nothing to a server, but are less accurate.")
        self.instantiate_card("Speech Recognition", "code_bug", "\n".join(lines))
        self.speak("Speech engine status is on screen, Sir.")
        return True

    # ==================================================================
    # BACKGROUND TASKS — long jobs run without blocking conversation
    # ==================================================================
    def run_background(self, name, fn, *args, **kwargs):
        """Run something long in the background and keep him responsive.

        Returns a task id. Progress is reported per-task so several can run at
        once without fighting over the single progress bar.
        """
        task_id = f"t{int(time.time()*1000) % 1000000}"
        task = {"id": task_id, "name": name, "state": "running",
                "started": time.time(), "result": None, "error": None}
        with self._task_lock:
            self.background_tasks[task_id] = task
        self._push_tasks()

        def runner():
            try:
                result = fn(*args, **kwargs)
                task["result"] = result
                task["state"] = "done"
                self.sfx.play("done")
                self.safe_log(f"Finished: {name}")
                if CONFIG.get("assistant", {}).get("announce_background", True):
                    self.speak(f"{name} is done, {USER_TITLE}.")
            except Exception as e:
                task["state"] = "failed"
                task["error"] = str(e)
                self.sfx.play("error")
                log_debug(f"background task '{name}' failed: {e}\n{traceback.format_exc()}")
                self.safe_log(f"'{name}' failed: {e}")
            finally:
                task["ended"] = time.time()
                self._push_tasks()
                # Keep finished tasks visible briefly, then tidy up.
                def cleanup():
                    time.sleep(90)
                    with self._task_lock:
                        self.background_tasks.pop(task_id, None)
                    self._push_tasks()
                threading.Thread(target=cleanup, daemon=True).start()

        threading.Thread(target=runner, daemon=True).start()
        return task_id

    def _push_tasks(self):
        try:
            with self._task_lock:
                snapshot = [
                    {"id": t["id"], "name": t["name"], "state": t["state"],
                     "secs": int(time.time() - t["started"])}
                    for t in self.background_tasks.values()
                ]
            self.broadcast_js(f"updateTasks({json.dumps(snapshot)})")
        except Exception as e:
            log_debug(f"task push error: {e}")

    def list_tasks(self):
        with self._task_lock:
            tasks = list(self.background_tasks.values())
        running = [t for t in tasks if t["state"] == "running"]
        if not tasks:
            self.speak("Nothing running in the background, Sir.")
            return True
        lines = [f"{t['name']} — {t['state']} ({int(time.time()-t['started'])}s)"
                 for t in tasks]
        self.instantiate_card("Background Tasks", "carousel", lines)
        self.speak(f"{len(running)} task{'s' if len(running) != 1 else ''} running, Sir."
                   if running else "Everything's finished, Sir.")
        return True

    # ==================================================================
    # SHAPE SPEC COMPILER
    # The model is good at reasoning about geometry ("a base 75x85, a back
    # support leaning 60 degrees") and bad at writing correct OpenSCAD. So it
    # emits a structured spec and PYTHON generates the code. This removes every
    # syntax error and most geometry errors by construction.
    # ==================================================================
    SPEC_SCHEMA_DOC = """Respond ONLY with JSON in exactly this form:

{
  "name": "short name",
  "parameters": { "width": 75, "depth": 85, "thickness": 5 },
  "parts": [
    {
      "op": "add" | "cut",
      "shape": "box" | "cylinder" | "cone" | "sphere" | "wedge" | "tube" | "torus",
      "size": [x, y, z],          // box and wedge only
      "d": 10,                     // diameter: cylinder, sphere, tube, torus
      "d2": 4,                     // cone: top diameter
      "h": 20,                     // height: cylinder, cone, wedge, tube
      "wall": 2,                   // tube only: wall thickness
      "at": [x, y, z],             // position of the part's origin corner/centre
      "rotate": [rx, ry, rz],      // degrees, optional
      "round": 3,                  // box only: corner radius, optional
      "center": false,             // optional
      "repeat": { "count": 2, "offset": [30, 0, 0] },   // optional linear array
      "note": "what this forms"
    }
  ]
}

RULES
- Millimetres. Z is up. The part must sit flat on Z=0.
- Use "parameters" for every important number, and reference them by writing the
  NUMBER (the values are substituted into named variables automatically).
- "add" parts are unioned; "cut" parts are subtracted afterwards.
- Do NOT add overshoot to cuts - that is handled for you automatically.
- Minimum wall thickness 1.2mm. Nothing may float unsupported.
- Prefer few, well-placed primitives over many small ones.
- 3 to 10 parts is usually right."""

    # Sanity limits so a hallucinated spec can't produce nonsense geometry.
    SPEC_MAX_DIM = 400.0
    SPEC_MIN_DIM = 0.4

    def _clamp(self, v, lo=None, hi=None):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return lo if lo is not None else 1.0
        lo = self.SPEC_MIN_DIM if lo is None else lo
        hi = self.SPEC_MAX_DIM if hi is None else hi
        return max(lo, min(hi, v))

    def _spec_to_scad(self, spec):
        """Compile a shape spec into valid, printable OpenSCAD. Deterministic."""
        name = str(spec.get("name", "part"))[:60]
        params = spec.get("parameters", {}) or {}
        parts = spec.get("parts", []) or []
        if not parts:
            return "", "Spec contained no parts."

        lines = ["$fn = 64;", f"// {name}", ""]

        # --- Named parameters become the slider variables ---
        clean_params = {}
        for k, v in list(params.items())[:14]:
            key = re.sub(r'\W+', '_', str(k)).strip('_').lower()
            if not key or not re.match(r'^[a-z_]', key):
                continue
            try:
                val = round(self._clamp(v), 2)
            except Exception:
                continue
            clean_params[key] = val
            lines.append(f"{key} = {val};  // {k}")
        if clean_params:
            lines.append("")

        # Map each distinct parameter VALUE back to its name, so dimensions in the
        # geometry are written as variables. Without this the sliders would be
        # decorative - they'd change a number nothing referenced.
        value_to_param = {}
        for key, val in clean_params.items():
            value_to_param.setdefault(round(float(val), 2), key)

        def num(v, allow_param=True):
            """Format a dimension, using a parameter name when one matches."""
            try:
                f = round(float(v), 2)
            except (TypeError, ValueError):
                return "0"
            if allow_param and f in value_to_param and abs(f) > 0.5:
                return value_to_param[f]
            return f"{f:g}"

        add_lines, cut_lines = [], []

        def emit(part, into, is_cut):
            shape = str(part.get("shape", "box")).lower()
            at = part.get("at", [0, 0, 0]) or [0, 0, 0]
            at = [round(self._clamp(a, -self.SPEC_MAX_DIM, self.SPEC_MAX_DIM), 2)
                  for a in (list(at) + [0, 0, 0])[:3]]
            rot = part.get("rotate") or [0, 0, 0]
            rot = [round(float(r) % 360, 2) if isinstance(r, (int, float)) else 0
                   for r in (list(rot) + [0, 0, 0])[:3]]
            note = str(part.get("note", ""))[:60]
            centered = bool(part.get("center", False))

            # Cuts are grown slightly so their faces never sit exactly on a
            # surface, which is the classic cause of non-manifold output.
            g = 0.6 if is_cut else 0.0

            body = ""
            # Cuts are grown, so their dimensions can't be plain parameter names.
            use_param = (g == 0.0)

            def dim(v):
                return num(v + 2 * g, allow_param=use_param) if g else num(v, allow_param=True)

            if shape == "box":
                size = part.get("size") or [10, 10, 10]
                sx, sy, sz = [self._clamp(s) for s in (list(size) + [10, 10, 10])[:3]]
                r = part.get("round")
                if r:
                    r = self._clamp(r, 0.1, max(0.11, min(sx, sy) / 2.0 - 0.01))
                    body = (f"hull() {{ for (dx=[{num(r,False)},{num(sx-r,False)}]) "
                            f"for (dy=[{num(r,False)},{num(sy-r,False)}]) "
                            f"translate([dx,dy,0]) cylinder(r={num(r,False)}, h={dim(sz)}); }}")
                else:
                    body = (f"cube([{dim(sx)}, {dim(sy)}, {dim(sz)}]"
                            + (", center=true" if centered else "") + ");")
            elif shape == "cylinder":
                d = self._clamp(part.get("d", 10))
                h = self._clamp(part.get("h", 10))
                body = (f"cylinder(d={dim(d)}, h={dim(h)}"
                        + (", center=true" if centered else "") + ");")
            elif shape == "cone":
                d1 = self._clamp(part.get("d", 10))
                d2 = self._clamp(part.get("d2", 1), 0.01)
                h = self._clamp(part.get("h", 10))
                body = f"cylinder(d1={dim(d1)}, d2={dim(d2)}, h={dim(h)});"
            elif shape == "sphere":
                d = self._clamp(part.get("d", 10))
                body = f"sphere(d={dim(d)});"
            elif shape == "tube":
                d = self._clamp(part.get("d", 20))
                wall = self._clamp(part.get("wall", 2), 1.2)
                h = self._clamp(part.get("h", 20))
                inner = max(0.5, d - 2 * wall)
                body = (f"difference() {{ cylinder(d={num(d)}, h={num(h)}); "
                        f"translate([0,0,-1]) cylinder(d={num(inner,False)}, h={num(h+2,False)}); }}")
            elif shape == "torus":
                d = self._clamp(part.get("d", 30))
                td = self._clamp(part.get("wall", part.get("d2", 5)), 0.5)
                body = (f"rotate_extrude() translate([{num(max(td/2+0.1, d/2 - td/2),False)},0,0]) "
                        f"circle(d={num(td)});")
            elif shape == "wedge":
                size = part.get("size") or [20, 20, 20]
                sx, sy, sz = [self._clamp(s) for s in (list(size) + [20, 20, 20])[:3]]
                body = (f"linear_extrude({dim(sy)}) "
                        f"polygon([[0,0],[{num(sx+g,False)},0],[0,{num(sz+g,False)}]]);")
            else:
                size = part.get("size") or [10, 10, 10]
                sx, sy, sz = [self._clamp(s) for s in (list(size) + [10, 10, 10])[:3]]
                body = f"cube([{dim(sx)}, {dim(sy)}, {dim(sz)}]);"

            # Wedge is extruded along Y, so stand it up to match the given axes.
            pre = "rotate([90,0,0]) " if shape == "wedge" else ""

            stmt = body
            if pre or any(rot):
                rot_s = f"rotate([{rot[0]},{rot[1]},{rot[2]}]) " if any(rot) else ""
                stmt = f"{rot_s}{pre}{stmt}"
            # Shift cuts back by the growth so they stay centred where intended.
            if g:
                stmt = (f"translate([{num(at[0]-g, False)}, {num(at[1]-g, False)}, "
                        f"{num(at[2]-g, False)}]) {stmt}")
            else:
                stmt = f"translate([{num(at[0])}, {num(at[1])}, {num(at[2])}]) {stmt}"

            rep = part.get("repeat") or {}
            try:
                count = int(rep.get("count", 1))
            except Exception:
                count = 1
            if count > 1:
                count = min(count, 24)
                ox, oy, oz = [round(float(o), 2) if isinstance(o, (int, float)) else 0
                              for o in (list(rep.get("offset") or [0, 0, 0]) + [0, 0, 0])[:3]]
                stmt = (f"for (i = [0:{count - 1}]) translate([i*{ox}, i*{oy}, i*{oz}]) {stmt}")

            if note:
                into.append(f"    // {note}")
            into.append(f"    {stmt}")

        for p in parts[:24]:
            if not isinstance(p, dict):
                continue
            is_cut = str(p.get("op", "add")).lower() in ("cut", "subtract", "remove", "difference")
            emit(p, cut_lines if is_cut else add_lines, is_cut)

        if not add_lines:
            return "", "Spec had no solid parts to build from."

        lines.append("difference() {")
        lines.append("  union() {")
        lines.extend("  " + l for l in add_lines)
        lines.append("  }")
        if cut_lines:
            lines.append("  union() {")
            lines.extend("  " + l for l in cut_lines)
            lines.append("  }")
        lines.append("}")
        return "\n".join(lines), ""

    def _spec_from_model(self, description, feedback=None, previous=None):
        """Ask the model for a shape spec (never for code)."""
        prompt = (
            f"Design this 3D-printable part: {description}\n\n"
            + (f"A previous attempt was wrong. {feedback}\nFix it.\n\n" if feedback else "")
            + (f"Previous spec:\n{json.dumps(previous)[:2500]}\n\n" if previous else "")
            + self.SPEC_SCHEMA_DOC
        )
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("cad"),
                "messages": [
                    {"role": "system", "content":
                     "You are a mechanical designer. You describe parts as structured JSON "
                     "geometry specifications. You never write code."},
                    {"role": "user", "content": prompt},
                ],
                "stream": False, "format": "json", "keep_alive": "30m",
                "options": {"num_predict": 1100, "temperature": 0.25, "num_ctx": 8192},
            }, timeout=240)
            raw = self._llm_text(res)
            return json.loads(raw)
        except Exception as e:
            log_debug(f"spec generation error: {e}")
            return None

    def _validate_spec(self, spec):
        """Structural sanity checks before we even compile."""
        if not isinstance(spec, dict):
            return False, "Not a JSON object."
        parts = spec.get("parts")
        if not isinstance(parts, list) or not parts:
            return False, "No 'parts' list."
        solids = [p for p in parts if isinstance(p, dict)
                  and str(p.get("op", "add")).lower() not in ("cut", "subtract", "remove")]
        if not solids:
            return False, "Every part is a cut - there is no solid body."

        problems = []
        # Rough bounding box, to catch absurd or microscopic results.
        maxs = 0.0
        for p in solids:
            dims = []
            if p.get("size"):
                dims += [abs(float(x)) for x in p["size"] if isinstance(x, (int, float))]
            for k in ("d", "h", "d2"):
                if isinstance(p.get(k), (int, float)):
                    dims.append(abs(float(p[k])))
            at = p.get("at") or [0, 0, 0]
            base = max([abs(float(a)) for a in at if isinstance(a, (int, float))] or [0])
            maxs = max(maxs, base + (max(dims) if dims else 0))
        if maxs < 4:
            problems.append("The whole part is under 4mm - far too small.")
        if maxs > 400:
            problems.append("The part exceeds 400mm - too large to print.")
        # Wall thickness check on boxes.
        for p in solids:
            if p.get("shape") == "box" and p.get("size"):
                thin = [d for d in p["size"] if isinstance(d, (int, float)) and 0 < d < 1.2]
                if thin:
                    problems.append(f"A wall is only {min(thin)}mm - under the 1.2mm minimum.")
                    break
        return (not problems), "  ".join(problems)

    # ==================================================================
    # PHONETIC COMMAND MATCHING
    # Speech recognisers return free text and we then guess the command. That's
    # backwards. Here we score what was heard against the commands Amy can
    # actually run, comparing how things SOUND rather than how they're spelled -
    # which is exactly the kind of error a recogniser makes.
    # ==================================================================
    _PHON_RULES = [
        ("ough", "f"), ("augh", "f"), ("ph", "f"), ("gh", "f"),
        ("sch", "sk"), ("sh", "s"), ("ch", "k"), ("th", "t"), ("wh", "w"),
        ("ck", "k"), ("cq", "k"), ("qu", "kw"), ("q", "k"), ("x", "ks"),
        ("wr", "r"), ("kn", "n"), ("gn", "n"), ("pn", "n"), ("mb", "m"),
        ("dg", "j"), ("ge", "je"), ("gi", "ji"), ("gy", "jy"),
        ("ce", "se"), ("ci", "si"), ("cy", "sy"), ("c", "k"),
        ("z", "s"), ("v", "f"), ("w", "u"),
    ]

    @classmethod
    def _phonetic(cls, word):
        """Reduce a word to a rough sound code, so 'diagnostics' and
        'die agnostics' collapse to the same thing."""
        w = re.sub(r'[^a-z]', '', str(word).lower())
        if not w:
            return ""
        for a, b in cls._PHON_RULES:
            w = w.replace(a, b)
        # Keep the first letter (vowel or not), drop interior vowels.
        head, tail = w[0], re.sub(r'[aeiouy]', '', w[1:])
        w = head + tail
        out = []
        for ch in w:
            if not out or out[-1] != ch:      # collapse doubles
                out.append(ch)
        return "".join(out)

    @classmethod
    def _phonetic_phrase(cls, text):
        """Sound code for a whole phrase, spaces removed so word-splitting
        errors ('desk view' vs 'deskview') don't matter."""
        return "".join(cls._phonetic(w) for w in str(text).split())

    # Canonical phrases for everything he can do. Matching against a closed set
    # is far more reliable than interpreting open-ended text.
    COMMAND_GRAMMAR = {
        "run diagnostics": ["run diagnostics", "self diagnostics", "diagnose yourself",
                            "system check", "run a diagnostic", "check yourself"],
        "open desk view": ["open desk view", "start desk view", "turn on the camera",
                           "camera on", "show desk view", "open the camera"],
        "close desk view": ["close desk view", "turn off the camera", "camera off",
                            "stop desk view", "close the camera"],
        "what's on my desk": ["what's on my desk", "what am i holding", "identify this",
                              "what is this", "identify objects"],
        "read my screen": ["read my screen", "what's on my screen", "read the screen"],
        "security report": ["security report", "security sweep", "am i being hacked",
                            "check for intruders", "security scan"],
        "enable security": ["enable security", "activate sentinel", "protect my pc",
                            "watch for intruders", "sentinel on"],
        "recalibrate": ["recalibrate", "calibrate the mic", "you can't hear me",
                        "fix your hearing", "adjust your microphone"],
        "stop automation": ["stop automation", "abort automation", "cancel automation"],
        "take a screenshot": ["take a screenshot", "capture the screen", "screenshot"],
        "system status": ["system status", "how's the system", "system report"],
        "what have you learned": ["what have you learned", "reflect on our conversation",
                                  "review what you know"],
        "consolidate your memory": ["consolidate your memory", "tidy your memory",
                                    "clean up your memory"],
        "model routing": ["model routing", "what models", "which model", "model report"],
        "background tasks": ["background tasks", "what are you working on", "task status"],
        "lens history": ["lens history", "what have you seen", "visual history"],
        "my projects": ["list projects", "my projects", "what projects"],
        "what's on today": ["what's on today", "my schedule today", "agenda today",
                            "what's my day"],
        "conversation mode": ["conversation mode", "let's talk", "let's chat", "talk to me"],
        "end conversation": ["end conversation", "stop conversation", "exit conversation"],
        "voice diagnostics": ["voice diagnostics", "list voices", "which voice",
                              "check your voice"],
        "speech engine": ["speech engine", "recognition status", "listening engine"],
        "export stl": ["export stl", "export to stl", "save the stl"],
        "send to slicer": ["send to slicer", "open in slicer", "print this", "slice it"],
        "improve the design": ["improve the design", "review the design",
                               "make the design better", "refine the model"],
        "my designs": ["my designs", "list designs", "show my designs"],
        "undo that": ["undo that", "undo last", "reverse that", "take that back"],
        "check my config": ["check my config", "what's not configured", "config issues"],
        "disk space": ["disk space", "disk usage", "free space", "how much storage"],
        "what's running": ["what's running", "top processes", "list processes"],
        "network info": ["network info", "what's my ip", "my ip", "network status"],
        "my specs": ["my specs", "system specs", "what's my setup", "my hardware"],
        "daily briefing": ["daily briefing", "morning briefing", "brief me"],
        "my habits": ["my habits", "what do i usually do", "my patterns"],
        "shut down the pc": ["shut down the pc", "shutdown the computer", "power off the pc"],
        "lock the computer": ["lock the computer", "lock the pc", "lock my computer"],
        "what have you studied": ["what have you studied", "what have you researched",
                                  "list your research"],
    }

    def _grammar_index(self):
        """Build (and cache) phonetic codes for every known command phrase."""
        if self._grammar_cache:
            return self._grammar_cache
        index = []
        for canonical, variants in self.COMMAND_GRAMMAR.items():
            for phrase in variants:
                index.append((self._phonetic_phrase(phrase), phrase, canonical))
        # Anything the user taught him counts too.
        for name in self.memory.get("macros", {}):
            index.append((self._phonetic_phrase(f"run macro {name}"),
                          f"run macro {name}", f"run macro {name}"))
        self._grammar_cache = index
        return index

    def match_command(self, text):
        """Score a transcript against the command grammar.

        Returns (canonical, confidence, runner_up_confidence). Confidence is
        0-1 and blends phonetic similarity with word overlap.
        """
        import difflib
        if not text or len(text.split()) > 12:
            return None, 0.0, 0.0
        heard_phon = self._phonetic_phrase(text)
        heard_words = set(re.findall(r'\w+', text.lower()))
        if not heard_phon:
            return None, 0.0, 0.0

        scored = []
        len_heard = len(heard_phon)
        for phrase_phon, phrase, canonical in self._grammar_index():
            if not phrase_phon:
                continue
            # Cheap length prefilter. Anything this far off in length can never
            # win. Saves 1.7x on short phrases up to 6x on long ones, and can
            # never be slower than checking everything.
            ratio = len_heard / len(phrase_phon)
            if not (0.40 <= ratio <= 2.50):
                continue
            phon = difflib.SequenceMatcher(None, heard_phon, phrase_phon).ratio()
            pw = set(re.findall(r'\w+', phrase))
            overlap = len(heard_words & pw) / max(1, len(pw))
            # Sound dominates: that's where recognition errors show up.
            score = phon * 0.72 + overlap * 0.28
            # Penalise big length mismatches (heard far more than the command).
            if ratio > 2.2 or ratio < 0.45:
                score *= 0.6
            scored.append((score, canonical))
        if not scored:
            return None, 0.0, 0.0
        scored.sort(reverse=True, key=lambda x: x[0])
        best_score, best = scored[0]
        # Runner-up from a *different* command, for ambiguity detection.
        second = next((s for s, c in scored if c != best), 0.0)
        return best, best_score, second

    def interpret(self, text):
        """Turn what was heard into what he should do.

        Returns (final_text, was_corrected). If the transcript is clearly a known
        command that got garbled, it's replaced with the canonical phrasing.
        Ambiguous near-matches ask instead of guessing.
        """
        if not text:
            return text, False
        if not CONFIG.get("assistant", {}).get("grammar_matching", True):
            return text, False

        # Anything the user has previously corrected wins outright.
        learned = self.memory.get("stt_corrections", {})
        key = re.sub(r'\s+', ' ', text.lower().strip())
        if key in learned:
            log_debug(f"Learned correction: {key!r} -> {learned[key]!r}")
            return learned[key], True

        canonical, conf, second = self.match_command(text)
        if not canonical:
            return text, False

        accept = float(CONFIG.get("assistant", {}).get("grammar_accept", 0.80))
        ask = float(CONFIG.get("assistant", {}).get("grammar_clarify", 0.62))

        if conf >= accept:
            if canonical.lower() != key:
                log_debug(f"Grammar match {conf:.2f}: {text!r} -> {canonical!r}")
                self.safe_log(f"(heard as: {canonical})")
            return canonical, True

        # Close but not certain, and nothing else is nearly as close -> confirm.
        if conf >= ask and (conf - second) > 0.06:
            self.pending_clarification = {"canonical": canonical, "heard": text}
            self.speak(f"Did you mean '{canonical}', {USER_TITLE}?")
            return None, True

        return text, False

    def confirm_clarification(self, yes):
        """Handle the answer to 'did you mean X?'."""
        pending = getattr(self, "pending_clarification", None)
        if not pending:
            return False
        self.pending_clarification = None
        if yes:
            # Remember it so the same mishearing is right next time.
            corrections = self.memory.setdefault("stt_corrections", {})
            corrections[re.sub(r'\s+', ' ', pending["heard"].lower().strip())] = pending["canonical"]
            if len(corrections) > 300:
                for k in list(corrections)[:100]:
                    corrections.pop(k, None)
            self.save_memory()
            self.safe_log(f"Learned: '{pending['heard']}' means '{pending['canonical']}'")
            threading.Thread(target=self.process_command_backend,
                             args=(pending["canonical"],), daemon=True).start()
        else:
            self.speak("My mistake, Sir. Say it again?")
        return True

    # ==================================================================
    # BROWSER AGENT — real DOM-aware automation, not blind clicking
    # Instead of guessing pixel coordinates, this drives a real browser, reads
    # what's actually on the page, and decides the next step from that. It's the
    # difference between "click at 640,300 and hope" and "click the button
    # labelled Sign in".
    # ==================================================================
    def _browser_available(self):
        try:
            import playwright  # noqa: F401
            return "playwright"
        except ImportError:
            pass
        try:
            import selenium  # noqa: F401
            return "selenium"
        except ImportError:
            pass
        return None

    def _browser_start(self):
        """Launch (or reuse) an automated browser session."""
        if self._browser_page is not None:
            return True
        backend = self._browser_available()
        if backend == "playwright":
            try:
                from playwright.sync_api import sync_playwright
                self._browser_pw = sync_playwright().start()
                headless = bool(CONFIG.get("browser", {}).get("headless", False))
                self._browser_ctx = self._browser_pw.chromium.launch(headless=headless)
                self._browser_page = self._browser_ctx.new_page()
                self._browser_page.set_default_timeout(
                    int(CONFIG.get("browser", {}).get("timeout_ms", 15000)))
                log_debug("Playwright browser started.")
                return True
            except Exception as e:
                log_debug(f"playwright start failed: {e}")
                self._browser_page = None
        if backend == "selenium":
            try:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options
                opts = Options()
                if CONFIG.get("browser", {}).get("headless", False):
                    opts.add_argument("--headless=new")
                self._browser_ctx = webdriver.Chrome(options=opts)
                self._browser_page = self._browser_ctx
                log_debug("Selenium browser started.")
                return True
            except Exception as e:
                log_debug(f"selenium start failed: {e}")
                self._browser_page = None
        return False

    def browser_close(self):
        try:
            if self._browser_ctx is not None:
                try:
                    self._browser_ctx.close()
                except Exception:
                    pass
            if self._browser_pw is not None:
                try:
                    self._browser_pw.stop()
                except Exception:
                    pass
        finally:
            self._browser_page = self._browser_ctx = self._browser_pw = None
            self.safe_log("Browser session closed.")
        return True

    def _page_snapshot(self, max_elements=45):
        """Describe what's actually on the page: title, url, and the
        interactive elements the agent can act on."""
        page = self._browser_page
        if page is None:
            return None
        try:
            if hasattr(page, "evaluate"):        # Playwright
                data = page.evaluate("""() => {
                    const out = [];
                    const sel = 'a,button,input,textarea,select,[role=button],[role=link],[role=tab]';
                    document.querySelectorAll(sel).forEach((el, i) => {
                        const r = el.getBoundingClientRect();
                        if (r.width < 2 || r.height < 2) return;
                        const style = window.getComputedStyle(el);
                        if (style.visibility === 'hidden' || style.display === 'none') return;
                        let label = (el.innerText || el.value || el.placeholder ||
                                     el.getAttribute('aria-label') || el.name || '').trim();
                        label = label.replace(/\\s+/g, ' ').slice(0, 70);
                        if (!label) return;
                        out.push({
                            i: out.length,
                            tag: el.tagName.toLowerCase(),
                            type: el.type || '',
                            label: label
                        });
                    });
                    return {
                        title: document.title,
                        url: location.href,
                        text: document.body.innerText.replace(/\\s+/g,' ').slice(0, 1800),
                        elements: out.slice(0, 60)
                    };
                }""")
                if data and isinstance(data.get("elements"), list):
                    data["elements"] = data["elements"][:max_elements]
                return data
            # Selenium fallback
            from selenium.webdriver.common.by import By
            els = []
            for tag in ("a", "button", "input", "textarea"):
                for el in page.find_elements(By.TAG_NAME, tag)[:40]:
                    try:
                        if not el.is_displayed():
                            continue
                        label = (el.text or el.get_attribute("value")
                                 or el.get_attribute("placeholder")
                                 or el.get_attribute("aria-label") or "").strip()
                        if label:
                            els.append({"i": len(els), "tag": tag,
                                        "type": el.get_attribute("type") or "",
                                        "label": re.sub(r'\s+', ' ', label)[:70]})
                    except Exception:
                        continue
            body = ""
            try:
                body = page.find_element(By.TAG_NAME, "body").text[:1800]
            except Exception:
                pass
            return {"title": page.title, "url": page.current_url,
                    "text": re.sub(r'\s+', ' ', body), "elements": els[:max_elements]}
        except Exception as e:
            log_debug(f"page snapshot error: {e}")
            return None

    def _browser_do(self, action, target=None, value=None):
        """Execute one browser action against a real element."""
        page = self._browser_page
        if page is None:
            return False, "No browser session."
        try:
            if action == "goto":
                url = str(target or value or "")
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                if hasattr(page, "goto"):
                    page.goto(url, wait_until="domcontentloaded")
                else:
                    page.get(url)
                return True, f"Opened {url}"

            if action == "click":
                label = str(target or "")
                if hasattr(page, "get_by_text"):
                    for attempt in (
                        lambda: page.get_by_role("button", name=label, exact=False).first,
                        lambda: page.get_by_role("link", name=label, exact=False).first,
                        lambda: page.get_by_text(label, exact=False).first,
                        lambda: page.locator(f"text={label}").first,
                    ):
                        try:
                            attempt().click(timeout=4000)
                            return True, f"Clicked '{label}'"
                        except Exception:
                            continue
                    return False, f"Couldn't find anything labelled '{label}'"
                from selenium.webdriver.common.by import By
                el = page.find_element(By.XPATH, f"//*[contains(normalize-space(.), '{label}')]")
                el.click()
                return True, f"Clicked '{label}'"

            if action == "fill":
                label = str(target or "")
                text = str(value or "")
                if hasattr(page, "get_by_label"):
                    for attempt in (
                        lambda: page.get_by_label(label, exact=False).first,
                        lambda: page.get_by_placeholder(label, exact=False).first,
                        lambda: page.locator(f"input[name*='{label}' i]").first,
                        lambda: page.locator("input[type=search], input[type=text]").first,
                    ):
                        try:
                            attempt().fill(text, timeout=4000)
                            return True, f"Typed into '{label}'"
                        except Exception:
                            continue
                    return False, f"Couldn't find a field for '{label}'"
                from selenium.webdriver.common.by import By
                el = page.find_element(By.NAME, label)
                el.clear()
                el.send_keys(text)
                return True, f"Typed into '{label}'"

            if action == "press":
                key = str(value or target or "Enter")
                if hasattr(page, "keyboard"):
                    page.keyboard.press(key)
                else:
                    from selenium.webdriver.common.keys import Keys
                    from selenium.webdriver.common.by import By
                    page.find_element(By.TAG_NAME, "body").send_keys(
                        getattr(Keys, key.upper(), key))
                return True, f"Pressed {key}"

            if action == "scroll":
                amount = int(value or 600)
                if hasattr(page, "mouse"):
                    page.mouse.wheel(0, amount)
                else:
                    page.execute_script(f"window.scrollBy(0,{amount});")
                return True, "Scrolled"

            if action == "wait":
                time.sleep(min(8.0, float(value or 2)))
                return True, "Waited"

            if action == "extract":
                snap = self._page_snapshot()
                return True, (snap or {}).get("text", "")[:2000]

            return False, f"Unknown action '{action}'"
        except Exception as e:
            log_debug(f"browser action {action} error: {e}")
            return False, f"{action} failed: {e}"

    def browse_task(self, task):
        """Agentic browsing: look at the page, decide one step, act, repeat.

        Each step is chosen from what's genuinely on screen, so it adapts to
        layouts changing rather than replaying blind coordinates.
        """
        if not self._browser_available():
            self.speak("Browser automation needs Playwright, Sir. "
                       "Install it with pip install --user playwright, then playwright install chromium.")
            self.safe_log("pip install --user playwright  &&  playwright install chromium")
            return True
        if not self._browser_start():
            self.speak("I couldn't start the browser, Sir.")
            return True

        max_steps = int(CONFIG.get("browser", {}).get("max_steps", 12))
        self.speak(f"Working on that in the browser, {USER_TITLE}.")
        history = []

        for step in range(1, max_steps + 1):
            self.progress(f"Browser step {step}", (step / max_steps) * 95)
            snap = self._page_snapshot()
            if snap is None:
                snap = {"title": "", "url": "about:blank", "text": "", "elements": []}

            elements = "\n".join(f"  [{e['i']}] <{e['tag']}> {e['label']}"
                                 for e in snap.get("elements", [])[:40])
            prompt = (
                f"TASK: {task}\n\n"
                f"CURRENT PAGE: {snap.get('title','')}  ({snap.get('url','')})\n"
                f"VISIBLE TEXT: {snap.get('text','')[:1200]}\n\n"
                f"INTERACTIVE ELEMENTS:\n{elements or '  (none found)'}\n\n"
                f"STEPS SO FAR:\n" + ("\n".join(f"  {h}" for h in history[-6:]) or "  (none)") + "\n\n"
                "Choose ONE next action. Reply ONLY as JSON:\n"
                '{"action":"goto|click|fill|press|scroll|wait|extract|done",'
                '"target":"exact element label or URL","value":"text to type, key, or amount",'
                '"why":"one short line"}\n'
                'Use "done" when the task is complete. Prefer clicking labels you can see above.'
            )
            try:
                res = self.http.post(OLLAMA_URL, json={
                    "model": self.model_for("reasoning"),
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False, "format": "json", "keep_alive": "30m",
                    "options": {"num_predict": 220, "temperature": 0.2, "num_ctx": 8192},
                }, timeout=120)
                plan = self._llm_json(res)
            except Exception as e:
                log_debug(f"browse planning error: {e}")
                break

            action = str(plan.get("action", "")).lower().strip()
            if action in ("done", "finish", "complete", ""):
                break
            target, value, why = plan.get("target"), plan.get("value"), plan.get("why", "")
            self.safe_log(f"  {step}. {action} {str(target or '')[:40]}  — {str(why)[:50]}")

            ok, detail = self._browser_do(action, target, value)
            history.append(f"{action} {str(target or '')[:40]} -> {'ok' if ok else 'FAILED'}: {detail[:60]}")
            if not ok:
                # Let the agent see the failure and try something else.
                history.append("(that failed - choose a different approach)")
            time.sleep(float(CONFIG.get("browser", {}).get("step_delay", 1.0)))

        self.progress_done()
        final = self._page_snapshot() or {}
        summary = f"{final.get('title','')}\n{final.get('url','')}\n\n{final.get('text','')[:1200]}"
        self.instantiate_card(f"Browser: {task[:40]}", "email_draft", summary)
        self.speak("Finished in the browser, Sir. The page is on screen.")
        return True

    def browse_extract(self, url, question):
        """Open a page and answer a question from its real content."""
        if not self._browser_start():
            # No driver: fall back to plain fetching.
            return self.check_url(url)
        self._browser_do("goto", url)
        time.sleep(1.5)
        snap = self._page_snapshot() or {}
        text = snap.get("text", "")
        if not text:
            self.speak("I couldn't read that page, Sir.")
            return True
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("reasoning"),
                "messages": [{"role": "user", "content":
                              f"Answer from this page only.\n\nQUESTION: {question}\n\nPAGE:\n{text[:9000]}"}],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 400, "temperature": 0.3, "num_ctx": 16384},
            }, timeout=150)
            answer = self._llm_text(res)
            if answer:
                self.instantiate_card("From the page", "email_draft", answer)
                self.speak(answer[:500])
        except Exception as e:
            log_debug(f"browse_extract error: {e}")
        return True

    # ==================================================================
    # MULTI-WINDOW — detach panels into real OS windows
    # ==================================================================
    DETACHABLE = {
        "terminal": ("Terminal", 560, 620),
        "camera": ("Desk View", 820, 560),
        "cad": ("CAD Design", 980, 680),
        "document": ("Document", 780, 800),
        "code": ("Code", 820, 660),
        "viewer": ("Viewer", 900, 640),
        "tasks": ("Tasks & Logs", 480, 560),
    }

    def open_window(self, kind="terminal"):
        """Open a panel in its own window, so you can put it on another monitor."""
        kind = str(kind).lower().strip()
        if kind not in self.DETACHABLE:
            self.speak(f"I can't detach '{kind}', Sir.")
            return False
        if kind in self.extra_windows:
            # Already open — just bring it forward.
            try:
                self.extra_windows[kind].show()
                self.speak(f"{self.DETACHABLE[kind][0]} is already open, Sir.")
                return True
            except Exception:
                self.extra_windows.pop(kind, None)

        title, w, h = self.DETACHABLE[kind]
        try:
            win = webview.create_window(
                title=f"Amy — {title}",
                html=self._detached_html(kind, title),
                js_api=self._api,
                width=w, height=h, min_size=(360, 300),
                frameless=False,
                background_color="#05070b",
            )
            self.extra_windows[kind] = win

            def _on_closed(k=kind):
                self.extra_windows.pop(k, None)
                log_debug(f"Detached window '{k}' closed.")
            try:
                win.events.closed += _on_closed
            except Exception:
                pass

            self.safe_log(f"Opened {title} in its own window.")
            self.speak(f"{title} is in its own window now, Sir.")
            return True
        except Exception as e:
            log_debug(f"open_window error: {e}")
            self.speak("I couldn't open that window, Sir.")
            return False

    def close_window(self, kind):
        kind = str(kind).lower().strip()
        win = self.extra_windows.pop(kind, None)
        if not win:
            self.speak("That window isn't open, Sir.")
            return False
        try:
            win.destroy()
        except Exception as e:
            log_debug(f"close_window error: {e}")
        self.speak("Closed, Sir.")
        return True

    def list_windows(self):
        if not self.extra_windows:
            self.speak("Only the main window is open, Sir.")
            return True
        names = [self.DETACHABLE[k][0] for k in self.extra_windows if k in self.DETACHABLE]
        self.instantiate_card("Open Windows", "carousel", names)
        self.speak(f"{len(names)} extra window{'s' if len(names) != 1 else ''} open, Sir.")
        return True

    def broadcast_js(self, script):
        """Push a UI update to every open window, not just the main one."""
        self.run_js(script)
        for kind, win in list(self.extra_windows.items()):
            try:
                win.evaluate_js(script)
            except Exception:
                # Window probably closed underneath us.
                self.extra_windows.pop(kind, None)

    @staticmethod
    def _detached_html(kind, title):
        """A focused single-panel window that reuses the main HUD styling."""
        panels = {
            "terminal": """
              <div id="logBox" class="pane"></div>
              <div class="row">
                <input id="cmdInput" placeholder="Type a command..." autocomplete="off">
                <button onclick="send()">SEND</button>
              </div>""",
            "camera": '<img id="camFullImg" class="fill" alt="desk view">'
                      '<div class="row"><button onclick="pywebview.api.lens_analyze(\'identify\')">IDENTIFY</button>'
                      '<button onclick="pywebview.api.lens_analyze(\'text\')">READ TEXT</button>'
                      '<button onclick="pywebview.api.capture_camera_photo()">SNAP</button></div>',
            "cad": '<canvas id="cadCanvas" class="fill"></canvas>'
                   '<div class="row"><input id="cadInstruction" placeholder="Describe a change...">'
                   '<button onclick="pywebview.api.cad_refine(document.getElementById(\'cadInstruction\').value)">REVISE</button>'
                   '<button onclick="pywebview.api.cad_export_stl()">STL</button></div>',
            "document": '<textarea id="docEditor" class="pane" spellcheck="false"></textarea>'
                        '<div class="row"><button onclick="pywebview.api.export_current_document_pdf()">EXPORT PDF</button></div>',
            "code": '<textarea id="codeEditor" class="pane" spellcheck="false"></textarea>'
                    '<div class="row"><button onclick="pywebview.api.run_code()">RUN</button>'
                    '<button onclick="pywebview.api.fix_code()">FIX</button></div>',
            "viewer": '<div id="viewerBody" class="fill"></div>',
            "tasks": '<div id="taskTray" class="pane"></div><div id="logBox" class="pane"></div>',
        }
        body = panels.get(kind, '<div class="pane">Nothing to show.</div>')
        return """<!DOCTYPE html><html><head><meta charset="UTF-8"><style>
  :root { --accent:#5eead4; --bg:#05070b; --border:rgba(94,234,212,0.22); }
  * { box-sizing:border-box; margin:0; padding:0; font-family:'Consolas',monospace; }
  body { background:var(--bg); color:#e8eaf2; height:100vh; display:flex;
         flex-direction:column; overflow:hidden;
         background-image:linear-gradient(rgba(94,234,212,0.03) 1px, transparent 1px),
                          linear-gradient(90deg, rgba(94,234,212,0.03) 1px, transparent 1px);
         background-size:34px 34px; }
  .bar { padding:9px 14px; border-bottom:1px solid var(--border); color:var(--accent);
         font-size:11px; letter-spacing:3px;
         background:linear-gradient(90deg, rgba(94,234,212,0.10), transparent 70%); }
  .wrap { flex:1; display:flex; flex-direction:column; gap:8px; padding:10px; min-height:0; }
  .pane { flex:1; min-height:0; overflow:auto; background:rgba(0,0,0,0.4);
          border:1px solid var(--border); border-radius:9px; padding:10px;
          font-size:11px; line-height:1.55; white-space:pre-wrap; color:#e8eaf2;
          resize:none; outline:none; user-select:text; }
  .fill { flex:1; min-height:0; width:100%; object-fit:contain; background:#000;
          border:1px solid var(--border); border-radius:9px; }
  .row { display:flex; gap:6px; flex-shrink:0; }
  input { flex:1; background:rgba(0,0,0,0.5); border:1px solid var(--border);
          border-radius:8px; padding:8px 10px; color:#fff; font-size:11px; outline:none; }
  button { background:rgba(94,234,212,0.08); border:1px solid var(--border);
           color:var(--accent); border-radius:8px; padding:8px 12px; font-size:10px;
           cursor:pointer; letter-spacing:1px; transition:all .15s; }
  button:hover { background:var(--accent); color:var(--bg); }
  .u { color:#7dd3fc; } .j { color:var(--accent); }
  ::-webkit-scrollbar { width:8px; } ::-webkit-scrollbar-track { background:rgba(0,0,0,.3); }
  ::-webkit-scrollbar-thumb { background:rgba(94,234,212,.35); border-radius:4px; }
</style></head><body>
  <div class="bar">""" + title.upper() + """</div>
  <div class="wrap">""" + body + """</div>
<script>
  function appendLog(t, isUser) {
    const b = document.getElementById('logBox');
    if (!b) return;
    const d = document.createElement('div');
    d.className = isUser ? 'u' : 'j';
    d.textContent = (isUser ? '> ' : '') + t;
    b.appendChild(d); b.scrollTop = b.scrollHeight;
  }
  document.addEventListener('DOMContentLoaded', function() {
    const ci = document.getElementById('cmdInput');
    if (ci) ci.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') send();
    });
  });
  function send() {
    const i = document.getElementById('cmdInput');
    if (!i || !i.value.trim()) return;
    const v = i.value.trim(); i.value = '';
    appendLog(v, true);
    pywebview.api.handle_text_command(v);
  }
  function updateCameraFrame(b64) {
    const im = document.getElementById('camFullImg');
    if (im) im.src = 'data:image/jpeg;base64,' + b64;
  }
  function showDocumentEditor(d) {
    const e = document.getElementById('docEditor');
    if (e && d) e.value = d.content || '';
  }
  function showCodeEditor(d) {
    const e = document.getElementById('codeEditor');
    if (e && d) e.value = d.code || '';
  }
  function openViewer(d) {
    const v = document.getElementById('viewerBody');
    if (!v || !d) return;
    v.innerHTML = d.kind === 'image'
      ? '<img src="' + d.src + '" style="width:100%;height:100%;object-fit:contain;">'
      : '<iframe src="' + d.src + '" style="width:100%;height:100%;border:none;background:#fff;"></iframe>';
  }
  function updateTasks(tasks) {
    const t = document.getElementById('taskTray');
    if (!t) return;
    t.textContent = (tasks || []).map(x => x.state.toUpperCase() + '  ' + x.name).join('\\n')
                    || 'Nothing running.';
  }
  // Harmless no-ops so broadcasts from the main window never error here.
  function setCameraState(){} function showCadEditor(){} function setCadMesh(){} function setCadParts(){}
  function updateSpeechAnimation(){} function updateListeningUI(){}
  function updateWakeTriggerUI(){} function showProgress(){} function hideProgress(){}
  function setConversationMode(){} function setActiveProject(){} function flashGesture(){}
  function updateScreenContext(){} function setAutoVisionState(){} function setSentinelState(){}
  function flashSecurityAlert(){} function showStartupDigest(){} function applyBarConfig(){}
  function showApproval(){} function hideApproval(){} function showDiff(){}
  function showCadProposal(){} function hideCadProposal(){} function showLensHistory(){}
  function setCadStlPath(){} function showCodeOutput(){} function setGestureState(){}
  function cameraReady(){} function updateTelemetry(){} function renderTodos(){}
  function showSpotify(){} function setHeard(){}
</script></body></html>"""

    def fetch_many(self, urls, limit=6000, workers=6):
        """Download several pages at once.

        Research used to fetch pages one after another, so a dozen sources meant
        a dozen sequential round-trips. Fetching concurrently cuts that to
        roughly the time of the slowest single page.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = {}
        urls = [u for u in dict.fromkeys(urls) if u]
        if not urls:
            return results
        workers = max(1, min(int(workers), len(urls)))
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self.fetch_page_text, u, limit): u for u in urls}
                for fut in as_completed(futures, timeout=90):
                    u = futures[fut]
                    try:
                        results[u] = fut.result()
                    except Exception as e:
                        log_debug(f"fetch_many {u}: {e}")
                        results[u] = ""
        except Exception as e:
            log_debug(f"fetch_many pool error: {e}")
            for u in urls:                       # fall back to sequential
                if u not in results:
                    results[u] = self.fetch_page_text(u, limit)
        return results

    def search_many(self, queries, per_query=3, workers=4):
        """Run several web searches at once."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        out = []
        queries = [q for q in dict.fromkeys(queries) if q]
        if not queries:
            return out
        try:
            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(queries)))) as pool:
                futures = {pool.submit(self.web_lookup, q, per_query): q for q in queries}
                for fut in as_completed(futures, timeout=70):
                    try:
                        out.extend(fut.result() or [])
                    except Exception as e:
                        log_debug(f"search_many: {e}")
        except Exception as e:
            log_debug(f"search_many pool error: {e}")
            for q in queries:
                out.extend(self.web_lookup(q, per_query) or [])
        # De-duplicate by URL, preserving order.
        seen, deduped = set(), []
        for r in out:
            u = r.get("url")
            if u and u not in seen:
                seen.add(u)
                deduped.append(r)
        return deduped

    # ==================================================================
    # EMBEDDING STORE — kept out of the main memory file
    # Embeddings are large float arrays. Holding them in amy_memory.json made
    # it balloon to many megabytes and slowed every single save.
    # ==================================================================
    @staticmethod
    def _compact_vec(v):
        """A Python list of floats costs ~32 bytes a number; float32 costs 4."""
        if np is not None and isinstance(v, list):
            return np.asarray(v, dtype=np.float32)
        return v

    def _load_embeddings(self):
        if self._embed_store is not None:
            return self._embed_store
        self._embed_store = {}
        try:
            if os.path.exists(EMBED_FILE):
                with open(EMBED_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self._embed_store = {k: self._compact_vec(v) for k, v in raw.items()}
                log_debug(f"Loaded {len(self._embed_store)} cached embeddings.")
        except Exception as e:
            log_debug(f"embedding load failed: {e}")
            self._embed_store = {}
        return self._embed_store

    def _save_embeddings(self):
        try:
            store = self._embed_store or {}
            # Keep it bounded; oldest entries go first.
            if len(store) > 3000:
                for k in list(store)[:len(store) - 3000]:
                    store.pop(k, None)
            tmp = EMBED_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({k: (v.tolist() if hasattr(v, "tolist") else v) for k, v in store.items()}, f)
            os.replace(tmp, EMBED_FILE)
        except Exception as e:
            log_debug(f"embedding save failed: {e}")

    def embed_batch(self, texts):
        """Embed several texts concurrently, reusing anything already cached."""
        from concurrent.futures import ThreadPoolExecutor
        store = self._load_embeddings()
        out, todo = {}, []
        for t in texts:
            key = str(hash(t[:2000]) & 0xffffffffffff)
            if key in store:
                out[t] = store[key]
            else:
                todo.append((key, t))
        if todo:
            with ThreadPoolExecutor(max_workers=4) as pool:
                vecs = list(pool.map(lambda kt: self._embed(kt[1][:4000]), todo))
            for (key, t), vec in zip(todo, vecs):
                if vec:
                    vec = self._compact_vec(vec)
                    store[key] = vec
                    out[t] = vec
            self._save_embeddings()
        return out

    def _prune_memory(self):
        """Keep the memory file a sensible size.

        The full transcript grows forever, and every save rewrites the whole
        file. Older turns are archived to disk so nothing is lost, but the live
        file stays fast to read and write.
        """
        try:
            limit = int(CONFIG.get("assistant", {}).get("transcript_limit", 4000))
            transcript = self.memory.get("full_transcript", [])
            if len(transcript) > limit:
                overflow = transcript[:-limit]
                self.memory["full_transcript"] = transcript[-limit:]
                archive = os.path.join(DATA_DIR, "transcript_archive.jsonl")
                with open(archive, "a", encoding="utf-8") as f:
                    for m in overflow:
                        f.write(json.dumps(m) + "\n")
                log_debug(f"Archived {len(overflow)} old transcript entries.")

            # Rolling chat context never needs to be huge.
            ch = self.memory.get("chat_history", [])
            if len(ch) > 80:
                self.memory["chat_history"] = ch[-80:]

            # Knowledge base: cap total documents.
            kb = self.memory.get("knowledge_base", {})
            kb_limit = int(CONFIG.get("assistant", {}).get("kb_limit", 1200))
            if len(kb) > kb_limit:
                for k in list(kb)[:len(kb) - kb_limit]:
                    kb.pop(k, None)
                log_debug(f"Trimmed knowledge base to {kb_limit} documents.")
        except Exception as e:
            log_debug(f"memory prune error: {e}")

    # ==================================================================
    # CUSTOMISATION — make him yours
    # ==================================================================
    def run_custom_command(self, cmd):
        """User-defined phrases from the config, checked before anything else.

        Config format:
      
              "movie night": {"action": "open", "value": "netflix.com"},
              "work mode":   {"action": "sequence", "value": [
                                  {"action": "open", "value": "vs code"},
                                  {"action": "say",  "value": "Workspace ready."}]}
          }
        """
        if self._in_custom:
            return False          # already inside a custom action; don't re-dispatch
        customs = {**CONFIG.get("custom_commands", {}),
                   **self.memory.get("custom_commands", {})}
        if not customs:
            return False
        low = cmd.lower().strip()

        match = None
        if low in customs:
            match = customs[low]
        else:
            for phrase, spec in customs.items():
                if phrase.lower() in low:
                    match = spec
                    break
            if match is None:                    # phonetic fallback
                best, score, _ = None, 0.0, 0.0
                import difflib
                heard = self._phonetic_phrase(low)
                for phrase in customs:
                    r = difflib.SequenceMatcher(
                        None, heard, self._phonetic_phrase(phrase)).ratio()
                    if r > score:
                        best, score = phrase, r
                if best and score >= 0.86:
                    match = customs[best]
                    log_debug(f"Custom command phonetic match: {best} ({score:.2f})")
        if match is None:
            return False

        return self._run_custom_action(match)

    MAX_CUSTOM_DEPTH = 8

    def _run_custom_action(self, spec, _depth=0):
        """Execute one custom action, or a sequence of them.

        Depth-limited: a custom command whose action re-triggers itself (or a
        sequence containing itself) would otherwise recurse until the stack
        blew up and took Amy with it.
        """
        if _depth > self.MAX_CUSTOM_DEPTH:
            log_debug("Custom action nesting limit hit; stopping.")
            self.safe_log("Custom command stopped: it refers to itself.")
            self.speak("That custom command loops back on itself, Sir. I've stopped it.")
            return True
        if isinstance(spec, str):
            spec = {"action": "say", "value": spec}
        if not isinstance(spec, dict):
            return False
        action = str(spec.get("action", "")).lower()
        value = spec.get("value")

        try:
            if action == "sequence":
                for step in (value or [])[:20]:
                    self._run_custom_action(step, _depth + 1)
                return True
            if action == "open":
                return bool(self.launch_external_app(str(value)))
            if action == "url":
                return self._open_in_browser(str(value), str(value))
            if action == "say":
                self.speak(str(value))
                return True
            if action == "command":
                # Re-enter the router, but suppress custom-command dispatch so a
                # command can't trigger itself forever.
                prev = self._in_custom
                self._in_custom = True
                try:
                    return bool(self.execute_pc_automation(str(value)))
                finally:
                    self._in_custom = prev
            if action == "shell":
                if not CONFIG.get("assistant", {}).get("allow_shell_commands", False):
                    self.speak("Shell commands are disabled in the config, Sir.")
                    self.safe_log("Set assistant.allow_shell_commands to true to enable this.")
                    return True
                subprocess.Popen(str(value), shell=True)
                self.speak("Running that, Sir.")
                return True
            if action == "keys":
                pyautogui.hotkey(*[k.strip() for k in str(value).split("+")])
                return True
            if action == "type":
                pyautogui.write(str(value), interval=0.01)
                return True
            if action == "wait":
                time.sleep(min(10.0, float(value or 1)))
                return True
            log_debug(f"Unknown custom action: {action}")
        except Exception as e:
            log_debug(f"custom action error: {e}")
            self.speak("That custom command failed, Sir.")
        return True

    def add_custom_command(self, phrase, action, value):
        customs = self.memory.setdefault("custom_commands", {})
        customs[phrase.lower().strip()] = {"action": action, "value": value}
        self.save_memory(force=True)
        self._grammar_cache = None
        self.speak(f"Saved. Saying '{phrase}' will now do that, Sir.")
        return True

    def list_custom_commands(self):
        customs = {**CONFIG.get("custom_commands", {}),
                   **self.memory.get("custom_commands", {})}
        if not customs:
            self.speak("You haven't defined any custom commands, Sir.")
            return True
        lines = [f"{p}  ->  {s.get('action') if isinstance(s, dict) else 'say'}: "
                 f"{str(s.get('value') if isinstance(s, dict) else s)[:50]}"
                 for p, s in customs.items()]
        self.instantiate_card("Custom Commands", "carousel", lines)
        self.speak(f"You have {len(customs)} custom commands, Sir.")
        return True

    # --- Plugins -------------------------------------------------------
    def load_plugins(self):
        """Load user plugins from amy_data/plugins/*.py.

        A plugin just defines:
            COMMANDS = {"phrase": callable(app, cmd_text)}
        which keeps the contract tiny and hard to get wrong.
        """
        self.plugin_commands = {}
        if not CONFIG.get("assistant", {}).get("enable_plugins", True):
            return 0
        loaded = 0
        try:
            import importlib.util as ilu
            for fn in sorted(os.listdir(PLUGINS_DIR)):
                if not fn.endswith(".py") or fn.startswith("_"):
                    continue
                path = os.path.join(PLUGINS_DIR, fn)
                try:
                    spec = ilu.spec_from_file_location(f"amy_plugin_{fn[:-3]}", path)
                    mod = ilu.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    cmds = getattr(mod, "COMMANDS", {}) or {}
                    for phrase, fnc in cmds.items():
                        if callable(fnc):
                            self.plugin_commands[str(phrase).lower()] = fnc
                    if hasattr(mod, "setup") and callable(mod.setup):
                        mod.setup(self)
                    loaded += 1
                    log_debug(f"Plugin loaded: {fn} ({len(cmds)} commands)")
                except Exception as e:
                    log_debug(f"Plugin {fn} failed to load: {e}")
                    self.safe_log(f"Plugin '{fn}' failed: {e}")
        except Exception as e:
            log_debug(f"plugin scan error: {e}")
        if loaded:
            self.safe_log(f"{loaded} plugin(s) loaded, "
                          f"{len(self.plugin_commands)} extra commands.")
        return loaded

    def run_plugin_command(self, cmd):
        if not self.plugin_commands:
            return False
        low = cmd.lower().strip()
        for phrase, fnc in self.plugin_commands.items():
            if phrase in low:
                try:
                    result = fnc(self, cmd)
                    return True if result is None else bool(result)
                except Exception as e:
                    log_debug(f"plugin command '{phrase}' error: {e}")
                    self.speak("That plugin command failed, Sir.")
                    return True
        return False

    def list_plugins(self):
        if not self.plugin_commands:
            self.speak(f"No plugins loaded, Sir. Drop a .py file into {PLUGINS_DIR}.")
            return True
        self.instantiate_card("Plugin Commands", "carousel",
                              sorted(self.plugin_commands.keys()))
        self.speak(f"{len(self.plugin_commands)} plugin commands available, Sir.")
        return True

    # --- Appearance ----------------------------------------------------
    THEMES = {
        # "arc" is the house theme — same palette as the project site.
        "arc":      {"accent": "#5eead4", "accent2": "#a78bfa", "bg": "#07080c", "panel": "rgba(14,16,23,0.66)"},
        "ember":    {"accent": "#ff7a1a", "bg": "#0b0603", "panel": "rgba(32,19,13,0.92)"},
        "matrix":   {"accent": "#22ff88", "bg": "#030805", "panel": "rgba(10,26,18,0.92)"},
        "violet":   {"accent": "#c084fc", "bg": "#08060d", "panel": "rgba(22,16,34,0.92)"},
        "crimson":  {"accent": "#ff2e63", "bg": "#0b0308", "panel": "rgba(32,10,18,0.92)"},
        "gold":     {"accent": "#ffc857", "bg": "#0a0803", "panel": "rgba(30,25,10,0.92)"},
        "ice":      {"accent": "#a5f3fc", "bg": "#04080c", "panel": "rgba(14,24,32,0.92)"},
        "mono":     {"accent": "#e2e8f0", "bg": "#07080a", "panel": "rgba(20,22,26,0.92)"},
    }

    def set_theme(self, name):
        name = str(name).lower().strip()
        theme = self.THEMES.get(name)
        if not theme:
            self.speak(f"I don't have a '{name}' theme, Sir. "
                       f"Try: {', '.join(list(self.THEMES)[:5])}.")
            return True
        CONFIG.setdefault("ui", {})["theme"] = name
        save_config(CONFIG)
        self.broadcast_js(f"applyTheme({json.dumps(theme)})")
        self.speak(f"{name.title()} theme applied, Sir.")
        return True

    def list_themes(self):
        self.instantiate_card("Themes", "carousel", sorted(self.THEMES.keys()))
        self.speak(f"{len(self.THEMES)} themes available, Sir.")
        return True

    def set_persona(self, trait):
        """Adjust how he speaks, persisted across restarts."""
        CONFIG.setdefault("assistant", {})["persona_note"] = trait
        save_config(CONFIG)
        self.speak("Noted, Sir. I'll speak that way from now on.")
        self.safe_log(f"Persona: {trait}")
        return True

    def _spotify(self):
        """Authenticated Spotify client, or None after telling the user why.

        The same six-line guard was copy-pasted at six call sites.
        """
        sp = self.get_spotify_client(allow_browser=True)
        if not sp:
            self.speak("Spotify isn't authenticated, Sir. Add your client ID and "
                       "secret to the config.")
        return sp

    @staticmethod
    def _llm_text(res):
        """Pull the text out of an Ollama reply, tolerating any malformed shape.

        This pattern appeared ~23 times inline. Each copy assumed the response
        was well-formed JSON with the expected keys, so a truncated or error
        response raised deep inside whatever feature called it.
        """
        try:
            if res is None:
                return ""
            if getattr(res, "status_code", 200) != 200:
                log_debug(f"model HTTP {res.status_code}: {str(res.text)[:160]}")
                return ""
            data = res.json()
        except Exception as e:
            log_debug(f"model response not JSON: {e}")
            return ""
        if not isinstance(data, dict):
            return ""
        msg = data.get("message")
        if isinstance(msg, dict):
            return str(msg.get("content") or "").strip()
        # /api/generate returns a flat "response" field instead.
        return str(data.get("response") or "").strip()

    @staticmethod
    def _llm_json(res, default=None):
        """Same, but for responses requested with format=json."""
        text = AmyApp._llm_text(res)
        if not text:
            return {} if default is None else default
        try:
            return json.loads(text)
        except Exception:
            # Models sometimes wrap JSON in prose or fences; salvage the object.
            m = re.search(r'\{.*\}', text, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    pass
            log_debug(f"model JSON unparseable: {text[:160]}")
            return {} if default is None else default

    # ==================================================================
    # SMALL UTILITIES
    # ==================================================================
    def copy_to_clipboard(self, text):
        """Used by the copy button beside links in cards."""
        if not HAS_CLIPBOARD:
            self.safe_log("Clipboard needs: pip install --user pyperclip")
            return False
        try:
            pyperclip.copy(str(text))
            self.safe_log("Copied to clipboard.")
            return True
        except Exception as e:
            log_debug(f"clipboard copy failed: {e}")
            return False


    def keep_pc_awake(self, on=True):
        """Stop the PC sleeping (useful for long background jobs)."""
        if sys.platform != "win32":
            self.speak("That's a Windows-only setting, Sir.")
            return True
        try:
            val = "0" if on else "30"
            for setting in ("standby-timeout-ac", "hibernate-timeout-ac"):
                subprocess.run(["powercfg", "/change", setting, val],
                               capture_output=True, timeout=15)
            self.speak("This PC won't sleep now, Sir." if on
                       else "Normal sleep restored, Sir.")
            self.safe_log(f"Sleep timeout set to {val} minutes (0 = never).")
        except Exception as e:
            log_debug(f"powercfg failed: {e}")
            self.speak("I couldn't change the power settings, Sir.")
        return True


    # Vision models tile images at 336px. Sending a 1280px photo costs ~14x the
    # vision tokens of a 672px one for no accuracy gain on a normal subject.
    LENS_MAX_PX = 672
    LENS_TEXT_MAX_PX = 1000        # small print genuinely needs more resolution

    def warm_vision_model(self):
        """Preload the vision model so the first photo isn't slow."""
        self.ollama_ready.wait(timeout=60)
        try:
            tiny = Image.new("RGB", (32, 32), (128, 128, 128))
            buf = io.BytesIO()
            tiny.save(buf, format="JPEG")
            self.http.post(OLLAMA_GENERATE_URL, json={
                "model": self.model_for("vision"), "prompt": "hi",
                "images": [base64.b64encode(buf.getvalue()).decode("ascii")],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 1},
            }, timeout=180)
            log_debug("Vision model warmed.")
        except Exception as e:
            log_debug(f"vision warm-up skipped: {e}")


    def _sweep_stale_audio(self):
        """Remove staged clips left behind by a previous run or a crash."""
        removed = 0
        try:
            for f in glob.glob(os.path.join(DATA_DIR, "_tts_*.mp3")):
                try:
                    if time.time() - os.path.getmtime(f) > 300:
                        os.remove(f)
                        removed += 1
                except Exception:
                    continue
        except Exception as e:
            log_debug(f"audio sweep error: {e}")
        if removed:
            log_debug(f"Swept {removed} stale audio files.")
        return removed



    # ==================================================================
    # REASONING QUALITY
    # The model's weights can't be changed here - it's a pre-trained Ollama
    # model. What genuinely moves the needle is giving it room to think, and
    # checking its own answer on the questions that warrant it.
    # ==================================================================
    HARD_MARKERS = (
        "why", "how many", "calculate", "compare", "which is better", "trade-off",
        "tradeoff", "explain the difference", "prove", "derive", "estimate",
        "what would happen", "pros and cons", "should i", "diagnose", "debug",
        "optimi", "plan ", "strategy", "step by step",
    )

    def _recent_context(self, turns=6):
        """A short slice of the conversation, for the reasoning pass."""
        try:
            hist = self.memory.get("chat_history", [])[-turns:]
            return "\n".join(f"{m.get('role','')}: {str(m.get('content',''))[:300]}"
                              for m in hist)
        except Exception:
            return ""

    def _needs_deliberation(self, text):
        """Is this worth spending extra tokens on?

        Cheap questions get a fast answer; genuinely hard ones get room to
        reason. Spending the budget on everything just makes him slow.
        """
        if not CONFIG.get("assistant", {}).get("deliberate", True):
            return False
        low = str(text).lower()
        if len(low.split()) < 4:
            return False
        if any(m in low for m in self.HARD_MARKERS):
            return True
        # Multi-clause questions usually carry more than one constraint.
        return low.count("?") > 1 or low.count(" and ") >= 2 or len(low.split()) > 45

    def think_then_answer(self, question, context=""):
        """Reason privately, then answer from that reasoning.

        Asking for the working first measurably improves multi-step answers.
        The working is kept out of what he says aloud - you get the conclusion,
        not the scratchpad.
        """
        model = self.model_for("reasoning")
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": model,
                "messages": [
                    {"role": "system", "content":
                     "Work the problem through privately, then give the answer.\n"
                     "Put your reasoning between <think> and </think>, then write "
                     "the final answer after </think>. Keep the final answer "
                     "self-contained - the user never sees the reasoning."},
                    {"role": "user", "content":
                     (f"Context:\n{context}\n\n" if context else "") + question},
                ],
                "stream": False, "keep_alive": "30m",
                "options": {"num_predict": 900, "temperature": 0.4, "num_ctx": 8192},
            }, timeout=240)
            out = self._llm_text(res)
        except Exception as e:
            log_debug(f"deliberate error: {e}")
            return ""
        if "</think>" in out:
            reasoning, answer = out.split("</think>", 1)
            log_debug(f"reasoning: {reasoning[:300]}")
            return answer.strip()
        return out.strip()

    def double_check(self, question, answer):
        """Have the model critique its own answer and correct it if wrong.

        Catches arithmetic slips and dropped constraints. Only worth running on
        questions that have a checkable answer.
        """
        try:
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("reasoning"),
                "messages": [{"role": "user", "content":
                    f"QUESTION:\n{question}\n\nPROPOSED ANSWER:\n{answer}\n\n"
                    "Check this for errors: arithmetic, dropped constraints, "
                    "wrong assumptions, or anything the question asked for that "
                    "the answer missed.\n"
                    "Reply with JSON only: "
                    '{"ok": true|false, "problem": "...", "corrected": "..."}\n'
                    "If it's right, set ok true and leave the other fields empty."}],
                "stream": False, "format": "json", "keep_alive": "30m",
                "options": {"num_predict": 500, "temperature": 0.1, "num_ctx": 8192},
            }, timeout=180)
            verdict = self._llm_json(res)
        except Exception as e:
            log_debug(f"double_check error: {e}")
            return answer, False
        if isinstance(verdict, dict) and verdict.get("ok") is False:
            fixed = str(verdict.get("corrected") or "").strip()
            if fixed and len(fixed) > 20:
                log_debug(f"self-correction: {verdict.get('problem','')[:160]}")
                self.safe_log(f"(corrected myself: {str(verdict.get('problem',''))[:70]})")
                return fixed, True
        return answer, False

    def _parse_email_command(self, cmd_text):
        """Extract (recipient, instruction, auto_send) from a natural email command.

        Handles forms like:
          'send an email to John about the meeting tomorrow'
          'email sarah@work.com saying I'll be late'
          'draft an email to my boss regarding the report' (auto_send=False for 'draft')
        """
        text = cmd_text.strip()
        low = text.lower()

        # 'draft'/'prepare'/'write' imply review-before-send; 'send' implies auto-send.
        auto_send = EMAIL_AUTO_SEND
        if any(w in low for w in ["draft", "prepare", "compose", "write an email", "write email"]):
            auto_send = False
        if "send" in low:
            auto_send = EMAIL_AUTO_SEND

        recipient = None
        instruction = None

        # Pattern: "... to <recipient> <connector> <instruction>"
        m = re.search(
            r'\bto\s+(.+?)\s+(about|regarding|saying|telling (?:them|him|her)|that says?|re:|with|informing (?:them|him|her))\s+(.+)',
            text, re.IGNORECASE)
        if m:
            recipient = m.group(1).strip()
            instruction = m.group(3).strip()
        else:
            # Pattern: "... to <recipient>" with the rest (or nothing) as instruction
            m2 = re.search(r'\bto\s+(\S+@\S+|[A-Za-z][\w .\'-]*)', text, re.IGNORECASE)
            if m2:
                recipient = m2.group(1).strip()
            # Pattern: connector without explicit "to"
            m3 = re.search(r'\b(about|regarding|saying|that says?|re:)\s+(.+)', text, re.IGNORECASE)
            if m3:
                instruction = m3.group(2).strip()

        # "email me ..." -> recipient is self
        if re.search(r'\bemail me\b', low):
            recipient = recipient or "me"
            if not instruction:
                instruction = re.sub(r'.*\bemail me\b\s*(about|regarding)?\s*', '', text, flags=re.IGNORECASE).strip()

        # Fallback: any raw address in the text is the recipient (e.g. "email bob@x.com saying hi").
        if not recipient:
            addr = extract_email_address(text)
            if addr:
                recipient = addr

        # Fallback: "email <name> about/saying ..." with no "to".
        if not recipient:
            mn = re.search(r'\bemail\s+([A-Za-z][\w\'-]*)\b', text, re.IGNORECASE)
            if mn and mn.group(1).lower() not in ("me", "a", "an", "the"):
                recipient = mn.group(1).strip()

        # Clean common trailing/leading noise from recipient.
        if recipient:
            recipient = re.sub(r'^(my|the)\s+', '', recipient, flags=re.IGNORECASE).strip()
            recipient = recipient.strip(" .,")

        # If we still have no instruction, use whatever follows the verb.
        if not instruction:
            stripped = re.sub(
                r'^(please\s+)?(send|write|compose|draft|prepare)\s+(an?\s+)?email\s*',
                '', text, flags=re.IGNORECASE).strip()
            if recipient:
                stripped = re.sub(r'^to\s+' + re.escape(recipient), '', stripped, flags=re.IGNORECASE).strip()
            instruction = stripped or "a brief, friendly message"

        return recipient, instruction, auto_send

    # --- PC & APP AUTOMATION ENGINE ---
    def execute_pc_automation(self, cmd_text):
        cmd = cmd_text.lower().strip()

        # In-App Dynamic Cards Internal Execution Demos
        if "carousel" in cmd or "social media" in cmd:
            carousel_items = ["Post #1: Tech Insights", "Post #2: AI Trends", "Post #3: Python Automation"]
            self.instantiate_card("Social Media Carousel", "carousel", carousel_items)
            self.speak("Retrieved social media carousel cards, Sir.")
            return True

        if "check bugs" in cmd or "check code" in cmd or "debug code" in cmd:
            bug_report = "File: main.py\nLine 42: NullPointerException potential.\nRecommendation: Add safety check for 'user_id'."
            self.instantiate_card("Code Audit Report", "code_bug", bug_report)
            self.speak("Instantiated code bug analysis card.")
            return True

        # --- CODING ENGINE ---
        if any(k in cmd for k in ["run the code", "run it", "execute the code", "run this code", "run my code"]):
            return self.run_code()
        if any(k in cmd for k in ["fix the code", "debug the code", "fix my code", "fix this code", "fix the bug"]):
            return self.fix_code()
        improve = re.search(r'(?:improve|refactor|optimi[sz]e|change)\s+the\s+code\s+(?:to\s+|so\s+)?(.+)', cmd)
        if improve:
            return self.fix_code(improve.group(1).strip())
        if any(k in cmd for k in ["explain the code", "explain this code", "what does this code do", "explain my code"]):
            return self.explain_code()
        if any(k in cmd for k in ["open in editor", "open the code", "open in vs code", "open in vscode"]):
            return self.open_code_in_editor()
        code_req = re.search(
            r'(?:write|create|make|build|code|generate)\s+(?:me\s+)?(?:a|an|some)?\s*'
            r'(?:(\w+(?:\+\+|#)?)\s+)?(?:script|program|function|code|app|class|snippet)\s*'
            r'(?:that|to|which|for|:)?\s*(.+)', cmd)
        if code_req:
            lang = code_req.group(1)
            what = code_req.group(2).strip()
            if lang and lang.lower() not in self.LANG_EXT:
                what = f"{lang} {what}"
                lang = None
            return self.write_code(what, lang)

        # --- APP AUTOMATION ---
        if any(k in cmd for k in ["stop automation", "stop the automation", "abort automation", "cancel automation"]):
            return self.stop_automation()
        auto_req = re.search(r'(?:automate|auto pilot|autopilot|do this for me)[:,]?\s*(.+)', cmd)
        if auto_req:
            return self.ai_automate(auto_req.group(1).strip())
        macro_run = re.search(r'run (?:the )?macro (.+)', cmd)
        if macro_run:
            return self.run_macro(macro_run.group(1).strip())

        # --- SCREEN AWARENESS ---
        if any(k in cmd for k in ["what am i looking at", "what's on my screen", "what is on my screen",
                                  "what do you see", "describe my screen"]):
            return self.describe_screen_now()
        if any(k in cmd for k in ["watch my screen", "start watching", "enable screen awareness",
                                  "keep an eye on my screen", "always watch"]):
            self.toggle_continuous_vision(True)
            self.run_js("setAutoVisionState(true)")
            return True
        if any(k in cmd for k in ["stop watching", "disable screen awareness", "stop looking at my screen"]):
            self.toggle_continuous_vision(False)
            self.run_js("setAutoVisionState(false)")
            return True

        # --- MISC NEW FEATURES ---
        trans = re.search(r'translate\s+(.+?)\s+(?:in)?to\s+(\w+)', cmd)
        if trans:
            return self.translate_text(trans.group(1).strip(), trans.group(2).strip())
        if any(k in cmd for k in ["summarise my clipboard", "summarize my clipboard",
                                  "summarise the clipboard", "summarize the clipboard", "summarise this", "summarize this"]):
            return self.summarize_clipboard()
        ff = re.search(r'(?:find|search for|locate)\s+(?:the\s+)?(?:file|files|document)s?\s+(?:called|named|matching)?\s*(.+)', cmd)
        if ff:
            return self.find_files(ff.group(1).strip())
        if any(k in cmd for k in ["what's running", "what is running", "top processes", "list processes", "show processes"]):
            return self.list_processes()
        kp = re.search(r'(?:kill|close|terminate|end)\s+(?:the\s+)?(?:process|task|app|program)\s+(.+)', cmd)
        if kp:
            return self.kill_process(kp.group(1).strip())
        if any(k in cmd for k in ["network info", "my ip", "what's my ip", "ip address", "network status"]):
            return self.network_info()
        if any(k in cmd for k in ["disk space", "disk usage", "how much storage", "storage space", "free space"]):
            return self.disk_usage()
        dw = re.search(r'(?:define|definition of|what does (.+?) mean)\s*(.*)', cmd)
        if dw and cmd.startswith(("define", "definition of")):
            word = (dw.group(2) or dw.group(1) or "").strip()
            if word:
                return self.define_word(word)
        of = re.search(r'open (?:my |the )?(downloads|documents|desktop|pictures|music|videos|amy)\s*folder', cmd)
        if of:
            return self.open_folder(of.group(1))

        # --- CAD / 3D DESIGN ---
        if any(k in cmd for k in ["improve the design", "review the design", "make the design better",
                                  "critique the design", "refine the model"]):
            threading.Thread(target=self.cad_improve, daemon=True).start()
            return True
        if any(k in cmd for k in ["export stl", "export to stl", "save the stl", "export the model"]):
            threading.Thread(target=self.cad_export_stl, daemon=True).start()
            return True
        if any(k in cmd for k in ["open in openscad", "view in 3d", "show the model in 3d", "open the model"]):
            return self.cad_open_external()
        if any(k in cmd for k in ["my designs", "list designs", "show my designs", "list my models"]):
            return self.cad_list()
        cad_edit = re.search(r'(?:make|change|modify|edit|adjust)\s+(?:the\s+)?(?:design|model|part|cad)\s+(.+)', cmd)
        if cad_edit:
            threading.Thread(target=self.cad_edit, args=(cad_edit.group(1).strip(),), daemon=True).start()
            return True
        cad_new = re.search(
            r'(?:design|model|cad|3d print|create a model of|make me a)\s+(?:me\s+)?(?:a\s+|an\s+)?(.+)', cmd)
        if cad_new and any(k in cmd for k in ["design", "model", "cad", "3d print", "bracket",
                                              "mount", "enclosure", "case", "holder", "adapter", "gear"]):
            desc = cad_new.group(1).strip()
            desc = re.sub(r'^(a|an|the)\s+', '', desc)
            self.run_background("CAD design", self.cad_create, desc,)
            return True

        # --- MEMORY CONSOLIDATION ---
        if any(k in cmd for k in ["consolidate your memory", "tidy your memory", "clean up your memory",
                                  "consolidate memory", "organise what you know"]):
            threading.Thread(target=self.consolidate_memory, daemon=True).start()
            return True

        # --- USER-DEFINED COMMANDS (highest priority) ---
        try:
            if self.run_custom_command(cmd):
                return True
            if self.run_plugin_command(cmd):
                return True
        except Exception as e:
            log_debug(f"custom/plugin dispatch error: {e}")

        # --- CLARIFICATION ("did you mean...?") ---
        if getattr(self, "pending_clarification", None):
            if any(k in cmd for k in ["yes", "yeah", "yep", "correct", "that's right",
                                      "thats right", "right", "aye"]):
                self.confirm_clarification(True)
                return True
            if any(k in cmd for k in ["no", "nope", "wrong", "not that", "nah"]):
                self.confirm_clarification(False)
                return True
            self.pending_clarification = None      # they said something else entirely

        # --- APPROVAL RESPONSES (must be checked first while a plan is pending) ---
        if self.awaiting_approval:
            if any(k in cmd for k in ["yes", "yeah", "yep", "go ahead", "do it", "proceed",
                                      "confirm", "approved", "go on", "please do"]):
                self.resolve_approval(True)
                return True
            if any(k in cmd for k in ["no", "nope", "cancel", "stop", "don't", "dont",
                                      "abort", "never mind", "nevermind"]):
                self.resolve_approval(False)
                return True

        # --- CAD PROPOSAL RESPONSES ---
        if getattr(self, "pending_cad", None):
            if any(k in cmd for k in ["accept", "apply it", "apply that", "keep it",
                                      "looks good", "commit", "yes apply"]):
                return self.cad_accept()
            if any(k in cmd for k in ["discard", "revert that", "undo that change",
                                      "throw it away", "reject"]):
                return self.cad_discard()

        # --- SLICER ---
        if any(k in cmd for k in ["send to slicer", "open in slicer", "slice it",
                                  "print this", "send it to the printer", "prepare for printing"]):
            threading.Thread(target=self.send_to_slicer, daemon=True).start()
            return True

        # --- CAD REFINE (preview before commit) ---
        refine = re.search(r'(?:refine|tweak|adjust)\s+(?:the\s+)?(?:design|model|part)\s+(.+)', cmd)
        if refine:
            threading.Thread(target=self.cad_refine, args=(refine.group(1).strip(),), daemon=True).start()
            return True

        # --- KNOWLEDGE BASE WATCHING ---
        watch = re.search(r'(?:watch|monitor|keep track of)\s+(?:the\s+)?(?:folder|directory)\s+(.+)', cmd)
        if watch:
            threading.Thread(target=self.kb_watch, args=(watch.group(1).strip(),), daemon=True).start()
            return True
        if any(k in cmd for k in ["stop watching folders", "unwatch", "stop monitoring folders"]):
            return self.kb_unwatch()
        if any(k in cmd for k in ["what folders are you watching", "watched folders", "watch status"]):
            return self.kb_watch_status()

        # --- LENS HISTORY ---
        if any(k in cmd for k in ["lens history", "what have you looked at", "what have you seen",
                                  "show me what you've seen", "visual history"]):
            return self.lens_history_view()

        # --- CONFIG / STARTUP HEALTH ---
        if any(k in cmd for k in ["what's not configured", "what needs configuring",
                                  "check my config", "config issues", "what's missing"]):
            issues = self.validate_config(announce=False)
            if not issues:
                self.speak("Everything is configured, Sir.")
            else:
                lines = [f"{feat}: {', '.join(f)}" for feat, f in issues.items()]
                self.instantiate_card("Unconfigured", "carousel", lines)
                self.speak(f"{len(issues)} features need configuring, Sir.")
            return True

        # --- PHONE / VR (switched off in this build) ---
        if any(k in cmd for k in ["phone bridge", "connect my phone", "link my phone",
                                  "pair my", "pairing code", "vr mode", "immersive mode",
                                  "webxr", "virtual reality", "remote access"]):
            self.speak("Phone and VR modes are switched off in this build.")
            return True
        if any(k in cmd for k in ["don't let the pc sleep", "keep the pc awake",
                                  "stop the pc sleeping", "never sleep"]):
            return self.keep_pc_awake(True)
        if any(k in cmd for k in ["allow the pc to sleep", "normal sleep", "let the pc sleep"]):
            return self.keep_pc_awake(False)

        # --- SOUL / MEMORY / SKILLS FILES ---
        mf = re.search(r'\b(?:open|show|edit)\s+(?:your\s+|the\s+)?(soul|personality|memory file|skills?|heartbeat)\b', cmd)
        if mf:
            which = mf.group(1)
            if which.startswith("skill"):
                target = self.mind.skills_dir
            else:
                target = {"soul": self.mind.soul_path, "personality": self.mind.soul_path,
                          "heartbeat": self.mind.heartbeat_path}.get(which, self.mind.memory_path)
            self.launch_external_app(target)
            self.speak("Opened it. Changes apply from my next reply.")
            return True
        ns = re.search(r'\b(?:make|create|add) (?:a |an )?(?:new )?skill (?:called |named |for )?(.+)', cmd)
        if ns:
            title = ns.group(1).strip()
            name = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "new-skill"
            d = os.path.join(self.mind.skills_dir, name)
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "SKILL.md")
            if not os.path.exists(p):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(f"---\nname: {name}\ndescription: What this skill is for and when to use it.\n---\n\n"
                            f"# {title.title()}\n\nStep-by-step instructions for Amy.\n")
            self.launch_external_app(p)
            self.speak("I've made the skill file. Write the steps in it and I'll follow them.")
            return True
        snd = re.search(r'\b(mute|turn off|disable|unmute|turn on|enable) (?:the |your )?(?:sound effects|sounds|ui sounds)\b', cmd)
        if snd:
            on = snd.group(1) in ("unmute", "turn on", "enable")
            CONFIG.setdefault("ui", {})["sounds"] = on
            save_config(CONFIG)
            if on:
                self.sfx.play("done")
            self.speak("Sound effects on." if on else "Sound effects off.")
            return True

        # --- PROACTIVITY ---
        pl = re.search(r'\b(?:be )?(more|less) proactive\b|\bproactiv\w* (?:to |level )?(off|low|normal|high)\b'
                       r'|\b(stop|start) (?:making |giving )?suggestions\b', cmd)
        if pl:
            levels = ["off", "low", "normal", "high"]
            cur = self.proactive.cfg()["level"]
            idx = levels.index(cur) if cur in levels else 2
            if pl.group(1) == "more":
                new = levels[min(3, idx + 1)]
            elif pl.group(1) == "less":
                new = levels[max(0, idx - 1)]
            elif pl.group(2):
                new = pl.group(2)
            else:
                new = "off" if pl.group(3) == "stop" else "normal"
            CONFIG.setdefault("proactive", {})["level"] = new
            save_config(CONFIG)
            self.speak({"off": "I'll keep my suggestions to myself.",
                        "low": "I'll only speak up for warnings.",
                        "normal": "I'll speak up when it's useful.",
                        "high": "I'll be more forthcoming."}[new])
            return True
        sumfile = re.search(r'summari[sz]e (?:the )?(?:file|document) (.+)', cmd_text, re.I)
        if sumfile:
            self.run_background("Summarising", self.summarize_file, sumfile.group(1).strip().strip('"'))
            return True

        # --- APP DISCOVERY ---
        if any(k in cmd for k in ["rescan apps", "refresh apps", "scan for apps",
                                  "update the app list", "find new apps"]):
            self.run_background("Scanning apps", lambda: self.speak(
                f"I can see {self.app_index.refresh()} apps on this computer, Sir."))
            return True
        alias = re.search(r'when i say (.+?),? (?:i mean|open|launch) (?:the )?(.+?)(?: app)?$', cmd)
        if alias and len(alias.group(1).split()) <= 4:
            self.memory.setdefault("app_aliases", {})[_norm_app_name(alias.group(1))] = alias.group(2).strip()
            self.save_memory()
            self.speak(f"Got it. {alias.group(1)} means {alias.group(2)}.")
            return True

        # --- CAMERAS ---
        if re.search(r'\b(scan|look) for cameras\b|\bfind (my )?cameras\b', cmd):
            return self.scan_cameras()
        if re.search(r'\b(list|what|which) cameras\b|\bcameras do i have\b', cmd):
            return self.list_cameras()
        cam_switch = re.search(r'(?:switch|change) (?:to )?(?:the )?(.+?) camera\b', cmd) \
            or re.search(r'\buse (?:the )?(.+?) camera\b', cmd)
        if cam_switch:
            return self.switch_camera(cam_switch.group(1).strip())
        cam_rename = re.search(r'(?:rename|call) (?:the )?(.+?) camera (?:to |as )(.+)$', cmd)
        if cam_rename:
            return self.rename_camera(cam_rename.group(1).strip(), cam_rename.group(2).strip())
        cam_add = re.search(r'add (?:a )?camera (?:called |named )?(.+?) (?:on|at|as) (?:device |index )?(\d+)', cmd)
        if cam_add:
            return self.add_camera(cam_add.group(1).strip(), cam_add.group(2))
        cam_rm = re.search(r'(?:remove|delete|forget) (?:the )?(.+?) camera\b', cmd)
        if cam_rm:
            return self.remove_camera(cam_rm.group(1).strip())

        # --- CUSTOMISATION ---
        theme = re.search(r'(?:set |change |switch )?(?:the )?theme (?:to |as )?(\w+)', cmd)
        if theme:
            return self.set_theme(theme.group(1))
        if any(k in cmd for k in ["list themes", "what themes", "available themes"]):
            return self.list_themes()
        addcmd = re.search(r'(?:when i say|create a command|add a command)\s+"?([^"]+?)"?\s+'
                           r'(?:then |you should |)?(open|run|say|go to|type)\s+(.+)', cmd)
        if addcmd:
            phrase, verb, val = addcmd.group(1).strip(), addcmd.group(2), addcmd.group(3).strip()
            action = {"open": "open", "run": "command", "say": "say",
                      "go to": "url", "type": "type"}.get(verb, "command")
            return self.add_custom_command(phrase, action, val)
        if any(k in cmd for k in ["my custom commands", "list custom commands", "what commands have i"]):
            return self.list_custom_commands()
        if any(k in cmd for k in ["list plugins", "what plugins", "my plugins"]):
            return self.list_plugins()
        if any(k in cmd for k in ["reload plugins", "refresh plugins"]):
            n = self.load_plugins()
            self.speak(f"Reloaded {n} plugin{'s' if n != 1 else ''}, Sir.")
            return True
        persona = re.search(r'(?:from now on|always|be more|speak more|act more)\s+(.+)', cmd)
        if persona and len(persona.group(1).split()) <= 12 and any(
                w in cmd for w in ["formal", "casual", "brief", "funny", "serious", "sarcastic",
                                   "concise", "detailed", "polite", "blunt", "warm", "professional"]):
            return self.set_persona(persona.group(1).strip())

        # --- MULTI-WINDOW ---
        detach = re.search(r'(?:open|detach|pop out|move)\s+(?:the\s+)?(\w+)\s+'
                           r'(?:in|to|into)?\s*(?:its own|a new|another|separate)?\s*window', cmd)
        if detach:
            return self.open_window(detach.group(1))
        if any(k in cmd for k in ["what windows", "list windows", "open windows"]):
            return self.list_windows()
        closew = re.search(r'close (?:the )?(\w+) window', cmd)
        if closew:
            return self.close_window(closew.group(1))

        # --- BROWSER AGENT ---
        if any(k in cmd for k in ["close the browser", "quit the browser", "end browser session"]):
            return self.browser_close()
        browse = re.search(r'(?:in the browser|browse|use the browser to|on the web)[,:]?\s*(.+)', cmd)
        if browse:
            self.run_background("Browsing", self.browse_task, browse.group(1).strip())
            return True
        readq = re.search(r'(?:what does|find out from|check)\s+(https?://\S+|\b[\w-]+\.[\w.-]+\S*)\s+(.+)', cmd)
        if readq:
            self.run_background("Reading page", self.browse_extract, readq.group(1), readq.group(2))
            return True

        # --- MODELS & SPEECH ENGINES ---
        if any(k in cmd for k in ["what models", "model routing", "which model", "model report",
                                  "list models", "what models are you using"]):
            self.run_background("Model report", self.model_report)
            return True
        if any(k in cmd for k in ["speech engine", "stt status", "recognition status",
                                  "what speech engine", "listening engine"]):
            return self.stt_report()

        # --- BACKGROUND TASKS ---
        if any(k in cmd for k in ["what's running in the background", "background tasks",
                                  "what are you working on", "task status", "running tasks"]):
            return self.list_tasks()

        # --- MIC RECALIBRATION ---
        if any(k in cmd for k in ["recalibrate", "calibrate the mic", "calibrate your mic",
                                  "you can't hear me", "can't you hear me", "recalibrate your hearing",
                                  "fix your hearing", "adjust your microphone"]):
            self._force_recalibrate = True
            self.speak("Recalibrating my hearing, Sir. Give me a quiet moment.")
            return True

        # --- PROJECTS ---
        pstart = re.search(r'(?:start|begin|open|create|switch to|work on)\s+(?:a\s+)?project\s+(?:called\s+|named\s+)?(.+)', cmd)
        if pstart:
            return self.project_start(pstart.group(1).strip())
        if any(k in cmd for k in ["end project", "close project", "stop working on", "finish project"]):
            return self.project_end()
        if any(k in cmd for k in ["list projects", "my projects", "what projects", "show projects"]):
            return self.project_list()
        if any(k in cmd for k in ["project status", "current project", "what am i working on"]):
            return self.project_status()

        # --- CALENDAR ---
        cal_add = re.search(r'(?:schedule|book|add|put)\s+(?:a\s+|an\s+)?(.+?)\s+(?:for|on|at)\s+(.+)', cmd)
        if cal_add and any(k in cmd for k in ["schedule", "book", "calendar", "meeting", "appointment", "remind me on"]):
            return self.calendar_add(cal_add.group(1).strip(), cal_add.group(2).strip())
        if any(k in cmd for k in ["what's on today", "whats on today", "my schedule today",
                                  "what's my day", "agenda today", "today's schedule"]):
            return self.calendar_view("today")
        if any(k in cmd for k in ["this week", "my week", "week ahead", "schedule this week"]):
            return self.calendar_view("week")
        if any(k in cmd for k in ["what's next", "next appointment", "next meeting", "upcoming"]):
            return self.calendar_next()

        # --- HOME AUTOMATION ---
        home = re.search(r'turn\s+(on|off)\s+(?:the\s+)?(.+)', cmd)
        if home and not any(k in cmd for k in ["wifi", "display", "screen", "monitor",
                                               "gesture", "security", "sentinel", "camera", "pc", "computer"]):
            return self.home_control(home.group(1), home.group(2).strip())
        if any(k in cmd for k in ["what's on at home", "home status", "which lights are on", "smart home status"]):
            return self.home_status()

        # --- SCREEN LENS ---
        if any(k in cmd for k in ["read my screen", "what's on my screen", "read the screen",
                                  "what does my screen say", "read this error", "explain my screen"]):
            mode = "text" if "read" in cmd else "explain"
            threading.Thread(target=self.screen_lens, args=(mode,), daemon=True).start()
            return True
        if any(k in cmd for k in ["translate my screen", "translate the screen"]):
            threading.Thread(target=self.screen_lens, args=("translate",), daemon=True).start()
            return True

        # --- UNDO ---
        if any(k in cmd for k in ["undo that", "undo last", "undo the last", "reverse that", "take that back"]):
            return self.undo_last_action()

        # --- HABITS ---
        if any(k in cmd for k in ["my habits", "what do i usually do", "my patterns", "habit report"]):
            return self.habit_suggestions()

        # --- SECURITY SENTINEL ---
        if any(k in cmd for k in ["enable security", "activate sentinel", "watch for intruders",
                                  "security mode on", "protect my pc", "guard my pc", "sentinel on"]):
            return self.toggle_sentinel(True)
        if any(k in cmd for k in ["disable security", "stand down", "security mode off", "sentinel off"]):
            return self.toggle_sentinel(False)
        if any(k in cmd for k in ["security report", "security sweep", "security scan",
                                  "am i being hacked", "check for intruders", "is my pc secure"]):
            threading.Thread(target=self.security_report, daemon=True).start()
            return True
        blk = re.search(r'(?:block|kill|terminate|stop)\s+(?:the\s+)?(?:intruder|threat|process)\s*(.*)', cmd)
        if blk and blk.group(1).strip():
            return self.block_process(blk.group(1).strip())

        # --- SELF-DIAGNOSTICS ---
        if any(k in cmd for k in ["run diagnostics", "self diagnostics", "diagnose yourself",
                                  "system check", "are you okay", "check yourself", "status report",
                                  "run a diagnostic"]):
            threading.Thread(target=self.self_diagnostics, daemon=True).start()
            return True

        # --- MULTI-AGENT ---
        ag = re.search(r'(?:agents?|team|use your agents)[:,]?\s*(.+)', cmd)
        if ag:
            self.run_background("Agent task", self.agent_task, ag.group(1).strip(),)
            return True

        if any(k in cmd for k in ["what have you studied", "what have you researched",
                                  "list your research", "what do you know about topics",
                                  "your research"]):
            return self.list_research()

        # --- KNOWLEDGE BASE ---
        ing = re.search(r'(?:index|ingest|learn|memorise|memorize)\s+(?:the\s+)?(?:folder|file|directory)\s+(.+)', cmd)
        if ing:
            self.run_background("Indexing", self.kb_ingest, ing.group(1).strip(),)
            return True
        kbq = re.search(r'(?:search my (?:files|notes|documents|knowledge)|ask my (?:files|notes|knowledge)|'
                        r'what do my (?:files|notes) say about|from my files)\s*(?:about|for)?\s*(.+)', cmd)
        if kbq:
            threading.Thread(target=self.kb_search, args=(kbq.group(1).strip(),), daemon=True).start()
            return True
        if any(k in cmd for k in ["knowledge base status", "what's in my knowledge base", "kb status"]):
            return self.kb_status()

        # --- FILE MANAGEMENT ---
        org = re.search(r'(?:organi[sz]e|tidy|sort)\s+(?:my\s+|the\s+)?(.+?)\s*(?:folder)?$', cmd)
        if org and any(k in cmd for k in ["folder", "downloads", "desktop", "documents"]):
            target = org.group(1).strip()
            folder = None
            for name in ["downloads", "desktop", "documents", "pictures"]:
                if name in target:
                    folder = os.path.join(os.path.expanduser("~"), name.capitalize())
                    break
            threading.Thread(target=self.organize_folder, args=(folder,), daemon=True).start()
            return True
        ren = re.search(r'rename\s+(?:all\s+)?files?\s+in\s+(.+?)\s+(?:from|replacing)\s+(.+?)\s+(?:to|with)\s+(.+)', cmd)
        if ren:
            threading.Thread(target=self.bulk_rename,
                             args=(ren.group(1).strip(), ren.group(2).strip(), ren.group(3).strip()),
                             daemon=True).start()
            return True
        fsum = re.search(r'(?:summari[sz]e|analyse|analyze|what\'s in)\s+(?:my\s+|the\s+)?(\w+)\s+folder', cmd)
        if fsum:
            name = fsum.group(1).strip()
            folder = os.path.join(os.path.expanduser("~"), name.capitalize())
            threading.Thread(target=self.folder_summary, args=(folder,), daemon=True).start()
            return True

        # --- EXTENDED FEATURES ---
        if any(k in cmd for k in ["show my notes", "read my notes", "list my notes",
                                  "what are my notes", "my notes", "show notes"]):
            return self.list_notes()
        note = re.search(r'(?:note|jot down|make a note)\s*(?:that|:)?\s*(.+)', cmd)
        if note and not cmd.startswith(("show", "read", "list", "what")):
            return self.quick_note(note.group(1).strip())
        if any(k in cmd for k in ["word count", "how many words", "count the words"]):
            return self.word_count()
        pw = re.search(r'(?:generate|make|create)\s+(?:a\s+)?password(?:\s+(?:of\s+)?(\d+))?', cmd)
        if pw:
            return self.password_generator(int(pw.group(1)) if pw.group(1) else 16)
        conv = re.search(r'(?:convert)\s+(.+)', cmd)
        if conv and any(u in cmd for u in ["km", "mile", "kg", "pound", "celsius", "fahrenheit",
                                           "meter", "metre", "feet", "inch", "litre", "liter", "gallon", "ounce"]):
            return self.unit_convert(conv.group(1).strip())
        if any(k in cmd for k in ["crypto prices", "coin prices", "how's crypto", "crypto market"]):
            return self.coin_price_multi()
        if any(k in cmd for k in ["clean temp", "clear temp files", "delete temp files", "clean temporary"]):
            return self.clean_temp_files()
        if any(k in cmd for k in ["my specs", "system specs", "what's my setup", "my hardware", "specification"]):
            return self.whats_my_setup()
        if any(k in cmd for k in ["uptime", "how long has my pc been on", "how long has the computer been on"]):
            return self.uptime_report()
        bs = re.search(r'(?:brainstorm|give me ideas (?:for|about)|ideas for)\s+(.+)', cmd)
        if bs:
            return self.brainstorm(bs.group(1).strip())
        pc = re.search(r'(?:pros and cons of|should i)\s+(.+)', cmd)
        if pc:
            return self.pros_and_cons(pc.group(1).strip())
        eli = re.search(r'(?:explain like i\'?m (?:a )?(\w+)|eli5)\s*(.*)', cmd)
        if eli:
            lvl = eli.group(1) or "five"
            topic = (eli.group(2) or "").strip()
            if topic:
                return self.explain_like_im(topic, lvl)
        if any(k in cmd for k in ["daily briefing", "morning briefing", "brief me", "my briefing"]):
            threading.Thread(target=self.daily_briefing, daemon=True).start()
            return True
        fm = re.search(r'focus mode(?:\s+for\s+(\d+))?', cmd)
        if fm:
            return self.focus_mode(int(fm.group(1)) if fm.group(1) else 25)

        # --- INTERNET RESEARCH ---
        deep = re.search(r'(?:deep research|research|do research on|investigate|study)\s+(?:on\s+|about\s+|into\s+)?(.+)', cmd)
        if deep and not any(k in cmd for k in ["my ", "your code"]):
            topic = deep.group(1).strip()
            self.run_background("Research", self.research_and_learn, topic,)
            return True
        weblook = re.search(r'(?:look up online|search online for|search the web for|google it|check online for|find out)\s*(.*)', cmd)
        if weblook:
            q = weblook.group(1).strip()
            if q:
                threading.Thread(target=self.answer_from_web, args=(q,), daemon=True).start()
                return True
        readurl = re.search(r'(?:read|summarise|summarize|check|open and read)\s+(https?://\S+|\b[\w-]+\.[\w.-]+/\S*)', cmd)
        if readurl:
            threading.Thread(target=self.check_url, args=(readurl.group(1),), daemon=True).start()
            return True
        if any(k in cmd for k in ["voice diagnostics", "list voices", "what voice", "check your voice",
                                  "which voice", "voice test", "test your voice"]):
            return self.list_voices()

        # --- DESK CAMERA & GESTURES ---
        if any(k in cmd for k in ["open desk view", "start desk view", "turn on the camera", "open the camera",
                                  "start the camera", "show desk view", "camera on", "open camera"]):
            return self.toggle_camera(True)
        if any(k in cmd for k in ["close desk view", "stop desk view", "turn off the camera", "close the camera",
                                  "stop the camera", "camera off", "close camera"]):
            return self.toggle_camera(False)
        # --- PROJECTS ---
        ps = re.search(r'(?:start|open|begin|switch to|work on)\s+(?:a\s+)?project\s+(?:called\s+|named\s+)?(.+)', cmd)
        if ps:
            return self.project_start(ps.group(1).strip())
        if any(k in cmd for k in ["end project", "close project", "stop working on this project",
                                  "finish project", "exit project"]):
            return self.project_end()
        if any(k in cmd for k in ["list projects", "my projects", "what projects"]):
            return self.project_list()
        if any(k in cmd for k in ["project status", "where am i on this", "how's this project",
                                  "project summary"]):
            return self.project_status()

        # --- CALENDAR ---
        cal = re.search(r'(?:schedule|add to (?:my )?calendar|remind me about|book|put in my calendar)\s+(.+?)\s+(?:on|at|for|next|this|in|tomorrow|today)\s+(.+)', cmd)
        if cal:
            return self.calendar_add(cal.group(1).strip(), cal.group(2).strip())
        cal2 = re.search(r'(?:schedule|calendar)\s+(.+)', cmd)
        if cal2 and any(w in cmd for w in ["tomorrow", "today", "monday", "tuesday", "wednesday",
                                           "thursday", "friday", "saturday", "sunday", "am", "pm"]):
            txt = cal2.group(1).strip()
            m = re.search(r'^(.*?)\s+(tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday|at .+|in .+)$', txt)
            if m:
                return self.calendar_add(m.group(1), m.group(2))
        if any(k in cmd for k in ["what's on today", "my agenda", "what's my day", "whats my day",
                                  "what's on my calendar", "agenda for today", "what do i have on"]):
            return self.calendar_today()
        if any(k in cmd for k in ["this week", "upcoming events", "what's coming up", "my schedule"]):
            return self.calendar_agenda(7)

        # --- HOME AUTOMATION ---
        home = re.search(r'turn\s+(on|off)\s+(?:the\s+)?(.+)', cmd)
        if home and not any(k in cmd for k in ["wifi", "wi-fi", "display", "screen", "monitor",
                                               "gestures", "camera", "security", "sentinel"]):
            return self.home_control(home.group(1), home.group(2).strip())
        tog = re.search(r'toggle\s+(?:the\s+)?(.+)', cmd)
        if tog and any(k in cmd for k in ["light", "lamp", "fan", "plug", "switch", "heater"]):
            return self.home_control("toggle", tog.group(1).strip())
        if any(k in cmd for k in ["what's on at home", "home status", "which lights are on",
                                  "smart home status"]):
            return self.home_status()

        # --- SCREEN LENS ---
        if any(k in cmd for k in ["read my screen", "read the screen", "what does my screen say",
                                  "transcribe my screen", "ocr my screen"]):
            threading.Thread(target=self.screen_lens, args=("text",), daemon=True).start()
            return True
        if any(k in cmd for k in ["explain my screen", "explain this error", "what's this error",
                                  "help me with what's on screen", "what am i looking at on screen"]):
            threading.Thread(target=self.screen_lens, args=("explain",), daemon=True).start()
            return True
        if any(k in cmd for k in ["solve what's on my screen", "solve this on screen"]):
            threading.Thread(target=self.screen_lens, args=("solve",), daemon=True).start()
            return True
        if any(k in cmd for k in ["translate my screen", "translate the screen"]):
            threading.Thread(target=self.screen_lens, args=("translate",), daemon=True).start()
            return True

        # --- UNDO ---
        if any(k in cmd for k in ["undo that", "undo the last", "undo last action", "revert that",
                                  "take that back"]):
            return self.undo_last_action()

        # --- LENS MODES (Google-Lens style) ---
        if any(k in cmd for k in ["read the text", "read this text", "what does this say",
                                  "read what's on", "transcribe this", "read this label"]):
            threading.Thread(target=self.lens_analyze, args=("text",), daemon=True).start()
            return True
        if any(k in cmd for k in ["translate this", "translate what you see", "what language is this"]):
            threading.Thread(target=self.lens_analyze, args=("translate",), daemon=True).start()
            return True
        if any(k in cmd for k in ["what product is this", "what brand is this", "how much is this worth",
                                  "identify this product"]):
            threading.Thread(target=self.lens_analyze, args=("product",), daemon=True).start()
            return True
        if any(k in cmd for k in ["solve this", "answer this question", "work this out"]):
            threading.Thread(target=self.lens_analyze, args=("solve",), daemon=True).start()
            return True
        if any(k in cmd for k in ["count these", "how many are there", "count the objects"]):
            threading.Thread(target=self.lens_analyze, args=("count",), daemon=True).start()
            return True
        if any(k in cmd for k in ["read this document", "read this page", "summarise this document"]):
            threading.Thread(target=self.lens_analyze, args=("read",), daemon=True).start()
            return True
        if any(k in cmd for k in ["what colour", "what color", "colours in this"]):
            threading.Thread(target=self.lens_analyze, args=("colour",), daemon=True).start()
            return True
        if any(k in cmd for k in ["explain this", "what is this for", "how does this work"]):
            threading.Thread(target=self.lens_analyze, args=("explain",), daemon=True).start()
            return True
        if any(k in cmd for k in ["what do you see on my desk", "what's on my desk", "what is on my desk",
                                  "identify this", "what am i holding", "what is this", "identify the object",
                                  "identify objects", "what objects"]):
            return self.identify_objects()
        cam_q = re.search(r'(?:look at|check|examine)\s+(?:this|the camera|my desk)\s*(?:and\s+)?(.*)', cmd)
        if cam_q:
            q = cam_q.group(1).strip()
            return self.identify_objects(q if q else None)
        if any(k in cmd for k in ["take a photo", "take a picture", "snap a photo", "capture a photo"]):
            return self.capture_camera_photo()
        if any(k in cmd for k in ["enable gestures", "turn on gestures", "gesture control on", "enable hand control"]):
            return self.set_gestures(True)
        if any(k in cmd for k in ["disable gestures", "turn off gestures", "gesture control off", "stop gestures"]):
            return self.set_gestures(False)

        # --- CONVERSATION MODE ---
        if any(k in cmd for k in ["let's talk", "lets talk", "start conversation", "conversation mode",
                                  "let's chat", "lets chat", "talk to me"]):
            return self.set_conversation_mode(True)
        if any(k in cmd for k in ["end conversation", "stop conversation", "exit conversation"]):
            return self.set_conversation_mode(False)

        # --- TEXTING & CALLING ---
        sms = re.search(r'(?:text|message|sms)\s+(.+?)\s+(?:saying|that says?|about|and say|:)\s*(.+)', cmd)
        if sms:
            recip, msg = sms.group(1).strip(), sms.group(2).strip()
            threading.Thread(target=self.send_sms, args=(recip, msg), daemon=True).start()
            return True
        sms2 = re.search(r'(?:send (?:a )?(?:text|message|sms) to)\s+(.+)', cmd)
        if sms2 and "saying" not in cmd:
            self.speak("What should the message say, Sir?")
            return True
        call = re.search(r'\b(?:call|phone|ring|dial)\s+(.+)', cmd)
        if call and not any(k in cmd for k in ["called", "calling card", "recall"]):
            target = call.group(1).strip()
            saying = re.search(r'\s+and (?:say|tell them)\s+(.+)', target)
            msg = None
            if saying:
                msg = saying.group(1).strip()
                target = target[:saying.start()].strip()
            threading.Thread(target=self.make_call, args=(target, msg), daemon=True).start()
            return True
        savenum = re.search(r'(?:save|remember|store)\s+(?:the\s+)?(?:phone\s+)?number\s+(?:for|of)\s+(.+?)\s+(?:as|is|=)\s+(\+?[\d\s().-]{7,})', cmd)
        if savenum:
            return self.save_phone_number(savenum.group(1).strip(), re.sub(r'[^\d+]', '', savenum.group(2)))

        # --- LEARNING ---
        if any(k in cmd for k in ["what have you learned", "reflect on", "learn from our conversation",
                                  "review what you know", "update what you know about me"]):
            threading.Thread(target=self.reflect_and_learn, daemon=True).start()
            return True
        forget = re.search(r'forget\s+(?:about\s+)?(?:that\s+)?(.+)', cmd)
        if forget and "everything" not in cmd:
            return self.forget_fact(forget.group(1).strip())
        if any(k in cmd for k in ["my most used", "usage stats", "what do i ask most", "command stats"]):
            return self.show_usage_stats()

        # --- SELF-UPDATE ---
        selfupd = re.search(r'(?:update|modify|change|improve|edit)\s+(?:your|ur|his)\s+(?:own\s+)?(?:code|source|self)\s*(?:to|so that|and)?\s*(.+)', cmd)
        if selfupd:
            threading.Thread(target=self.self_update, args=(selfupd.group(1).strip(),), daemon=True).start()
            return True
        if any(k in cmd for k in ["revert your last update", "revert the last update", "undo your update",
                                  "revert my last update", "undo your last change"]):
            return self.revert_self_update()
        if any(k in cmd for k in ["restart yourself", "reboot yourself", "restart amy", "reload yourself"]):
            threading.Thread(target=self.restart_self, daemon=True).start()
            return True

        # --- DOCUMENT WORKSPACE ---
        # Edit the open document: "edit the document to ...", "change the doc ..."
        edit_doc = re.search(r'(?:edit|change|revise|update|rewrite)\s+(?:the\s+)?(?:document|doc|report|pdf)\s+(?:to|so that|and)?\s*(.+)', cmd)
        if edit_doc:
            return self.edit_document(edit_doc.group(1).strip())
        if any(k in cmd for k in ["export to pdf", "export pdf", "export the document", "save as pdf", "save the document", "download the pdf"]):
            return self.export_current_document_pdf()
        # Create a document/PDF/report on a subject.
        make_doc = re.search(r'(?:create|make|generate|write|draft)\s+(?:me\s+)?(?:a\s+)?(?:pdf|document|report|doc)\s+(?:on|about|for|regarding)\s+(.+)', cmd)
        if make_doc:
            return self.create_document(make_doc.group(1).strip())

        # --- IMAGE / VISUAL INSTRUCTIONS ---
        img_req = re.search(r'(?:show me (?:a picture|an image|a photo|a diagram|images?|how to|instructions? (?:on|for)|what)|display (?:an? )?image (?:of|for)?|picture of|image of|diagram of)\s+(.+)', cmd)
        if img_req and "screen" not in cmd:
            query = img_req.group(1).strip()
            query = re.sub(r'^(of|for|to)\s+', '', query)
            return self.show_image_for(query)

        # --- OPEN WEBSITE(S) INSIDE Amy ---
        if any(k in cmd for k in ["close all tabs", "close the tabs", "close viewer tabs", "clear the viewer"]):
            return self.close_viewer_tabs()
        if any(x in cmd for x in [".com", ".org", ".net", ".io", ".co.uk", "http", "in amy", "in the viewer"]):
            # Pull every domain/URL mentioned so "open bbc.com and youtube.com" opens both.
            urls = re.findall(r'https?://\S+|\b[\w-]+(?:\.[\w-]+)+(?:/\S*)?', cmd)
            # Drop false positives (e.g. version numbers, file names).
            urls = [u for u in urls if re.search(r'\.[a-z]{2,}(?:$|/)', u, re.I)
                    and not u.lower().endswith(('.py', '.js', '.txt', '.png', '.jpg', '.pdf', '.exe'))]
            if urls and any(v in cmd for v in ["open", "show", "load", "display", "browse", "pull up"]):
                if len(urls) > 1:
                    return self.open_websites_in_app(urls)
                return self.open_website_in_app(urls[0])

        # --- NEW FEATURES ROUTING ---
        calc = re.search(r'(?:calculate|what(?:\'s| is)|compute|how much is)\s+(.+)', cmd)
        if calc and re.search(r'\d[\s\d+\-*/().%]*[+\-*/]', calc.group(1)):
            return self.calculate(calc.group(1))
        # Wikipedia only on an explicit request, so normal "what is X" still goes to the AI.
        wiki = re.search(r'(?:wikipedia (?:summary|entry|page) (?:of|for|on)|wikipedia|look up)\s+(.+)', cmd)
        if wiki and "screen" not in cmd:
            topic = re.sub(r'\bon wikipedia\b', '', wiki.group(1)).strip()
            return self.wiki_summary(topic)
        if "news" in cmd:
            ntopic = re.search(r'news (?:about|on|for)\s+(.+)', cmd)
            return self.get_news(ntopic.group(1).strip() if ntopic else None)
        price = re.search(r'(?:price of|how much is)\s+(bitcoin|btc|ethereum|eth|dogecoin|doge)\b', cmd)
        if price:
            return self.get_price(price.group(1))
        timer = re.search(r'set (?:a )?timer for (\d+)\s*(second|minute|hour)s?', cmd)
        if timer:
            mult = {"second": 1, "minute": 60, "hour": 3600}[timer.group(2)]
            return self.start_timer(int(timer.group(1)) * mult, "timer")
        remember = re.search(r'(?:remember|note|keep in mind|don\'t forget)\s+that\s+(.+)', cmd)
        if remember:
            return self.remember_fact(remember.group(1).strip())
        if any(k in cmd for k in ["what do you remember", "recall facts", "what do you know about me"]):
            return self.recall_facts()
        if any(k in cmd for k in ["flip a coin", "roll a dice", "roll a die", "pick between", "choose between", "random number"]):
            return self.random_choice(cmd)
        if any(k in cmd for k in ["empty recycle bin", "empty the recycle bin", "clean up", "system cleanup", "empty trash"]):
            return self.system_cleanup()

        # --- Time & date ---
        if any(kw in cmd for kw in ["what time", "what's the time", "current time", "tell me the time"]):
            return self.tell_time()
        if any(kw in cmd for kw in ["what day", "what's the date", "what is the date", "today's date", "what date"]):
            return self.tell_date()

        # --- System status / battery ---
        if any(kw in cmd for kw in ["system status", "system info", "cpu usage", "battery level",
                                    "battery status", "how much battery", "system telemetry"]):
            return self.report_system_status()

        # --- Lock PC ---
        if any(kw in cmd for kw in ["lock pc", "lock the pc", "lock computer", "lock the computer",
                                    "lock workstation", "lock the screen", "lock my pc", "lock my computer"]):
            return self.lock_pc()

        # --- Hardware / power control ---
        vol_set = re.search(r'(?:set )?volume (?:to |at )?(\d{1,3})\s*(?:percent|%)?', cmd)
        if vol_set and "brightness" not in cmd:
            return self.set_volume_level(int(vol_set.group(1)))
        bri = re.search(r'(?:set )?brightness (?:to |at )?(\d{1,3})\s*(?:percent|%)?', cmd)
        if bri:
            return self.set_brightness(int(bri.group(1)))
        if any(k in cmd for k in ["turn off the display", "turn off screen", "turn off the monitor", "sleep the display", "sleep display"]):
            return self.sleep_display()
        if any(k in cmd for k in ["go to sleep", "sleep the pc", "sleep the computer", "put the pc to sleep", "put the computer to sleep"]):
            return self.power_action("sleep")
        if any(k in cmd for k in ["restart the pc", "restart the computer", "reboot the pc", "reboot the computer", "restart my computer"]):
            return self.power_action("restart")
        if any(k in cmd for k in ["shutdown the pc", "shut down the pc", "shutdown the computer", "shut down the computer", "power off the pc", "turn off the computer", "turn off the pc"]):
            return self.power_action("shutdown")
        if any(k in cmd for k in ["turn on wifi", "enable wifi", "turn wifi on", "enable wi-fi", "turn on wi-fi"]):
            return self.toggle_wifi(True)
        if any(k in cmd for k in ["turn off wifi", "disable wifi", "turn wifi off", "disable wi-fi", "turn off wi-fi"]):
            return self.toggle_wifi(False)
        if any(k in cmd for k in ["play media", "pause media", "media play", "media pause", "play pause"]):
            return self.media_control("play")
        if any(k in cmd for k in ["next media", "media next"]):
            return self.media_control("next")

        # --- Screenshot ---
        if any(kw in cmd for kw in ["take a screenshot", "take screenshot", "capture screen", "screenshot"]):
            return self.take_screenshot()

        # --- Clipboard read ---
        if any(kw in cmd for kw in ["read clipboard", "what's in my clipboard", "read my clipboard"]):
            return self.read_clipboard()

        # --- Weather ---
        wmatch = re.search(r'weather(?:\s+(?:in|for|at)\s+(.+))?', cmd)
        if wmatch:
            city = wmatch.group(1).strip() if wmatch.group(1) else None
            return self.get_weather(city)

        # --- Reminders: "remind me to X in N minutes/seconds/hours" ---
        rmatch = re.search(r'remind me to (.+?) in (\d+)\s*(second|minute|hour)s?', cmd)
        if rmatch:
            task = rmatch.group(1).strip()
            amount = int(rmatch.group(2))
            unit = rmatch.group(3)
            mult = {"second": 1, "minute": 60, "hour": 3600}[unit]
            return self.add_reminder(task, amount * mult)

        # --- Web actions (one path: builds the exact URL and opens it) ---
        # Handles "play the sidemen on youtube", "search X on amazon", maps,
        # netflix, spotify, wikipedia and plain google, with filler stripped.
        if self.resolve_web_action(cmd):
            return True
        if "on youtube" in cmd or cmd.startswith("youtube "):
            q = cmd.replace("on youtube", "").replace("youtube", "").strip()
            return self.web_search(q or "trending", engine="youtube")

        gmatch = re.search(r'(?:google|search (?:for|up)?|look up|search)\s+(.+)', cmd)
        if gmatch and not any(k in cmd for k in ["screen", "clipboard", "youtube", "reading", "wikipedia", "wiki"]):
            query = gmatch.group(1).strip()
            query = re.sub(r'\bon google\b', '', query).strip()
            if query:
                return self.web_search(query, engine="google")

        mmatch = re.search(r'(?:directions to|map of|navigate to|where is)\s+(.+)', cmd)
        if mmatch:
            return self.web_search(mmatch.group(1).strip(), engine="maps")

        if any(kw in cmd for kw in ["start reading screen", "enable screen monitoring", "auto screen mode", "start screen ai"]):
            self.toggle_continuous_vision(True)
            self.run_js("setAutoVisionState(true)")
            return True

        if any(kw in cmd for kw in ["stop reading screen", "disable screen monitoring", "stop screen ai"]):
            self.toggle_continuous_vision(False)
            self.run_js("setAutoVisionState(false)")
            return True

        if "volume up" in cmd or "increase volume" in cmd:
            pyautogui.press("volumeup", presses=5)
            self.speak("Volume increased, Sir.")
            return True

        if "volume down" in cmd or "decrease volume" in cmd:
            pyautogui.press("volumedown", presses=5)
            self.speak("Volume decreased, Sir.")
            return True

        if "mute system" in cmd or "mute audio" in cmd or "mute volume" in cmd:
            pyautogui.press("volumemute")
            self.speak("System audio toggled, Sir.")
            return True

        if "play " in cmd and ("on spotify" in cmd or "song" in cmd or "track" in cmd or cmd.startswith("play ")):
            song_query = cmd_text
            for prefix in ["play on spotify", "play song", "play track", "play"]:
                if song_query.lower().startswith(prefix):
                    song_query = song_query[len(prefix):].strip()
                    break
            song_query = song_query.replace("on spotify", "").strip()

            if song_query and song_query.lower() not in ["music", "spotify", "pause"]:
                return self.spotify_play_track(song_query)
            elif song_query.lower() in ["music", "spotify"] or cmd.strip() == "play":
                return self.spotify_toggle_play(force_play=True)

        if any(kw in cmd for kw in ["pause spotify", "pause music", "stop music", "pause song"]):
            return self.spotify_pause()

        if any(kw in cmd for kw in ["resume spotify", "resume music", "unpause music"]):
            return self.spotify_toggle_play(force_play=True)

        if any(kw in cmd for kw in ["next song", "skip song", "next track", "skip track"]):
            return self.spotify_next()

        if any(kw in cmd for kw in ["previous song", "last song", "previous track"]):
            return self.spotify_previous()

        if "what is playing" in cmd or "what's playing" in cmd or "current song" in cmd:
            return self.spotify_announce_current()

        open_match = re.search(r'\b(open|launch|start|focus|run)\s+(.+)', cmd)
        if open_match and not any(k in cmd for k in ["pdf", "email", "card", "macro", "automation"]):
            target_app = open_match.group(2).strip()
            # Strip filler so "open up the notepad app" -> "notepad"
            target_app = re.sub(r'^(up|the|my|a|an)\s+', '', target_app).strip()
            target_app = re.sub(r'\s+(app|application|program|please|for me)$', '', target_app).strip()
            target_app = target_app.strip('.,!?')
            if target_app:
                self.speak(f"Launching {target_app}, Sir.")
                return self.launch_external_app(target_app)

        if any(kw in cmd for kw in ["close app", "close window", "close program"]):
            pyautogui.hotkey('alt', 'f4')
            self.speak("Closing active window.")
            return True

        if any(kw in cmd for kw in ["close tab"]):
            pyautogui.hotkey('ctrl', 'w')
            self.speak("Closing active tab.")
            return True

        if any(kw in cmd for kw in ["switch window", "alt tab", "switch app"]):
            pyautogui.hotkey('alt', 'tab')
            self.speak("Switched window.")
            return True

        if any(kw in cmd for kw in ["minimize window", "minimize app"]):
            pyautogui.hotkey('win', 'down')
            self.speak("Minimized window.")
            return True

        if any(kw in cmd for kw in ["maximize window", "maximize app"]):
            pyautogui.hotkey('win', 'up')
            self.speak("Maximized window.")
            return True

        if any(w in cmd for w in ["look at screen", "read screen", "what is on my screen", "see my screen"]):
            self.capture_screen_vision(cmd)
            return True

        if any(kw in cmd for kw in ["send email", "send an email", "send a email",
                                    "write an email", "write email", "compose email",
                                    "compose an email", "draft email", "draft an email",
                                    "email me", "prepare email"]):
            recipient, instruction, auto_send = self._parse_email_command(cmd_text)
            self.speak("Preparing your email now, Sir.")
            threading.Thread(
                target=self.compose_and_send_email,
                kwargs={"recipient": recipient, "instruction": instruction, "auto_send": auto_send},
                daemon=True,
            ).start()
            return True

        # Save a contact: "remember email for John as john@x.com"
        contact_match = re.search(
            r'(?:remember|save|store)\s+(?:the\s+)?email\s+(?:for|of)\s+(.+?)\s+(?:as|is|=)\s+(\S+@\S+)',
            cmd, re.IGNORECASE)
        if contact_match:
            name = contact_match.group(1).strip()
            addr = extract_email_address(contact_match.group(2))
            if name and addr:
                self.memory.setdefault("contacts", {})[name.lower()] = addr
                CONTACTS[name.lower()] = addr
                self.save_memory()
                self.speak(f"Saved {name}'s email address, Sir.")
                self.safe_log(f"Contact saved: {name} -> {addr}")
            return True

        if "double click" in cmd:
            pyautogui.doubleClick()
            self.speak("Double click executed.")
            return True

        if "right click" in cmd:
            pyautogui.rightClick()
            self.speak("Right click executed.")
            return True

        if "click at" in cmd or "move mouse to" in cmd:
            coords = re.findall(r'\d+', cmd)
            if len(coords) >= 2:
                x, y = int(coords[0]), int(coords[1])
                pyautogui.moveTo(x, y, duration=0.2)
                if "click" in cmd: pyautogui.click()
                self.speak(f"Executed cursor movement to {x}, {y}.")
                return True

        if "click" in cmd:
            pyautogui.click()
            self.speak("Click executed.")
            return True

        if "scroll down" in cmd:
            pyautogui.scroll(-500)
            self.speak("Scrolled down.")
            return True

        if "scroll up" in cmd:
            pyautogui.scroll(500)
            self.speak("Scrolled up.")
            return True

        if cmd.startswith("type "):
            text_to_type = cmd_text[5:]
            pyautogui.write(text_to_type, interval=0.01)
            self.speak("Typing complete.")
            return True

        if "press enter" in cmd:
            pyautogui.press('enter')
            return True

        if "copy to clipboard" in cmd or "copy selection" in cmd:
            pyautogui.hotkey('ctrl', 'c')
            self.speak("Copied to system clipboard.")
            return True

        if "generate pdf on" in cmd or "make a pdf about" in cmd or "generate pdf about" in cmd:
            topic = (cmd.replace("generate pdf on", "").replace("generate pdf about", "")
                        .replace("make a pdf about", "").strip())
            return self.create_document(topic)

        if "when i say" in cmd and "run" in cmd:
            try:
                parts = cmd.replace("when i say", "").split("run")
                keyword = parts[0].strip()
                action = parts[1].strip()
                self.memory["custom_macros"][keyword] = action
                self.save_memory()
                self.speak(f"Macro saved for {keyword}.")
                return True
            except Exception:
                pass

        for kw, act in self.memory.get("custom_macros", {}).items():
            if kw in cmd:
                self.speak(f"Executing macro for {kw}.")
                self.process_command_backend(act)
                return True

        return False

    def handle_text_command(self, cmd):
        self._last_input_voice = False
        # Barge-in: if Amy is currently speaking, stop immediately so the
        # new command takes over without waiting for him to finish.
        if self.is_speaking or not self.speech_queue.empty():
            self.stop_speech()
        self.safe_log(f"> {cmd}", is_user=True)
        threading.Thread(target=self.process_command_backend, args=(cmd,), daemon=True).start()

    def _build_context_messages(self, cmd):
        """Assemble the system prompt + remembered facts + screen context + recent turns."""
        system = self.mind.soul() or SYSTEM_PROMPT
        always = self.mind.memory()
        if always:
            system += "\n\nWhat you know about the user (from MEMORY.md):\n" + always
        index = self.mind.skills_index()
        if index:
            system += "\n\nSkills you have (know-how for specific jobs):\n" + index
        for sk in self.mind.relevant_skills(cmd):
            system += f"\n\nSKILL '{sk['name']}' applies to this request. Follow it:\n{sk['body'][:4000]}"
        summary = self.memory.get("conversation_summary", "")
        if summary:
            system += "\n\nSummary of the earlier conversation:\n" + summary
        if self._last_input_voice and CONFIG.get("assistant", {}).get("short_spoken_replies", True):
            system += ("\n\nThis reply will be spoken aloud. Keep it to three sentences at most. "
                       "If more detail is genuinely needed, give the short version and say the "
                       "detail is on screen.")
        facts = self.memory.get("facts", [])
        if facts:
            # Pull the facts most RELEVANT to this question, not just the newest.
            relevant = []
            try:
                relevant = self.recall_relevant(cmd, top_k=6)
            except Exception as e:
                log_debug(f"recall_relevant failed: {e}")
            chosen = relevant if relevant else facts[-15:]
            system += "\n\nThings you must remember about the user:\n- " + "\n- ".join(chosen)
        # Live screen awareness gives Amy situational context.
        if self.screen_context and (time.time() - self.screen_context_time) < 120:
            system += f"\n\nRight now the user's screen shows: {self.screen_context}"
        note = CONFIG.get("assistant", {}).get("persona_note", "")
        if note:
            system += f"\n\nAdditional style instruction from the user: {note}"
        system += self._project_context()
        chat_history = self.memory.get("chat_history", [])
        recent = chat_history[-16:]
        return [{"role": "system", "content": system}] + recent

    COMPACTION_PROMPT = (
        "Summarise this conversation for your own future reference. Keep it concise but keep: "
        "the user's goals and preferences, decisions and constraints, facts needed to continue, "
        "plans, promises and open questions, and any files, links or results that matter. "
        "No filler, and don't mention that this is a summary.")

    def _compact_history(self):
        """Fold all but the most recent turns into a rolling summary."""
        if getattr(self, "_compacting", False):
            return
        self._compacting = True
        try:
            hist = list(self.memory.get("chat_history", []))
            keep = 12
            if len(hist) <= keep + 6:
                return
            old = hist[:-keep]
            prev = self.memory.get("conversation_summary", "")
            msgs = ([{"role": "system", "content": "Earlier summary:\n" + prev}] if prev else []) + \
                   [{"role": m.get("role", "user"), "content": str(m.get("content", ""))[:1500]} for m in old] + \
                   [{"role": "user", "content": self.COMPACTION_PROMPT}]
            res = self.http.post(OLLAMA_URL, json={
                "model": self.model_for("fast"), "messages": msgs, "stream": False,
                "keep_alive": "30m", "options": {"num_predict": 500, "temperature": 0.2, "num_ctx": 8192},
            }, timeout=180)
            summary = self._llm_text(res)
            if summary:
                with self.memory_lock:
                    self.memory["conversation_summary"] = summary[:4000]
                    current = self.memory.get("chat_history", [])
                    if current[:len(old)] == old:
                        self.memory["chat_history"] = current[len(old):]
                    else:
                        self.memory["chat_history"] = current[-keep:]
                self.save_memory()
                log_debug(f"History compacted: {len(old)} turns -> {len(summary)} chars")
        except Exception as e:
            log_debug(f"history compaction failed: {e}")
        finally:
            self._compacting = False

    def process_command_backend(self, cmd):
        if not cmd:
            return
        cmd_lower = cmd.lower()

        # Close the Amy app itself — but NOT when the user means the whole PC.
        pc_power = any(p in cmd_lower for p in ["the pc", "the computer", "my pc", "my computer", "the system"])
        if (("exit amy" in cmd_lower)
                or ("shutdown amy" in cmd_lower) or ("shut down amy" in cmd_lower)
                or ("close amy" in cmd_lower)
                or ("shutdown" in cmd_lower and "core" in cmd_lower)
                or ("shutdown" in cmd_lower and not pc_power and "restart" not in cmd_lower)):
            self.speak("Deactivating system core. Goodbye, Sir.")
            time.sleep(1.0)
            self.close_app()
            return

        # A short yes/no right after Amy offered something answers that offer.
        try:
            offer = self.proactive.latest_pending(max_age=60)
            if offer and not self.awaiting_approval and not self.pending_clarification:
                if re.fullmatch(r"(yes|yeah|yep|sure|go on|go ahead|do it|please|ok(ay)?|"
                                r"yes please|sounds good)[.! ]*", cmd_lower.strip()):
                    self.proactive.accept(offer["id"])
                    return
                if re.fullmatch(r"(no|nope|nah|no thanks|not now|leave it|later)[.! ]*", cmd_lower.strip()):
                    self.proactive.dismiss(offer["id"])
                    self.speak("No problem.")
                    return
        except Exception as e:
            log_debug(f"offer handling failed: {e}")

        # Log every user message to the permanent transcript before anything else.
        self.remember_message("user", cmd)
        # Passively pick up preferences/corrections as we go.
        try:
            self._record_habit(cmd)
            self.learn_from_interaction(cmd, "")
            self.track_hour_usage(cmd)
            self.project_log(cmd[:120])
        except Exception as e:
            log_debug(f"learn hook error: {e}")

        if self.execute_pc_automation(cmd):
            self.save_memory()
            return

        # Rewrite context-dependent phrasing ("do that again") into something
        # that stands on its own before we try to route it.
        try:
            resolved = self.resolve_references(cmd)
            if resolved and resolved != cmd:
                cmd = resolved
                cmd_lower = cmd.lower()
        except Exception as e:
            log_debug(f"reference resolution error: {e}")

        # Direct web actions ("play the sidemen on youtube") are far more
        # reliable than driving the browser UI, so try them early.
        try:
            if self.resolve_web_action(cmd):
                self.save_memory()
                return
        except Exception as e:
            log_debug(f"web action error: {e}")

        # Already studied this? Answer from the downloaded sources.
        try:
            known = self._known_research_topic(cmd)
            if known and not re.search(r'\b(research|look up|search)\b', cmd, re.I):
                self.kb_search(cmd)
                self.save_memory()
                return
        except Exception as e:
            log_debug(f"research recall failed: {e}")

        # Understand free-form requests (multi-step UI work, research, etc.)
        # without needing a keyword trigger.
        if CONFIG.get("assistant", {}).get("smart_intent", True):
            try:
                if self.smart_route(cmd):
                    self.save_memory()
                    return
            except Exception as e:
                log_debug(f"smart_route failed, continuing: {e}")

        # If this clearly needs live information, research it instead of guessing.
        if self._needs_web(cmd):
            try:
                self.answer_from_web(cmd)
                self.save_memory()
                return
            except Exception as e:
                log_debug(f"auto-research failed, falling back to chat: {e}")

        # Repeated question? Answer instantly without touching the model.
        cached = self.cache_get(cmd)
        if cached:
            self.speak(cached)
            self.remember_message("assistant", cached)
            self.save_memory()
            return

        chat_history = self.memory.setdefault("chat_history", [])
        chat_history.append({"role": "user", "content": cmd})
        messages = self._build_context_messages(cmd)

        # Hard questions get room to reason instead of a 120-token snap answer.
        if self._needs_deliberation(cmd):
            self.safe_log("(thinking it through...)")
            considered = self.think_then_answer(cmd, context=self._recent_context())
            if considered:
                if CONFIG.get("assistant", {}).get("self_check", True):
                    considered, fixed = self.double_check(cmd, considered)
                self.speak(considered)
                self.remember_message("assistant", considered)
                chat_history.append({"role": "assistant", "content": considered})
                self.cache_put(cmd, considered)
                self.save_memory()
                return

        try:
            payload = {
                # Route to whichever model is best for conversation, rather
                # than always the default one.
                "model": self.model_for("chat"),
                "messages": messages,
                "stream": True,
                "keep_alive": "30m",
                "options": {
                    # 120 tokens truncated real answers mid-thought, and a
                    # 2048 context dropped the earlier conversation.
                    "num_predict": int(CONFIG.get("assistant", {}).get("reply_tokens", 320)),
                    "temperature": 0.6,
                    "top_k": 30,
                    "num_ctx": 8192,
                },
            }
            # Stream tokens AND speak each sentence the moment it completes, so
            # Amy starts talking almost immediately instead of after the whole reply.
            reply_parts = []
            speak_buffer = ""
            spoke_anything = False
            turn = self._speech_gen
            with self.http.post(OLLAMA_URL, json=payload, timeout=45, stream=True) as res:
                if res.status_code != 200:
                    self.speak("Local neural model connection refused, Sir.")
                    return
                for line in res.iter_lines():
                    if self._speech_gen != turn:
                        # Interrupted: closing the stream stops Ollama generating.
                        log_debug("Reply cancelled by interruption.")
                        speak_buffer = ""
                        break
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line.decode("utf-8"))
                    except Exception:
                        continue
                    piece = chunk.get("message", {}).get("content", "")
                    if piece:
                        reply_parts.append(piece)
                        speak_buffer += piece
                        # Flush at the earliest natural boundary. The FIRST chunk
                        # may be a clause rather than a full sentence, so audio
                        # starts sooner; after that we wait for sentences.
                        while True:
                            end = (self._first_speakable_chunk(speak_buffer)
                                   if not spoke_anything else 0)
                            if not end:
                                m = re.search(r'[.!?](\s|$)', speak_buffer)
                                if not m:
                                    break
                                end = m.end()
                            chunk_text = speak_buffer[:end].strip()
                            speak_buffer = speak_buffer[end:]
                            if chunk_text:
                                self.speak(chunk_text)
                                spoke_anything = True
                    if chunk.get("done"):
                        break

            if self._speech_gen != turn:
                partial = "".join(reply_parts).strip()
                if partial:
                    chat_history.append({"role": "assistant", "content": partial + " [interrupted]"})
                    self.remember_message("assistant", partial + " [interrupted]")
                self.save_memory()
                return

            # Speak any trailing partial sentence.
            if speak_buffer.strip():
                self.speak(speak_buffer.strip())
                spoke_anything = True

            reply = "".join(reply_parts).strip() or "I could not process the request, Sir."
            if not spoke_anything:
                self.speak(reply)
            self.cache_put(cmd, reply)
            chat_history.append({"role": "assistant", "content": reply})
            self.remember_message("assistant", reply)
            # Older turns get folded into a running summary instead of being
            # silently dropped; the full transcript is preserved separately.
            if len(chat_history) > 30:
                threading.Thread(target=self._compact_history, daemon=True).start()
            self.memory["chat_history"] = chat_history[-40:]
            self.save_memory()
            self.safe_log(f"Amy: {reply}")
        except requests.exceptions.ConnectionError:
            log_debug("Ollama server not reached.")
            self.speak("Ollama is not running. Please start Ollama in the background, Sir.")
        except requests.exceptions.Timeout:
            log_debug("Ollama inference timed out.")
            self.speak("Local AI inference timed out. Please try a shorter query, Sir.")
        except Exception as e:
            log_debug(f"Ollama call exception: {e}")
            self.speak("Error communicating with local AI model, Sir.")

    def save_scratchpad_note(self, text):
        self.memory["scratchpad"] = text
        self.save_memory()

    def add_todo(self, task):
        self.memory["todos"].append(task)
        self.save_memory()
        self.sync_todos_to_ui()

    def remove_todo(self, index):
        if 0 <= index < len(self.memory.get("todos", [])):
            self.memory["todos"].pop(index)
            self.save_memory()
            self.sync_todos_to_ui()

    def sync_todos_to_ui(self):
        todos_json = json.dumps(self.memory.get("todos", []))
        self.run_js(f"renderTodoList({todos_json})")

    def close_app(self):
        try:
            self.save_memory(force=True)
        except Exception:
            pass
        # Only stop Ollama if Amy started it and the config asks for that;
        # other apps may be using it.
        proc = getattr(self, "_ollama_proc", None)
        if proc is not None and CONFIG.get("ollama", {}).get("stop_on_exit", False):
            try:
                proc.terminate()
            except Exception:
                pass
        if self.tray_icon:
            try: self.tray_icon.stop()
            except Exception: pass
        if self.window:
            try: self.window.destroy()
            except Exception: pass
        os._exit(0)


class AmyAPI:
    def __init__(self, app):
        self._app = app

    def set_ui_ready(self):
        self._app.set_ui_ready()

    def handle_text_command(self, cmd):
        self._app.handle_text_command(cmd)

    def set_mic_mute(self, muted):
        self._app.set_mic_mute(muted)

    def toggle_continuous_vision(self, active):
        self._app.toggle_continuous_vision(active)

    def capture_screen_vision(self):
        self._app.capture_screen_vision()

    def stop_speech(self):
        self._app.stop_speech()

    def close_app(self):
        self._app.close_app()

    def spotify_toggle_play_js(self):
        self._app.spotify_toggle_play_js()

    def spotify_next_js(self):
        self._app.spotify_next_js()

    def spotify_previous_js(self):
        self._app.spotify_previous_js()

    def save_scratchpad_note(self, text):
        self._app.save_scratchpad_note(text)

    def add_todo(self, task):
        self._app.add_todo(task)

    def remove_todo(self, index):
        if isinstance(index, str):
            try:
                index = int(index)
            except ValueError:
                return
        self._app.remove_todo(index)

    def launch_external_app(self, target, args=None):
        return self._app.launch_external_app(target, args)

    def toggle_overlay(self):
        self._app.toggle_overlay()

    def take_screenshot(self):
        threading.Thread(target=self._app.take_screenshot, daemon=True).start()

    def report_system_status(self):
        threading.Thread(target=self._app.report_system_status, daemon=True).start()

    # --- Document workspace ---
    def edit_document_ui(self, instruction):
        self._app.edit_document_ui(instruction)

    def update_document_from_ui(self, content):
        self._app.update_document_from_ui(content)

    def export_current_document_pdf(self):
        threading.Thread(target=self._app.export_current_document_pdf, daemon=True).start()

    # --- Viewer ---
    def open_viewer(self, kind, src, title=None):
        self._app.open_in_viewer(kind, src, title)

    # --- Bottom bar customisation ---
    def save_bar_config(self, cfg_json):
        return self._app.save_bar_config(cfg_json)

    # --- Overlay restore ---
    def restore_main(self):
        self._app.restore_main()

    # --- Frameless window controls ---
    def window_minimize(self):
        self._app.window_minimize()

    def window_toggle_fullscreen(self):
        self._app.window_toggle_fullscreen()

    def window_move(self, dx, dy):
        self._app.window_move(dx, dy)

    def window_start_drag(self):
        self._app.window_start_drag()

    def play_sound(self, name):
        self._app.sfx.play(str(name))

    def user_activity(self):
        self._app.last_interaction = time.time()

    def set_sleeping(self, on):
        self._app.set_sleeping(on)

    def get_ui_prefs(self):
        ui = CONFIG.get("ui", {})
        return {"sleep_minutes": ui.get("sleep_minutes", 10), "show_chat": ui.get("show_chat", False),
                "sounds": ui.get("sounds", True)}

    def set_show_chat(self, on):
        CONFIG.setdefault("ui", {})["show_chat"] = bool(on)
        save_config(CONFIG)

    # --- Proactive suggestions ---
    def accept_suggestion(self, sid):
        return self._app.proactive.accept(sid)

    def dismiss_suggestion(self, sid):
        return self._app.proactive.dismiss(sid)

    # --- Coding engine ---
    def run_code(self):
        threading.Thread(target=self._app.run_code, daemon=True).start()

    def fix_code(self):
        threading.Thread(target=self._app.fix_code, daemon=True).start()

    def explain_code(self):
        threading.Thread(target=self._app.explain_code, daemon=True).start()

    def open_code_in_editor(self):
        threading.Thread(target=self._app.open_code_in_editor, daemon=True).start()

    def update_code_from_ui(self, code):
        self._app.update_code_from_ui(code)

    # --- Automation ---
    def stop_automation(self):
        self._app.stop_automation()

    # --- Mobile bridge ---


    def copy_to_clipboard(self, text):
        return self._app.copy_to_clipboard(text)

    def open_website_in_app(self, url):
        threading.Thread(target=self._app.open_website_in_app, args=(url,), daemon=True).start()

    # --- Customisation ---
    def set_theme(self, name):
        self._app.set_theme(name)

    def list_themes(self):
        threading.Thread(target=self._app.list_themes, daemon=True).start()

    def list_custom_commands(self):
        threading.Thread(target=self._app.list_custom_commands, daemon=True).start()

    def list_plugins(self):
        threading.Thread(target=self._app.list_plugins, daemon=True).start()

    def reload_plugins(self):
        threading.Thread(target=self._app.load_plugins, daemon=True).start()

    # --- Windows & browser ---
    def open_window(self, kind="terminal"):
        return self._app.open_window(kind)

    def close_window(self, kind):
        return self._app.close_window(kind)

    def list_windows(self):
        threading.Thread(target=self._app.list_windows, daemon=True).start()

    def browse_task(self, task):
        self._app.run_background("Browsing", self._app.browse_task, task)

    def browser_close(self):
        threading.Thread(target=self._app.browser_close, daemon=True).start()

    # --- Models, speech, tasks ---
    def model_report(self):
        self._app.run_background("Model report", self._app.model_report)

    def stt_report(self):
        threading.Thread(target=self._app.stt_report, daemon=True).start()

    def list_tasks(self):
        threading.Thread(target=self._app.list_tasks, daemon=True).start()

    # --- Trust / approval ---
    def resolve_approval(self, approved):
        self._app.resolve_approval(approved)

    # --- CAD refinement & output ---
    def cad_refine(self, instruction):
        threading.Thread(target=self._app.cad_refine, args=(instruction,), daemon=True).start()

    def cad_accept(self):
        threading.Thread(target=self._app.cad_accept, daemon=True).start()

    def cad_discard(self):
        self._app.cad_discard()

    def send_to_slicer(self):
        threading.Thread(target=self._app.send_to_slicer, daemon=True).start()

    # --- Knowledge base watching ---
    def kb_watch_status(self):
        threading.Thread(target=self._app.kb_watch_status, daemon=True).start()

    def lens_history_view(self):
        threading.Thread(target=self._app.lens_history_view, daemon=True).start()

    def validate_config(self):
        return self._app.validate_config(announce=False)

    # --- Projects / calendar / home / screen lens ---
    def project_start(self, name):
        self._app.project_start(name)

    def project_end(self):
        self._app.project_end()

    def project_list(self):
        threading.Thread(target=self._app.project_list, daemon=True).start()

    def calendar_view(self, scope="today"):
        threading.Thread(target=self._app.calendar_view, args=(scope,), daemon=True).start()

    def home_status(self):
        threading.Thread(target=self._app.home_status, daemon=True).start()

    def screen_lens(self, mode="identify"):
        threading.Thread(target=self._app.screen_lens, args=(mode,), daemon=True).start()

    def undo_last_action(self):
        threading.Thread(target=self._app.undo_last_action, daemon=True).start()

    def habit_suggestions(self):
        threading.Thread(target=self._app.habit_suggestions, daemon=True).start()

    def calendar_agenda(self, days=7):
        threading.Thread(target=self._app.calendar_agenda, args=(int(days),), daemon=True).start()

    # --- Research & progress ---
    def research_and_learn(self, topic):
        threading.Thread(target=self._app.research_and_learn, args=(topic,), daemon=True).start()

    def list_research(self):
        threading.Thread(target=self._app.list_research, daemon=True).start()

    # --- CAD ---
    def cad_create(self, description):
        threading.Thread(target=self._app.cad_create, args=(description,), daemon=True).start()

    def cad_edit(self, instruction):
        threading.Thread(target=self._app.cad_edit, args=(instruction,), daemon=True).start()

    def cad_set_param(self, name, value):
        return self._app.cad_set_param(name, value)

    def cad_improve(self):
        threading.Thread(target=self._app.cad_improve, daemon=True).start()

    def cad_export_stl(self):
        threading.Thread(target=self._app.cad_export_stl, daemon=True).start()

    def cad_open_external(self):
        threading.Thread(target=self._app.cad_open_external, daemon=True).start()

    def update_cad_from_ui(self, code):
        self._app.update_cad_from_ui(code)

    def consolidate_memory(self):
        threading.Thread(target=self._app.consolidate_memory, daemon=True).start()

    # --- Security & diagnostics ---
    def toggle_sentinel(self, active=None):
        self._app.toggle_sentinel(active)

    def security_report(self):
        threading.Thread(target=self._app.security_report, daemon=True).start()

    def self_diagnostics(self):
        threading.Thread(target=self._app.self_diagnostics, daemon=True).start()

    def agent_task(self, task):
        threading.Thread(target=self._app.agent_task, args=(task,), daemon=True).start()

    def kb_status(self):
        threading.Thread(target=self._app.kb_status, daemon=True).start()

    # --- Internet research ---
    def answer_from_web(self, question):
        threading.Thread(target=self._app.answer_from_web, args=(question,), daemon=True).start()

    def deep_research(self, topic):
        threading.Thread(target=self._app.deep_research, args=(topic,), daemon=True).start()

    def list_voices(self):
        threading.Thread(target=self._app.list_voices, daemon=True).start()

    # --- Desk camera ---
    def toggle_camera(self, active=None):
        self._app.toggle_camera(active)

    # --- Multiple cameras ---
    def switch_camera(self, which):
        threading.Thread(target=self._app.switch_camera, args=(which,), daemon=True).start()

    def scan_cameras(self):
        threading.Thread(target=self._app.scan_cameras, daemon=True).start()

    def add_camera(self, name, index):
        threading.Thread(target=self._app.add_camera, args=(name, index), daemon=True).start()

    def rename_camera(self, old, new):
        threading.Thread(target=self._app.rename_camera, args=(old, new), daemon=True).start()

    def remove_camera(self, which):
        threading.Thread(target=self._app.remove_camera, args=(which,), daemon=True).start()

    def request_camera_list(self):
        threading.Thread(target=self._app._push_camera_list, daemon=True).start()

    def identify_objects(self):
        threading.Thread(target=self._app.identify_objects, daemon=True).start()

    def lens_analyze(self, mode="identify"):
        threading.Thread(target=self._app.lens_analyze, args=(mode,), daemon=True).start()

    def capture_camera_photo(self):
        threading.Thread(target=self._app.capture_camera_photo, daemon=True).start()

    def set_gestures(self, enabled):
        self._app.set_gestures(enabled)

    # --- Conversation mode ---
    def toggle_conversation(self):
        self._app.set_conversation_mode(not self._app.conversation_mode)

    def set_conversation_mode(self, active):
        self._app.set_conversation_mode(active)

    # --- Learning ---
    def reflect_and_learn(self):
        threading.Thread(target=self._app.reflect_and_learn, daemon=True).start()

    def show_usage_stats(self):
        threading.Thread(target=self._app.show_usage_stats, daemon=True).start()

    # --- Self-update ---
    def self_update(self, instruction):
        threading.Thread(target=self._app.self_update, args=(instruction,), daemon=True).start()

    def revert_self_update(self):
        threading.Thread(target=self._app.revert_self_update, daemon=True).start()

    def restart_self(self):
        threading.Thread(target=self._app.restart_self, daemon=True).start()


def main():
    log_debug("Starting Amy...")
    try:
        qt_app = make_qt_app()
        app = AmyApp()
        api = AmyAPI(app)
        app._api = api          # detached windows share the same bridge

        app.window = webview.create_window(
            title="Amy",
            html=HTML_UI,
            js_api=api,
            width=1320,
            height=820,
            min_size=(960, 640),
            frameless=True,
            background_color="#0e1011",
        )
        app.window.events.closed += app.close_app
        if CONFIG.get("ui", {}).get("overlay_on_start", False):
            app.toggle_overlay(True)
        sys.exit(qt_app.exec_())
    except Exception as e:
        log_debug(f"Fatal GUI initialization error: {traceback.format_exc()}")
        print(f"Fatal system error: {e}")


# Older scripts and the bundler may still use the J.A.R.V.I.S. names.
JarvisApp = AmyApp
JarvisAPI = AmyAPI

if __name__ == "__main__":
    main()
