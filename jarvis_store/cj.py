"""
CJdropshipping official API client (read-only: search, product details, shipping quotes).

Jarvis never places or pays for CJ orders from here.
"""

import json
import os
import time

from . import config, creds


class CJError(Exception):
    pass


def _requests_transport(method, url, headers=None, params=None, json_body=None, timeout=30):
    import requests
    response = requests.request(method, url, headers=headers, params=params, json=json_body, timeout=timeout)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"raw": response.text[:500]}


TOKEN_CACHE = os.path.join(os.path.expanduser("~"), ".jarvis_store", "cj_token.bin")


class CJ:
    def __init__(self, transport=None, api_key=None, sleep=time.sleep, token_cache=TOKEN_CACHE):
        self.transport = transport or _requests_transport
        self._api_key = api_key
        self._token = None
        self._last = 0.0
        self._sleep = sleep
        self._token_cache = token_cache

    def _pace(self):
        wait = config.CJ_MIN_INTERVAL - (time.time() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.time()

    def _cached_token(self):
        if not self._token_cache or not os.path.exists(self._token_cache):
            return None
        try:
            with open(self._token_cache, "rb") as f:
                data = json.loads(creds._dpapi(f.read(), protect=False).decode("utf-8"))
            return data["token"] if data.get("expires", 0) > time.time() + 86400 else None
        except Exception:
            return None

    def _store_token(self, token, days=170):
        if not self._token_cache:
            return
        os.makedirs(os.path.dirname(self._token_cache), exist_ok=True)
        blob = creds._dpapi(json.dumps({"token": token, "expires": time.time() + days * 86400}).encode(), protect=True)
        with open(self._token_cache, "wb") as f:
            f.write(blob)

    def _access_token(self):
        if self._token:
            return self._token
        self._token = self._cached_token()
        if self._token:
            return self._token
        key = self._api_key or creds.load().get("cj_api_key")
        if not key:
            raise CJError("No CJ API key stored. Danny needs to run: py -3 -m jarvis_store.setup_credentials")
        self._pace()
        status, body = self.transport("POST", f"{config.CJ_API}/authentication/getAccessToken", json_body={"apiKey": key})
        token = (body.get("data") or {}).get("accessToken") if isinstance(body, dict) else None
        if status != 200 or not token:
            raise CJError(f"CJ refused the API key: {body.get('message') if isinstance(body, dict) else status}")
        self._token = token
        self._store_token(token)
        return token

    def _get(self, path, params):
        self._pace()
        status, body = self.transport("GET", f"{config.CJ_API}/{path}",
                                      headers={"CJ-Access-Token": self._access_token()}, params=params)
        return self._check(status, body)

    def _post(self, path, payload):
        self._pace()
        status, body = self.transport("POST", f"{config.CJ_API}/{path}",
                                      headers={"CJ-Access-Token": self._access_token(),
                                               "Content-Type": "application/json"}, json_body=payload)
        return self._check(status, body)

    @staticmethod
    def _check(status, body):
        if status != 200 or not isinstance(body, dict) or body.get("result") is False or body.get("code") not in (200, "200", None):
            message = body.get("message") if isinstance(body, dict) else ""
            raise CJError(f"CJ API error (HTTP {status}): {message}")
        return body.get("data")

    # ---------------------------------------------------------- public
    def search(self, keyword, size=30, page=1):
        """Products matching `keyword`, most-listed first. Each: id, name, sku, price_usd, image, listed."""
        data = self._get("product/listV2", {"keyWord": keyword, "page": page, "size": size, "orderBy": 1, "sort": "desc"})
        out = []
        for block in (data or {}).get("content", []) or []:
            for p in block.get("productList", []) or []:
                price = str(p.get("nowPrice") or p.get("sellPrice") or "0").split("--")[0].strip()
                try:
                    price_usd = float(price)
                except ValueError:
                    continue
                out.append({"id": str(p.get("id")), "name": p.get("nameEn") or "", "sku": p.get("sku") or p.get("spu") or "",
                            "price_usd": price_usd, "image": p.get("bigImage"), "listed": int(p.get("listedNum") or 0)})
        return out

    def details_by_spu(self, spu):
        """The same as details(), found by CJ product code (the start of our CJ SKUs, e.g. CJJT1538272)."""
        return self._details(self._get("product/query", {"productSku": spu}) or {}, None)

    def details(self, pid):
        return self._details(self._get("product/query", {"pid": pid}) or {}, pid)

    def _details(self, d, pid):
        images = d.get("productImageSet") or []
        if isinstance(images, str):
            images = [u for u in images.split(",") if u]
        variants = []
        for v in d.get("variants") or []:
            variants.append({"vid": v.get("vid"), "sku": v.get("variantSku") or "", "name": v.get("variantNameEn") or "",
                             "key": v.get("variantKey") or "", "price_usd": float(v.get("variantSellPrice") or 0),
                             "weight_g": float(v.get("variantWeight") or 0), "image": v.get("variantImage")})
        return {"pid": d.get("pid") or pid, "name": d.get("productNameEn") or "", "spu": d.get("productSku") or "",
                "description": d.get("description") or "", "option_names": d.get("productKeyEn") or "",
                "images": images, "variants": variants,
                "category": d.get("categoryName") or "", "material": d.get("materialNameEn") or ""}

    def stock(self, vid):
        """Units CJ reports in stock for one variant, summed over its warehouses. None if CJ doesn't say."""
        data = self._get("product/stock/queryByVid", {"vid": vid})
        rows = data if isinstance(data, list) else (data or {}).get("list") or []
        found = [r for r in rows if isinstance(r, dict)]
        if not found:
            return None
        total = 0
        for r in found:
            n = r.get("totalInventoryNum", r.get("storageNum"))
            if n is None:
                return None
            total += int(n)
        return total

    def uk_shipping(self, vid):
        """Shipping options from China to the UK for one unit: [{name, usd, min_days, max_days}], cheapest first."""
        data = self._post("logistic/freightCalculate",
                          {"startCountryCode": "CN", "endCountryCode": "GB", "products": [{"quantity": 1, "vid": vid}]}) or []
        out = []
        for opt in data:
            usd = max(float(opt.get("logisticPrice") or 0), float(opt.get("totalPostageFee") or 0))
            days = [int(x) for x in str(opt.get("logisticAging") or "").replace(" ", "").split("-") if x.isdigit()]
            if not days or usd <= 0:
                continue
            out.append({"name": opt.get("logisticName"), "usd": usd, "min_days": min(days), "max_days": max(days)})
        return sorted(out, key=lambda o: o["usd"])
