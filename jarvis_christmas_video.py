"""
Jarvis Christmas Video Maker
============================

The classy sibling of jarvis_video.py, for the Christmas range: no
voiceover over the product (the decorations do the talking), slow
cross-faded shots with a gentle push-in, soft falling snow, short
serif captions, and a carol Jarvis arranges itself (celesta melody,
string pad, bass, bells and reverb). Only the end card speaks:
"Shop the Christmas collection at Collective."

Every melody used is a traditional carol in the public domain, and the
audio is synthesised from scratch with numpy, so there is no copyright
problem.

    python jarvis_christmas_video.py spec.json

spec.json:
    {
      "images":   ["p01.jpg", "p02.jpg"],
      "captions": ["The velvet tree bow", "Red, wine or green"],
      "tune":     "carol_of_the_bells" | "silent_night" | "jingle_bells"
                  | "deck_the_halls" | "we_wish_you",
      "output":   "generated_videos/christmas/TIKTOK/1_bow.mp4",
      "link":     "collectivefinds.co.uk",            (optional)
      "end_line": "Shop the Christmas collection at Collective."  (optional)
    }
"""

import asyncio
import json
import os
import random
import subprocess
import sys
import tempfile
import wave

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import jarvis_video as jv

W, H, FPS = jv.W, jv.H, jv.FPS
SR = jv.SAMPLE_RATE
SHOT_SECONDS = 2.7
FADE_SECONDS = 0.6
END_SECONDS = 3.6
GOLD = (222, 188, 112)
SERIF_ITALIC = r"C:\Windows\Fonts\georgiai.ttf"
SERIF = r"C:\Windows\Fonts\georgia.ttf"
DEFAULT_END_LINE = "Shop the Christmas collection at Collective."


def _serif(size, italic=True):
    path = SERIF_ITALIC if italic else SERIF
    return ImageFont.truetype(path, size) if os.path.exists(path) else jv._font(size)


# ---------------------------------------------------------------- music

_NOTE = {"C": -9, "C#": -8, "Db": -8, "D": -7, "D#": -6, "Eb": -6, "E": -5, "F": -4,
         "F#": -3, "Gb": -3, "G": -2, "G#": -1, "Ab": -1, "A": 0, "A#": 1, "Bb": 1, "B": 2}


def hz(name):
    """'A4' -> 440.0; 'Bb3', 'F#5' etc."""
    pitch, octave = name[:-1], int(name[-1])
    return 440.0 * 2 ** ((_NOTE[pitch] + 12 * (octave - 4)) / 12)


def _seq(text):
    """'G4:1.5 A4:.5 R:1' -> [(name|None, beats)]"""
    out = []
    for tok in text.split():
        n, b = tok.split(":")
        out.append((None if n == "R" else n, float(b)))
    return out


