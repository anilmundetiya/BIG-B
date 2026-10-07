"""
Sibling grouping + metafield writing for Phase 6.

Two ported pieces:

1. GROUPING — the merge rule from the Variant & Sibling extension
   (background.js lines ~640-723): for each ASIN, keep only variant ASINs that
   are ALSO in the batch; merge into an existing group if any member overlaps,
   else create a new group; ASINs with no batch-mate stay solo.

2. WRITING — the sibling metafields + tags from brand_viewer.py:911
   (/api/create-siblings): 5 metafields per member, group-name tag on all,
   sibling-admin tag on the first member only.
"""
import json
from typing import Dict, List, Optional


def build_groups(batch_asins: List[str],
                 variant_map: Dict[str, List[dict]]) -> List[dict]:
    """
    batch_asins  : ordered list of ASINs in this run
    variant_map  : { asin: [{asin, label}, ...] }  (what the scraper found per ASIN)

    Returns list of groups: [{ "members": [{asin, label}, ...] }, ...]
    Only ASINs with >=2 total members form a group. Solo ASINs are omitted.

    Mirrors the extension's merge logic.
    """
    batch_set = set(batch_asins)
    placed: Dict[str, int] = {}      # asin -> group index
    groups: List[Dict] = []          # each {"members": {asin: label}}

    def find_group_for(asins: List[str]) -> Optional[int]:
        for a in asins:
            if a in placed:
                return placed[a]
        return None

    for asin in batch_asins:
        if asin in placed:
            continue  # already grouped as someone's variant

        variants = variant_map.get(asin, []) or []
        # self label (if the page listed the current ASIN among its variants)
        self_label = ""
        for v in variants:
            if v["asin"] == asin:
                self_label = v.get("label", "")
                break

        # variant ASINs that are also in this batch (exclude self)
        mates = [v for v in variants
                 if v["asin"] != asin and v["asin"] in batch_set]

        if not mates:
            continue  # solo — no group

        # does current asin or any mate already belong to a group? -> merge
        candidate_asins = [asin] + [m["asin"] for m in mates]
        gi = find_group_for(candidate_asins)

        if gi is None:
            gi = len(groups)
            groups.append({"members": {}})

        members = groups[gi]["members"]
        if asin not in members:
            members[asin] = self_label
            placed[asin] = gi
        for m in mates:
            if m["asin"] not in members:
                members[m["asin"]] = m.get("label", "")
                placed[m["asin"]] = gi

    # to ordered list, dropping any accidental singletons
    out = []
    for g in groups:
        if len(g["members"]) >= 2:
            # preserve batch order within the group
            ordered = [{"asin": a, "label": g["members"].get(a, "")}
                       for a in batch_asins if a in g["members"]]
            out.append({"members": ordered})
    return out


def build_metafields_for_member(member_pid: str, all_gids: List[str],
                                label: str, group_name: str,
                                compare_paise: str) -> List[dict]:
    """
    The exact 5 metafields brand_viewer.py:934 writes, for one member.
    member_pid : numeric product id
    all_gids   : ordered list of gid://shopify/Product/<id> for the whole group
    """
    gid = f"gid://shopify/Product/{member_pid}"
    return [
        {"namespace": "custom", "key": "siblings", "ownerId": gid,
         "type": "list.product_reference", "value": json.dumps(all_gids)},
        {"namespace": "custom", "key": "sibling_label", "ownerId": gid,
         "type": "single_line_text_field", "value": label},
        {"namespace": "custom", "key": "sibling_bestseller", "ownerId": gid,
         "type": "boolean", "value": "false"},
        {"namespace": "custom", "key": "sibling_compare", "ownerId": gid,
         "type": "number_integer", "value": str(compare_paise)},
        {"namespace": "custom", "key": "sibling_group_name", "ownerId": gid,
         "type": "single_line_text_field", "value": group_name},
    ]
