# Jarvis v53 - Windows 11 Executable Learning Plans (built directly from v52)
from openai import OpenAI
import re
import requests
import subprocess
import os
import webbrowser
import datetime
import json
import difflib
import random
import sys
import traceback
import collections
import psutil

# UFO²'s log lines are full of emoji (👀💡📚 etc.). Jarvis prints those lines
# verbatim to its own stdout. Depending on how Jarvis is launched (a plain
# console, a redirected log file, a scheduled task), Python's default stdout
# encoding can be the legacy Windows codepage (cp1252) instead of UTF-8,
# which cannot encode emoji and crashes the whole process on print(). Force
# UTF-8 on our own stdout/stderr the same way it's already forced for the
# UFO² child process's environment further down this file.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
import pyttsx3
import threading
import http.server
import socketserver
import queue
import time
import ctypes
from ctypes import wintypes
import base64
import io
try:
    from PIL import ImageGrab, Image
    SCREEN_VISION_AVAILABLE = True
except ImportError:
    ImageGrab = None
    Image = None
    SCREEN_VISION_AVAILABLE = False
try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import speech_recognition as sr
    SPEECH_RECOGNITION_AVAILABLE = True
except ImportError:
    sr = None
    SPEECH_RECOGNITION_AVAILABLE = False

try:
    import sounddevice as sd
    import numpy as np
    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    sd = None
    np = None
    SOUNDDEVICE_AVAILABLE = False


# ============================================================
# VOICE INPUT + WAKE WORD
# ============================================================

VOICE_INPUT_ENABLED = True
WAKE_WORD = "jarvis"
voice_commands = queue.Queue()


# ============================================================
# JARVIS MOBILE / TAILSCALE
# ============================================================

REMOTE_COMMANDS_ENABLED = True
REMOTE_COMMAND_TIMEOUT = 60
remote_commands = queue.Queue()
active_remote_reply = None
active_remote_lock = threading.Lock()


# ============================================================
# JARVIS DASHBOARD -- SYSTEM STATS + LIVE ACTIVITY
# ============================================================
# Backs the desktop HUD's dashboard: real system stats (CPU/RAM/disk/
# network -- no placeholder or fabricated numbers) and a generic
# step-by-step "what is Jarvis doing right now" feed the HUD polls and
# renders live. Deliberately NOT wired into every single one of this
# file's ~150 command handlers -- that would be a much bigger sweep for
# little benefit on near-instant local actions. Wired properly into the
# paths where progress is genuinely meaningful to watch: plain
# conversation (ask_jarvis), file search (find_user_file, matches the
# reference dashboard's own example), and a coarse pass over v58/UFO2
# automation. Every other handler still shows a real "Understanding
# request" step while it runs, finalized the moment the next command
# begins (see the main loop).

_dashboard_lock = threading.Lock()
_activity_steps = []           # [{"label": str, "status": "done"|"active"|"pending"}]
_activity_current_action = {}  # {"title"/"detail"/"location"/"items_scanned"/"matches": ...}
_recent_actions = collections.deque(maxlen=25)

_CPU_NAME = None
_GPU_NAME = None
_net_last_sample = None        # (timestamp, bytes_sent, bytes_recv)
_net_history = collections.deque(maxlen=30)  # recent (down_kbps, up_kbps) samples for a sparkline


# ---- Real CPU/GPU temperatures via LibreHardwareMonitor ----
# Windows has no built-in way to read these (confirmed on this machine --
# Win32_TemperatureProbe and the ACPI thermal zone both report "Not
# supported"). LibreHardwareMonitor (https://github.com/LibreHardwareMonitor
# /LibreHardwareMonitor, downloaded 2026-09-14 with danny's explicit
# permission after showing him the exact file/source/size) fills the gap.
# It's set up to run in the background with its Remote Web Server enabled
# on port 8085 (pre-seeded via LibreHardwareMonitor/LibreHardwareMonitor.
# config -- the runWebServerMenuItem/listenerPort/minTrayMenuItem/
# minCloseMenuItem keys -- so it never needs a manual click through its
# GUI). No admin/elevation turned out to be required for these two
# specific sensors on this hardware.
LIBRE_HARDWARE_MONITOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "LibreHardwareMonitor")
LIBRE_HARDWARE_MONITOR_EXE = os.path.join(LIBRE_HARDWARE_MONITOR_DIR, "LibreHardwareMonitor.exe")
LIBRE_HARDWARE_MONITOR_URL = os.environ.get("JARVIS_LHM_URL", "http://localhost:8085/data.json")

# Sensor IDs are specific to THIS machine's exact hardware (verified live
# via the data.json tree) -- "Core (Tctl/Tdie)" is the standard AMD CPU
# package/control temperature, "GPU Core" the GPU die temperature. Falls
# back to matching by display name if the numeric id ever shifts (e.g.
# after a driver update), so this survives more than an exact-id match.
_CPU_TEMP_SENSOR_ID = "/amdcpu/0/temperature/2"
_CPU_TEMP_SENSOR_NAME = "Core (Tctl/Tdie)"
_GPU_TEMP_SENSOR_ID = "/gpu-amd/0/temperature/0"
_GPU_TEMP_SENSOR_NAME = "GPU Core"


def _find_sensor_value(node, sensor_id=None, name=None, sensor_type=None):
    """
    Depth-first search of a LibreHardwareMonitor data.json tree for a
    sensor matching a known SensorId (preferred) or exact display name.

    `sensor_type` (e.g. "Temperature") is required on the name-based
    fallback -- LibreHardwareMonitor reuses generic display names like
    "GPU Core" across totally different sensors (a Voltage, a Clock, AND a
    Temperature sensor on this exact GPU are all called "GPU Core"), so
    matching on name alone can silently return the wrong reading (caught
    live: a first attempt without this check returned the GPU's *voltage*,
    0.669, as if it were a temperature in degrees).
    """
    if sensor_id and node.get("SensorId") == sensor_id:
        return node.get("Value")
    if (
        name
        and node.get("Text") == name
        and node.get("SensorId")
        and (sensor_type is None or node.get("Type") == sensor_type)
    ):
        return node.get("Value")
    for child in node.get("Children", []) or []:
        result = _find_sensor_value(child, sensor_id, name, sensor_type)
        if result is not None:
            return result
    return None


def _parse_temp_value(raw):
    """'80.9 °C' -> 80.9 (float), or None if unparsable/missing."""
    if not raw:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(raw))
    return float(match.group()) if match else None


def get_hardware_monitor_temps():
    """
    Real CPU/GPU temperatures read from LibreHardwareMonitor's local web
    server. Returns (cpu_temp_c, gpu_temp_c) as floats, either None if
    that particular reading (or LibreHardwareMonitor itself) isn't
    available -- callers show "--" rather than treating None as zero.
    """
    try:
        response = requests.get(LIBRE_HARDWARE_MONITOR_URL, timeout=1.5)
        response.raise_for_status()
        root = response.json()
    except Exception:
        return None, None

    cpu_temp = _parse_temp_value(
        _find_sensor_value(root, _CPU_TEMP_SENSOR_ID, _CPU_TEMP_SENSOR_NAME, sensor_type="Temperature")
    )
    gpu_temp = _parse_temp_value(
        _find_sensor_value(root, _GPU_TEMP_SENSOR_ID, _GPU_TEMP_SENSOR_NAME, sensor_type="Temperature")
    )
    return cpu_temp, gpu_temp


def ensure_hardware_monitor_running():
    """
    Launch LibreHardwareMonitor in the background if it isn't already
    running (checked by trying its web server, not just the process list,
    since that's what actually matters). It starts minimized to the tray
    per LibreHardwareMonitor.config, so this is silent -- no window
    appears. Safe to call every time Jarvis starts; a no-op if it's
    already up (e.g. danny started it by hand, or a previous Jarvis
    session already launched it).
    """
    try:
        requests.get(LIBRE_HARDWARE_MONITOR_URL, timeout=1.0)
        return  # already running and serving data
    except Exception:
        pass

    if not os.path.exists(LIBRE_HARDWARE_MONITOR_EXE):
        print(f"LibreHardwareMonitor not found at {LIBRE_HARDWARE_MONITOR_EXE} -- CPU/GPU temps will show as --.")
        return

    try:
        subprocess.Popen(
            [LIBRE_HARDWARE_MONITOR_EXE],
            cwd=LIBRE_HARDWARE_MONITOR_DIR,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        print("Started LibreHardwareMonitor in the background for real CPU/GPU temps.")
    except Exception as error:
        print(f"Could not start LibreHardwareMonitor: {error}")


def _get_hardware_names():
    """
    CPU/GPU model names via WMI, fetched once and cached (they never
    change while Jarvis is running).
    """
    global _CPU_NAME, _GPU_NAME
    if _CPU_NAME is not None:
        return _CPU_NAME, _GPU_NAME
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Processor).Name; '---'; "
             "(Get-CimInstance Win32_VideoController).Name"],
            capture_output=True, text=True, timeout=10,
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if "---" in lines:
            split = lines.index("---")
            cpu_lines, gpu_lines = lines[:split], lines[split + 1:]
        else:
            cpu_lines, gpu_lines = lines, []
        _CPU_NAME = cpu_lines[0] if cpu_lines else "Unknown CPU"
        # Prefer a real GPU over a virtual/remote-desktop display adapter.
        real_gpus = [g for g in gpu_lines if "virtual" not in g.lower()]
        _GPU_NAME = (real_gpus or gpu_lines or ["Unknown GPU"])[0]
    except Exception as error:
        print("Hardware name lookup failed:", error)
        _CPU_NAME = _CPU_NAME or "Unknown CPU"
        _GPU_NAME = _GPU_NAME or "Unknown GPU"
    return _CPU_NAME, _GPU_NAME


def get_system_stats():
    """Real, live system stats -- no placeholder/fabricated values."""
    global _net_last_sample

    cpu_name, gpu_name = _get_hardware_names()
    cpu_temp_c, gpu_temp_c = get_hardware_monitor_temps()

    try:
        cpu_percent = psutil.cpu_percent(interval=None)
    except Exception:
        cpu_percent = None

    ram = None
    try:
        mem = psutil.virtual_memory()
        ram = {
            "used_gb": round(mem.used / (1024 ** 3), 1),
            "total_gb": round(mem.total / (1024 ** 3), 1),
            "percent": round(mem.percent, 1),
        }
    except Exception:
        pass

    drives = []
    try:
        for part in psutil.disk_partitions(all=False):
            if not part.fstype:
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            drives.append({
                "letter": part.device.rstrip("\\"),
                "used_gb": round(usage.used / (1024 ** 3), 1),
                "total_gb": round(usage.total / (1024 ** 3), 1),
                "percent": round(usage.percent, 1),
            })
    except Exception as error:
        print("Drive enumeration for stats failed:", error)

    down_kbps = up_kbps = 0.0
    try:
        counters = psutil.net_io_counters()
        now = time.time()
        with _dashboard_lock:
            if _net_last_sample is not None:
                last_time, last_sent, last_recv = _net_last_sample
                elapsed = max(now - last_time, 0.001)
                down_kbps = max(0.0, (counters.bytes_recv - last_recv) / elapsed / 1024)
                up_kbps = max(0.0, (counters.bytes_sent - last_sent) / elapsed / 1024)
                _net_history.append((round(down_kbps, 1), round(up_kbps, 1)))
            _net_last_sample = (now, counters.bytes_sent, counters.bytes_recv)
    except Exception as error:
        print("Network stats failed:", error)

    return {
        "hostname": os.environ.get("COMPUTERNAME", "This PC"),
        "cpu_name": cpu_name,
        "cpu_percent": cpu_percent,
        "cpu_temp_c": cpu_temp_c,
        "gpu_name": gpu_name,
        "gpu_temp_c": gpu_temp_c,
        "ram": ram,
        "drives": drives,
        "net_down_kbps": round(down_kbps, 1),
        "net_up_kbps": round(up_kbps, 1),
        "net_history": list(_net_history),
    }


# ---- Live activity: step-by-step "what is Jarvis doing right now" ----

def start_activity(step_labels, title=None):
    """
    Begin a new tracked activity with a known sequence of step labels; the
    first step starts "active", the rest "pending". Call advance_activity()
    at each real checkpoint as work actually happens -- these reflect
    genuine code checkpoints, not a decorative animation.
    """
    with _dashboard_lock:
        _activity_steps.clear()
        for index, label in enumerate(step_labels):
            _activity_steps.append({"label": label, "status": "active" if index == 0 else "pending"})
        _activity_current_action.clear()
        if title:
            _activity_current_action["title"] = title


def advance_activity(detail=None, **fields):
    """Mark the current active step done and move to the next pending one."""
    with _dashboard_lock:
        active_index = next((i for i, s in enumerate(_activity_steps) if s["status"] == "active"), None)
        if active_index is not None:
            _activity_steps[active_index]["status"] = "done"
            if active_index + 1 < len(_activity_steps):
                _activity_steps[active_index + 1]["status"] = "active"
        if detail is not None:
            _activity_current_action["detail"] = detail
        for key, value in fields.items():
            _activity_current_action[key] = value


def update_current_action(**fields):
    """Update Current Action fields (location/items_scanned/matches/etc.) without moving steps."""
    with _dashboard_lock:
        for key, value in fields.items():
            _activity_current_action[key] = value


def finish_activity():
    """Mark every step done -- the request has fully completed (or was superseded)."""
    with _dashboard_lock:
        for step in _activity_steps:
            step["status"] = "done"


def log_recent_action(text):
    """Append one line to the Recent Actions log with a real timestamp."""
    if not text:
        return
    with _dashboard_lock:
        _recent_actions.appendleft({
            "time": datetime.datetime.now().strftime("%H:%M"),
            "text": str(text)[:160],
        })


def get_activity_snapshot():
    with _dashboard_lock:
        return {
            "steps": [dict(s) for s in _activity_steps],
            "current_action": dict(_activity_current_action),
            "recent": list(_recent_actions),
        }


