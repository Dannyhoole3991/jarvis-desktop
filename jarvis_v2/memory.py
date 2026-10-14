"""
Jarvis's long-term memory: a folder of plain Markdown Danny can read and edit.

One fact per file, so a single memory can be fixed or deleted by hand. MEMORY.md is
the index every brain sees at the start of a conversation; `recall` searches the rest.
The journal is the handover note between conversations (and between brains).
Nothing here deletes anything.
"""

import datetime
import os
import re

from . import config

INDEX = "MEMORY.md"
JOURNAL = "journal.md"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _dir():
    config.ensure_dirs()
    return config.MEMORY_DIR


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "note"


def remember(title, fact, kind="fact"):
    """Store one durable fact. Same title replaces that fact's file; the index gains one line."""
    if not title.strip() or not fact.strip():
        return "Nothing to remember: a title and the fact are both needed."
    slug = _slug(title)
    path = os.path.join(_dir(), f"{slug}.md")
    existed = os.path.exists(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\ntitle: {title.strip()}\nkind: {kind}\nupdated: {datetime.date.today().isoformat()}\n---\n\n{fact.strip()}\n")
    index = os.path.join(_dir(), INDEX)
    line = f"- [{title.strip()}]({slug}.md) — {fact.strip().splitlines()[0][:100]}\n"
    lines = _read(index).splitlines(keepends=True) if os.path.exists(index) else ["# Jarvis memory\n\n"]
    lines = [l for l in lines if f"]({slug}.md)" not in l] + [line]
    with open(index, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return f"{'Updated' if existed else 'Remembered'}: {title.strip()}"


def index_text():
    path = os.path.join(_dir(), INDEX)
    return _read(path) if os.path.exists(path) else "(no memories yet)"


def recall(query, limit=5):
    """Memories whose text contains any of the query's words, best match first."""
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
    if not words:
        return "Say what to look for."
    scored = []
    for name in os.listdir(_dir()):
        if not name.endswith(".md") or name == INDEX:
            continue
        text = _read(os.path.join(_dir(), name))
        score = sum(text.lower().count(w) for w in words)
        if score:
            scored.append((score, name, text))
    if not scored:
        return f"Nothing remembered about '{query}'."
    scored.sort(reverse=True)
    return "\n\n".join(f"### {name}\n{text.strip()[:1200]}" for _, name, text in scored[:limit])


def write_journal(note):
    """Append a handover note: what was being worked on and where it was left."""
    path = os.path.join(_dir(), JOURNAL)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n## {stamp}\n{note.strip()}\n")
    return "Journal updated."


def recent_journal(chars=2000):
    path = os.path.join(_dir(), JOURNAL)
    if not os.path.exists(path):
        return ""
    return _read(path)[-chars:]