# Each tune: tempo (quarter-note bpm), beats per bar, melody, one chord per bar
# (bass note first), and optional counter-melody. All traditional carols.
TUNES = {
    "silent_night": dict(
        bpm=92, bar=3,
        melody=_seq("G4:1.5 A4:.5 G4:1 E4:3 G4:1.5 A4:.5 G4:1 E4:3 D5:2 D5:1 B4:3 "
                    "C5:2 C5:1 G4:3 A4:2 A4:1 C5:1.5 B4:.5 A4:1 G4:1.5 A4:.5 G4:1 E4:3 "
                    "A4:2 A4:1 C5:1.5 B4:.5 A4:1 G4:1.5 A4:.5 G4:1 E4:3 D5:2 D5:1 "
                    "F5:1.5 D5:.5 B4:1 C5:3 E5:3 C5:1 G4:1 E4:1 G4:1.5 F4:.5 D4:1 C4:3 R:3"),
        chords=["C3 C4 E4 G4"] * 4 + ["G2 G3 B3 D4"] * 2 + ["C3 C4 E4 G4"] * 2
        + ["F2 F3 A3 C4"] * 2 + ["C3 C4 E4 G4"] * 2 + ["F2 F3 A3 C4"] * 2
        + ["C3 C4 E4 G4"] * 2 + ["G2 G3 B3 D4"] * 2 + ["C3 C4 E4 G4"] * 3
        + ["G2 G3 B3 F4"] + ["C3 C4 E4 G4"] * 2,
        voice="celesta"),
    "carol_of_the_bells": dict(
        bpm=138, bar=3,
        melody=_seq(" ".join(["Bb4:1 A4:.5 Bb4:.5 G4:1"] * 8)
                    + " G5:.5 G5:.5 G5:.5 F5:.5 Eb5:.5 D5:.5 C5:.5 C5:.5 C5:.5 Bb4:.5 A4:.5 G4:.5"
                    + " " + " ".join(["Bb4:1 A4:.5 Bb4:.5 G4:1"] * 2) + " G4:3"),
        counter=_seq("R:12 " + " ".join(["D5:1 C5:.5 D5:.5 Bb4:1"] * 4)
                     + " R:6 R:6 R:3"),
        chords=["G2 G3 Bb3 D4", "F2 F3 A3 D4", "Eb2 Eb3 G3 Bb3", "D2 D3 F#3 A3"] * 3
        + ["G2 G3 Bb3 D4"],
        voice="musicbox"),
    "jingle_bells": dict(
        bpm=112, bar=4,
        melody=_seq("E5:1 E5:1 E5:2 E5:1 E5:1 E5:2 E5:1 G5:1 C5:1.5 D5:.5 E5:4 "
                    "F5:1 F5:1 F5:1.5 F5:.5 F5:1 E5:1 E5:1 E5:.5 E5:.5 E5:1 D5:1 D5:1 E5:1 "
                    "D5:2 G5:2 E5:1 E5:1 E5:2 E5:1 E5:1 E5:2 E5:1 G5:1 C5:1.5 D5:.5 E5:4 "
                    "F5:1 F5:1 F5:1 F5:1 F5:1 E5:1 E5:1 E5:.5 E5:.5 G5:1 G5:1 F5:1 D5:1 C5:4"),
        chords=["C3 C4 E4 G4"] * 4 + ["F2 F3 A3 C4", "C3 C4 E4 G4", "D3 D4 F#4 A4", "G2 G3 B3 D4"]
        + ["C3 C4 E4 G4"] * 4 + ["F2 F3 A3 C4", "C3 C4 E4 G4", "G2 G3 B3 F4", "C3 C4 E4 G4"],
        voice="musicbox"),
    "deck_the_halls": dict(
        bpm=104, bar=4,
        melody=_seq(" ".join(["C5:1.5 Bb4:.5 A4:1 G4:1 F4:1 G4:1 A4:1 F4:1 "
                              "G4:.5 A4:.5 Bb4:.5 G4:.5 A4:1.5 G4:.5 F4:1 E4:1 F4:2"] * 2)
                    + " G4:1.5 A4:.5 Bb4:1 G4:1 A4:1.5 Bb4:.5 C5:1 G4:1 "
                    "A4:.5 B4:.5 C5:1 D5:.5 E5:.5 F5:1 E5:1 D5:1 C5:2 "
                    "C5:1.5 Bb4:.5 A4:1 G4:1 F4:1 G4:1 A4:1 F4:1 "
                    "D5:.5 D5:.5 D5:.5 D5:.5 C5:1.5 Bb4:.5 A4:1 G4:1 F4:2"),
        chords=["F2 F3 A3 C4", "F2 F3 A3 C4", "C3 C4 E4 Bb4", "F2 F3 A3 C4"] * 2
        + ["C3 C4 E4 G4", "C3 C4 E4 G4", "C3 C4 E4 G4", "C3 C4 E4 G4"]
        + ["F2 F3 A3 C4", "F2 F3 A3 C4", "Bb2 Bb3 D4 F4", "F2 F3 A3 C4"],
        voice="celesta"),
    "we_wish_you": dict(
        bpm=126, bar=3,
        melody=_seq("R:2 D4:1 G4:1 G4:.5 A4:.5 G4:.5 F#4:.5 E4:1 E4:1 E4:1 "
                    "A4:1 A4:.5 B4:.5 A4:.5 G4:.5 F#4:1 D4:1 D4:1 "
                    "B4:1 B4:.5 C5:.5 B4:.5 A4:.5 G4:1 E4:1 D4:.5 D4:.5 E4:1 A4:1 F#4:1 G4:2 R:1"),
        chords=["G2 G3 B3 D4", "G2 G3 B3 D4", "C3 C4 E4 G4", "A2 A3 C#4 G4", "D3 D4 F#4 A4",
                "B2 B3 D#4 F#4", "C3 C4 E4 G4", "D3 D4 F#4 A4", "G2 G3 B3 D4"],
        voice="musicbox"),
}


