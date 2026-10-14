"""
Jarvis TikTok Video Maker
=========================

Turns a few product photos plus a short script into a vertical
(1080x1920, 9:16) TikTok/Reels video in the fast style that performs
on TikTok: a new shot every ~1.8s with punch-in zooms and flash cuts,
2-3 word pop-up captions synced word-by-word to a neural voiceover,
a soft upbeat music bed Jarvis composes itself (so there is no
copyright problem), and an end card with the shop name and link.

Free and keyless: Pillow draws the frames, numpy synthesises the
music, the ffmpeg binary bundled with imageio-ffmpeg encodes and
mixes, and edge-tts (Microsoft's public neural voices) speaks.
Jarvis never posts anything itself -- the finished .mp4 is handed to
Danny to upload.

    python jarvis_video.py spec.json

spec.json:
    {
      "images": ["photo1.jpg", "photo2.jpg"],
      "lines":  ["Hook sentence.", "Benefit.", "Call to action."],
      "brand":  "Hooleza",
      "link":   "",   (optional; blank shows "Link in bio")
      "output": "generated_videos/my_product.mp4",
      "voice":  "en-GB-SoniaNeural",          (optional)
      "music":  true,                         (optional, default true)
      "logo":   null, "badge": null           (optional; null = no branding)
    }
"""

import asyncio
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import wave

import edge_tts
import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
FPS = 30
DEFAULT_VOICE = "en-GB-SoniaNeural"
VOICE_RATE = "+12%"
LINE_GAP = 0.12            # seconds of breath between script lines
SHOT_SECONDS = 1.8         # target cut rhythm (TikTok: 2-3s for impulse buys)
END_CARD_SECONDS = 2.2
CAPTION_Y = 0.66           # clear of TikTok's bottom UI and side buttons
MUSIC_BPM = 112
SAMPLE_RATE = 44100
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_videos")
DEFAULT_LOGO = os.path.join(OUTPUT_DIR, "brand", "collective_logo_white.png")
DEFAULT_BADGE = os.path.join(OUTPUT_DIR, "brand", "collective_icon.png")

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\seguibl.ttf",   # Segoe UI Black
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def _font(size):
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _ffmpeg():
    return imageio_ffmpeg.get_ffmpeg_exe()