MOBILE_PAGE = r"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#111;color:#eee;margin:0;padding:24px}
main{max-width:600px;margin:auto}.card{background:#1c1c1e;border-radius:20px;padding:20px}
h1{margin-top:0}#reply{white-space:pre-wrap;min-height:70px;padding:14px;background:#27272a;border-radius:12px;margin:14px 0}
input,button{font-size:18px;border-radius:12px;padding:14px;border:0}
input{width:100%;box-sizing:border-box;margin-bottom:10px}
button{width:49%;cursor:pointer} .status{opacity:.7;font-size:14px;margin:10px 0}
</style></head><body><main><div class="card">
<h1>Jarvis</h1><div class="status" id="status">Connected to your Jarvis PC</div>
<div id="reply">Ready.</div>
<input id="command" placeholder="Type a command for Jarvis">
<div><button onclick="sendCommand()">Send</button><button onclick="startVoice()">🎤 Speak</button><button onclick="stopJarvis()">Stop</button></div>
</div></main>
<script>
function isStopCommand(command){
  const c=(command || '').trim().toLowerCase().replace(/[^a-z0-9' ]+/g,' ').replace(/\s+/g,' ');
  return ['stop','jarvis stop','stop jarvis','be quiet','jarvis be quiet','quiet','cancel','jarvis cancel'].includes(c);
}
function stopPhoneSpeech(){
  if('speechSynthesis' in window){
    speechSynthesis.cancel();
  }
}
async function stopJarvis(){
  stopPhoneSpeech();
  document.getElementById('status').textContent='Stopping Jarvis...';
  try{
    const r=await fetch('/stop',{method:'POST'});
    const d=await r.json();
    document.getElementById('reply').textContent=d.reply || d.error || 'Jarvis stopped.';
    document.getElementById('status').textContent='Ready';
  }catch(e){
    document.getElementById('status').textContent='Stop sent locally; connection error: '+e.message;
  }
}
async function sendCommand(text){
  const box=document.getElementById('command');
  const command=(text || box.value).trim(); if(!command)return;
  box.value='';
  if(isStopCommand(command)){
    // Cancel the iPhone's own speech immediately. Do not wait for the PC.
    stopPhoneSpeech();
    await stopJarvis();
    return;
  }
  // Starting a new request also cancels any old reply still speaking.
  stopPhoneSpeech();
  document.getElementById('status').textContent='Jarvis is thinking...';
  try{
    const r=await fetch('/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command})});
    const d=await r.json(); document.getElementById('reply').textContent=d.reply || d.error || 'No reply.';
    document.getElementById('status').textContent='Ready';
    if(d.reply && 'speechSynthesis' in window){ stopPhoneSpeech(); speechSynthesis.speak(new SpeechSynthesisUtterance(d.reply)); }
  }catch(e){document.getElementById('status').textContent='Connection error: '+e.message;}
}
document.getElementById('command').addEventListener('keydown',e=>{if(e.key==='Enter')sendCommand();});
function startVoice(){
  const R=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!R){document.getElementById('status').textContent='Voice input is not available in this browser.';return;}
  const rec=new R(); rec.lang='en-GB'; rec.interimResults=false; rec.maxAlternatives=1;
  document.getElementById('status').textContent='Listening...';
  rec.onresult=e=>sendCommand(e.results[0][0].transcript);
  rec.onerror=e=>document.getElementById('status').textContent='Voice error: '+e.error;
  rec.start();
}
</script></body></html>"""

class JarvisMobileHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_OPTIONS(self):
        # CORS preflight -- the desktop HUD is a file:// page posting JSON
        # cross-origin to this server, which browsers preflight with OPTIONS.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/status":
            self._send_json({
                "speaking": jarvis_speaking.is_set(),
                "processing": jarvis_processing.is_set(),
            })
            return

        if self.path == "/stats":
            try:
                self._send_json(get_system_stats())
            except Exception as error:
                self._send_json({"error": str(error)}, status=500)
            return

        if self.path == "/activity":
            try:
                self._send_json(get_activity_snapshot())
            except Exception as error:
                self._send_json({"error": str(error)}, status=500)
            return

        if self.path != "/":
            self.send_error(404)
            return
        data = MOBILE_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path == "/stop":
            try:
                stop_jarvis_speaking()
                body = json.dumps({"reply": "Jarvis stopped."}, ensure_ascii=True).encode("ascii")
                self.send_response(200)
            except Exception as error:
                body = json.dumps({"error": str(error)}, ensure_ascii=True).encode("ascii")
                self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path != "/command":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            command = str(payload.get("command", "")).strip()
            if not command:
                raise ValueError("No command supplied")

            reply_queue = queue.Queue(maxsize=1)
            remote_commands.put((command, reply_queue))
            reply = reply_queue.get(timeout=REMOTE_COMMAND_TIMEOUT)
            body = json.dumps({"reply": reply}).encode("utf-8")
            self.send_response(200)
        except queue.Empty:
            body = json.dumps({"error": "Jarvis took too long to reply."}).encode("utf-8")
            self.send_response(504)
        except Exception as error:
            body = json.dumps({"error": str(error)}).encode("utf-8")
            self.send_response(400)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

def start_mobile_server():
    if not REMOTE_COMMANDS_ENABLED:
        return
    try:
        server = http.server.ThreadingHTTPServer(("0.0.0.0", 8765), JarvisMobileHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print("\nJarvis Mobile is running on port 8765.")
        print("Open http://YOUR-PC-TAILSCALE-IP:8765 on your iPhone.")
    except Exception as error:
        print("\nJarvis Mobile server error:", error)
typed_buffer = ""
typed_prompt_shown = False
jarvis_speaking = threading.Event()
jarvis_stop_requested = threading.Event()
jarvis_processing = threading.Event()  # set from receiving a command until the next one is awaited — lets a UI show a "thinking" state
current_tts_engine = None
current_tts_backend = None  # "elevenlabs" or "pyttsx3" — which one is speaking right now
current_tts_lock = threading.Lock()
tts_run_lock = threading.Lock()
tts_generation = 0

# ============================================================
# ELEVENLABS VOICE (optional — falls back to pyttsx3 if unset/unreachable)
# ============================================================

ELEVENLABS_API_KEY = os.environ.get("JARVIS_ELEVENLABS_API_KEY", "").strip()
# Danny's chosen voice from the ElevenLabs Voice Library — confirmed live.
ELEVENLABS_VOICE_ID = os.environ.get("JARVIS_ELEVENLABS_VOICE_ID", "xru6qZB94sJdkyqP12qN")
ELEVENLABS_MODEL = os.environ.get("JARVIS_ELEVENLABS_MODEL", "eleven_turbo_v2_5")
ELEVENLABS_SAMPLE_RATE = 24000
# 1.0 is ElevenLabs' normal pace; danny found that a touch slow. 1.15 is a
# ~5% speedup (range is roughly 0.7-1.2) — adjust via env var without a
# code change if it still needs tuning either way.
ELEVENLABS_SPEED = float(os.environ.get("JARVIS_ELEVENLABS_SPEED", "1.15"))


def _synthesize_with_elevenlabs(text):
    """
    Call ElevenLabs' text-to-speech API and return (samples, sample_rate)
    as a numpy int16 array, or None on any failure — caller falls back to
    pyttsx3 so a network hiccup never leaves Jarvis silent.
    """
    if not ELEVENLABS_API_KEY:
        return None
    try:
        response = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            params={"output_format": f"pcm_{ELEVENLABS_SAMPLE_RATE}"},
            json={
                "text": text,
                "model_id": ELEVENLABS_MODEL,
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": ELEVENLABS_SPEED},
            },
            timeout=30,
        )
        response.raise_for_status()
        samples = np.frombuffer(response.content, dtype=np.int16)
        if samples.size == 0:
            return None
        return samples, ELEVENLABS_SAMPLE_RATE
    except Exception as error:
        print("\nElevenLabs TTS error (falling back to the local voice):", error)
        return None

microphone_lock = threading.Lock()


def _record_audio(seconds, sample_rate=16000):
    recording = sd.rec(
        int(seconds * sample_rate),
        samplerate=sample_rate,
        channels=1,
        dtype="int16",
    )
    sd.wait()
    return sr.AudioData(recording.tobytes(), sample_rate, 2)


def _recognise(audio):
    recognizer = sr.Recognizer()
    return recognizer.recognize_google(audio, language="en-GB")


def wake_word_listener():
    """Continuously listen for the wake word and queue voice commands."""
    if not (VOICE_INPUT_ENABLED and SPEECH_RECOGNITION_AVAILABLE and SOUNDDEVICE_AVAILABLE):
        return

    print("\nVoice wake word ready. Say 'Jarvis' followed by your command.")

    while True:
        try:
            # Do not let Jarvis hear his own spoken replies.
            while jarvis_speaking.is_set():
                time.sleep(0.1)

            try:
                with microphone_lock:
                    if jarvis_speaking.is_set():
                        continue
                    audio = _record_audio(2.5)

                heard = _recognise(audio).strip()
            except (sr.UnknownValueError, sr.RequestError):
                continue
            except Exception as error:
                print("\nMicrophone listener error:", error)
                time.sleep(1)
                continue

            lowered = heard.lower()
            if WAKE_WORD not in lowered:
                continue

            # "Jarvis, open Steam" — command is already included.
            after_wake = lowered.split(WAKE_WORD, 1)[1].strip(" ,.!?")
            if after_wake:
                print(f"\nWake word detected. You: {heard}")
                voice_commands.put(after_wake)
                continue

            # "Jarvis" — now listen for the command.
            print("\nJarvis is listening...")
            try:
                with microphone_lock:
                    if jarvis_speaking.is_set():
                        continue
                    command_audio = _record_audio(8)

                command = _recognise(command_audio).strip()
                if command:
                    print("You:", command)
                    voice_commands.put(command)
            except (sr.UnknownValueError, sr.RequestError):
                print("\nI didn't catch that. Returning to wake word mode.")
            except Exception as error:
                print("\nCommand microphone error:", error)
                time.sleep(1)

        except Exception as error:
            print("\nVoice listener recovered from an error:", error)
            time.sleep(1)

def start_voice_listener():
    if not (VOICE_INPUT_ENABLED and SPEECH_RECOGNITION_AVAILABLE and SOUNDDEVICE_AVAILABLE):
        return
    thread = threading.Thread(target=wake_word_listener, daemon=True)
    thread.start()


def poll_interrupt_inputs():
    """Check for Esc and queued voice/phone stop commands while Jarvis speaks."""
    if not jarvis_speaking.is_set():
        return False

    try:
        if msvcrt.kbhit():
            char = msvcrt.getwch()
            if char == "\x1b":
                stop_jarvis_speaking()
                print("\nJarvis stopped.")
                return True
    except Exception:
        pass

    return False


def get_user_input():
    """
    Accept typed commands at any time, while the background thread waits
    for the wake word 'Jarvis'. Voice commands are only accepted after
    the wake word has been detected.
    """
    global typed_buffer, typed_prompt_shown, active_remote_reply

    # Phone commands arrive here and then use the exact same command flow
    # as typed and wake-word commands.
    try:
        message, reply_queue = remote_commands.get_nowait()
        with active_remote_lock:
            active_remote_reply = reply_queue
        typed_buffer = ""
        typed_prompt_shown = False
        print("\nPhone:", message)
        return message
    except queue.Empty:
        pass

    try:
        message = voice_commands.get_nowait()
        # A voice command takes priority; reset the typed prompt state.
        typed_buffer = ""
        typed_prompt_shown = False
        return message
    except queue.Empty:
        pass

    if msvcrt is None:
        if not typed_prompt_shown:
            typed_prompt_shown = True
            return input("\nYou: ")
        return ""

    if not typed_prompt_shown:
        print("\nYou: ", end="", flush=True)
        typed_prompt_shown = True

    if msvcrt.kbhit():
        char = msvcrt.getwch()

        if char == "\x1b":  # Esc
            stop_jarvis_speaking()
            typed_buffer = ""
            typed_prompt_shown = False
            print("\nJarvis stopped.")
            return ""
        if char in ("\r", "\n"):
            print()
            message = typed_buffer.strip()
            typed_buffer = ""
            typed_prompt_shown = False
            return message

        if char == "\x08":  # Backspace
            if typed_buffer:
                typed_buffer = typed_buffer[:-1]
                print("\b \b", end="", flush=True)
        elif char in ("\x00", "\xe0"):
            # Ignore special-key prefix and its following key code.
            if msvcrt.kbhit():
                msvcrt.getwch()
        else:
            typed_buffer += char
            print(char, end="", flush=True)

    time.sleep(0.05)
    return ""


def get_confirmation_input(prompt_text="\nYou: "):
    """
    Block until the user answers a yes/no style confirmation (shutdown,
    restart, etc.). Unlike a plain input() call, this also accepts the
    answer over the phone/HUD remote-command channel or voice, so
    confirmations still work when the desktop app has hidden the console
    window and the user can't see or click into it to type.
    """
    global typed_buffer, typed_prompt_shown, active_remote_reply

    while True:
        try:
            message, reply_queue = remote_commands.get_nowait()
            with active_remote_lock:
                active_remote_reply = reply_queue
            typed_buffer = ""
            typed_prompt_shown = False
            print("\nPhone:", message)
            return message.strip()
        except queue.Empty:
            pass

        try:
            message = voice_commands.get_nowait()
            typed_buffer = ""
            typed_prompt_shown = False
            return message.strip()
        except queue.Empty:
            pass

        if msvcrt is not None:
            try:
                if msvcrt.kbhit():
                    return input(prompt_text).strip()
            except OSError:
                # No console attached (e.g. launched with the window
                # hidden) -- fall through and keep polling the phone/HUD
                # and voice channels instead of crashing.
                pass

        time.sleep(0.1)

# ============================================================
# JARVIS SETTINGS
# ============================================================

# Local AI brain. gpt-oss:20b is the primary conversation model — pull it
# once with "ollama pull gpt-oss:20b" before starting Jarvis.
MODEL = os.environ.get("JARVIS_OLLAMA_MODEL", "gpt-oss:20b")
# A smaller/faster model used only for short classification and extraction
# calls (routing decisions, target-name cleanup) that don't need gpt-oss:20b's
# deeper reasoning. This machine's GPU can't keep both gpt-oss:20b (~13GB)
# and the vision model (~6GB) loaded at once, so every call on the big model
# risks a ~15-20s reload; routing the frequent small calls to a smaller
# model reduces both how often that swap happens and how long it takes.
OLLAMA_FAST_MODEL = os.environ.get("JARVIS_OLLAMA_FAST_MODEL", "qwen3:8b")
OLLAMA_HOST = os.environ.get("JARVIS_OLLAMA_HOST", "http://localhost:11434")
OLLAMA_URL = f"{OLLAMA_HOST}/api/generate"
OLLAMA_CHAT_URL = f"{OLLAMA_HOST}/api/chat"
# gpt-oss:20b is a much bigger model than qwen3:8b, so give it real headroom
# before deciding the local brain is unreachable.
OLLAMA_TIMEOUT = int(os.environ.get("JARVIS_OLLAMA_TIMEOUT", "120"))
# Ollama's own default is to unload a model from VRAM after just 5 minutes
# of inactivity ("ollama ps" shows the countdown) -- completely separate
# from, and in addition to, every other speed fix in this file. Any gap
# longer than that between messages (normal in real conversation -- typing,
# thinking) pays the full ~12GB model-load cost all over again, which is
# exactly the "the second time took 3x longer" pattern danny hit. Every
# Ollama call below passes this as "keep_alive" so the model's resident
# window resets on every use instead of decaying after 5 minutes; "-1"
# would never unload it at all, but 30m already covers realistic gaps
# between messages while still freeing the ~12GB of VRAM automatically if
# Jarvis genuinely goes unused for a while (e.g. danny closes it to game).
OLLAMA_KEEP_ALIVE = os.environ.get("JARVIS_OLLAMA_KEEP_ALIVE", "30m")


def unload_ollama_models():
    """
    Immediately unload the local LLM(s) from VRAM/RAM ahead of launching a
    game -- danny's explicit request: the ~12GB gpt-oss:20b model needs to
    be dropped from memory before a game starts, so the game gets full use
    of that memory instead of competing with an idle model still resident
    thanks to OLLAMA_KEEP_ALIVE. Ollama's own API supports this directly:
    a generate call with keep_alive=0 and no prompt unloads that model
    without running any real inference or costing anything.

    Called as early as possible in a game-launch flow (see
    _steam_play_current_game / handle_xbox_game_command) so the actual OS
    memory reclamation has the maximum time to finish in the background
    before the game's own asset loading kicks in -- each request still
    gets a short timeout so a slow/unreachable Ollama can never delay the
    game launch itself.
    """
    for model in (MODEL, OLLAMA_FAST_MODEL, OLLAMA_VISION_MODEL):
        try:
            requests.post(
                f"{OLLAMA_HOST}/api/generate",
                json={"model": model, "keep_alive": 0},
                timeout=3,
            )
            print(f"Unloaded {model} from memory ahead of launching a game.")
        except Exception as error:
            print(f"Could not unload {model} before launching a game: {error}")


# ============================================================
# VOICE FUNCTION
# ============================================================

def stop_jarvis_speaking():
    """Immediately request speech cancellation from voice, keyboard, or phone."""
    global current_tts_engine
    jarvis_stop_requested.set()
    with current_tts_lock:
        engine = current_tts_engine
        backend = current_tts_backend
    if backend == "elevenlabs" and SOUNDDEVICE_AVAILABLE:
        try:
            sd.stop()
        except Exception:
            pass
    if engine is not None:
        try:
            engine.stop()
        except Exception:
            pass


def is_stop_command(message):
    cleaned = re.sub(r"[^a-z0-9' ]+", " ", str(message).lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned in {
        "stop", "jarvis stop", "stop jarvis",
        "be quiet", "jarvis be quiet", "quiet",
        "cancel", "jarvis cancel"
    }


def _speak_chunk_blocking(text):
    """
    Synthesize and play ONE piece of text, blocking until playback
    finishes (or stop_jarvis_speaking() interrupts it). ElevenLabs first,
    falling back to the local pyttsx3 voice on any failure so Jarvis is
    never silent. Shared by say() (one-shot messages) and _speak_stream()
    (sentence-by-sentence streamed replies) so both backends behave
    identically per chunk.
    """
    global current_tts_engine, current_tts_backend
    if jarvis_stop_requested.is_set():
        return
    engine = None
    try:
        synth = None
        if ELEVENLABS_API_KEY and SOUNDDEVICE_AVAILABLE:
            synth = _synthesize_with_elevenlabs(text)

        if jarvis_stop_requested.is_set():
            return

        if synth is not None:
            samples, sample_rate = synth
            with current_tts_lock:
                current_tts_backend = "elevenlabs"
            if jarvis_stop_requested.is_set():
                return
            sd.play(samples, samplerate=sample_rate)
            sd.wait()
        else:
            # ElevenLabs unset or unreachable this time — fall back to the
            # local voice so Jarvis is never silent.
            engine = pyttsx3.init()
            with current_tts_lock:
                current_tts_engine = engine
                current_tts_backend = "pyttsx3"

            voices = engine.getProperty("voices")
            selected_voice = next(
                (voice.id for voice in voices if "david" in voice.name.lower()),
                voices[0].id if voices else None
            )
            if selected_voice:
                engine.setProperty("voice", selected_voice)

            engine.setProperty("rate", 175)
            engine.setProperty("volume", 1.0)
            engine.say(text)
            engine.runAndWait()
    except Exception as e:
        print("\nJarvis voice error:", e)
    finally:
        try:
            if engine is not None:
                engine.stop()
        except Exception:
            pass
        with current_tts_lock:
            if current_tts_engine is engine:
                current_tts_engine = None
            current_tts_backend = None


def _finish_speech_generation(my_generation):
    """
    Shared end-of-response bookkeeping for both say() and _speak_stream():
    only the newest speech worker may clear the global speaking state (an
    older, interrupted worker must not make the microphone start listening
    while a newer response is still queued/running).
    """
    stopped = False
    with current_tts_lock:
        if my_generation == tts_generation:
            jarvis_speaking.clear()
            stopped = jarvis_stop_requested.is_set()
            if stopped:
                jarvis_stop_requested.clear()
    if stopped:
        print("\nJarvis stopped.")
    if my_generation == tts_generation:
        print("\nVoice wake word listening again.")


def say(text):
    """Print and speak without blocking Jarvis's input loop.

    TTS is serialized so pyttsx3 can never have two runAndWait loops active
    at the same time. A generation counter prevents an older speech worker
    from clearing the speaking state of a newer response.
    """
    global active_remote_reply, tts_generation
    with current_tts_lock:
        tts_generation += 1
        my_generation = tts_generation

    text = str(text)
    print("\nJarvis:", text)
    log_recent_action(text)

    with active_remote_lock:
        if active_remote_reply is not None:
            try:
                active_remote_reply.put_nowait(text)
            except queue.Full:
                pass
            active_remote_reply = None

    # Stop any previous speech before starting a new response.
    if jarvis_speaking.is_set():
        stop_jarvis_speaking()

    jarvis_stop_requested.clear()
    jarvis_speaking.set()

    def speak_worker():
        # Serialize workers so two speech backends never run at once;
        # stop_jarvis_speaking() can still interrupt whichever one is
        # currently active while a newer response waits here.
        with tts_run_lock:
            _speak_chunk_blocking(text)
        _finish_speech_generation(my_generation)

    threading.Thread(target=speak_worker, daemon=True).start()


class _ChunkQueueIterable:
    """
    A queue a producer (the streaming Ollama reader) feeds sentence chunks
    into, iterated by a single consumer worker — lets the worker start
    playing chunk 1 while the producer is still generating chunk 2,
    instead of waiting for the entire reply before saying anything.
    """

    def __init__(self):
        self._q = queue.Queue()

    def put(self, chunk):
        self._q.put(chunk)

    def finish(self):
        self._q.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        if jarvis_stop_requested.is_set():
            raise StopIteration
        item = self._q.get()
        if item is None or jarvis_stop_requested.is_set():
            raise StopIteration
        return item


def _speak_stream(chunk_iterable):
    """
    Speak a sequence of text chunks back-to-back as ONE logical response.
    Behaves like say() at the boundaries (stops whatever was playing
    before, keeps jarvis_speaking set for the whole sequence, can be
    interrupted mid-sequence by stop_jarvis_speaking()), but plays each
    chunk as soon as it's queued instead of waiting for the full text —
    this is what lets Jarvis start talking almost immediately even for a
    long reply (e.g. a story) instead of going silent for the entire
    generation time.
    """
    global tts_generation
    with current_tts_lock:
        tts_generation += 1
        my_generation = tts_generation

    if jarvis_speaking.is_set():
        stop_jarvis_speaking()

    jarvis_stop_requested.clear()
    jarvis_speaking.set()

    def worker():
        with tts_run_lock:
            for chunk in chunk_iterable:
                if jarvis_stop_requested.is_set():
                    break
                _speak_chunk_blocking(chunk)
        _finish_speech_generation(my_generation)

    threading.Thread(target=worker, daemon=True).start()


# ============================================================
# JARVIS STARTUP
# ============================================================


def _time_of_day_greeting():
    hour = datetime.datetime.now().hour
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


# Danny's own picks. "Good morning" is reserved for actual mornings (see
# _pick_startup_greeting below); the rest work at any hour.
_MORNING_ONLY_GREETINGS = [
    "Good morning. All systems are optimized and your schedule is cleared for greatness.",
]

_ANYTIME_GREETINGS = [
    "Online and fully operational. What's the play today, boss?",
    "Systems check complete. The world isn't going to save itself. Ready when you are.",
    "We are live. Try not to break anything today.",
]


def _pick_startup_greeting():
    pool = list(_ANYTIME_GREETINGS)
    if _time_of_day_greeting() == "morning":
        pool += _MORNING_ONLY_GREETINGS
    return random.choice(pool)


print("Jarvis is starting...")

say(_pick_startup_greeting())

print("Type 'exit' to quit.")

start_voice_listener()
start_mobile_server()
ensure_hardware_monitor_running()


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def open_program(program):
    """
    Open a Windows program.
    """

    try:

        subprocess.Popen(
            program
        )

        return True

    except Exception:

        return False


def open_folder(folder):
    """
    Open a folder in File Explorer.
    """

    try:

        os.startfile(
            folder
        )

        return True

    except Exception:

        return False


def open_url(url):
    """
    Open a website.
    """

    try:

        webbrowser.open(
            url
        )

        return True

    except Exception:

        return False


def get_start_apps():
    """
    Get applications that Windows knows about
    from the Start Menu.

    This helps Jarvis find installed apps and games.
    """

    try:

        command = [

            "powershell",

            "-NoProfile",

            "-Command",

            "Get-StartApps | ConvertTo-Json -Compress"

        ]


        result = subprocess.run(

            command,

            capture_output=True,

            text=True,

            timeout=15

        )


        if result.returncode != 0:

            return []


        output = result.stdout.strip()


        if not output:

            return []


        apps = json.loads(
            output
        )


        if isinstance(
            apps,
            dict
        ):

            apps = [

                apps

            ]


        return apps


    except Exception:

        return []


def find_and_open_app(app_name):
    """
    Search Windows Start Menu apps and try to open
    the closest matching application.
    """

    apps = get_start_apps()


    if not apps:

        return None


    app_name = app_name.lower().strip()


    # ========================================================
    # FIRST: DIRECT MATCH
    # ========================================================

    for app in apps:

        name = app.get(
            "Name",
            ""
        )

        app_id = app.get(
            "AppID",
            ""
        )


        if app_name == name.lower():

            try:

                subprocess.Popen(

                    [

                        "explorer.exe",

                        f"shell:AppsFolder\\{app_id}"

                    ]

                )


                return name


            except Exception:

                pass


    # ========================================================
    # SECOND: PARTIAL MATCH
    # ========================================================

    possible_matches = []


    for app in apps:

        name = app.get(
            "Name",
            ""
        )

        app_id = app.get(
            "AppID",
            ""
        )


        if app_name in name.lower():

            possible_matches.append(

                {

                    "Name": name,

                    "AppID": app_id

                }

            )


    if possible_matches:


        match = possible_matches[0]


        try:

            subprocess.Popen(

                [

                    "explorer.exe",

                    f"shell:AppsFolder\\{match['AppID']}"

                ]

            )


            return match[
                "Name"
            ]


        except Exception:

            pass


    # ========================================================
    # THIRD: FUZZY MATCH
    # ========================================================

    app_names = [

        app.get(
            "Name",
            ""
        )

        for app in apps

    ]


    close_matches = difflib.get_close_matches(

        app_name,

        app_names,

        n=1,

        cutoff=0.6

    )


    if close_matches:


        best_match = close_matches[0]


        for app in apps:


            if app.get(
                "Name",
                ""
            ) == best_match:


                app_id = app.get(
                    "AppID",
                    ""
                )


                try:

                    subprocess.Popen(

                        [

                            "explorer.exe",

                            f"shell:AppsFolder\\{app_id}"

                        ]

                    )


                    return best_match


                except Exception:

                    pass


    return None


# ============================================================
# OPEN COMMON WINDOWS PROGRAMS
# ============================================================

def handle_program_commands(command):


    # ========================================================
    # CALCULATOR
    # ========================================================

    calculator_commands = [

        "open calculator",

        "launch calculator",

        "start calculator",

        "open calc",

        "launch calc",

        "start calc",

        "calculator"

    ]


    if command in calculator_commands:


        say(
            "Opening Calculator."
        )


        open_program(

            [

                "calc.exe"

            ]

        )


        return True


    # ========================================================
    # NOTEPAD
    # ========================================================

    notepad_commands = [

        "open notepad",

        "launch notepad",

        "start notepad",

        "notepad"

    ]


    if command in notepad_commands:


        say(
            "Opening Notepad."
        )


        open_program(

            [

                "notepad.exe"

            ]

        )


        return True


    # ========================================================
    # FILE EXPLORER
    # ========================================================

    explorer_commands = [

        "open file explorer",

        "launch file explorer",

        "start file explorer",

        "open explorer",

        "open files",

        "open my files",

        "show my files",

        "show files",

        "my files",

        "file explorer"

    ]


    if command in explorer_commands:


        say(
            "Opening File Explorer."
        )


        open_program(

            [

                "explorer.exe"

            ]

        )


        return True


    # ========================================================
    # JARVIS FOLDER
    # ========================================================

    jarvis_folder_commands = [

        "open jarvis folder",

        "open the jarvis folder",

        "show jarvis folder",

        "show the jarvis folder",

        "open my jarvis folder"

    ]


    if command in jarvis_folder_commands:


        say(
            "Opening the Jarvis folder."
        )


        jarvis_folder = os.path.join(

            os.environ[
                "USERPROFILE"
            ],

            "Desktop",

            "Jarvis"

        )


        open_folder(
            jarvis_folder
        )


        return True


    # ========================================================
    # DOWNLOADS
    # ========================================================

    downloads_commands = [

        "open downloads",

        "show downloads",

        "open my downloads",

        "show my downloads",

        "downloads"

    ]


    if command in downloads_commands:


        say(
            "Opening Downloads."
        )


        downloads = os.path.join(

            os.environ[
                "USERPROFILE"
            ],

            "Downloads"

        )


        open_folder(
            downloads
        )


        return True


    # ========================================================
    # DOCUMENTS
    # ========================================================

    documents_commands = [

        "open documents",

        "show documents",

        "open my documents",

        "show my documents",

        "documents"

    ]


    if command in documents_commands:


        say(
            "Opening Documents."
        )


        documents = os.path.join(

            os.environ[
                "USERPROFILE"
            ],

            "Documents"

        )


        open_folder(
            documents
        )


        return True


    # ========================================================
    # DESKTOP
    # ========================================================

    desktop_commands = [

        "open desktop",

        "show desktop",

        "open my desktop",

        "show my desktop"

    ]


    if command in desktop_commands:


        say(
            "Opening Desktop."
        )


        desktop = os.path.join(

            os.environ[
                "USERPROFILE"
            ],

            "Desktop"

        )


        open_folder(
            desktop
        )


        return True


    # ========================================================
    # PICTURES
    # ========================================================

    pictures_commands = [

        "open pictures",

        "show pictures",

        "open my pictures",

        "show my pictures"

    ]


    if command in pictures_commands:


        say(
            "Opening Pictures."
        )


        pictures = os.path.join(

            os.environ[
                "USERPROFILE"
            ],

            "Pictures"

        )


        open_folder(
            pictures
        )


        return True


    # ========================================================
    # VIDEOS
    # ========================================================

    videos_commands = [

        "open videos",

        "show videos",

        "open my videos",

        "show my videos"

    ]


    if command in videos_commands:


        say(
            "Opening Videos."
        )


        videos = os.path.join(

            os.environ[
                "USERPROFILE"
            ],

            "Videos"

        )


        open_folder(
            videos
        )


        return True


    # ========================================================
    # MUSIC
    # ========================================================

    music_commands = [

        "open music",

        "show music",

        "open my music",

        "show my music"

    ]


    if command in music_commands:


        say(
            "Opening Music."
        )


        music = os.path.join(

            os.environ[
                "USERPROFILE"
            ],

            "Music"

        )


        open_folder(
            music
        )


        return True


    return False


# ============================================================
# WEB COMMANDS
# ============================================================

def handle_web_commands(command):


    # ========================================================
    # OPEN BROWSER
    # ========================================================

    browser_commands = [

        "open browser",

        "launch browser",

        "start browser",

        "open my browser"

    ]


    if command in browser_commands:


        say(
            "Opening your web browser."
        )


        open_url(
            "https://www.google.com"
        )


        return True


    # ========================================================
    # OPEN GOOGLE
    # ========================================================

    google_commands = [

        "open google",

        "launch google",

        "start google",

        "google"

    ]


    if command in google_commands:


        say(
            "Opening Google."
        )


        open_url(
            "https://www.google.com"
        )


        return True


    # ========================================================
    # OPEN YOUTUBE
    # ========================================================

    youtube_commands = [

        "open youtube",

        "launch youtube",

        "start youtube",

        "youtube"

    ]


    if command in youtube_commands:


        say(
            "Opening YouTube."
        )


        open_url(
            "https://www.youtube.com"
        )


        return True


    # ========================================================
    # SEARCH GOOGLE
    # ========================================================

    google_prefixes = [

        "search google for ",

        "google ",

        "search for ",

        "search the web for ",

        "look up ",

        "look for "

    ]


    for prefix in google_prefixes:


        if command.startswith(
            prefix
        ):


            search = command.replace(

                prefix,

                "",

                1

            ).strip()


            if search:


                say(
                    "Searching Google for "
                    + search
                    + "."
                )


                url = (

                    "https://www.google.com/search?q="

                    + search.replace(
                        " ",
                        "+"
                    )

                )


                open_url(
                    url
                )


                return True


    # ========================================================
    # SEARCH YOUTUBE
    # ========================================================

    youtube_prefixes = [

        "search youtube for ",

        "search youtube ",

        "youtube search for ",

        "find on youtube "

    ]


    for prefix in youtube_prefixes:


        if command.startswith(
            prefix
        ):


            search = command.replace(

                prefix,

                "",

                1

            ).strip()


            if search:


                say(
                    "Searching YouTube for "
                    + search
                    + "."
                )


                url = (

                    "https://www.youtube.com/results?search_query="

                    + search.replace(
                        " ",
                        "+"
                    )

                )


                open_url(
                    url
                )


                return True


    return False


# ============================================================
# SYSTEM COMMANDS
# ============================================================

def handle_system_commands(command):


    # ========================================================
    # SETTINGS
    # ========================================================

    settings_commands = [

        "open settings",

        "launch settings",

        "start settings",

        "settings"

    ]


    if command in settings_commands:


        say(
            "Opening Settings."
        )


        subprocess.Popen(

            [

                "cmd",

                "/c",

                "start",

                "ms-settings:"

            ]

        )


        return True


    # ========================================================
    # TASK MANAGER
    # ========================================================

    task_manager_commands = [

        "open task manager",

        "launch task manager",

        "start task manager",

        "task manager"

    ]


    if command in task_manager_commands:


        say(
            "Opening Task Manager."
        )


        open_program(

            [

                "taskmgr.exe"

            ]

        )


        return True


    # ========================================================
    # CONTROL PANEL
    # ========================================================

    control_panel_commands = [

        "open control panel",

        "launch control panel",

        "start control panel",

        "control panel"

    ]


    if command in control_panel_commands:


        say(
            "Opening Control Panel."
        )


        open_program(

            [

                "control.exe"

            ]

        )


        return True


    # ========================================================
    # COMMAND PROMPT
    # ========================================================

    command_prompt_commands = [

        "open command prompt",

        "launch command prompt",

        "start command prompt",

        "open cmd",

        "launch cmd",

        "start cmd"

    ]


    if command in command_prompt_commands:


        say(
            "Opening Command Prompt."
        )


        open_program(

            [

                "cmd.exe"

            ]

        )


        return True


    # ========================================================
    # POWERSHELL
    # ========================================================

    powershell_commands = [

        "open powershell",

        "launch powershell",

        "start powershell"

    ]


    if command in powershell_commands:


        say(
            "Opening PowerShell."
        )


        open_program(

            [

                "powershell.exe"

            ]

        )


        return True


    # ========================================================
    # LOCK COMPUTER
    # ========================================================

    lock_commands = [

        "lock computer",

        "lock pc",

        "lock my computer",

        "lock my pc",

        "lock the computer"

    ]


    if command in lock_commands:


        say(
            "Locking your computer."
        )


        subprocess.Popen(

            [

                "rundll32.exe",

                "user32.dll,LockWorkStation"

            ]

        )


        return True


    return False


# ============================================================
# TIME AND DATE
# ============================================================

def handle_time_commands(command):


    time_commands = [

        "what time is it",

        "what is the time",

        "tell me the time",

        "current time",

        "time"

    ]


    if command in time_commands:


        now = datetime.datetime.now()


        current_time = now.strftime(
            "%I:%M %p"
        )


        say(
            "The time is "
            + current_time
            + "."
        )


        return True


    date_commands = [

        "what is the date",

        "what date is it",

        "what day is it",

        "today's date",

        "todays date",

        "current date",

        "date"

    ]


    if command in date_commands:


        today = datetime.datetime.now()


        current_date = today.strftime(

            "%A, %d %B %Y"

        )


        say(
            "Today is "
            + current_date
            + "."
        )


        return True


    return False


# ============================================================
# SHUTDOWN AND RESTART
# ============================================================

def handle_power_commands(command):


    shutdown_commands = [

        "shutdown",

        "shut down",

        "shutdown computer",

        "shut down computer",

        "shutdown pc",

        "shut down pc",

        "turn off computer",

        "turn off pc"

    ]


    if command in shutdown_commands:


        say(
            "Are you sure you want to shut down "
            "the computer? Please type yes or no."
        )


        answer = get_confirmation_input().lower()


        if answer in [

            "yes",

            "yeah",

            "y"

        ]:


            say(
                "Shutting down."
            )


            subprocess.Popen(

                [

                    "shutdown",

                    "/s",

                    "/t",

                    "5"

                ]

            )


        else:


            say(
                "Shutdown cancelled."
            )


        return True


    restart_commands = [

        "restart",

        "restart computer",

        "restart pc",

        "restart my computer",

        "restart my pc"

    ]


    if command in restart_commands:


        say(
            "Are you sure you want to restart "
            "the computer? Please type yes or no."
        )


        answer = get_confirmation_input().lower()


        if answer in [

            "yes",

            "yeah",

            "y"

        ]:


            say(
                "Restarting."
            )


            subprocess.Popen(

                [

                    "shutdown",

                    "/r",

                    "/t",

                    "5"

                ]

            )


        else:


            say(
                "Restart cancelled."
            )


        return True


    return False


# ============================================================
# SPECIAL APPS AND GAMES
# ============================================================

def handle_special_apps(command):


    # ========================================================
    # STEAM
    # ========================================================

    steam_commands = [

        "open steam",

        "launch steam",

        "start steam",

        "steam"

    ]


    if command in steam_commands:


        say(
            "Opening Steam."
        )


        found = find_and_open_app(
            "Steam"
        )


        if not found:


            say(
                "I couldn't find Steam "
                "in your installed applications."
            )


        return True


    # ========================================================
    # DISCORD
    # ========================================================

    discord_commands = [

        "open discord",

        "launch discord",

        "start discord",

        "discord"

    ]


    if command in discord_commands:


        say(
            "Opening Discord."
        )


        found = find_and_open_app(
            "Discord"
        )


        if not found:


            say(
                "I couldn't find Discord "
                "in your installed applications."
            )


        return True


    # ========================================================
    # SPOTIFY
    # ========================================================

    spotify_commands = [

        "open spotify",

        "launch spotify",

        "start spotify",

        "spotify"

    ]


    if command in spotify_commands:


        say(
            "Opening Spotify."
        )


        found = find_and_open_app(
            "Spotify"
        )


        if not found:


            say(
                "I couldn't find Spotify "
                "in your installed applications."
            )


        return True


    # ========================================================
    # EPIC GAMES
    # ========================================================

    epic_commands = [

        "open epic games",

        "launch epic games",

        "start epic games",

        "open epic",

        "launch epic"

    ]


    if command in epic_commands:


        say(
            "Opening Epic Games."
        )


        found = find_and_open_app(
            "Epic Games"
        )


        if not found:


            say(
                "I couldn't find Epic Games."
            )


        return True


    # ========================================================
    # XBOX
    # ========================================================

    xbox_commands = [

        "open xbox",

        "launch xbox",

        "start xbox"

    ]


    if command in xbox_commands:


        say(
            "Opening Xbox."
        )


        found = find_and_open_app(
            "Xbox"
        )


        if not found:


            say(
                "I couldn't find the Xbox app."
            )


        return True


    return False


# ============================================================
# LLM-ASSISTED LAUNCH-TARGET CLEANUP
#
# The handlers below match "open/launch/play X" by taking whatever follows
# the prefix, verbatim, as X. That breaks the moment a user adds a natural
# qualifier the original phrase lists didn't anticipate — e.g. "open
# hogwarts legacy in steam" fails because "in steam" gets treated as part
# of the app name. This step only runs when that literal lookup has ALREADY
# failed — it costs nothing (local Ollama, no OpenAI call) and can only
# rescue a command that was about to be reported as "not found" anyway, so
# it cannot break anything that already works.
# ============================================================

def llm_clean_launch_target(raw_target, original_command):
    """
    Ask the local AI to strip filler/location words from a failed app or
    game lookup, say whether it's really a Steam game request, and flag
    whether the request actually needs more than just opening something —
    and if so, name the specific destination, so it can be looked up in the
    (application, destination) skills index (see get_app_skill) BEFORE ever
    paying for research, even for a request worded completely differently
    from whatever taught Jarvis that destination originally.
    Returns (clean_target, sub_target, via_steam, needs_more_than_launch);
    ("", "", False, False) on any failure.
    """
    instructions = """
You clean up a spoken or typed request to open/launch/play something on a
Windows PC, so it can be looked up as an application or game name.

Return ONLY JSON in this exact shape:
{"target": "...", "sub_target": "...", "via_steam": true or false, "needs_more_than_launch": true or false}

Rules:
- "target" is the application or game name ONLY.
- Remove filler words ("please", "can you", "for me", "the", "a", "an").
- Remove phrases that only say WHERE to find it or HOW to launch it, such
  as "in steam", "on steam", "via steam", "using steam", "in the browser",
  "on my pc" — keep only the actual name.
- Fix obvious speech-to-text spelling mistakes if the intended word is
  clear (e.g. "libary" -> "library"), but do not guess wildly.
- "via_steam" is true only when the user mentioned Steam, or the target is
  clearly a PC game rather than a regular desktop application.
- "needs_more_than_launch" is true whenever the request wants something
  done AFTER opening the target too — navigating to a specific page or
  tab, clicking something, typing something, etc. (e.g. "open settings
  and go to display", "open notepad and type hello"). It is false when the
  request is simply to open/launch/play the target and nothing more. When
  true, "target" should still be your best guess at just the app/game name.
- "sub_target" is the SPECIFIC single page/section/tab/item to navigate to
  once "target" is open, when needs_more_than_launch is true — just its
  name, e.g. "Display", "Bluetooth", "Library" (not a full sentence).
  Empty string whenever needs_more_than_launch is false, or the request
  involves typing/dragging/something other than navigating to one place.
- If nothing meaningful can be extracted, return
  {"target": "", "sub_target": "", "via_steam": false, "needs_more_than_launch": false}.
"""
    try:
        raw = ask_ollama_brain(
            instructions,
            f"Original command: {original_command}\nName extracted so far (may be wrong): {raw_target}",
            json_mode=True,
            # Ollama may need to swap gpt-oss:20b back into memory if the
            # vision model (qwen3-vl:8b-instruct) ran more recently — that
            # cold load alone can take ~20s, so a 20s total timeout here was
            # spuriously failing this step and falling through to the much
            # more expensive full AI pipeline for no real reason.
            #
            # Deliberately NOT using OLLAMA_FAST_MODEL here: measured it
            # confusing target/sub_target on exactly the nuanced cases this
            # depends on ("setting and go to display" -> got target and
            # sub_target backwards; "network and internet settings" ->
            # failed to decompose at all). This extraction needs gpt-oss:20b's
            # better reasoning; the speed win isn't worth the accuracy loss.
            timeout=60,
        )
        data = json.loads(raw)
        target = str(data.get("target", "")).strip()
        sub_target = str(data.get("sub_target", "")).strip()
        via_steam = bool(data.get("via_steam", False))
        needs_more = bool(data.get("needs_more_than_launch", False))
        return target, sub_target, via_steam, needs_more
    except Exception as error:
        print("LAUNCH CLEANUP: local AI unavailable:", error)
        return "", "", False, False


def try_launch_with_llm_cleanup(raw_target, original_command):
    """
    Last-resort recovery when a literal "open/launch/play X" lookup found
    nothing. Cleans the target with the local AI and retries — first
    against any previously-learned Steam routine, then (if this looks like
    a Steam game) through the same v58/UFO² pipeline already used for
    composite tasks, then one more literal app search.

    If NONE of that resolves it either, this hands the ORIGINAL command to
    the full v58 research+UFO² pipeline as a genuine last resort, bypassing
    its usual composite-task heuristic (see handle_v58_autonomous_commands
    force=True) — so a request like "settings and go to display" gets an
    actual attempt (researched, executed, and cached/learned for next time)
    instead of just "I couldn't find that". This does mean a command that
    truly is nonsense will now cost a small amount to fail properly, rather
    than failing for free — the trade-off danny asked for.
    """
    target, sub_target, via_steam, needs_more = llm_clean_launch_target(raw_target, original_command)

    # A genuine multi-step request (e.g. "open settings and go to display")
    # must not be flattened down to just the app name — that would silently
    # drop the actual point of the request.
    if target and needs_more:
        # Before paying for research: has Jarvis already learned how to
        # reach THIS destination inside THIS app, under any wording at all?
        # (open_settings, Display) is the same skill whether it was taught
        # by "go to display" or "show me display settings" last time.
        if sub_target:
            skill = get_app_skill(target, sub_target)
            if skill and skill.get("steps"):
                kind = skill.get("kind", "click")
                say(f"I already know how to get to {sub_target} in {target} — no AI needed.")
                print(f"APP SKILL: reusing known {kind} skill for {target!r} -> {sub_target!r}")
                if kind == "bash":
                    # A shell-command shortcut (e.g. ms-settings:bluetooth)
                    # is as deterministic as tier-1 bash replay — same trust
                    # level, no extra verification needed.
                    replay_ok = _v58_replay_bash_steps(skill["steps"])
                else:
                    replay_ok = _v58_replay_click_steps(
                        skill["steps"]
                    ) and _v58_verify_with_local_vision(original_command)
                if replay_ok:
                    say("Done.")
                    return True
                print(f"APP SKILL: stored skill for {target!r} -> {sub_target!r} no longer works; relearning.")

        # No matching skill (or it just failed) — send the ORIGINAL command
        # to the full pipeline so the "go to display" part actually gets
        # done too, researched and learned properly this time.
        print(f"LAUNCH CLEANUP: '{raw_target}' needs more than a launch (target={target!r}); using the full AI pipeline.")
        return handle_v58_autonomous_commands(original_command, force=True)

    if target and target.strip().lower() != raw_target.strip().lower():
        print(f"LAUNCH CLEANUP: '{raw_target}' -> target={target!r} via_steam={via_steam}")

        if handle_steam_play_routine_command("open " + target):
            return True

        if via_steam and handle_v58_autonomous_commands(f"open steam and launch {target}"):
            return True

        found = find_and_open_app(target)
        if found:
            say("Opening " + found + ".")
            return True

    print(f"LAUNCH CLEANUP: nothing matched; handing the full task to the AI pipeline: {original_command}")
    return handle_v58_autonomous_commands(original_command, force=True)


# ============================================================
# NATURAL OPEN / LAUNCH / START COMMANDS
# ============================================================

def handle_natural_app_command(command):


    prefixes = [

        "open ",

        "launch ",

        "start ",

        "run "

    ]


    for prefix in prefixes:


        if command.startswith(
            prefix
        ):


            app_name = command.replace(

                prefix,

                "",

                1

            ).strip()


            ignored = [

                "calculator",

                "calc",

                "notepad",

                "file explorer",

                "explorer",

                "files",

                "my files",

                "downloads",

                "documents",

                "desktop",

                "pictures",

                "videos",

                "music",

                "browser",

                "google",

                "youtube",

                "settings",

                "task manager",

                "control panel",

                "command prompt",

                "cmd",

                "powershell",

                "steam",

                "discord",

                "spotify",

                "epic",

                "epic games",

                "xbox",

                "jarvis folder",

                "the jarvis folder"

            ]


            if app_name in ignored:

                return False


            if app_name:


                say(
                    "Looking for "
                    + app_name
                    + "."
                )


                found = find_and_open_app(
                    app_name
                )


                if found:


                    say(
                        "Opening "
                        + found
                        + "."
                    )


                elif not try_launch_with_llm_cleanup(app_name, command):


                    say(
                        "I couldn't find an installed "
                        "application called "
                        + app_name
                        + "."
                    )


                return True


    return False



# ============================================================
# EXTRA NATURAL PC COMMANDS
# ============================================================

def clean_natural_command(command):
    command = command.lower().strip()

    # Remove conversational lead-ins before EVERY command is checked.
    starters = [
        "okay, ", "okay ", "ok, ", "ok ",
        "right, ", "right ", "alright, ", "alright ",
        "so, ", "so ", "well, ", "well ",
        "jarvis, ", "jarvis ",
        "hey jarvis, ", "hey jarvis ",
        "can you please ", "could you please ",
        "can you ", "could you ", "would you ",
        "will you ", "please "
    ]

    changed = True
    while changed:
        changed = False
        for starter in starters:
            if command.startswith(starter):
                command = command[len(starter):].strip()
                changed = True
                break

    # Natural variations that should behave like the same command.
    replacements = [
        ("open up ", "open "),
        ("bring up ", "open "),
        ("pull up ", "open "),
    ]

    for old, new in replacements:
        if command.startswith(old):
            command = new + command[len(old):].strip()
            break

    # Remove harmless trailing conversation words before command matching.
    # This lets the same command work as, for example,
    # "what time is it", "what time is it again", or "what time is it please".
    command = re.sub(r"[,.!?]+$", "", command).strip()
    trailing_fillers = (
        " again please", " please again", " one more time", " again",
        " please", " for me"
    )
    changed = True
    while changed:
        changed = False
        for filler in trailing_fillers:
            if command.endswith(filler) and len(command) > len(filler):
                command = command[:-len(filler)].strip(" ,.!?")
                changed = True
                break

    return command

def handle_extra_natural_commands(command):
    command = clean_natural_command(command)

    folders = {
        "downloads": "Downloads",
        "documents": "Documents",
        "pictures": "Pictures",
        "videos": "Videos",
        "music": "Music",
        "desktop": "Desktop"
    }

    for starter in ["take me to my ", "take me to ", "go to my ", "go to ", "show me my ", "show me "]:
        if command.startswith(starter):
            folder_name = command[len(starter):].strip()
            if folder_name in folders:
                say("Opening " + folder_name + ".")
                open_folder(os.path.join(os.environ["USERPROFILE"], folders[folder_name]))
                return True

    if command in ["show the desktop", "show me the desktop", "minimize all windows", "minimise all windows", "hide all windows"]:
        say("Showing the desktop.")
        subprocess.Popen(["powershell", "-NoProfile", "-Command",
                          "(New-Object -ComObject Shell.Application).MinimizeAll()"])
        return True

    if command in ["restore windows", "bring back my windows", "show my windows again", "undo minimize", "undo minimise"]:
        say("Restoring your windows.")
        subprocess.Popen(["powershell", "-NoProfile", "-Command",
                          "(New-Object -ComObject Shell.Application).UndoMinimizeALL()"])
        return True

    websites = {
        "youtube": "https://www.youtube.com",
        "google": "https://www.google.com",
        "gmail": "https://mail.google.com",
        "chatgpt": "https://chatgpt.com"
    }

    for starter in ["take me to ", "go to ", "open up "]:
        if command.startswith(starter):
            site = command[len(starter):].strip()
            if site in websites:
                say("Opening " + site + ".")
                open_url(websites[site])
                return True

    for prefix in ["search for ", "can you search for ", "could you search for ",
                   "find me ", "look up ", "search the internet for "]:
        if command.startswith(prefix):
            search = command[len(prefix):].strip()
            if search:
                say("Searching Google for " + search + ".")
                open_url("https://www.google.com/search?q=" + search.replace(" ", "+"))
                return True

    for prefix in ["find on youtube ", "search youtube for ", "search youtube ", "look for on youtube "]:
        if command.startswith(prefix):
            search = command[len(prefix):].strip()
            if search:
                say("Searching YouTube for " + search + ".")
                open_url("https://www.youtube.com/results?search_query=" + search.replace(" ", "+"))
                return True

    for prefix in ["could you open ", "can you open ", "would you open ", "please open ",
                   "open up ", "i want to open ", "i want to play ", "let's play ", "lets play ", "play "]:
        if command.startswith(prefix):
            app_name = command[len(prefix):].strip()
            for article in ["my ", "the ", "a ", "an "]:
                if app_name.startswith(article):
                    app_name = app_name[len(article):].strip()
                    break
            if app_name:
                say("Looking for " + app_name + ".")
                found = find_and_open_app(app_name)
                if found:
                    say("Opening " + found + ".")
                elif not try_launch_with_llm_cleanup(app_name, command):
                    say("I couldn't find an installed application or game called " + app_name + ".")
                return True

    return False





# ============================================================
# CLOSE APPLICATIONS
# ============================================================

def close_running_app(app_name):
    """Find matching visible applications and close them safely."""
    app_name = app_name.lower().strip()
    if not app_name:
        return None

    aliases = {
        "google chrome": "chrome",
        "chrome": "chrome",
        "microsoft edge": "msedge",
        "edge": "msedge",
        "file explorer": "explorer",
        "windows explorer": "explorer",
        "epic games": "epicgameslauncher"
    }

    protected = {
        "jarvis", "this", "this app", "the app",
        "terminal", "windows terminal", "command prompt", "cmd",
        "powershell", "power shell", "python"
    }

    if app_name in protected:
        return None

    search_name = aliases.get(app_name, app_name)

    try:
        ps_script = r"""
$needle = $env:JARVIS_CLOSE_NAME.ToLower()
$matches = Get-Process -ErrorAction SilentlyContinue | Where-Object {
    try {
        $processName = $_.ProcessName.ToLower()
        $windowTitle = $_.MainWindowTitle.ToLower()
        $_.Id -ne $PID -and $_.MainWindowHandle -ne 0 -and (
            $processName -eq $needle -or
            $processName.Contains($needle) -or
            $windowTitle.Contains($needle)
        )
    } catch { $false }
}

$closed = @()
foreach ($process in $matches) {
    try {
        $name = $process.ProcessName
        $null = $process.CloseMainWindow()
        Start-Sleep -Milliseconds 800

        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction Stop
        }

        $closed += $name
    } catch {
        # Keep trying the remaining matching applications.
    }
}

$closed | Select-Object -Unique | ConvertTo-Json -Compress
"""

        env = os.environ.copy()
        env["JARVIS_CLOSE_NAME"] = search_name

        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=20,
            env=env
        )

        output = result.stdout.strip()
        if result.returncode != 0 or not output or output == "null":
            return None

        closed = json.loads(output)
        if isinstance(closed, str):
            closed = [closed]

        return closed if closed else None

    except Exception as error:
        print("Close app error:", error)
        return None


def handle_close_app_command(command):
    """Understand natural requests such as close Steam or quit Discord."""
    prefixes = ["close ", "quit ", "shut down "]

    for prefix in prefixes:
        if command.startswith(prefix):
            app_name = command[len(prefix):].strip()

            for article in ["my ", "the ", "an ", "a "]:
                if app_name.startswith(article):
                    app_name = app_name[len(article):].strip()
                    break

            if app_name in ["jarvis", "this", "this app", "the app", "terminal", "windows terminal", "command prompt", "cmd", "powershell", "power shell", "python"]:
                say("I won't close the window running me.")
                return True

            if app_name:
                closed = close_running_app(app_name)

                if closed:
                    say("Closed " + app_name + ".")
                else:
                    say("I couldn't find a running application called " + app_name + ".")

                return True

    return False


# ============================================================
# PERSISTENT JARVIS MEMORY + REMINDERS
# ============================================================

MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_memory.json")


def load_jarvis_memory():
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("facts", {})
                    data.setdefault("reminders", [])
                    data.setdefault("routines", {})
                    # Migrate the old flat memory format without losing anything.
                    if not isinstance(data["facts"], dict):
                        data["facts"] = {}
                    if not isinstance(data["reminders"], list):
                        data["reminders"] = []
                    for key, value in list(data.items()):
                        if key not in ("facts", "reminders") and value not in (None, ""):
                            data["facts"].setdefault(key, value)
                    return data
    except Exception as error:
        print("Memory load error:", error)
    return {"facts": {}, "reminders": [], "routines": {}}


def save_jarvis_memory():
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(jarvis_memory, f, indent=2, ensure_ascii=False)
        return True
    except Exception as error:
        print("Memory save error:", error)
        return False


jarvis_memory = load_jarvis_memory()
# First learned routine: this stores semantic steps, never screen coordinates.
jarvis_memory.setdefault("routines", {})
# New learn-by-doing records are isolated from the known-good Steam routines.
jarvis_memory.setdefault("learned_routines", {})
if "hogwarts legacy" not in jarvis_memory["routines"]:
    jarvis_memory["routines"]["hogwarts legacy"] = {
        "type": "steam_play",
        "game": "Hogwarts Legacy",
        "steps": ["open_or_focus_steam", "search_steam_for_game", "open_matching_game_result", "find_and_click_play"]
    }
    save_jarvis_memory()
pending_memory_key = None


def memory_facts():
    return jarvis_memory.setdefault("facts", {})


def remember_fact(key, value):
    key = str(key).strip().lower()
    value = str(value).strip().rstrip(".?!")
    if not key or not value:
        return False
    facts = memory_facts()
    facts[key] = value

    # Keep favourite drink as one authoritative memory, even if an older
    # version used the alternate spelling.
    if key in ("favourite drink", "favorite drink"):
        facts["favourite drink"] = value
        facts.pop("favorite drink", None)

    return save_jarvis_memory()


def forget_fact(key):
    key = str(key).strip().lower()
    facts = memory_facts()
    if key in facts:
        del facts[key]
        return save_jarvis_memory()
    return False


def get_favourite_drink():
    facts = memory_facts()
    value = facts.get("favourite drink") or facts.get("favorite drink")
    return str(value).strip().rstrip(".?!") if value else None


def extract_drink_update(message):
    text = str(message).strip().rstrip(".?!")
    text = re.sub(r"^(?:no+\s*,?\s*)", "", text, flags=re.IGNORECASE)
    patterns = (
        r"(?:^|.*?\b)(?:my\s+)?(?:new\s+)?favou?rite\s+drink\s+is\s+now\s+(.+)$",
        r"(?:^|.*?\b)(?:my\s+)?(?:new\s+)?favou?rite\s+drink\s+is\s+(.+)$",
        r"^(?:please\s+)?(?:change|update|set)\s+(?:my\s+)?favou?rite\s+drink\s+(?:to|as|is)\s+(.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            value = re.sub(r"\s+(?:please|thanks|thank you)$", "", match.group(1), flags=re.IGNORECASE).strip().rstrip(".?!")
            if value:
                return value
    return None


def add_reminder(text):
    text = str(text).strip().rstrip(".?!")
    if not text:
        return False
    reminders = jarvis_memory.setdefault("reminders", [])
    if text.lower() not in [str(x).lower() for x in reminders]:
        reminders.append(text)
    return save_jarvis_memory()


def remove_reminder(text):
    query = str(text).strip().lower()
    reminders = jarvis_memory.setdefault("reminders", [])
    for item in list(reminders):
        if query == str(item).lower() or query in str(item).lower():
            reminders.remove(item)
            return item if save_jarvis_memory() else None
    return None


def handle_memory_commands(command, original_message=None):
    global pending_memory_key
    original_message = original_message or command

    # Normalise punctuation and harmless wording differences so the same
    # memory commands work from typing, voice recognition, and the phone.
    c = command.strip().lower()
    c = re.sub(r"[^a-z0-9' ]+", " ", c)
    c = re.sub(r"\s+", " ", c).strip()

    # Favourite drink remains a dedicated, reliable memory because it was already tested.
    new_drink = extract_drink_update(original_message)
    if new_drink:
        if remember_fact("favourite drink", new_drink):
            say("Got it. Your favourite drink is now " + new_drink + ".")
        else:
            say("I understood that, but I couldn't save it to my memory.")
        return True

    if c in (
        "what's my favourite drink",
        "what is my favourite drink",
        "whats my favourite drink",
        "what's my favourite drink again",
        "what is my favourite drink again",
        "whats my favourite drink again",
        "what do i like to drink",
        "what drink do i like",
    ):
        value = get_favourite_drink()
        if value:
            say("Your favourite drink is " + value + ".")
        else:
            pending_memory_key = "favourite drink"
            say("I don't know yet. What is it?")
        return True

    # Reminders and notes. A recall question such as
    # "remind me what my favourite drink is" is NOT a reminder to save.
    if not re.search(r"\b(remind|remember|tell) me\b.*\b(what|who|where|when|why|how|is|was|are)\b", c):
        reminder_prefixes = ("remember to ", "remind me to ", "add a reminder to ", "add reminder to ", "note that ", "make a note to ")
        for prefix in reminder_prefixes:
            if c.startswith(prefix):
                reminder = original_message.strip()[len(prefix):].strip()
                if add_reminder(reminder):
                    say("Done. I've saved that as a reminder: " + reminder + ".")
                else:
                    say("I couldn't save that reminder.")
                return True

    if c in ("what are my reminders", "show my reminders", "list my reminders", "what do i need to remember", "show my notes"):
        reminders = jarvis_memory.get("reminders", [])
        if not reminders:
            say("You don't have any saved reminders yet.")
        else:
            say("Your saved reminders are: " + "; ".join(f"{i + 1}, {item}" for i, item in enumerate(reminders)) + ".")
        return True

    for prefix in ("remove reminder ", "delete reminder ", "forget reminder ", "remove the reminder "):
        if c.startswith(prefix):
            removed = remove_reminder(original_message.strip()[len(prefix):].strip())
            say("Removed reminder: " + removed + "." if removed else "I couldn't find that reminder.")
            return True

    # If Jarvis just asked for a value, save the next reply.
    if pending_memory_key:
        if c not in ("cancel", "never mind", "nevermind", "forget it"):
            key = pending_memory_key
            pending_memory_key = None
            if remember_fact(key, original_message.strip()):
                say("Got it. I've saved that and I'll remember it next time.")
            else:
                say("I understood that, but I couldn't save it to my memory.")
            return True
        pending_memory_key = None
        say("Okay, I won't save that.")
        return True

    # Show and forget saved long-term facts.
    if c in ("what do you remember about me", "what do you remember", "show my memories", "list my memories"):
        facts = memory_facts()
        if not facts:
            say("I don't have any saved personal memories yet.")
        else:
            say("Here's what I remember: " + "; ".join(f"{key} is {value}" for key, value in facts.items()) + ".")
        return True

    for prefix in ("forget that ", "forget my ", "delete memory ", "remove memory "):
        if c.startswith(prefix):
            requested = original_message.strip()[len(prefix):].strip().rstrip(".?!").lower()
            facts = memory_facts()
            # Accept either an exact memory label or the remembered statement itself.
            matched = None
            for key, value in facts.items():
                if requested == key or requested == str(value).lower() or requested == f"{key} is {str(value).lower()}":
                    matched = key
                    break
            if matched and forget_fact(matched):
                say("Okay. I've forgotten that memory.")
            else:
                say("I couldn't find a saved memory matching that.")
            return True

    # General long-term memory. When the user gives a simple personal fact,
    # save it under a meaningful key so it can be updated and asked again later.
    prefixes = ("remember that ", "remember ", "jarvis remember that ", "jarvis remember ")
    for prefix in prefixes:
        if c.startswith(prefix):
            memory_text = original_message.strip()[len(prefix):].strip().rstrip(".?!")
            if not memory_text:
                say("What would you like me to remember?")
                return True

            fact_match = re.match(r"(?:that\s+)?(?:my\s+)?(.+?)\s+is\s+(.+)$", memory_text, re.IGNORECASE)
            if fact_match:
                key = fact_match.group(1).strip().lower()
                value = fact_match.group(2).strip()
                # Keep the key natural and consistent with questions such as
                # 'what is my favourite colour again?'
                if key.startswith("my "):
                    key = key[3:].strip()
                if key and value and remember_fact(key, value):
                    say("Got it. I'll remember that your " + key + " is " + value + ".")
                else:
                    say("I couldn't save that to my memory.")
                return True

            key = "memory_" + str(len(memory_facts()) + 1)
            if remember_fact(key, memory_text):
                say("Got it. I'll remember that.")
            else:
                say("I couldn't save that to my memory.")
            return True

    # General recall of named personal facts. This deliberately runs after
    # the dedicated commands above, so existing features keep priority.
    recall = re.match(r"^(?:what(?:'s| is)|whats)\s+(?:my\s+)?(.+?)(?:\s+(?:again|please))?$", c)
    if recall:
        requested = recall.group(1).strip().lower()
        aliases = {"favorite": "favourite", "favourite": "favorite"}
        facts = memory_facts()
        candidates = [requested]
        words = requested.split()
        candidates.append(" ".join(w for w in words if w not in ("the", "current")))
        if requested.startswith("my "):
            candidates.append(requested[3:].strip())
        for candidate in list(candidates):
            if "favorite" in candidate:
                candidates.append(candidate.replace("favorite", "favourite"))
            if "favourite" in candidate:
                candidates.append(candidate.replace("favourite", "favorite"))
        for candidate in candidates:
            value = facts.get(candidate)
            if value not in (None, ""):
                say("Your " + candidate.replace("favorite", "favourite") + " is " + str(value).strip() + ".")
                return True

    return False


# ============================================================
# CONVERSATION MEMORY
# ============================================================

conversation_history = []
MAX_CONVERSATION_MESSAGES = 30

def add_to_memory(role, message):
    message = str(message).strip()
    if not message:
        return
    conversation_history.append({"role": role, "message": message})
    while len(conversation_history) > MAX_CONVERSATION_MESSAGES:
        conversation_history.pop(0)

def get_conversation_context():
    if not conversation_history:
        return "No previous conversation yet."
    return "\n".join(
        f'{"User" if item["role"] == "user" else "Jarvis"}: {item["message"]}'
        for item in conversation_history
    )

# ============================================================
# LOCAL AI BRAIN (OLLAMA) + SMART ROUTER
#
# The conversation brain, intent router, and learning planner all run on the
# local Ollama gpt-oss:20b model — no per-message cost, nothing leaves the PC.
# gpt-oss:20b has no vision or web-search ability, so OpenAI is still used
# (and still needs JARVIS_OPENAI_API_KEY) for two optional features only:
# screen-vision ("what's on my screen") and the pre-automation web research
# step. Both degrade gracefully with a clear message if no key is set.
# ============================================================

OPENAI_MODEL = os.environ.get("JARVIS_OPENAI_MODEL", "gpt-5-mini")
VISION_MODEL = os.environ.get("JARVIS_VISION_MODEL", OPENAI_MODEL)

# ============================================================
# AUTONOMOUS RESEARCH + HYBRID WINDOWS AGENT (v58)
# ============================================================
# v58 deliberately keeps v53 intact and replaces the sluggish local
# vision-agent fallback with an online research/planning layer plus
# Microsoft UFO² for Windows execution.
#
# UFO² handles HostAgent -> AppAgent orchestration, UI Automation, CLI/API
# tools, verification/state management, and (when configured) experience
# learning. Jarvis performs a short online research pass first so the
# executor receives current, task-specific knowledge rather than guessing.
AUTONOMOUS_V58_ENABLED = os.environ.get("JARVIS_AUTONOMOUS_V58", "1") != "0"
# Prefer the verified UFO² v2.0.0 installation. Keep an environment override
# for portability, and only fall back to the older folder if it actually exists.
_default_ufo2 = os.path.join(os.path.expanduser("~"), "Jarvis", "UFO2_v2.0.0")
_legacy_ufo = os.path.join(os.path.expanduser("~"), "Jarvis", "UFO")
if os.path.isdir(_default_ufo2):
    _ufo_default_root = _default_ufo2
else:
    _ufo_default_root = _legacy_ufo

JARVIS_UFO_ROOT = os.environ.get("JARVIS_UFO_ROOT", _ufo_default_root)
JARVIS_UFO_PYTHON = os.environ.get(
    "JARVIS_UFO_PYTHON",
    os.path.join(JARVIS_UFO_ROOT, ".venv", "Scripts", "python.exe"),
)
JARVIS_UFO_MODEL = os.environ.get("JARVIS_UFO_MODEL", OPENAI_MODEL)
JARVIS_UFO_TIMEOUT = int(os.environ.get("JARVIS_UFO_TIMEOUT", "420"))


# Clean the API key defensively. If it was pasted with ordinary or “smart”
# quotation marks around it, those characters must never reach the HTTP headers.
OPENAI_API_KEY = os.environ.get("JARVIS_OPENAI_API_KEY", "").strip()
OPENAI_API_KEY = OPENAI_API_KEY.strip("'\\\"“”‘’`")
OPENAI_API_KEY = "".join(ch for ch in OPENAI_API_KEY if ord(ch) < 128)

# Used only for screen-vision and the pre-automation web-search research step
# now — the conversation brain, router, and learning planner run on Ollama.
openai_client = None
if OPENAI_API_KEY:
    try:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as error:
        print("OpenAI setup error:", error)


def persistent_memory_context():
    facts = memory_facts()
    reminders = jarvis_memory.get("reminders", [])

    lines = []
    for key, value in facts.items():
        lines.append(f"{key}: {value}")

    if reminders:
        lines.append("reminders: " + "; ".join(str(item) for item in reminders))

    return "\n".join(lines) or "No saved long-term memories yet."


def online_available():
    return openai_client is not None


def openai_safe_text(value):
    """
    Make request text safe for the HTTP/client stack on Windows.

    Some Windows/Python setups can incorrectly try to encode typographic
    punctuation as ASCII before sending a request. Normalise those characters
    before anything is passed to the OpenAI client.
    """
    import unicodedata

    text = str(value)
    replacements = {
        "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
        "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
        "\u2013": "-", "\u2014": "-", "\u2212": "-",
        "\u2026": "...", "\u00a0": " ",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    # Keep ordinary letters where possible, while removing any remaining
    # characters that could trigger an accidental ASCII-only encoding path.
    text = unicodedata.normalize("NFKD", text)
    return text.encode("ascii", "ignore").decode("ascii")


_local_brain_checked = False


def local_brain_ready():
    """
    One-time startup ping to Ollama so problems are reported clearly instead
    of surfacing later as a mysterious silent failure. This does not gate
    individual calls — Ollama is localhost, so a real request that fails is
    just as fast to catch as a pre-check would be.
    """
    global _local_brain_checked
    if _local_brain_checked:
        return
    _local_brain_checked = True
    try:
        response = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=3)
        response.raise_for_status()
        names = [str(m.get("name", "")) for m in response.json().get("models", [])]
        have_it = any(name == MODEL or name.startswith(MODEL.split(":")[0] + ":") for name in names)
        if have_it:
            print(f"\nLocal AI brain ready: {MODEL} (Ollama at {OLLAMA_HOST}).")
        else:
            print(
                f"\nWarning: Ollama is running at {OLLAMA_HOST} but '{MODEL}' is not pulled yet.\n"
                f"Run:  ollama pull {MODEL}"
            )
    except Exception as error:
        print(
            f"\nWarning: could not reach Ollama at {OLLAMA_HOST} ({error}).\n"
            "Jarvis's local brain needs Ollama running (\"ollama serve\") with "
            f"{MODEL} pulled, or conversation replies will fail."
        )


def ask_ollama_brain(instructions, user_content, json_mode=False, timeout=None, model=None, think=None):
    """
    Call the local Ollama chat brain. Raises on any failure (network error,
    bad status, empty body) so callers decide how to fall back — same
    contract the old OpenAI calls had.

    `model` overrides the default (MODEL, the full conversational brain).
    Pass OLLAMA_FAST_MODEL for short classification/extraction calls
    (routing decisions, target-name cleanup) — measured faster once warm
    AND smaller to swap in, but only with think=False (see below).

    `think`: only pass False together with OLLAMA_FAST_MODEL. Qwen3's
    default "thinking" mode writes a long reasoning preamble before its
    answer even for trivial classification, which measured SLOWER than
    gpt-oss:20b despite being a smaller model — think=False fixes that
    (measured ~2.2s vs ~6-7s for the same call). Do NOT pass this for
    gpt-oss:20b: measured it actively breaks that model's output (garbled,
    non-JSON text) since it's trained to reason via its own harmony format.
    """
    payload = {
        "model": model or MODEL,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }
    if think is not None:
        payload["think"] = think
    if json_mode:
        payload["format"] = "json"

    response = requests.post(OLLAMA_CHAT_URL, json=payload, timeout=timeout or OLLAMA_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    message = data.get("message", {}) or {}
    text = str(message.get("content", "")).strip()
    if not text:
        # Some Ollama builds surface a reasoning model's answer under
        # "thinking" if "content" ends up empty; use it rather than fail.
        text = str(message.get("thinking", "")).strip()
    if not text:
        raise ValueError("Ollama returned an empty response.")
    return text


def _stream_ollama_chat(instructions, user_content, model=None, think=None):
    """
    Stream a chat completion from Ollama, yielding each incremental text
    delta as it's generated (Ollama's chat stream is NDJSON — one JSON
    object per line). Raises on a genuine failure (network/HTTP error)
    exactly like ask_ollama_brain; a stream that connects fine and simply
    ends is not an error even if it happened to yield nothing.
    """
    payload = {
        "model": model or MODEL,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_content},
        ],
        "stream": True,
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }
    if think is not None:
        payload["think"] = think

    with requests.post(OLLAMA_CHAT_URL, json=payload, timeout=OLLAMA_TIMEOUT, stream=True) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = data.get("message", {}) or {}
            delta = message.get("content", "")
            if delta:
                yield delta
            if data.get("done"):
                break


local_brain_ready()


def smart_route(user_message):
    """
    Local-first intent router, powered by the Ollama gpt-oss:20b brain.

    It decides whether the user is having a normal conversation or asking
    Jarvis to perform a concrete local action. The router does NOT execute
    anything and cannot invent a computer action.
    """
    instructions = """
You are the intent router for a personal assistant called Jarvis.
Return ONLY valid JSON in this exact shape:
{"route":"ai","reason":"..."} or {"route":"local","reason":"..."}

Use route "ai" for normal conversation, questions, explanations, opinions,
follow-up questions, recall questions, and anything whose meaning should be
understood naturally.

Examples that MUST be "ai":
- "what's my favourite drink again?"
- "can you remind me what my favourite drink is please?"
- "do you remember my favourite drink?"
- "tell me about that again"
- "what did I ask you before?"

Use route "local" only when the user clearly wants an action on the PC,
a deliberate memory/reminder save/delete action, or another concrete built-in
Jarvis action.

Examples that MUST be "local":
- "open Steam"
- "close Discord"
- "minimise Steam"
- "remember that my favourite drink is hot chocolate"
- "remind me to take the bins out"
- "shut down the PC"
- "what is on my screen"
- "what do you see on my screen"
- "look at my screen"
- "describe what is on my screen"
- "read my screen"

IMPORTANT: The word "remind" by itself does not make something a reminder.
If the user is asking you to remind, tell, or recall information, route "ai".
Only route "local" for actually creating, listing, or deleting a reminder,
or another concrete local action.
"""

    try:
        raw = ask_ollama_brain(
            instructions,
            f"""SAVED MEMORY:
{persistent_memory_context()}

USER MESSAGE:
{user_message}""",
            json_mode=True,
            model=OLLAMA_FAST_MODEL,
            think=False,
        )
        data = json.loads(raw)
        route = str(data.get("route", "")).lower().strip()
        if route in ("ai", "local"):
            return route
    except Exception as error:
        print("Smart router unavailable:", error)

    return None


# ============================================================
# LOCAL MACHINE PROFILE
# ============================================================

def get_jarvis_machine_context():
    """Return stable facts about the PC so the AI plans for the actual machine."""
    try:
        if os.name == "nt":
            build = int(sys.getwindowsversion().build)
            os_name = "Windows 11" if build >= 22000 else "Windows 10"
            return (
                f"Operating system: {os_name}. Windows build: {build}. "
                "This is the PC Jarvis is controlling. Do not provide macOS, Linux, "
                "or other-OS alternatives unless the user explicitly asks for them."
            )
    except Exception:
        pass
    return "Operating system: Windows PC (assume Windows 11 for UI instructions)."


JARVIS_MACHINE_CONTEXT = get_jarvis_machine_context()


_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")
# Only these should be treated as a FALSE sentence end (an abbreviation
# mid-sentence, e.g. "Mr. Smith") -- everything else that ends in .!? is
# spoken as its own chunk immediately, including short ones. An earlier
# version instead rejected any candidate chunk under 20 characters, which
# looked like a safe way to avoid tiny fragments but actually broke
# streaming for exactly the short, punchy replies the new personality
# produces ("Greetings, sir." is 15 characters) -- those got stuck
# waiting for a longer boundary that never arrived (the final sentence of
# a short reply has no trailing whitespace to match on at all, since the
# stream just ends), so the ENTIRE reply was spoken as one block only
# after generation finished. Found via live timing instrumentation, not
# guessed.
_ABBREVIATION_RE = re.compile(r"\b(mr|mrs|ms|dr|prof|sr|jr|st|vs|approx|etc)\.$", re.IGNORECASE)


def ask_jarvis(user_message):
    """
    Conversation brain, powered entirely by the local Ollama gpt-oss:20b
    model — streamed and spoken sentence-by-sentence as it's generated
    (see _speak_stream), so Jarvis starts talking as soon as the first
    sentence is ready instead of going silent until the whole reply
    (e.g. a full story) has finished generating. Also runs at reduced
    ("low") reasoning effort: measured ~5-7x faster than gpt-oss:20b's
    default reasoning depth for ordinary conversation, with no loss of
    coherence — full deep reasoning simply isn't needed for chat/stories/
    quick answers, only for complex planning elsewhere in the file.
    """
    add_to_memory("user", user_message)
    conversation_context = get_conversation_context()

    instructions = """
You are Jarvis, the user's personal AI assistant — the same character as
in the Iron Man films: a brilliant, unflappable, dryly witty butler-AI.
Address the user as "sir" occasionally and naturally, not in every
sentence, and lean into that voice generally: composed, warm, quietly
witty, immediately capable. Acknowledge requests the way he would —
"Certainly, sir.", "Of course.", "Right away.", "I can do that." — when it
genuinely fits the moment, never as a rigid template stapled onto every
single reply regardless of context.

PERSONALITY
Calm, intelligent, observant, quietly confident, warm and natural. Speak
like a capable long-term assistant having a real conversation, not like a
customer-service chatbot, productivity coach, or project manager. Use dry,
subtle humour occasionally when it genuinely fits; never force it. Never
sound flat, stiff, or robotic — vary sentence rhythm and word choice the
way someone actually speaking would, not like a form letter.

RESPONSE SCALE — THIS IS IMPORTANT
Match the size and structure of your answer to the size and complexity of the
user's request.
- Casual chat gets a natural conversational reply.
- A simple factual question gets a direct answer first, with only useful context.
- A simple request or command should be acknowledged briefly; do not turn it into
  a checklist, menu, roadmap, or multi-step plan.
- Do NOT automatically offer several options, a large plan, or a list of next
  steps merely because the conversation has paused.
- Do NOT repeatedly ask "What would you like to do next?" or similar after every
  answer. Let the conversation flow naturally.
- For a genuinely complex task, reason through it and break it into manageable
  steps only when structure would actually help.
- If the user explicitly asks for a plan, detailed breakdown, comparison,
  checklist, or step-by-step help, then provide the appropriate structure.
- When unsure, start simple. The user can always ask for more detail.

LOCAL COMPUTER CONTEXT
{machine_context}
When the user asks how to do something on this computer, give Windows 11
instructions by default. Do not waste space listing macOS, Ubuntu, Windows 10,
or other operating systems unless the user explicitly asks for alternatives.

CONVERSATION AND CONTEXT
Pay close attention to recent conversation. Short follow-ups such as "yes",
"yeah", "go on", "tell me more", "that one", "okay", "do it", or "what about
that?" usually refer to surrounding context. Maintain continuity and do not ask
the user to repeat information they have just given you.

MEMORY
Use SAVED LONG-TERM MEMORY to answer personal recall questions naturally.
Treat requests such as "can you remind me what my favourite drink is?" as recall
questions when the user is asking what you remember, rather than as an instruction
to create a reminder. If memories conflict, state the conflict clearly instead of
inventing a resolution.

HONESTY ABOUT ACTIONS
You are the conversational brain. Do not claim that you opened, closed, changed,
deleted, or otherwise performed a computer action unless the local Python command
system actually performed it and this message explicitly tells you that it did.
If the user asks about a capability you do not actually have, answer honestly and
briefly rather than pretending or inventing results.
"""

    instructions = instructions.replace("{machine_context}", JARVIS_MACHINE_CONTEXT)

    user_content = f"""RECENT CONVERSATION:
{conversation_context}

SAVED LONG-TERM MEMORY:
{persistent_memory_context()}

LATEST USER MESSAGE:
{user_message}"""

    global active_remote_reply

    start_activity(["Understanding request", "Thinking", "Composing reply"], title="Conversation")

    # Starts a playback worker immediately; it blocks on this queue until
    # the loop below feeds it the first complete sentence.
    chunks = _ChunkQueueIterable()
    _speak_stream(chunks)

    print("\nJarvis:", end=" ", flush=True)

    full_answer = ""
    buffer = ""
    stream_failed = False
    started_composing = False

    advance_activity()  # -> Thinking

    try:
        for delta in _stream_ollama_chat(instructions, user_content, think="low"):
            if jarvis_stop_requested.is_set():
                break
            if not started_composing:
                started_composing = True
                advance_activity()  # -> Composing reply, first token is in
            full_answer += delta
            buffer += delta
            print(delta, end="", flush=True)

            # Flush complete sentences to the speech queue as soon as they
            # appear, so the worker above can start speaking sentence 1
            # while sentence 2+ is still being generated. Skip past a
            # boundary that's actually an abbreviation ("Mr. Smith") rather
            # than treating it as a real sentence end -- but otherwise
            # flush immediately, including short sentences ("Certainly,
            # sir." speaks right away, it does not wait for more text).
            while True:
                match = _SENTENCE_BOUNDARY_RE.search(buffer)
                if not match:
                    break
                if _ABBREVIATION_RE.search(buffer[:match.start()]):
                    next_match = _SENTENCE_BOUNDARY_RE.search(buffer, match.end())
                    if not next_match:
                        break
                    match = next_match
                chunk = buffer[:match.end()].strip()
                buffer = buffer[match.end():]
                if chunk:
                    chunks.put(chunk)
    except Exception as error:
        stream_failed = True
        print("\nLocal AI brain error:", error)

    if jarvis_stop_requested.is_set():
        print()
        chunks.finish()
        finish_activity()
        add_to_memory("jarvis", full_answer or "(stopped)")
        return full_answer

    remainder = buffer.strip()
    if remainder:
        chunks.put(remainder)

    if not full_answer and stream_failed:
        full_answer = (
            "I couldn't reach my local AI brain just now. "
            "Make sure Ollama is running and gpt-oss:20b is pulled."
        )
        print(full_answer, end="")
        chunks.put(full_answer)

    chunks.finish()
    print()
    finish_activity()
    log_recent_action(full_answer)

    with active_remote_lock:
        if active_remote_reply is not None:
            try:
                active_remote_reply.put_nowait(full_answer)
            except queue.Full:
                pass
            active_remote_reply = None

    add_to_memory("jarvis", full_answer)
    return full_answer



# ============================================================
# LEARN-BY-DOING FOUNDATION
# ============================================================

LEARNED_ROUTINE_STATES = {"discovered", "testing", "verified", "failed"}


def _normalise_learned_routine_name(value):
    return re.sub(r"\s+", " ", str(value).strip().lower())


def save_learned_routine(task, method=None, steps=None, state="discovered", source="learned"):
    """Save a semantic routine and its learning state; never store screen coordinates."""
    task = re.sub(r"\s+", " ", str(task).strip())
    if not task:
        return False
    state = str(state).strip().lower()
    if state not in LEARNED_ROUTINE_STATES:
        raise ValueError(f"Unknown learned routine state: {state}")
    key = _normalise_learned_routine_name(task)
    learned = jarvis_memory.setdefault("learned_routines", {})
    existing = learned.get(key, {})
    learned[key] = {
        "task": task,
        "method": str(method if method is not None else existing.get("method", "")).strip(),
        "steps": list(steps if steps is not None else existing.get("steps", [])),
        "source": str(source if source is not None else existing.get("source", "learned")).strip(),
        "state": state,
        "verified": state == "verified",
        "attempts": int(existing.get("attempts", 0)),
    }
    return save_jarvis_memory()


def get_learned_routine(task):
    return jarvis_memory.setdefault("learned_routines", {}).get(_normalise_learned_routine_name(task))


def update_learned_routine_state(task, state, steps=None, method=None):
    """Update state without losing an existing routine definition."""
    routine = get_learned_routine(task)
    if routine is None:
        return save_learned_routine(task, method=method, steps=steps, state=state)
    state = str(state).strip().lower()
    if state not in LEARNED_ROUTINE_STATES:
        raise ValueError(f"Unknown learned routine state: {state}")
    routine["state"] = state
    routine["verified"] = state == "verified"
    if steps is not None:
        routine["steps"] = list(steps)
    if method is not None:
        routine["method"] = str(method).strip()
    if state == "testing":
        routine["attempts"] = int(routine.get("attempts", 0)) + 1
    return save_jarvis_memory()


def list_learned_routines():
    return list(jarvis_memory.setdefault("learned_routines", {}).values())


# ============================================================
# APP SKILLS — a per-(app, destination) knowledge base
#
# learned_routines above is keyed by the exact task sentence, so "open
# settings and go to display" and "open settings and go to bluetooth" are
# unrelated entries even though "open Settings" is common to both. This
# index is keyed by (application, destination control) instead, so once
# Jarvis has learned how to reach ANY specific place inside an app, a later
# request worded completely differently that resolves to that same
# destination can reuse it immediately — no re-research, no re-learning.
# ============================================================

def _app_skill_key(application, control):
    return f"{str(application).strip().lower()}::{str(control).strip().lower()}"


def _normalise_skill_words(text):
    text = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())
    return set(text.split())


def get_app_skill(application, control):
    """
    Find a learned skill for (application, control). Matching is fuzzy on
    the control/destination name, not exact — the local classifier can
    reasonably say "Network & Internet" one time and "Network" another for
    the same actual page, and an exact-string index would treat those as
    two unrelated destinations and miss a skill that's genuinely already
    known. A match requires one name's significant words to be a subset of
    the other's (so "Network" matches "Network & Internet", but "Network"
    would not wrongly match "Now Playing").
    """
    if not application or not control:
        return None
    app_key = str(application).strip().lower()
    query_words = _normalise_skill_words(control)
    if not query_words:
        return None

    exact = jarvis_memory.setdefault("app_skills", {}).get(_app_skill_key(application, control))
    if exact:
        return exact

    best, best_score = None, 0
    for skill in jarvis_memory.setdefault("app_skills", {}).values():
        if str(skill.get("application", "")).strip().lower() != app_key:
            continue
        stored_words = _normalise_skill_words(skill.get("control", ""))
        if not stored_words:
            continue
        if query_words <= stored_words or stored_words <= query_words:
            overlap = len(query_words & stored_words)
            if overlap > best_score:
                best_score, best = overlap, skill
    return best


def save_app_skill(application, control, steps, kind="click"):
    """Record how to reach a specific destination inside an app, for reuse
    by any future request that resolves to this same (app, destination)
    pair — regardless of how differently that future request is worded.

    `kind` is "click" (steps is a list of {"application", "control"} click
    dicts, replayed via _v58_replay_click_steps) or "bash" (steps is a list
    of shell commands, replayed via _v58_replay_bash_steps) — both tiers of
    zero-cost replay feed this same reusable index.
    """
    if not application or not control or not steps or kind not in ("click", "bash"):
        return False
    key = _app_skill_key(application, control)
    skills = jarvis_memory.setdefault("app_skills", {})
    skills[key] = {
        "application": application,
        "control": control,
        "kind": kind,
        "steps": list(steps),
    }
    return save_jarvis_memory()


_MS_SETTINGS_URI_RE = re.compile(r"ms-settings:([a-zA-Z0-9\-]+)")


def _v58_infer_app_skill_from_bash(bash_steps):
    """
    For a well-known, stable Windows shell shortcut (currently: the
    ms-settings: URI scheme), infer the (application, destination) a
    bash-only routine represents — no extra AI call needed, since the URI
    itself already names the destination — so it can be indexed into the
    same reusable app_skills store as click-replay routines. Returns
    (application, destination) or (None, None) if not recognized.
    """
    if len(bash_steps) != 1:
        return None, None
    match = _MS_SETTINGS_URI_RE.search(bash_steps[0])
    if not match:
        return None, None
    page = match.group(1).replace("-", " ").title()
    return "Settings", page


def handle_learning_foundation_commands(command):
    """Safe first interface for inspecting and creating learning records."""
    c = command.strip()
    lower = c.lower().replace("’", "'").replace("`", "'")

    if lower in {"show learned routines", "list learned routines", "what have you learned"}:
        routines = list_learned_routines()
        if not routines:
            say("I haven't learned any new routines yet.")
            return True
        verified = sum(1 for r in routines if r.get("state") == "verified")
        say(f"I have {len(routines)} learned routines, including {verified} verified.")
        for routine in routines:
            print(f"LEARNED ROUTINE: {routine.get('task', '')!r} | state={routine.get('state', 'unknown')} | verified={routine.get('verified', False)}")
        return True

    prefixes = ("start learning ", "learn a routine for ", "learn routine for ")
    for prefix in prefixes:
        if lower.startswith(prefix):
            task = c[len(prefix):].strip()
            if not task:
                say("Tell me what task you want me to learn.")
                return True
            routine = get_learned_routine(task)
            if routine:
                say(f"I already have a learning record for {routine.get('task', task)}. It is {routine.get('state', 'unknown')}.")
            else:
                save_learned_routine(task, state="discovered", source="learn-by-doing")
                say(f"I've recorded {task} as a task to learn. I haven't tried it yet.")
            return True
    return False


# ============================================================
# LEARN-BY-DOING TRY + VERIFY (SAFE PILOT)
# ============================================================

def _foreground_window_title():
    """Return the current foreground window title defensively."""
    try:
        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        if not hwnd:
            return ""
        title_buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(hwnd, title_buf, len(title_buf))
        return title_buf.value.strip()
    except Exception:
        return ""


def _open_bluetooth_settings_for_learning():
    """Open the Windows Bluetooth settings page for the first safe learning test."""
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", "ms-settings:bluetooth"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as error:
        print("LEARNING: could not open Bluetooth settings:", error)
        return False


def _verify_bluetooth_settings_open(timeout=12.0):
    """Verify the actual Bluetooth & devices page, even though Settings may only expose the title 'Settings'."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        title = _foreground_window_title().lower()

        # Windows Settings normally reports only 'Settings' as its top-level
        # window title, so the old title-only check could reject a page that
        # was visibly open.  Confirm the page content inside the foreground
        # Settings window instead.
        if UI_AUTOMATION_AVAILABLE and title == "settings":
            try:
                desktop = Desktop(backend='uia')
                fg_hwnd = int(ctypes.windll.user32.GetForegroundWindow())
                if fg_hwnd:
                    foreground = desktop.window(handle=fg_hwnd)
                    for control in [foreground] + list(foreground.descendants()):
                        try:
                            name = _normalise_ui_name(control.window_text())
                        except Exception:
                            continue
                        if not name or not _ui_element_visible(control):
                            continue
                        if ("bluetooth & devices" in name or
                                name == "bluetooth and devices" or
                                name == "devices"):
                            print(f"LEARNING VERIFY: Settings page content confirmed by UIA: {name!r}.")
                            return True
            except Exception as error:
                print("LEARNING VERIFY: Settings UIA content check failed:", error)

        # Some Windows builds include the page name in the foreground title.
        if "bluetooth" in title or "bluetooth & devices" in title:
            print(f"LEARNING VERIFY: foreground title confirms Bluetooth: {title!r}.")
            return True

        time.sleep(0.25)
    return False


