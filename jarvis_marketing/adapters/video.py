"""
Video providers (swappable). A provider turns a render spec into files plus a technical probe.

    spec = {"creative_uid", "images": [real product image URLs], "lines": [VO/caption lines],
            "duration_s", "link", "brand", "voice"}

  MockVideo       SIMULATED: writes a manifest only (no video), and a probe that describes the spec.
  LocalSlideshow  real: jarvis_video.py (Pillow + edge-tts + ffmpeg, 1080x1920, captions, music)
                  using ONLY the supplier's own product photos. Runs on Danny's PC.

Generative AI video is deliberately not a provider yet: it can change how the product looks, which
breaks the "real product imagery, accurate claims" rule. See the provider comparison in the plan.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

from . import Adapter, Unavailable

PROVIDERS = {
    "local_slideshow": {"cost": "free", "quality": "clean slideshow with zooms, captions, neural VO, own music",
                        "reliability": "high (local; needs edge-tts online for the voice)",
                        "product_accuracy": "exact: only the supplier's photos are shown"},
    "mock": {"cost": "free", "quality": "none (manifest only)", "reliability": "n/a", "product_accuracy": "n/a (SIMULATED)"},
}


def probe(path):
    """ffprobe facts about a rendered file: size, duration, streams, audio. Raises Unavailable without ffprobe."""
    exe = shutil.which("ffprobe")
    if not exe:
        raise Unavailable("ffprobe isn't installed, so the render can't be checked.")
    out = subprocess.run([exe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr[:200]}")
    data = json.loads(out.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    return {"width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
            "duration_s": round(float(fmt.get("duration") or 0), 2), "has_audio": audio is not None,
            "video_codec": video.get("codec_name"), "bitrate_kbps": round(int(fmt.get("bit_rate") or 0) / 1000),
            "size_bytes": int(fmt.get("size") or 0)}


class MockVideo(Adapter):
    name, simulated = "mock_video", True

    def __init__(self, recorder=None):
        super().__init__(recorder)
        self.defect = None           # tests: "landscape" | "silent" | "long" to simulate a bad render

    def render(self, spec, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        manifest = os.path.join(out_dir, f"{spec['creative_uid']}.SIMULATED.json")
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(dict(spec, simulated=True, note="SIMULATED render: no video file was made"), f, indent=1)
        w, h = (1920, 1080) if self.defect == "landscape" else (1080, 1920)
        dur = spec["duration_s"] * (3 if self.defect == "long" else 1)
        p = {"width": w, "height": h, "duration_s": dur, "has_audio": self.defect != "silent", "video_codec": "h264",
             "bitrate_kbps": 4000, "size_bytes": 0, "simulated": True}
        return {"files": {"manifest": manifest}, "probe": p, "log": "SIMULATED render (manifest only)."}


class LocalSlideshow(Adapter):
    name = "local_slideshow"

    def __init__(self, recorder=None, python=sys.executable, script=None, timeout=900):
        super().__init__(recorder)
        from jarvis_marketing import config
        self.python, self.timeout = python, timeout
        self.script = script or os.path.join(config.REPO_DIR, "jarvis_video.py")

    def render(self, spec, out_dir):
        if not os.path.exists(self.script):
            raise Unavailable("jarvis_video.py isn't here.")
        os.makedirs(out_dir, exist_ok=True)
        work = tempfile.mkdtemp(prefix="jm-render-")
        images = []
        for i, url in enumerate(spec["images"]):
            if not str(url).startswith("https://"):
                raise Unavailable(f"Image {url} isn't a real product photo URL.")
            path = os.path.join(work, f"img{i}.jpg")
            req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/marketing"})
            with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
                f.write(r.read())
            images.append(path)
        output = os.path.join(out_dir, f"{spec['creative_uid']}.mp4")
        job = {"images": images, "lines": spec["lines"], "brand": spec.get("brand") or "Collective",
               "link": spec.get("link_text") or "", "output": output, "music": True}
        if spec.get("voice"):
            job["voice"] = spec["voice"]
        spec_file = os.path.join(work, "spec.json")
        with open(spec_file, "w", encoding="utf-8") as f:
            json.dump(job, f)
        run = subprocess.run([self.python, self.script, spec_file], capture_output=True, text=True, timeout=self.timeout)
        log = (run.stdout + run.stderr)[-3000:]
        if run.returncode != 0 or not os.path.exists(output):
            raise RuntimeError(f"jarvis_video.py failed: {log[-300:]}")
        return {"files": {"video": output}, "probe": probe(output), "log": log}
