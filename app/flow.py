"""
The 6-phase orchestrator.

Phase 1  Validate       inputs + Shopify creds + Mr Scrape reachable + Keepa coverage
Phase 2  Collection     find by title, create if missing
Phase 3  Scrape         Mr Scrape, one ASIN at a time
Phase 4  Upload         productSet upsert-by-handle + Online-Store-only publish + collection
Phase 5  Pricing        Keepa -> blackbox -> price / MRP metafield / weight / barcode / inventory
Phase 6  Done           summary with admin URLs

Failure policy: a bad ASIN is dropped and the run continues. A phase only aborts
the whole job if nothing can proceed.
"""
import asyncio
import re
import time
from typing import Dict, List

from . import keepa as keepa_mod
from . import pricing
from . import siblings as siblings_mod
from .config import settings
from .jobs import Job
from .scraper import ScrapeError, fallback_handle, mr_scrape
from .shopify import ShopifyError, shopify

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def parse_asins(raw: str) -> List[str]:
    """Split on newline / comma / whitespace, dedupe, preserve order."""
    parts = re.split(r"[\s,;]+", (raw or "").strip().upper())
    seen, out = set(), []
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ────────────────────────────────────────────────────────────────
# Phase 1 — Validate
# ────────────────────────────────────────────────────────────────
async def phase1_validate(job: Job, keepa_bytes: bytes) -> Dict[str, dict]:
    job.set_phase(1, "Validate", 2)
    job.log(f"Phase 1 - validating {len(job.asins)} ASIN(s)")

    if not job.asins:
        raise RuntimeError("No ASINs given")
    if len(job.asins) > settings.max_asins:
        raise RuntimeError(
            f"{len(job.asins)} ASINs given, max is {settings.max_asins}"
        )

    bad = [a for a in job.asins if not ASIN_RE.match(a)]
    if bad:
        raise RuntimeError(f"Not valid 10-character ASINs: {', '.join(bad)}")

    if not job.collection.strip():
        raise RuntimeError("Collection name is required")
    if not job.tag.strip():
        raise RuntimeError("Tag is required")

    # Shopify creds
    try:
        shop = await shopify.ping()
        job.log(f"  Shopify OK - {shop.get('name')} ({shop.get('myshopifyDomain')})")
    except ShopifyError as exc:
        raise RuntimeError(f"Shopify credentials failed: {exc}") from exc

    # Mr Scrape
    if await mr_scrape.ping():
        job.log(f"  Mr Scrape OK - {settings.mr_scrape_url}")
    else:
        raise RuntimeError(
            f"Mr Scrape not reachable at {settings.mr_scrape_url} - start it first"
        )

    # Keepa CSV
    try:
        keepa_data = keepa_mod.load_keepa_csv(keepa_bytes)
    except keepa_mod.KeepaError as exc:
        raise RuntimeError(f"Keepa CSV problem: {exc}") from exc
    job.log(f"  Keepa CSV OK - {len(keepa_data)} rows")

    missing = [a for a in job.asins if a not in keepa_data]
    if missing:
        for m in missing:
            job.log(f"  ⚠ {m} not in Keepa CSV — skipped", "warn")
        job.asins = [a for a in job.asins if a in keepa_data]
        if not job.asins:
            raise RuntimeError(
                "No ASINs left — none were found in the Keepa CSV"
            )
        job.log(f"  Continuing with {len(job.asins)} ASIN(s) "
                f"({len(missing)} skipped)")
    else:
        job.log(f"  All {len(job.asins)} ASIN(s) found in Keepa CSV")

    job.set_progress(8)
    return keepa_data


# ────────────────────────────────────────────────────────────────
# Phase 2 — Collection
# ────────────────────────────────────────────────────────────────
async def phase2_collection(job: Job) -> dict:
    job.set_phase(2, "Collection", 10)
    job.log(f"Phase 2 - ensuring smart collection \"{job.collection}\" "
            f"(rule: TAG = \"{job.tag}\")")

    info = await shopify.ensure_collection(job.collection.strip(), job.tag.strip())
    verb = "created" if info.get("created") else "already exists"
    job.log(f"  Collection {verb} - {info['title']}")
    job.log(f"  {info['id']}")
    if info.get("created"):
        job.log("  Published to all sales channels")
    else:
        job.log("  (existing collection left as-is - if it's not smart on this "
                "tag, delete it in Shopify admin and re-run)", "warn")

    job.collection_info = info
    job.set_progress(14)
    return info