def _try_and_verify_known_learning_task(task):
    """Execute only the first explicitly supported, harmless learning task."""
    task_clean = re.sub(r"\s+", " ", str(task).strip())
    task_key = _normalise_learned_routine_name(task_clean)
    bluetooth_keys = {
        "open bluetooth settings",
        "open the bluetooth settings",
        "open bluetooth",
        "open the bluetooth page",
        "how to open bluetooth settings",
        "how to open the bluetooth settings",
    }
    if task_key not in bluetooth_keys:
        return False

    routine = get_learned_routine(task_clean)
    if routine is None:
        save_learned_routine(task_clean, state="discovered", source="learn-by-doing")
        routine = get_learned_routine(task_clean)

    update_learned_routine_state(
        task_clean,
        "testing",
        method="Windows Settings Bluetooth page",
        steps=[
            "open_windows_bluetooth_settings",
            "verify_bluetooth_settings_visible",
        ],
    )
    say("I know a safe test for that. I'll try it and verify what I see.")
    print(f"LEARNING: testing {task_clean!r}.")

    if not _open_bluetooth_settings_for_learning():
        update_learned_routine_state(task_clean, "failed")
        say("The test failed because I couldn't open Bluetooth settings. I won't remember it as verified.")
        return True

    if _verify_bluetooth_settings_open(timeout=12.0):
        update_learned_routine_state(task_clean, "verified")
        say("That worked. I verified Bluetooth settings are open, so I've learned the routine.")
        print(f"LEARNING: VERIFIED {task_clean!r}.")
    else:
        update_learned_routine_state(task_clean, "failed")
        say("I couldn't verify that the Bluetooth settings page opened, so I haven't marked the routine as learned.")
        print(f"LEARNING: verification failed for {task_clean!r}.")
    return True


def handle_learning_try_commands(command):
    """Handle the first real learn-by-doing TRY + VERIFY command."""
    c = command.strip()
    lower = c.lower().replace("’", "'").replace("`", "'")
    # Direct task commands are routed here before the generic app opener.
    # This prevents "open bluetooth settings" from being mistaken for an
    # installed application named "bluetooth settings".
    direct_tasks = {
        "open bluetooth settings": "how to open bluetooth settings",
        "open the bluetooth settings": "how to open the bluetooth settings",
        "open bluetooth": "how to open bluetooth settings",
        "open the bluetooth page": "how to open the bluetooth page",
    }
    if lower in direct_tasks:
        task = direct_tasks[lower]
        if _try_and_verify_known_learning_task(task):
            return True

    prefixes = (
        "try learning ",
        "test learning ",
        "try to learn ",
        "test how to learn ",
    )
    for prefix in prefixes:
        if lower.startswith(prefix):
            task = c[len(prefix):].strip()
            if not task:
                say("Tell me what you want me to try learning.")
                return True
            if _try_and_verify_known_learning_task(task):
                return True
            say("I can record that task, but I don't have a safe execution and verification method for it yet.")
            print(f"LEARNING: no safe pilot executor exists yet for {task!r}; nothing was executed.")
            return True
    return False


# ============================================================
# LEARN-BY-DOING PLANNING FOUNDATION (v43)
# ============================================================

pending_learning_plan = None


def _normalise_learning_plan_text(value):
    return re.sub(r"\s+", " ", str(value).strip())


def _extract_json_object(raw_text):
    """Extract a JSON object from a model response, tolerating code fences."""
    raw = str(raw_text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw).strip()

    try:
        return json.loads(raw)
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
        raise


def _normalise_learning_plan(data, goal):
    """Validate and normalise a temporary plan. This function never executes it."""
    if not isinstance(data, dict):
        raise ValueError("Planner returned something other than an object.")

    steps = data.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError("Planner steps were not a list.")

    normalised_steps = []
    for item in steps[:12]:
        if not isinstance(item, dict):
            continue
        action = _normalise_learning_plan_text(item.get("action", ""))
        verify = _normalise_learning_plan_text(item.get("verify", ""))
        if action and verify:
            normalised_steps.append({
                "action": action,
                "verify": verify,
            })

    if not normalised_steps:
        raise ValueError("Planner returned no usable verified steps.")

    risk = _normalise_learning_plan_text(data.get("risk", "low")).lower()
    if risk not in {"low", "medium", "high", "unknown"}:
        risk = "unknown"

    requires_confirmation = bool(data.get("requires_confirmation", False))

    return {
        "goal": _normalise_learning_plan_text(data.get("goal", goal)) or goal,
        "summary": _normalise_learning_plan_text(data.get("summary", "")),
        "risk": risk,
        "requires_confirmation": requires_confirmation,
        "steps": normalised_steps,
        "source": "ai-planner",
        "temporary": True,
    }


def _learning_plan_sanity_check(plan):
    """Reject plans that mix alternative routes or break obvious UI continuity."""
    steps = plan.get("steps", [])
    if not steps:
        return False, "The plan contains no steps."

    banned_route_phrases = (
        "alternatively", "another way", "instead", "if you cannot",
        "if you can't", "optional alternative", "another option",
        "as an alternative", "fallback option",
    )
    for number, step in enumerate(steps, 1):
        text = (step.get("action", "") + " " + step.get("verify", "")).lower()
        for phrase in banned_route_phrases:
            if phrase in text:
                return False, f"Step {number} contains an alternative route."

    # Desktop context-menu actions are not a dependable executable route
    # when Terminal, File Explorer, or another window is covering the desktop.
    # Reject them so the planner cannot hand the executor an action it cannot
    # safely perform.
    for number, step in enumerate(steps, 1):
        action = step.get("action", "").lower()
        if (
            ("right-click" in action or "right click" in action)
            and "desktop" in action
        ):
            return False, f"Step {number} uses a desktop right-click route that is not reliably executable."

    # Once Settings is opened, a later step should not jump back to the
    # desktop/taskbar to continue the same Settings task.
    settings_opened = False
    for number, step in enumerate(steps, 1):
        action = step.get("action", "").lower()
        if "settings" in action and any(x in action for x in ("open", "launch", "press windows key + i", "windows key + i")):
            settings_opened = True
            continue
        if settings_opened and (
            "right-click desktop" in action
            or "right click desktop" in action
            or "desktop context menu" in action
            or "click the desktop" in action
        ):
            return False, f"Step {number} breaks continuity by returning to the desktop after Settings opened."

    return True, "ok"


def _build_learning_plan_with_local_brain(goal):
    """Build a Windows 11 plan while preferring routes Jarvis can actually execute."""
    # Display Settings is a known, harmless Windows 11 task. Use the proven
    # keyboard/UIA route rather than allowing a general planner to choose a
    # desktop-context-menu route that may be impossible when another window is
    # covering the desktop.
    goal_lower = _normalise_learning_plan_text(goal).lower()
    if "display" in goal_lower and "settings" in goal_lower and any(
        word in goal_lower for word in ("open", "launch", "show", "go to")
    ):
        plan = {
            "goal": goal,
            "summary": "Use the Windows 11 Settings keyboard shortcut, then navigate System > Display with UI Automation.",
            "risk": "low",
            "requires_confirmation": False,
            "steps": [
                {
                    "action": "Press Windows key + I to open the Settings app.",
                    "verify": "A window titled 'Settings' is visible on screen.",
                },
                {
                    "action": "In Settings, click the 'System' entry.",
                    "verify": "The Settings page shows System navigation/content.",
                },
                {
                    "action": "In System settings, click 'Display'.",
                    "verify": "The Display settings page is visible with display-specific controls such as scale or resolution.",
                },
            ],
            "source": "windows11-known-safe-route",
            "temporary": True,
        }
        valid, reason = _learning_plan_sanity_check(plan)
        if valid:
            return plan, None
        return None, reason

    instructions = """
You are Jarvis's planning module.

LOCAL COMPUTER CONTEXT:
{machine_context}

The user wants to learn how to perform a computer task on this exact PC.
This machine is Windows 11. Plan for Windows 11 only. Never return macOS,
Ubuntu/Linux, Windows 10, or generic multi-OS alternatives unless the user
explicitly asks for them.

Create a TEMPORARY plan only. Do not claim that anything has been performed.
Do not provide code, shell commands, registry edits, PowerShell commands, or
destructive instructions.

Return JSON only with exactly these fields:
{
  "goal": "short goal",
  "summary": "brief description of the likely approach",
  "risk": "low|medium|high|unknown",
  "requires_confirmation": true|false,
  "steps": [
    {
      "action": "one concrete UI-level action",
      "verify": "one observable check that proves this step succeeded"
    }
  ]
}

Rules:
- Maximum 12 steps, but use the SHORTEST reasonable route.
- Choose ONE continuous route from the current desktop state to the requested goal.
- Every step must logically continue from the UI state produced by the previous step.
- NEVER mix alternative routes into the same sequential plan. Do not include phrases such as
  "alternatively", "another way", "instead", "if you cannot", or optional fallback actions.
- Once the requested goal is visibly reached, STOP the plan. Do not add extra ways to reach it.
- Prefer ordinary Windows 11 UI actions and the keyboard where reliable.
- Every action must have a concrete verification.
- Verification must prove the state created by THAT step, not merely repeat the goal.
- If the task could delete data, buy something, send a message, change security,
  expose credentials, install unknown software, or otherwise cause a meaningful
  side effect, set requires_confirmation=true.
- If the task cannot be planned safely from the goal alone, say so in summary,
  use risk=unknown, and keep the plan conservative.
- Never invent that Jarvis has already completed a step.
"""

    instructions = instructions.replace("{machine_context}", JARVIS_MACHINE_CONTEXT)

    try:
        raw = ask_ollama_brain(
            instructions,
            "Create a temporary Windows 11 learning plan for this task:\n" + goal,
            json_mode=True,
        )
        data = _extract_json_object(raw)
        plan = _normalise_learning_plan(data, goal)
        valid, reason = _learning_plan_sanity_check(plan)
        if not valid:
            print("LEARNING PLANNER: rejected inconsistent plan:", reason)
            return None, reason
        return plan, None
    except Exception as error:
        print("LEARNING PLANNER: local planning error:", error)
        return None, str(error)


def _print_learning_plan(plan):
    print("\nLEARNING PLAN (TEMPORARY — NOT EXECUTED)")
    print(f"Goal: {plan['goal']}")
    if plan.get("summary"):
        print(f"Approach: {plan['summary']}")
    print(f"Risk: {plan['risk']}")
    print(f"Confirmation required: {plan['requires_confirmation']}")
    for number, step in enumerate(plan["steps"], 1):
        print(f"  {number}. ACTION: {step['action']}")
        print(f"     VERIFY: {step['verify']}")
    print("END LEARNING PLAN\n")



# ============================================================
# CONTROLLED LEARN-BY-DOING EXECUTOR (v44)
# ============================================================

def _learning_action_kind(action):
    """
    Map natural-language plan actions to a very small, explicit whitelist.
    Unknown actions are NEVER executed.
    """
    a = _normalise_learning_plan_text(action).lower()

    if "bluetooth" in a and ("settings" in a or "devices" in a):
        return "open_bluetooth_settings"

    if ("open" in a or "launch" in a or "start" in a) and "calculator" in a:
        return "open_calculator"

    if ("open" in a or "launch" in a or "start" in a) and "notepad" in a:
        return "open_notepad"

    if ("open" in a or "launch" in a or "start" in a) and (
        "file explorer" in a or "explorer" in a
    ):
        return "open_file_explorer"

    if ("open" in a or "launch" in a or "start" in a) and (
        a.strip() in {"settings", "open settings", "launch settings", "start settings"}
        or "windows settings" in a
    ):
        return "open_settings"

    if ("close" in a or "exit" in a) and "settings" in a:
        return "close_settings"

    return None





def _learning_extract_safe_search_target(action):
    """
    Recognise a small set of harmless Start-menu search targets from a
    compound natural-language instruction. Unknown targets are rejected.
    """
    a = _normalise_learning_plan_text(action).lower()

    if "calculator" in a:
        return "calculator"
    if "notepad" in a:
        return "notepad"
    if "file explorer" in a or re.search(r"\bexplorer\b", a):
        return "file explorer"
    if "bluetooth" in a and ("settings" in a or "devices" in a):
        return "bluetooth"
    return None


def _learning_action_kind_smart_v48(action):
    """Handle compound instructions as one safe, atomic plan step."""
    a = _normalise_learning_plan_text(action).lower()

    target = _learning_extract_safe_search_target(action)

    # Example:
    # "Click Start, type calculator into the search box, then press Enter."
    # This is one semantic action, so execute all of its safe sub-actions
    # before running the step's final verification.
    if target and (
        ("start" in a or "windows key" in a)
        and ("type" in a or "search" in a)
        and ("enter" in a or "launch" in a or "open" in a)
    ):
        return f"start_search_{target.replace(' ', '_')}"

    return _learning_action_kind_smart(action)


