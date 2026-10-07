"""
SEO prompt builder + Gemini response parser.
Direct port of Mr. Scout's generator.js (buildPrompt, parseGeminiResponse) —
same sections, same 2000-word cap, same field rules, same truncation.
"""
import json
import re
from typing import Dict


def _get_brand(product: dict, state: dict) -> str:
    amz = (state.get("steps", {}).get("amazonIn") or {}).get("data") or {}
    if amz.get("brand"):
        return amz["brand"]
    for src in (state.get("scrapedData") or {}).values():
        if isinstance(src, dict) and src.get("brand"):
            return src["brand"]
    if product.get("category"):
        return product["category"]
    t = product.get("title") or ""
    return t.split(" ")[0] if t else "Unknown"


def _count_words(s: str) -> int:
    return len([w for w in re.split(r"\s+", s.strip()) if w])


def build_prompt(product: dict, state: dict) -> str:
    brand = _get_brand(product, state)
    images = product.get("images") or []
    image_count = len(images)
    sections = []

    # Amazon
    amz = (state.get("steps", {}).get("amazonIn") or {}).get("data") or {}
    if amz.get("found"):
        desc = amz.get("description") or ""
        if isinstance(desc, list):
            desc = " ".join(desc)
        sections.append(
            "=== AMAZON INDIA PRODUCT DATA ===\n"
            f"Title: {amz.get('title','')}\n"
            f"Brand: {amz.get('brand','')}\n"
            f"Rating: {amz.get('rating','')} ({amz.get('reviewCount','')})\n"
            + ("Bullet Points:\n" + "\n".join("- " + b for b in amz["bullets"]) if amz.get("bullets") else "")
            + ("\nDescription: " + str(desc)[:800] if desc else "")
            + ("\nIngredients: " + amz["ingredients"] if amz.get("ingredients") else "")
            + ("\nHow To Use: " + amz["howToUse"] if amz.get("howToUse") else "")
        )

    # Lens AI
    lens = (state.get("steps", {}).get("googleLens") or {}).get("data") or {}
    if lens.get("aiOverview"):
        sections.append("=== GOOGLE LENS AI OVERVIEW (verified product info from image) ===\n" + lens["aiOverview"])

    # Title SERP
    title_serp = (state.get("serpData") or {}).get("titleSearch")
    if title_serp:
        t = "=== GOOGLE SEARCH PAGE 1 (title search) ===\n"
        if title_serp.get("fullPageText"):
            t += "Raw page content:\n" + title_serp["fullPageText"][:3000] + "\n"
        if title_serp.get("resultTitles"):
            t += "\nResult Titles:\n" + "\n".join("- " + x for x in title_serp["resultTitles"]) + "\n"
        if title_serp.get("paaQuestions"):
            t += "\nQuestions found:\n" + "\n".join("- " + q for q in title_serp["paaQuestions"]) + "\n"
        if title_serp.get("relatedSearches"):
            t += "\nRelated Searches: " + ", ".join(title_serp["relatedSearches"]) + "\n"
        sections.append(t)

    # ASIN SERP (dedup + brand-relevant)
    asin_serp = (state.get("serpData") or {}).get("asinSearch")
    if asin_serp:
        b = _get_brand(product, state).lower()
        title_titles = set(x.lower() for x in (title_serp.get("resultTitles") if title_serp else []) or [])
        uniq = [x for x in (asin_serp.get("resultTitles") or []) if x.lower() not in title_titles]
        raw_rel = asin_serp.get("fullPageText") and b and b in asin_serp["fullPageText"].lower()
        if uniq or raw_rel:
            t = "=== GOOGLE SEARCH PAGE 1 (ASIN search) ===\n"
            if raw_rel and (not (title_serp and title_serp.get("fullPageText"))
                            or asin_serp["fullPageText"][:200] != (title_serp.get("fullPageText") or "")[:200]):
                t += "Raw page content:\n" + asin_serp["fullPageText"][:2000] + "\n"
            if uniq:
                t += "\nAdditional Result Titles:\n" + "\n".join("- " + x for x in uniq) + "\n"
            sections.append(t)

    # PAA
    all_paa = []
    if title_serp and title_serp.get("paaQuestions"):
        all_paa.extend(title_serp["paaQuestions"])
    if asin_serp and asin_serp.get("paaQuestions"):
        for q in asin_serp["paaQuestions"]:
            if q not in all_paa:
                all_paa.append(q)
    if all_paa:
        sections.append("=== PEOPLE ALSO ASK ===\n" + "\n".join("- " + q for q in all_paa))

    # Related
    all_rel = []
    if title_serp and title_serp.get("relatedSearches"):
        all_rel.extend(title_serp["relatedSearches"])
    if asin_serp and asin_serp.get("relatedSearches"):
        for r in asin_serp["relatedSearches"]:
            if r not in all_rel:
                all_rel.append(r)
    if all_rel:
        sections.append("=== RELATED SEARCHES ===\n" + ", ".join(all_rel))

    # Autocomplete
    if state.get("keywords"):
        sections.append("=== GOOGLE AUTOCOMPLETE (India) ===\n" + ", ".join(state["keywords"]))

    # Scraped (sorted by score)
    scraped = state.get("scrapedData") or {}
    if scraped:
        st = "=== TOP RANKING PAGES CONTENT ===\n"
        scores = state.get("scrapedScores") or {}
        for site, data in sorted(scraped.items(), key=lambda kv: scores.get(kv[0], 0), reverse=True):
            if not isinstance(data, dict) or data.get("error") or (not data.get("title") and not data.get("description")):
                continue
            st += "\n--- " + site + " ---\n"
            if data.get("title"):
                st += "Title: " + data["title"] + "\n"
            if data.get("brand"):
                st += "Brand: " + data["brand"] + "\n"
            if data.get("metaDesc"):
                st += "Meta Description: " + data["metaDesc"] + "\n"
            if data.get("description"):
                st += "Description: " + str(data["description"])[:500] + "\n"
            if data.get("ingredients"):
                st += "Ingredients: " + str(data["ingredients"])[:500] + "\n"
            if data.get("howToUse"):
                st += "How To Use: " + str(data["howToUse"])[:300] + "\n"
        sections.append(st)

    raw_data = "\n\n".join(sections)

    # image alt fields
    image_alt_fields = '"image_alt_1": "Brand + product + front view. One commercial keyword."'
    for i in range(2, min(image_count, 8) + 1):
        image_alt_fields += f',\n  "image_alt_{i}": "Different from all previous alt texts."'

    # 2000-word cap (trim rawData from the end)
    MAX_PROMPT_WORDS = 2000
    static_words = 450
    budget = MAX_PROMPT_WORDS - static_words
    if _count_words(raw_data) > budget:
        arr = raw_data.strip().split()
        raw_data = " ".join(arr[:budget]) + "\n[...trimmed to fit 2000-word limit]"

    return f"""You are an SEO content writer for Dropy India (dropy.in), an Indian e-commerce store selling authentic USA-imported beauty, skincare, health, and personal care products.

Below is RAW research data collected from Google Search page 1, Amazon India, Google Lens AI analysis, and top-ranking competitor pages for this product. Use ALL of this data to generate India-optimized SEO content.

PRODUCT: {product.get('title') or 'Unknown'}
BRAND: {brand}
ASIN: {product.get('asin') or 'N/A'}
CATEGORY: {product.get('category') or 'Not specified'}
NUMBER OF IMAGES: {image_count}

{raw_data}

====== GENERATE THESE FIELDS AS JSON ======

{{
  "handle": "Lowercase. Hyphens only. brand-product-variant format. Max 8 words. Include brand and key product identifiers to ensure uniqueness. No special characters. GMC compliant. Example: john-frieda-frizz-ease-dream-curls-spray",
  "title": "Title Case (Capitalize First Letter Of Every Word). 120-150 chars. No shortforms (Ounce not oz, Fluid Ounce not fl oz). Include brand, product type, key attribute, variant, and size.",
  "seo_title": "Title Case. 50-60 chars MAX. Brand first. Most important keywords. GMC compliant: no promotional text, no price, no ALL CAPS.",
  "seo_description": "STRICT 150 characters maximum. Count carefully. Must be UNDER 150 chars. NO brand name. 2-3 key features. NO call-to-action words (no Shop/Buy/Order/Get). GMC compliant. Different from title.",
  "description": "150-300 words. 100% unique. Structure: what it does, who its for, key ingredients/features, why this product. Use keywords from research data naturally. No shipping info. No promotional claims.",
  "bullet_points": "3-5 bullets separated by ||. Format: Benefit: feature explanation. Use COLON (:) between benefit and explanation. Each answers: why should I care?",
  "ingredients": "If research data contains ingredients, use them exactly. If NOT found in research data, generate a generic but realistic ingredient list appropriate for this product category. Mark with [Generic] at the start if generated.",
  "how_to_use": "If research data contains usage instructions, use them. If NOT found, generate basic usage instructions appropriate for this product type.",
  "faq_q1": "Use a real People Also Ask question if available",
  "faq_a1": "2-4 sentences. Direct answer.",
  "faq_q2": "",
  "faq_a2": "",
  "faq_q3": "",
  "faq_a3": "",
  "faq_q4": "Include one India-specific question about authenticity or availability",
  "faq_a4": "Confirm authentic USA import. Mention Dropy sources directly from USA.",
  "faq_q5": "",
  "faq_a5": "",
  {image_alt_fields}
}}

STRICT RULES:
1. NO shortforms: Ounce not oz, Fluid Ounce not fl oz, count not ct, Number not No., Number not No
2. NO promotional claims: no best price, cheapest, #1 selling
3. NO competitor mentions in description or bullets
4. Description must be ORIGINAL -- never copy from any source
5. Google Merchant Center compliant -- all fields
6. Target: Indian consumers buying imported USA products
7. Language: English only
8. Title Case for title field: Capitalize First Letter Of Every Word
9. Bullet format: "Benefit: explanation" using COLON separator
10. SEO description MUST be under 150 characters -- count carefully
11. Trust keywords (authentic, imported, USA sourced) -- ONLY use when product origin is a genuine selling point. Skip for generic commodity products.
12. Handle must be unique, lowercase, hyphens only, GMC compliant

RESPOND WITH ONLY THE JSON. No explanation. No markdown. Start with {{ end with }}."""