# ────────────────────────────────────────────────────────────────
# Phase 3 — Scrape
# ────────────────────────────────────────────────────────────────
async def phase3_scrape(job: Job) -> None:
    job.set_phase(3, "Scrape", 15)
    job.log(f"Phase 3 - scraping {len(job.asins)} ASIN(s) via Mr Scrape")

    total = len(job.asins)
    for i, asin in enumerate(job.asins, 1):
        try:
            product = await mr_scrape.scrape_one(asin, tags=job.tag)
        except ScrapeError as exc:
            job.failures[asin] = f"scrape: {exc}"
            job.log(f"  [{i}/{total}] {asin} FAILED - {exc}", "error")
            job.set_progress(15 + int(30 * i / total))
            continue
        except Exception as exc:  # noqa: BLE001 - keep the batch alive
            job.failures[asin] = f"scrape: {exc}"
            job.log(f"  [{i}/{total}] {asin} FAILED - {exc}", "error")
            job.set_progress(15 + int(30 * i / total))
            continue

        if not product.get("handle"):
            product["handle"] = fallback_handle(product["title"], asin)

        job.scraped[asin] = product
        job.log(
            f"  [{i}/{total}] {asin} - {product['title'][:55]} "
            f"({len(product['images'])} images, {len(product['variants'])} variants)"
        )
        job.set_progress(15 + int(30 * i / total))

    if not job.scraped:
        raise RuntimeError("Every ASIN failed to scrape - nothing to upload")

    if job.failures:
        job.log(f"  {len(job.failures)} ASIN(s) dropped, continuing with "
                f"{len(job.scraped)}", "warn")


# ────────────────────────────────────────────────────────────────
# Phase 4 — Upload
# ────────────────────────────────────────────────────────────────
def _build_product_input(job: Job, asin: str, p: dict) -> dict:
    """
    ProductSetInput. Matches the CSV importer's column semantics:
      Handle, Title, Body (HTML), Vendor, Tags, Published, Variant SKU,
      Variant Grams, Image Src, SEO Title, SEO Description, Status
    """
    sku = f"{settings.sku_prefix}-{asin}"
    title = p["title"]

    files = [
        {
            "originalSource": url,
            "alt": f"{title[:240]} - Image {n}",
            "contentType": "IMAGE",
        }
        for n, url in enumerate(p["images"][:15], 1)
    ]

    weight = float(p.get("weight_g") or 0) or settings.min_weight_g

    variant = {
        "optionValues": [{"optionName": "Title", "name": "Default Title"}],
        "sku": sku,
        "inventoryItem": {
            "tracked": True,
            "measurement": {"weight": {"value": weight, "unit": "GRAMS"}},
        },
        "inventoryPolicy": "CONTINUE",
    }

    product_input = {
        "title": title,
        "handle": p["handle"],
        "descriptionHtml": p.get("body_html") or "",
        "vendor": p.get("vendor") or job.tag,
        "tags": [job.tag.strip()],
        "status": "ACTIVE",
        "productOptions": [
            {"name": "Title", "values": [{"name": "Default Title"}]}
        ],
        "variants": [variant],
        "seo": {
            "title": (p.get("seo_title") or title)[:60],
            "description": (p.get("seo_description")
                            or re.sub(r"<[^>]+>", " ", p.get("body_html") or title))[:160].strip(),
        },
    }
    if files:
        product_input["files"] = files
    return product_input