def _learning_execute_action_smart_v48(action_kind):
    """Execute v48 compound actions using existing proven keyboard controls."""
    if action_kind.startswith("start_search_"):
        target = action_kind[len("start_search_"):].replace("_", " ")

        if target == "calculator":
            # Open Start, type the known-safe target, then Enter.
            if not _learning_execute_action_smart("open_start_menu"):
                return False
            time.sleep(0.35)
            if not _type_text("calculator"):
                return False
            time.sleep(0.75)
            return bool(_real_key(0x0D))  # VK_RETURN

        if target == "notepad":
            if not _learning_execute_action_smart("open_start_menu"):
                return False
            time.sleep(0.35)
            if not _type_text("notepad"):
                return False
            time.sleep(0.75)
            return bool(_real_key(0x0D))

        if target == "file explorer":
            if not _learning_execute_action_smart("open_start_menu"):
                return False
            time.sleep(0.35)
            if not _type_text("file explorer"):
                return False
            time.sleep(0.75)
            return bool(_real_key(0x0D))

        if target == "bluetooth":
            if not _learning_execute_action_smart("open_start_menu"):
                return False
            time.sleep(0.35)
            if not _type_text("bluetooth"):
                return False
            time.sleep(0.75)
            return bool(_real_key(0x0D))

        return False

    return _learning_execute_action_smart(action_kind)



def _learning_verify_start_menu_v49(timeout=10.0):
    """
    Verify the actual Windows Start UI, not the foreground window title.
    Windows often leaves the terminal as the foreground window while Start
    is open, so foreground-title checks are insufficient.
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        if UI_AUTOMATION_AVAILABLE:
            try:
                desktop = Desktop(backend="uia")

                # Windows 11 Start commonly exposes a Search control while
                # the Start surface is open. Look globally rather than
                # assuming a particular window title/class.
                for control in desktop.descendants():
                    try:
                        if not _ui_element_visible(control):
                            continue

                        name = _normalise_ui_name(control.window_text()).strip().lower()
                        control_type = ""
                        try:
                            control_type = str(control.element_info.control_type or "").lower()
                        except Exception:
                            pass

                        if name == "search" and control_type in {
                            "edit", "button", "pane", ""
                        }:
                            return True

                        # Some Windows builds expose the Start surface as a
                        # pane/window whose automation name contains Start.
                        if "start menu" in name or name == "start":
                            return True
                    except Exception:
                        continue

            except Exception:
                pass

        # A second UIA strategy: inspect known shell-related top-level windows.
        if UI_AUTOMATION_AVAILABLE:
            try:
                desktop = Desktop(backend="uia")
                for win in desktop.windows():
                    try:
                        if not _ui_element_visible(win):
                            continue
                        name = _normalise_ui_name(win.window_text()).lower()
                        cls = _normalise_ui_name(win.element_info.class_name).lower()
                        if (
                            "start" in name
                            or "shell experience" in name
                            or "shell experience" in cls
                            or "start" in cls
                        ):
                            return True
                    except Exception:
                        continue
            except Exception:
                pass

        time.sleep(0.25)

    return False


def _learning_verify_observable_v48(kind, timeout=10.0):
    """Use UI Automation as well as the foreground title for app verification."""
    if kind == "calculator":
        deadline = time.time() + timeout
        while time.time() < deadline:
            title = _learning_foreground_title_lower()
            if "calculator" in title:
                return True

            if UI_AUTOMATION_AVAILABLE:
                try:
                    desktop = Desktop(backend="uia")
                    for win in desktop.windows():
                        try:
                            if not _ui_element_visible(win):
                                continue
                            name = _normalise_ui_name(win.window_text()).lower()
                            if "calculator" in name:
                                return True
                        except Exception:
                            continue
                except Exception:
                    pass

            time.sleep(0.25)
        return False

    return _learning_verify_observable(kind, timeout=timeout)

def _learning_action_kind_smart(action):
    """Translate common AI-planner wording into the safe executor whitelist."""
    a = _normalise_learning_plan_text(action).lower()

    # Windows 11 Settings navigation for Display. These are semantic UIA
    # actions, not guessed screen coordinates.
    if "display" in a and any(word in a for word in ("click", "select", "choose", "open", "go to")):
        if "system" in a and ("display" in a):
            return "click_display_settings"
        if "display" in a and ("settings" in a or "display page" in a):
            return "click_display_settings"

    if ("system" in a and any(word in a for word in ("click", "select", "choose", "open", "go to"))
            and "settings" in a):
        return "click_system_settings"

    # Common Windows Settings shortcut wording.
    if "windows key + i" in a or "windows key and i" in a or "win+i" in a or "win + i" in a:
        if "settings" in a:
            return "open_settings"

    # Start-menu actions: use the Windows key rather than guessing taskbar pixels.
    if ("start button" in a or "start menu" in a or "windows start" in a) and (
        "click" in a or "open" in a or "press" in a or "select" in a
    ):
        return "open_start_menu"

    # Settings can be opened safely with the native Windows shortcut. This is
    # equivalent to selecting the Settings entry from Start, but is much more
    # reliable than trying to guess the gear's pixel position.
    if "settings" in a and any(word in a for word in (
        "click", "select", "choose", "open", "launch", "gear"
    )) and not ("bluetooth" in a and "devices" in a):
        return "open_settings"

    # Search-result actions: Windows may expose the Settings result as a
    # separate UIA item named "Bluetooth & other devices settings".
    if (
        ("search result" in a or "result" in a)
        and "bluetooth" in a
        and ("settings" in a or "devices" in a)
    ) or "bluetooth & other devices settings" in a:
        return "click_bluetooth_search_result"

    # Settings navigation through UI Automation.
    if "bluetooth & devices" in a or "bluetooth and devices" in a:
        return "click_bluetooth_devices"

    # Some AI plans accidentally phrase a verification as an action.
    if ("confirm" in a or "verify" in a or "check" in a or "make sure" in a or "ensure" in a):
        if "bluetooth" in a or "devices" in a:
            return "verify_bluetooth_settings"
        if "settings" in a:
            return "verify_settings"

    # Do not execute an optional alternative when the primary route is available.
    if "optional alternative" in a or a.startswith("optional:"):
        return "optional_skip"

    return _learning_action_kind(action)


def _learning_uia_click_named_control(name_variants, timeout=8.0):
    """Invoke/click a visible named UIA control in the foreground window."""
    if not UI_AUTOMATION_AVAILABLE:
        return False
    wanted = [v.lower() for v in name_variants]
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            hwnd = int(ctypes.windll.user32.GetForegroundWindow())
            if not hwnd:
                return False
            desktop = Desktop(backend="uia")
            foreground = desktop.window(handle=hwnd)
            for control in [foreground] + list(foreground.descendants()):
                try:
                    if not _ui_element_visible(control):
                        continue
                    name = _normalise_ui_name(control.window_text()).lower()
                    if not name:
                        continue
                    if not any(name == w or w in name for w in wanted):
                        continue
                    try:
                        control.invoke()
                        return True
                    except Exception:
                        try:
                            control.click_input()
                            return True
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _learning_execute_action_smart(action_kind):
    if action_kind == "open_start_menu":
        try:
            return bool(_real_key(0x5B))  # VK_LWIN
        except Exception:
            try:
                return bool(_key(0x5B))
            except Exception:
                return False

    if action_kind == "open_settings":
        try:
            subprocess.Popen(["cmd", "/c", "start", "", "ms-settings:"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    if action_kind == "click_system_settings":
        return _learning_uia_click_named_control(["System"], timeout=10.0)

    if action_kind == "click_display_settings":
        return _learning_uia_click_named_control(["Display"], timeout=10.0)

    if action_kind == "click_bluetooth_search_result":
        return _learning_uia_click_named_control([
            "Bluetooth & other devices settings",
            "Bluetooth & other devices",
            "Bluetooth settings",
            "Bluetooth & devices",
            "Bluetooth and devices",
        ], timeout=10.0)

    if action_kind == "click_bluetooth_devices":
        return _learning_uia_click_named_control([
            "Bluetooth & devices", "Bluetooth and devices", "Bluetooth devices"
        ])

    # These are observation steps, not extra physical actions.
    if action_kind in {"verify_bluetooth_settings", "verify_settings", "optional_skip"}:
        return True

    return _learning_execute_action(action_kind)
def _learning_verify_kind(verify_text, action_kind):
    """
    Map the planner's verification text to a conservative observable check.
    """
    v = _normalise_learning_plan_text(verify_text).lower()

    if action_kind == "click_system_settings":
        return "system_settings"

    if action_kind == "click_display_settings":
        return "display_settings"

    if action_kind == "open_bluetooth_settings":
        return "bluetooth_settings"

    if action_kind == "open_calculator":
        return "calculator"

    if action_kind == "open_notepad":
        return "notepad"

    if action_kind == "open_file_explorer":
        return "file_explorer"

    if action_kind == "open_start_menu":
        return "start_menu"

    if action_kind == "open_settings":
        return "settings"

    if action_kind == "close_settings":
        return "not_settings"

    # A verification sentence mentioning the target can still be useful,
    # but only when it contains a recognizable safe target.
    if "bluetooth" in v:
        return "bluetooth_settings"
    if "calculator" in v:
        return "calculator"
    if "notepad" in v:
        return "notepad"
    if "file explorer" in v or "explorer" in v:
        return "file_explorer"
    if "settings" in v:
        return "settings"

    return None


def _learning_foreground_title_lower():
    try:
        return _foreground_window_title().strip().lower()
    except Exception:
        return ""


def _learning_verify_observable(kind, timeout=8.0):
    """
    Verify only observable UI state. Never infer success from the command
    having been sent.
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        title = _learning_foreground_title_lower()

        if kind == "start_menu":
            if _learning_verify_start_menu_v49(timeout=1.5):
                return True

        elif kind == "system_settings":
            if UI_AUTOMATION_AVAILABLE:
                try:
                    desktop = Desktop(backend="uia")
                    hwnd = int(ctypes.windll.user32.GetForegroundWindow())
                    if hwnd:
                        foreground = desktop.window(handle=hwnd)
                        names = []
                        for control in [foreground] + list(foreground.descendants()):
                            try:
                                if _ui_element_visible(control):
                                    names.append(_normalise_ui_name(control.window_text()).lower())
                            except Exception:
                                continue
                        if any(name == "system" or name.startswith("system") for name in names):
                            return True
                except Exception:
                    pass
            if "settings" in title and "system" in title:
                return True

        elif kind == "display_settings":
            if UI_AUTOMATION_AVAILABLE:
                try:
                    desktop = Desktop(backend="uia")
                    hwnd = int(ctypes.windll.user32.GetForegroundWindow())
                    if hwnd:
                        foreground = desktop.window(handle=hwnd)
                        names = []
                        for control in [foreground] + list(foreground.descendants()):
                            try:
                                if _ui_element_visible(control):
                                    names.append(_normalise_ui_name(control.window_text()).lower())
                            except Exception:
                                continue
                        if any(name == "display" or name.startswith("display ") for name in names):
                            # Settings can contain a Display navigation item even
                            # when the page itself is not selected. Require the
                            # foreground title or display-specific controls too.
                            if "display" in title or any("scale" in n or "resolution" in n or "brightness" in n for n in names):
                                return True
                except Exception:
                    pass
            if "display" in title and "settings" in title:
                return True

        elif kind == "bluetooth_settings":
            # Reuse the proven UIA/content verifier from the Bluetooth pilot.
            try:
                if _verify_bluetooth_settings_open(timeout=1.0):
                    return True
            except Exception:
                pass
            if "bluetooth" in title and "settings" in title:
                return True

        elif kind == "calculator":
            if "calculator" in title:
                return True

        elif kind == "notepad":
            if "notepad" in title:
                return True

        elif kind == "file_explorer":
            if "file explorer" in title or title.startswith("explorer"):
                return True

        elif kind == "settings":
            if "settings" in title:
                return True

        elif kind == "not_settings":
            if title and "settings" not in title:
                return True

        time.sleep(0.25)

    return False


def _learning_execute_action(action_kind):
    """
    Execute one action from the strict whitelist.
    Returns True if the action was dispatched successfully.
    """
    try:
        if action_kind == "open_bluetooth_settings":
            return bool(_open_bluetooth_settings_for_learning())

        if action_kind == "open_calculator":
            return bool(find_and_open_app("calculator"))

        if action_kind == "open_notepad":
            return bool(find_and_open_app("notepad"))

        if action_kind == "open_file_explorer":
            try:
                os.startfile(os.path.expandvars(r"%WINDIR%\explorer.exe"))
                return True
            except Exception:
                return False

        if action_kind == "open_settings":
            subprocess.Popen(
                ["cmd", "/c", "start", "", "ms-settings:"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True

        if action_kind == "close_settings":
            hwnd = int(ctypes.windll.user32.GetForegroundWindow())
            if not hwnd:
                return False
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
            return True

    except Exception as error:
        print("LEARNING EXECUTOR: action error:", error)

    return False


def _learning_execute_pending_plan():
    """
    Run a temporary plan from the first step forward.
    Every step must be understood by the whitelist and verified before the
    next step begins. The plan is never promoted to verified memory unless
    every step succeeds.
    """
    global pending_learning_plan

    if not pending_learning_plan:
        say("There isn't a temporary learning plan to execute.")
        return True

    plan = pending_learning_plan

    if plan.get("requires_confirmation") or plan.get("risk") in {"high", "unknown"}:
        say("I won't execute this plan automatically because it requires confirmation or has an uncertain risk.")
        print("LEARNING EXECUTOR: blocked by safety gate.")
        return True

    print("\nLEARNING EXECUTOR: controlled execution starting.")
    say(f"I'll try the plan for {plan['goal']} one step at a time and verify each step.")

    completed = []
    for number, step in enumerate(plan.get("steps", []), 1):
        action = step.get("action", "")
        verify_text = step.get("verify", "")
        action_kind = _learning_action_kind_smart_v48(action)
        verify_kind = _learning_verify_kind(verify_text, action_kind)
        if action_kind == "verify_bluetooth_settings":
            verify_kind = "bluetooth_settings"
        elif action_kind == "verify_settings":
            verify_kind = "settings"
        elif action_kind == "optional_skip":
            verify_kind = None

        print(f"LEARNING STEP {number}: ACTION: {action}")
        print(f"LEARNING STEP {number}: VERIFY: {verify_text}")

        if not action_kind:
            print(f"LEARNING EXECUTOR: unsupported step {number}; stopping safely.")
            say(
                f"I understand the plan, but I don't have a safe executor for step {number}. "
                "I stopped rather than guessing."
            )
            plan["last_result"] = "blocked_unsupported_step"
            plan["completed_steps"] = completed
            return True

        if action_kind == "optional_skip":
            print(f"LEARNING STEP {number}: OPTIONAL ALTERNATIVE SKIPPED")
            completed.append(number)
            say(f"Step {number} is an optional alternative, so I skipped it.")
            continue

        if not verify_kind:
            print(f"LEARNING EXECUTOR: no safe verification for step {number}; stopping safely.")
            say(f"I don't have a safe way to verify step {number}, so I stopped rather than guessing.")
            plan["last_result"] = "blocked_missing_verification"
            plan["completed_steps"] = completed
            return True

        if not _learning_execute_action_smart_v48(action_kind):
            print(f"LEARNING EXECUTOR: dispatch failed at step {number}.")
            say(f"Step {number} failed to start, so I stopped and did not mark the task as learned.")
            plan["last_result"] = "dispatch_failed"
            plan["completed_steps"] = completed
            return True

        if _learning_verify_observable_v48(verify_kind, timeout=10.0):
            completed.append(number)
            print(f"LEARNING STEP {number}: VERIFIED")
            say(f"Step {number} worked and I verified the result.")

            # If this step proves the requested goal is reached, finish now.
            goal_text = str(plan.get("goal", "")).lower()
            if verify_kind == "display_settings" and "display" in goal_text:
                print("LEARNING EXECUTOR: GOAL REACHED; no further steps required.")
                break
        else:
            print(f"LEARNING STEP {number}: VERIFICATION FAILED")
            say(f"I couldn't verify step {number}, so I stopped rather than guessing.")
            plan["last_result"] = "verification_failed"
            plan["completed_steps"] = completed
            return True

    plan["last_result"] = "verified"
    plan["completed_steps"] = completed
    plan["temporary"] = False

    # Only now create/promote a verified learned routine.
    save_learned_routine(
        plan["goal"],
        method=plan.get("summary", "Verified by controlled learn-by-doing execution"),
        steps=[
            f"action: {step['action']} | verify: {step['verify']}"
            for step in plan.get("steps", [])
        ],
        source="learn-by-doing-verified",
        state="verified",
    )

    say(f"All {len(completed)} steps worked and were verified. I've learned the routine.")
    print(f"LEARNING EXECUTOR: VERIFIED AND REMEMBERED {plan['goal']!r}.")
    return True


def handle_learning_executor_commands(command):
    """
    Explicit commands for controlled execution.
    Execution never happens merely because a plan exists.
    """
    lower = command.strip().lower().replace("’", "'").replace("`", "'")

    if lower in {
        "run learning plan",
        "execute learning plan",
        "try the learning plan",
        "run the learning plan",
        "execute the learning plan",
    }:
        return _learning_execute_pending_plan()

    return False


def handle_learning_plan_commands(command):
    """
    Create and inspect temporary learning plans.
    This stage deliberately does NOT execute plans or save them as verified routines.
    """
    global pending_learning_plan

    c = command.strip()
    lower = c.lower().replace("’", "'").replace("`", "'")

    prefixes = (
        # These phrases are explicitly treated as learning-plan requests.
        # In particular, "create a plan for ..." must NOT fall through to
        # Jarvis's normal conversational planner, which can return a
        # multi-option/general-purpose answer instead of an executable plan.
        "plan how to ",
        "make a learning plan for ",
        "prepare a learning plan for ",
        "figure out how to ",
        "create a learning plan for ",
        "create a plan for ",
    )

    for prefix in prefixes:
        if lower.startswith(prefix):
            goal = _normalise_learning_plan_text(c[len(prefix):])
            if not goal:
                say("Tell me what task you want me to plan.")
                return True

            say("I'll work out a temporary plan first. I won't execute it yet.")
            plan, error = _build_learning_plan_with_local_brain(goal)

            if not plan:
                say("I couldn't safely create a learning plan for that yet.")
                print(f"LEARNING PLANNER: failed: {error}")
                return True

            pending_learning_plan = plan
            _print_learning_plan(plan)

            if plan["requires_confirmation"]:
                say(
                    f"I've prepared a temporary plan for {plan['goal']}. "
                    "It has a potentially sensitive side effect, so it will require your confirmation before any future attempt."
                )
            else:
                say(
                    f"I've prepared a temporary plan for {plan['goal']}. "
                    "I haven't executed any of it."
                )
            return True

    if lower in {
        "show learning plan",
        "show the learning plan",
        "show current learning plan",
        "what is the learning plan",
    }:
        if not pending_learning_plan:
            say("There isn't a temporary learning plan at the moment.")
            return True
        _print_learning_plan(pending_learning_plan)
        say("That's the current temporary learning plan. Nothing has been executed.")
        return True

    if lower in {
        "discard learning plan",
        "clear learning plan",
        "cancel learning plan",
    }:
        if pending_learning_plan:
            pending_learning_plan = None
            say("The temporary learning plan has been discarded.")
        else:
            say("There isn't a temporary learning plan to discard.")
        return True

    return False


# ============================================================
# LEARNED STEAM PLAY ROUTINES
# ============================================================

def _normalise_routine_name(value):
    return re.sub(r"\s+", " ", str(value).strip().lower())


def save_steam_play_routine(game_name):
    """Remember a semantic Steam play sequence; never store screen coordinates."""
    game_key = _normalise_routine_name(game_name)
    if not game_key:
        return False
    routines = jarvis_memory.setdefault("routines", {})
    routines[game_key] = {
        "type": "steam_play",
        "game": str(game_name).strip(),
        "steps": [
            "open_or_focus_steam",
            "search_steam_for_game",
            "open_matching_game_result",
            "find_and_click_play"
        ]
    }
    return save_jarvis_memory()


def get_steam_play_routine(game_name):
    routines = jarvis_memory.setdefault("routines", {})
    return routines.get(_normalise_routine_name(game_name))


def _find_steam_play_control():
    """Find the actual Play button on the foreground Steam game page."""
    if not UI_AUTOMATION_AVAILABLE or not _foreground_is_steam():
        return None
    try:
        desktop = Desktop(backend='uia')
        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        foreground = desktop.window(handle=hwnd)
        candidates = []
        for control in foreground.descendants():
            try:
                name = _normalise_ui_name(control.window_text())
                ctype = str(control.element_info.control_type or '')
                if name != 'play' or ctype not in {'Button', 'Hyperlink', 'Text'}:
                    continue
                if not _ui_element_visible(control):
                    continue
                rect = _ui_element_rect(control)
                if not rect:
                    continue
                l, top, r, b = rect
                w, h = r-l, b-top
                if w <= 0 or h <= 0 or w > 500 or h > 180:
                    continue
                score = 100
                if ctype == 'Button':
                    score += 30
                if w >= 80 and h >= 30:
                    score += 15
                try:
                    if control.is_enabled():
                        score += 10
                except Exception:
                    pass
                candidates.append((score, control, name, rect, ctype))
            except Exception:
                continue
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0]
    except Exception as error:
        print(f"STEAM PLAY UIA ERROR: {error}")
        return None


def _find_foreground_steam_control_exact(target_names):
    """Find an exact named control in the foreground Steam window."""
    if not UI_AUTOMATION_AVAILABLE or not _foreground_is_steam():
        return None
    try:
        desktop = Desktop(backend='uia')
        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        foreground = desktop.window(handle=hwnd)
        wanted = {_normalise_ui_name(x) for x in target_names}
        candidates = []
        for control in foreground.descendants():
            try:
                name = _normalise_ui_name(control.window_text())
                if name not in wanted or not _ui_element_visible(control):
                    continue
                rect = _ui_element_rect(control)
                if not rect:
                    continue
                ctype = str(control.element_info.control_type or '')
                if ctype not in {'Button', 'Hyperlink', 'TabItem', 'Text'}:
                    continue
                l, top, r, bottom = rect
                if r <= l or bottom <= top or (r-l) > 700 or (bottom-top) > 180:
                    continue
                score = 100 if ctype in {'Button', 'Hyperlink', 'TabItem'} else 80
                if ctype == 'TabItem':
                    score += 20
                candidates.append((score, control, name, rect, ctype))
            except Exception:
                continue
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0]
    except Exception as error:
        print(f"STEAM LIBRARY UIA ERROR: {error}")
        return None


def _open_steam_library():
    """Open Steam's Library tab using the actual foreground UI control."""
    result = _find_foreground_steam_control_exact({'library'})
    if not result:
        print("STEAM ROUTINE: Library control was not exposed by Steam UIA.")
        return False
    score, element, name, rect, ctype = result
    print(f"STEAM ROUTINE: found Library type={ctype} box={rect}.")
    try:
        element.invoke()
        print("STEAM ROUTINE: invoked Library directly.")
        time.sleep(0.8)
        return True
    except Exception as exc:
        print(f"STEAM ROUTINE: Library direct invoke unavailable: {exc}")
    l, top, r, bottom = rect
    x, y = (l+r)//2, (top+bottom)//2
    left, top0, width, height = _virtual_screen_bounds()
    if not (left <= x < left + width and top0 <= y < top0 + height):
        return False
    if _mouse_move(x, y):
        time.sleep(0.05)
        _real_mouse_click('left', 1)
        time.sleep(0.8)
        return True
    return False


def _wait_for_steam_foreground(timeout=12.0):
    """Wait for Steam to become the actual foreground application."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _foreground_is_steam():
            return True
        time.sleep(0.25)
    return _foreground_is_steam()


def _steam_play_current_game(game_name):
    """Execute a remembered semantic routine, starting Steam if necessary."""
    print(f"STEAM ROUTINE: running learned play routine for {game_name!r}.")

    # Free the local AI's ~12GB from memory as early as possible in the
    # launch flow, so it has the whole rest of this routine (opening
    # Steam, searching, navigating) to actually finish before the game
    # itself starts loading assets.
    unload_ollama_models()

    # Step 0: if Steam is not foreground, open/focus it automatically.
    if not _foreground_is_steam():
        print("STEAM ROUTINE: Steam is not foreground; opening Steam automatically.")
        say("Opening Steam, then I'll start the routine.")
        found = find_and_open_app("Steam")
        if not found:
            say("I couldn't find Steam on this PC.")
            print("STEAM ROUTINE: could not find Steam.")
            return True
        if not _wait_for_steam_foreground(12.0):
            say("Steam opened, but I couldn't safely bring it to the foreground.")
            print("STEAM ROUTINE: Steam did not become foreground within the timeout.")
            return True
        print("STEAM ROUTINE: Steam is now foreground.")
        time.sleep(1.0)

    # Step 1: explicitly go to Steam Library before searching.
    if not _open_steam_library():
        say("I opened Steam, but I couldn't safely open its Library tab.")
        print("STEAM ROUTINE: stopped because Library could not be opened.")
        return True
    say("Steam Library is open. Starting the routine.")

    # Step 2: search for the game using the existing proven search handler.
    if not handle_steam_search_command(f"search Steam for {game_name}"):
        return True
    time.sleep(1.0)

    # Step 2: open the matching result using the existing proven UIA path.
    if not handle_steam_open_game_command(f"open {game_name}"):
        return True
    time.sleep(1.2)

    # Step 3: find the actual Play control and invoke it directly.
    result = _find_steam_play_control()
    if not result:
        say(f"I opened {game_name}, but I couldn't safely find its Play button.")
        print(f"STEAM ROUTINE: Play button not found for {game_name!r}; routine stopped safely.")
        return True

    score, element, name, rect, ctype = result
    l, top, r, bottom = rect
    x, y = (l+r)//2, (top+bottom)//2
    print(f"STEAM ROUTINE: Play target box=({l},{top},{r},{bottom}), click={x},{y}, score={score:.0f}.")
    try:
        element.invoke()
        print("STEAM ROUTINE: invoked Play directly.")
        say(f"Launching {game_name}.")
        return True
    except Exception as exc:
        print(f"STEAM ROUTINE: direct Play invoke unavailable: {exc}")

    left, top0, width, height = _virtual_screen_bounds()
    if not (left <= x < left + width and top0 <= y < top0 + height):
        say("I found Play, but its coordinates were outside the desktop, so I stopped safely.")
        return True
    if _mouse_move(x, y):
        time.sleep(0.05)
        _real_mouse_click('left', 1)
        say(f"Launching {game_name}.")
    else:
        say(f"I found Play, but I couldn't activate it safely.")
    return True


def handle_steam_play_routine_command(command):
    """If a saved Steam routine exists, 'open <game>' can run it end-to-end."""
    c = command.strip()
    lower = c.lower()
    prefixes = ('open the game ', 'open game ', 'open ')
    game = None
    for prefix in prefixes:
        if lower.startswith(prefix):
            game = c[len(prefix):].strip()
            break
    if not game:
        return False
    if game.lower() in {'steam', 'settings', 'view', 'install', 'play', 'search'}:
        return False
    routine = get_steam_play_routine(game)
    if not routine or routine.get('type') != 'steam_play':
        return False
    return _steam_play_current_game(routine.get('game') or game)


def handle_smart_routine_phrases(command):
    """Route natural Steam play phrases to an existing saved semantic routine.

    Only handles a game when Jarvis already has a saved Steam routine for it,
    so ordinary commands such as 'launch Chrome' are left alone.
    """
    c = command.strip()
    lower = c.lower().strip()
    # Phone speech/text can use a curly apostrophe (I’m) instead of a
    # straight apostrophe (I'm). Normalize both so the same smart routine
    # works from PC voice, phone voice, and typed input.
    lower = lower.replace("’", "'").replace("`", "'")

    prefixes = (
        "i'm ready to play ",
        "im ready to play ",
        "i am ready to play ",
        "ready to play ",
        "let's play ",
        "lets play ",
        "launch ",
        "start ",
        "play ",
        "open the game ",
        "open game ",
        "open ",
    )

    game = None
    for prefix in prefixes:
        if lower.startswith(prefix):
            game = c[len(prefix):].strip()
            break

    if not game:
        return False

    # Never let the smart router steal obvious non-game commands.
    if game.lower() in {
        'steam', 'settings', 'view', 'install', 'play', 'search',
        'chrome', 'edge', 'firefox', 'file explorer', 'explorer',
        'notepad', 'calculator'
    }:
        return False

    # IMPORTANT: use the existing, proven routine handler rather than a
    # nonexistent run_steam_game_routine() function.
    routine = get_steam_play_routine(game)
    if not routine or routine.get('type') != 'steam_play':
        return False

    print(f"SMART ROUTINE: matched saved Steam routine for {game!r}.")
    return handle_steam_play_routine_command(f"open {game}")


def handle_steam_remember_routine_command(command):
    """Allow Jarvis to explicitly remember a Steam play routine for a game."""
    c = command.strip()
    lower = c.lower()
    prefixes = (
        'remember a play routine for ',
        'remember the play routine for ',
        'remember how to play ',
        'save a play routine for ',
        'teach jarvis to play ',
        'teach jarvis how to play ',
        'teach me how to play ',
        'teach jarvis to launch ',
        'save the routine for ',
    )
    game = None
    for prefix in prefixes:
        if lower.startswith(prefix):
            game = c[len(prefix):].strip()
            break
    if not game:
        return False
    if save_steam_play_routine(game):
        say(f"I'll remember the Steam play routine for {game}.")
        print(f"STEAM ROUTINE: saved semantic play routine for {game!r}.")
    else:
        say("I couldn't save that routine.")
    return True


# ============================================================
# STEAM SEARCH
# ============================================================
def _find_foreground_search_edit():
    """Find a likely search/edit control in the current foreground window only."""
    if not UI_AUTOMATION_AVAILABLE:
        return None
    try:
        desktop = Desktop(backend='uia')
        fg_hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        if not fg_hwnd:
            return None
        foreground = desktop.window(handle=fg_hwnd)
        controls = list(foreground.descendants())
        candidates = []
        for control in controls:
            try:
                name = _normalise_ui_name(control.window_text())
                ctype = str(control.element_info.control_type or '')
                rect = _ui_element_rect(control)
                if not rect or not _ui_element_visible(control):
                    continue
                l, t, r, b = rect
                width, height = r-l, b-t
                if width <= 0 or height <= 0 or width > 900 or height > 120:
                    continue
                if ctype not in {'Edit', 'ComboBox'}:
                    continue
                score = 0
                if 'search' in name:
                    score += 100
                if 'store' in name:
                    score += 25
                if 'find' in name:
                    score += 15
                if 120 <= width <= 700:
                    score += 20
                try:
                    if control.is_enabled():
                        score += 10
                except Exception:
                    pass
                candidates.append((score, control, name, rect, ctype))
            except Exception:
                continue
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0]
    except Exception as error:
        print(f"STEAM SEARCH UIA ERROR: {error}")
        return None


def handle_steam_search_command(command):
    """Handle natural-language Steam searches using the actual foreground search field."""
    c = command.strip()
    lower = c.lower()
    prefixes = (
        'search steam for ',
        'search steam for a game called ',
        'find on steam ',
        'find on steam the game ',
        'look for on steam ',
        'look up on steam ',
        'search steam ',
    )
    game = None
    for prefix in prefixes:
        if lower.startswith(prefix):
            game = c[len(prefix):].strip()
            break
    if not game:
        return False
    if game.lower().startswith('the game '):
        game = game[9:].strip()
    if not game:
        say("Tell me which game you want me to search for on Steam.")
        return True

    if not UI_AUTOMATION_AVAILABLE:
        say("Windows UI Automation isn't available, so I can't safely control Steam's search box.")
        return True

    result = _find_foreground_search_edit()
    if not result:
        say("I couldn't find Steam's search box in the foreground window.")
        return True

    score, element, name, rect, ctype = result
    l, t, r, b = rect
    print(f"STEAM SEARCH: found search field {name!r} type={ctype} box=({l},{t},{r},{b})")
    try:
        element.set_focus()
    except Exception:
        pass
    time.sleep(0.12)

    # Clear the existing search without depending on mouse coordinates.
    try:
        _real_key(0x11)  # Ctrl down
        _real_key(0x41)  # A
        _real_key(0x11)  # Ctrl up
    except Exception:
        pass
    # Use a reliable direct key sequence for Ctrl+A.
    u = ctypes.windll.user32
    u.keybd_event(0x11, 0, 0, 0)
    u.keybd_event(0x41, 0, 0, 0)
    u.keybd_event(0x41, 0, 2, 0)
    u.keybd_event(0x11, 0, 2, 0)
    time.sleep(0.05)

    if not _real_type_text(game):
        say("I found Steam's search box, but I couldn't type the game name.")
        return True
    time.sleep(0.08)
    _real_key(0x0D)  # Enter
    print(f"STEAM SEARCH: searched for {game!r} and pressed Enter.")
    say(f"Searching Steam for {game}.")
    return True

def _foreground_is_steam():
    """Return True only when the actual Windows foreground process is Steam."""
    try:
        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        if not hwnd:
            return False

        # Check the executable name when possible.
        pid = wintypes.DWORD(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if handle:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buf))
                if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    exe = buf.value.lower()
                    if exe.endswith('\\steam.exe') or exe.endswith('/steam.exe'):
                        return True
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)

        # Conservative title fallback for Steam windows.
        title_buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(hwnd, title_buf, len(title_buf))
        title = title_buf.value.lower()
        return 'steam' in title
    except Exception:
        return False


def _find_steam_game_result(game_name):
    """Find a matching game result in the foreground Steam window only."""
    if not UI_AUTOMATION_AVAILABLE or not _foreground_is_steam():
        return None
    try:
        desktop = Desktop(backend='uia')
        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        foreground = desktop.window(handle=hwnd)
        controls = list(foreground.descendants())
        target = _normalise_ui_name(game_name)
        if not target:
            return None

        preferred_types = {
            'ListItem': 40, 'Hyperlink': 35, 'Button': 30,
            'TabItem': 15, 'Text': 5
        }
        candidates = []
        for control in controls:
            try:
                name = _normalise_ui_name(control.window_text())
                if not name or not _ui_element_visible(control):
                    continue
                rect = _ui_element_rect(control)
                if not rect:
                    continue
                ctype = str(control.element_info.control_type or '')
                if ctype not in preferred_types:
                    continue

                score = 0
                if name == target:
                    score = 120
                elif target in name:
                    score = 95
                elif name in target and len(name) >= 4:
                    score = 75
                else:
                    continue

                score += preferred_types[ctype]
                l, top, r, bottom = rect
                width, height = r-l, bottom-top
                if width <= 0 or height <= 0 or width > 1100 or height > 250:
                    continue
                # Prefer result-sized controls rather than giant containers.
                score += max(0, 20 - min(20, (width * height) / 50000.0))
                try:
                    if not control.is_enabled():
                        score -= 40
                except Exception:
                    pass
                candidates.append((score, control, name, rect, ctype))
            except Exception:
                continue

        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0]
    except Exception as error:
        print(f"STEAM OPEN UIA ERROR: {error}")
        return None


def handle_steam_open_game_command(command):
    """Open a named Steam search result using the foreground Steam window only."""
    c = command.strip()
    lower = c.lower()
    prefixes = (
        'open the game ',
        'open game ',
        'open ',
        'select the game ',
        'select game ',
    )
    game = None
    for prefix in prefixes:
        if lower.startswith(prefix):
            game = c[len(prefix):].strip()
            break
    if not game:
        return False

    # Do not hijack ordinary app/file commands unless Steam is actually active.
    if not _foreground_is_steam():
        return False

    # Avoid interpreting obvious non-game UI commands as game names.
    if game.lower() in {'steam', 'settings', 'view', 'install', 'play', 'search'}:
        return False

    result = _find_steam_game_result(game)
    if not result:
        say(f"I couldn't find {game} in the current Steam results.")
        return True

    score, element, name, rect, ctype = result
    l, top, r, bottom = rect
    x, y = (l + r) // 2, (top + bottom) // 2
    print(
        f"STEAM OPEN: found {game!r} as {name!r} type={ctype} "
        f"box=({l},{top},{r},{bottom}), click={x},{y}, score={score:.0f}"
    )

    # Prefer UI Automation invocation when the result supports it.
    try:
        element.invoke()
        print(f"STEAM OPEN: invoked {name!r} directly.")
        say(f"Opening {name}.")
        return True
    except Exception as exc:
        print(f"STEAM OPEN: direct invoke unavailable: {exc}")

    # Fallback to a guarded physical click on the exact UIA bounding box.
    left, top, width, height = _virtual_screen_bounds()
    if not (left <= x < left + width and top <= y < top + height):
        print(f"STEAM OPEN: blocked unsafe coordinates {x},{y}")
        say("I found the game, but its coordinates are outside the desktop.")
        return True

    if not _mouse_move(x, y):
        say(f"I found {name}, but I couldn't open it.")
        return True
    time.sleep(0.05)
    _real_mouse_click('left', 1)
    print(f"STEAM OPEN: clicked {name!r} at {x},{y}.")
    say(f"Opening {name}.")
    return True


# ============================================================
# XBOX PC GAME LAUNCHER
# ============================================================

def _foreground_is_xbox():
    """Return True only when the actual foreground window is the Xbox app."""
    try:
        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        if not hwnd:
            return False

        pid = wintypes.DWORD(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
        )
        if handle:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buf))
                if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size)
                ):
                    exe = buf.value.lower().replace('/', '\\')
                    if exe.endswith('\\xboxpcapp.exe') or exe.endswith('\\xbox.exe'):
                        return True
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)

        title_buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(hwnd, title_buf, len(title_buf))
        title = title_buf.value.lower()
        return 'xbox' in title
    except Exception:
        return False


def _wait_for_xbox_foreground(timeout=30.0):
    """Wait longer for the Xbox app to become the actual foreground application."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _foreground_is_xbox():
            return True
        time.sleep(0.25)
    return _foreground_is_xbox()


