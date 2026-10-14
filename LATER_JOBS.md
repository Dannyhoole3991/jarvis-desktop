# Jobs for later

## Danny needs to do these himself
- [ ] **Returns address.** Sign up for a UK address that accepts parcels and customer returns. A normal "virtual office" usually only takes letters, so make sure it says it accepts parcels. Example: Warehouse Address, from about £11.99/month: https://warehouseaddress.com/collections/virtual-address
  - When you have it, tell Claude the address.
- [ ] **Redbubble** (redbubble.com, Account Settings). Add:
  - your name & address
  - payment details
  - your mobile number (confirm the code they text you)
- [ ] **AdSense** (adsense.google.com). Click "Tell us about you", then "Enter information", and fill in your name, address and payment profile.
- [ ] **Restart Jarvis** so his "check money" command is switched on.
- [ ] **Jarvis's store keys (about 10 minutes).**
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

## Claude will do these once the above is done
- [ ] Full Jarvis test, after "keys done":
  1. Jarvis adds a new Christmas product as a draft.
  2. Claude checks the photos, price, delivery time, wording and video.
  3. If it passes, Claude makes it live, adds it to the Christmas collection and links it in CJ.
- [ ] Rewrite the refund policy:
  - 14 days to let us know after delivery.
  - Change-of-mind returns: the customer pays postage back to the returns address.
  - Faulty or damaged items: a photo, then a refund or replacement, with no return needed.
  - Remove "[INSERT RETURN ADDRESS]" and the wrong "30 days" from the live policy page.
- [ ] Pick the best 2 of Jarvis's 6 unused designs (in generated_print_on_demand) and upload them to Redbubble, so it has the 5 designs it needs.
