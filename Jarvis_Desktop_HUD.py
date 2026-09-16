"""
Jarvis Desktop HUD
==================

A system-tray app that gives Jarvis a small, see-through floating orb on
screen instead of a plain console: a circle that glows and moves gently
while idle, pulses faster while thinking, and animates more while
speaking, plus a simple text box for typed commands as a fallback to
voice.

Design:
  - Jarvis_FINAL_WORKING.py (the actual engine -- wake word, brain, TTS,
    everything) is started completely UNCHANGED, as its own process, in its
    own real console. That console is created hidden (SW_HIDE) rather than
    not created at all, so the engine's existing keyboard/console code
    (msvcrt, input()) keeps working exactly as before with zero engine
    changes required for this to work.
  - This script itself shows only the dashboard window (jarvis_hud.html)
    plus a tray icon. The dashboard talks to the engine over the same
    local HTTP interface the phone app already uses (http://localhost:8765
    -- /status, /stats, /activity, /command, /stop), so the engine and the
    dashboard are two independent processes connected only by that API.
  - Closing the dashboard's (x) button or choosing "Quit Jarvis" from the
    tray stops the engine process (if this app is the one that started it)
    and exits. The tray's "Hide HUD" / "Show HUD" just toggles window
    visibility without touching the engine.

Run this instead of Jarvis_FINAL_WORKING.py directly. Launch it with
pythonw.exe (no console for this launcher itself) for a fully console-free
experience, e.g.:

    pythonw Jarvis_Desktop_HUD.py
"""

import ctypes
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pystray
import webview
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
JARVIS_SCRIPT = os.path.join(HERE, "Jarvis_FINAL_WORKING.py")
HUD_HTML = os.path.join(HERE, "jarvis_hud.html")
WALLPAPER_IMAGE = os.path.join(HERE, "jarvis_sky_background.jpg")

STATUS_URL = "http://localhost:8765/status"
STOP_URL = "http://localhost:8765/stop"

WINDOW_WIDTH = 320
WINDOW_HEIGHT = 300
WINDOW_MIN_SIZE = (320, 300)

# Back to the original design: a small, see-through floating orb rather
# than a full dashboard window -- it should sit on top of whatever else
# is on screen, like a companion widget, not a normal app window.
ALWAYS_ON_TOP = False

# Danny's request: he wants Jarvis to genuinely feel like part of his
# desktop. DESKTOP_MODE controls the safe part: setting the wallpaper.
#
# ATTEMPT_DESKTOP_ATTACH controls the risky part -- reparenting the orb
# window into the WorkerW layer Windows keeps behind desktop icons, so
# it renders behind them instead of floating on top. Confirmed live on
# this machine that this DOESN'T work the standard way: this Windows
# build never creates a fresh WorkerW when asked, and there are 13
# pre-existing, unrelated WorkerW windows already present, so a
# permissive "any WorkerW that isn't the icon host" match grabbed one of
# those instead -- which is why the orb vanished entirely (parented into
# some unrelated hidden window). Left OFF until this is solved properly
# (most likely by using an existing, actively-maintained tool built for
# exactly this -- e.g. Lively Wallpaper -- instead of hand-rolling
# undocumented Windows internals against a moving target).
DESKTOP_MODE = True
ATTEMPT_DESKTOP_ATTACH = False

jarvis_process = None          # Popen handle, only set if WE started the engine
window = None                  # webview.Window
tray_icon = None               # pystray.Icon
_shutdown_lock = threading.Lock()
_shutting_down = False
_is_maximized = False


# ------------------------------------------------------------------
# Engine process management
# ------------------------------------------------------------------

def is_jarvis_running():
    """True if something is already answering the engine's /status endpoint."""
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _resolve_console_python():
    """
    Return a python.exe (console-capable) interpreter path, even if this
    launcher itself was started with pythonw.exe (no console). The engine
    needs a real console under the hood for its existing msvcrt/input()
    keyboard code to keep working -- we just hide that console's window.
    """
    exe_dir = os.path.dirname(sys.executable)
    candidate = os.path.join(exe_dir, "python.exe")
    if os.path.exists(candidate):
        return candidate
    return sys.executable


def launch_jarvis_hidden():
    """
    Start Jarvis_FINAL_WORKING.py in its own console window, created hidden.
    Does nothing if an engine is already answering /status (e.g. started by
    hand, or by a previous run of this HUD).
    """
    global jarvis_process

    if is_jarvis_running():
        print("Jarvis engine already running -- attaching to it.")
        return

    python_exe = _resolve_console_python()

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE

    jarvis_process = subprocess.Popen(
        [python_exe, JARVIS_SCRIPT],
        cwd=HERE,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        startupinfo=startupinfo,
    )

    # Give it a little time to come up before the HUD starts polling.
    for _ in range(60):
        if is_jarvis_running():
            break
        time.sleep(0.5)