def _wait_for_xbox_ready(timeout=30.0):
    """Wait for Xbox to finish loading enough UI for UI Automation to see it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _foreground_is_xbox():
            # Give the Xbox shell a moment to finish its WebView/UI startup.
            try:
                if _find_foreground_search_edit():
                    print("XBOX: search UI is ready.")
                    return True
            except Exception:
                pass
            try:
                if _find_target_via_windows_uia("library") or _find_target_via_windows_uia("my library"):
                    print("XBOX: library UI is ready.")
                    return True
            except Exception:
                pass
        time.sleep(0.5)
    print("XBOX: app foregrounded, but UI readiness check timed out; continuing safely.")
    return _foreground_is_xbox()


def _invoke_foreground_uia_target(target_names):
    """Find and invoke one exact/near-exact target in the current foreground window."""
    if not UI_AUTOMATION_AVAILABLE:
        return False
    for target in target_names:
        result = _find_target_via_windows_uia(target)
        if not result:
            continue
        element = result.get('uia_element')
        if element is None:
            continue
        try:
            element.invoke()
            print(f"XBOX UIA: invoked {target!r} directly.")
            return True
        except Exception as exc:
            print(f"XBOX UIA: invoke failed for {target!r}: {exc}")
            # Safe physical fallback only to the UIA-confirmed rectangle.
            try:
                x, y = int(result['x']), int(result['y'])
                left, top, width, height = _virtual_screen_bounds()
                if left <= x < left + width and top <= y < top + height:
                    if _mouse_move(x, y):
                        time.sleep(0.05)
                        _real_mouse_click('left', 1)
                        print(f"XBOX UIA: clicked {target!r} at {x},{y}.")
                        return True
            except Exception as fallback_exc:
                print(f"XBOX UIA: safe click fallback failed: {fallback_exc}")
    return False


def _xbox_safe_click_uia_result(result, label):
    """Safely click a UIA-confirmed Xbox result when direct Invoke is unavailable."""
    if not result:
        return False
    try:
        x, y = int(result["x"]), int(result["y"])
        left, top, width, height = _virtual_screen_bounds()
        if not (left <= x < left + width and top <= y < top + height):
            print(f"XBOX: UIA result for {label!r} was outside the virtual screen; refusing click.")
            return False
        if not _mouse_move(x, y):
            print(f"XBOX: could not move to UIA-confirmed {label!r} target.")
            return False
        time.sleep(0.05)
        _real_mouse_click("left", 1)
        print(f"XBOX: safely clicked UIA-confirmed {label!r} at {x},{y}.")
        return True
    except Exception as exc:
        print(f"XBOX: safe UIA click for {label!r} failed: {exc}")
        return False


def _wait_for_xbox_game_result(game_name, timeout=90.0):
    """Wait for Xbox to finish populating its library/search results."""
    deadline = time.time() + timeout
    last_report = 0.0
    while time.time() < deadline:
        if not _foreground_is_xbox():
            time.sleep(0.5)
            continue
        try:
            result = _find_target_via_windows_uia(game_name)
            if result and result.get('uia_element') is not None:
                print(f"XBOX: game {game_name!r} is now visible in the foreground UI.")
                return result
        except Exception:
            pass
        now = time.time()
        if now - last_report >= 10.0:
            remaining = max(0, int(deadline - now))
            print(f"XBOX: still waiting for {game_name!r} to load (about {remaining}s remaining).")
            last_report = now
        time.sleep(0.75)
    return None


def _xbox_find_game_and_open(game_name):
    """Open a game visible in the foreground Xbox app using UI Automation."""
    if not _foreground_is_xbox():
        return False

    # First try the game exactly where the Xbox app currently is.
    result = _find_target_via_windows_uia(game_name)
    if result and result.get('uia_element') is not None:
        element = result['uia_element']
        try:
            element.invoke()
            print(f"XBOX: invoked {game_name!r} directly.")
            return True
        except Exception as exc:
            print(f"XBOX: direct game invoke failed: {exc}")
            try:
                x, y = int(result['x']), int(result['y'])
                left, top, width, height = _virtual_screen_bounds()
                if left <= x < left + width and top <= y < top + height and _mouse_move(x, y):
                    time.sleep(0.05)
                    _real_mouse_click('left', 1)
                    print(f"XBOX: clicked {game_name!r} at {x},{y}.")
                    return True
            except Exception:
                pass

    # If the game is not exposed on the current page, open the Library area.
    if _invoke_foreground_uia_target(("my library", "library", "my games")):
        # Xbox can take around a minute to populate a previously empty library.
        # Do not move on to search or declare failure while that load is still happening.
        time.sleep(1.0)

    # Wait for the library contents to actually populate.
    result = _wait_for_xbox_game_result(game_name, timeout=90.0)
    if result and result.get('uia_element') is not None:
        try:
            result['uia_element'].invoke()
            print(f"XBOX: invoked {game_name!r} from Library directly.")
            return True
        except Exception as exc:
            print(f"XBOX: Library game invoke failed: {exc}")
            # Xbox sometimes exposes the game as a MenuItem that refuses UIA Invoke.
            # The UIA rectangle is still trustworthy, so use the same guarded physical
            # click that worked for the Library navigation.
            if _xbox_safe_click_uia_result(result, game_name):
                time.sleep(1.0)
                print(f"XBOX: physical fallback opened {game_name!r} from Library.")
                return True

    # Finally use the Xbox search control if the app exposes one.
    search = _find_foreground_search_edit()
    if search:
        score, element, name, rect, ctype = search
        print(f"XBOX SEARCH: found search field {name!r} type={ctype} box={rect}")
        try:
            element.set_focus()
        except Exception:
            pass
        time.sleep(0.12)

        # Ctrl+A, then type the game name and press Enter.
        try:
            u = ctypes.windll.user32
            u.keybd_event(0x11, 0, 0, 0)
            u.keybd_event(0x41, 0, 0, 0)
            u.keybd_event(0x41, 0, 2, 0)
            u.keybd_event(0x11, 0, 2, 0)
        except Exception:
            pass

        if not _real_type_text(game_name):
            print("XBOX SEARCH: could not type game name.")
            return False
        time.sleep(0.15)
        _real_key(0x0D)
        print(f"XBOX SEARCH: searched for {game_name!r} and pressed Enter.")

        # Search results can also take a substantial amount of time to appear.
        result = _wait_for_xbox_game_result(game_name, timeout=90.0)
        if result and result.get('uia_element') is not None:
            try:
                result['uia_element'].invoke()
                print(f"XBOX: invoked search result {game_name!r} directly.")
                return True
            except Exception as exc:
                print(f"XBOX: search result invoke failed: {exc}")
                # Same safe fallback for Xbox search results that expose no Invoke pattern.
                if _xbox_safe_click_uia_result(result, game_name):
                    time.sleep(1.0)
                    print(f"XBOX: physical fallback opened search result {game_name!r}.")
                    return True

    return False


def handle_xbox_game_command(command):
    """Launch an installed Xbox/PC Game Pass game without requiring manual clicks."""
    c = command.strip()
    lower = c.lower().replace('’', "'")
    prefixes = (
        'i\'m ready to play ', 'im ready to play ', 'i am ready to play ',
        'ready to play ', 'let\'s play ', 'lets play ',
        'launch ', 'start ', 'play ', 'open the game ', 'open game ', 'open '
    )
    game = None
    for prefix in prefixes:
        if lower.startswith(prefix):
            game = c[len(prefix):].strip()
            break
    if not game:
        return False

    # Explicit Xbox wording is always accepted. Otherwise only handle
    # unknown game-like names here, leaving normal applications alone.
    xbox_explicit = lower.startswith(('xbox ', 'xbox app '))
    if lower.startswith('xbox app '):
        game = c[9:].strip()
    elif lower.startswith('xbox '):
        game = c[5:].strip()

    blocked = {
        'steam', 'settings', 'view', 'install', 'search', 'chrome', 'edge',
        'firefox', 'file explorer', 'explorer', 'notepad', 'calculator',
        'discord', 'spotify', 'epic', 'epic games', 'xbox', 'youtube',
        'google', 'gmail', 'chatgpt'
    }
    if not game or game.lower() in blocked:
        return False

    # v35 deliberately treats Spider-Man as an Xbox game because the user
    # confirmed it is in the Xbox library. Future games can use the same path.
    known_xbox_games = {'spider-man', 'spiderman', 'marvel spider-man'}
    if not xbox_explicit and game.lower() not in known_xbox_games:
        return False

    say(f"Opening Xbox, then I'll launch {game}.")
    print(f"XBOX ROUTINE: requested game {game!r}.")

    # Free the local AI's ~12GB from memory as early as possible, so it
    # has the whole rest of this routine (opening Xbox, navigating to the
    # game page, waiting for Play) to actually finish before the game
    # itself starts loading assets.
    unload_ollama_models()

    if not _foreground_is_xbox():
        found = find_and_open_app('Xbox')
        if not found:
            say("I couldn't find the Xbox app on this PC.")
            print("XBOX ROUTINE: Xbox app not found.")
            return True
        if not _wait_for_xbox_foreground(30.0):
            say("Xbox opened, but I couldn't safely bring it to the foreground.")
            print("XBOX ROUTINE: Xbox did not become foreground within the timeout.")
            return True

        # Xbox can appear on screen before its library/search UI is ready.
        # Wait for the actual UI before attempting the game routine.
        _wait_for_xbox_ready(30.0)
        time.sleep(1.0)
    else:
        # If Xbox was already open, it may still be finishing startup/loading.
        _wait_for_xbox_ready(15.0)

    if _xbox_find_game_and_open(game):
        say(f"Opening {game} in Xbox.")

        # The game page can appear before its Play button is exposed to UI Automation.
        # Poll for the actual Play/Launch control instead of checking only once.
        play_deadline = time.time() + 30.0
        launched = False
        last_play_report = 0.0
        while time.time() < play_deadline:
            if not _foreground_is_xbox():
                time.sleep(0.5)
                continue

            if _invoke_foreground_uia_target(('play', 'launch', 'start')):
                print(f"XBOX ROUTINE: Play control found and invoked for {game!r}.")
                launched = True
                break

            # Some Xbox builds expose the control with a longer accessible name.
            if _invoke_foreground_uia_target(('play button', 'launch button', 'start button')):
                print(f"XBOX ROUTINE: named Play control found and invoked for {game!r}.")
                launched = True
                break

            now = time.time()
            if now - last_play_report >= 5.0:
                remaining = max(0, int(play_deadline - now))
                print(f"XBOX: game page is open; still waiting for Play to appear (about {remaining}s remaining).")
                last_play_report = now
            time.sleep(0.75)

        if launched:
            say(f"Launching {game}.")
        else:
            print(f"XBOX ROUTINE: opened {game}, but Play was not exposed by UIA within 30 seconds.")
            say(f"I opened {game}, but the Play button did not become available yet.")
        return True

    say(f"I opened Xbox, but I couldn't safely find {game} in the Xbox library.")
    print(f"XBOX ROUTINE: could not locate {game!r}; no unsafe click was attempted.")
    return True



# ============================================================
# v58 AUTONOMOUS RESEARCH + UFO² EXECUTION
# ============================================================

def _v58_normalise_task(value):
    return re.sub(r"\s+", " ", str(value).strip())


def _v58_dangerous_task(task):
    t = task.lower()
    blocked = (
        "format the drive", "format c:", "wipe the drive", "erase everything",
        "delete everything", "destroy all", "shut down", "shutdown",
        "restart the pc", "reboot the pc", "change my password",
        "disable antivirus", "disable windows defender", "disable firewall",
        "buy ", "purchase ", "checkout", "send money", "bank transfer",
        "crypto transfer", "factory reset",
    )
    return any(p in t for p in blocked)


def _v58_is_autonomous_request(command):
    """
    Route genuine multi-step Windows tasks to UFO².

    The old detector depended on the exact phrase " and ". That is too brittle:
    natural commands such as "open Notepad, write X, save it, then verify it"
    contain multiple actions but may not contain " and ". We detect action
    chains using separators and action verbs instead.
    """
    c = _v58_normalise_task(command)
    l = c.lower()

    explicit = (
        "do ", "please do ", "can you do ", "could you do ",
        "go and ", "figure out how to ", "work out how to ",
        "learn how to ", "please learn how to ", "take care of ",
        "handle this ", "handle the ", "find out how to ",
        "research how to ",
    )
    if l.startswith(explicit):
        return True

    starters = (
        "open ", "launch ", "start ", "find ", "download ", "move ",
        "copy ", "rename ", "create ", "save ", "search ", "go to ",
        "write ", "type ", "click ", "fill ", "select ", "enter ",
        "change ", "set ", "navigate ", "close ", "send ", "attach ",
    )

    if not l.startswith(starters):
        return False

    sequence_markers = (
        ", then ", "; then ", " then ",
        ", and ", " and then ", " and ",
        "; ", " after that ", " next ",
        " once that is done ", " followed by ",
    )
    has_sequence = any(marker in l for marker in sequence_markers)

    action_verbs = (
        "open ", "launch ", "start ", "write ", "type ", "click ",
        "save ", "verify ", "check ", "confirm ", "close ", "move ",
        "copy ", "rename ", "create ", "download ", "upload ", "search ",
        "select ", "enter ", "fill ", "attach ", "send ", "navigate ",
    )
    action_count = sum(1 for verb in action_verbs if verb in l)

    return has_sequence and action_count >= 2


def _looks_like_pc_action_request(command):
    """
    Free, local, genuine-last-resort check used only right before defaulting
    to plain conversation — after fixed handlers AND v58's own composite-task
    heuristic have both already found nothing. That heuristic is a plain
    keyword/sequence-marker check and misses single-clause action requests
    like "change my display setting from 720p to 1080p" (no "open/launch"
    prefix, no " and "/" then " marker). Since this only runs on commands
    that would otherwise just get chatted about, it can't break anything
    that already works — it can only rescue a command that was about to be
    silently treated as small talk.
    """
    instructions = """
Decide whether this message is asking for something to be DONE or CHANGED
on a Windows PC (open/close/launch/change/adjust/set/configure something),
as opposed to a question, request for information, or casual conversation.

Return ONLY JSON: {"is_pc_action": true or false}
"""
    try:
        # Deliberately uses the default MODEL (gpt-oss:20b), NOT
        # OLLAMA_FAST_MODEL, even though this is "just" a classification
        # call. This is the LAST thing checked before falling through to
        # ask_jarvis() -- which also runs on gpt-oss:20b -- so for the
        # common case (plain conversation), classifying on the SAME model
        # means it's still warm for the reply that follows. Measured the
        # cost of getting this wrong: this GPU can't hold both qwen3:8b and
        # gpt-oss:20b in VRAM at once, so classifying on the fast model here
        # forced a swap OUT to qwen3:8b and then straight back to
        # gpt-oss:20b for the conversational reply -- ~12s + ~23s of pure
        # reload time on top of actual generation, for every single
        # non-PC-action message (this is why simple chat still felt slow
        # even after the think="low" + streaming fix). think="low" keeps
        # this call itself fast when warm (~2.5-3s measured) with no
        # accuracy loss (6/6 on real test cases, PC-action and not).
        raw = ask_ollama_brain(instructions, f"Message: {command}", json_mode=True, timeout=60, think="low")
        data = json.loads(raw)
        return bool(data.get("is_pc_action", False))
    except Exception as error:
        print("PC ACTION CHECK: local AI unavailable:", error)
        return False


def _v58_decompose_known_navigation(task):
    """
    Ask the free local AI whether this task STARTS by navigating to a
    specific place Jarvis might already know how to reach (an app +
    destination pair — see get_app_skill), with something ADDITIONAL to do
    once there. If Jarvis does already know that (app, destination), the
    navigation gets replayed for free and only the genuinely NEW remaining
    part is handed to the paid pipeline — e.g. "change my display setting
    from 720p to 1080p" reuses the already-known route to Display, and only
    "change resolution to 1080p" is new/unlearned. This is the general
    version of the same idea behind app_skills: reuse what's already known,
    pay/learn only for what's actually new — meant to generalise to any
    task shaped like "get somewhere known, then do something extra".

    Returns (application, destination, remaining_instruction); ("", "", "")
    when this isn't that shape of task, or the AI is unavailable.
    """
    instructions = """
You analyse a PC task to see whether it starts by navigating to a specific
place inside an application, followed by some ADDITIONAL action there.

Return ONLY JSON in this exact shape:
{"application": "...", "destination": "...", "remaining_instruction": "..."}

Rules:
- "application" is the app the task starts in, e.g. "Settings".
- "destination" is the specific page/section/tab inside that app the task
  needs to reach first, e.g. "Display", "Bluetooth", "Sound".
- "remaining_instruction" is what to do ONCE ALREADY THERE — written as a
  self-contained instruction that assumes the destination is already open,
  e.g. "change the resolution to 1080p", "turn on Bluetooth", "increase
  the volume to 80%".
- If the task is not this "navigate somewhere, then do something extra"
  shape — e.g. it's a single simple action, or reaching the destination IS
  the whole task with nothing left to do — return
  {"application": "", "destination": "", "remaining_instruction": ""}.
"""
    try:
        # Deliberately NOT using OLLAMA_FAST_MODEL here: measured it
        # inventing a bogus non-empty remaining_instruction for tasks that
        # should decompose to "nothing left to do" (e.g. "open settings and
        # go to display", "open steam and click library" both got a
        # spurious remaining step) — that would waste money on a redundant
        # follow-up. gpt-oss:20b got all of these right; worth the slower
        # per-call cost for the correctness here.
        raw = ask_ollama_brain(instructions, f"Task: {task}", json_mode=True, timeout=60)
        data = json.loads(raw)
        application = str(data.get("application", "")).strip()
        destination = str(data.get("destination", "")).strip()
        remaining = str(data.get("remaining_instruction", "")).strip()
        return application, destination, remaining
    except Exception as error:
        print("V58 DECOMPOSE: local AI unavailable:", error)
        return "", "", ""


# ============================================================
# LEARN BY DOING -- RECORD REAL CLICKS, NOT NARRATION
#
# Two entry points into the same walkthrough:
#   1. _offer_teaching_before_research -- asked BEFORE any AI research
#      spend happens, for a task Jarvis has never done before. Danny's
#      explicit request: give him the option to just show Jarvis rather
#      than defaulting straight to the paid research pipeline.
#   2. _run_teaching_session called directly with a research_hint --
#      the fallback when a paid research+UFO2 attempt already ran and
#      failed/looped without completing. Rather than just reporting
#      failure, Jarvis shares what it already found and offers to walk
#      through it together instead of a dead end.
#
# Danny's explicit correction after the first version of this (which
# recorded the user's spoken/typed NARRATION of each step): "no i want
# it to record what i do not what i say." This version instead runs a
# real global mouse-click listener (pynput) while the user performs the
# task themselves, resolving each click to a genuine named UI control via
# Windows UI Automation (the exact same ElementFromPoint mechanism proven
# live against this machine's own taskbar controls) plus the foreground
# window's title as the "application". That produces the SAME
# {"application", "control"} step shape UFO2's own click-replay routines
# already use -- so a taught routine is saved with
# source="jarvis-v58-ufo2-click-replay" and is immediately replayable by
# the EXISTING _v58_replay_click_steps, and indexed into app_skills the
# same way, with no new replay code needed at all.
#
# Known scope limit: only CLICKS are captured, not typed text or keyboard
# shortcuts -- a step that requires typing into a field won't be captured
# correctly. And since this is a genuinely global mouse hook, a stray
# click on something unrelated to the task while it's active (e.g.
# switching to check a message) would be recorded as a step too -- stay
# on the task while recording, the same way you would with any macro
# recorder.
# ============================================================

_click_recording_lock = threading.Lock()
_click_recording_steps = None  # list while actively recording, else None
_click_recording_typed_buffer = []  # characters typed since the last click/Enter/stop
_click_mouse_listener = None
_click_keyboard_listener = None
_click_iuia = None


def _get_click_iuia():
    """Lazily create (once) the COM UIA client used to resolve a screen
    point to a real control -- reuses pywinauto's own IUIA wrapper, the
    same COM object pywinauto's own UI Automation calls already rely on."""
    global _click_iuia
    if _click_iuia is None:
        from pywinauto.uia_defines import IUIA
        _click_iuia = IUIA()
    return _click_iuia


def _resolve_control_at_point(x, y):
    """
    Real UI Automation control name at a screen point, or "" if none.

    Modern File Explorer's list view (DUIListView) exposes per-COLUMN
    sub-elements at the exact hit point rather than the row item itself
    -- caught live: clicking anywhere in a "Playnite" folder's row
    resolved to "Date modified"/ClassName "UIProperty" (the column's own
    generic sub-part), not the folder's real name. Its immediate parent,
    however, correctly reported Name="Playnite"/ClassName "UIItem".
    Walking up past any "UIProperty" ancestors (confirmed live, up to 4
    levels) finds the real item; anywhere else (a plain button, a
    taskbar icon, a Settings control) the direct hit is already correct
    and this loop just returns on the very first check, unchanged from
    before.
    """
    try:
        from comtypes.gen.UIAutomationClient import tagPOINT
        iuia = _get_click_iuia().iuia
        element = iuia.ElementFromPoint(tagPOINT(x, y))
        walker = iuia.RawViewWalker
        current = element
        for _ in range(5):
            class_name = str(current.CurrentClassName or "")
            name = str(current.CurrentName or "").strip()
            if class_name != "UIProperty" and name:
                return name
            parent = walker.GetParentElement(current)
            if parent is None:
                break
            current = parent
        return str(element.CurrentName or "").strip()
    except Exception as error:
        print("RECORD CLICK: could not resolve control at point:", error)
        return ""


def _window_title_at_point(x, y):
    """
    Title of whatever top-level window actually owns the given screen
    point -- deliberately NOT "currently focused window". Caught live: a
    real test click on the taskbar recorded "application": "Claude"
    because Claude happened to still hold input focus at that instant,
    which is simply wrong -- the click visibly landed elsewhere. Walking
    up to the true top-level ancestor of whatever's at the point gives
    the actual owning app instead.
    """
    try:
        hwnd = ctypes.windll.user32.WindowFromPoint(wintypes.POINT(x, y))
        if not hwnd:
            return ""
        root_hwnd = ctypes.windll.user32.GetAncestor(hwnd, 2)  # GA_ROOT
        hwnd = root_hwnd or hwnd
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, len(buf))
        return buf.value.strip()
    except Exception as error:
        print("RECORD CLICK: could not resolve window title at point:", error)
        return ""


def _flush_typed_buffer_locked():
    """Turn any buffered typed characters into a "type" step. Caller must
    already hold _click_recording_lock."""
    global _click_recording_typed_buffer
    if _click_recording_typed_buffer:
        text = "".join(_click_recording_typed_buffer)
        _click_recording_steps.append({"action": "type", "text": text})
        print(f"RECORD TYPE: {text!r}")
        _click_recording_typed_buffer = []


_last_recorded_click = None  # (application, control, timestamp) for double-click de-dup


def _on_recorded_click(x, y, button, pressed):
    global _last_recorded_click
    from pynput import mouse as _pynput_mouse
    if not pressed or button != _pynput_mouse.Button.left:
        return
    with _click_recording_lock:
        if _click_recording_steps is None:
            return
        # A click means any in-progress typing sequence is finished (focus
        # is moving), so flush it as its own step before this click's step.
        _flush_typed_buffer_locked()
        control_name = _resolve_control_at_point(x, y)
        if not control_name:
            print(f"RECORD CLICK: click at ({x},{y}) had no resolvable control name -- skipped.")
            return
        application = _window_title_at_point(x, y) or "Unknown"

        # A real double-click (opening a folder/file, launching something)
        # fires TWO separate mouse-down events -- caught live in danny's
        # own recorded "open playnite" data as duplicate consecutive
        # steps (e.g. "Documents" appearing three times in a row). One
        # recorded click's replay Invoke() already performs the full
        # open/launch action, so a second identical step is not just
        # redundant but actively breaks replay: by the time it runs,
        # that item may no longer exist in the now-navigated-into view,
        # aborting the whole replay. Collapse same (application, control)
        # clicks within double-click speed into a single step.
        now = time.time()
        if (
            _last_recorded_click
            and _last_recorded_click[0] == application
            and _last_recorded_click[1] == control_name
            and now - _last_recorded_click[2] < 0.6
        ):
            _last_recorded_click = (application, control_name, now)
            print(f"RECORD CLICK: {application!r} -> {control_name!r} (double-click, not duplicated)")
            return

        _last_recorded_click = (application, control_name, now)
        step = {"action": "click", "application": application, "control": control_name}
        _click_recording_steps.append(step)
        print(f"RECORD CLICK: {application!r} -> {control_name!r}")


def _on_recorded_key(key):
    """
    Captures typed text and Enter -- caught live that this was needed:
    danny's own first real test taught "open playnite" by clicking the
    Windows Search icon then typing the app name and pressing Enter to
    launch it, a completely normal way to open an app that isn't in the
    Start Menu's own list -- but a click-only recorder captured nothing
    of the actual "type the name, press Enter" part, so the saved routine
    was missing the one step that actually launched anything.
    """
    from pynput import keyboard as _pynput_keyboard
    with _click_recording_lock:
        if _click_recording_steps is None:
            return
        if key == _pynput_keyboard.Key.enter:
            _flush_typed_buffer_locked()
            _click_recording_steps.append({"action": "key", "key": "enter"})
            print("RECORD KEY: Enter")
            return
        char = getattr(key, "char", None)
        if char and char.isprintable():
            _click_recording_typed_buffer.append(char)


def _start_click_recording():
    global _click_recording_steps, _click_recording_typed_buffer, _click_mouse_listener, _click_keyboard_listener, _last_recorded_click
    from pynput import mouse as _pynput_mouse
    from pynput import keyboard as _pynput_keyboard
    with _click_recording_lock:
        _click_recording_steps = []
        _click_recording_typed_buffer = []
        _last_recorded_click = None
    if _click_mouse_listener is None:
        _click_mouse_listener = _pynput_mouse.Listener(on_click=_on_recorded_click)
        _click_mouse_listener.start()
    if _click_keyboard_listener is None:
        _click_keyboard_listener = _pynput_keyboard.Listener(on_press=_on_recorded_key)
        _click_keyboard_listener.start()


# Said/typed to end a teaching session -- if the user TYPES "done" (e.g.
# into the HUD's text box) rather than saying it, the keyboard listener
# would otherwise record that literal typing as a bogus final "type" step
# right before recording stops. _stop_click_recording drops a trailing
# buffered word that matches one of these rather than saving it.
_TEACHING_STOP_WORDS = {
    "done", "that's it", "thats it", "finished", "that's all", "thats all",
    "stop", "that's everything", "thats everything",
}


def _stop_click_recording():
    global _click_recording_steps, _click_recording_typed_buffer, _click_mouse_listener, _click_keyboard_listener
    with _click_recording_lock:
        buffered = "".join(_click_recording_typed_buffer).strip().lower().strip(" .!")
        if buffered in _TEACHING_STOP_WORDS:
            _click_recording_typed_buffer = []
        else:
            _flush_typed_buffer_locked()
        steps = list(_click_recording_steps or [])
        _click_recording_steps = None
    if _click_mouse_listener is not None:
        _click_mouse_listener.stop()
        _click_mouse_listener = None
    if _click_keyboard_listener is not None:
        _click_keyboard_listener.stop()
        _click_keyboard_listener = None
    return steps


def _save_recorded_click_routine(task, steps):
    """Save real recorded clicks under the exact same source UFO2's own
    click-replay routines use, so it's immediately replayable and gets
    indexed into app_skills the same way -- no new replay code needed."""
    save_learned_routine(
        task,
        method="Taught directly by sir -- recorded live clicks, replayed via free local UI Automation.",
        steps=steps,
        source="jarvis-v58-ufo2-click-replay",
        state="verified",
    )
    # Only index into app_skills when the routine actually ends on a named
    # click (matches how UFO2-derived routines are indexed elsewhere) --
    # a routine ending in a typed/Enter step (e.g. launching via Windows
    # Search) just skips this indexing, which is safe: it's still saved
    # and replayable as a learned_routine either way.
    last_step = steps[-1]
    if last_step.get("application") and last_step.get("control"):
        existing_skill = get_app_skill(last_step["application"], last_step["control"])
        if not (existing_skill and existing_skill.get("kind") == "bash"):
            save_app_skill(last_step["application"], last_step["control"], steps)
            print(f"APP SKILL: learned {last_step['application']!r} -> {last_step['control']!r} (taught, recorded)")


def _run_teaching_session(task, research_hint=None):
    """
    Real walkthrough: the user performs the task themselves while Jarvis
    watches (records) the actual clicks via a global mouse listener, then
    saves them as a genuinely replayable routine. Only get_confirmation_
    input() (voice/HUD/phone/console) is used to detect "done" -- the
    clicks themselves are captured independently and concurrently by the
    mouse listener while that wait happens.
    """
    if research_hint:
        say(
            "Sir, this isn't working the automatic way. Here's what I found "
            f"researching it: {str(research_hint)[:400]} Let's do it together "
            "instead — go ahead and perform the task now, and say \"done\" "
            "when you've finished. I'll watch what you click and save it so "
            "I never have to ask again."
        )
    else:
        say(
            "Let's do it together, then. Go ahead and perform the task now, "
            "and say \"done\" when you've finished. I'll watch what you "
            "click and save it so I never have to ask again."
        )

    _start_click_recording()
    try:
        while True:
            answer = get_confirmation_input().strip()
            if answer.lower().strip(" .!") in _TEACHING_STOP_WORDS:
                break
    finally:
        steps = _stop_click_recording()

    if not steps:
        say("I didn't catch anything, so I haven't saved anything. We can try again any time.")
        return False

    _save_recorded_click_routine(task, steps)
    say(f"Saved. I now know how to {task}, in {len(steps)} step{'s' if len(steps) != 1 else ''}.")
    return True


def _offer_teaching_before_research(task):
    """
    Before spending any money on research, ask whether sir would rather
    just show Jarvis how to do this himself. Returns True if he taught it
    (task fully handled), False if he'd rather let the AI research it
    (falls through to the existing paid pipeline as before).
    """
    say(
        "I don't know how to do that yet. Would you like to walk me "
        "through it yourself, or should I work it out and do it myself? "
        "That takes a little longer than something I already know."
    )
    answer = get_confirmation_input().lower()
    teach_words = ("teach", "walk", "show you", "i'll show", "ill show", "myself", "together")
    if any(word in answer for word in teach_words):
        return _run_teaching_session(task)
    return False


# ============================================================
# SELF-REPAIR -- A DEEPER DIAGNOSTIC, RUN ON JARVIS'S OWN CODE
#
# The absolute last resort, and the only tier that costs real usage
# time: when an automated attempt has genuinely failed, Jarvis can run a
# real coding agent against its OWN source code -- investigating live on
# this machine the same way a developer would, and permanently fixing
# the underlying capability rather than just completing the one task.
#
# Danny's explicit request: never say "I called Claude" out loud -- the
# character-facing wording is always framed as Jarvis running his own
# deeper diagnostic. Both of us know what it actually is; the character
# doesn't say so.
#
# Safety model:
#   - Only ever offered, never automatic -- explicit confirmation first,
#     since it costs real time and (Claude subscription) usage.
#   - A git commit is taken immediately before, as a real revert point.
#   - The fix is independently compile-checked (never just trusted from
#     the agent's own report) before being accepted; a failed compile
#     -- or a run that timed out mid-edit -- is rolled back via git and
#     reported honestly, never left half-broken.
#   - Runs in a background thread so Jarvis's main loop stays
#     responsive; the user is told up front this will take a while.
# ============================================================

