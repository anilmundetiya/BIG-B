"""
Shopify Admin API client — standalone (no Dropy Central import).

Key design choice: product upload uses the `productSet` mutation with
`identifier: { handle: ... }`. That is Shopify's native upsert-by-handle and is
exactly what the admin CSV importer's "Overwrite products with matching handles"
checkbox does — create if the handle is new, replace if it exists.

Pricing push mirrors Dropy Central's proven paths:
  - price / weight / barcode -> productVariantsBulkUpdate
  - MRP                      -> metafieldsSet on the VARIANT, custom.mrp (money)
  - inventory                -> inventorySetQuantities (ignoreCompareQuantity)
"""
import json
from typing import Any, Dict, List, Optional

import httpx

from .config import settings


class ShopifyError(Exception):
    pass


def _gid(kind: str, raw: Any) -> str:
    s = str(raw)
    if s.startswith("gid://"):
        return s
    return f"gid://shopify/{kind}/{s.rsplit('/', 1)[-1]}"


class ShopifyClient:
    def __init__(self) -> None:
        self.url = settings.shopify_graphql_url
        self.headers = {
            "X-Shopify-Access-Token": settings.access_token,
            "Content-Type": "application/json",
        }
        self._location_gid: Optional[str] = None
        self._online_store_pub: Optional[str] = None

    # ── Transport ───────────────────────────────────────────────
    async def graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                self.url,
                headers=self.headers,
                json={"query": query, "variables": variables or {}},
            )
        if r.status_code != 200:
            raise ShopifyError(f"HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        if body.get("errors"):
            raise ShopifyError(f"GraphQL errors: {json.dumps(body['errors'])[:400]}")
        return body.get("data") or {}

    # ── Health ──────────────────────────────────────────────────
    async def ping(self) -> dict:
        data = await self.graphql("{ shop { name myshopifyDomain currencyCode } }")
        return data.get("shop") or {}

    async def primary_location(self) -> str:
        if self._location_gid:
            return self._location_gid
        data = await self.graphql(
            "{ locations(first: 1) { edges { node { id name } } } }"
        )
        edges = (data.get("locations") or {}).get("edges") or []
        if not edges:
            raise ShopifyError("No Shopify locations found")
        self._location_gid = edges[0]["node"]["id"]
        return self._location_gid

    async def online_store_publication(self) -> Optional[str]:
        """Publication ID for Online Store only (NOT all sales channels)."""
        if self._online_store_pub:
            return self._online_store_pub
        try:
            data = await self.graphql(
                "{ publications(first: 25) { edges { node { id name } } } }"
            )
            for e in (data.get("publications") or {}).get("edges") or []:
                if str(e["node"].get("name", "")).strip().lower() == "online store":
                    self._online_store_pub = e["node"]["id"]
                    return self._online_store_pub
        except ShopifyError:
            return None
        return None

    # ── Collections ─────────────────────────────────────────────
    async def find_collection_by_title(self, title: str) -> Optional[dict]:
        """Exact (case-insensitive) title match. Uses search, not a full fetch."""
        query = """
        query FindCollection($q: String!) {
          collections(first: 50, query: $q) {
            edges { node { id title handle } }
          }
        }
        """
        safe = title.replace('"', '\\"')
        data = await self.graphql(query, {"q": f'title:"{safe}"'})
        target = title.strip().lower()
        for e in (data.get("collections") or {}).get("edges") or []:
            if str(e["node"]["title"]).strip().lower() == target:
                return e["node"]
        return None

    async def create_collection(self, title: str, tag: str) -> dict:
        """
        Create a SMART collection with rule: TAG equals <tag>.
        Any product carrying that tag auto-appears inside. No manual add needed.
        """
        mutation = """
        mutation CreateCollection($input: CollectionInput!) {
          collectionCreate(input: $input) {
            collection { id title handle }
            userErrors { field message }
          }
        }
        """
        variables = {
            "input": {
                "title": title,
                "ruleSet": {
                    "appliedDisjunctively": False,
                    "rules": [
                        {"column": "TAG", "relation": "EQUALS", "condition": tag}
                    ],
                },
            }
        }
        data = await self.graphql(mutation, variables)
        node = data.get("collectionCreate") or {}
        if node.get("userErrors"):
            raise ShopifyError(f"collectionCreate: {node['userErrors']}")
        coll = node["collection"]
        # Publish it to every available sales channel (matches your Keychron pattern).
        await self.publish_all_channels(coll["id"])
        return coll

    async def ensure_collection(self, title: str, tag: str) -> dict:
        found = await self.find_collection_by_title(title)
        if found:
            return {**found, "created": False}
        made = await self.create_collection(title, tag)
        return {**made, "created": True}

    async def publish_all_channels(self, resource_gid: str) -> int:
        """
        Publish a resource (collection or product) to every publication the
        Shopify store has. Returns how many succeeded. Non-fatal on any failure.
        """
        q = "{ publications(first: 25) { edges { node { id name } } } }"
        try:
            data = await self.graphql(q)
        except ShopifyError:
            return 0
        pub_ids = [e["node"]["id"] for e in
                   (data.get("publications") or {}).get("edges") or []]
        if not pub_ids:
            return 0

        mutation = """
        mutation Publish($id: ID!, $input: [PublicationInput!]!) {
          publishablePublish(id: $id, input: $input) {
            userErrors { field message }
          }
        }
        """
        try:
            data = await self.graphql(mutation, {
                "id": resource_gid,
                "input": [{"publicationId": p} for p in pub_ids],
            })
            errs = (data.get("publishablePublish") or {}).get("userErrors") or []
            return len(pub_ids) - len(errs)
        except ShopifyError:
            return 0

    async def add_to_collection(self, collection_gid: str, product_gids: List[str]) -> None:
        """Additive — does not remove the product's other collections."""
        mutation = """
        mutation AddToCollection($id: ID!, $productIds: [ID!]!) {
          collectionAddProducts(id: $id, productIds: $productIds) {
            collection { id }
            userErrors { field message }
          }
        }
        """
        data = await self.graphql(
            mutation, {"id": collection_gid, "productIds": product_gids}
        )
        node = data.get("collectionAddProducts") or {}
        errs = node.get("userErrors") or []
        # "already in collection" is not a real failure
        real = [e for e in errs if "already" not in str(e.get("message", "")).lower()]
        if real:
            raise ShopifyError(f"collectionAddProducts: {real}")

    # ── Products ────────────────────────────────────────────────
    async def get_product_by_handle(self, handle: str) -> Optional[dict]:
        query = """
        query ByHandle($handle: String!) {
          productByHandle(handle: $handle) {
            id title handle status
            variants(first: 50) {
              nodes {
                id sku barcode price
                inventoryItem { id }
              }
            }
          }
        }
        """
        try:
            data = await self.graphql(query, {"handle": handle})
            return data.get("productByHandle")
        except ShopifyError:
            # productByHandle is removed in some newer versions — fall back to search
            fallback = """
            query ByHandleSearch($q: String!) {
              products(first: 1, query: $q) {
                nodes {
                  id title handle status
                  variants(first: 50) {
                    nodes { id sku barcode price inventoryItem { id } }
                  }
                }
              }
            }
            """
            data = await self.graphql(fallback, {"q": f"handle:{handle}"})
            nodes = (data.get("products") or {}).get("nodes") or []
            return nodes[0] if nodes else None

    async def product_set(self, product_input: dict,
                          identifier_handle: Optional[str] = None) -> dict:
        """
        Upsert a product. With identifier {handle}, this IS the admin CSV
        importer's "Overwrite products with matching handles" behaviour.

        List fields (variants, files) replace what's there. Omitted fields are
        left unchanged.
        """
        mutation = """
        mutation UpsertProduct($input: ProductSetInput!, $identifier: ProductSetIdentifiers, $synchronous: Boolean!) {
          productSet(input: $input, identifier: $identifier, synchronous: $synchronous) {
            product {
              id title handle status
              variants(first: 50) {
                nodes {
                  id sku barcode price
                  inventoryItem { id }
                }
              }
            }
            userErrors { field message code }
          }
        }
        """
        variables: Dict[str, Any] = {"input": product_input, "synchronous": True}
        if identifier_handle:
            variables["identifier"] = {"handle": identifier_handle}

        data = await self.graphql(mutation, variables)
        node = data.get("productSet") or {}
        errs = node.get("userErrors") or []
        if errs and not node.get("product"):
            raise ShopifyError(f"productSet: {errs}")
        return {"product": node.get("product"), "userErrors": errs}

    async def publish_online_store(self, product_gid: str) -> bool:
        """Publish to Online Store ONLY — never all sales channels."""
        pub = await self.online_store_publication()
        if not pub:
            return False
        mutation = """
        mutation Publish($id: ID!, $input: [PublicationInput!]!) {
          publishablePublish(id: $id, input: $input) {
            userErrors { field message }
          }
        }
        """
        try:
            data = await self.graphql(
                mutation, {"id": product_gid, "input": [{"publicationId": pub}]}
            )
            errs = (data.get("publishablePublish") or {}).get("userErrors") or []
            return not errs
        except ShopifyError:
            return False

    # ── Pricing push (mirrors Dropy Central) ────────────────────
    async def update_variant(self, product_gid: str, variant_gid: str,
                             price: Optional[float] = None,
                             weight_g: Optional[float] = None,
                             barcode: Optional[str] = None) -> None:
        variant_input: Dict[str, Any] = {"id": variant_gid}
        if price is not None:
            variant_input["price"] = str(price)
        if weight_g is not None:
            variant_input["inventoryItem"] = {
                "measurement": {"weight": {"value": float(weight_g), "unit": "GRAMS"}}
            }
        if barcode is not None:
            variant_input["barcode"] = str(barcode)

        if len(variant_input) == 1:
            return  # nothing to do

        mutation = """
        mutation UpdateVariant($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            productVariants { id }
            userErrors { field message }
          }
        }
        """
        data = await self.graphql(
            mutation, {"productId": product_gid, "variants": [variant_input]}
        )
        errs = (data.get("productVariantsBulkUpdate") or {}).get("userErrors") or []
        if errs:
            raise ShopifyError("; ".join(e["message"] for e in errs))

    async def set_variant_mrp(self, variant_gid: str, mrp: float,
                              currency: str = "INR") -> None:
        """
        MRP lives in the VARIANT metafield custom.mrp (type: money),
        not in compareAtPrice. Matches Dropy Central's
        bulk_set_variant_mrp_graphql().
        """
        mutation = """
        mutation SetMrp($metafields: [MetafieldsSetInput!]!) {
          metafieldsSet(metafields: $metafields) {
            metafields { id }
            userErrors { field message }
          }
        }
        """
        payload = [{
            "namespace": "custom",
            "key": "mrp",
            "ownerId": variant_gid,
            "type": "money",
            "value": json.dumps({"amount": f"{float(mrp):.2f}",
                                 "currency_code": currency}),
        }]
        data = await self.graphql(mutation, {"metafields": payload})
        errs = (data.get("metafieldsSet") or {}).get("userErrors") or []
        if errs:
            raise ShopifyError("; ".join(e["message"] for e in errs))

    async def set_inventory(self, inventory_item_gid: str, quantity: int) -> None:
        location = await self.primary_location()
        mutation = """
        mutation SetInventory($input: InventorySetQuantitiesInput!) {
          inventorySetQuantities(input: $input) {
            inventoryAdjustmentGroup { id }
            userErrors { field message }
          }
        }
        """
        payload = {
            "ignoreCompareQuantity": True,
            "reason": "correction",
            "name": "available",
            "quantities": [{
                "inventoryItemId": inventory_item_gid,
                "locationId": location,
                "quantity": int(quantity),
            }],
        }
        data = await self.graphql(mutation, {"input": payload})
        errs = (data.get("inventorySetQuantities") or {}).get("userErrors") or []
        if errs:
            raise ShopifyError("; ".join(e["message"] for e in errs))

    async def enable_inventory_tracking(self, inventory_item_gid: str) -> None:
        mutation = """
        mutation Track($id: ID!, $input: InventoryItemInput!) {
          inventoryItemUpdate(id: $id, input: $input) {
            inventoryItem { id tracked }
            userErrors { field message }
          }
        }
        """
        try:
            await self.graphql(mutation, {"id": inventory_item_gid,
                                          "input": {"tracked": True}})
        except ShopifyError:
            pass  # non-fatal


    async def set_metafields(self, metafields: List[dict]) -> List[dict]:
        """
        Generic metafieldsSet. Used by Phase 6 for sibling metafields.
        Returns userErrors (empty list on success). Raises only on transport error.
        """
        if not metafields:
            return []
        mutation = """
        mutation SetMetafields($mf: [MetafieldsSetInput!]!) {
          metafieldsSet(metafields: $mf) {
            metafields { id }
            userErrors { field message }
          }
        }
        """
        data = await self.graphql(mutation, {"mf": metafields})
        return (data.get("metafieldsSet") or {}).get("userErrors") or []

    async def add_tags(self, resource_gid: str, tags: List[str]) -> List[dict]:
        """tagsAdd. Used by Phase 6 for group-name and sibling-admin tags."""
        if not tags:
            return []
        mutation = """
        mutation AddTags($id: ID!, $tags: [String!]!) {
          tagsAdd(id: $id, tags: $tags) {
            node { id }
            userErrors { field message }
          }
        }
        """
        data = await self.graphql(mutation, {"id": resource_gid, "tags": tags})
        return (data.get("tagsAdd") or {}).get("userErrors") or []


    async def write_seo(self, product_gid: str, seo: dict,
                        image_media: Optional[List[dict]] = None) -> dict:
        """
        Write SEO to Shopify — exact port of main.py update_product_seo():
          productUpdate (title/handle/descriptionHtml/seo.title/seo.description)
          + metafieldsSet (ingredients/how_to_use/f_q_question_N/f_q_answer_N)
          + bullet_points as rich_text_field (separate call)
        `seo` uses Scout field names: title, handle, description, seo_title,
        seo_description, ingredients, how_to_use, faq_q1..5, faq_a1..5,
        bullet_points (rich_text JSON string).
        Returns {"ok": bool, "errors": [...]}.
        """
        from . import seo_prompt as _sp
        errors = []

        # ── STEP 1: product-level ──
        product_input = {"id": product_gid}
        if seo.get("title"):
            product_input["title"] = seo["title"]
        if seo.get("handle"):
            product_input["handle"] = seo["handle"]
        if seo.get("description"):
            product_input["descriptionHtml"] = seo["description"]
        seo_block = {}
        if seo.get("seo_title") is not None and seo.get("seo_title") != "":
            seo_block["title"] = seo["seo_title"]
        if seo.get("seo_description") is not None and seo.get("seo_description") != "":
            seo_block["description"] = seo["seo_description"]
        if seo_block:
            product_input["seo"] = seo_block

        mutation = """
        mutation productUpdate($input: ProductInput!) {
          productUpdate(input: $input) {
            product { id title handle }
            userErrors { field message }
          }
        }
        """
        try:
            data = await self.graphql(mutation, {"input": product_input})
            errs = (data.get("productUpdate") or {}).get("userErrors") or []
            if errs:
                errors.append(f"product: {errs[0].get('message')}")
        except ShopifyError as exc:
            errors.append(f"product: {exc}")

        # ── STEP 2: metafields (exact keys/types from main.py) ──
        mfs = []
        if seo.get("ingredients"):
            mfs.append({"namespace": "custom", "key": "ingredients",
                        "value": seo["ingredients"], "type": "multi_line_text_field"})
        if seo.get("how_to_use"):
            mfs.append({"namespace": "custom", "key": "how_to_use",
                        "value": seo["how_to_use"], "type": "multi_line_text_field"})
        for n in range(1, 6):
            q = seo.get(f"faq_q{n}")
            a = seo.get(f"faq_a{n}")
            if q is not None and q != "":
                mfs.append({"namespace": "custom", "key": f"f_q_question_{n}",
                            "value": q, "type": "single_line_text_field"})
            if a is not None and a != "":
                mfs.append({"namespace": "custom", "key": f"f_q_answer_{n}",
                            "value": a, "type": "multi_line_text_field"})
        for m in mfs:
            m["ownerId"] = product_gid
        if mfs:
            try:
                errs = await self.set_metafields(mfs)
                if errs:
                    errors.append(f"metafields: {errs[0].get('message')}")
            except ShopifyError as exc:
                errors.append(f"metafields: {exc}")

        # ── bullet_points rich_text_field (separate call, main.py) ──
        bullets = seo.get("bullet_points")
        if bullets:
            rich = bullets if str(bullets).lstrip().startswith("{") \
                else _sp.bullets_to_rich_text(bullets)
            if rich:
                try:
                    errs = await self.set_metafields([{
                        "namespace": "custom", "key": "bullet_points",
                        "value": rich, "type": "rich_text_field",
                        "ownerId": product_gid,
                    }])
                    if errs:
                        errors.append(f"bullets: {errs[0].get('message')}")
                except ShopifyError as exc:
                    errors.append(f"bullets: {exc}")

        # ── image alts ──
        alt_texts = seo.get("image_alt_texts")
        if alt_texts and image_media:
            alts = [a.strip() for a in str(alt_texts).split("|")]
            for media, alt in zip(image_media, alts):
                if not alt:
                    continue
                try:
                    await self._update_media_alt(product_gid, media["id"], alt)
                except ShopifyError:
                    pass  # non-fatal

        return {"ok": not errors, "errors": errors}

    async def get_product_media(self, product_gid: str) -> List[dict]:
        """Return [{id, alt}] for a product's images, in order."""
        query = """
        query Media($id: ID!) {
          product(id: $id) {
            media(first: 30) {
              nodes { ... on MediaImage { id alt } }
            }
          }
        }
        """
        try:
            data = await self.graphql(query, {"id": product_gid})
            nodes = ((data.get("product") or {}).get("media") or {}).get("nodes") or []
            return [{"id": n["id"], "alt": n.get("alt")} for n in nodes if n.get("id")]
        except ShopifyError:
            return []

    async def _update_media_alt(self, product_gid: str, media_id: str, alt: str) -> None:
        mutation = """
        mutation UpdAlt($productId: ID!, $media: [UpdateMediaInput!]!) {
          productUpdateMedia(productId: $productId, media: $media) {
            mediaUserErrors { field message }
          }
        }
        """
        await self.graphql(mutation, {
            "productId": product_gid,
            "media": [{"id": media_id, "alt": alt[:512]}],
        })


shopify = ShopifyClient()
