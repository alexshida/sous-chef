"""Are two recipes really the same dish?

Titles alone miss it: "Gochujang Chicken Rice Bowls" and "Gochujang Chicken
Bowls with Kimchi" are one dinner under two names. So two recipes are compared
on what they are made of — each ingredient's share of the recipe by weight,
with close swaps (thighs for breast, basmati for jasmine, turkey for beef)
counted as the same thing — and on the words in their names.

`overlap` is the fraction of one recipe's weight that the other shares, from
0 (nothing in common) to 1 (identical proportions). Spices and sauces barely
move it — they barely weigh anything — yet they are what makes gochujang
chicken a different dinner from teriyaki chicken over the same rice and
broccoli. So the signature seasonings are compared separately, ignoring the
salt, pepper and oil every recipe has.
"""

from __future__ import annotations

import re

from sous_chef.units import UnitError, to_grams

# Ingredients that stand in for each other without making a different dish.
FAMILIES = {
    "chicken": {"chicken-thigh", "chicken-breast", "rotisserie-chicken"},
    "ground-meat": {"ground-turkey", "ground-beef", "ground-pork"},
    "rice": {"jasmine-rice", "basmati-rice", "brown-rice", "brown-rice-frozen"},
    "pasta": {"pasta", "ww-pasta", "lentil-pasta"},
    "onion": {"onion", "red-onion"},
    "spinach": {"baby-spinach", "spinach-frozen"},
    "broth": {"chicken-broth", "veg-broth"},
    "coconut-milk": {"coconut-milk", "coconut-milk-light"},
    "tomatoes": {"diced-tomatoes", "crushed-tomatoes"},
    "white-fish": {"cod"},
}
_FAMILY_OF = {iid: fam for fam, members in FAMILIES.items() for iid in members}

# What a dish tastes of: spices, condiments and the sauces that define it.
FLAVOR_AISLES = {"spices", "oils & condiments"}
FLAVOR_EXTRA = {"masala-simmer-sauce", "red-curry-sauce", "marinara", "salsa", "pesto",
                "kimchi", "tzatziki", "coconut-milk", "coconut-milk-light"}
GENERIC = {"salt", "black-pepper", "olive-oil", "neutral-oil"}

# Words that describe a format rather than a dish.
_TITLE_STOP = {"with", "and", "the", "a", "an", "of", "in", "on", "style", "easy", "quick",
               "big", "batch", "sheet", "pan", "one", "pot", "bowls", "bowl", "shortcut",
               "homemade", "simple", "weeknight", "meal", "prep"}

# Thresholds, set against the starter library (no two of its recipes come
# close — see tests/test_similarity.py) and against near-copies of them. Any
# one rule is enough:
SAME_BY_INGREDIENTS = 0.85        # near-identical make-up, whatever it is called
SAME_WITH_TITLE = (0.6, 0.4)      # much the same make-up and much the same name
SAME_WITH_FLAVOR = (0.72, 0.7)    # much the same make-up and the same seasoning


def title_words(title: str) -> set[str]:
    words = re.sub(r"[^a-z0-9 ]", " ", title.lower().replace("&", " and ")).split()
    return {w.rstrip("s") for w in words if w not in _TITLE_STOP}


def title_overlap(a: str, b: str) -> float:
    wa, wb = title_words(a), title_words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def composition(recipe: dict, catalog: dict[str, dict]) -> dict[str, float]:
    """Each ingredient family's share of the recipe's weight."""
    grams: dict[str, float] = {}
    for item in recipe["ingredients"]:
        ing = catalog.get(item["id"])
        if not ing or item.get("optional"):
            continue
        try:
            g = to_grams(float(item["qty"]), item["unit"], ing)
        except (UnitError, ValueError):
            continue
        key = _FAMILY_OF.get(item["id"], item["id"])
        grams[key] = grams.get(key, 0.0) + g
    total = sum(grams.values())
    return {k: v / total for k, v in grams.items()} if total else {}


def overlap(a: dict[str, float], b: dict[str, float]) -> float:
    return sum(min(a[k], b[k]) for k in a.keys() & b.keys())


def flavors(recipe: dict, catalog: dict[str, dict]) -> set[str]:
    out = set()
    for item in recipe["ingredients"]:
        ing = catalog.get(item["id"])
        if not ing or item.get("optional") or item["id"] in GENERIC:
            continue
        if ing["aisle"] in FLAVOR_AISLES or item["id"] in FLAVOR_EXTRA:
            out.add(_FAMILY_OF.get(item["id"], item["id"]))
    return out


def flavor_overlap(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def compare(a: dict, b: dict, catalog: dict[str, dict]) -> dict:
    ingredients = overlap(composition(a, catalog), composition(b, catalog))
    title = title_overlap(a["title"], b["title"])
    flavor = flavor_overlap(flavors(a, catalog), flavors(b, catalog))
    same = (ingredients >= SAME_BY_INGREDIENTS
            or (ingredients >= SAME_WITH_TITLE[0] and title >= SAME_WITH_TITLE[1])
            or (ingredients >= SAME_WITH_FLAVOR[0] and flavor >= SAME_WITH_FLAVOR[1]))
    return {"same": same, "ingredients": round(ingredients, 2), "title": round(title, 2),
            "flavor": round(flavor, 2)}


def most_similar(recipe: dict, pool: list[dict], catalog: dict[str, dict]) -> tuple[dict, dict] | None:
    """The recipe in `pool` that `recipe` duplicates, with the scores, if any."""
    best = None
    for other in pool:
        if other.get("id") is not None and other.get("id") == recipe.get("id"):
            continue
        c = compare(recipe, other, catalog)
        if c["same"] and (best is None or c["ingredients"] > best[1]["ingredients"]):
            best = (other, c)
    return best
