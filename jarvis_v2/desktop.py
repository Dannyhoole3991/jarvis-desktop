"""
Opens the Jarvis v2 window.

    py -3 -m jarvis_v2.desktop          (or double-click START_JARVIS_V2.bat)

Starts the v2 server (brain router + dashboard API), makes sure the voice engine
is running (the same engine the old HUD uses), switches the engine's conversation
over to the new brain while this window is open, and switches it back on close.
The old HUD is untouched and still works on its own.
"""

import os
import sys
import threading

from . import config, server


def _start_engine():
    """The same launcher the old HUD uses: starts Jarvis_FINAL_WORKING.py hidden unless it's already running."""
    try:
        sys.path.insert(0, config.REPO_DIR)
        import Jarvis_Desktop_HUD as hud
        hud.launch_jarvis_hidden()
    except Exception as error:
        print(f"Couldn't start the voice engine: {error}")


class WindowApi:
    """
    Called from the page (F11 / the fullscreen button).

    The window is kept in a PRIVATE attribute on purpose: pywebview exposes every public
    attribute of js_api to the page by walking it, and walking the native window object
    recursed forever and crashed the app on open (measured).
    """

    def __init__(self):
        self._window = None

    def toggle_fullscreen(self):
        if self._window is not None:
            self._window.toggle_fullscreen()


def _profile_in_use(path):
    """True while any WebView2 helper (msedgewebview2.exe) still has this profile folder open."""
    import subprocess
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | ForEach-Object CommandLine"],
                             capture_output=True, text=True, timeout=15, creationflags=0x08000000).stdout
    except Exception:
        return False
    return os.path.normcase(path) in os.path.normcase(out)


def _pick_profile():
    """
    WebView2 refuses to start ("the requested resource is in use") while helpers from a previous,
    just-closed or crashed window still hold the profile. Wait briefly for them, else use a spare folder.
    """
    import time
    main_dir = os.path.join(config.DATA_DIR, "webview")
    spare = os.path.join(config.DATA_DIR, "webview_spare")
    for _ in range(10):
        if not _profile_in_use(main_dir):
            return main_dir
        time.sleep(1)
    return spare if not _profile_in_use(spare) else os.path.join(config.DATA_DIR, f"webview_{os.getpid()}")


def main():
    import webview
    srv, state = server.start()
    server.set_engine_brain(True)
    threading.Thread(target=_start_engine, daemon=True).start()
    api = WindowApi()
    # Opens maximised to whatever screen it's on (Danny's is 1280x720); the page itself scales to fit.
    api._window = webview.create_window("Jarvis", f"http://127.0.0.1:{server.PORT}/", width=1200, height=660,
                                       min_size=(800, 500), maximized=True, background_color="#050508", js_api=api)
    try:
        # Its own WebView2 profile folder: sharing the old HUD's would clash if both windows are open.
        webview.start(private_mode=False, storage_path=_pick_profile())
    finally:
        server.stop(srv, state)


if __name__ == "__main__":
    main()