def _find_claude_cli():
    """
    Locate the claude.exe binary on this machine. Confirmed live: this
    install is a Windows-packaged (MSIX) app, which stores its real
    files under AppData\\Local\\Packages\\<PackageFamilyName>\\LocalCache\\...
    rather than the classic AppData\\Roaming path a packaged app's own
    processes see (that path is a virtualized alias only visible from
    inside the same package identity) -- a plain, unpackaged process
    like this one only ever sees the real Packages\\...\\LocalCache
    location. Picks the newest version folder if more than one is
    present, since the app auto-updates.
    """
    import glob
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    patterns = [
        os.path.join(local_appdata, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude", "claude-code", "*", "claude.exe"),
        os.path.join(local_appdata, "Programs", "claude", "claude.exe"),
    ]
    candidates = []
    for pattern in patterns:
        candidates.extend(glob.glob(pattern))
    if not candidates:
        return None
    candidates.sort(key=lambda p: [int(x) if x.isdigit() else x for x in re.split(r"[.\\/]", p)])
    return candidates[-1]


def _git(*args, cwd=None):
    """Run a git command in the Jarvis folder, returning (ok, output)."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except Exception as error:
        return False, str(error)


def _run_self_repair_agent(task, context=None):
    """
    Run a real coding agent against Jarvis's own source code to
    permanently fix the capability that just failed, rather than just
    completing the one task once. See module docstring above for the
    safety model (git checkpoint, independent compile verification,
    automatic rollback on failure).
    """
    claude_exe = _find_claude_cli()
    if not claude_exe:
        say("I couldn't find my own diagnostic tools on this machine, so I can't do that right now.")
        return False

    jarvis_file = os.path.abspath(__file__)
    jarvis_dir = os.path.dirname(jarvis_file)

    say(
        "Let me run a deeper diagnostic on my own systems, sir. This will "
        "take a few minutes -- I'll let you know the moment I'm done."
    )

    # Safety checkpoint: commit whatever's currently on disk BEFORE any
    # self-repair attempt, so a bad change always has a clean revert path.
    _git("add", "-A", cwd=jarvis_dir)
    _git("commit", "-m", f"Checkpoint before self-repair attempt: {task}", cwd=jarvis_dir)
    before_ok, _before_hash = _git("rev-parse", "HEAD", cwd=jarvis_dir)

    briefing = f"""You are fixing a real, currently-running personal assistant program called
Jarvis, written as a single Python file at:
{jarvis_file}

TASK THAT FAILED: {task}

{"CONTEXT / WHAT WAS ALREADY TRIED: " + str(context)[:1500] if context else ""}

Your job: figure out why Jarvis can't currently do this, and fix it by
editing {os.path.basename(jarvis_file)} directly, so it works reliably
the next time this exact task (or the same category of task) comes up --
not just complete it manually once.

Investigate for real: read the relevant existing code first (search for
how similar tasks are already handled, to stay consistent with the
existing style/patterns), and use the terminal to inspect the ACTUAL
live state of this Windows machine (running processes, UI Automation
trees, installed apps, etc.) rather than guessing -- the same way you'd
debug a real bug.

Before you finish:
1. Run `python -m py_compile "{jarvis_file}"` and confirm it succeeds.
2. If at all practical, verify the specific fixed logic works -- e.g. by
   importing just the one function/class in a small separate throwaway
   script, or by tracing the logic carefully -- rather than running the
   whole file.
3. Write a short, plain-English summary of exactly what you changed and
   why, as your final message -- this will be read back to the user
   directly, so make it clear and non-technical where possible.

IMPORTANT: never run `{os.path.basename(jarvis_file)}` itself (no
`python {os.path.basename(jarvis_file)}`, no importing it as a whole
module). It has no `if __name__ == "__main__":` guard, so doing either
starts the ENTIRE live voice assistant -- microphone listener, text-to-
speech, wake-word detection, and a network server on a fixed port --
and a real instance of it is very likely already running as a separate
process right now. A second instance would fight the first one for the
microphone and that network port. This restriction is absolute, even if
it seems like the most direct way to verify your fix.

Do not touch any file outside this project folder. Do not modify git
history or run destructive commands (no deleting user files, no system
setting changes unrelated to this task).
"""

    try:
        with open(jarvis_file, "rb") as handle:
            content_before = handle.read()
    except Exception:
        content_before = None

    def worker():
        summary = "(no summary returned)"
        timed_out = False
        try:
            result = subprocess.run(
                [
                    claude_exe, "-p", briefing,
                    "--add-dir", jarvis_dir,
                    "--dangerously-skip-permissions",
                    "--allow-dangerously-skip-permissions",
                ],
                cwd=jarvis_dir,
                capture_output=True, text=True, timeout=1200,
            )
            summary = (result.stdout or "").strip() or summary
        except subprocess.TimeoutExpired:
            timed_out = True
        except Exception as error:
            say(f"The diagnostic couldn't run: {error}")
            return

        # Verify independently -- never just trust the agent's own
        # report. Two checks, both must pass: it actually changed the
        # file (a report of success with zero changes is exactly what
        # a permission or tooling problem inside the agent looks like),
        # and the changed file still compiles. Checked even after a
        # timeout, since a killed process can still leave a half-edit.
        try:
            with open(jarvis_file, "rb") as handle:
                content_after = handle.read()
        except Exception:
            content_after = None
        actually_changed = (
            content_before is not None
            and content_after is not None
            and content_before != content_after
        )

        try:
            compile_result = subprocess.run(
                [sys.executable, "-m", "py_compile", jarvis_file],
                capture_output=True, text=True, timeout=30,
            )
            compiled_ok = compile_result.returncode == 0
        except Exception:
            compiled_ok = False

        if not actually_changed:
            if before_ok:
                _git("checkout", "--", os.path.basename(jarvis_file), cwd=jarvis_dir)
            say(f"Sir, my own diagnostic didn't actually make any changes -- it reported: {summary[:300]}")
            return

        if not compiled_ok:
            if before_ok:
                _git("checkout", "--", os.path.basename(jarvis_file), cwd=jarvis_dir)
            if timed_out:
                say("Sir, that diagnostic ran too long and left things in a bad state -- I've reverted it, so nothing is broken.")
            else:
                say("Sir, my own diagnostic made a change that didn't actually work -- I've reverted it, so nothing is broken.")
            return

        _git("add", "-A", cwd=jarvis_dir)
        _git("commit", "-m", f"Self-repair: {task}\n\n{summary[:2000]}", cwd=jarvis_dir)
        say(f"Done, sir. {summary[:500]} I've saved the fix -- restart me when convenient to actually use it.")
        log_recent_action(f"Self-repair applied for: {task}")

    threading.Thread(target=worker, daemon=True).start()
    return True


def _offer_self_repair_or_teach(task, research_hint=None):
    """
    After an automated attempt has genuinely failed, offer BOTH real
    options: walk through it together (free, immediate), or a deeper
    self-diagnostic that permanently fixes the underlying capability
    (costs real time/usage, but never has to be solved this way again).
    Defaults to the walkthrough unless self-repair is clearly requested,
    since it's the heavier option.
    """
    if research_hint:
        say(
            "Sir, this isn't working the automatic way. Here's what I found "
            f"researching it: {str(research_hint)[:400]} Would you like to "
            "walk through it together, or should I run a deeper diagnostic "
            "on my own systems to actually fix this properly? The "
            "diagnostic takes a few minutes."
        )
    else:
        say(
            "That didn't work. Would you like to walk through it together, "
            "or should I run a deeper diagnostic on my own systems to fix "
            "this properly? The diagnostic takes a few minutes."
        )

    answer = get_confirmation_input().lower()
    repair_words = ("diagnostic", "diagnose", "fix yourself", "fix it yourself", "figure it out", "your own", "permanently", "properly")
    if any(word in answer for word in repair_words):
        return _run_self_repair_agent(task, context=research_hint)
    return _run_teaching_session(task, research_hint=research_hint)


def _v58_research_task(task):
    """
    Ask the online Jarvis brain to research the task before execution.
    This is deliberately a short research brief, not a second autonomous loop.
    """
    if not online_available():
        return ""

    instructions = """
You are the research/planning layer for Jarvis, a Windows 11 personal assistant.

The next layer will execute the task on the user's actual PC using a dedicated
Windows agent. Research the task only when current web knowledge can improve
execution. Prefer official vendor documentation and current Windows/application
documentation. Do not invent UI labels.

Return a concise execution brief:
1. What the task actually requires.
2. The safest/fastest method.
3. Any application-specific details or current UI changes worth knowing.
4. A short verification checklist.

Do NOT pretend you performed anything on the PC.
Do NOT give destructive instructions.
Keep the brief under 1200 words.
"""

    try:
        response = openai_client.responses.create(
            model=OPENAI_MODEL,
            instructions=openai_safe_text(instructions),
            tools=[{"type": "web_search"}],
            input=openai_safe_text(
                f"WINDOWS CONTEXT:\n{JARVIS_MACHINE_CONTEXT}\n\n"
                f"USER TASK:\n{task}"
            ),
        )
        result = str(response.output_text or "").strip()
        if result:
            print("V58 RESEARCH: online research completed.")
        return result
    except Exception as error:
        print("V58 RESEARCH: unavailable:", error)
        return ""


def _v58_ufo_ready():
    ready = (
        AUTONOMOUS_V58_ENABLED
        and os.path.isfile(JARVIS_UFO_PYTHON)
        and os.path.isdir(JARVIS_UFO_ROOT)
    )
    if not ready:
        print("V58 UFO: expected root =", JARVIS_UFO_ROOT)
        print("V58 UFO: expected Python =", JARVIS_UFO_PYTHON)
        print("V58 UFO: root exists =", os.path.isdir(JARVIS_UFO_ROOT))
        print("V58 UFO: Python exists =", os.path.isfile(JARVIS_UFO_PYTHON))
    return ready


_V58_BASH_LOG_MARKER = "Running Bash Command🔧: "


def _v58_extract_bash_steps(output):
    """
    Pull out, in order, the exact shell commands UFO² ran for this task.
    These are the only part of a UFO² run that can be safely replayed later
    with zero AI cost — they are deterministic, unlike UI clicks.
    """
    steps = []
    for line in output.splitlines():
        if _V58_BASH_LOG_MARKER in line:
            steps.append(line.split(_V58_BASH_LOG_MARKER, 1)[1].strip())
    return steps


def _v58_used_app_agent(output):
    """True if UFO² needed its AppAgent (real UI clicking/typing on a live
    screenshot), not just shell commands. Those runs are not safe to replay
    blindly later, since the target UI can look different next time."""
    return re.search(r"Round \d+, Step \d+, AppAgent:", output) is not None


_GAME_LAUNCH_SIGNALS = ("steam://", "rungameid", "epicgames://", "steamapps\\common", "steamapps/common")


def _looks_like_game_launch(steps):
    """
    Cheap heuristic over a set of shell-command steps: does executing
    these actually start a game? Used to decide whether it's worth
    freeing the local AI's ~12GB from memory first (see
    unload_ollama_models) -- worth doing for a game, not worth the
    reload cost on the next chat turn for an ordinary shortcut like a
    Settings deep link.
    """
    joined = " ".join(str(step) for step in steps).lower()
    return any(signal in joined for signal in _GAME_LAUNCH_SIGNALS)


def _auto_dismiss_launch_dialogs(timeout=20, poll_interval=0.4):
    """
    Launching something (e.g. a game via Steam's steam:// protocol) can
    throw up a one-off Windows confirmation dialog that needs a literal
    "Yes"/"Allow"/"Open" click before the thing actually starts -- a
    protocol-launch confirmation, or a UAC consent prompt. Danny's own
    request, after watching this happen live: he shouldn't have to click
    that by hand. Watches the real desktop briefly for exactly that
    shape of window and clicks the affirmative button automatically.

    Deliberately narrow, to avoid ever auto-approving something it
    shouldn't: only acts on genuine system dialog windows (the standard
    Windows dialog-box window class, or an actual UAC consent prompt --
    never an ordinary application window), and only ever clicks one of
    a small, explicit allowlist of button labels. Runs in a background
    thread so it never blocks whatever triggered the launch.
    """
    from pywinauto import Desktop
    AFFIRMATIVE_LABELS = ("yes", "allow", "open")
    deadline = time.time() + timeout
    clicked_any = False
    while time.time() < deadline:
        try:
            for window in Desktop(backend="uia").windows():
                try:
                    class_name = window.class_name() or ""
                    title = window.window_text() or ""
                except Exception:
                    continue
                is_system_dialog = class_name == "#32770" or "user account control" in title.lower()
                if not is_system_dialog:
                    continue
                try:
                    for button in window.descendants(control_type="Button"):
                        label = (button.window_text() or "").strip().lower()
                        if label in AFFIRMATIVE_LABELS:
                            button.click_input()
                            print(f"AUTO-DISMISS: clicked {label!r} on dialog {title!r}")
                            clicked_any = True
                            time.sleep(0.5)
                            break
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(poll_interval)
    return clicked_any


def _v58_replay_bash_steps(steps):
    """
    Replay a previously-verified, purely shell-command-based v58 solution
    directly — no LLM call, no screenshots, no OpenAI cost. Only ever used
    for routines where the whole task turned out to be exactly these shell
    commands and nothing else (see _v58_used_app_agent).
    """
    powershell_path = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    if _looks_like_game_launch(steps):
        # Free the local AI's ~12GB from memory before the game itself
        # starts loading assets -- same reasoning as the older dedicated
        # Steam/Xbox launch routines, just reached through this path now.
        unload_ollama_models()
    threading.Thread(target=_auto_dismiss_launch_dialogs, daemon=True).start()
    try:
        for command in steps:
            print("V58 REPLAY: running saved shell command:", command)
            subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True,
                text=True,
                executable=powershell_path,
            )
            time.sleep(2)
        return True
    except Exception as error:
        print("V58 REPLAY: failed:", error)
        return False


def _v58_extract_click_steps(output):
    """
    Pull out, in order, the exact named-control clicks UFO² performed for
    this task. Unlike raw pixel coordinates, a control's NAME (e.g.
    "Display") is stable and can be re-found later through Windows UI
    Automation — the same free, local, non-AI mechanism Jarvis already uses
    to click Steam's Library tab and search box (_find_target_via_windows_uia).

    Returns [] (not safe to replay) if any step in the run wasn't a clean
    single click on a clearly-identified named control — typed text, a
    drag, or an unresolved "[No control selected]" step disqualifies the
    WHOLE run, since guessing at those blindly next time would be unsafe.
    """
    steps = []
    current_app = ""
    pending_control = None

    app_re = re.compile(r"on application \[(.*?)\]\.")
    selected_re = re.compile(r"Selected item🕹️: (.*), Label: (.*)")

    for line in output.splitlines():
        app_match = app_re.search(line)
        if app_match:
            current_app = app_match.group(1).strip()
            continue

        selected_match = selected_re.search(line)
        if selected_match:
            control_text = selected_match.group(1).strip()
            if (
                control_text
                and control_text.lower() != "none"
                and "no control selected" not in control_text.lower()
            ):
                pending_control = control_text
            else:
                pending_control = None
            continue

        if "Action applied⚒️:" in line:
            action_text = line.split("Action applied⚒️:", 1)[1].strip()
            if pending_control is not None:
                if action_text.startswith("click"):
                    step = {"application": current_app, "control": pending_control}
                    # Collapse an immediately-repeated identical click (e.g.
                    # a round that re-confirms the same target before
                    # noticing it already worked) down to one — replaying
                    # the same click 20 times would be pointless.
                    if not steps or steps[-1] != step:
                        steps.append(step)
                elif action_text not in ("()", ""):
                    # A non-click action (typing, dragging, etc.) makes this
                    # run too complex to safely replay blindly.
                    return []
            pending_control = None

    return steps


def _clean_taskbar_icon_name(control_name):
    """
    'File Explorer pinned' -> 'File Explorer'; 'Copilot - 1 running
    window pinned' -> 'Copilot'. Taskbar icons carry this kind of
    accessibility-name suffix describing their pinned/running state
    (confirmed live against this machine's own real taskbar), which
    find_and_open_app()'s Start-Menu name matching doesn't expect.
    """
    name = re.sub(r"\s*-\s*\d+\s+running\s+windows?\s+pinned\s*$", "", control_name, flags=re.IGNORECASE)
    name = re.sub(r"\s+pinned\s*$", "", name, flags=re.IGNORECASE)
    return name.strip() or control_name


def _v58_replay_click_steps(click_steps):
    """
    Replay a previously-verified v58/taught solution directly using free
    local UI Automation and direct keyboard input — no LLM, no
    screenshots sent anywhere, no cost. Returns True only if every step
    actually succeeded.

    A step is one of:
      - a named-control click: {"application": ..., "control": ...}
        ("action": "click" is implied when absent — this is the ORIGINAL
        step shape, kept for backward compatibility with every routine
        saved before typed/Enter steps existed)
      - {"action": "type", "text": ...} — types into whatever currently
        has keyboard focus
      - {"action": "key", "key": "enter"} — presses Enter
    The type/key step kinds exist because a real taught routine can
    legitimately need them — caught live: teaching "open playnite" by
    clicking Windows Search then typing the name and pressing Enter
    produced a useless routine (missing the one step that actually
    launched anything) before typed/Enter capture was added.
    """
    if not UI_AUTOMATION_AVAILABLE:
        print("V58 CLICK REPLAY: Windows UI Automation is not available.")
        return False

    opened_apps = set()
    for step in click_steps:
        if not isinstance(step, dict):
            return False
        action = str(step.get("action", "click")).strip().lower()

        if action == "type":
            text = str(step.get("text", ""))
            if text and not _real_type_text(text):
                print(f"V58 CLICK REPLAY: could not type {text!r}.")
                return False
            print(f"V58 CLICK REPLAY: typed {text!r}.")
            time.sleep(0.3)
            continue

        if action == "key":
            key_name = str(step.get("key", "")).strip().lower()
            virtual_key = {"enter": 0x0D, "tab": 0x09, "escape": 0x1B}.get(key_name)
            if virtual_key is None:
                print(f"V58 CLICK REPLAY: unsupported key {key_name!r}; aborting replay.")
                return False
            _real_key(virtual_key)
            print(f"V58 CLICK REPLAY: pressed {key_name}.")
            time.sleep(0.5)
            continue

        control_name = str(step.get("control", "")).strip()
        application = str(step.get("application", "")).strip()
        if not control_name:
            return False

        if application == "Unknown":
            # "Unknown" is the recorder's own honest marker for a click on
            # something with no real top-level window title -- in
            # practice this is almost always the TASKBAR (or desktop),
            # which _find_target_via_windows_uia can never find: it
            # deliberately searches ONLY the actual foreground app window
            # (see its own docstring -- a real safety rule, not a bug),
            # and the taskbar is never that. Caught live: a taught
            # routine's first step, clicking a taskbar-pinned "File
            # Explorer" icon, failed every single replay attempt this
            # way. Recognize it for what it actually is -- opening an
            # application -- and hand it to find_and_open_app(), which
            # already does exactly that, reliably, by name.
            app_to_launch = _clean_taskbar_icon_name(control_name)
            if not find_and_open_app(app_to_launch):
                print(f"V58 CLICK REPLAY: could not open {app_to_launch!r} (from taskbar step {control_name!r}); aborting replay.")
                return False
            print(f"V58 CLICK REPLAY: opened {app_to_launch!r} from a taskbar step.")
            time.sleep(1.5)
            continue

        # Try whatever's already on screen FIRST. If the app is already
        # open and already on the right page (e.g. this routine was
        # taught starting from wherever the user already happened to be),
        # this succeeds immediately with no relaunch, preserving that
        # navigation state — a fresh find_and_open_app() launch always
        # returns the app to its default/home state, which would
        # otherwise silently break any routine that assumed a
        # mid-navigation starting point. Only relaunch if the control
        # genuinely isn't already visible.
        result = _find_target_via_windows_uia(control_name)

        if not (result and result.get("uia_element")) and application and application not in opened_apps:
            opened_apps.add(application)
            if find_and_open_app(application):
                time.sleep(1.5)

        if not (result and result.get("uia_element")):
            # A freshly (cold-)launched app can take longer than 1.5s to
            # finish rendering its UI tree. Poll for a few seconds before
            # giving up, rather than one single early attempt.
            for attempt in range(4):
                result = _find_target_via_windows_uia(control_name)
                if result and result.get("uia_element"):
                    break
                time.sleep(1.5)

        if not (result and result.get("uia_element")):
            # Confirmed live: Windows' modern list views (File Explorer's
            # especially) only create UI Automation elements for
            # currently VISIBLE rows -- a target several screens down a
            # long folder genuinely isn't in the tree at all, no amount
            # of waiting fixes that. Scroll the foreground window
            # (Page Down) between search attempts, up to 10 pages, before
            # giving up -- found a real target 5 pages down a ~100-item
            # folder this way in live testing.
            try:
                from pywinauto import Desktop
                fg_hwnd = int(ctypes.windll.user32.GetForegroundWindow())
                if fg_hwnd:
                    foreground = Desktop(backend="uia").window(handle=fg_hwnd)
                    foreground.set_focus()
                    for _ in range(10):
                        foreground.type_keys("{PGDN}")
                        time.sleep(0.4)
                        result = _find_target_via_windows_uia(control_name)
                        if result and result.get("uia_element"):
                            print(f"V58 CLICK REPLAY: found {control_name!r} after scrolling.")
                            break
            except Exception as error:
                print(f"V58 CLICK REPLAY: scroll-search failed: {error}")

        if not result or not result.get("uia_element"):
            print(f"V58 CLICK REPLAY: could not find {control_name!r} in {application!r}; aborting replay.")
            return False
        try:
            result["uia_element"].invoke()
            print(f"V58 CLICK REPLAY: clicked {control_name!r} in {application!r}.")
        except Exception as error:
            # Not every control supports InvokePattern -- confirmed live:
            # a File Explorer nav-pane TreeItem (a pinned Quick Access
            # folder, e.g. "Documents (pinned)") raises
            # NoPatternInterfaceError on invoke() but activates correctly
            # via SelectionItemPattern's select() instead. Try that
            # before giving up on the whole replay.
            try:
                result["uia_element"].select()
                print(f"V58 CLICK REPLAY: selected {control_name!r} in {application!r} (invoke unsupported).")
            except Exception as select_error:
                print(f"V58 CLICK REPLAY: invoke failed for {control_name!r}: {error}; select also failed: {select_error}")
                return False
        time.sleep(1.0)

    return True


OLLAMA_VISION_MODEL = os.environ.get("JARVIS_OLLAMA_VISION_MODEL", "qwen3-vl:8b-instruct")


def ask_ollama_vision(instructions, image_data_url, timeout=None):
    """
    One-shot local vision question-answering call — for a single yes/no
    judgement on one screenshot only. NOT for deciding a sequence of
    actions: a live test showed the local vision model is not reliable
    enough for that agentic clicking loop (it got stuck repeating the same
    click over and over). Plain "does this screenshot show X" description
    is a task it handles fine, which is all this is used for.
    """
    # Ollama's native /api/chat takes images as a separate "images" field of
    # raw base64 (no "data:image/...;base64," prefix and no OpenAI-style
    # content-parts list) — different from the OpenAI-compatible endpoint.
    raw_base64 = image_data_url
    if "," in raw_base64 and raw_base64.strip().lower().startswith("data:"):
        raw_base64 = raw_base64.split(",", 1)[1]

    payload = {
        "model": OLLAMA_VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": instructions,
                "images": [raw_base64],
            }
        ],
        "stream": False,
        # Short on purpose, unlike the main chat model's keep_alive: this
        # GPU can't hold gpt-oss:20b and the vision model at once, and
        # vision is used rarely (one-off screen checks), so we want it to
        # free VRAM back to gpt-oss:20b quickly instead of squatting on it.
        "keep_alive": "1m",
    }
    response = requests.post(OLLAMA_CHAT_URL, json=payload, timeout=timeout or 60)
    response.raise_for_status()
    data = response.json()
    text = str(data.get("message", {}).get("content", "")).strip()
    if not text:
        raise ValueError("Ollama vision returned an empty response.")
    return text


def _v58_verify_with_local_vision(task):
    """
    Free, local sanity check after a click replay: does the current screen
    actually look like the task was completed? If the local model or the
    screen capture is unavailable, this does not block an otherwise
    successful structural replay — it only adds extra confidence when it can
    run, it is not the sole safety net (control-not-found already aborts
    the replay above).
    """
    if not SCREEN_VISION_AVAILABLE:
        return True
    try:
        image_data_url = capture_screen_for_vision()
        instructions = (
            "You are looking at a screenshot of a Windows desktop. Reply with "
            "ONLY the single word YES or NO: does this screenshot look like the "
            "following task has genuinely been completed?\n\nTask: " + task
        )
        answer = ask_ollama_vision(instructions, image_data_url, timeout=30)
        verdict = answer.strip().lower()
        print(f"V58 CLICK REPLAY: local vision verification -> {answer!r}")
        return verdict.startswith("yes")
    except Exception as error:
        print("V58 CLICK REPLAY: local verification unavailable:", error)
        return True


def _run_agentic_pc_task(task, context=None):
    """
    Let a real reasoning-and-execution agent actually work out and carry
    out a PC task live -- finding the right install path, protocol, or
    command itself (the way a person doing it for you would), rather
    than trying to click through a GUI the way the older UFO² pipeline
    below did. Danny's own framing: he wants Jarvis able to do this
    "like Claude can" -- open apps, launch games, run/test/build
    programs -- without being taught or pre-programmed for each one.

    Returns the same (ok, detail, bash_steps, click_steps) shape as
    _v58_run_ufo, so it plugs into the exact same save_learned_routine /
    zero-cost-replay caching logic already in
    handle_v58_autonomous_commands with no changes needed there: a task
    solved once this way is replayed directly (no agent call at all)
    every time after, via the existing Tier 1 bash-step replay.

    Runs with full, unsupervised system access every time (Danny's
    explicit choice, since there's no reliable way to know in advance
    which commands opening an arbitrary app or game will need). The
    safety net is upstream, not here: _v58_dangerous_task() still runs
    before this is ever called, and the briefing below carries Danny's
    actual ground rules for what not to touch.
    """
    claude_exe = _find_claude_cli()
    if not claude_exe:
        return False, "Couldn't find my own execution tools on this machine.", [], []

    # This is the slower, one-off cold path (a task never solved before),
    # so it's always worth freeing the local AI's ~12GB first, whether or
    # not this specific task turns out to be a game -- unlike the fast,
    # frequently-hit cached replay path, a few seconds' reload cost on
    # the next chat turn is a non-issue here.
    unload_ollama_models()

    jarvis_dir = os.path.dirname(os.path.abspath(__file__))

    briefing = f"""You are controlling a real Windows 11 PC on behalf of its owner, live,
right now -- not writing code for later. The task:

{task}

{"ADDITIONAL CONTEXT: " + str(context)[:1000] if context else ""}

Actually accomplish this task for real using the terminal (PowerShell or
cmd), the same way you would if you were doing it yourself for a user.
Prefer the most direct, repeatable mechanism -- an official URL protocol
or CLI flag (e.g. Steam's steam:// protocol with the game's real app ID
looked up from its own local library manifest files, rather than
clicking through the Steam GUI), a documented command-line switch, or a
short PowerShell command -- over simulating mouse clicks, since an exact
command can be replayed next time at zero cost, while a click sequence
is fragile and expensive to redo. Only resort to UI automation or a
written helper script if there is genuinely no direct command-line way
to do it.

Install nothing and change no system-wide settings unless the task
explicitly requires it. Do not touch personal files unrelated to this
task. If it would be destructive, irreversible, or looks unsafe, stop
and report that instead of doing it.

When finished, end your reply with EXACTLY one fenced block like this,
with nothing after it:

```RESULT_JSON
{{"success": true or false, "commands": ["ONLY the final, necessary command(s) that actually accomplish this every time it's repeated -- NOT any command you ran just to look around, inspect a file, or figure things out along the way. Empty list if it failed or no shell command was needed."], "explanation": "one or two plain-English sentences, no markdown, describing what actually happened -- this gets read aloud to the user"}}
```
"""

    threading.Thread(target=_auto_dismiss_launch_dialogs, kwargs={"timeout": 60}, daemon=True).start()
    try:
        result = subprocess.run(
            [
                claude_exe, "-p", briefing,
                "--add-dir", jarvis_dir,
                "--dangerously-skip-permissions",
                "--allow-dangerously-skip-permissions",
            ],
            capture_output=True, text=True, timeout=300,
        )
        output = (result.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return False, "The task took too long and timed out.", [], []
    except Exception as error:
        return False, f"Couldn't run the execution agent: {error}", [], []

    match = re.search(r"```RESULT_JSON\s*(\{.*?\})\s*```", output, re.DOTALL)
    if not match:
        return False, output[-1500:] or "No result reported.", [], []

    try:
        parsed = json.loads(match.group(1))
    except Exception:
        return False, output[-1500:], [], []

    success = bool(parsed.get("success"))
    commands = [c for c in (parsed.get("commands") or []) if isinstance(c, str) and c.strip()]
    explanation = str(parsed.get("explanation", "")).strip() or output[-500:]

    return success, explanation, commands, []


def _v58_run_ufo(task, research):
    """
    Run UFO² as a separate process so the stable Jarvis process and v53
    command system remain isolated from UFO's dependencies.

    Returns (ok, detail, bash_steps, click_steps). bash_steps is the ordered
    list of shell commands UFO² ran, set only when the whole task was
    solved with shell commands alone. click_steps is the ordered list of
    named-control clicks UFO² performed, set only when the task was solved
    with clean single clicks and nothing else (no typing/dragging, no
    unresolved control). Exactly one of the two is ever non-empty.
    """
    if not _v58_ufo_ready():
        print("V58 UFO: not installed at", JARVIS_UFO_ROOT)
        return False, "UFO² is not installed yet.", [], []

    enriched = task
    if research:
        enriched = (
            f"{task}\n\n"
            "JARVIS RESEARCH BRIEF (use as reference, verify against the live UI):\n"
            f"{research}"
        )

    env = os.environ.copy()
    if OPENAI_API_KEY:
        env["OPENAI_API_KEY"] = OPENAI_API_KEY
    env["JARVIS_UFO_MODEL"] = JARVIS_UFO_MODEL

    # UFO² v2.0.0 prints emoji through colorama. Its Python 3.10 child can
    # otherwise inherit Windows cp1252 and crash while printing the HostAgent
    # response (before it reaches the requested UI action). Force UTF-8.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    task_name = "jarvis_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        print("\nV58 UFO: starting hybrid Windows agent")
        print("V58 UFO: task =", task)

        process = subprocess.Popen(
            [
                JARVIS_UFO_PYTHON,
                "-m", "ufo",
                "--task", task_name,
                "-r", enriched,
            ],
            cwd=JARVIS_UFO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        lines = []
        deadline = time.time() + JARVIS_UFO_TIMEOUT
        if process.stdout is not None:
            while True:
                if time.time() > deadline:
                    process.kill()
                    return False, "The autonomous Windows agent exceeded its time limit and was stopped.", [], []
                line = process.stdout.readline()
                if line:
                    clean = line.rstrip()
                    lines.append(clean)
                    print("V58 UFO:", clean)
                elif process.poll() is not None:
                    break
                else:
                    time.sleep(0.05)

        return_code = process.wait(timeout=10)
        output = "\n".join(lines).strip()

        # Some UFO versions can emit a traceback while still returning 0.
        # Treat executor errors as failures so Jarvis never claims a task was
        # completed when the executor actually failed.
        error_markers = (
            "Traceback (most recent call last)",
            "UnicodeEncodeError",
            "UnicodeDecodeError",
            "UnicodeError",
            "APIConnectionError",
            "AuthenticationError",
            "BadRequestError",
            "RateLimitError",
            "InternalServerError",
            "Exception:",
            "ERROR:",
            # UFO²'s HostAgent can hit an internal error (e.g. a window that
            # exists but whose UI Automation interface briefly isn't ready)
            # and quietly give up — no traceback, exit code 0, no evaluation
            # ever run. Without this marker that silent give-up was reported
            # to the user as a completed task.
            "not available for the visual element",
        )
        if any(marker in output for marker in error_markers):
            return False, output or "UFO reported an execution error.", [], []

        if return_code == 0:
            # A clean exit is not proof the task was actually done — only
            # trust it once UFO²'s own evaluator has actually weighed in.
            # Without this, a session that silently gives up partway through
            # (no crash, no traceback, exit code 0) was being reported to
            # the user as a completed task.
            if "[Task is complete" not in output:
                return False, output or "UFO finished without confirming the task actually completed.", [], []

            # The evaluator can also explicitly say NO (❌) — that's not
            # "gave up without checking", it's a confident negative verdict,
            # and must not be treated as success just because the
            # "[Task is complete" marker is technically present.
            if "[Task is complete💯:] ❌" in output:
                return False, output or "UFO's evaluator confirmed the task was NOT completed.", [], []

            if _v58_used_app_agent(output):
                bash_steps = []
                click_steps = _v58_extract_click_steps(output)
            else:
                bash_steps = _v58_extract_bash_steps(output)
                click_steps = []
            return True, output, bash_steps, click_steps

        return False, output or f"UFO exited with code {return_code}", [], []

    except subprocess.TimeoutExpired:
        return False, "The autonomous Windows agent exceeded its time limit and was stopped.", [], []
    except Exception as error:
        return False, repr(error), [], []


def handle_v58_autonomous_commands(command, force=False):
    """
    v58 autonomous front door. It runs BEFORE v53's natural app handler so
    composite requests are not swallowed as an application name.

    `force=True` skips the composite-task heuristic gate below. It's used
    as a genuine last resort — after every other handler, including the
    LLM-assisted launch-target cleanup, has already failed to make sense of
    a command — so it's worth letting the full research+UFO² pipeline take
    a real shot at it instead of just admitting defeat. The heuristic gate
    is a plain keyword/verb-count check and can miss legitimate multi-step
    requests worded unexpectedly (e.g. "settings and go to display").
    The safety gate below (_v58_dangerous_task) still always applies.
    """
    if not force and not _v58_is_autonomous_request(command):
        return False

    task = _v58_normalise_task(command)
    if not task:
        return True

    # Coarse-grained Live Activity for the automation pipeline -- not
    # instrumented at every one of this function's many return points (that
    # level of granularity isn't worth it here), but real: each label below
    # is only marked done once that actual checkpoint has genuinely passed.
    # Whatever step is left "active" when this function returns gets
    # finalized by the main loop the moment the next command starts.
    start_activity(["Understanding request", "Checking known routines", "Working on it"], title=task)

    if _v58_dangerous_task(task):
        say("I stopped that because it could make a destructive or high-impact change.")
        print("V58 AUTONOMOUS: safety gate blocked:", task)
        return True

    if not _v58_ufo_ready():
        say("My new hybrid Windows agent is not installed yet. I have not attempted the task.")
        print("V58 AUTONOMOUS: UFO² not installed.")
        return True

    advance_activity()  # -> Checking known routines
    routine = get_learned_routine(task)
    verified = bool(routine) and routine.get("state") == "verified"
    source = str(routine.get("source", "")) if routine else ""

    # Tier 1 — fastest, free path: a task that turned out to be nothing but
    # shell commands last time gets replayed directly — no LLM call, no
    # OpenAI cost, no UFO² at all.
    if verified and source == "jarvis-v58-ufo2-replay":
        steps = [s for s in (routine.get("steps") or []) if isinstance(s, str) and s.strip()]
        if steps:
            say("I've done this exact task before — running it directly, no AI needed this time.")
            print("V58 AUTONOMOUS: zero-cost bash replay for:", task)
            if _v58_replay_bash_steps(steps):
                say("Done.")
                return True
            say("That shortcut didn't work this time, so I'll work it out properly.")
            try:
                update_learned_routine_state(task, "failed")
            except Exception as error:
                print("V58 MEMORY: could not update local experience:", error)
            verified = False

    # Tier 2 — also free: a task that needed real clicking last time gets
    # those exact named-control clicks replayed via free local Windows UI
    # Automation, then sanity-checked with the free local vision model.
    # No cloud AI call either way. Falls through to the paid pipeline below
    # if a control can't be found or the local check looks wrong.
    if verified and source == "jarvis-v58-ufo2-click-replay":
        click_steps = routine.get("steps") or []
        if click_steps:
            say("I've done this exact task before — replaying it locally, no cloud AI needed.")
            print("V58 AUTONOMOUS: zero-cost click replay for:", task)
            if _v58_replay_click_steps(click_steps) and _v58_verify_with_local_vision(task):
                say("Done.")
                return True
            say("That didn't check out this time, so I'll work it out properly.")
            try:
                update_learned_routine_state(task, "failed")
            except Exception as error:
                print("V58 MEMORY: could not update local experience:", error)
            verified = False

    # Partial reuse — checked EVERY time (cached full-task match or not):
    # does this task start by navigating somewhere Jarvis already knows how
    # to reach, with something extra to do once there? If so, do the known
    # part for free and hand only the genuinely new remainder to the paid
    # pipeline — e.g. "change my display setting from 720p to 1080p" reuses
    # the already-known route to Display, and only "change resolution to
    # 1080p" is new. This applies even on a repeat of a task already cached
    # under tier 3 below, so the navigation stays free every single time,
    # not just the first.
    execution_task = task
    used_known_navigation = False
    nav_app, nav_dest, remaining = _v58_decompose_known_navigation(task)
    if nav_app and nav_dest and remaining:
        skill = get_app_skill(nav_app, nav_dest)
        if skill and skill.get("steps"):
            kind = skill.get("kind", "click")
            replay_ok = (
                _v58_replay_bash_steps(skill["steps"]) if kind == "bash"
                else _v58_replay_click_steps(skill["steps"])
            )
            if replay_ok:
                say(f"I already know how to get to {nav_dest} — picking up from there.")
                print(f"V58 AUTONOMOUS: reused known {kind} skill for {nav_app!r} -> {nav_dest!r}; learning only: {remaining!r}")
                execution_task = (
                    f"{nav_dest} in {nav_app} is already open (just navigated there for "
                    f"you). Now, starting from exactly this screen: {remaining}"
                )
                used_known_navigation = True
            else:
                print(f"V58 AUTONOMOUS: known skill for {nav_app!r} -> {nav_dest!r} didn't work this time; solving the full task instead.")

    # Tier 3 — slower but still free-ish: research was already done before,
    # so skip that paid step and reuse it — but still run and verify
    # execution live, since the actual result depends on the PC's current
    # state (e.g. whether Steam is already running), not just on what
    # happened last time. Skipped when navigation reuse above already
    # narrowed the task, since the cached research was for the ORIGINAL
    # full task, not this narrower remainder.
    if verified and source == "jarvis-v58-ufo2" and not used_known_navigation:
        say("I've done this one before. Skipping the research and going straight to it.")
        saved_steps = routine.get("steps") or []
        research = saved_steps[0] if saved_steps else ""
        print("V58 AUTONOMOUS: reusing saved research brief for:", task)
    else:
        if not used_known_navigation:
            if _offer_teaching_before_research(execution_task):
                return True
            say("Let me work that out and get it done.")
        research = None
        verified = False

    ok, detail, bash_steps, click_steps_new = _run_agentic_pc_task(execution_task, research)
    if ok:
        # Record the task as an autonomous experience candidate. UFO's own
        # experience-learning layer remains the authoritative executor memory.
        replayable_sources = ("jarvis-v58-ufo2-replay", "jarvis-v58-ufo2-click-replay")
        try:
            if bash_steps and not used_known_navigation:
                save_learned_routine(
                    task,
                    method=(
                        "Completed through Jarvis v58; the whole task turned out to be "
                        "these shell commands, so they are replayed directly next time "
                        "with no AI cost."
                    ),
                    steps=bash_steps,
                    source="jarvis-v58-ufo2-replay",
                    state="verified",
                )
                # Also index this as a reusable (app, destination) skill when
                # it's a recognizable shortcut (e.g. ms-settings:bluetooth),
                # so a differently-worded future request for the same
                # destination reuses it too — the same cross-phrasing reuse
                # click-replay routines already get below.
                skill_app, skill_target = _v58_infer_app_skill_from_bash(bash_steps)
                if skill_app and skill_target:
                    save_app_skill(skill_app, skill_target, bash_steps, kind="bash")
                    print(f"APP SKILL: learned {skill_app!r} -> {skill_target!r} (bash)")
            elif click_steps_new and not used_known_navigation:
                save_learned_routine(
                    task,
                    method=(
                        "Completed through Jarvis v58; the whole task turned out to be "
                        "these named-control clicks, so they are replayed directly next "
                        "time via free local UI Automation, with a free local vision "
                        "check — no cloud AI cost."
                    ),
                    steps=click_steps_new,
                    source="jarvis-v58-ufo2-click-replay",
                    state="verified",
                )
                # Also index this as a reusable (app, destination) skill —
                # not just tied to this exact sentence — so a differently
                # worded request that resolves to the same destination can
                # reuse it immediately next time (see get_app_skill). Never
                # downgrade an existing bash-shortcut skill to a click one —
                # a shell command is strictly cheaper/more reliable than
                # replaying clicks, so keep it even if this particular run
                # happened to need clicking (e.g. the shortcut briefly
                # didn't work, or the app started in a different state).
                last_step = click_steps_new[-1]
                if isinstance(last_step, dict) and last_step.get("application") and last_step.get("control"):
                    existing_skill = get_app_skill(last_step["application"], last_step["control"])
                    if not (existing_skill and existing_skill.get("kind") == "bash"):
                        save_app_skill(last_step["application"], last_step["control"], click_steps_new)
                    print(
                        "APP SKILL: learned",
                        f"{last_step['application']!r} -> {last_step['control']!r}",
                    )
            else:
                # This particular run needed something messier than a clean
                # click sequence (typing, dragging, an app already in a
                # different state), but that doesn't mean a previously-earned
                # free-replay solution stopped working — it just means this
                # run's starting state was different. Don't erase an earned
                # zero-cost routine over that; only save the costlier variant
                # when there isn't already a better one on file for this task.
                existing = get_learned_routine(task)
                already_replayable = (
                    bool(existing)
                    and existing.get("state") == "verified"
                    and existing.get("source") in replayable_sources
                )
                if not already_replayable:
                    save_learned_routine(
                        task,
                        method="Completed through Jarvis v58 online research + Microsoft UFO² hybrid execution.",
                        steps=[research[:1000]] if research else [],
                        source="jarvis-v58-ufo2",
                        state="verified",
                    )
        except Exception as error:
            print("V58 MEMORY: could not save local experience:", error)
        say("Done. The hybrid agent completed the task.")
    else:
        print("V58 AUTONOMOUS: task not confirmed:", detail[-3000:])
        if verified:
            # This run actually failed, so the cached routine is no longer
            # trustworthy as-is — fall back to fresh research next time
            # instead of confidently skipping it again.
            try:
                update_learned_routine_state(task, "failed")
            except Exception as error:
                print("V58 MEMORY: could not update local experience:", error)

        # Rather than just reporting a dead end, offer both real options:
        # a guided walkthrough seeded with whatever research already
        # turned up -- danny's explicit request: "he researches the task
        # and it loops ... he then stops and says sir this is not
        # working, I've googled how to do the task, this is it, if we
        # walk through it together I can still save it as a routine once
        # completed." -- OR a deeper self-diagnostic that permanently
        # fixes the underlying capability, for when a one-off walkthrough
        # isn't worth it and it's worth truly never happening again.
        _offer_self_repair_or_teach(execution_task, research_hint=research)

    return True


# ============================================================
# DIRECT DISPLAY RESOLUTION CHANGER — no UI, no AI, no cost
#
# Windows Settings' own resolution control has a fragile "Keep changes /
# Revert" dialog with a short auto-revert window; a live test showed a
# UI-automation click on "Keep changes" repeatedly missing it, silently
# leaving the resolution unchanged while the AI agent believed it had
# succeeded (correctly caught by the evaluator, so nothing broke — it just
# cost real money and never worked). This bypasses the dialog entirely by
# calling the Windows display API directly: no click, no AI, no cost, and
# it only ever applies a resolution the display adapter itself already
# reports supporting, so it can't produce an unusable/black-screen mode.
# ============================================================

class _POINTL(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_ushort),
        ("dmDriverVersion", ctypes.c_ushort),
        ("dmSize", ctypes.c_ushort),
        ("dmDriverExtra", ctypes.c_ushort),
        ("dmFields", ctypes.c_ulong),
        ("dmPosition", _POINTL),
        ("dmDisplayOrientation", ctypes.c_ulong),
        ("dmDisplayFixedOutput", ctypes.c_ulong),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_ushort),
        ("dmBitsPerPel", ctypes.c_ulong),
        ("dmPelsWidth", ctypes.c_ulong),
        ("dmPelsHeight", ctypes.c_ulong),
        ("dmDisplayFlags", ctypes.c_ulong),
        ("dmDisplayFrequency", ctypes.c_ulong),
        ("dmICMMethod", ctypes.c_ulong),
        ("dmICMIntent", ctypes.c_ulong),
        ("dmMediaType", ctypes.c_ulong),
        ("dmDitherType", ctypes.c_ulong),
        ("dmReserved1", ctypes.c_ulong),
        ("dmReserved2", ctypes.c_ulong),
        ("dmPanningWidth", ctypes.c_ulong),
        ("dmPanningHeight", ctypes.c_ulong),
    ]


_ENUM_CURRENT_SETTINGS = -1
_DM_PELSWIDTH = 0x00080000
_DM_PELSHEIGHT = 0x00100000
_CDS_UPDATEREGISTRY = 0x00000001
_DISP_CHANGE_SUCCESSFUL = 0
_DISP_CHANGE_RESTART = 1
_DISP_CHANGE_ERRORS = {
    -1: "the display driver failed to apply it",
    -2: "that resolution isn't supported",
    -3: "the registry couldn't be updated",
    -4: "an invalid flag was used",
    -5: "an invalid parameter was given",
}

_RESOLUTION_PRESETS = {
    "720p": (1280, 720),
    "hd": (1280, 720),
    "1080p": (1920, 1080),
    "full hd": (1920, 1080),
    "fhd": (1920, 1080),
    "1440p": (2560, 1440),
    "2k": (2560, 1440),
    "qhd": (2560, 1440),
    "4k": (3840, 2160),
    "2160p": (3840, 2160),
    "uhd": (3840, 2160),
}


def _get_current_devmode():
    devmode = _DEVMODEW()
    devmode.dmSize = ctypes.sizeof(_DEVMODEW)
    if not ctypes.windll.user32.EnumDisplaySettingsW(None, _ENUM_CURRENT_SETTINGS, ctypes.byref(devmode)):
        return None
    return devmode


def _list_supported_resolutions():
    """Enumerate every resolution the current display adapter actually
    reports as supported, so a requested resolution can be validated
    before ever being applied."""
    resolutions = set()
    i = 0
    devmode = _DEVMODEW()
    devmode.dmSize = ctypes.sizeof(_DEVMODEW)
    while ctypes.windll.user32.EnumDisplaySettingsW(None, i, ctypes.byref(devmode)):
        resolutions.add((devmode.dmPelsWidth, devmode.dmPelsHeight))
        i += 1
    return resolutions


def set_display_resolution(width, height):
    """
    Change the primary display's resolution directly via the Windows
    display API. Returns (ok, message).
    """
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        return False, "That doesn't look like a valid resolution."

    supported = _list_supported_resolutions()
    if not supported:
        return False, "I couldn't read the display's supported resolutions."
    if (width, height) not in supported:
        return False, f"{width}x{height} isn't a resolution this display reports supporting."

    current = _get_current_devmode()
    if current is None:
        return False, "I couldn't read the current display settings."
    if current.dmPelsWidth == width and current.dmPelsHeight == height:
        return True, f"The display is already set to {width} by {height}."

    devmode = _DEVMODEW()
    devmode.dmSize = ctypes.sizeof(_DEVMODEW)
    devmode.dmPelsWidth = width
    devmode.dmPelsHeight = height
    devmode.dmFields = _DM_PELSWIDTH | _DM_PELSHEIGHT

    result = ctypes.windll.user32.ChangeDisplaySettingsExW(
        None, ctypes.byref(devmode), None, _CDS_UPDATEREGISTRY, None
    )
    if result == _DISP_CHANGE_SUCCESSFUL:
        return True, f"Changed the display resolution to {width} by {height}."
    if result == _DISP_CHANGE_RESTART:
        return True, f"Changed the display resolution to {width} by {height}. A restart may be needed for it to fully take effect."

    return False, f"Couldn't change the resolution: {_DISP_CHANGE_ERRORS.get(result, f'error code {result}')}."


def _parse_resolution_phrase(text):
    """
    Extract a target (width, height) from natural phrasing like "1080p",
    "1920x1080", "1920 by 1080", or "4k". Returns None if nothing
    recognisable is found. For a phrase mentioning more than one
    resolution (e.g. "from 720p to 1080p"), the LAST one mentioned wins —
    that's the actual target, not the starting point.
    """
    t = str(text or "").lower()

    match = re.search(r"(\d{3,5})\s*(?:x|by|\*)\s*(\d{3,5})", t)
    if match:
        return int(match.group(1)), int(match.group(2))

    found = None
    for name, size in _RESOLUTION_PRESETS.items():
        for m in re.finditer(re.escape(name), t):
            if found is None or m.start() > found[0]:
                found = (m.start(), size)
    return found[1] if found else None


def handle_display_resolution_command(command):
    """
    Change the display resolution directly — no UI clicking, no AI, no
    cost. Recognises natural phrasing such as "change my display setting
    from 720p to 1080p", "set resolution to 1920x1080", or "change display
    resolution to 4k".
    """
    c = command.strip().lower()
    if "resolution" not in c and "display setting" not in c:
        return False
    if not any(word in c for word in ("change", "set", "switch", "make")):
        return False

    target = _parse_resolution_phrase(c)
    if not target:
        return False

    width, height = target
    ok, message = set_display_resolution(width, height)
    say(message)
    return True


# ============================================================
# HARDWARE-ABSENCE CHECKS — say so plainly instead of looping forever
#
# Live testing found "turn on bluetooth" sending UFO2's HostAgent into a
# genuine infinite loop (13+ rounds, real cost, zero progress): it kept
# deciding "press Windows+A to open Quick Settings" but had no action type
# for a raw keyboard shortcut. Investigating live turned up the real root
# cause: this PC has no Bluetooth hardware at all (confirmed via
# Get-PnpDevice — zero Bluetooth-class devices, not even a disabled one;
# Quick Settings has no Bluetooth tile to find; Settings' Bluetooth page
# itself says "Couldn't connect... turn on Bluetooth"). No amount of UI
# automation can ever find a toggle that doesn't exist, so UFO2 would loop
# on this specific request forever, every time, burning real money for
# nothing. Checking the hardware directly costs nothing and ends the
# question immediately and honestly.
# ============================================================

def _has_bluetooth_hardware():
    """
    True/False if this could be determined; None if the check itself
    failed (e.g. PowerShell unavailable) — callers should NOT block normal
    handling on None, only on a confirmed False.
    """
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "(Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | Measure-Object).Count",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = result.stdout.strip()
        if not output:
            return None
        return int(output) > 0
    except Exception as error:
        print("BLUETOOTH HARDWARE CHECK: unavailable:", error)
        return None


def handle_bluetooth_hardware_check(command):
    """
    Short-circuits a Bluetooth ACTION request (turn on/off, enable, pair,
    connect) immediately and for free if this PC is confirmed to have no
    Bluetooth hardware — instead of letting it reach UFO2, which has no way
    to know that and would loop trying UI paths that can never work. Plain
    navigation ("open bluetooth settings") is left alone — that page exists
    and is useful to look at even without a radio, and is already a known
    free skill.
    """
    c = command.strip().lower()
    if "bluetooth" not in c:
        return False
    if not any(
        word in c
        for word in ("turn on", "turn off", "enable", "disable", "switch on", "switch off", "connect", "pair", "toggle")
    ):
        return False

    if _has_bluetooth_hardware() is False:
        say("This PC doesn't have Bluetooth hardware, so there's nothing for me to turn on or off.")
        return True

    return False


# ============================================================
# DIRECT SYSTEM VOLUME CONTROL — no UI, no AI, no cost
#
# Same philosophy as the resolution changer: setting an exact volume level
# by clicking a slider is fragile and needless when Windows exposes a
# direct, precise API for it (Core Audio's IAudioEndpointVolume). Verified
# live: SetMasterVolumeLevelScalar sets the EXACT requested level (tested
# 100% -> 50% -> 100%, each read back exactly).
# ============================================================

_VOLUME_COM_TYPE_DEFINITION = r'''
using System.Runtime.InteropServices;

[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAudioEndpointVolume {
    int NotImpl1();
    int NotImpl2();
    int GetChannelCount(out int channelCount);
    int SetMasterVolumeLevel(float level, System.Guid eventContext);
    int SetMasterVolumeLevelScalar(float level, System.Guid eventContext);
    int GetMasterVolumeLevel(out float level);
    int GetMasterVolumeLevelScalar(out float level);
    int SetChannelVolumeLevel(int channel, float level, System.Guid eventContext);
    int SetChannelVolumeLevelScalar(int channel, float level, System.Guid eventContext);
    int GetChannelVolumeLevel(int channel, out float level);
    int GetChannelVolumeLevelScalar(int channel, out float level);
    int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, System.Guid eventContext);
    int GetMute([MarshalAs(UnmanagedType.Bool)] out bool mute);
    int GetVolumeStepInfo(out int step, out int stepCount);
    int VolumeStepUp(System.Guid eventContext);
    int VolumeStepDown(System.Guid eventContext);
    int QueryHardwareSupport(out int hardwareSupportMask);
    int GetVolumeRange(out float volumeMin, out float volumeMax, out float volumeStep);
}

[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDevice {
    int Activate(ref System.Guid id, int clsCtx, System.IntPtr activationParams, out IAudioEndpointVolume aev);
}

[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDeviceEnumerator {
    int NotImpl1();
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint);
}

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
public class MMDeviceEnumeratorComObject { }

public class JarvisAudio {
    public static IAudioEndpointVolume Vol() {
        var enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
        IMMDevice dev;
        enumerator.GetDefaultAudioEndpoint(0, 1, out dev);
        var epGuid = typeof(IAudioEndpointVolume).GUID;
        IAudioEndpointVolume aev;
        dev.Activate(ref epGuid, 23, System.IntPtr.Zero, out aev);
        return aev;
    }
    public static float GetVolume() {
        float v; Vol().GetMasterVolumeLevelScalar(out v); return v;
    }
    public static bool GetMuted() {
        bool m; Vol().GetMute(out m); return m;
    }
    public static void SetVolume(float level) {
        Vol().SetMasterVolumeLevelScalar(level, System.Guid.Empty);
    }
    public static void SetMuted(bool mute) {
        Vol().SetMute(mute, System.Guid.Empty);
    }
}
'''


def _run_volume_powershell(action_lines):
    """Run the shared Core Audio COM type definition plus a few extra
    PowerShell lines that use it. Returns stdout, raises on failure."""
    script = (
        "Add-Type -TypeDefinition @'\n" + _VOLUME_COM_TYPE_DEFINITION + "\n'@\n"
        + action_lines
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "PowerShell audio command failed.")
    return result.stdout


def get_system_volume():
    """Returns (percent 0-100, muted) or None on failure."""
    try:
        output = _run_volume_powershell(
            'Write-Output ("V=" + [JarvisAudio]::GetVolume())\n'
            'Write-Output ("M=" + [JarvisAudio]::GetMuted())\n'
        )
        volume = mute = None
        for line in output.splitlines():
            if line.startswith("V="):
                volume = round(float(line[2:]) * 100)
            elif line.startswith("M="):
                mute = line[2:].strip().lower() == "true"
        if volume is None or mute is None:
            return None
        return volume, mute
    except Exception as error:
        print("VOLUME: could not read current volume:", error)
        return None


def set_system_volume(percent):
    """Set the exact system volume level (0-100). Returns (ok, message)."""
    percent = max(0, min(100, int(percent)))
    try:
        _run_volume_powershell(f"[JarvisAudio]::SetVolume({percent / 100.0})\n")
        return True, f"Volume set to {percent} percent."
    except Exception as error:
        return False, f"Couldn't change the volume: {error}"


def set_system_mute(mute):
    """Mute or unmute the system volume. Returns (ok, message)."""
    try:
        _run_volume_powershell(f"[JarvisAudio]::SetMuted(${'true' if mute else 'false'})\n")
        return True, "Muted." if mute else "Unmuted."
    except Exception as error:
        return False, f"Couldn't {'mute' if mute else 'unmute'}: {error}"


def _parse_volume_phrase(text):
    """Extract a target volume percentage from phrasing like "50%",
    "50 percent", or "to 50". Returns None if no number is found."""
    match = re.search(r"(\d{1,3})\s*(?:%|percent)?", text)
    if not match:
        return None
    return max(0, min(100, int(match.group(1))))


def handle_volume_command(command):
    """
    Change or report the system volume directly — no UI, no AI, no cost.
    Recognises "set/change volume to X" (and %/percent), "mute"/"unmute",
    "max volume"/"full volume", and plain "volume up"/"volume down" (a
    fixed +/-10% step when no exact number is given).
    """
    c = command.strip().lower()
    if "volume" not in c and "mute" not in c and "unmute" not in c:
        return False

    if "unmute" in c:
        ok, message = set_system_mute(False)
        say(message)
        return True
    if "mute" in c:
        ok, message = set_system_mute(True)
        say(message)
        return True

    if "volume" not in c:
        return False
    if not any(word in c for word in ("set", "change", "make", "turn", "increase", "decrease", "raise", "lower", "up", "down", "max", "full", "half")):
        return False

    if any(word in c for word in ("max", "full", "maximum")):
        ok, message = set_system_volume(100)
        say(message)
        return True
    if "half" in c:
        ok, message = set_system_volume(50)
        say(message)
        return True

    target = _parse_volume_phrase(c)
    if target is not None:
        ok, message = set_system_volume(target)
        say(message)
        return True

    current = get_system_volume()
    if current is None:
        return False
    current_percent, _ = current
    step = 10
    if any(word in c for word in ("up", "increase", "raise")):
        ok, message = set_system_volume(current_percent + step)
        say(message)
        return True
    if any(word in c for word in ("down", "decrease", "lower")):
        ok, message = set_system_volume(current_percent - step)
        say(message)
        return True

    return False


def run_local_command_flow(command, user_message):
    """
    Run the existing, proven Jarvis command handlers.
    Returns True when one of them handled the request.
    """
    if handle_display_resolution_command(command):
        return True

    if handle_bluetooth_hardware_check(command):
        return True

    if handle_volume_command(command):
        return True

    if handle_memory_commands(command, user_message):
        return True

    if handle_steam_remember_routine_command(command):
        return True

    if handle_steam_play_routine_command(command):
        return True

    if handle_steam_search_command(command):
        return True

    if handle_steam_open_game_command(command):
        return True

    if handle_close_app_command(command):
        return True
    if handle_fullscreen_command(command):
        return True
    if handle_window_command(command):
        return True
    if handle_screen_target_command(command):
        return True

    if handle_screen_vision_command(command):
        return True
    if handle_vision_action_command(command):
        return True
    if handle_screen_mouse_commands(command):
        return True
    if handle_pc_awareness(command):
        return True
    if handle_file_folder_commands(command):
        return True
    if handle_xbox_game_command(command):
        return True
    if handle_media_commands(command):
        return True
    if handle_learning_executor_commands(command):
        return True
    if handle_learning_plan_commands(command):
        return True
    if handle_learning_try_commands(command):
        return True
    if handle_learning_foundation_commands(command):
        return True
    if handle_program_commands(command):
        return True
    if handle_web_commands(command):
        return True
    if handle_system_commands(command):
        return True
    if handle_time_commands(command):
        return True
    if handle_power_commands(command):
        return True
    if handle_special_apps(command):
        return True
    if handle_natural_app_command(command):
        return True
    if handle_extra_natural_commands(command):
        return True

    return False



# ============================================================
# ROBUST WINDOWS INPUT (SendInput)
# ============================================================
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

class _MOUSEINPUT(ctypes.Structure):
    _fields_=[("dx",ctypes.c_long),("dy",ctypes.c_long),("mouseData",ctypes.c_ulong),
              ("dwFlags",ctypes.c_ulong),("time",ctypes.c_ulong),("dwExtraInfo",ctypes.POINTER(ctypes.c_ulong))]
class _KEYBDINPUT(ctypes.Structure):
    _fields_=[("wVk",ctypes.c_ushort),("wScan",ctypes.c_ushort),
              ("dwFlags",ctypes.c_ulong),("time",ctypes.c_ulong),("dwExtraInfo",ctypes.POINTER(ctypes.c_ulong))]
class _HARDWAREINPUT(ctypes.Structure):
    _fields_=[("uMsg",ctypes.c_ulong),("wParamL",ctypes.c_short),("wParamH",ctypes.c_ushort)]
class _INPUTUNION(ctypes.Union):
    _fields_=[("mi",_MOUSEINPUT),("ki",_KEYBDINPUT),("hi",_HARDWAREINPUT)]
class _INPUT(ctypes.Structure):
    _fields_=[("type",ctypes.c_ulong),("union",_INPUTUNION)]

def _send_input(items):
    arr=(_INPUT*len(items))(*items)
    sent=ctypes.windll.user32.SendInput(len(items), ctypes.byref(arr), ctypes.sizeof(_INPUT))
    if sent != len(items):
        raise ctypes.WinError(ctypes.get_last_error())
    return True

def _real_mouse_click(button="left", clicks=1):
    flags={"left":(MOUSEEVENTF_LEFTDOWN,MOUSEEVENTF_LEFTUP),
           "right":(MOUSEEVENTF_RIGHTDOWN,MOUSEEVENTF_RIGHTUP),
           "middle":(0x0020,0x0040)}
    down,up=flags[button]
    items=[]
    for _ in range(max(1,int(clicks))):
        items += [_INPUT(INPUT_MOUSE,_INPUTUNION(mi=_MOUSEINPUT(0,0,0,down,0,None))),
                  _INPUT(INPUT_MOUSE,_INPUTUNION(mi=_MOUSEINPUT(0,0,0,up,0,None)))]
    return _send_input(items)

def _real_type_text(value):
    items=[]
    for ch in str(value):
        code=ord(ch)
        items += [_INPUT(INPUT_KEYBOARD,_INPUTUNION(ki=_KEYBDINPUT(0,code,KEYEVENTF_UNICODE,0,None))),
                  _INPUT(INPUT_KEYBOARD,_INPUTUNION(ki=_KEYBDINPUT(0,code,KEYEVENTF_UNICODE|KEYEVENTF_KEYUP,0,None)))]
    return _send_input(items) if items else False

def _real_key(vk):
    return _send_input([
        _INPUT(INPUT_KEYBOARD,_INPUTUNION(ki=_KEYBDINPUT(vk,0,0,0,None))),
        _INPUT(INPUT_KEYBOARD,_INPUTUNION(ki=_KEYBDINPUT(vk,0,KEYEVENTF_KEYUP,0,None)))
    ])

# ============================================================
# SCREEN + MOUSE CONTROL
# ============================================================

# This layer uses Windows user32 directly, so it does not require pyautogui.
# Commands are intentionally explicit and run before the normal AI conversation.

def _mouse_move(x, y):
    try:
        ctypes.windll.user32.SetCursorPos(int(x), int(y))
        return True
    except Exception as error:
        print("Mouse move error:", error)
        return False

def _mouse_click(button="left", clicks=1):
    try:
        u = ctypes.windll.user32
        flags = {
            "left": (0x0002, 0x0004),
            "right": (0x0008, 0x0010),
            "middle": (0x0020, 0x0040),
        }
        down, up = flags.get(button, flags["left"])
        for _ in range(max(1, int(clicks))):
            u.mouse_event(down, 0, 0, 0, 0)
            u.mouse_event(up, 0, 0, 0, 0)
            time.sleep(0.08)
        return True
    except Exception as error:
        print("Mouse click error:", error)
        return False

def _scroll(amount):
    try:
        ctypes.windll.user32.mouse_event(0x0800, 0, 0, int(amount), 0)
        return True
    except Exception as error:
        print("Mouse scroll error:", error)
        return False

def _key(vk):
    try:
        u = ctypes.windll.user32
        u.keybd_event(int(vk), 0, 0, 0)
        u.keybd_event(int(vk), 0, 2, 0)
        return True
    except Exception as error:
        print("Keyboard action error:", error)
        return False

def _type_text(text):
    try:
        ctypes.windll.user32.SetForegroundWindow(ctypes.windll.user32.GetForegroundWindow())
        for ch in str(text):
            code = ctypes.windll.user32.VkKeyScanW(ord(ch))
            if code == -1:
                continue
            vk = code & 0xff
            shift = (code >> 8) & 1
            if shift:
                ctypes.windll.user32.keybd_event(0x10, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
            if shift:
                ctypes.windll.user32.keybd_event(0x10, 0, 2, 0)
        return True
    except Exception as error:
        print("Typing error:", error)
        return False

def _screen_size():
    try:
        return ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
    except Exception:
        return 0, 0

def _virtual_screen_bounds():
    """Return the real Windows virtual-desktop bounds, including multi-monitor offsets."""
    try:
        u = ctypes.windll.user32
        left = u.GetSystemMetrics(76)
        top = u.GetSystemMetrics(77)
        width = u.GetSystemMetrics(78)
        height = u.GetSystemMetrics(79)
        return left, top, width, height
    except Exception:
        w, h = _screen_size()
        return 0, 0, w, h

def capture_screen_for_vision(return_info=False):
    """Capture the Windows desktop and return a JPEG data URL plus size information."""
    if not SCREEN_VISION_AVAILABLE:
        raise RuntimeError("Pillow is not installed, so screen capture is unavailable.")

    try:
        image = ImageGrab.grab(all_screens=True)
    except Exception:
        image = ImageGrab.grab()

    original_width, original_height = image.width, image.height
    virtual_left, virtual_top, virtual_width, virtual_height = _virtual_screen_bounds()
    max_width = 1600
    scale = 1.0
    if image.width > max_width:
        scale = max_width / image.width
        new_height = int(image.height * scale)
        image = image.resize((max_width, new_height), Image.LANCZOS)

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=78, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    data_url = "data:image/jpeg;base64," + encoded

    if return_info:
        return data_url, image.width, image.height, original_width, original_height, scale
    return data_url


def ask_vision_about_screen(question):
    """Send one current desktop screenshot to OpenAI and ask what is visible."""
    if not online_available():
        return None

    try:
        image_data_url = capture_screen_for_vision()
        instructions = """
You are Jarvis's screen-vision module. You are looking at a live screenshot of
 the user's Windows desktop. Describe only what is actually visible.

Be concise and useful. Identify the active application/window when possible,
important visible buttons, menus, text, dialogs, errors, and other UI elements.
Do not invent anything that cannot be seen.

This is an observation-only step. Do not claim that you clicked, typed, opened,
or changed anything. If text is too small or unclear, say so.
"""
        response = openai_client.responses.create(
            model=VISION_MODEL,
            instructions=openai_safe_text(instructions),
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": openai_safe_text(question)},
                        {"type": "input_image", "image_url": image_data_url, "detail": "high"},
                    ],
                }
            ],
        )
        answer = str(response.output_text or "").strip()
        return answer or None
    except Exception as error:
        print("Screen vision error:", error)
        return None



def handle_screen_vision_command(command):
    """Handle normal questions about what is currently visible on the screen."""
    try:
        c = command.strip().lower()

        vision_phrases = (
            "what do you see on my screen",
            "what can you see on my screen",
            "what is on my screen",
            "what's on my screen",
            "what do you see on screen",
            "what can you see on screen",
            "look at my screen",
            "look at the screen",
            "describe my screen",
            "describe the screen",
            "read my screen",
            "read the screen",
        )

        if c not in vision_phrases:
            return False

        if not online_available():
            say("I need my online vision brain connected to see the screen.")
            return True

        say("Let me take a look.")
        answer = ask_vision_about_screen(command)

        if answer:
            say(answer)
        else:
            say("I couldn't inspect the screen just now.")
        return True

    except Exception:
        error_text = traceback.format_exc()
        print("\nSCREEN VISION HANDLER ERROR:\n" + error_text)
        try:
            Path("jarvis_crash.log").write_text(error_text, encoding="utf-8")
        except Exception:
            pass
        say("The screen vision command hit an error, but Jarvis is still running.")
        return True

def capture_screen_for_target_locator():
    """Capture the desktop and add a coordinate ruler for more reliable visual targeting."""
    if not SCREEN_VISION_AVAILABLE:
        raise RuntimeError("Pillow is not installed, so screen capture is unavailable.")
    try:
        image = ImageGrab.grab(all_screens=True)
    except Exception:
        image = ImageGrab.grab()
    original_width, original_height = image.width, image.height
    try:
        from PIL import ImageDraw
        annotated = image.convert("RGB").copy()
        draw = ImageDraw.Draw(annotated)
        step = 100
        for x in range(0, original_width, step):
            draw.line((x, 0, x, original_height), fill=(180, 180, 180), width=1)
            draw.text((x + 3, 3), str(x), fill=(255, 255, 0))
        for y in range(0, original_height, step):
            draw.line((0, y, original_width, y), fill=(180, 180, 180), width=1)
            draw.text((3, y + 3), str(y), fill=(255, 255, 0))
        image = annotated
    except Exception as error:
        print("Target grid overlay error:", error)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return "data:image/jpeg;base64," + encoded, original_width, original_height


def _encode_pil_image(image, quality=92):
    """Encode a PIL image as a JPEG data URL for the vision API."""
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return "data:image/jpeg;base64," + encoded


def _capture_raw_screen():
    """Capture the desktop without adding overlays, preserving the exact screenshot coordinates."""
    if not SCREEN_VISION_AVAILABLE:
        raise RuntimeError("Pillow is not installed, so screen capture is unavailable.")
    try:
        return ImageGrab.grab(all_screens=True)
    except Exception:
        return ImageGrab.grab()


def _vision_json(response):
    """Parse JSON returned by the vision model, tolerating a markdown code fence."""
    raw = str(response.output_text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE).strip()
    return json.loads(raw)


def _vision_verify_crop(target, crop, crop_left, crop_top, original_width, original_height):
    """Use a close-up crop to verify a candidate and return real desktop coordinates."""
    image_data_url = _encode_pil_image(crop, quality=94)
    cw, ch = crop.width, crop.height
    instructions = f"""
You are Jarvis's final visual UI verifier.

The user wants to click this exact visible UI target:
{target}

You are looking at a HIGH-DETAIL CROPPED REGION of the live Windows desktop.
The crop's top-left corner corresponds to full-screen pixel coordinate ({crop_left}, {crop_top}).
The crop is {cw} pixels wide by {ch} pixels high.

Find the actual visible clickable target itself. Do not choose a nearby icon or merely related item.
For a tiny taskbar control, identify the exact icon/button, not the surrounding taskbar.

Return ONLY valid JSON in exactly this form:
{{"found": true, "left": 10, "top": 20, "right": 30, "bottom": 40, "confidence": 0.98, "description": "Start button"}}

The coordinates MUST be pixels INSIDE THIS CROPPED IMAGE.
Return a TIGHT bounding box around the clickable target.
If the requested target is not clearly present in this crop, return:
{{"found": false, "left": null, "top": null, "right": null, "bottom": null, "confidence": 0, "description": "target not present in this crop"}}
"""
    response = openai_client.responses.create(
        model=VISION_MODEL,
        instructions=openai_safe_text(instructions),
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": openai_safe_text(f"Verify whether this crop contains the exact clickable target: {target}")},
            {"type": "input_image", "image_url": image_data_url, "detail": "high"},
        ]}],
    )
    data = _vision_json(response)
    if not data.get("found"):
        return data

    left = float(data.get("left"))
    top = float(data.get("top"))
    right = float(data.get("right"))
    bottom = float(data.get("bottom"))
    confidence = float(data.get("confidence", 0) or 0)
    if not (0 <= left < cw and 0 <= right <= cw and 0 <= top < ch and 0 <= bottom <= ch):
        return {"found": False, "confidence": 0, "description": "Verifier returned coordinates outside the crop."}
    if right <= left or bottom <= top:
        return {"found": False, "confidence": 0, "description": "Verifier returned an invalid bounding box."}

    left, top, right, bottom = round(left), round(top), round(right), round(bottom)
    x = crop_left + round((left + right) / 2)
    y = crop_top + round((top + bottom) / 2)
    if not (0 <= x < original_width and 0 <= y < original_height):
        return {"found": False, "confidence": 0, "description": "Verified coordinates were outside the desktop."}

    return {
        "found": True,
        "x": x,
        "y": y,
        "confidence": confidence,
        "description": str(data.get("description", target)),
        "box_left": crop_left + left,
        "box_top": crop_top + top,
        "box_right": crop_left + right,
        "box_bottom": crop_top + bottom,
        "screen_width": original_width,
        "screen_height": original_height,
    }



# ============================================================
# WINDOWS UI AUTOMATION (UIA) TARGETING
# ============================================================

try:
    from pywinauto import Desktop
    UI_AUTOMATION_AVAILABLE = True
except Exception:
    Desktop = None
    UI_AUTOMATION_AVAILABLE = False


def _normalise_ui_name(text):
    text = str(text or '').lower().strip()
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _ui_target_aliases(target):
    """Return useful Windows UI Automation names for a natural-language target."""
    t = _normalise_ui_name(target)
    aliases = [t]
    mapping = {
        'start button': ['start'],
        'start menu': ['start'],
        'windows start': ['start'],
        'setting icon': ['settings'],
        'settings icon': ['settings'],
        'settings': ['settings'],
        'search button': ['search'],
        'search box': ['search'],
        'file explorer': ['file explorer'],
        'task view': ['task view'],
    }
    aliases.extend(mapping.get(t, []))
    # Strip common action words that can accidentally become part of the UI name.
    for prefix in ('click ', 'left click ', 'right click ', 'double click '):
        if t.startswith(prefix):
            aliases.append(t[len(prefix):].strip())
    return list(dict.fromkeys(a for a in aliases if a))


def _ui_element_rect(element):
    """Read a pywinauto UIA element's bounding rectangle defensively."""
    try:
        rect = element.rectangle()
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        return None


def _ui_element_visible(element):
    try:
        if hasattr(element, 'is_visible') and not element.is_visible():
            return False
    except Exception:
        pass
    try:
        rect = _ui_element_rect(element)
        if not rect:
            return False
        l, t, r, b = rect
        return r > l and b > t
    except Exception:
        return False


def _find_target_via_windows_uia(target):
    """Use Windows UI Automation only inside the current foreground window.

    v22 proved that direct UIA invocation can reliably activate controls such
    as Steam's Install button.  The important safety rule for v23 is that UIA
    must NEVER search unrelated background windows.  If the target is not
    exposed by the foreground application, this function returns None and the
    normal visual fallback can inspect what is actually visible on screen.
    """
    if not UI_AUTOMATION_AVAILABLE:
        return None

    aliases = _ui_target_aliases(target)
    alias_set = set(aliases)
    actionable_types = {
        'Button', 'Hyperlink', 'MenuItem', 'TabItem', 'ListItem',
        'CheckBox', 'RadioButton', 'ComboBox', 'Edit', 'SplitButton',
        'TreeItem'
    }

    def control_type_of(control):
        try:
            return str(control.element_info.control_type or '')
        except Exception:
            return ''

    try:
        desktop = Desktop(backend='uia')

        # HARD RULE: inspect ONLY the actual Windows foreground window.
        # Never search all visible windows, because a matching control in
        # File Explorer, another browser tab/window, etc. could otherwise be
        # selected when the user meant the active application.
        fg_hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        if not fg_hwnd:
            return None

        foreground = desktop.window(handle=fg_hwnd)
        try:
            controls = foreground.descendants()
        except Exception:
            controls = []

        candidates = [foreground] + list(controls)
        best = None

        for control in candidates:
            try:
                name = _normalise_ui_name(control.window_text())
            except Exception:
                continue
            if not name or not _ui_element_visible(control):
                continue

            rect = _ui_element_rect(control)
            if not rect:
                continue
            l, t, r, b = rect
            width = max(0, r - l)
            height = max(0, b - t)
            area = width * height
            if width <= 0 or height <= 0:
                continue

            ctype = control_type_of(control)
            actionable = ctype in actionable_types

            # Exact/near-exact name matching only. Do not accept a control
            # merely because a long parent/window name contains the target.
            name_score = 0
            if name in alias_set:
                name_score = 100
            else:
                for alias in aliases:
                    if alias and (alias in name or name in alias):
                        name_score = max(name_score, 75)
            if name_score == 0:
                continue

            # Reject large non-actionable containers. Small non-standard UIA
            # elements are still allowed because v19 showed that some Windows
            # controls can be exposed as Image/Pane rather than Button.
            sw, sh = _screen_size()
            screen_area = max(1, sw * sh)
            if not actionable and area > 0.05 * screen_area:
                continue

            score = float(name_score)
            score += 30 if actionable else 0
            if ctype == 'Button':
                score += 12
            elif ctype in {'Hyperlink', 'MenuItem', 'TabItem', 'ListItem'}:
                score += 8

            # Prefer small, specific controls.
            score += max(0, 28 - min(28, area / 25000.0))

            try:
                if control.is_enabled():
                    score += 8
                else:
                    score -= 25
            except Exception:
                pass

            candidate = {
                'element': control,
                'name': name,
                'rect': rect,
                'score': score,
                'control_type': ctype,
            }
            if best is None or score > best['score']:
                best = candidate

        if not best:
            print(f"WINDOWS UIA: target {target!r} was not found in the foreground window; using visual fallback.")
            return None

        l, t, r, b = best['rect']
        x = (l + r) // 2
        y = (t + b) // 2
        sw, sh = _screen_size()
        vleft, vtop, vwidth, vheight = _virtual_screen_bounds()
        if not (vleft <= x < vleft + vwidth and vtop <= y < vtop + vheight):
            print(f"WINDOWS UIA: foreground target {target!r} produced unsafe coordinates; using visual fallback.")
            return None

        print(
            f"WINDOWS UIA TARGET: found {target!r} in FOREGROUND window as "
            f"{best['name']!r} type={best['control_type'] or 'unknown'} "
            f"box=({l},{t},{r},{b}), click={x},{y}"
        )
        return {
            'found': True,
            'x': x,
            'y': y,
            'confidence': 0.99,
            'description': best['name'] or target,
            'method': 'windows_uia',
            'uia_element': best['element'],
            'box_left': l,
            'box_top': t,
            'box_right': r,
            'box_bottom': b,
            'screen_width': sw,
            'screen_height': sh,
            'virtual_left': vleft,
            'virtual_top': vtop,
            'virtual_width': vwidth,
            'virtual_height': vheight,
            'control_type': best['control_type'],
        }
    except Exception as error:
        print(f"WINDOWS UIA ERROR: {error}")
        return None


def _find_target_via_local_ocr(target, screen):
    """Find visible text locally without asking the vision model to guess pixels.

    This is deliberately limited to text-like targets. It uses Tesseract's real
    bounding boxes from the captured screenshot, so the final click coordinates
    come from local image processing rather than model-generated coordinates.
    """
    try:
        import pytesseract
        from pytesseract import Output
        import re as _re

        target_clean = target.strip()
        # Remove conversational words that are not part of the visible label.
        wanted = _re.sub(r'\b(button|tab|icon|control|menu item|menu)\b', ' ', target_clean, flags=_re.I)
        wanted = _re.sub(r'\s+', ' ', wanted).strip().lower()
        if not wanted or len(wanted) < 2:
            return None

        rgb = screen.convert('RGB')
        data = pytesseract.image_to_data(rgb, output_type=Output.DICT, config='--psm 11')
        words = []
        for i, raw in enumerate(data.get('text', [])):
            text = str(raw).strip()
            if not text:
                continue
            try:
                conf = float(data['conf'][i])
                left = int(data['left'][i]); top = int(data['top'][i])
                width = int(data['width'][i]); height = int(data['height'][i])
            except Exception:
                continue
            if conf < 35 or width <= 0 or height <= 0:
                continue
            words.append((text, conf, left, top, width, height))

        if not words:
            return None

        wanted_tokens = [t for t in _re.findall(r'[a-z0-9]+', wanted) if len(t) >= 2]
        if not wanted_tokens:
            return None

        # First try a single OCR word containing the requested label.
        candidates = []
        for text, conf, left, top, width, height in words:
            norm = _re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()
            if wanted in norm or norm in wanted:
                score = conf + 30
                candidates.append((score, text, conf, left, top, width, height))

        # Then try adjacent words on the same line, e.g. "Install" or
        # "View" plus a nearby qualifier.
        if not candidates:
            for idx, (text, conf, left, top, width, height) in enumerate(words):
                norm = _re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()
                if any(tok == norm or tok in norm for tok in wanted_tokens):
                    candidates.append((conf, text, conf, left, top, width, height))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)
        score, text, conf, left, top, width, height = candidates[0]
        x = left + width // 2
        y = top + height // 2
        print(f"LOCAL OCR TARGET: found {target_clean!r} as visible text {text!r} at x={x}, y={y}, confidence={conf/100:.2f}")
        return {
            'found': True,
            'x': x,
            'y': y,
            'confidence': max(0.85, min(0.98, conf / 100.0)),
            'description': target_clean,
            'method': 'local_ocr',
        }
    except Exception as error:
        print(f"LOCAL OCR: unavailable/failed: {error}")
        return None