def restart_jarvis_engine():
    stop_jarvis_engine_if_ours()
    time.sleep(1.0)
    launch_jarvis_hidden()


def stop_jarvis_engine_if_ours():
    """
    Only terminate the engine process if THIS app started it -- never kill
    an engine instance someone started by hand, since we can't tell whether
    they're relying on it separately.
    """
    global jarvis_process
    if jarvis_process is None:
        return
    try:
        # Best-effort: let it stop mid-speech cleanly first.
        req = urllib.request.Request(STOP_URL, method="POST")
        urllib.request.urlopen(req, timeout=1.5)
    except Exception:
        pass
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(jarvis_process.pid)],
            capture_output=True,
        )
    except Exception as error:
        print(f"Could not stop Jarvis engine process: {error}")
    jarvis_process = None


# ------------------------------------------------------------------
# Desktop mode: wallpaper + reparenting the orb behind desktop icons
# ------------------------------------------------------------------

DESKTOP_MODE_LOG = os.path.join(HERE, "_desktop_mode_debug.log")


def _desktop_log(message):
    """
    This launcher normally runs console-free (pythonw), so plain print()
    here goes nowhere anyone can see. Log to a file instead so desktop-mode
    issues can actually be diagnosed after the fact.
    """
    print(f"Desktop mode: {message}")
    try:
        with open(DESKTOP_MODE_LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
    except Exception:
        pass


SPI_SETDESKWALLPAPER = 20
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02


def set_desktop_wallpaper(path):
    """Set the Windows desktop wallpaper. Safe/reversible -- just a normal
    wallpaper change, the same as doing it from Settings by hand."""
    if not os.path.exists(path):
        _desktop_log(f"wallpaper image not found, skipping: {path}")
        return False
    try:
        ctypes.windll.user32.SystemParametersInfoW(
            SPI_SETDESKWALLPAPER, 0, path, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
        )
        return True
    except Exception as error:
        _desktop_log(f"could not set desktop wallpaper: {error}")
        return False


def _find_worker_w():
    """
    Windows keeps a hidden 'WorkerW' window directly behind the desktop
    icons (above the wallpaper). Asking Progman to spawn one, then finding
    it, is the standard technique wallpaper-engine-style apps use to
    render something that looks like it's part of the desktop.

    The naive version of this (find the WorkerW that comes immediately
    after the one hosting SHELLDLL_DefView in z-order) is a known-fragile
    match on some Windows 11 builds -- confirmed live here: it found
    nothing at all. This version is more permissive: collect every
    WorkerW that exists, and pick any one that ISN'T itself hosting the
    icon view, since that's the "empty" one apps are meant to render into.
    """
    user32 = ctypes.windll.user32
    progman = user32.FindWindowW("Progman", None)
    if not progman:
        _desktop_log("no Progman window found at all.")
        return None

    result = ctypes.c_ulong()
    user32.SendMessageTimeoutW(progman, 0x052C, 0, 0, 0x0, 1000, ctypes.byref(result))
    time.sleep(0.3)  # Explorer creates the WorkerW asynchronously

    icon_host = [None]
    all_worker_w = []

    def enum_windows_proc(hwnd, _lparam):
        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, 256)
        if class_buf.value == "WorkerW":
            all_worker_w.append(hwnd)
        if user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None):
            icon_host[0] = hwnd
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(WNDENUMPROC(enum_windows_proc), 0)

    _desktop_log(f"found {len(all_worker_w)} WorkerW window(s); icon host = {icon_host[0]}.")

    for candidate in all_worker_w:
        if candidate != icon_host[0]:
            return candidate

    # Some Windows versions host the icon view directly under Progman,
    # with no separate WorkerW at all -- Progman itself is then the
    # right thing to attach behind.
    if icon_host[0] == progman:
        return progman

    return None


HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
HWND_BOTTOM = 1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008


def attach_window_to_desktop(hwnd):
    """
    Reparent our own window into the WorkerW layer so it renders behind
    the desktop icons instead of floating on top of everything. Returns
    True on success; the caller should just leave the window as a normal
    floating one if this returns False -- nothing else depends on it.

    Also explicitly drops the "always on top" style pywebview's on_top=True
    sets (TopMost, via SetWindowPos/HWND_TOPMOST under the hood) -- a
    topmost window fights this reparenting and can still render above the
    icons even once SetParent has succeeded, which is exactly the "orb is
    floating on the icons instead of behind them" symptom this fixes.
    """
    user32 = ctypes.windll.user32
    try:
        worker_w = _find_worker_w()
        if not worker_w:
            _desktop_log("couldn't find the WorkerW layer; staying as a floating orb.")
            return False

        # Only drop "always on top" once we know reparenting is actually
        # going to happen -- leaving it topmost is the correct, safe
        # fallback for the "couldn't find it" case above, so the orb
        # never ends up as a normal window that windows can bury.
        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style & ~WS_EX_TOPMOST)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)

        result = user32.SetParent(hwnd, worker_w)
        if not result:
            _desktop_log("SetParent returned failure; restoring always-on-top.")
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
            return False

        user32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
        _desktop_log("attached to the desktop WorkerW layer successfully.")
        return True
    except Exception as error:
        _desktop_log(f"couldn't attach to the desktop layer: {error}")
        try:
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
        except Exception:
            pass
        return False


class MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


def enable_true_transparency(hwnd):
    """
    pywebview's own transparency support (setting the WebView2 control's
    background to Color.Transparent -- see its winforms.py, which even
    calls this "a hack... no idea why this works") isn't enough on its
    own here -- confirmed live, it renders solid white instead of
    see-through. DwmExtendFrameIntoClientArea is the actual documented
    Windows API for telling the compositor to treat a window's whole
    client area as real glass, composited with true per-pixel alpha
    against whatever is behind it, which is what's missing.
    """
    try:
        margins = MARGINS(-1, -1, -1, -1)
        result = ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
        _desktop_log(f"DwmExtendFrameIntoClientArea returned {result} (0 = success).")
    except Exception as error:
        _desktop_log(f"couldn't extend the DWM frame for transparency: {error}")


def enable_desktop_mode():
    """Runs once the webview window actually exists. See DESKTOP_MODE above."""
    set_desktop_wallpaper(WALLPAPER_IMAGE)

    hwnd = None
    for _ in range(20):
        try:
            hwnd = window.native.Handle.ToInt32()
            if hwnd:
                break
        except Exception:
            pass
        time.sleep(0.25)

    if not hwnd:
        _desktop_log("window handle never became available; staying as a floating orb.")
        return

    enable_true_transparency(hwnd)

    if ATTEMPT_DESKTOP_ATTACH:
        attach_window_to_desktop(hwnd)


# ------------------------------------------------------------------
# Tray icon
# ------------------------------------------------------------------

def build_tray_image():
    """Small glowing-orb icon drawn with PIL (no external icon file needed)."""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    center = size // 2
    for radius, color in [
        (30, (28, 60, 90, 120)),
        (22, (40, 130, 200, 200)),
        (14, (100, 220, 255, 255)),
        (7, (235, 250, 255, 255)),
    ]:
        draw.ellipse(
            (center - radius, center - radius, center + radius, center + radius),
            fill=color,
        )
    return image


def show_window():
    if window is not None:
        window.show()


def hide_window():
    if window is not None:
        window.hide()


def on_tray_restart(icon, item):
    threading.Thread(target=restart_jarvis_engine, daemon=True).start()


def on_tray_quit(icon, item):
    quit_app()


def build_tray_menu():
    return pystray.Menu(
        pystray.MenuItem("Show HUD", lambda icon, item: show_window(), default=True),
        pystray.MenuItem("Hide HUD", lambda icon, item: hide_window()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Restart Jarvis Engine", on_tray_restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Jarvis", on_tray_quit),
    )


def run_tray():
    global tray_icon
    tray_icon = pystray.Icon(
        "jarvis_hud", build_tray_image(), "Jarvis", menu=build_tray_menu()
    )
    tray_icon.run()


# ------------------------------------------------------------------
# JS <-> Python bridge for window chrome (hide/quit buttons in the HUD)
# ------------------------------------------------------------------

class HudApi:
    def hide_to_tray(self):
        hide_window()

    def toggle_maximize(self):
        global _is_maximized
        if window is None:
            return
        try:
            if _is_maximized:
                window.restore()
            else:
                window.maximize()
            _is_maximized = not _is_maximized
        except Exception as error:
            print(f"Could not toggle maximize: {error}")

    def quit_app(self):
        quit_app()


def quit_app():
    global _shutting_down
    with _shutdown_lock:
        if _shutting_down:
            return
        _shutting_down = True

    stop_jarvis_engine_if_ours()

    if tray_icon is not None:
        try:
            tray_icon.stop()
        except Exception:
            pass

    if window is not None:
        try:
            window.destroy()
        except Exception:
            pass


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main():
    global window

    launch_jarvis_hidden()

    threading.Thread(target=run_tray, daemon=True).start()

    window = webview.create_window(
        "Jarvis",
        HUD_HTML,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=WINDOW_MIN_SIZE,
        resizable=False,
        frameless=True,
        easy_drag=False,   # only .pywebview-drag-region elements drag the window
        transparent=True,
        on_top=ALWAYS_ON_TOP,  # attach_window_to_desktop drops this itself, only on success
        js_api=HudApi(),
    )

    webview.start(func=(enable_desktop_mode if DESKTOP_MODE else None))

    # webview.start() returns once the window is closed/destroyed.
    quit_app()


if __name__ == "__main__":
    main()
