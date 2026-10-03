"""What a recipe costs and what it gives you, at any number of servings.

Cost here is what the food you *use* costs — a quarter of a bunch of cilantro
is a quarter of its price. What you pay at the register is a different number
(you buy the whole bunch), and that one belongs to the grocery list.

Optional ingredients are listed and priced but left out of the totals, and
pantry items are counted (you do use them up) but flagged, since they are not
on the shopping list.
"""

from __future__ import annotations

from sous_chef.config import FIRM_SOURCES
from sous_chef.units import UnitError, format_amount, to_grams

NUTRIENTS = ("kcal", "protein", "carbs", "fat", "fiber")


def choose_offer(ing: dict, stores: list[str]) -> tuple[str | None, dict | None]:
    """The first store, in priority order, that carries this ingredient."""
    for store in stores:
        offer = (ing.get("offers") or {}).get(store)
        if offer and offer.get("carried", 1):
            return store, offer
    return None, None


def package_grams(ing: dict, offer: dict) -> float:
    return to_grams(offer["pkg_qty"], offer["pkg_unit"], ing)


def cost_recipe(recipe: dict, catalog: dict[str, dict], *, servings: int | None = None,
                stores: list[str], pantry: set[str] = frozenset()) -> dict:
    """Cost, nutrition and scaled ingredient lines for `recipe` at `servings`."""
    servings = int(recipe["servings"] if servings is None else servings)
    if servings < 1:
        raise ValueError("servings must be at least 1")
    factor = servings / recipe["servings"]

    lines: list[dict] = []
    total = 0.0
    nutrition = dict.fromkeys(NUTRIENTS, 0.0)
    unavailable: list[str] = []
    problems: list[str] = []
    stores_used: set[str] = set()
    estimated = False

    for item in recipe["ingredients"]:
        ing = catalog.get(item["id"])
        qty = float(item["qty"]) * factor
        line = {
            "id": item["id"], "name": ing["name"] if ing else item["id"],
            "qty": round(qty, 3), "unit": item["unit"], "amount": format_amount(qty, item["unit"]),
            "prep": item.get("prep", ""), "optional": bool(item.get("optional")),
            "pantry": item["id"] in pantry, "grams": None, "store": None, "cost": None,
            "price_source": None,
        }
        lines.append(line)
        if not ing:
            problems.append(f"{item['id']} is not in the catalog")
            continue
        try:
            grams = to_grams(qty, item["unit"], ing)
        except UnitError as e:
            problems.append(str(e))
            continue
        line["grams"] = round(grams, 1)

        store, offer = choose_offer(ing, stores)
        if offer:
            line["store"] = store
            line["price_source"] = offer["source"]
            line["cost"] = round(grams / package_grams(ing, offer) * offer["price"], 2)
        elif not line["optional"] and not line["pantry"]:
            unavailable.append(ing["name"])

        if line["optional"]:
            continue
        if line["cost"] is not None:
            total += line["cost"]
            stores_used.add(store)
            estimated = estimated or offer["source"] not in FIRM_SOURCES
        for k in NUTRIENTS:
            nutrition[k] += grams / 100 * (ing.get(k) or 0)

    per_serving = {k: round(v / servings, 1) for k, v in nutrition.items()}
    return {
        "servings": servings,
        "total_cost": round(total, 2),
        "cost_per_serving": round(total / servings, 2),
        "per_serving": per_serving,
        "lines": lines,
        "unavailable": unavailable,
        "problems": problems,
        "stores": sorted(stores_used, key=stores.index) if stores_used else [],
        "estimated": estimated,
    }


def meets_targets(per_serving: dict, prefs) -> dict:
    """How a serving compares with the per-meal targets."""
    return {
        "protein": per_serving["protein"] >= prefs.protein_g,
        "fiber": per_serving["fiber"] >= prefs.fiber_g,
        "protein_gap": round(max(0.0, prefs.protein_g - per_serving["protein"]), 1),
        "fiber_gap": round(max(0.0, prefs.fiber_g - per_serving["fiber"]), 1),
    }
