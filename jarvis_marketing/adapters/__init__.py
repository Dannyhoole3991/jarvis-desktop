"""
Adapters: the only code that talks to the outside world. Each one records what it read (a fetch)
so evidence can be traced back to it.

    supplier     search / details / shipping      live: CJ (jarvis_store.cj)         mock: MockSupplier
    competitors  prices for a keyword             live: public Shopify products.json  mock
    trends       search demand + seasonality      live: UNAVAILABLE (Google Trends API is invite-only)
    shortform    public short-form video signals  live: UNAVAILABLE (TikTok Research API is academic-only)
    reviews      complaints / reviews             live: UNAVAILABLE (no supported source yet)
    shop         our store's products + pages     live: public products.json          mock
    orders       our real orders + attribution    live: Shopify API (read_orders) or Jarvis's browser read
    analytics    per-post platform numbers        live: TikTok Studio (jarvis_v2.advertising)
    publisher    organic posts                    live: TikTok via Jarvis's browser (jarvis_v2.posting)
    paid         paid campaigns                   live: Meta Marketing API (validate + read insights only;
                                                  launch/set_budget ALWAYS REFUSE)    mock: MockMetaMarketing
    video        renders                          live: jarvis_video.py slideshow     mock: MockVideo
    ad_library   existing adverts                 live: Meta Ad Library API           mock: MockAdLibrary
    ad_imports   adverts Danny saved by hand      live and mock: ManualAdImports (TikTok Creative Center has no API)
    tiktok_ads   TikTok Commercial Content API    UNAVAILABLE (needs TikTok's approval)
    shop_admin   Shopify drafts + approved publish live: jarvis_store guarded client  mock: MockShopAdmin

Mock adapters set simulated=True; everything they produce is stored and shown as SIMULATED.
"""


class Unavailable(Exception):
    """This capability isn't available (no official API / not configured). Never filled in with guesses."""


class Disabled(Exception):
    """Switched off in code for this milestone."""


class Adapter:
    name = "adapter"
    simulated = False

    def __init__(self, recorder=None):
        self._recorder = recorder

    def record(self, url, content, ok=True):
        if self._recorder is None:
            return None
        return self._recorder(self.name, url, content, ok, self.simulated)


class PaidAds(Adapter):
    """Paid campaigns. Refuses every call: paid spend is disabled in code until Danny sets limits."""
    name = "paid_ads"

    def launch(self, *args, **kwargs):
        raise Disabled("Paid campaigns are disabled in this milestone.")

    def set_budget(self, *args, **kwargs):
        raise Disabled("Paid budgets are disabled in this milestone.")


class Bundle:
    def __init__(self, **adapters):
        self.__dict__.update(adapters)
        self.simulated = any(getattr(a, "simulated", False) for a in adapters.values() if isinstance(a, Adapter))

    def describe(self):
        out = {}
        for key, a in self.__dict__.items():
            if isinstance(a, Adapter):
                out[key] = {"adapter": a.name, "simulated": a.simulated,
                            "available": not getattr(a, "unavailable_reason", None),
                            "note": getattr(a, "unavailable_reason", None)}
        return out


def build(business, store, business_id, world=None, overrides=None):
    """The adapter set for a business's profile. `overrides` lets tests swap any one adapter."""
    from jarvis_marketing.evidence import record_fetch

    def recorder(adapter, url, content, ok, simulated):
        return record_fetch(store, business_id, adapter, url, content, ok=ok, simulated=simulated)

    if business["profile"] == "mock":
        from . import mock
        world = world or mock.World(seed=business["slug"])
        adapters = mock.bundle(world, recorder)
    else:
        from . import live
        adapters = live.bundle(business, recorder)
    import os
    from jarvis_marketing import config
    from .adlibrary import ManualAdImports
    from .mock import Unsupported
    from .video import scene_analysis
    adapters.setdefault("ad_imports", ManualAdImports(os.path.join(config.data_dir(business["profile"]), "ad_imports.json"),
                                                      recorder, analyse_video=scene_analysis))
    adapters.setdefault("tiktok_ads", Unsupported("tiktok_commercial_content",
                                                  "TikTok's Commercial Content API needs TikTok's approval (not granted). "
                                                  "Creative Center Top Ads have no official API: add ads you see there by hand.", recorder))
    adapters.setdefault("merchant", Unsupported("google_merchant_center",
                                                "Merchant Center isn't connected: needs a Google Cloud OAuth client with the Merchant API "
                                                "(Danny signs in once). Products reach Google through Shopify's Google & YouTube channel.", recorder))
    adapters.setdefault("search_console", Unsupported("google_search_console",
                                                      "Search Console isn't connected: needs the same OAuth sign-in with the Search Console API.", recorder))
    adapters.setdefault("google_shopping", Unsupported("google_shopping_results",
                                                       "Google has no official API for Shopping search results; competitor prices come from "
                                                       "competitor stores Danny lists.", recorder))
    adapters.update(overrides or {})
    adapters.setdefault("paid", PaidAds(recorder))
    return Bundle(**adapters)