async def phase4_upload(job: Job) -> None:
    job.set_phase(4, "Upload", 46)
    job.log(f"Phase 4 - uploading {len(job.scraped)} product(s) to Shopify")
    job.log("  Mode: overwrite matching handles = ON, "
            "publish to all sales channels = OFF")
    job.log(f"  (products auto-join collection \"{job.collection_info['title']}\" "
            f"via tag \"{job.tag}\")")

    total = len(job.scraped)

    for i, (asin, p) in enumerate(list(job.scraped.items()), 1):
        handle = p["handle"]
        try:
            existing = await shopify.get_product_by_handle(handle)
            action = "updated" if existing else "created"

            product_input = _build_product_input(job, asin, p)
            try:
                res = await shopify.product_set(product_input,
                                                identifier_handle=handle)
            except ShopifyError as exc:
                # Most common cause: an image URL Shopify refuses. Retry without files.
                if "files" in product_input:
                    job.log(f"  [{i}/{total}] {asin} - image upload rejected, "
                            f"retrying without images", "warn")
                    product_input.pop("files", None)
                    res = await shopify.product_set(product_input,
                                                    identifier_handle=handle)
                else:
                    raise exc

            product = res.get("product")
            if not product:
                raise ShopifyError(f"productSet returned no product: {res.get('userErrors')}")

            for w in res.get("userErrors") or []:
                job.log(f"      warn: {w.get('message')}", "warn")

            variants = (product.get("variants") or {}).get("nodes") or []
            if not variants:
                raise ShopifyError("product has no variants after upload")
            variant = variants[0]

            published = await shopify.publish_online_store(product["id"])
            # No collectionAddProducts call here — the smart collection rule
            # created in Phase 2 picks up this product automatically via its tag.

            job.uploaded[asin] = {
                "asin": asin,
                "product_gid": product["id"],
                "variant_gid": variant["id"],
                "inventory_item_gid": (variant.get("inventoryItem") or {}).get("id"),
                "existing_barcode": (variant.get("barcode") or "").strip(),
                "sku": variant.get("sku") or f"{settings.sku_prefix}-{asin}",
                "handle": product["handle"],
                "title": product["title"],
                "action": action,
            }
            job.log(
                f"  [{i}/{total}] {asin} {action} - {product['title'][:45]}"
                f"{'' if published else '  (publish skipped)'}"
            )

        except Exception as exc:  # noqa: BLE001
            job.failures[asin] = f"upload: {exc}"
            job.log(f"  [{i}/{total}] {asin} UPLOAD FAILED - {exc}", "error")

        job.set_progress(46 + int(29 * i / total))

    if not job.uploaded:
        raise RuntimeError("Every product failed to upload")


