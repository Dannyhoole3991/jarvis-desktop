"""
Jarvis Store
============

Lets Jarvis run the Collective Shopify store (collectivefinds.co.uk) safely:
research products on CJdropshipping, price them from CJ's real UK shipping
cost, write honest copy, create the product in Shopify as a DRAFT, and make
its TikTok video -- then leave it for Danny to review.

Safety is enforced in code, not left to the model:
  - Shopify access is through the official Admin API only (no clicking
    around the admin), and the client refuses every mutation except
    creating products and editing Jarvis's own DRAFT products.
  - Products are always created as DRAFT; nothing here can publish,
    delete, or touch the theme, menus, settings, payments or orders.
    The live site therefore cannot be taken down by Jarvis.
  - Every write is appended to store_data/audit.jsonl.
  - Credentials are entered by Danny in his own terminal
    (python -m jarvis_store.setup_credentials), encrypted with Windows
    DPAPI outside the project folder, and never logged.
"""