def _env(n, attack, decay):
    t = np.arange(n) / SR
    return np.minimum(1.0, t / max(attack, 1e-4)) * np.exp(-t * decay)


def _tone(freq, seconds, voice):
    n = int(seconds * SR)
    t = np.arange(n) / SR
    if voice == "musicbox":       # bright tines: slightly inharmonic, quick high decay
        parts = [(1.0, 1.0, 2.2), (0.35, 2.76, 5.0), (0.15, 5.40, 9.0), (0.06, 8.93, 14.0)]
    else:                          # celesta: rounder, mostly harmonic
        parts = [(1.0, 1.0, 1.6), (0.30, 2.0, 3.5), (0.12, 4.0, 7.0), (0.05, 6.0, 10.0)]
    sig = np.zeros(n)
    for amp, mult, dec in parts:
        sig += amp * np.sin(2 * np.pi * freq * mult * t) * np.exp(-t * dec)
    return sig * np.minimum(1.0, t / 0.004)


def _pad(freqs, seconds):
    """Warm string pad: detuned band-limited saws, slow swell."""
    n = int((seconds + 0.8) * SR)
    t = np.arange(n) / SR
    sig = np.zeros(n)
    for f in freqs:
        for det in (-0.004, 0.0, 0.0045):
            ff = f * (1 + det)
            for k in range(1, 7):
                sig += np.sin(2 * np.pi * ff * k * t + k) / (k * 1.6)
    env = np.minimum(1.0, t / 0.45)
    rel = np.clip((seconds + 0.8 - t) / 0.8, 0, 1)
    return sig * env * rel / (len(freqs) * 3 * 2.2)


def _bell(freq, seconds=3.0):
    n = int(seconds * SR)
    t = np.arange(n) / SR
    sig = np.zeros(n)
    for amp, mult in [(0.5, 0.5), (1.0, 1.0), (0.4, 1.19), (0.35, 1.56), (0.3, 2.0), (0.2, 2.51), (0.12, 2.66)]:
        sig += amp * np.sin(2 * np.pi * freq * mult * t) * np.exp(-t * (1.2 + mult))
    return sig * np.minimum(1.0, t / 0.002)


def _reverb(x, seconds=2.4, wet=0.28):
    rng = np.random.default_rng(3)
    n = int(seconds * SR)
    ir = rng.standard_normal(n) * np.exp(-np.arange(n) / SR * 3.0)
    ir[: int(0.02 * SR)] = 0                       # pre-delay
    size = 1 << int(np.ceil(np.log2(len(x) + n)))
    y = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)[: len(x)]
    y = y / (np.abs(y).max() + 1e-9) * np.abs(x).max()
    return (1 - wet) * x + wet * y


def compose_carol(tune_name, seconds, path):
    """Arrange the named carol to `seconds` long and write a mono WAV."""
    tune = TUNES[tune_name]
    beat = 60.0 / tune["bpm"]
    total = int((seconds + 3) * SR)
    mix = np.zeros(total)

    def place(sig, at, vol):
        i = int(at * SR)
        if i >= total:
            return
        j = min(total, i + len(sig))
        mix[i:j] += vol * sig[: j - i]

    def play_line(line, vol, voice):
        t = 0.0
        while t < seconds:
            for note, beats in line:
                if t >= seconds:
                    break
                if note:
                    place(_tone(hz(note), min(beats * beat + 1.2, 3.0), voice), t, vol)
                t += beats * beat

    play_line(tune["melody"], 0.55, tune["voice"])
    if tune.get("counter"):
        play_line(tune["counter"], 0.30, tune["voice"])

    bar_len = tune["bar"] * beat
    t, k = 0.0, 0
    while t < seconds:
        notes = tune["chords"][k % len(tune["chords"])].split()
        bass, upper = hz(notes[0]), [hz(n) for n in notes[1:]]
        place(_pad(upper, bar_len), t, 0.20)
        place(np.sin(2 * np.pi * bass * np.arange(int(bar_len * SR)) / SR)
              * _env(int(bar_len * SR), 0.02, 1.4), t, 0.22)
        if k % 2 == 0:
            place(_bell(hz(notes[1]) * 2), t, 0.07)
        t += bar_len
        k += 1

    out = _reverb(mix)[: int(seconds * SR)]
    fade_in, fade_out = int(0.4 * SR), int(1.2 * SR)
    out[:fade_in] *= np.linspace(0, 1, fade_in)
    out[-fade_out:] *= np.linspace(1, 0, fade_out)
    out = out / (np.abs(out).max() + 1e-9) * 0.85
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes((out * 32767).astype(np.int16).tobytes())


