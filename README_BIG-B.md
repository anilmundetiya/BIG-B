# BIG-B — bulk ASIN listing

BIG-B is a separate copy of the ten-asin tool with a bulk front end.
It reads an `ASIN,Brand` CSV of any size, puts every brand into its own
Shopify collection, and runs the **same tested ten-asin pipeline** on 10 ASINs at a time.

- Port **2000** (ten-asin stays on 1999)
- Own Chrome profile (separate Google login from ten-asin)
- Bulk page at `http://localhost:2000` · single-run page at `http://localhost:2000/single`

---

## First-time setup (each PC)

1. Install **Python 3.13** from python.org (not 3.14).
2. Install **Google Chrome**.
3. Make sure **Mr Scrape** is installed and running on this PC (`http://localhost:5003`).
4. Unzip `BIG-B` anywhere, e.g. `C:\Users\<you>\Documents\BIG-B`.
5. Copy these 3 files from `ten-asin\app\` into `BIG-B\app\`:
   - `__init__.py`
   - `siblings.py`
   - `variant_scraper.py`
6. Copy your `.env` from the ten-asin folder into the `BIG-B` folder.
7. Double-click `setup.bat`.
8. Double-click `start.bat`.
9. Run one product with **SEO mode ON** — a Chrome window opens.
10. In that Chrome window, sign into the Google account that has Gemini.
11. Close Chrome. The login is now saved in `BIG-B\chrome_variant_profile`.

Never copy `chrome_variant_profile` between PCs — sign in fresh on each PC.

---

## Running a bulk job

1. Double-click `start.bat`.
2. Drop the `ASIN,Brand` CSV.
3. Drop the Keepa CSV.
4. Set SEO mode / Variant mode.
5. Click **Preview plan**.
6. Check the brands table.
7. Click **Start BIG-B run**.
8. When it finishes, click **Download report CSV**.

### CSV format
```
ASIN,Brand
B07F2M31B3,Swanson
B0D4QNVP2H,Comfytemp
```

### Stopping and resuming
1. Click **Stop after this chunk**.
2. Wait for the status to show `stopped`.
3. Later, open the run from **Recent runs**.
4. Click **Resume**.

If the PC or server crashes, the run shows `interrupted` — open it and click **Resume**.
Finished chunks are never redone; the chunk that was running is re-run (safe — upload overwrites by handle).

---

## Brand → collection rules (same as Dropy Central)

| Situation | What BIG-B does |
|---|---|
| Tag | `brand_to_tag`: lowercase, remove everything except a-z and 0-9 (`Nature's Bounty` → `naturesbounty`) |
| Same brand, different spelling | Merged (`La Roche-Posay` + `LA ROCHE-POSAY`); first spelling = collection name |
| Collection with that TAG rule exists | Reused; products get the collection's own rule tag |
| Collection with that title exists | Reused; products get the collection's own rule tag |
| Title exists but it's a manual collection | Brand skipped (same refusal as Dropy Central) |
| Nothing exists | New smart collection: title = brand as written, rule TAG = tag |
| Blank brand | `Unknown` |
| Brand with no a-z/0-9 characters | Skipped (empty tag) |
| ASIN not in Keepa CSV | Skipped |

Known Dropy Central behaviour kept on purpose: accented letters are dropped
(`L'Oréal` → `loral`), so tags match what Dropy Central already created.

---

## Rules

- Don't run a `/single` job while a bulk run is going (same Chrome profile).
- Don't run ten-asin and BIG-B at the same time unless Mr Scrape can handle two clients.
- Two PCs must never get the same ASINs.
- Keep Variant mode OFF for large runs (siblings are only grouped inside each chunk of 10).