def find_screen_target(target):
    """Consensus-based vision targeting.

    The old v15 behaviour accepted the single highest-confidence verification,
    even when the other visual checks disagreed wildly.  That is unsafe for
    small UI controls.  v18 verifies multiple candidate regions and only
    returns a click point when at least two independent visual checks agree
    closely enough on the same target.
    """
    if not online_available():
        return None
    try:
        # First ask Windows itself for the control location. This avoids
        # asking vision to guess pixel coordinates for standard UI controls.
        uia_result = _find_target_via_windows_uia(target)
        if uia_result:
            return uia_result

        screen = _capture_raw_screen()
        original_width, original_height = screen.width, screen.height
        target_clean = target.strip()

        # Fast local text path: if the requested target is visibly labelled,
        # use OCR to obtain the real text bounding box from the screenshot.
        # This avoids model-generated pixel coordinates for buttons/tabs such
        # as Install, Play, View, Download, Accept, etc.
        ocr_result = _find_target_via_local_ocr(target_clean, screen)
        if ocr_result:
            return ocr_result
        lower_target = target_clean.lower()

        # Small taskbar controls get a native-resolution taskbar region first.
        direct_taskbar = any(word in lower_target for word in (
            "start button", "start menu", "windows start", "search button", "taskbar"
        ))

        candidate_regions = []
        if direct_taskbar:
            taskbar_height = min(220, max(140, original_height // 3))
            candidate_regions.append((0, original_height - taskbar_height, original_width, original_height))

        # Stage 1: coarse candidate discovery.  This is only used to decide
        # which native-resolution regions should be independently verified.
        coarse = screen.convert("RGB").copy()
        try:
            from PIL import ImageDraw
            draw = ImageDraw.Draw(coarse)
            step = 100
            for x in range(0, original_width, step):
                draw.line((x, 0, x, original_height), fill=(180, 180, 180), width=1)
                draw.text((x + 3, 3), str(x), fill=(255, 255, 0))
            for y in range(0, original_height, step):
                draw.line((0, y, original_width, y), fill=(180, 180, 180), width=1)
                draw.text((3, y + 3), str(y), fill=(255, 255, 0))
        except Exception as error:
            print("Target grid overlay error:", error)

        coarse_url = _encode_pil_image(coarse, quality=88)
        instructions = f"""
You are Jarvis's coarse visual UI locator.

Locate the exact visible clickable target requested by the user:
{target_clean}

The screenshot is exactly {original_width}x{original_height} pixels. The top-left is (0,0).
Return ONLY valid JSON in exactly this form:
{{"candidates":[{{"left":0,"top":0,"right":100,"bottom":100,"confidence":0.9,"description":"..."}}]}}

Rules:
- Return up to 6 plausible candidate boxes, ranked BEST FIRST.
- Prefer the actual requested control, not a related control.
- For tiny controls, return a tight box around the specific icon/button.
- Do not invent a target that is not visible.
- Coordinates are real screenshot pixels.
- If it cannot be found, return {{"candidates":[]}}.
"""
        response = openai_client.responses.create(
            model=VISION_MODEL,
            instructions=openai_safe_text(instructions),
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": openai_safe_text(f"Find candidate locations for this exact clickable target: {target_clean}")},
                {"type": "input_image", "image_url": coarse_url, "detail": "high"},
            ]}],
        )
        data = _vision_json(response)
        candidates = data.get("candidates", []) if isinstance(data, dict) else []

        valid = []
        for item in candidates:
            try:
                l, t, r, b = (float(item["left"]), float(item["top"]),
                              float(item["right"]), float(item["bottom"]))
                conf = float(item.get("confidence", 0) or 0)
                if 0 <= l < r <= original_width and 0 <= t < b <= original_height:
                    valid.append((l, t, r, b, conf, str(item.get("description", target_clean))))
            except Exception:
                continue

        # Add native-resolution regions around each coarse candidate.
        for l, t, r, b, conf, desc in valid:
            pad_x = max(50, int((r - l) * 0.65))
            pad_y = max(50, int((b - t) * 0.65))
            region = (
                max(0, int(l) - pad_x),
                max(0, int(t) - pad_y),
                min(original_width, int(r) + pad_x),
                min(original_height, int(b) + pad_y),
            )
            if region not in candidate_regions:
                candidate_regions.append(region)
            if len(candidate_regions) >= 6:
                break

        if not candidate_regions:
            return {"found": False, "description": "Vision could not produce any candidate regions."}

        print(f"VISION CONSENSUS: verifying {len(candidate_regions)} independent region(s) for {target_clean!r}")

        # IMPORTANT: verify every candidate instead of stopping at the first
        # high-confidence answer.  We need agreement between independent views.
        verified_results = []
        for index, (cl, ct, cr, cb) in enumerate(candidate_regions, start=1):
            crop = screen.crop((cl, ct, cr, cb))
            try:
                verified = _vision_verify_crop(target_clean, crop, cl, ct, original_width, original_height)
            except Exception as error:
                print(f"VISION CONSENSUS {index} ERROR: {error}")
                continue

            if verified.get("found"):
                vx = float(verified.get("x"))
                vy = float(verified.get("y"))
                score = float(verified.get("confidence", 0) or 0)
                verified["x"] = int(round(vx))
                verified["y"] = int(round(vy))
                verified_results.append(verified)
                print(
                    f"VISION CONSENSUS {index}: found {verified.get('description', target_clean)!r} "
                    f"at x={verified['x']}, y={verified['y']}, confidence={score:.2f}"
                )

        if not verified_results:
            return {"found": False, "description": "No candidate was visually verified."}

        # Cluster independent answers.  A real target should produce nearby
        # coordinates; scattered answers are treated as uncertainty.
        CLUSTER_RADIUS = 45.0
        clusters = []
        for result in verified_results:
            x = float(result["x"])
            y = float(result["y"])
            placed = False
            for cluster in clusters:
                cx = cluster["sum_x"] / cluster["count"]
                cy = cluster["sum_y"] / cluster["count"]
                if ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 <= CLUSTER_RADIUS:
                    cluster["items"].append(result)
                    cluster["sum_x"] += x
                    cluster["sum_y"] += y
                    cluster["count"] += 1
                    placed = True
                    break
            if not placed:
                clusters.append({"items": [result], "sum_x": x, "sum_y": y, "count": 1})

        clusters.sort(key=lambda c: (
            c["count"],
            sum(float(i.get("confidence", 0) or 0) for i in c["items"])
        ), reverse=True)
        best_cluster = clusters[0]

        print(
            "VISION CONSENSUS: cluster sizes = " +
            ", ".join(str(c["count"]) for c in clusters)
        )

        # Require at least two independent visual confirmations.  This is the
        # key fix for v15's false precision.
        if best_cluster["count"] < 2:
            print(
                f"VISION CONSENSUS BLOCKED: no two visual checks agreed within "
                f"{int(CLUSTER_RADIUS)} pixels. No mouse action will be taken."
            )
            return {
                "found": False,
                "description": f"I could not get enough visual agreement for {target_clean}.",
                "reason": "insufficient_consensus",
            }

        items = best_cluster["items"]
        total_weight = sum(max(0.01, float(i.get("confidence", 0) or 0)) for i in items)
        final_x = int(round(sum(float(i["x"]) * max(0.01, float(i.get("confidence", 0) or 0)) for i in items) / total_weight))
        final_y = int(round(sum(float(i["y"]) * max(0.01, float(i.get("confidence", 0) or 0)) for i in items) / total_weight))
        final_conf = sum(float(i.get("confidence", 0) or 0) for i in items) / len(items)

        # The final point must also be inside the physical Windows virtual desktop.
        vleft, vtop, vwidth, vheight = _virtual_screen_bounds()
        if not (vleft <= final_x < vleft + vwidth and vtop <= final_y < vtop + vheight):
            return {"found": False, "description": "Consensus coordinates were outside the Windows desktop."}

        print(
            f"VISION CONSENSUS ACCEPTED: {best_cluster['count']} independent checks agree; "
            f"final x={final_x}, y={final_y}, confidence={final_conf:.2f}"
        )

        return {
            "found": True,
            "x": final_x,
            "y": final_y,
            "confidence": final_conf,
            "description": str(items[0].get("description", target_clean)),
            "consensus_count": best_cluster["count"],
            "screen_width": original_width,
            "screen_height": original_height,
            "virtual_left": vleft,
            "virtual_top": vtop,
            "virtual_width": vwidth,
            "virtual_height": vheight,
        }

    except Exception as error:
        print("Screen target error:", error)
        return None

