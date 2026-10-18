"""
Getting Jarvis's print designs ready for Redbubble.

Redbubble's Cloudflare check blocks Jarvis's own (automated) browser, and passing a human check is
never Jarvis's job, so the upload itself happens in Danny's own Edge (Claude does it with him there).
This module does everything before that, so designs don't sit unseen in a folder:
  - cuts the solid black background out (flood-filled in from the edges, so black lines inside the
    art stay), trims and pads it, and keeps it under 9 MB for the browser upload;
  - writes a title / main tag / 14 supporting tags / description into redbubble_ready/listings.json
    with status "ready";
  - keeps ONE line on Danny's to-do list saying how many designs are waiting.
"""

import json
import os
import re
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
READY_DIR = os.path.join(HERE, "generated_print_on_demand", "redbubble_ready")
UPLOAD_DIR = os.path.join(READY_DIR, "up")
LISTINGS = os.path.join(READY_DIR, "listings.json")
MAX_BYTES = 9_000_000
STOP = {"a", "an", "and", "the", "for", "of", "with", "pod", "design", "tee", "print", "art", "set", "pack"}


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:50] or "design"


def load_listings():
    try:
        with open(LISTINGS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_listings(items):
    os.makedirs(READY_DIR, exist_ok=True)
    with open(LISTINGS, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=1, ensure_ascii=False)


def cut_out(src, dst):
    """Black background -> transparent (only the black connected to the edges), trimmed, upload-sized."""
    import numpy as np
    from PIL import Image, ImageFilter
    im = Image.open(src).convert("RGB")
    a = np.asarray(im).astype(np.int32)
    dark = a.max(axis=2) < 38
    h, w = dark.shape
    bg = np.zeros_like(dark)
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            if dark[y, x] and not bg[y, x]:
                bg[y, x] = True; q.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if dark[y, x] and not bg[y, x]:
                bg[y, x] = True; q.append((y, x))
    while q:
        y, x = q.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and dark[ny, nx] and not bg[ny, nx]:
                bg[ny, nx] = True; q.append((ny, nx))
    if bg.mean() < 0.05:                      # no real background (e.g. an all-over pattern): keep as is
        out = im
    else:
        alpha = Image.fromarray(np.where(bg, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2))
        out = im.convert("RGBA"); out.putalpha(alpha)
        box = alpha.point(lambda v: 255 if v > 20 else 0).getbbox()
        if box:
            out = out.crop(box)
        pad = int(max(out.size) * 0.04)
        canvas = Image.new("RGBA", (out.width + 2 * pad, out.height + 2 * pad), (0, 0, 0, 0))
        canvas.paste(out, (pad, pad)); out = canvas
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    for side in (4000, 3600, 3200, 2900, 2600):
        t = out.copy(); t.thumbnail((side, side))
        t.save(dst, optimize=True, compress_level=9)
        if os.path.getsize(dst) <= MAX_BYTES:
            break
    return dst


def suggest_listing(name, prompt=""):
    """A plain, honest title + tags from the design's name and prompt (Danny/Claude can improve them before upload)."""
    title = re.sub(r"\s*\((?:pod|print[^)]*)\)\s*", " ", name, flags=re.I)
    title = re.sub(r"\b(tee|t-shirt|design|pod)\b", "", title, flags=re.I)
    title = re.sub(r"\s{2,}", " ", title).strip(" -–,") or name
    words = [w for w in re.findall(r"[a-z0-9']+", (name + " " + prompt).lower()) if w not in STOP and len(w) > 2]
    tags = []
    for w in words:
        if w not in tags:
            tags.append(w)
    main = tags[0] if tags else "art"
    return {"title": title[:60], "main_tag": main, "tags": tags[1:15],
            "description": f"{title}. An original design, printed on quality products by Redbubble."}


def prepare(design_path, name, prompt=""):
    """Cut out + list one new design as ready for Redbubble. Returns the listing (or the existing one)."""
    items = load_listings()
    for it in items:
        if it.get("source") == design_path:
            return it
    slug = _slug(name)
    dst = cut_out(design_path, os.path.join(UPLOAD_DIR, slug + ".png"))
    item = dict(suggest_listing(name, prompt), file=os.path.relpath(dst, READY_DIR), source=design_path, status="ready")
    items.append(item)
    _save_listings(items)
    return item


def waiting():
    return [it for it in load_listings() if it.get("status") == "ready"]


def mark_uploaded(source_or_file, url):
    items = load_listings()
    for it in items:
        if source_or_file in (it.get("source"), it.get("file")):
            it.update(status="uploaded", url=url)
    _save_listings(items)


def todo_text(count):
    return (f"{count} new print design(s) are ready for Redbubble (backgrounds cleaned, titles and tags written) in "
            "generated_print_on_demand/redbubble_ready. Redbubble blocks Jarvis's browser, so ask Claude to upload them "
            "in your own Edge.")
