"""
Jarvis Blog Publisher
======================

Turns the Markdown articles write_article_online() already produces into
a real, live static site, and actually publishes it -- no human step in
the middle. Reuses infrastructure already set up this session rather
than inventing new accounts:

  generated_articles/*.md
        -> rendered to HTML here
        -> committed to a local clone of github.com/Dannyhoole3991/jarvis-blog
        -> git push
        -> Cloudflare Pages (connected to that repo) auto-deploys,
           same push-to-deploy pattern already used for Jarvis Phone -> Render.

This closes the one real gap in the content_article experiment runner:
previously it could only draft an article and ask Danny to find
somewhere to publish it. Now the "publish" step is itself automatic;
the only human-only step left for this business line is signing up for
actual monetization (ads/affiliate), which still can't be done here --
see BLOG_MONETIZATION_NOTE below, queued once via human_action_queue by
the caller, not per article.
"""

import datetime
import os
import re
import subprocess

import markdown as _markdown_lib

HERE = os.path.dirname(os.path.abspath(__file__))
ARTICLES_DIR = os.path.join(HERE, "generated_articles")
BLOG_REPO_DIR = os.path.join(os.path.dirname(HERE), "jarvis-blog-site")
SITE_BASE_URL = "https://jarvis-blog-8gb.pages.dev"  # confirmed live -- Cloudflare Pages' assigned *.pages.dev URL; a custom domain can replace this later without touching this code.

PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    max-width: 720px; margin: 0 auto; padding: 32px 20px 80px 20px;
    line-height: 1.65; font-size: 17px; color: #1a1a1a; background: #fff;
  }}
  @media (prefers-color-scheme: dark) {{ body {{ color: #e8e8e8; background: #121212; }} }}
  h1 {{ font-size: 1.9em; line-height: 1.25; margin-bottom: 0.3em; }}
  h2 {{ font-size: 1.35em; margin-top: 1.6em; }}
  h3 {{ font-size: 1.1em; margin-top: 1.3em; }}
  a {{ color: #2563eb; }}
  @media (prefers-color-scheme: dark) {{ a {{ color: #60a5fa; }} }}
  code {{ background: rgba(127,127,127,0.15); padding: 2px 5px; border-radius: 4px; font-size: 0.9em; }}
  pre {{ background: rgba(127,127,127,0.12); padding: 14px 16px; border-radius: 8px; overflow-x: auto; }}
  pre code {{ background: none; padding: 0; }}
  .meta {{ color: #888; font-size: 0.9em; margin-bottom: 2em; }}
  .back {{ display: inline-block; margin-bottom: 2em; color: #888; text-decoration: none; font-size: 0.9em; }}
  footer {{ margin-top: 4em; padding-top: 1.5em; border-top: 1px solid rgba(127,127,127,0.25); color: #888; font-size: 0.85em; }}
</style>
</head>
<body>
<a class="back" href="/">&larr; All articles</a>
<div class="meta">Published {date}</div>
{content}
<footer>Written and published autonomously by Jarvis.</footer>
</body>
</html>
"""

INDEX_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis Blog</title>
<meta name="description" content="Articles researched and written autonomously by Jarvis.">
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    max-width: 720px; margin: 0 auto; padding: 48px 20px 80px 20px;
    line-height: 1.6; color: #1a1a1a; background: #fff;
  }}
  @media (prefers-color-scheme: dark) {{ body {{ color: #e8e8e8; background: #121212; }} }}
  h1 {{ font-size: 1.8em; }}
  .sub {{ color: #888; margin-top: -0.6em; margin-bottom: 2em; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ padding: 16px 0; border-bottom: 1px solid rgba(127,127,127,0.2); }}
  li a {{ font-size: 1.15em; text-decoration: none; color: inherit; font-weight: 600; }}
  li a:hover {{ text-decoration: underline; }}
  .date {{ color: #888; font-size: 0.85em; }}
</style>
</head>
<body>
<h1>Jarvis Blog</h1>
<div class="sub">Researched and written autonomously.</div>
<ul>
{items}
</ul>
</body>
</html>
"""


def _slugify(text):
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:80] or "article"


def _extract_title(markdown_text):
    for line in markdown_text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return "Untitled"


def _article_files():
    if not os.path.isdir(ARTICLES_DIR):
        return []
    return sorted(
        (f for f in os.listdir(ARTICLES_DIR) if f.endswith(".md")),
    )


def _run_git(*args):
    result = subprocess.run(
        ["git", *args], cwd=BLOG_REPO_DIR, capture_output=True, text=True, timeout=60,
    )
    return result.returncode, result.stdout, result.stderr


def rebuild_and_publish():
    """
    Rebuilds the whole site from every article in generated_articles/ and
    pushes it -- simple full-rebuild rather than incremental, since
    volume is low and this guarantees the live site never drifts from
    what's actually on disk. Returns (ok, message, article_urls dict).
    """
    if not os.path.isdir(BLOG_REPO_DIR):
        return False, f"Blog repo clone not found at {BLOG_REPO_DIR}.", {}

    files = _article_files()
    if not files:
        return False, "No articles to publish yet.", {}

    article_urls = {}
    index_items = []
    for filename in sorted(files, reverse=True):  # newest first on the index
        path = os.path.join(ARTICLES_DIR, filename)
        with open(path, "r", encoding="utf-8") as f:
            md_text = f.read()
        title = _extract_title(md_text)
        slug = _slugify(title)
        html_body = _markdown_lib.markdown(md_text, extensions=["fenced_code", "tables"])
        # Strip the leading <h1> from the body since the template doesn't repeat it separately -- it's already the page <title>/meta.
        html_body = re.sub(r"^\s*<h1>.*?</h1>\s*", "", html_body, count=1, flags=re.DOTALL)

        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
        date_str = mtime.strftime("%-d %B %Y") if os.name != "nt" else mtime.strftime("%d %B %Y")

        page_html = PAGE_TEMPLATE.format(
            title=title,
            description=(re.sub("<[^<]+?>", "", html_body)[:150]).strip(),
            date=date_str,
            content=html_body,
        )
        out_path = os.path.join(BLOG_REPO_DIR, f"{slug}.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(page_html)

        # Cloudflare Pages serves "/slug" directly (redirecting "/slug.html"
        # to it) -- confirmed live -- so link and report the clean form.
        url = f"{SITE_BASE_URL}/{slug}"
        article_urls[filename] = url
        index_items.append(f'<li><a href="/{slug}">{title}</a><div class="date">{date_str}</div></li>')

    index_html = INDEX_TEMPLATE.format(items="\n".join(index_items))
    with open(os.path.join(BLOG_REPO_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)

    code, out, err = _run_git("add", "-A")
    if code != 0:
        return False, f"git add failed: {err}", article_urls

    code, out, err = _run_git("diff", "--cached", "--quiet")
    if code == 0:
        return True, "Nothing changed -- site already up to date.", article_urls

    code, out, err = _run_git("-c", "user.email=jarvis@dj-ai.org", "-c", "user.name=Jarvis",
                               "commit", "-m", f"Publish {len(files)} article(s)")
    if code != 0:
        return False, f"git commit failed: {err}", article_urls

    code, out, err = _run_git("push", "origin", "main")
    if code != 0:
        return False, f"git push failed: {err}", article_urls

    return True, f"Published {len(files)} article(s).", article_urls
