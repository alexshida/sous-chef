"""Quantities: converting what a recipe says into grams, and back into words.

Every amount is reduced to grams before anything is added up, priced or
measured for nutrition. Mass converts directly. Volume needs the ingredient's
density (`g_per_cup`), and counted units — a clove, a can, a bunch — need the
ingredient's own weight for one (`unit_g`). A can of chickpeas is its drained
weight, because that is what the nutrition figures describe.
"""

from __future__ import annotations

import math
from fractions import Fraction

MASS_G = {"g": 1.0, "kg": 1000.0, "oz": 28.3495, "lb": 453.592}
VOLUME_ML = {
    "ml": 1.0, "l": 1000.0, "tsp": 4.92892, "tbsp": 14.7868, "cup": 236.588,
    "fl oz": 29.5735, "pinch": 0.31,
}
CUP_ML = VOLUME_ML["cup"]

_ALIASES = {
    "gram": "g", "grams": "g", "kilogram": "kg", "kilograms": "kg",
    "ounce": "oz", "ounces": "oz", "pound": "lb", "pounds": "lb", "lbs": "lb",
    "teaspoon": "tsp", "teaspoons": "tsp", "t": "tsp",
    "tablespoon": "tbsp", "tablespoons": "tbsp", "tbs": "tbsp", "tbl": "tbsp",
    "cups": "cup", "c": "cup",
    "milliliter": "ml", "milliliters": "ml", "liter": "l", "liters": "l", "litre": "l",
    "floz": "fl oz", "fluid ounce": "fl oz", "fluid ounces": "fl oz",
    "pinches": "pinch", "dash": "pinch",
    "": "each", "x": "each", "whole": "each", "piece": "each", "pieces": "each",
    "pc": "each", "pcs": "each", "medium": "each", "large": "each", "small": "each",
}
# Plurals of counted units: "cloves" → "clove", "cans" → "can".
_COUNT_PLURALS = {
    "cloves": "clove", "cans": "can", "bunches": "bunch", "heads": "head",
    "packages": "package", "pkgs": "package", "pkg": "package", "bags": "bag",
    "jars": "jar", "blocks": "block", "fillets": "fillet", "stalks": "stalk",
    "links": "link", "inches": "inch", "in": "inch", "pints": "pint",
    "cartons": "carton", "bottles": "bottle", "tubs": "tub", "pouches": "pouch",
    "boxes": "box", "sprigs": "sprig", "slices": "slice", "leaves": "leaf",
}


class UnitError(ValueError):
    """An amount that cannot be turned into grams for this ingredient."""


def normalize_unit(unit: str | None) -> str:
    u = (unit or "").strip().lower().rstrip(".")
    u = _ALIASES.get(u, u)
    return _COUNT_PLURALS.get(u, u)


def allowed_units(ing: dict) -> list[str]:
    """Units this ingredient can be measured in."""
    units = list(MASS_G)
    if ing.get("g_per_cup"):
        units += [u for u in VOLUME_ML if u != "pinch"]
    units += list((ing.get("unit_g") or {}).keys())
    return units


def to_grams(qty: float, unit: str | None, ing: dict) -> float:
    """Grams of `ing` in `qty` `unit`. Raises UnitError if it cannot be known."""
    u = normalize_unit(unit)
    if qty < 0:
        raise UnitError(f"{ing['name']}: quantity cannot be negative")
    if u in MASS_G:
        return qty * MASS_G[u]
    if u in VOLUME_ML:
        density = ing.get("g_per_cup")
        if not density:
            raise UnitError(
                f"{ing['name']} is not measured by volume; use one of "
                f"{', '.join(allowed_units(ing))}")
        return qty * VOLUME_ML[u] / CUP_ML * density
    unit_g = ing.get("unit_g") or {}
    if u in unit_g:
        return qty * unit_g[u]
    raise UnitError(
        f"'{unit}' is not a unit for {ing['name']}; use one of {', '.join(allowed_units(ing))}")


def convertible(unit: str | None, ing: dict) -> bool:
    try:
        to_grams(1, unit, ing)
        return True
    except UnitError:
        return False


# ── formatting ──────────────────────────────────────────────

_NICE = [Fraction(n, d) for d in (2, 3, 4, 8) for n in range(1, d)]
_GLYPH = {
    Fraction(1, 2): "½", Fraction(1, 3): "⅓", Fraction(2, 3): "⅔", Fraction(1, 4): "¼",
    Fraction(3, 4): "¾", Fraction(1, 8): "⅛", Fraction(3, 8): "⅜", Fraction(5, 8): "⅝",
    Fraction(7, 8): "⅞",
}


def format_qty(q: float) -> str:
    """1.5 → '1½', 0.333 → '⅓', 2.0 → '2', 13.3 → '13'."""
    if q <= 0:
        return "0"
    if q >= 10:
        return str(round(q))
    whole = math.floor(q)
    frac = q - whole
    if frac < 0.06:
        return str(whole) if whole else "⅛"
    if frac > 0.94:
        return str(whole + 1)
    best = min(_NICE, key=lambda f: abs(float(f) - frac))
    glyph = _GLYPH.get(best, f"{best.numerator}/{best.denominator}")
    return f"{whole}{glyph}" if whole else glyph


def pluralize(unit: str, qty: float) -> str:
    if qty <= 1 or unit in MASS_G or unit in VOLUME_ML or unit == "each":
        return "" if unit == "each" else unit
    if unit == "bunch":
        return "bunches"
    if unit == "leaf":
        return "leaves"
    if unit == "box":
        return "boxes"
    if unit == "pouch":
        return "pouches"
    if unit == "inch":
        return "inches"
    return unit + "s"


def format_amount(qty: float, unit: str | None) -> str:
    """'1½ cups', '2 cloves', '3' (for each), '12 oz'."""
    u = normalize_unit(unit)
    if qty == 0:
        return "to taste"
    word = pluralize(u, qty)
    return f"{format_qty(qty)} {word}".strip()


def format_grams(g: float) -> str:
    """A weight a shopper reads: ounces under a pound, pounds above."""
    oz = g / MASS_G["oz"]
    if oz < 16:
        return f"{format_qty(oz)} oz"
    return f"{format_qty(oz / 16)} lb"