# ────────────────────────────────────────────────────────────────
# Phase 5 — Pricing
# ────────────────────────────────────────────────────────────────
async def phase5_pricing(job: Job, keepa_data: Dict[str, dict]) -> None:
    job.set_phase(5, "Pricing", 76)
    job.log(f"Phase 5 - pricing {len(job.uploaded)} product(s)")
    job.log(f"  $1 = Rs {settings.dollar_exchange} | cargo Rs {settings.cargo_markup}/g "
            f"| tax {settings.tax_margin}% x2 | profit {settings.profit_margin}%")

    total = len(job.uploaded)

    for i, (asin, up) in enumerate(list(job.uploaded.items()), 1):
        k = keepa_data.get(asin, {})
        usd = k.get("price_usd")
        override = (getattr(job, "price_overrides", {}) or {}).get(asin)
        if override and float(override) > 0:
            usd = round(float(override), 2)
            job.log(f"    {asin} price override: ${usd} "
                    f"(Keepa was ${k.get('price_usd')})")
        weight = k.get("weight_g") or 0
        in_stock = k.get("in_stock", False)
        barcode = k.get("barcode")

        try:
            # ── DEAD: no Amazon price, or out of stock ──
            if not usd or not in_stock:
                reason = "no Amazon price" if not usd else "out of stock"
                if up.get("inventory_item_gid"):
                    await shopify.set_inventory(up["inventory_item_gid"], 0)
                job.priced[asin] = {
                    **up, "dead": True, "reason": reason,
                    "price": None, "mrp": None, "inventory": 0,
                }
                job.log(f"  [{i}/{total}] {asin} DEAD ({reason}) - "
                        f"inventory 0, price left unchanged", "warn")
                job.set_progress(76 + int(20 * i / total))
                continue

            # ── LIVE: full pricing ──
            calc = pricing.compute(usd, weight)

            write_barcode = None
            if barcode and not up.get("existing_barcode"):
                write_barcode = barcode   # skip-if-exists rule

            await shopify.update_variant(
                product_gid=up["product_gid"],
                variant_gid=up["variant_gid"],
                price=calc["price"],
                weight_g=calc["weight_g"],
                barcode=write_barcode,
            )

            try:
                await shopify.set_variant_mrp(up["variant_gid"], calc["mrp"])
                mrp_ok = True
            except ShopifyError as exc:
                mrp_ok = False
                job.log(f"      MRP metafield failed: {exc}", "warn")

            if up.get("inventory_item_gid"):
                await shopify.enable_inventory_tracking(up["inventory_item_gid"])
                await shopify.set_inventory(up["inventory_item_gid"], 100)

            job.priced[asin] = {
                **up, "dead": False,
                "amazon_usd": calc["amazon_usd"],
                "weight_g": calc["weight_g"],
                "price": calc["price"],
                "mrp": calc["mrp"] if mrp_ok else None,
                "barcode": write_barcode or up.get("existing_barcode") or "",
                "inventory": 100,
            }
            job.log(
                f"  [{i}/{total}] {asin} - ${calc['amazon_usd']} x {calc['weight_g']}g "
                f"-> Rs {calc['price']:,} (MRP Rs {calc['mrp']:,})"
                f"{'' if write_barcode is None else '  barcode ' + write_barcode}"
            )

        except Exception as exc:  # noqa: BLE001
            job.failures[asin] = f"pricing: {exc}"
            job.log(f"  [{i}/{total}] {asin} PRICING FAILED - {exc}", "error")

        job.set_progress(76 + int(20 * i / total))