# ---------------------------------------------------------------- frames

def _backdrop(photo):
    bg = jv._backdrop(photo)
    return Image.blend(bg, Image.new("RGB", (W, H), (12, 8, 6)), 0.30)


def _framed(photo, width):
    """Photo with a hairline gold border, sized to `width`."""
    p = photo.resize((width, int(photo.height * width / photo.width)), Image.LANCZOS)
    frame = Image.new("RGB", (p.width + 8, p.height + 8), GOLD)
    frame.paste(p, (4, 4))
    return frame


def _caption_layer(text):
    """Pre-rendered caption (serif italic, soft shadow) as an RGBA layer."""
    font = _serif(76)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    words, lines, cur = text.split(), [], ""
    for w in words:                                  # wrap to the safe width
        trial = (cur + " " + w).strip()
        if probe.textlength(trial, font=font) > W - 160 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    lines.append(cur)
    layer = Image.new("RGBA", (W, 120 * len(lines) + 80), (0, 0, 0, 0))
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    ds, dl = ImageDraw.Draw(shadow), ImageDraw.Draw(layer)
    for i, line in enumerate(lines):
        x = (W - probe.textlength(line, font=font)) / 2
        y = 30 + i * 110
        ds.text((x + 3, y + 4), line, font=font, fill=(0, 0, 0, 200))
        dl.text((x, y), line, font=font, fill=(255, 250, 240, 255))
    shadow = shadow.filter(ImageFilter.GaussianBlur(8))
    rule_y = layer.height - 26                       # thin gold rule under the text
    ImageDraw.Draw(layer).line((W / 2 - 70, rule_y, W / 2 + 70, rule_y), fill=GOLD + (255,), width=3)
    return Image.alpha_composite(shadow, layer)


class _Snow:
    def __init__(self, seed, count=70):
        rnd = random.Random(seed)
        self.flakes = [(rnd.uniform(0, W), rnd.uniform(-H, H), rnd.uniform(40, 110),
                        rnd.uniform(2.0, 6.5), rnd.uniform(0.3, 0.85), rnd.uniform(0, 6.28))
                       for _ in range(count)]

    def draw(self, frame, now):
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        for x, y, speed, r, a, ph in self.flakes:
            yy = (y + speed * now) % (H + 40) - 20
            xx = x + 18 * np.sin(now * 0.8 + ph)
            d.ellipse((xx - r, yy - r, xx + r, yy + r), fill=(255, 255, 255, int(160 * a)))
        return Image.alpha_composite(frame.convert("RGBA"), layer).convert("RGB")


