"""
Jarvis's eyes on the PC, priced very differently:

  list_windows()   which app is in front and what's open: names only, no picture. Usually enough.
  capture()        ONE screenshot, only when Danny asks Jarvis to look. Saved to data/screens
                   (the last 5 are kept) so the Claude brain can Read the image.
"""

import ctypes
import ctypes.wintypes as wt
import glob
import os
import time

from . import config

SCREENS_DIR = os.path.join(config.DATA_DIR, "screens")
KEEP = 5


def _process_name(hwnd):
    pid = wt.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(512)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def list_windows():
    """[{title, app, foreground, minimised}] for visible top-level windows with a title."""
    user32 = ctypes.windll.user32
    fg = user32.GetForegroundWindow()
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def each(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if title and title not in ("Program Manager", "Windows Input Experience"):
            out.append({"title": title, "app": _process_name(hwnd), "foreground": hwnd == fg,
                        "minimised": bool(user32.IsIconic(hwnd))})
        return True

    user32.EnumWindows(each, 0)
    return sorted(out, key=lambda w: (not w["foreground"], w["minimised"]))


def describe_windows():
    wins = list_windows()
    if not wins:
        return "I can't see any open windows."
    front = next((w for w in wins if w["foreground"]), None)
    lines = []
    if front:
        lines.append(f"In front: {front['title']} ({front['app']}).")
    others = [w for w in wins if not w["foreground"]]
    if others:
        lines.append("Also open: " + "; ".join(f"{w['title']} ({w['app']}{', minimised' if w['minimised'] else ''})" for w in others[:20]) + ".")
    return " ".join(lines)


def capture():
    """Takes one screenshot of the main display. Returns the saved file's path."""
    from PIL import ImageGrab
    os.makedirs(SCREENS_DIR, exist_ok=True)
    path = os.path.join(SCREENS_DIR, f"screen-{int(time.time())}.png")
    img = ImageGrab.grab()
    if img.width > 1600:
        img = img.resize((1600, int(img.height * 1600 / img.width)))
    img.save(path)
    for old in sorted(glob.glob(os.path.join(SCREENS_DIR, "screen-*.png")))[:-KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass
    return path