def _audio_seconds(path):
    """Read a clip's duration from ffmpeg's banner (no ffprobe bundled)."""
    proc = subprocess.run([_ffmpeg(), "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", proc.stderr)
    if not m:
        raise RuntimeError(f"could not read duration of {path}")
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


# ---------------------------------------------------------------- voice

async def _speak(text, voice, path):
    """Save speech to path; return [(word, start_s, end_s)]."""
    comm = edge_tts.Communicate(text, voice, rate=VOICE_RATE, boundary="WordBoundary")
    words = []
    with open(path, "wb") as fh:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                fh.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append((chunk["text"], start, start + chunk["duration"] / 1e7))
    return words


def _caption_chunks(line, words, line_start):
    """Group a line's spoken words into 2-3 word pop-up captions.

    edge-tts reports words without punctuation, so the punctuation is
    re-attached from the script text to decide where a chunk may end.
    """
    script_words = line.split()
    chunks, current = [], []
    for i, (word, start, end) in enumerate(words):
        shown = script_words[i] if i < len(script_words) else word
        current.append((shown, line_start + start, line_start + end))
        if len(current) == 3 or (len(current) >= 2 and re.search(r"[.,!?]$", shown)):
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------- music

def _compose_music(seconds, path):
    """Soft, upbeat pop bed: plucked I-V-vi-IV arps, bass, gentle drums.

    Composed from scratch with numpy, so it is ours to use anywhere.
    """
    sr = SAMPLE_RATE
    beat = 60.0 / MUSIC_BPM
    n = int(seconds * sr) + sr
    out = np.zeros(n)
    t_note = lambda dur: np.arange(int(dur * sr)) / sr

    def pluck(freq, dur, vol):
        t = t_note(dur)
        env = np.exp(-t * 6.0)
        tone = (np.sin(2 * np.pi * freq * t) + 0.35 * np.sin(4 * np.pi * freq * t)
                + 0.12 * np.sin(6 * np.pi * freq * t))
        return vol * env * tone

    def add(sig, at):
        i = int(at * sr)
        if i >= n:
            return
        end = min(n, i + len(sig))
        out[i:end] += sig[: end - i]

    # C - G - Am - F, one bar each (4 beats)
    chords = [(261.63, 329.63, 392.00), (196.00, 246.94, 293.66),
              (220.00, 261.63, 329.63), (174.61, 220.00, 261.63)]
    bar = 4 * beat
    bars = int(seconds / bar) + 2
    rng = np.random.default_rng(7)
    for b in range(bars):
        root = chords[b % 4]
        t0 = b * bar
        # 8th-note arpeggio, up an octave
        pattern = [0, 1, 2, 1, 0, 1, 2, 1]
        for k, idx in enumerate(pattern):
            add(pluck(root[idx] * 2, beat * 0.9, 0.16), t0 + k * beat / 2)
        # bass on each beat
        for k in range(4):
            tb = t_note(beat * 0.95)
            add(0.28 * np.exp(-tb * 3) * np.sin(2 * np.pi * root[0] / 2 * tb), t0 + k * beat)
        # soft kick on 1 and 3
        for k in (0, 2):
            tk = t_note(0.25)
            freq = 120 * np.exp(-tk * 18) + 45
            add(0.55 * np.exp(-tk * 14) * np.sin(2 * np.pi * np.cumsum(freq) / sr), t0 + k * beat)
        # light closed hats on the off-beats
        for k in range(4):
            th = t_note(0.05)
            add(0.05 * np.exp(-th * 90) * rng.uniform(-1, 1, len(th)), t0 + k * beat + beat / 2)

    out = out[: int(seconds * sr)]
    # gentle fade in/out, normalise
    fade = int(0.6 * sr)
    out[:fade] *= np.linspace(0, 1, fade)
    out[-fade:] *= np.linspace(1, 0, fade)
    out = out / (np.abs(out).max() + 1e-9) * 0.9
    pcm = (out * 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


# ---------------------------------------------------------------- frames

def _backdrop(photo):
    """Photo blurred and darkened to fill the 9:16 frame behind the product."""
    scale = max(W / photo.width, H / photo.height)
    bg = photo.resize((int(photo.width * scale) + 1, int(photo.height * scale) + 1))
    left, top = (bg.width - W) // 2, (bg.height - H) // 2
    bg = bg.crop((left, top, left + W, top + H)).filter(ImageFilter.GaussianBlur(40))
    return Image.blend(bg, Image.new("RGB", (W, H), "black"), 0.35)


def _draw_caption(img, chunk, now, size=104):
    """TikTok-style caption: white words, the one being spoken in yellow."""
    draw = ImageDraw.Draw(img)
    font = _font(size)
    words = [w for w, _, _ in chunk]
    space = draw.textlength(" ", font=font)
    widths = [draw.textlength(w, font=font) for w in words]
    total = sum(widths) + space * (len(words) - 1)
    # wrap to two lines if too wide
    lines = [list(range(len(words)))]
    if total > W - 120 and len(words) > 1:
        lines = [list(range(len(words) - 1)), [len(words) - 1]]
    line_h = int(size * 1.15)
    y = int(H * CAPTION_Y) - (len(lines) * line_h) // 2
    # pop: scale-in over the chunk's first 0.12s is approximated by a lift
    since = now - chunk[0][1]
    y += int(max(0.0, 0.12 - since) * 120)
    for line in lines:
        lw = sum(widths[i] for i in line) + space * (len(line) - 1)
        x = (W - lw) / 2
        for i in line:
            _, start, end = chunk[i]
            fill = "#FFE135" if start <= now < end + 0.05 else "white"
            draw.text((x, y), words[i], font=font, fill=fill,
                      stroke_width=max(5, size // 13), stroke_fill="black")
            x += widths[i] + space
        y += line_h


def _shot_frames(photo, bg, seconds, motion):
    """Yield frames for one shot: punch-in then a slow push/pull/pan."""
    base_w = W - 60
    base = photo.resize((base_w, int(photo.height * base_w / photo.width)))
    n = max(1, round(seconds * FPS))
    for i in range(n):
        p = i / max(1, n - 1)
        punch = 1.0 + 0.10 * max(0.0, 1 - i / 4)          # first 4 frames
        if motion == "in":
            zoom, dx = 1.0 + 0.10 * p, 0
        elif motion == "out":
            zoom, dx = 1.10 - 0.10 * p, 0
        elif motion == "left":
            zoom, dx = 1.08, int(40 - 80 * p)
        else:
            zoom, dx = 1.08, int(-40 + 80 * p)
        zoom *= punch
        pw, ph = int(base.width * zoom), int(base.height * zoom)
        frame = bg.copy()
        frame.paste(base.resize((pw, ph)), ((W - pw) // 2 + dx, (H - ph) // 2 - 180))
        if i < 2:   # flash cut
            frame = Image.blend(frame, Image.new("RGB", (W, H), "white"), 0.35 - 0.15 * i)
        yield frame


def _load_logo(path, width):
    if not path or not os.path.exists(path):
        return None
    logo = Image.open(path).convert("RGBA")
    return logo.resize((width, int(logo.height * width / logo.width)), Image.LANCZOS)


def _end_card(photo, brand, link, logo=None):
    card = _backdrop(photo)
    draw = ImageDraw.Draw(card)
    # No web address on screen by default: the link lives in the TikTok
    # bio/caption, so a domain change never means re-rendering videos.
    lines = [("Shop now", 0.50, 96, "#FFE135"), (link or "Link in bio", 0.58, 72, "white")]
    if logo is not None:
        card.paste(logo, ((W - logo.width) // 2, int(H * 0.40) - logo.height // 2), logo)
    else:
        lines.insert(0, (brand, 0.38, 160, "white"))
    for text, y, size, fill in lines:
        font = _font(size)
        x = (W - draw.textlength(text, font=font)) / 2
        draw.text((x, int(H * y)), text, font=font, fill=fill,
                  stroke_width=max(4, size // 14), stroke_fill="black")
    return card


# ---------------------------------------------------------------- build

def make_video(images, lines, brand, link, output, voice=DEFAULT_VOICE, music=True,
               logo=DEFAULT_LOGO, badge=DEFAULT_BADGE):
    """Build the video; returns the output path."""
    if not images or not lines:
        raise ValueError("need at least one image and one line")
    end_logo = _load_logo(logo, 860)
    corner = _load_logo(badge, 110)
    photos = [Image.open(p).convert("RGB") for p in images]
    backdrops = [_backdrop(p) for p in photos]
    output = os.path.abspath(output)
    os.makedirs(os.path.dirname(output), exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        # 1. Voiceover, one clip per line, with word timings.
        clips, starts, chunks = [], [], []
        cursor = 0.0
        for i, line in enumerate(lines):
            clip = os.path.join(tmp, f"line{i}.mp3")
            words = asyncio.run(_speak(line, voice, clip))
            clips.append(clip)
            starts.append(cursor)
            chunks += _caption_chunks(line, words, cursor)
            cursor += _audio_seconds(clip) + LINE_GAP
        speech_end = cursor
        total = speech_end + END_CARD_SECONDS

        # 2. Shot list: cut every ~SHOT_SECONDS, cycling photos.
        rnd = random.Random(len(lines) * 31 + len(images))
        motions = ["in", "left", "out", "right"]
        shots, t, k = [], 0.0, 0
        while t < speech_end - 0.3:
            length = min(SHOT_SECONDS + rnd.uniform(-0.3, 0.3), speech_end - t)
            shots.append((k % len(photos), length, motions[k % 4]))
            t += length
            k += 1

        # 3. Frames, piped straight into ffmpeg.
        silent = os.path.join(tmp, "silent.mp4")
        writer = imageio_ffmpeg.write_frames(
            silent, (W, H), fps=FPS, codec="libx264", pix_fmt_out="yuv420p",
            macro_block_size=1, ffmpeg_log_level="error")
        writer.send(None)
        frame_no = 0
        for idx, length, motion in shots:
            for frame in _shot_frames(photos[idx], backdrops[idx], length, motion):
                now = frame_no / FPS
                active = [c for c in chunks if c[0][1] <= now]
                if active and now < active[-1][-1][2] + 0.35:
                    _draw_caption(frame, active[-1], now)
                if corner is not None:   # small brand badge, top-left safe zone
                    frame.paste(corner, (48, 140), corner)
                writer.send(frame.tobytes())
                frame_no += 1
        card = _end_card(photos[0], brand, link, end_logo)
        while frame_no < round(total * FPS):
            writer.send(card.tobytes())
            frame_no += 1
        writer.close()

        # 4. Audio: voice clips placed on the timeline, music ducked under it.
        inputs, filters = [], []
        for i, (clip, start) in enumerate(zip(clips, starts)):
            inputs += ["-i", clip]
            ms = int(start * 1000)
            filters.append(f"[{i + 1}:a]aresample=44100,adelay={ms}|{ms}[v{i}]")
        joined = "".join(f"[v{i}]" for i in range(len(clips)))
        filters.append(f"{joined}amix=inputs={len(clips)}:normalize=0,"
                       f"apad,atrim=0:{total:.3f},asplit=2[voice][key]")
        if music:
            bed = os.path.join(tmp, "music.wav")
            _compose_music(total, bed)
            inputs += ["-i", bed]
            m = len(clips) + 1
            filters.append(f"[{m}:a]aresample=44100,volume=0.22[bed]")
            filters.append("[bed][key]sidechaincompress=threshold=0.02:ratio=5:"
                           "attack=20:release=300[ducked]")
            filters.append("[voice][ducked]amix=inputs=2:normalize=0,"
                           f"atrim=0:{total:.3f}[aout]")
        else:
            filters.append("[key]anullsink;[voice]anull[aout]")
        subprocess.run(
            [_ffmpeg(), "-y", "-i", silent, *inputs,
             "-filter_complex", ";".join(filters),
             "-map", "0:v", "-map", "[aout]",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", output],
            check=True, capture_output=True)
    return output


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 1
    with open(argv[1], encoding="utf-8") as fh:
        spec = json.load(fh)
    out = make_video(spec["images"], spec["lines"], spec["brand"], spec.get("link", ""),
                     spec.get("output") or os.path.join(OUTPUT_DIR, "video.mp4"),
                     spec.get("voice", DEFAULT_VOICE), spec.get("music", True),
                     spec.get("logo", DEFAULT_LOGO), spec.get("badge", DEFAULT_BADGE))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
