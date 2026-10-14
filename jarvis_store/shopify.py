"""
Guarded Shopify Admin API client.

Reads are unrestricted. Writes are limited, in code, to:
  productCreate              -- always forced to status DRAFT and tagged "jarvis"
  productVariantsBulkCreate  -- only on a DRAFT product tagged "jarvis"
  productVariantsBulkUpdate  -- only on a DRAFT product tagged "jarvis"
  productUpdate              -- only on a DRAFT product tagged "jarvis", and may
                                never change its status
Any other mutation (delete, publish, theme, menu, settings, orders, ...) is
refused before a request is sent. Nothing in this module can publish a product
or alter anything customers see, so it cannot take the live site down.
"""

import json
import re
import time
from datetime import datetime, timezone

from . import config, creds


class ShopError(Exception):
    pass


class GuardError(ShopError):
    """Raised when a request would break the safety rules. Never retried."""


ALLOWED_MUTATIONS = {"productCreate", "productVariantsBulkCreate", "productVariantsBulkUpdate", "productUpdate"}
DRAFT_ONLY_MUTATIONS = {"productVariantsBulkCreate", "productVariantsBulkUpdate", "productUpdate"}
_FORBIDDEN_WORDS = re.compile(r"(?i)delete|publish|theme|menu|shopUpdate|order|refund|fulfil|discount|"
                              r"collection(?:Create|Update|Delete)|metafieldsSet|webhook|script|stagedUpload")


def _requests_transport(method, url, headers=None, data=None, json_body=None, timeout=30):
    import requests
    response = requests.request(method, url, headers=headers, data=data, json=json_body, timeout=timeout)
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:500]}
    return response.status_code, body