def _shot(photo, bg, p, motion):
    """One frame of a shot at progress p (0..1): slow push in/out."""
    zoom = 1.0 + 0.06 * p if motion == "in" else 1.06 - 0.06 * p
    framed = _framed(photo, int((W - 120) * zoom))
    frame = bg.copy()
    frame.paste(framed, ((W - framed.width) // 2, int(H * 0.42) - framed.height // 2))
    return frame


def _end_card(photo, link):
    card = _backdrop(photo)
    card = Image.blend(card, Image.new("RGB", (W, H), (8, 6, 4)), 0.35)
    logo = jv._load_logo(jv.DEFAULT_LOGO, 760)
    if logo is not None:
        card.paste(logo, ((W - logo.width) // 2, int(H * 0.36) - logo.height // 2), logo)
    d = ImageDraw.Draw(card)
    for text, y, font, fill in [("The Christmas Collection", 0.47, _serif(84), GOLD),
                                ("Shop now", 0.555, _serif(64, italic=False), (255, 250, 240)),
                                (link, 0.61, _serif(60, italic=False), (255, 250, 240))]:
        x = (W - d.textlength(text, font=font)) / 2
        d.text((x, int(H * y)), text, font=font, fill=fill)
    return card


# ---------------------------------------------------------------- build

def make_christmas_video(images, captions, tune, output, link="collectivefinds.co.uk",
                         end_line=DEFAULT_END_LINE):
    photos = [Image.open(p).convert("RGB") for p in images]
    backdrops = [_backdrop(p) for p in photos]
    caps = [_caption_layer(c) for c in captions]
    n_shots = len(photos)
    step = SHOT_SECONDS - FADE_SECONDS
    body = step * n_shots + FADE_SECONDS
    total = body + END_SECONDS
    output = os.path.abspath(output)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    snow = _Snow(len(images) * 7 + len(captions))
    end = _end_card(photos[0], link)

    def frame_at_shot(k, now):
        p = min(1.0, max(0.0, (now - k * step) / SHOT_SECONDS))
        img = _shot(photos[k], backdrops[k], p, "in" if k % 2 == 0 else "out")
        if k < len(caps):
            local = now - k * step
            alpha = min(1.0, max(0.0, (local - 0.25) / 0.45), max(0.0, (SHOT_SECONDS - 0.15 - local) / 0.45))
            if alpha > 0:
                cap = caps[k]
                rgba = img.convert("RGBA")
                faded = cap.copy()
                faded.putalpha(cap.getchannel("A").point(lambda v: int(v * alpha)))
                rgba.alpha_composite(faded, (0, int(H * 0.73)))
                img = rgba.convert("RGB")
        return img

    with tempfile.TemporaryDirectory() as tmp:
        silent = os.path.join(tmp, "silent.mp4")
        writer = jv.imageio_ffmpeg.write_frames(
            silent, (W, H), fps=FPS, codec="libx264", pix_fmt_out="yuv420p",
            macro_block_size=1, ffmpeg_log_level="error", output_params=["-crf", "19"])
        writer.send(None)
        for i in range(round(total * FPS)):
            now = i / FPS
            k = min(n_shots - 1, int(now / step))
            img = frame_at_shot(k, now)
            if k > 0 and now < k * step + FADE_SECONDS:          # cross-fade from previous shot
                img = Image.blend(frame_at_shot(k - 1, now), img, (now - k * step) / FADE_SECONDS)
            if now >= body - FADE_SECONDS:                        # dissolve into the end card
                img = Image.blend(img, end, min(1.0, (now - (body - FADE_SECONDS)) / FADE_SECONDS))
            writer.send(snow.draw(img, now).tobytes())
        writer.close()

        music = os.path.join(tmp, "carol.wav")
        compose_carol(tune, total, music)
        voice = os.path.join(tmp, "end.mp3")
        asyncio.run(jv._speak(end_line, jv.DEFAULT_VOICE, voice))
        start_ms = int((body + 0.35) * 1000)
        filters = (f"[1:a]aresample=44100,volume=0.9[m];"
                   f"[2:a]aresample=44100,adelay={start_ms}|{start_ms},volume=1.25,asplit=2[v][key];"
                   f"[m][key]sidechaincompress=threshold=0.02:ratio=4:attack=30:release=400[duck];"
                   f"[duck][v]amix=inputs=2:normalize=0,atrim=0:{total:.3f}[aout]")
        subprocess.run([jv._ffmpeg(), "-y", "-i", silent, "-i", music, "-i", voice,
                        "-filter_complex", filters, "-map", "0:v", "-map", "[aout]",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", output],
                       check=True, capture_output=True)
    return output


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 1
    with open(argv[1], encoding="utf-8") as fh:
        spec = json.load(fh)
    print(make_christmas_video(spec["images"], spec["captions"], spec["tune"], spec["output"],
                               spec.get("link", "collectivefinds.co.uk"),
                               spec.get("end_line", DEFAULT_END_LINE)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
