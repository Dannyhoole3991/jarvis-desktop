"""
Posting videos to TikTok, only when Danny presses "Post to TikTok" on that specific video.

The queue (data/post_queue.json) holds videos Jarvis has prepared with their captions. Posting is
done in Jarvis's own logged-in browser window, and EVERY step is checked rather than assumed:

  1. upload the file (Playwright sets the file input directly; TikTok accepts it)
  2. close TikTok's optional pop-ups, declining the optional extras
  3. paste the words, then type each #tag and pick it from TikTok's dropdown (a pasted tag is only
     text and doesn't count in search); read it all back: it must match word for word, or nothing is posted
  4. press Post, then confirm the video really appears on the account (Studio content list)

A human check, a logout, a caption mismatch or an upload that never finishes stops the post with
the reason. Videos may only come from Jarvis's generated_videos folder. Captions pass the same
claims check as store copy (no fake reviews, discounts or health claims).
"""

import json
import os
import re
import threading
import time

from . import config

QUEUE_FILE = os.path.join(config.DATA_DIR, "post_queue.json")
VIDEO_ROOT = os.path.join(config.REPO_DIR, "generated_videos")
STUDIO = "https://www.tiktok.com/tiktokstudio"
UPLOAD_TIMEOUT = 300
_lock = threading.Lock()


def _load():
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(items):
    os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=1, ensure_ascii=False)


def _update(item_id, **fields):
    with _lock:
        items = _load()
        for it in items:
            if it["id"] == item_id:
                it.update(fields)
        _save(items)


def listing():
    return list(_load())


def get(item_id):
    return next((it for it in _load() if it["id"] == int(item_id)), None)


def _safe_video(path):
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(VIDEO_ROOT) + os.sep) or not real.lower().endswith((".mp4", ".mov")):
        raise ValueError("Videos can only come from Jarvis's generated_videos folder.")
    if not os.path.isfile(real):
        raise ValueError(f"That video doesn't exist: {path}")
    return real


def add(video, caption, title, platform="tiktok", source=""):
    """Puts a prepared video in the Ready-to-post list. Never posts."""
    from jarvis_store import copywriter
    video = _safe_video(video)
    caption = (caption or "").strip()
    if not caption or len(caption) > 2200:
        raise ValueError("The caption must be between 1 and 2200 characters.")
    copywriter.check_claims(caption)
    with _lock:
        items = _load()
        if any(os.path.normcase(it["video"]) == os.path.normcase(video) and it["status"] != "failed" for it in items):
            raise ValueError("That video is already in the list.")
        item = {"id": max([it["id"] for it in items] + [0]) + 1, "platform": platform, "title": title, "video": video,
                "caption": caption, "status": "ready", "created_at": time.time(), "source": source}
        _save(items + [item])
    return item


def seed_christmas(folder=None):
    """Adds the Christmas TikTok videos with their written captions (skips any already listed)."""
    folder = folder or os.path.join(VIDEO_ROOT, "christmas")
    with open(os.path.join(folder, "CHRISTMAS_TIKTOK_CAPTIONS.md"), encoding="utf-8") as f:
        text = f.read()
    sections = re.findall(r"^## (\d+)\. (.+?)\n```\n(.*?)\n```", text, re.M | re.S)
    videos = {int(name.split("_", 1)[0]): os.path.join(folder, "TIKTOK", name)
              for name in os.listdir(os.path.join(folder, "TIKTOK")) if name.endswith(".mp4")}
    added = []
    for num, title, caption in sections:
        video = videos.get(int(num))
        if not video:
            continue
        try:
            added.append(add(video, caption, f"Christmas: {title.strip()}", source="christmas collection"))
        except ValueError:
            continue
    return added


def split_caption(caption):
    """Splits a caption into its words and its hashtags (the lines made only of #tags)."""
    body, tags = [], []
    for line in caption.splitlines():
        words = line.split()
        if words and all(re.fullmatch(r"#\w+", w) for w in words):
            for w in words:
                if w.lower() not in (t.lower() for t in tags):
                    tags.append(w)
        else:
            body.append(line)
    return "\n".join(body).strip(), tags


def _norm(s):
    return " ".join((s or "").split())


