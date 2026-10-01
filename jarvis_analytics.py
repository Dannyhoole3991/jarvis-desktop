"""
Jarvis Analytics
================

Reads real traffic data back from Cloudflare Web Analytics -- the beacon
jarvis_blog.py already injects into every published page -- via
Cloudflare's GraphQL Analytics API, so the business loop's
MEASURE/ANALYSE/SCALE-OR-ABANDON step can see whether anything it
publishes is actually being read, instead of guessing.

Read-only: the Cloudflare API token this uses is scoped to exactly
"Account Analytics: Read" on one account (verified live via the token
summary Cloudflare showed when it was created) -- it cannot change DNS,
Pages, Workers, billing, or anything else.

Configuration is three environment variables, same pattern as
JARVIS_OPENAI_API_KEY elsewhere in this project:
  JARVIS_CF_ANALYTICS_TOKEN  -- the scoped API token
  JARVIS_CF_ACCOUNT_ID       -- Cloudflare account id
  JARVIS_CF_SITE_TAG         -- this Web Analytics site's tag (NOT the
                                 beacon token in jarvis_blog.py -- a
                                 different id, found in the Web
                                 Analytics dashboard's URL for this site)
Not set -> available() is False and every function here returns None,
so the rest of the business loop degrades gracefully rather than
crashing when analytics isn't configured yet.

Deliberately a separate, independently-testable module (no OpenAI
dependency) -- same pattern as jarvis_blog.py and jarvis_business.py.
"""

import datetime
import os
import re

import requests

CF_API_TOKEN = os.environ.get("JARVIS_CF_ANALYTICS_TOKEN", "").strip()
CF_ACCOUNT_ID = os.environ.get("JARVIS_CF_ACCOUNT_ID", "").strip()
CF_SITE_TAG = os.environ.get("JARVIS_CF_SITE_TAG", "").strip()

GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"

_QUERY = """
query($acc: String!, $site: String!, $s: Date!, $e: Date!) {
  viewer {
    accounts(filter: { accountTag: $acc }) {
      rumPageloadEventsAdaptiveGroups(
        filter: { siteTag: $site, date_geq: $s, date_leq: $e }
        limit: 1000
        orderBy: [count_DESC]
      ) {
        count
        sum { visits }
        dimensions { requestPath }
      }
    }
  }
}
"""


def available():
    return bool(CF_API_TOKEN and CF_ACCOUNT_ID and CF_SITE_TAG)


def fetch_pageviews_by_path(days=30):
    """
    Returns {request_path: {"pageviews": int, "visits": int}} for the
    last `days` days, or None if analytics isn't configured or the
    request fails. request_path is the raw path Cloudflare recorded
    (e.g. "/some-article-slug" or "/").
    """
    if not available():
        return None

    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    try:
        resp = requests.post(
            GRAPHQL_URL,
            headers={
                "Authorization": f"Bearer {CF_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "query": _QUERY,
                "variables": {
                    "acc": CF_ACCOUNT_ID,
                    "site": CF_SITE_TAG,
                    "s": start.isoformat(),
                    "e": end.isoformat(),
                },
            },
            timeout=20,
        )
        data = resp.json()
    except Exception as error:
        print("ANALYTICS: Cloudflare GraphQL request failed:", error)
        return None

    if data.get("errors"):
        print("ANALYTICS: Cloudflare GraphQL returned errors:", data["errors"])
        return None

    try:
        groups = data["data"]["viewer"]["accounts"][0]["rumPageloadEventsAdaptiveGroups"]
    except (KeyError, IndexError, TypeError):
        return None

    result = {}
    for group in groups:
        path = (group.get("dimensions") or {}).get("requestPath")
        if not path:
            continue
        result[path] = {
            "pageviews": group.get("count", 0),
            "visits": (group.get("sum") or {}).get("visits", 0),
        }
    return result


def _path_of(url_or_path):
    match = re.match(r"https?://[^/]+(/.*)?", url_or_path)
    if match:
        return match.group(1) or "/"
    return url_or_path


def pageviews_for_url(url, by_path=None, days=30):
    """
    Convenience: how many pageviews this exact published URL (or bare
    path) got in the last `days` days. Pass an already-fetched `by_path`
    dict (from fetch_pageviews_by_path) to check several URLs without
    repeating the API call. Returns None if analytics isn't configured
    or the fetch failed, otherwise an int (0 if the path just has no
    recorded views).
    """
    if by_path is None:
        by_path = fetch_pageviews_by_path(days=days)
    if by_path is None:
        return None
    path = _path_of(url)
    stats = by_path.get(path) or by_path.get(path.rstrip("/")) or {}
    return stats.get("pageviews", 0)
