"""
Ad research adapters: where existing adverts come from.

    MetaAdLibrary     live: Meta's official Ad Library API (Graph API /ads_archive). Needs a token from
                      a Meta developer app with identity confirmation. Non-political ads are only
                      returned if they reached the EU, so searches use an EU country (default IE).
    ManualAdImports   live: ads Danny adds from the dashboard (e.g. TikTok Creative Center Top Ads,
                      which has no official API). Stored in the profile's data folder with their URL.
    MockAdLibrary     SIMULATED ads for tests and the demo.

Every ad comes back in one shape (see `normal()`), with what the source actually showed and
nothing more: no spend, clicks or sales are ever filled in. A local video file (an import Danny
saved) is measured with ffmpeg; without one, visual elements are reported as not observable.
"""

import datetime as dt
import hashlib
import json
import os
import random
import re

from . import Adapter, Unavailable

GRAPH_VERSION = os.environ.get("JARVIS_META_GRAPH_VERSION", "v23.0")
AD_FIELDS = ("id,ad_creation_time,ad_creative_bodies,ad_creative_link_captions,ad_creative_link_descriptions,"
             "ad_creative_link_titles,ad_delivery_start_time,ad_delivery_stop_time,ad_snapshot_url,eu_total_reach,"
             "languages,page_id,page_name,publisher_platforms,target_ages,target_gender,target_locations")


def normal(**kw):
    """The one ad shape every source returns."""
    out = {"external_id": None, "source": None, "url": None, "advertiser": None, "bodies": [], "titles": [], "descriptions": [],
           "link_captions": [], "cta": None, "landing_url": None, "start": None, "stop": None, "active": None,
           "platforms": [], "reach": None, "languages": [], "media_type": None, "video_path": None, "video": None,
           "observed_signals": {}, "notes": None}
    out.update({k: v for k, v in kw.items() if k in out})
    return out


def _date(text):
    if not text:
        return None
    return str(text)[:10]


class MetaAdLibrary(Adapter):
    name = "meta_ad_library"

    def __init__(self, recorder=None, token=None, countries=("IE",), transport=None, version=GRAPH_VERSION):
        super().__init__(recorder)
        self._token, self.countries, self.version = token, list(countries), version
        self.transport = transport

    def _get_token(self):
        if self._token is None:
            try:
                from jarvis_store import creds
                self._token = creds.load().get("meta_access_token") or ""
            except Exception:
                self._token = ""
        if not self._token:
            raise Unavailable("No Meta Ad Library access token is stored yet (needs a Meta developer app with "
                              "identity confirmation; save the token with py -3 -m jarvis_store.setup_credentials).")
        return self._token

    def _fetch(self, params):
        if self.transport:
            return self.transport(params)
        import requests
        r = requests.get(f"https://graph.facebook.com/{self.version}/ads_archive", params=params, timeout=30)
        body = r.json() if r.headers.get("content-type", "").startswith(("application/json", "text/javascript")) else {}
        if r.status_code != 200:
            msg = (body.get("error") or {}).get("message") or f"HTTP {r.status_code}"
            raise RuntimeError(f"Meta Ad Library refused: {msg}")
        return body

    def ads(self, keyword, limit=25):
        params = {"search_terms": keyword[:100], "ad_type": "ALL", "ad_reached_countries": json.dumps(self.countries),
                  "media_type": "VIDEO", "ad_active_status": "ALL", "fields": AD_FIELDS, "limit": limit,
                  "access_token": self._get_token()}
        body = self._fetch(params)
        out = []
        for a in body.get("data") or []:
            out.append(normal(external_id=str(a.get("id")), source=self.name, url=a.get("ad_snapshot_url"),
                              advertiser=a.get("page_name"), bodies=a.get("ad_creative_bodies") or [],
                              titles=a.get("ad_creative_link_titles") or [], descriptions=a.get("ad_creative_link_descriptions") or [],
                              link_captions=a.get("ad_creative_link_captions") or [], start=_date(a.get("ad_delivery_start_time")),
                              stop=_date(a.get("ad_delivery_stop_time")), active=None if "ad_delivery_stop_time" not in a else False,
                              platforms=a.get("publisher_platforms") or [], reach=a.get("eu_total_reach"),
                              languages=a.get("languages") or [], media_type="video"))
        safe = f"https://graph.facebook.com/{self.version}/ads_archive?search_terms={keyword[:100]}&ad_reached_countries={','.join(self.countries)}&media_type=VIDEO"
        return out, self.record(safe, out)          # the token is never part of the recorded URL