# ────────────────────────────────────────────────────────────────
# Phase SEO — Mr. Scout pipeline (only when seo_mode is ON)
# Runs AFTER pricing, BEFORE variants.
# ────────────────────────────────────────────────────────────────
async def phase_seo(job: Job) -> None:
    job.set_phase(6, "SEO", 76)
    # BIG-B: skip SEO for products that went DEAD because Keepa had no Amazon price.
    # "out of stock" dead products still get SEO (they can come back in stock).
    no_price = [a for a in job.uploaded
                if (job.priced.get(a) or {}).get("reason") == "no Amazon price"]
    skipped_results = [{"asin": a, "ok": None, "skipped": True,
                        "error": "skipped - no Amazon price"} for a in no_price]
    uploaded = [(a, up) for a, up in job.uploaded.items() if a not in no_price]
    for a in no_price:
        job.log(f"  {a} - SEO skipped (no Amazon price)")
    job.log(f"Phase SEO - generating SEO for {len(uploaded)} product(s)")

    if not uploaded:
        job.log("  Nothing to optimize - skipping SEO", "warn")
        job.seo_results = skipped_results
        return

    try:
        from .seo_scout import ScoutSEO, ScoutSEOError
    except Exception as exc:  # noqa: BLE001
        job.log(f"  SEO engine unavailable: {exc}", "error")
        job.seo_results = []
        return

    job.log("  SEO scraper starting (Chrome uses your logged-in Google/Gemini)")
    job.log("  Note: ~4-6 min per product (Amazon + Google + Lens + competitors + Gemini)")

    scout = ScoutSEO(visible=settings.variant_visible,
                     page_wait=settings.variant_page_wait,
                     logger=job.log)

    results = []
    total = len(uploaded)
    try:
        for i, (asin, up) in enumerate(uploaded, 1):
            job.log(f"  [{i}/{total}] {asin} - {up['title'][:45]}")
            # fetch this product's Shopify images so Lens has an image + we can write alts
            media = await shopify.get_product_media(up["product_gid"])
            image_urls = []
            for m in media:
                # media alt is not the url; get urls via a small query
                pass
            image_urls = await _product_image_urls(up["product_gid"])

            product = {
                "sku": up["sku"],
                "asin": asin,
                "title": up["title"],
                "barcode": (job.priced.get(asin, {}) or {}).get("barcode", "")
                           or up.get("existing_barcode", ""),
                "images": image_urls,
                "category": job.tag,
            }
            try:
                seo = scout.research(product)
            except ScoutSEOError as exc:
                job.log(f"    {asin} SEO failed - {exc}", "warn")
                results.append({"asin": asin, "ok": False, "error": str(exc)})
                job.set_progress(76 + int(18 * i / total))
                continue

            if not seo:
                job.log(f"    {asin} - Gemini produced no usable SEO", "warn")
                results.append({"asin": asin, "ok": False, "error": "no SEO generated"})
                job.set_progress(76 + int(18 * i / total))
                continue

            # write to Shopify (exact main.py mapping)
            try:
                res = await shopify.write_seo(up["product_gid"], seo, image_media=media)
                if res["ok"]:
                    job.log(f"    {asin} - SEO written "
                            f"(title, {sum(1 for n in range(1,6) if seo.get(f'faq_q{n}'))} FAQs, "
                            f"bullets, alts)")
                else:
                    job.log(f"    {asin} - SEO written with warnings: "
                            f"{res['errors'][0]}", "warn")
                results.append({"asin": asin, "ok": res["ok"],
                                "title": seo.get("title", ""),
                                "errors": res.get("errors", [])})
            except Exception as exc:  # noqa: BLE001
                job.log(f"    {asin} SEO write failed - {exc}", "warn")
                results.append({"asin": asin, "ok": False, "error": str(exc)})

            job.set_progress(76 + int(18 * i / total))
    finally:
        scout.close()

    ok = sum(1 for r in results if r.get("ok"))
    job.seo_results = results + skipped_results
    job.log(f"  SEO done - {ok}/{total} product(s) optimized"
            + (f", {len(skipped_results)} skipped (no Amazon price)" if skipped_results else ""))


async def _product_image_urls(product_gid: str) -> list:
    """Fetch a product's image URLs (for Lens + alt writing)."""
    query = """
    query Imgs($id: ID!) {
      product(id: $id) {
        media(first: 30) { nodes { ... on MediaImage { image { url } } } }
      }
    }
    """
    try:
        data = await shopify.graphql(query, {"id": product_gid})
        nodes = ((data.get("product") or {}).get("media") or {}).get("nodes") or []
        return [n["image"]["url"] for n in nodes
                if n.get("image") and n["image"].get("url")]
    except ShopifyError:
        return []