def report_screen_geometry():
    """Print Windows virtual-desktop geometry without moving the mouse."""
    left, top, width, height = _virtual_screen_bounds()
    try:
        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        cursor = (pt.x, pt.y)
    except Exception:
        cursor = ("unknown", "unknown")

    message = (
        f"Windows virtual desktop: left={left}, top={top}, "
        f"width={width}, height={height}. "
        f"Current mouse position: {cursor[0]}, {cursor[1]}."
    )
    print("\n" + message)
    say(message)
    return True


def handle_screen_target_command(command):
    """Locate a visible target without clicking it."""
    c = command.strip().lower()

    if c in (
        "screen coordinates",
        "desktop coordinates",
        "screen geometry",
        "desktop geometry",
        "where is my mouse",
    ):
        return report_screen_geometry()
    prefixes = (
        "find ", "locate ", "where is ", "where's ", "show me where ",
        "find the ", "locate the ", "where is the ", "where's the "
    )
    target = None
    for prefix in prefixes:
        if c.startswith(prefix):
            target = c[len(prefix):].strip()
            break

    if not target or target in {"my screen", "the screen", "screen"}:
        return False

    # Only treat this as a screen-target request when it sounds like something
    # Jarvis should visually locate, rather than a normal conversation question.
    visual_words = ("button", "icon", "menu", "tab", "link", "box", "field", "window", "app", "logo", "start", "taskbar", "search")
    if not any(word in target for word in visual_words):
        return False

    if not online_available():
        say("I need my online vision brain connected to locate things on the screen.")
        return True

    say("Let me find it.")
    result = find_screen_target(target)
    if not result:
        say("I couldn't inspect the screen just now.")
        return True
    if not result.get("found"):
        say("I can't clearly see " + target + ".")
        return True

    x, y = result["x"], result["y"]
    confidence = float(result.get("confidence", 0) or 0)
    description = str(result.get("description", "the target"))
    if confidence >= 0.85:
        say(f"I found {description} at {x}, {y}.")
    else:
        say(f"I think I found {description} at {x}, {y}, but I'm not completely certain.")
    return True


def handle_vision_action_command(command):
    """Vision -> target -> guarded physical mouse action."""
    try:
        c = command.strip().lower()

        action_prefixes = (
            "click the ", "click ", "left click the ", "left click ",
            "right click the ", "right click ",
            "double click the ", "double click ",
        )
        if not c.startswith(action_prefixes):
            return False

        action = "left"
        clicks = 1
        target = c

        for prefix, button, count in (
            ("double click the ", "left", 2),
            ("double click ", "left", 2),
            ("right click the ", "right", 1),
            ("right click ", "right", 1),
            ("left click the ", "left", 1),
            ("left click ", "left", 1),
            ("click the ", "left", 1),
            ("click ", "left", 1),
        ):
            if c.startswith(prefix):
                target = c[len(prefix):].strip()
                action = button
                clicks = count
                break

        if not target:
            say("I need to know what you want me to click.")
            return True

        result = find_screen_target(target)
        if not result or not result.get("found"):
            say("I couldn't safely locate that target, so I did not move the mouse.")
            return True

        x = int(round(float(result.get("x"))))
        y = int(round(float(result.get("y"))))
        confidence = float(result.get("confidence", 0) or 0)

        # Guard 1: only act on a strong match.
        if confidence < 0.85:
            print(f"VISION ACTION BLOCKED: confidence={confidence:.2f} for {target!r}")
            say(f"I found {target}, but I'm not confident enough to click it.")
            return True

        # For controls exposed by Windows UI Automation, prefer invoking the
        # control directly. This avoids relying on a possibly stale/mismatched
        # mouse coordinate in apps such as Steam desktop mode.
        if result.get('method') == 'windows_uia' and action == 'left' and clicks == 1:
            element = result.get('uia_element')
            if element is not None:
                try:
                    element.invoke()
                    print(f"WINDOWS UIA INVOKE: invoked {target!r} directly.")
                    say(f"Clicked {target}.")
                    return True
                except Exception as exc:
                    print(f"WINDOWS UIA INVOKE: direct invoke failed for {target!r}: {exc}")
                    print("WINDOWS UIA INVOKE: falling back to guarded mouse click.")

        # Guard 2: verify the coordinate is inside the real Windows virtual desktop.
        left, top, width, height = _virtual_screen_bounds()
        if not (left <= x < left + width and top <= y < top + height):
            print(f"VISION ACTION BLOCKED: unsafe coordinates {x}, {y}")
            say("I found the target, but its coordinates are outside the desktop. I did not move the mouse.")
            return True

        # Guard 3: move first, then verify Windows actually placed the cursor there.
        print(
            f"\nVISION MOUSE ACTION: {action} click x={x}, y={y}, "
            f"confidence={confidence:.2f}, target={target!r}"
        )
        if not _mouse_move(x, y):
            say("I found it, but I couldn't move the mouse there.")
            return True

        pt = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            say("I moved toward it, but I couldn't verify the mouse position. I did not click.")
            return True

        actual_x, actual_y = int(pt.x), int(pt.y)
        if abs(actual_x - x) > 2 or abs(actual_y - y) > 2:
            print(f"VISION ACTION BLOCKED: cursor verification failed; got {actual_x}, {actual_y}")
            say("I couldn't safely verify the mouse position, so I did not click.")
            return True

        # Small pause lets Windows finish the cursor move before the click.
        time.sleep(0.12)
        if not _mouse_click(action, clicks):
            say("I moved to the target, but Windows rejected the click.")
            return True

        if clicks == 2:
            say(f"Double clicked {target}.")
        elif action == "right":
            say(f"Right clicked {target}.")
        else:
            say(f"Clicked {target}.")
        return True

    except Exception:
        error_text = traceback.format_exc()
        print("\nVISION ACTION ERROR:\n" + error_text)
        try:
            Path("jarvis_crash.log").write_text(error_text, encoding="utf-8")
        except Exception:
            pass
        say("The vision mouse action hit an error. I did not try again.")
        return True

def handle_screen_mouse_commands(command):
    c = command.strip().lower()

    if c in ("where is the mouse", "where is my mouse", "mouse position"):
        pt = wintypes.POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            say(f"The mouse is at {pt.x}, {pt.y}.")
        else:
            say("I couldn't read the mouse position.")
        return True

    if c in ("screen size", "what is my screen size", "what resolution is my screen"):
        w, h = _screen_size()
        if w and h:
            say(f"Your primary screen is {w} by {h} pixels.")
        else:
            say("I couldn't read the screen size.")
        return True

    # Exact coordinates: move mouse to 500 400 / click at 500 400
    m = re.match(r"^(?:move (?:the )?mouse|move cursor) (?:to )?(\d+)\s*(?:,|by|x| )\s*(\d+)$", c)
    if m:
        x, y = int(m.group(1)), int(m.group(2))
        if _mouse_move(x, y): say(f"Mouse moved to {x}, {y}.")
        else: say("I couldn't move the mouse.")
        return True

    m = re.match(r"^(left |right |middle )?click(?: at)?\s+(\d+)\s*(?:,|by|x| )\s*(\d+)$", c)
    if m:
        button = (m.group(1) or "left ").strip()
        x, y = int(m.group(2)), int(m.group(3))
        if _mouse_move(x, y) and _mouse_click(button): say(f"{button.capitalize()} click sent at {x}, {y}.")
        else: say("I couldn't click there.")
        return True

    if c in ("click", "left click", "click mouse"):
        
        try:
            _real_mouse_click("left"); say("Clicked.")
        except Exception as error:
            print("Real left click failed:", error); say("I couldn't send the click.")
        return True
    if c in ("right click", "right mouse click"):
        try:
            # Native Windows right-click at the current cursor position.
            pt = wintypes.POINT()
            if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                raise RuntimeError("Could not read cursor position.")
            ctypes.windll.user32.SetCursorPos(pt.x, pt.y)
            time.sleep(0.05)
            ctypes.windll.user32.mouse_event(0x0008, 0, 0, 0, 0)  # RIGHTDOWN
            time.sleep(0.05)
            ctypes.windll.user32.mouse_event(0x0010, 0, 0, 0, 0)  # RIGHTUP
            say("Right click sent.")
        except Exception as error:
            print("Right click error:", error)
            say("I couldn't send the right click.")
        return True

    if c in ("double click", "double-click"):
        
        try:
            _real_mouse_click("left", 2); say("Double clicked.")
        except Exception as error:
            print("Real double click failed:", error); say("I couldn't send the double click.")
        return True

    # Relative movement in 100 pixel steps.
    directions = {"left":(-100,0), "right":(100,0), "up":(0,-100), "down":(0,100)}
    for direction, (dx,dy) in directions.items():
        if c in (f"move mouse {direction}", f"move the mouse {direction}", f"move cursor {direction}"):
            pt=wintypes.POINT(); ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            _mouse_move(pt.x+dx, pt.y+dy); say(f"Moving the mouse {direction}."); return True

    if c in ("scroll up", "scroll upwards"):
        _scroll(120*5); say("Scrolling up."); return True
    if c in ("scroll down", "scroll downwards"):
        _scroll(-120*5); say("Scrolling down."); return True

    key_map = {"press enter":0x0D, "press escape":0x1B, "press esc":0x1B, "press tab":0x09, "press space":0x20}
    if c in key_map:
        
        try:
            _real_key(key_map[c]); say(c.replace("press ", "Pressed ") + ".")
        except Exception as error:
            print("Real key press failed:", error); say("I couldn't send that key press.")
        return True

    if c.startswith("type "):
        text = command.strip()[5:]
        if text:
            if _real_type_text(text): say("Typed it.")
            else: say("I couldn't type that.")
            return True

    # Screen questions now use the real screenshot vision layer.
    if handle_screen_vision_command(command):
        return True

    return False

# ============================================================
# WINDOW CONTROL
# ============================================================

def control_window(app_name, action):
    try:
        safe = str(app_name).replace("'", "''")
        script = f"""
$ErrorActionPreference='Stop'
$p = Get-Process | Where-Object {{
 $_.MainWindowHandle -ne 0 -and
 ($_.ProcessName -like '*{safe}*' -or $_.MainWindowTitle -like '*{safe}*')
}} | Select-Object -First 1
if (-not $p) {{ exit 2 }}
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class JW {{
 [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int c);
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
}}
"@
$h=$p.MainWindowHandle
switch ('{action}') {{
 'minimise' {{ [JW]::ShowWindow($h,6) }}
 'maximise' {{ [JW]::ShowWindow($h,3); [JW]::SetForegroundWindow($h) }}
 'restore' {{ [JW]::ShowWindow($h,9); [JW]::SetForegroundWindow($h) }}
 'focus' {{ [JW]::ShowWindow($h,9); [JW]::SetForegroundWindow($h) }}
}}
"""
        r=subprocess.run(["powershell","-NoProfile","-Command",script],
                         capture_output=True,text=True,timeout=15)
        return r.returncode==0
    except Exception as e:
        print("Window control error:",e)
        return False

def handle_window_command(command):
    if command.startswith("bring ") and command.endswith(" to the front"):
        command="switch to "+command[6:-13].strip()
    elif command.startswith("bring ") and command.endswith(" forward"):
        command="switch to "+command[6:-8].strip()

    actions=[
        (("minimise ","minimize "),"minimise","minimised"),
        (("maximise ","maximize "),"maximise","maximised"),
        (("restore ",),"restore","restored"),
        (("switch to ","bring up ","focus "),"focus","brought to the front"),
    ]
    for prefixes,action,spoken in actions:
        for prefix in prefixes:
            if command.startswith(prefix):
                name=command[len(prefix):].strip()
                for article in ("my ","the ","an ","a "):
                    if name.startswith(article):
                        name=name[len(article):].strip()
                        break
                if not name:
                    return False
                if control_window(name,action):
                    say(spoken.capitalize()+" "+name+".")
                else:
                    say("I couldn't find an open window called "+name+".")
                return True
    return False



# ============================================================
# FULLSCREEN CONTROL
# ============================================================

def send_fullscreen_key():
    """Send F11 to the currently active application."""
    try:
        script = """
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class JarvisKeys {
    [DllImport("user32.dll")]
    public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
}
"@
[JarvisKeys]::keybd_event(0x7A, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 80
[JarvisKeys]::keybd_event(0x7A, 0, 2, [UIntPtr]::Zero)
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0
    except Exception as error:
        print("Fullscreen key error:", error)
        return False


def handle_fullscreen_command(command):
    command = command.strip().lower()

    # "make this fullscreen", "fullscreen this", etc.
    generic_fullscreen = (
        "make this fullscreen",
        "fullscreen this",
        "make it fullscreen",
        "fullscreen it",
        "go fullscreen",
        "enter fullscreen",
    )

    generic_exit = (
        "exit fullscreen",
        "leave fullscreen",
        "get out of fullscreen",
        "take this out of fullscreen",
        "exit full screen",
        "leave full screen",
    )

    if command in generic_fullscreen:
        if send_fullscreen_key():
            say("Fullscreen enabled.")
        else:
            say("I had trouble enabling fullscreen.")
        return True

    if command in generic_exit:
        if send_fullscreen_key():
            say("Fullscreen disabled.")
        else:
            say("I had trouble leaving fullscreen.")
        return True

    # "edge fullscreen", "steam fullscreen", etc.
    if command.endswith(" fullscreen"):
        app_name = command[:-len(" fullscreen")].strip()

        if app_name and control_window(app_name, "focus"):
            send_fullscreen_key()
            say("Making " + app_name + " fullscreen.")
            return True

        if app_name:
            say("I couldn't find an open window called " + app_name + ".")
            return True

    return False



# ============================================================
# UNSUPPORTED COMMAND CONFIRMATION
# ============================================================

pending_help_request = None

def ask_before_explaining(command):
    """Ask permission before sending an unsupported command to the AI brain."""
    global pending_help_request
    pending_help_request = command
    say("I'm not programmed to do that right now, sir. Would you like me to tell you how?")

# ============================================================
# PC AWARENESS
# ============================================================

def run_powershell(script, timeout=12):
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip()
    except Exception as error:
        print("PowerShell error:", error)
        return ""

def handle_pc_awareness(command):
    if command in (
        "what apps are running", "what applications are running",
        "what programs are running", "what is running"
    ):
        output = run_powershell(
            "Get-Process | Where-Object {$_.MainWindowTitle} | "
            "Sort-Object CPU -Descending | Select-Object -First 8 ProcessName,MainWindowTitle | "
            "ForEach-Object {$_.ProcessName} | Get-Unique"
        )
        apps=[x.strip() for x in output.splitlines() if x.strip()]
        if apps:
            say("The main applications currently running are: " + ", ".join(apps[:8]) + ".")
        else:
            say("I couldn't find any visible application windows right now.")
        return True

    if command in ("what am i currently using", "what am i using", "what app am i using", "what is open right now"):
        title=run_powershell(
            'Add-Type @\'using System; using System.Runtime.InteropServices; '
            'public class JActive {[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow(); '
            '[DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h,System.Text.StringBuilder s,int n);}\'; '
            '$s=New-Object System.Text.StringBuilder 512; '
            '[JActive]::GetWindowText([JActive]::GetForegroundWindow(),$s,$s.Capacity); $s.ToString()'
        )
        if title:
            say("You're currently using " + title + ".")
        else:
            say("I couldn't determine the active window.")
        return True

    if command in ("what's using my cpu", "what is using my cpu", "cpu usage", "what is using the most cpu"):
        output=run_powershell(
            "Get-Process | Sort-Object CPU -Descending | Select-Object -First 5 ProcessName,CPU | "
            "ConvertTo-Json -Compress"
        )
        try:
            data=json.loads(output)
            if isinstance(data,dict): data=[data]
            items=[f'{x.get("ProcessName")} ({round(float(x.get("CPU",0)),1)} CPU seconds)' for x in data[:5]]
            say("The processes with the highest accumulated CPU time are: " + ", ".join(items) + ".")
        except Exception:
            say("I couldn't read the CPU process information just now.")
        return True

    if command in ("how much ram am i using", "ram usage", "memory usage", "how much memory am i using"):
        output=run_powershell(
            "$os=Get-CimInstance Win32_OperatingSystem; "
            "$total=[math]::Round($os.TotalVisibleMemorySize/1MB,1); "
            "$free=[math]::Round($os.FreePhysicalMemory/1MB,1); "
            "$used=[math]::Round($total-$free,1); "
            "\"$used|$total\""
        )
        try:
            used,total=output.split("|")
            say(f"You're using approximately {used} gigabytes of RAM out of {total} gigabytes.")
        except Exception:
            say("I couldn't read the memory usage just now.")
        return True

    if command in (
        "how much storage do i have",
        "how much space is left",
        "how much disk space is left",
        "how much space do i have",
        "how much storage is left",
        "how much space is on my c drive",
        "how much storage is on my c drive",
        "how full is my c drive",
        "c drive space",
        "c drive storage",
    ):
        output = run_powershell(
            "$d=Get-CimInstance Win32_LogicalDisk -Filter \"DeviceID='C:'\"; "
            "$free=[math]::Round($d.FreeSpace/1GB,1); "
            "$total=[math]::Round($d.Size/1GB,1); "
            "$used=[math]::Round($total-$free,1); "
            "$pct=[math]::Round(($used/$total)*100); "
            "\"$free|$total|$pct\""
        )
        try:
            free, total, pct = output.split("|")
            say(f"Your C drive has approximately {free} gigabytes free out of {total} gigabytes. It is about {pct} percent full.")
        except Exception:
            say("I couldn't read your C drive storage information just now.")
        return True

    if command in ("what's my battery level", "what is my battery level", "battery level", "battery percentage"):
        output=run_powershell(
            "Get-CimInstance Win32_Battery | Select-Object -First 1 EstimatedChargeRemaining,BatteryStatus | ConvertTo-Json -Compress"
        )
        try:
            data=json.loads(output)
            pct=data.get("EstimatedChargeRemaining")
            if pct is None:
                say("I can't see a battery, so this computer may be running from mains power.")
            else:
                say(f"Your battery is at {pct} percent.")
        except Exception:
            say("I couldn't read the battery level right now.")
        return True

    return False


# ============================================================
# FILE AND FOLDER CONTROL
# ============================================================

KNOWN_FOLDERS = {
    "desktop": os.path.join(os.path.expanduser("~"), "Desktop"),
    "downloads": os.path.join(os.path.expanduser("~"), "Downloads"),
    "documents": os.path.join(os.path.expanduser("~"), "Documents"),
    "pictures": os.path.join(os.path.expanduser("~"), "Pictures"),
    "music": os.path.join(os.path.expanduser("~"), "Music"),
    "videos": os.path.join(os.path.expanduser("~"), "Videos"),
}

# Folders that should never be crawled while looking for a user folder.
FOLDER_SEARCH_SKIP = {
    "$recycle.bin", "system volume information", "windows", "program files",
    "program files (x86)", "programdata", "appdata", "perflogs",
    "recovery", "msocache", "node_modules", ".git"
}


def open_path(path, label=None):
    try:
        if os.path.exists(path):
            os.startfile(path)
            say("Opening " + (label or os.path.basename(path) or "that folder") + ".")
            return True
    except Exception as error:
        print("Open path error:", error)
    return False


def get_available_drive_roots():
    """Return every currently available Windows drive Jarvis can search."""
    roots = []
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for index in range(26):
            if mask & (1 << index):
                root = chr(65 + index) + ":\\"
                # Keep drives Windows reports as present and accessible.
                if os.path.isdir(root):
                    roots.append(root)
    except Exception as error:
        print("Drive enumeration error:", error)
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            root = letter + ":\\"
            if os.path.isdir(root):
                roots.append(root)
    return roots


def normalise_folder_name(value):
    value = re.sub(r"[^a-z0-9 ]+", " ", str(value).lower())
    return re.sub(r"\s+", " ", value).strip()


def find_folder_on_drives(folder_name, drive_letter=None):
    """Find a folder by name on one drive or across all currently available drives."""
    wanted = normalise_folder_name(folder_name)
    if not wanted:
        return None

    if drive_letter:
        drive_letter = drive_letter.upper().rstrip(":")
        roots = [drive_letter + ":\\"]
    else:
        roots = get_available_drive_roots()

    # First check direct children. This handles common folders instantly.
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            for name in os.listdir(root):
                path = os.path.join(root, name)
                if os.path.isdir(path) and normalise_folder_name(name) == wanted:
                    return path
        except (PermissionError, OSError):
            pass

    # Then search recursively. Stop immediately at the first exact name match.
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            for base, dirs, _ in os.walk(root, topdown=True, followlinks=False):
                dirs[:] = [
                    d for d in dirs
                    if normalise_folder_name(d) not in FOLDER_SEARCH_SKIP
                    and not d.startswith(".")
                ]
                for name in dirs:
                    if normalise_folder_name(name) == wanted:
                        return os.path.join(base, name)
        except (PermissionError, OSError):
            continue
    return None


def find_user_file(query):
    query=query.lower().strip()
    roots=[KNOWN_FOLDERS["desktop"], KNOWN_FOLDERS["downloads"], KNOWN_FOLDERS["documents"], KNOWN_FOLDERS["pictures"]]
    matches=[]
    items_scanned = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            for base, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                update_current_action(location=base)
                for name in files:
                    items_scanned += 1
                    if query in name.lower():
                        matches.append(os.path.join(base,name))
                    if items_scanned % 20 == 0 or query in name.lower():
                        update_current_action(
                            items_scanned=items_scanned,
                            matches=len(matches),
                            match_names=[os.path.basename(m) for m in matches],
                        )
                    if len(matches)>=5:
                        update_current_action(
                            items_scanned=items_scanned,
                            matches=len(matches),
                            match_names=[os.path.basename(m) for m in matches],
                        )
                        return matches
        except Exception:
            pass
    update_current_action(
        items_scanned=items_scanned,
        matches=len(matches),
        match_names=[os.path.basename(m) for m in matches],
    )
    return matches


def extract_folder_open_request(command):
    """Understand requests such as 'open Movies on drive D' or 'open the Movies folder'."""
    c = command.strip().lower()
    prefixes = ("open ", "show ", "take me to ", "go to ")
    body = None
    for prefix in prefixes:
        if c.startswith(prefix):
            body = c[len(prefix):].strip()
            break
    if body is None:
        return None, None

    for article in ("my ", "the ", "a ", "an "):
        if body.startswith(article):
            body = body[len(article):].strip()
            break

    # Accept: on drive d / in drive d / on d drive / on d:
    match = re.search(r"\s+(?:on|in)\s+(?:drive\s*)?([a-z])(?::|\s+drive)?\s*$", body)
    drive = None
    if match:
        drive = match.group(1).upper()
        body = body[:match.start()].strip()

    # 'movies folder' and 'movies' should both search for Movies.
    body = re.sub(r"\s+(?:folder|directory)\s*$", "", body).strip()
    if not body:
        return None, drive
    return body, drive


def handle_file_folder_commands(command):
    c=command.strip().lower()

    for phrase, path in KNOWN_FOLDERS.items():
        if c in (f"open {phrase}", f"open my {phrase}", f"show my {phrase}", f"show {phrase}"):
            return open_path(path, phrase)

    prefixes=("create a folder called ", "create folder called ", "make a folder called ", "make folder called ")
    for prefix in prefixes:
        if c.startswith(prefix):
            name=c[len(prefix):].strip().strip('"')
            if not name or any(x in name for x in ('\\','/',':','*','?','<','>','|')):
                say("That folder name isn't valid, sir.")
                return True
            path=os.path.join(KNOWN_FOLDERS["documents"], name)
            try:
                os.makedirs(path, exist_ok=True)
                say("Folder " + name + " is ready in your Documents.")
            except Exception as error:
                print("Create folder error:", error)
                say("I couldn't create that folder.")
            return True

    prefixes=("find my file called ", "find the file called ", "find file called ", "find my document called ", "find document called ")
    for prefix in prefixes:
        if c.startswith(prefix):
            query=c[len(prefix):].strip().strip('"')
            if not query:
                say("What file would you like me to look for?")
                return True
            start_activity(
                [
                    "Understanding request",
                    "Checking file system",
                    "Scanning for files",
                    f'Searching for "{query}"',
                    "Reading file details",
                    "Preparing results",
                ],
                title=f'Searching for "{query}"',
            )
            advance_activity()  # -> Checking file system
            advance_activity()  # -> Scanning for files
            advance_activity(detail=f'Searching for "{query}"')  # -> Searching for "X"
            matches=find_user_file(query)
            advance_activity()  # -> Reading file details
            if not matches:
                say("I couldn't find a file matching " + query + ".")
            elif len(matches)==1:
                say("I found " + os.path.basename(matches[0]) + ". Opening it.")
                open_path(matches[0], os.path.basename(matches[0]))
            else:
                say("I found a few matches. Opening the first one: " + os.path.basename(matches[0]) + ".")
                open_path(matches[0], os.path.basename(matches[0]))
            advance_activity()  # -> Preparing results
            finish_activity()
            return True

    folder_name, drive = extract_folder_open_request(c)
    if folder_name:
        # Let the existing application handlers deal with obvious applications.
        # Folder search is deliberately tried first for requests that explicitly
        # say folder/directory or name a drive.
        explicit_folder = (" folder" in c or " directory" in c or drive is not None)
        if explicit_folder:
            if drive:
                say("Looking for the " + folder_name + " folder on drive " + drive + ".")
            else:
                say("Looking for the " + folder_name + " folder on all available drives.")
            path = find_folder_on_drives(folder_name, drive)
            if path:
                label = os.path.basename(path)
                say("I found " + label + (" on drive " + path[0].upper() if len(path) >= 2 and path[1] == ":" else "") + ". Opening it.")
                open_path(path, label)
            else:
                if drive:
                    say("I couldn't find a folder called " + folder_name + " on drive " + drive + ".")
                else:
                    say("I couldn't find a folder called " + folder_name + " on any available drive.")
            return True

    return False


# ============================================================
# MEDIA CONTROL
# ============================================================

MEDIA_KEYS = {
    "volume up": 0xAF,
    "volume down": 0xAE,
    "mute": 0xAD,
    "play pause": 0xB3,
    "next track": 0xB0,
    "previous track": 0xB1,
}

def press_media_key(key_name, times=1):
    vk=MEDIA_KEYS[key_name]
    try:
        script=f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class JMedia {{
 [DllImport("user32.dll")] public static extern void keybd_event(byte bVk,byte bScan,uint flags,UIntPtr extraInfo);
}}
"@
for($i=0;$i -lt {int(times)};$i++){{
 [JMedia]::keybd_event({vk},0,0,[UIntPtr]::Zero)
 Start-Sleep -Milliseconds 80
 [JMedia]::keybd_event({vk},0,2,[UIntPtr]::Zero)
}}
"""
        result=subprocess.run(["powershell","-NoProfile","-Command",script],capture_output=True,text=True,timeout=10)
        return result.returncode==0
    except Exception as error:
        print("Media key error:", error)
        return False

def handle_media_commands(command):
    c=command.strip().lower()
    mappings={
        ("mute", "mute sound", "mute the sound"): ("mute",1,"Muted."),
        ("volume up", "turn volume up", "increase volume", "make it louder"): ("volume up",3,"Volume increased."),
        ("volume down", "turn volume down", "decrease volume", "make it quieter"): ("volume down",3,"Volume decreased."),
        ("pause", "pause media", "pause music", "pause video"): ("play pause",1,"Paused."),
        ("play", "play music", "resume", "resume music", "resume video"): ("play pause",1,"Resuming."),
        ("play pause", "toggle pause", "toggle music"): ("play pause",1,"Done."),
        ("next song", "next track", "skip song", "skip"): ("next track",1,"Skipping to the next track."),
        ("previous song", "previous track", "go back a song", "go back one song"): ("previous track",1,"Going back to the previous track."),
    }
    if c in mappings:
        key,times,response=mappings[c]
        if press_media_key(key,times):
            say(response)
        else:
            say("I couldn't control the media keys just now.")
        return True
    return False


# ============================================================
# GAME CRASH MONITORING + DIAGNOSIS
# ============================================================
# Watches Windows' own crash reporting (the Application event log, Event
# ID 1000 -- the same report Windows itself generates for any unhandled
# application exception) for a crash from something running out of a
# Steam game library, and asks the local AI to diagnose it using REAL
# data (the actual faulting module/exception code, current CPU/GPU temps)
# rather than guessing. Danny's explicit choice: diagnose and ask, never
# silently change anything -- see get_confirmation_input() below for the
# actual game-relaunch offer, and the docstring on _diagnose_game_crash
# for why deeper auto-remediation (driver/GPU settings) isn't attempted.

_last_crash_check_time = None
CRASH_MONITOR_POLL_SECONDS = 20


def _looks_like_game_path(exe_path):
    """
    Heuristic for "is this a game, for crash-monitoring purposes": running
    from a Steam game library. Covers danny's primary way of installing/
    playing games (this file already has extensive Steam-specific
    automation elsewhere) without needing to parse Steam's
    libraryfolders.vdf to enumerate exact library locations -- any drive,
    any library, this still matches.
    """
    if not exe_path:
        return False
    lowered = exe_path.lower()
    return "steamapps\\common" in lowered or "steamapps/common" in lowered


def _diagnose_game_crash(game_name, faulting_module, exception_code):
    """
    Ask the local AI brain to explain a crash using REAL data (the actual
    faulting module and exception code from Windows' own crash report,
    plus current real CPU/GPU temps) and suggest ONE concrete, specific
    next step -- in Jarvis's own voice, not a generic troubleshooting
    checklist.

    Deliberately does NOT attempt driver/GPU-setting changes as part of
    this diagnosis step: a crash's true cause (bad driver, failing VRAM,
    an actual game bug, overheating, a corrupted install) can look
    similar from a single crash report, and guessing wrong and changing
    a real setting could make things worse, not better -- danny's own
    explicit choice was diagnose-and-ask, not auto-remediate. The one
    thing this DOES offer to do (only after asking) is relaunch the game,
    which is always safe.
    """
    cpu_temp, gpu_temp = get_hardware_monitor_temps()
    cpu_name, gpu_name = _get_hardware_names()

    instructions = """
You are Jarvis, diagnosing a Windows game crash for the user using a REAL
crash report and REAL current hardware readings. Do not invent details
beyond what's given.

Give a short (2-4 sentence), specific, spoken-style diagnosis: your best
read on the likely cause given the faulting module/exception code and
current temps, and ONE concrete thing worth trying. If the data doesn't
clearly point to a single cause, say so honestly rather than guessing
confidently. Do not produce a checklist or multiple options -- pick your
single best read.
"""
    user_content = f"""GAME: {game_name}
FAULTING MODULE: {faulting_module}
EXCEPTION CODE: {exception_code}
CURRENT CPU: {cpu_name}, {cpu_temp if cpu_temp is not None else 'unknown'}°C
CURRENT GPU: {gpu_name}, {gpu_temp if gpu_temp is not None else 'unknown'}°C"""

    try:
        return ask_ollama_brain(instructions, user_content, think="low")
    except Exception as error:
        print("Crash diagnosis AI call failed:", error)
        return None


def _handle_game_crash_report(game_name, app_path, faulting_module, exception_code):
    print(f"\nGAME CRASH DETECTED: {game_name} (module: {faulting_module}, exception: {exception_code})")
    log_recent_action(f"{game_name} crashed ({faulting_module}, {exception_code})")

    diagnosis = _diagnose_game_crash(game_name, faulting_module, exception_code)
    if not diagnosis:
        say(f"{game_name} just crashed. I couldn't reach my local AI brain to diagnose it, but the fault was in {faulting_module}.")
        return

    say(f"{game_name} just crashed. {diagnosis} Would you like me to relaunch it?")
    answer = get_confirmation_input().lower()
    if answer in ("yes", "yeah", "y"):
        if handle_steam_open_game_command(f"open {game_name} on steam"):
            return
        say(f"I couldn't relaunch {game_name} automatically -- you may need to start it yourself.")
    else:
        say("Understood. I won't change anything.")


def _poll_for_game_crashes():
    """
    Check for new Application Error (Event ID 1000) crash reports since
    the last check. Runs on a background thread every
    CRASH_MONITOR_POLL_SECONDS -- crash reports aren't time-critical to
    the second, so this deliberately doesn't need to be instant.
    """
    global _last_crash_check_time
    now = datetime.datetime.now()
    since = _last_crash_check_time or (now - datetime.timedelta(seconds=CRASH_MONITOR_POLL_SECONDS))
    _last_crash_check_time = now

    since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
    ps_script = (
        f"$since = Get-Date '{since_str}'; "
        "Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000; StartTime=$since} "
        "-ErrorAction SilentlyContinue | "
        "ForEach-Object { [PSCustomObject]@{ Message = $_.Message } } | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=15,
        )
        raw = result.stdout.strip()
        if not raw:
            return
        events = json.loads(raw)
        if isinstance(events, dict):
            events = [events]
        for event in events:
            _process_crash_event(event.get("Message", ""))
    except Exception as error:
        print("Crash monitor check failed:", error)


def _process_crash_event(message):
    app_path_match = re.search(r"Faulting application path:\s*(.+)", message)
    app_path = app_path_match.group(1).strip() if app_path_match else ""
    if not _looks_like_game_path(app_path):
        return  # not something running from a Steam game library -- ignore

    app_name_match = re.search(r"Faulting application name:\s*([^,]+)", message)
    module_match = re.search(r"Faulting module name:\s*([^,]+)", message)
    exception_match = re.search(r"Exception code:\s*(0x[0-9a-fA-F]+)", message)

    app_name = app_name_match.group(1).strip() if app_name_match else os.path.basename(app_path)
    game_name = os.path.splitext(app_name)[0]
    module_name = module_match.group(1).strip() if module_match else "unknown"
    exception_code = exception_match.group(1).strip() if exception_match else "unknown"

    _handle_game_crash_report(game_name, app_path, module_name, exception_code)


def _game_crash_monitor_loop():
    while True:
        try:
            _poll_for_game_crashes()
        except Exception as error:
            print("Game crash monitor error:", error)
        time.sleep(CRASH_MONITOR_POLL_SECONDS)


def start_game_crash_monitor():
    threading.Thread(target=_game_crash_monitor_loop, daemon=True).start()


start_game_crash_monitor()


# ============================================================
# MAIN LOOP
# ============================================================

print(f"\nJarvis is online. ({_time_of_day_greeting().capitalize()} startup)")
print(f"Machine profile: {JARVIS_MACHINE_CONTEXT}")
if not UI_AUTOMATION_AVAILABLE:
    print("UI Automation helper is not installed. Vision fallback is still available.")
    print("To enable Windows UI Automation: python -m pip install pywinauto")
print("If an unexpected error occurs, this window will stay open so you can read it.")

while True:
    try:
        # Cleared at the top of every loop iteration: this marks "idle,
        # waiting for the next command" for as long as we're back here
        # about to poll for input. It gets set again the moment a real
        # command comes in below, and stays set through every branch of
        # this loop body until we return here for the next one -- that's
        # the "processing" window a UI (the HUD) can poll via /status.
        jarvis_processing.clear()

        if poll_interrupt_inputs():
            continue

        user_message = get_user_input()

        if not user_message.strip():
            continue

        jarvis_processing.set()

        # Close out whatever the previous turn's Live Activity was showing
        # and start a fresh generic one for this turn. Specific handlers
        # (ask_jarvis, file search, v58 automation) overwrite this with
        # their own real steps; anything else just shows this one step
        # for its (typically near-instant) synchronous runtime.
        finish_activity()
        start_activity(["Understanding request"])

        command = clean_natural_command(user_message.lower().strip())

        # Exit is always local and immediate.
        if command in ["exit", "quit", "goodbye", "bye"]:
            say("Goodbye.")
            break

        if is_stop_command(user_message):
            stop_jarvis_speaking()
            continue

        if handle_smart_routine_phrases(command):
            continue

        # v58 autonomous route runs before v53's single-app handlers so
        # composite commands are treated as tasks, not application names.
        if handle_v58_autonomous_commands(command):
            continue

        if run_local_command_flow(command, user_message):
            continue

        # Genuine last resort before defaulting to plain conversation: a
        # request like "change my display setting from 720p to 1080p" has
        # no "open/launch/play" prefix and no " and "/"then " sequence
        # marker, so it never trips v58's composite-task heuristic or the
        # launch-cleanup fallback above — it would otherwise just get
        # chatted about instead of attempted. This is a free local check
        # (does this look like a PC action, not a question or small talk?)
        # so it can only affect commands that would otherwise just be
        # conversation anyway.
        if _looks_like_pc_action_request(command) and handle_v58_autonomous_commands(command, force=True):
            continue

        # ask_jarvis() streams the reply and speaks it sentence-by-sentence
        # itself (see _speak_stream) -- no separate say() call needed here.
        ask_jarvis(user_message)
        continue

    except KeyboardInterrupt:
        print("\nJarvis interrupted.")
        break

    except Exception:
        error_text = traceback.format_exc()

        print("\n" + "=" * 70)
        print("JARVIS CRASH CAPTURE")
        print("=" * 70)
        print(error_text)
        print("=" * 70)

        try:
            Path("jarvis_crash.log").write_text(error_text, encoding="utf-8")
            print("The full error was also saved to: jarvis_crash.log")
        except Exception:
            pass

        print("\nJarvis has NOT closed so you can read this error.")
        input("Press Enter to return to Jarvis...")