def post(item_id, browser=None, sleep=time.sleep):
    """DANNY ONLY (the window's Post button). Posts one queued video and verifies it. Returns the item."""
    item = get(item_id)
    if not item:
        raise ValueError("No such video in the list.")
    if item["status"] not in ("ready", "failed"):
        raise ValueError(f"That video is {item['status'].replace('_', ' ')}.")
    video = _safe_video(item["video"])
    _update(item["id"], status="posting", error=None, note=None, started_at=time.time())
    jb = browser
    if jb is None:
        import jarvis_browser as jb
    try:
        jb.goto(STUDIO + "/upload", settle=5)
        text = jb.read_text(3000)
        if jb._looks_blocked(text):
            raise RuntimeError("TikTok is showing a human check in Jarvis's window. Please complete it there, then press Post again.")
        if "/login" in jb.current()["url"]:
            raise RuntimeError("Jarvis's browser is logged out of TikTok. Log in there, then press Post again.")

        jb._worker.run(lambda page: page.set_input_files("input[type=file]", video), timeout=120)
        deadline = time.time() + UPLOAD_TIMEOUT
        while time.time() < deadline:
            sleep(3)
            text = jb.read_text(6000)
            if "Uploaded" in text and "Description" in text:
                break
            if jb._looks_blocked(text):
                raise RuntimeError("TikTok showed a human check during the upload.")
        else:
            raise RuntimeError("The upload didn't finish within 5 minutes.")

        body, tags = split_caption(item["caption"])

        def fill(page):
            for label in ("Got it", "Cancel"):          # "new features" notice; decline optional auto-checks
                b = page.get_by_role("button", name=label, exact=True)
                if b.count() and b.first.is_visible():
                    b.first.click()
                    sleep(0.8)
            ed = page.locator(".public-DraftEditor-content").first
            ed.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            sleep(0.5)
            # The words go in as a paste (typing scrambles them in TikTok's editor)...
            page.evaluate("""(text) => {
                const el = document.querySelector('.public-DraftEditor-content');
                el.focus();
                const dt = new DataTransfer();
                dt.setData('text/plain', text);
                el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
            }""", body + ("\n" if tags else ""))
            sleep(1.5)
            # ...but a pasted hashtag is just text. Each tag only counts (shows up in search) when it is
            # typed and then picked from TikTok's dropdown, so they go in one at a time.
            page.keyboard.press("Control+End")
            added, missing = [], []
            for tag in tags:
                page.keyboard.type(tag, delay=120)
                choice = None
                for _ in range(10):
                    sleep(0.8)
                    items = page.locator(".hashtag-suggestion-item")
                    for i in range(items.count()):
                        if items.nth(i).locator(".hash-tag-topic").inner_text().strip().lower() == tag.lower():
                            choice = items.nth(i)
                            break
                    if choice is not None:
                        break
                if choice is None:                          # TikTok doesn't know this tag: leave it out
                    for _ in tag:
                        page.keyboard.press("Backspace")
                    missing.append(tag)
                    continue
                choice.click()                              # TikTok adds the space after it
                sleep(1)
                added.append(tag)
            mentions = [m.inner_text().strip() for m in page.locator('.public-DraftEditor-content [data-testid="mentionText"]').all()]
            return {"text": ed.inner_text(), "added": added, "missing": missing, "mentions": mentions}
        got = jb._worker.run(fill, timeout=60 + 15 * len(tags))
        expected = _norm(body + " " + " ".join(got["added"]))
        if _norm(got["text"]) != expected or [m.lower() for m in got["mentions"]] != [t.lower() for t in got["added"]]:
            discard(jb)
            raise RuntimeError("The caption didn't go in exactly as written, so I didn't post it.")
        if got["missing"]:
            _update(item["id"], note="TikTok didn't offer these tags, so they were left out: " + " ".join(got["missing"]))

        def press_post(page):
            page.locator('[data-e2e="post_video_button"]').first.click()
            sleep(3)
            for label in ("Post now", "Post anyway"):     # TikTok sometimes asks to confirm while its checks run
                b = page.get_by_role("button", name=label, exact=True)
                if b.count() and b.first.is_visible():
                    b.first.click()
                    sleep(2)
            return page.url
        jb._worker.run(press_post, timeout=60)

        # Verify: the video must appear on the account with this caption.
        start = _norm(body)[:50]
        for _ in range(12):
            sleep(10)
            jb.goto(STUDIO + "/content", settle=5)
            if start in _norm(jb.read_text(40000)):
                _update(item["id"], status="posted", posted_at=time.time())
                return get(item["id"])
        _update(item["id"], status="check", error="Posted, but I couldn't see it on your TikTok yet. Please check your profile.")
        return get(item["id"])
    except Exception as error:
        _update(item["id"], status="failed", error=str(error)[:300])
        return get(item["id"])


def discard(jb):
    def do(page):
        b = page.locator('[data-e2e="discard_post_button"]')
        if b.count():
            b.first.click()
            time.sleep(1.2)
            confirm = page.get_by_role("button", name="Discard", exact=True)
            if confirm.count():
                confirm.last.click()
    try:
        jb._worker.run(do, timeout=30)
    except Exception:
        pass


def remove(item_id):
    """Takes an unposted video out of the list (the video file itself is kept)."""
    with _lock:
        items = _load()
        item = next((it for it in items if it["id"] == int(item_id)), None)
        if not item:
            raise ValueError("No such video in the list.")
        if item["status"] in ("posting", "posted"):
            raise ValueError("That one is already posted or posting.")
        _save([it for it in items if it["id"] != int(item_id)])
    return "Removed from the list. The video file is still in generated_videos."


def describe():
    items = _load()
    if not items:
        return "Nothing is waiting to be posted."
    ready = [it for it in items if it["status"] == "ready"]
    posted = [it for it in items if it["status"] == "posted"]
    parts = [f"{len(ready)} video(s) ready to post" + (": " + "; ".join(it["title"] for it in ready[:8]) if ready else "") + "."]
    if posted:
        parts.append(f"{len(posted)} posted.")
    failed = [it for it in items if it["status"] == "failed"]
    if failed:
        parts.append("Problems: " + "; ".join(f"{it['title']}: {it.get('error')}" for it in failed[:3]))
    parts.append("Danny posts them with the Post to TikTok button in the Advertising tab.")
    return " ".join(parts)