def audit(action, **details):
    """Append one line to the audit log (no secrets ever passed in)."""
    import os
    os.makedirs(config.STORE_DATA, exist_ok=True)
    entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": action, **details}
    with open(config.AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _mutation_names(query):
    """Top-level field names inside a `mutation { ... }` document."""
    body = query[query.index("{") + 1:]
    names, depth, i = [], 0, 0
    while i < len(body):
        ch = body[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0:
            m = re.match(r"\s*(\w+)\s*(?:\(|\{)", body[i:])
            if m and m.group(1) not in ("mutation",):
                names.append(m.group(1))
                i += m.end() - 1
                continue
        i += 1
    return names


class Shopify:
    def __init__(self, transport=None, credentials=None, domain=None, api_version=None):
        self.transport = transport or _requests_transport
        self.domain = domain or config.SHOP_DOMAIN
        self.api_version = api_version or config.SHOP_API_VERSION
        self._creds = credentials
        self._token = None
        self._token_expires = 0.0

    # ---------------------------------------------------------- auth
    def _credentials(self):
        if self._creds is None:
            self._creds = creds.load()
        return self._creds

    def _access_token(self):
        c = self._credentials()
        if c.get("shopify_token"):
            return c["shopify_token"]
        if not (c.get("shopify_client_id") and c.get("shopify_client_secret")):
            raise ShopError("No Shopify credentials stored. Danny needs to run: py -3 -m jarvis_store.setup_credentials")
        if self._token and time.time() < self._token_expires - 300:
            return self._token
        status, body = self.transport(
            "POST", f"https://{self.domain}/admin/oauth/access_token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "client_id": c["shopify_client_id"],
                  "client_secret": c["shopify_client_secret"]})
        if status != 200 or "access_token" not in body:
            raise ShopError(f"Shopify refused the app credentials (HTTP {status}). Check the app is installed on the store.")
        self._token = body["access_token"]
        self._token_expires = time.time() + float(body.get("expires_in", 3600))
        return self._token

    # ---------------------------------------------------------- transport
    def _post(self, query, variables=None):
        url = f"https://{self.domain}/admin/api/{self.api_version}/graphql.json"
        for attempt in range(4):
            status, body = self.transport(
                "POST", url, headers={"X-Shopify-Access-Token": self._access_token(),
                                      "Content-Type": "application/json"},
                json_body={"query": query, "variables": variables or {}})
            throttled = status == 429 or any(
                (e.get("extensions") or {}).get("code") == "THROTTLED" for e in body.get("errors") or [] if isinstance(e, dict))
            if throttled and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            if status != 200:
                raise ShopError(f"Shopify API error HTTP {status}: {str(body)[:300]}")
            if body.get("errors"):
                raise ShopError(f"Shopify GraphQL error: {str(body['errors'])[:300]}")
            return body.get("data") or {}
        raise ShopError("Shopify kept throttling the request.")

    def query(self, query, variables=None):
        if re.match(r"\s*mutation\b", query):
            raise GuardError("Use the guarded write methods for changes.")
        return self._post(query, variables)

    def _mutate(self, query, variables, product_id=None):
        names = _mutation_names(query)
        if not names or any(n not in ALLOWED_MUTATIONS for n in names) or _FORBIDDEN_WORDS.search(
                re.sub(r"(?i)\b(productCreate|productVariantsBulkCreate|productVariantsBulkUpdate|productUpdate)\b", "", query)):
            raise GuardError(f"Refused: only product drafts may be changed (asked for {names}).")
        if any(n in DRAFT_ONLY_MUTATIONS for n in names):
            self._assert_jarvis_draft(product_id)
        statuses = re.findall(r'"status"\s*:\s*"(\w+)"', json.dumps(variables))
        if any(s.upper() != "DRAFT" for s in statuses):
            raise GuardError("Refused: Jarvis may only ever set a product to DRAFT.")
        return self._post(query, variables)

    def _assert_jarvis_draft(self, product_id):
        if not product_id:
            raise GuardError("Refused: no product id given.")
        data = self._post("query($id: ID!) { product(id: $id) { id status tags } }", {"id": product_id})
        product = data.get("product")
        if not product:
            raise GuardError("Refused: product not found.")
        if product["status"] != "DRAFT" or config.JARVIS_TAG not in product.get("tags", []):
            raise GuardError("Refused: Jarvis may only edit his own DRAFT products, never live or Danny's products.")

    # ---------------------------------------------------------- reads
    def shop_info(self):
        return self.query("{ shop { name primaryDomain { url } currencyCode } }")["shop"]

    def list_products(self, query_text="", first=50):
        data = self.query(
            """query($q: String, $n: Int!) { products(first: $n, query: $q, sortKey: UPDATED_AT, reverse: true) {
                 nodes { id title handle status productType tags totalInventory
                         priceRangeV2 { minVariantPrice { amount } maxVariantPrice { amount } } } } }""",
            {"q": query_text or None, "n": first})
        return data["products"]["nodes"]

    def existing_cj_spus(self):
        """CJ product codes already in the store (Jarvis tags his products cj:<spu>; CJ-listed ones carry CJ SKUs)."""
        spus = set()
        for p in self.list_products(first=100):
            for tag in p.get("tags", []):
                if tag.startswith("cj:"):
                    spus.add(tag[3:].upper())
        data = self.query("""{ productVariants(first: 250) { nodes { sku } } }""")
        for v in data["productVariants"]["nodes"]:
            sku = (v.get("sku") or "").upper()
            m = re.match(r"(CJ[A-Z]+\d{7})", sku)
            if m:
                spus.add(m.group(1))
        return spus

    def existing_titles(self):
        return [p["title"] for p in self.list_products(first=100)]

    # ---------------------------------------------------------- guarded writes
    def create_draft_product(self, title, description_html, product_type, tags, options, variants, images):
        """
        options:  [{"name": "Colour", "values": ["Red", "Blue"]}]  (may be empty for one variant)
        variants: [{"options": {"Colour": "Red"}, "price": "9.99", "sku": "CJ..."}]
        images:   [{"url": "https://...", "alt": "..."}]
        Returns {"id", "handle", "admin_url"}. Always DRAFT.
        """
        tags = sorted(set(tags) | {config.JARVIS_TAG, config.REVIEW_TAG})
        product_input = {"title": title, "descriptionHtml": description_html, "vendor": config.VENDOR,
                         "productType": product_type, "tags": tags, "status": "DRAFT"}
        if options:
            product_input["productOptions"] = [{"name": o["name"], "values": [{"name": v} for v in o["values"]]}
                                               for o in options]
        media = [{"originalSource": i["url"], "mediaContentType": "IMAGE", "alt": i.get("alt", title)[:250]}
                 for i in images]
        data = self._mutate(
            """mutation($product: ProductCreateInput!, $media: [CreateMediaInput!]) {
                 productCreate(product: $product, media: $media) {
                   product { id handle status variants(first: 5) { nodes { id } } }
                   userErrors { field message } } }""",
            {"product": product_input, "media": media})
        result = data["productCreate"]
        if result["userErrors"]:
            raise ShopError(f"Shopify rejected the product: {result['userErrors']}")
        product = result["product"]
        if product["status"] != "DRAFT":
            raise ShopError("Safety check failed: product did not come back as DRAFT.")
        pid = product["id"]
        audit("productCreate", product_id=pid, title=title, status="DRAFT", variants=len(variants))

        def vinput(v, with_options):
            out = {"price": str(v["price"]), "inventoryItem": {"sku": v.get("sku", ""), "tracked": False},
                   "inventoryPolicy": "CONTINUE"}
            if with_options:
                out["optionValues"] = [{"optionName": k, "name": val} for k, val in v["options"].items()]
            return out

        if options:
            data = self._mutate(
                """mutation($pid: ID!, $variants: [ProductVariantsBulkInput!]!) {
                     productVariantsBulkCreate(productId: $pid, variants: $variants, strategy: REMOVE_STANDALONE_VARIANT) {
                       productVariants { id } userErrors { field message } } }""",
                {"pid": pid, "variants": [vinput(v, True) for v in variants]}, product_id=pid)
            errors = data["productVariantsBulkCreate"]["userErrors"]
        else:
            default_id = product["variants"]["nodes"][0]["id"]
            v = dict(vinput(variants[0], False), id=default_id)
            data = self._mutate(
                """mutation($pid: ID!, $variants: [ProductVariantsBulkInput!]!) {
                     productVariantsBulkUpdate(productId: $pid, variants: $variants) {
                       productVariants { id } userErrors { field message } } }""",
                {"pid": pid, "variants": [v]}, product_id=pid)
            errors = data["productVariantsBulkUpdate"]["userErrors"]
        if errors:
            audit("variants_failed", product_id=pid, errors=str(errors)[:300])
            raise ShopError(f"Draft created but variants failed: {errors}. It is still a hidden DRAFT.")
        audit("variants_set", product_id=pid, prices=[v["price"] for v in variants])
        numeric = pid.rsplit("/", 1)[-1]
        return {"id": pid, "handle": product["handle"],
                "admin_url": f"https://admin.shopify.com/store/{self.domain.split('.')[0]}/products/{numeric}"}
