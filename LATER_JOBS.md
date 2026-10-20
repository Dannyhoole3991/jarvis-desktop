# Jobs for later

## Danny needs to do these himself
- [x] **Returns address.** Sign up for a UK address that accepts parcels and customer returns. A normal "virtual office" usually only takes letters, so make sure it says it accepts parcels. Example: Warehouse Address, from about £11.99/month: https://warehouseaddress.com/collections/virtual-address — SKIPPED 17 Oct 2026: now £34/month; returns handled by email instead (refund policy updated). Revisit when sales justify it.
  - When you have it, tell Claude the address.
- [x] **Redbubble** (redbubble.com, Account Settings). Add: — DONE 17 Oct 2026 (5 designs, shop set up, phone confirmed; Redbubble review up to 5 business days).
  - your name & address
  - payment details
  - your mobile number (confirm the code they text you)
- [x] **AdSense** (adsense.google.com). Click "Tell us about you", then "Enter information", and fill in your name, address and payment profile. — DONE 17 Oct 2026 (Organisation profile "Daniel Hoole", site under review). Next: enter the PIN from Google's letter when it arrives.
- [x] **Restart Jarvis** so his "check money" command is switched on. — DONE.
- [x] **Jarvis's store keys (about 10 minutes).** — DONE 17 Oct 2026: Shopify app "Jarvis" (read/write products only) + CJ API key "Jarvis", both tested.
  - **Shopify**
    1. Go to dev.shopify.com and click Apps, then Create app. Name it "Jarvis".
    2. Under scopes, tick only read_products and write_products, then Release.
    3. Click Install and choose the Collective store.
    4. Go to Settings and copy the Client ID and Client secret.
  - **CJ**
    1. Go to My CJ, then Authorization, then API, and click Generate.
    2. Copy the key.
  - **Give the keys to Jarvis**
    1. In PowerShell, in the Jarvis folder, run: `py -3 -m jarvis_store.setup_credentials`
    2. Paste the three values when it asks.
    3. Tell Claude "keys done".

- [x] **Other advertising accounts.** Create them when you're ready (Instagram for the shop first, then any others), and log into each one once in Jarvis's browser window. Each one then appears in the Advertising tab. — DONE: Instagram connected 16 Oct 2026.
- [ ] **Marketing department: switch it on (when you're ready).** It runs on SIMULATED data until you do this. Full steps: docs/MARKETING_DEPT_PLAN.md, section 11.
  1. Restart Jarvis, open the Marketing tab, and look around the simulated run (everything there is marked SIMULATED).
  2. To use the laptop: install Tailscale on this PC and the laptop (same account), then in PowerShell in the Jarvis folder run `py -3 -m jarvis_marketing set-password --bind <this PC's Tailscale address>` and restart Jarvis. On the laptop open `http://<that address>:8790`.
  3. Optional: add competitor shop addresses to `jarvis_marketing\data\businesses.json` (Claude will write the file for you).
  4. Optional, for order attribution: add the `read_orders` scope to the "Jarvis" Shopify app (dev.shopify.com > Apps > Jarvis > Versions), release it, approve it in the store, then set `"orders_source": "api"`.
  5. When happy, tell Claude "switch marketing to live". Paid ads stay off until you set limits.
- [ ] **TikTok developer account (optional, later).** This is the safer, official way for Jarvis to post videos. Claude will give you the steps when you want to do it.

## Claude will do these once the above is done
- [ ] Full Jarvis test, after "keys done":
  1. Jarvis adds a new Christmas product as a draft.
  2. Claude checks the photos, price, delivery time, wording and video.
  3. If it passes, Claude makes it live, adds it to the Christmas collection and links it in CJ.
- [x] Rewrite the refund policy: — DONE 17 Oct 2026.
  - 14 days to let us know after delivery.
  - Change-of-mind returns: the customer pays postage back to the returns address.
  - Faulty or damaged items: a photo, then a refund or replacement, with no return needed.
  - Remove "[INSERT RETURN ADDRESS]" and the wrong "30 days" from the live policy page.
- [x] Pick the best 2 of Jarvis's 6 unused designs (in generated_print_on_demand) and upload them to Redbubble, so it has the 5 designs it needs. — DONE 17 Oct 2026 (botanical line art + Plant Mom).

## Christmas hero candidates saved for later (16 Oct 2026)
From Jarvis's shortlist (generated_videos/christmas_hero/SHORTLIST.md). Danny picked the apple rose necklace box first; do these two after it:
- Climbing chimney Santa (CJ F4098D71-A4CC-42AC-837A-CE75A4469B0D, variant Red, CJJJJRSD00167-Red). Amazon UK has it at £9.89-14.99, so price about £16.99 (~£7 profit). CJ sells it as a kids' toy (3+): get UKCA/CE paperwork or skip it.
- USB Christmas ceiling projector (CJ 2103035533571502082, SKU CJTY319325404DW). Amazon UK has one at £8.49; the supplier is new with no reviews.

- [ ] **CJ ticket T202610180809409921** (sent 18 Oct 2026): pre-sale questions on the Retroid Pocket Flip 2 (authenticity, no games, DDP/UK VAT over £135, charger, faulty units). Check for CJ's reply at cjdropshipping.com/mine/ticket before listing it.
