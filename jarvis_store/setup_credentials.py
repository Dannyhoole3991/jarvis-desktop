"""
Run this yourself in a terminal to give Jarvis access to the store:

    py -3 -m jarvis_store.setup_credentials

Nothing you type is shown or logged. The values are encrypted to your Windows
user and saved outside the Jarvis folder. Press Enter to skip any field you
don't want to change.

1. Shopify: dev.shopify.com -> your app (made for the Collective store) ->
   Settings -> copy the Client ID and Client secret. The app needs only the
   read_products and write_products scopes and must be installed on the store.
2. CJ: CJdropshipping -> My CJ -> Authorization -> API -> generate an API key
   (it looks like CJ1234567@api@xxxxxxxx).
"""

import getpass
import sys

from . import creds


def main():
    print("Jarvis store credentials (encrypted to this Windows user, outside the project folder).")
    print("Press Enter to skip a field.\n")
    updates = {
        "shopify_client_id": getpass.getpass("Shopify app Client ID: ").strip(),
        "shopify_client_secret": getpass.getpass("Shopify app Client secret: ").strip(),
        "cj_api_key": getpass.getpass("CJ API key: ").strip(),
    }
    if not any(updates.values()):
        print("Nothing entered; nothing changed.")
        return 1
    path = creds.save(updates)
    print(f"\nSaved (encrypted) at {path}.")
    print("Present now:", creds.status())
    print("Test it with:  py -3 -m jarvis_store status")
    return 0


if __name__ == "__main__":
    sys.exit(main())
