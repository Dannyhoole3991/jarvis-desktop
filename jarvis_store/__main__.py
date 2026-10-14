"""
py -3 -m jarvis_store status               -- what's connected, product counts, Jarvis's drafts
py -3 -m jarvis_store add [keyword]        -- research and add ONE everyday product as a DRAFT
py -3 -m jarvis_store add --dry-run [kw]   -- same, but don't touch Shopify (video + review only)
py -3 -m jarvis_store add --christmas [kw] -- a Christmas decoration instead (~£2 profit), tagged christmas
py -3 -m jarvis_store drafts               -- list Jarvis's drafts waiting for review
"""

import sys

from . import creds
from .shopify import Shopify, ShopError


def status_text():
    present = creds.status()
    lines = [f"Shopify API keys: {'set up' if present.get('shopify') else 'not set up yet'}",
             f"CJ API key: {'set up' if present.get('cj') else 'not set up yet'}"]
    if not (present.get("shopify") and present.get("cj")):
        lines.append("(The store itself is live at collectivefinds.co.uk. The missing keys are one of Danny's later jobs: "
                     "without them Jarvis can't read orders or add products through the API. Nothing is down.)")
    if present.get("shopify"):
        try:
            shop = Shopify()
            info = shop.shop_info()
            products = shop.list_products(first=100)
            active = sum(p["status"] == "ACTIVE" for p in products)
            drafts = [p for p in products if p["status"] == "DRAFT" and "jarvis" in p["tags"]]
            lines += [f"Store: {info['name']} ({info['primaryDomain']['url']})",
                      f"Products: {active} live, {len(drafts)} Jarvis draft(s) waiting for review"]
        except ShopError as error:
            lines.append(f"Shopify check failed: {error}")
    return "\n".join(lines)


def drafts_text():
    drafts = [p for p in Shopify().list_products("tag:jarvis status:draft", first=50)]
    if not drafts:
        return "No Jarvis drafts waiting."
    return "\n".join(f"- {p['title']} (£{float(p['priceRangeV2']['minVariantPrice']['amount']):.2f})" for p in drafts)


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "status":
        print(status_text())
    elif cmd == "drafts":
        print(drafts_text())
    elif cmd == "add":
        from .pipeline import add_product
        dry = "--dry-run" in rest
        kind = "christmas" if "--christmas" in rest else "everyday"
        keyword = " ".join(a for a in rest if not a.startswith("--")) or None
        result = add_product(keyword, kind=kind, dry_run=dry)
        print(result)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