class ManualAdImports(Adapter):
    """Ads Danny saved by hand (e.g. from TikTok Creative Center Top Ads, which has no official API)."""
    name = "manual_ads"
    SOURCES = ("tiktok_creative_center", "tiktok", "meta_ad_library_web", "instagram", "facebook", "competitor_site", "other")

    def __init__(self, path, recorder=None, analyse_video=None):
        super().__init__(recorder)
        self.path = path
        self.analyse_video = analyse_video

    def _load(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            return json.load(f).get("ads") or []

    def add(self, entry, added_by):
        """Validates and stores one ad Danny saw. Returns the stored entry."""
        url = str(entry.get("url") or "").strip()
        if not re.match(r"^https://[^\s]+$", url):
            raise ValueError("Give the ad's https:// link so the evidence can be traced back.")
        source = entry.get("source") or "other"
        if source not in self.SOURCES:
            raise ValueError(f"source must be one of {self.SOURCES}")
        text = str(entry.get("text") or "").strip()
        hook = str(entry.get("hook") or "").strip()
        if not (text or hook):
            raise ValueError("Copy in at least the opening line (hook) or the ad text.")
        video = str(entry.get("video_path") or "").strip() or None
        if video and not (os.path.isfile(video) and video.lower().endswith((".mp4", ".mov", ".webm"))):
            raise ValueError("The video file path doesn't point to an .mp4/.mov/.webm file on this PC.")
        signals = {k: str(v)[:60] for k, v in (entry.get("observed_signals") or {}).items() if str(v).strip()}
        item = {"id": "M" + hashlib.sha256((url + hook + text).encode()).hexdigest()[:12], "source": source, "url": url,
                "keyword": str(entry.get("keyword") or "").strip().lower(), "advertiser": str(entry.get("advertiser") or "")[:120],
                "hook": hook[:300], "text": text[:3000], "cta": str(entry.get("cta") or "")[:60] or None,
                "landing_url": str(entry.get("landing_url") or "")[:500] or None, "first_seen": _date(entry.get("first_seen")),
                "observed_signals": signals, "video_path": video, "notes": str(entry.get("notes") or "")[:500],
                "added_by": str(added_by), "added_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        items = [i for i in self._load() if i["id"] != item["id"]] + [item]
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ads": items}, f, indent=1)
        os.replace(tmp, self.path)
        return item

    def ads(self, keyword, limit=25):
        words = [w for w in re.split(r"\W+", keyword.lower()) if len(w) > 2]
        out = []
        for i in self._load():
            hay = " ".join([i.get("keyword") or "", i.get("hook") or "", i.get("text") or ""]).lower()
            if i.get("keyword") == keyword.lower() or (words and all(w.rstrip("s") in hay for w in words)):
                bodies = [((i.get("hook") or "") + ("\n" if i.get("hook") and i.get("text") else "") + (i.get("text") or "")).strip()]
                video = None
                if i.get("video_path") and self.analyse_video:
                    try:
                        video = self.analyse_video(i["video_path"])
                    except Exception as error:      # noqa: BLE001 - a broken file means "not observable", never a guess
                        video = {"error": str(error)[:200]}
                out.append(normal(external_id=i["id"], source=f"manual:{i['source']}", url=i["url"], advertiser=i.get("advertiser"),
                                  bodies=bodies, cta=i.get("cta"), landing_url=i.get("landing_url"), start=i.get("first_seen"),
                                  observed_signals=i.get("observed_signals") or {}, video_path=i.get("video_path"), video=video,
                                  media_type="video" if i.get("video_path") else None, notes=i.get("notes")))
        if not out:
            raise Unavailable("No saved ads match this keyword (add ones you've seen from the dashboard).")
        return out[:limit], self.record(f"file://{self.path}#keyword={keyword}", out[:limit])


# ---------------------------------------------------------------- SIMULATED
_HOOKS = [
    ("problem", "Tired of {kw} chaos? This fixes it in seconds."),
    ("curiosity", "I didn't expect this {kw} to change my mornings..."),
    ("demo", "Watch what happens when you use this {kw}."),
    ("benefit", "The easiest way to keep things tidy. No tools needed."),
    ("objection", "Thought every {kw} was the same? Here's the difference."),
    ("social_proof", "Everyone keeps asking where I got this {kw}."),
]
_OFFERS = ["Free UK delivery", "Now £{p}", "Buy 2, save 15%", "", "Limited stock"]


class MockAdLibrary(Adapter):
    name, simulated = "mock_ad_library", True

    def __init__(self, world, recorder=None):
        super().__init__(recorder)
        self.world = world

    def ads(self, keyword, limit=25):
        r = random.Random(hashlib.sha256(f"{self.world.seed}|ads|{keyword}".encode()).hexdigest())
        out = []
        for n in range(r.randint(4, 7)):
            kind, hook = _HOOKS[(n + r.randint(0, 5)) % len(_HOOKS)]
            price = round(r.uniform(9, 22), 2)
            offer = r.choice(_OFFERS).format(p=f"{price:.2f}")
            body = hook.format(kw=keyword) + " " + r.choice(["No more mess.", "Works on any shelf.", "Saves space instantly.",
                                                               "Made from sturdy aluminium."]) + (f" {offer}." if offer else "")
            days = r.choice([3, 9, 21, 45, 90, 160])
            start = dt.date(2026, 10, 1) - dt.timedelta(days=days)
            cuts = sorted(round(r.uniform(0.4, 14.5), 2) for _ in range(r.randint(3, 12)))
            out.append(normal(external_id=f"SIMAD-{keyword.replace(' ', '-')}-{n}", source=self.name,
                              url=f"mock://adlibrary/{keyword.replace(' ', '-')}/{n}", advertiser=f"Mock Store {n % 3 + 1}",
                              bodies=[body], titles=[f"{keyword.title()}"], link_captions=["mock-store.example"],
                              cta=r.choice(["Shop now", "Learn more", "Shop now"]), start=start.isoformat(),
                              stop=None if r.random() < 0.6 else (start + dt.timedelta(days=days)).isoformat(),
                              platforms=["facebook", "instagram"], reach=r.choice([None, 4000, 52000, 310000]),
                              media_type="video", video={"duration_s": 15.0, "cuts": cuts, "has_audio": True, "simulated": True}))
        return out[:limit], self.record(f"mock://adlibrary?q={keyword.replace(' ', '-')}", out[:limit])