# ────────────────────────────────────────────────────────────────
# Phase 6 — Variant / Sibling groups (only when variant_mode is ON)
# ────────────────────────────────────────────────────────────────
async def phase6_siblings(job: Job) -> None:
    job.set_phase(6, "Variants", 96)
    uploaded_asins = list(job.uploaded.keys())
    job.log(f"Phase 6 - variant grouping ({len(uploaded_asins)} uploaded ASIN(s))")

    if len(uploaded_asins) < 2:
        job.log("  Fewer than 2 products uploaded - nothing to group", "warn")
        job.sibling_groups = []
        return

    try:
        from .variant_scraper import VariantScraper, VariantScrapeError
    except Exception as exc:  # noqa: BLE001
        job.log(f"  Variant scraper unavailable: {exc}", "error")
        job.sibling_groups = []
        return

    job.log("  Variant scraper starting (one Chrome window for the whole phase, "
            "Mr Scrape untouched)")
    scraper = VariantScraper(visible=settings.variant_visible,
                             page_wait=settings.variant_page_wait)

    variant_map: Dict[str, list] = {}
    total = len(uploaded_asins)
    try:
        # ── Pass 1: scrape variants from each ASIN page ──
        for i, asin in enumerate(uploaded_asins, 1):
            try:
                data = scraper.scrape(asin)
            except VariantScrapeError as exc:
                job.log(f"  [{i}/{total}] {asin} scrape failed - {exc}", "warn")
                variant_map[asin] = []
                job.set_progress(96 + int(2 * i / total))
                continue

            if data.get("captcha"):
                job.log(f"  [{i}/{total}] {asin} - CAPTCHA hit, skipping", "warn")
                variant_map[asin] = []
                job.set_progress(96 + int(2 * i / total))
                continue

            vs = data.get("variants") or []
            in_batch = [v for v in vs
                        if v["asin"] != asin and v["asin"] in job.uploaded]
            variant_map[asin] = vs
            job.log(f"  [{i}/{total}] {asin} - {len(vs)} variant(s) on Amazon, "
                    f"{len(in_batch)} in your batch")
            job.set_progress(96 + int(2 * i / total))

        groups = siblings_mod.build_groups(uploaded_asins, variant_map)
        if not groups:
            job.log(f"  No variant groups found - all {len(uploaded_asins)} "
                    f"product(s) solo")
            job.sibling_groups = []
            return

        # ── Pass 2: fetch each member's exact LABEL_V6 label (SAME Chrome) ──
        job.log("  Fetching labels (visiting each ASIN page, same window)...")
        for g in groups:
            for m in g["members"]:
                lbl = scraper.fetch_label(m["asin"])
                if lbl:
                    m["label"] = lbl
                    job.log(f"    {m['asin']}: \"{lbl}\" [V6]")
                else:
                    job.log(f"    {m['asin']}: no label found "
                            f"(keeping group label)", "warn")
    finally:
        scraper.close()

    job.log(f"  Writing sibling metafields for {len(groups)} group(s)...")

    written = []
    for gi, g in enumerate(groups, 1):
        group_name = f"{job.tag.strip()} Group {gi}"
        members = g["members"]
        all_gids = [job.uploaded[m["asin"]]["product_gid"] for m in members]

        member_results = []
        ok = True
        for pos, m in enumerate(members):
            up = job.uploaded[m["asin"]]
            pid = up["product_gid"].rsplit("/", 1)[-1]

            mrp = (job.priced.get(m["asin"], {}) or {}).get("mrp")
            try:
                compare_paise = str(int(round(float(mrp) * 100))) if mrp else "0"
            except (TypeError, ValueError):
                compare_paise = "0"

            # label must never be blank for single_line_text_field
            label = (m.get("label") or "").strip() or m["asin"]

            mf = siblings_mod.build_metafields_for_member(
                pid, all_gids, label, group_name, compare_paise)
            try:
                errs = await shopify.set_metafields(mf)
                if errs:
                    ok = False
                    job.log(f"      {m['asin']} metafield error: "
                            f"{errs[0].get('message')}", "warn")
                else:
                    await shopify.add_tags(up["product_gid"], [group_name])
                    if pos == 0:
                        await shopify.add_tags(up["product_gid"], ["sibling-admin"])
                member_results.append({"asin": m["asin"], "label": m["label"],
                                       "admin": pos == 0})
            except Exception as exc:  # noqa: BLE001
                ok = False
                job.log(f"      {m['asin']} failed: {exc}", "warn")

        written.append({"name": group_name, "members": member_results, "ok": ok})
        status = "OK" if ok else "with errors"
        job.log(f"    {group_name} ({len(members)} products): metafields + tags "
                f"written {status}")

    solo_count = len(uploaded_asins) - sum(len(g["members"]) for g in groups)
    job.sibling_groups = written
    job.log(f"  {len(written)} group(s) formed, {solo_count} solo")
    job.set_progress(99)