def parse_gemini_response(text: str) -> Dict:
    """Direct port of generator.js parseGeminiResponse."""
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.I)
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first == -1 or last == -1:
        return {"success": False, "error": "No JSON found in response"}
    cleaned = cleaned[first:last + 1]
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)  # trailing commas
    try:
        data = json.loads(cleaned)
    except Exception as e:
        return {"success": False, "error": "Invalid JSON: " + str(e)}

    # image alts -> pipe-joined
    alts = []
    for i in range(1, 11):
        v = data.get("image_alt_" + str(i))
        if v:
            alts.append(v.strip())
    if alts:
        data["image_alt_texts"] = "|".join(alts)

    # truncation rules (exact)
    if data.get("title") and len(data["title"]) > 150:
        data["title"] = data["title"][:147] + "..."
    if data.get("seo_description") and len(data["seo_description"]) > 150:
        s = data["seo_description"][:147]
        sp = s.rfind(" ")
        if sp > 120:
            s = s[:sp]
        data["seo_description"] = s
    if data.get("seo_title") and len(data["seo_title"]) > 60:
        s = data["seo_title"][:57]
        sp = s.rfind(" ")
        if sp > 40:
            s = s[:sp]
        data["seo_title"] = s

    return {"success": True, "data": data}


def bullets_to_rich_text(bullet_points_str: str) -> str:
    """
    Convert Scout's '||'-separated bullets into the rich_text_field JSON
    (root -> list -> list-item) that main.py writes to custom.bullet_points.
    """
    items = [b.strip() for b in (bullet_points_str or "").split("||") if b.strip()][:5]
    if not items:
        return ""
    children = [{"type": "list-item", "children": [{"type": "text", "value": it}]} for it in items]
    return json.dumps({
        "type": "root",
        "children": [{"type": "list", "listType": "unordered", "children": children}],
    })
