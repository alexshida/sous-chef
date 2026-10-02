"""The shopping list: everything the week's recipes need, in whole packages.

Amounts from every chosen recipe are added up in grams per ingredient, then
rounded up to the packages a store actually sells. That rounding is where
ingredient overlap pays off: two recipes sharing one bunch of cilantro cost
one bunch, not two halves. `marginal_cost` measures exactly that — what a
candidate recipe would add to the bill given what is already on the list.

Each ingredient goes to the first selected store, in priority order, that
carries it, so one store does the whole shop unless it cannot.
"""

from __future__ import annotations

import math
from collections import defaultdict

from sous_chef.config import STORES
from sous_chef.costing import choose_offer, package_grams
from sous_chef.units import UnitError, format_amount, format_grams, format_qty, normalize_unit, to_grams

AISLE_ORDER = [
    "produce", "meat & seafood", "dairy & eggs", "refrigerated", "frozen", "bakery",
    "grains & pasta", "canned & jarred", "oils & condiments", "baking", "spices",
    "nuts & seeds",
]

# A perishable with at least this much of its package left over is worth
# planning around: it is the "use it up" list handed to the recipe chef.
LEFTOVER_FRACTION = 0.25
# A shelf-stable item mostly left over after the week is a stock-up — spices,
# sauces, a bag of rice — and belongs in the pantry once bought.
STOCK_UP_FRACTION = 0.5


def _needs(selections: list[tuple[dict, int]], catalog: dict[str, dict]) -> tuple[dict, list[str]]:
    """Grams needed per ingredient across all selected recipes."""
    need: dict[str, dict] = {}
    problems: list[str] = []
    for recipe, servings in selections:
        factor = servings / recipe["servings"]
        for item in recipe["ingredients"]:
            ing = catalog.get(item["id"])
            if not ing:
                problems.append(f"{recipe['title']}: {item['id']} is not in the catalog")
                continue
            qty = float(item["qty"]) * factor
            try:
                grams = to_grams(qty, item["unit"], ing)
            except UnitError as e:
                problems.append(f"{recipe['title']}: {e}")
                continue
            e = need.setdefault(item["id"], {"grams": 0.0, "required": False,
                                             "by_unit": defaultdict(float), "uses": []})
            e["grams"] += grams
            e["by_unit"][normalize_unit(item["unit"])] += qty
            e["required"] = e["required"] or not item.get("optional")
            e["uses"].append({"recipe_id": recipe.get("id"), "recipe": recipe["title"],
                              "amount": format_amount(qty, item["unit"])})
    return need, problems


def _buy(ing: dict, offer: dict, grams: float) -> dict:
    pg = package_grams(ing, offer)
    n = max(1, math.ceil(grams / pg - 1e-6)) if grams > 0 else 1
    leftover = n * pg - grams
    return {
        "packages": n,
        "cost": round(n * offer["price"], 2),
        "used_cost": round(grams / pg * offer["price"], 2),
        "leftover_g": round(leftover, 1),
        "leftover_fraction": round(leftover / (n * pg), 2),
    }