# ────────────────────────────────────────────────────────────────
# Summary
# ────────────────────────────────────────────────────────────────
def phase6_summary(job: Job) -> dict:
    created = sum(1 for v in job.uploaded.values() if v["action"] == "created")
    updated = sum(1 for v in job.uploaded.values() if v["action"] == "updated")
    dead = sum(1 for v in job.priced.values() if v.get("dead"))
    live = sum(1 for v in job.priced.values() if not v.get("dead"))

    products = []
    for asin in job.asins:
        up = job.uploaded.get(asin)
        pr = job.priced.get(asin, {})
        if not up:
            products.append({
                "asin": asin, "ok": False,
                "error": job.failures.get(asin, "unknown failure"),
            })
            continue
        numeric_id = up["product_gid"].rsplit("/", 1)[-1]
        products.append({
            "asin": asin,
            "ok": True,
            "sku": up["sku"],
            "title": up["title"],
            "handle": up["handle"],
            "action": up["action"],
            "dead": pr.get("dead", None),
            "reason": pr.get("reason"),
            "amazon_usd": pr.get("amazon_usd"),
            "price": pr.get("price"),
            "mrp": pr.get("mrp"),
            "weight_g": pr.get("weight_g"),
            "barcode": pr.get("barcode"),
            "inventory": pr.get("inventory"),
            "admin_url": f"{settings.admin_base_url}/products/{numeric_id}",
            "store_url": f"https://{settings.shop_domain.replace('.myshopify.com', '')}"
                         f".myshopify.com/products/{up['handle']}",
            "error": job.failures.get(asin),
        })

    return {
        "batch_size": len(job.asins),
        "collection": {
            "title": job.collection_info["title"],
            "gid": job.collection_info["id"],
            "created": job.collection_info.get("created", False),
            "admin_url": f"{settings.admin_base_url}/collections/"
                         f"{job.collection_info['id'].rsplit('/', 1)[-1]}",
        },
        "tag": job.tag,
        "scraped": len(job.scraped),
        "uploaded": {"created": created, "updated": updated},
        "priced": {"live": live, "dead": dead,
                   "failed": len(job.uploaded) - len(job.priced)},
        "failures": job.failures,
        "sibling_groups": getattr(job, "sibling_groups", []),
        "variant_mode": getattr(job, "variant_mode", False),
        "seo_results": getattr(job, "seo_results", []),
        "seo_mode": getattr(job, "seo_mode", False),
        "elapsed_seconds": round(time.time() - job.started_at, 1),
        "products": products,
    }


# ────────────────────────────────────────────────────────────────
# Runner
# ────────────────────────────────────────────────────────────────
async def run_job(job: Job, keepa_bytes: bytes) -> None:
    job.status = "running"
    job.log(f"Starting run - {len(job.asins)} ASIN(s), "
            f"collection \"{job.collection}\", tag \"{job.tag}\"")
    try:
        keepa_data = await phase1_validate(job, keepa_bytes)
        await phase2_collection(job)
        await phase3_scrape(job)
        await phase4_upload(job)
        await phase5_pricing(job, keepa_data)

        if getattr(job, "seo_mode", False):
            try:
                await phase_seo(job)
            except Exception as exc:  # noqa: BLE001 - never let SEO sink a good run
                job.log(f"  SEO error (products already saved): {exc}", "error")
                job.seo_results = []

        if getattr(job, "variant_mode", False):
            try:
                await phase6_siblings(job)
            except Exception as exc:  # noqa: BLE001 - never let Phase 6 sink a good run
                job.log(f"  Phase 6 error (products already saved): {exc}", "error")
                job.sibling_groups = []

        summary = phase6_summary(job)
        job.set_phase(6, "Done", 100)
        sib = summary.get("sibling_groups") or []
        sib_txt = f", {len(sib)} sibling group(s)" if job.variant_mode else ""
        seo_ok = sum(1 for r in (summary.get("seo_results") or []) if r.get("ok"))
        seo_txt = f", {seo_ok} SEO" if job.seo_mode else ""
        job.log(f"Finished in {summary['elapsed_seconds']}s - "
                f"{summary['uploaded']['created']} created, "
                f"{summary['uploaded']['updated']} updated, "
                f"{summary['priced']['live']} priced, "
                f"{summary['priced']['dead']} dead{seo_txt}{sib_txt}")
        job.finish(summary)

    except asyncio.CancelledError:
        job.fail("Run cancelled")
        raise
    except Exception as exc:  # noqa: BLE001
        job.fail(str(exc))
