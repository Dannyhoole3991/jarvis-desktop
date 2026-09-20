"""
Jarvis Desktop HUD
==================

A system-tray app that gives Jarvis a full mission-control style dashboard
window instead of a plain console: real CPU/GPU/RAM/disk/network stats, a
clickable folder/app shortcut graph, and a live step-by-step feed of
whatever Jarvis is currently doing.

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
import ctypes.wintypes
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

STATUS_URL = "http://localhost:8765/status"
STOP_URL = "http://localhost:8765/stop"

WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 860
WINDOW_MIN_SIZE = (1000, 680)

# The dashboard is a full opaque window (not a see-through floating orb
# like the earlier design), so it doesn't need to float above everything
# else by default.
ALWAYS_ON_TOP = False

jarvis_process = None          # Popen handle, only set if WE started the engine
window = None                  # webview.Window
tray_icon = None               # pystray.Icon
_shutdown_lock = threading.Lock()
_shutting_down = False
_is_maximized = True


def _get_work_area():
    """
    (left, top, width, height) of the primary monitor's work area, i.e. the
    full screen MINUS the taskbar. Confirmed live that pywebview's
    maximized=True / window.maximize() do not reliably fill this for a
    frameless window on this machine's backend (window opened at its
    default size, unmoved) -- so the HUD sizes itself explicitly instead of
    trusting either of those.
    """
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _fill_work_area():
    if window is None:
        return
    left, top, width, height = _get_work_area()
    window.resize(width, height)
    window.move(left, top)


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
    def get_shared_secret(self):
        # The engine's local HTTP server requires this on /command, /stop,
        # and /attach_image (added so the phone's public tunnel couldn't be
        # used to control the PC by anyone who found the URL) -- confirmed
        # live that the HUD's own calls were never sending it and would
        # get rejected with 401 whenever this is set. window.pywebview.api
        # is the only way to hand the HTML page an environment variable
        # from this process without writing it into the HTML file itself.
        return os.environ.get("JARVIS_PHONE_SECRET", "")

    def hide_to_tray(self):
        hide_window()

    def toggle_maximize(self):
        global _is_maximized
        if window is None:
            return
        try:
            if _is_maximized:
                window.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
                left, top, work_w, work_h = _get_work_area()
                window.move(left + (work_w - WINDOW_WIDTH) // 2, top + (work_h - WINDOW_HEIGHT) // 2)
            else:
                _fill_work_area()
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

def _maximize_on_startup():
    # Confirmed live: a single fill 0.3s after window creation isn't
    # enough right after a fresh PC boot -- this HUD auto-starts from the
    # Windows Startup folder, and the display/work-area (monitor
    # detection, DPI, taskbar) hadn't fully settled yet 46s after boot,
    # so the one-shot fill locked in a wrong size (window ended up
    # partly off-screen above the top edge). Retrying over the first
    # ~20s means even if an early attempt reads a stale/wrong work area,
    # a later one corrects it once things have actually settled.
    gaps_between_attempts = (0.3, 0.7, 1, 2, 3, 4, 5, 4)  # cumulative: 0.3, 1, 2, 4, 7, 11, 16, 20
    for gap in gaps_between_attempts:
        time.sleep(gap)
        try:
            _fill_work_area()
        except Exception as error:
            print(f"Could not size HUD to the work area on startup: {error}")


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
        resizable=True,
        frameless=True,
        easy_drag=False,   # only .pywebview-drag-region elements drag the window
        transparent=False,
        on_top=ALWAYS_ON_TOP,
        js_api=HudApi(),
    )

    webview.start(func=_maximize_on_startup)

    # webview.start() returns once the window is closed/destroyed.
    quit_app()


if __name__ == "__main__":
    main()