def build_list(selections: list[tuple[dict, int]], catalog: dict[str, dict], *,
               stores: list[str], pantry: set[str] = frozenset(),
               checked: set[str] = frozenset()) -> dict:
    """The full shopping list, grouped by store and aisle."""
    need, problems = _needs(selections, catalog)
    items, pantry_items, unavailable = [], [], []

    for iid, e in need.items():
        ing = catalog[iid]
        units = [u for u, q in e["by_unit"].items() if q > 0]
        need_label = (format_amount(e["by_unit"][units[0]], units[0]) if len(units) == 1
                      else format_grams(e["grams"]))
        base = {"id": iid, "name": ing["name"], "aisle": ing["aisle"], "need": need_label,
                "grams": round(e["grams"], 1), "uses": e["uses"], "optional": not e["required"]}
        if iid in pantry:
            pantry_items.append(base)
            continue
        store, offer = choose_offer(ing, stores)
        if not offer:
            unavailable.append({**base, "elsewhere": [
                STORES.get(s, s) for s, o in ing.get("offers", {}).items()
                if o.get("carried", 1) and s not in stores]})
            continue
        buy = _buy(ing, offer, e["grams"])
        items.append({
            **base, **buy, "store": store, "product": offer["product"],
            "package": f"{format_qty(offer['pkg_qty'])} {offer['pkg_unit']}",
            "price_each": offer["price"], "price_source": offer["source"],
            "perishable": ing["perishable"], "checked": iid in checked,
            "stock_up": not ing["perishable"] and buy["leftover_fraction"] >= STOCK_UP_FRACTION,
        })

    by_store: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for it in items:
        by_store[it["store"]][it["aisle"]].append(it)
    groups = []
    for store in stores:
        if store not in by_store:
            continue
        aisles = [{"aisle": a, "items": sorted(by_store[store][a], key=lambda i: i["name"])}
                  for a in sorted(by_store[store], key=_aisle_rank)]
        required = [i for a in aisles for i in a["items"] if not i["optional"]]
        groups.append({
            "store": store, "name": STORES.get(store, store), "aisles": aisles,
            "total": round(sum(i["cost"] for i in required), 2),
            "count": len(required),
        })

    required = [i for i in items if not i["optional"]]
    leftovers = sorted(
        ({"id": i["id"], "name": i["name"], "fraction": i["leftover_fraction"],
          "grams": i["leftover_g"], "value": round(i["cost"] - i["used_cost"], 2)}
         for i in required if i["perishable"] and i["leftover_fraction"] >= LEFTOVER_FRACTION),
        key=lambda x: -x["value"])
    return {
        "stores": groups,
        "total": round(sum(i["cost"] for i in required), 2),
        "used_total": round(sum(i["used_cost"] for i in required), 2),
        "perishable_leftover_value": round(sum(x["value"] for x in leftovers), 2),
        "stock_up_value": round(sum(i["cost"] - i["used_cost"] for i in required
                                    if i["stock_up"]), 2),
        "stock_up": [i["id"] for i in required if i["stock_up"]],
        "leftovers": leftovers,
        "pantry": sorted(pantry_items, key=lambda i: i["name"]),
        "unavailable": unavailable,
        "problems": problems,
        "estimated": any(i["price_source"] != "user" for i in required),
    }


def bill(selections: list[tuple[dict, int]], catalog: dict[str, dict], *,
         stores: list[str], pantry: set[str] = frozenset()) -> float:
    """What the register would charge for these selections."""
    need, _ = _needs(selections, catalog)
    total = 0.0
    for iid, e in need.items():
        if iid in pantry or not e["required"]:
            continue
        ing = catalog[iid]
        _, offer = choose_offer(ing, stores)
        if offer:
            total += _buy(ing, offer, e["grams"])["cost"]
    return round(total, 2)


def marginal_cost(candidate: tuple[dict, int], selections: list[tuple[dict, int]],
                  catalog: dict[str, dict], *, stores: list[str],
                  pantry: set[str] = frozenset()) -> float:
    """How much adding `candidate` would add to the bill."""
    before = bill(selections, catalog, stores=stores, pantry=pantry)
    after = bill([*selections, candidate], catalog, stores=stores, pantry=pantry)
    return round(after - before, 2)


def shared_perishables(recipe: dict, selections: list[tuple[dict, int]],
                       catalog: dict[str, dict]) -> list[str]:
    """Perishables this recipe has in common with what is already chosen."""
    chosen = {i["id"] for r, _ in selections for i in r["ingredients"]}
    return sorted({catalog[i["id"]]["name"] for i in recipe["ingredients"]
                   if i["id"] in chosen and i["id"] in catalog
                   and catalog[i["id"]]["perishable"]})


def _aisle_rank(aisle: str) -> int:
    return AISLE_ORDER.index(aisle) if aisle in AISLE_ORDER else len(AISLE_ORDER)
