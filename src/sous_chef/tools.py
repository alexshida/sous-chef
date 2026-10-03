"""The tool layer: everything the planner — you, or Claude — can read or change.

This is the single definition of what can be done. The MCP server (which the
recipe chef uses) and the web app are both thin adapters over it, so the
suggestions Claude reasons about and the numbers on your screen can never
disagree.

The division of labour mirrors Trainer's. Arithmetic is computed here and
handed over as fact: unit conversion, cost per serving, protein and fiber,
package rounding, what a recipe adds to the bill, which nights are free and
which meals leftovers cover. Judgement is not: no function here decides what
you should eat. Claude writes recipes and saves them with `propose_recipe`;
the code then tells it, and you, what they cost and what they deliver.

Every function returns plain JSON-ready data and raises ToolError with a
message meant for the caller to read.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

from sous_chef import calendars, grocery, schedule
from sous_chef.config import CUISINES, STORES, Preferences, load_prefs, save_prefs
from sous_chef.costing import choose_offer, cost_recipe, meets_targets, package_grams
from sous_chef.storage import db
from sous_chef.units import UnitError, allowed_units, format_qty, normalize_unit, to_grams


class ToolError(Exception):
    """A failure the caller should see and can act on."""


def _catalog() -> dict[str, dict]:
    return db.all_ingredients()


def _prefs() -> Preferences:
    return load_prefs()


# ─────────────────────────────────────────────────────────────
# Preferences
# ─────────────────────────────────────────────────────────────

def get_preferences() -> dict:
    """Household, stores, cuisines, nutrition targets and constraints."""
    p = _prefs()
    out = asdict(p)
    out["store_names"] = {s: STORES[s] for s in STORES}
    out["cuisine_names"] = CUISINES
    return out


def update_preferences(changes: dict) -> dict:
    p = _prefs()
    unknown = set(changes) - set(Preferences.__dataclass_fields__)
    if unknown:
        raise ToolError(f"Unknown preference(s): {', '.join(sorted(unknown))}")
    merged = Preferences.from_dict({**asdict(p), **changes})
    if not merged.stores or any(s not in STORES for s in merged.stores):
        raise ToolError(f"stores must be a non-empty list of {', '.join(STORES)}")
    if merged.household < 1 or merged.default_servings < 1 or merged.prep_servings < 1:
        raise ToolError("household, default_servings and prep_servings must be at least 1")
    try:
        schedule.parse_hhmm(merged.prep_start)
    except ValueError:
        raise ToolError("prep_start must look like 13:00")
    try:
        schedule.parse_hhmm(merged.dinner_time)
    except ValueError:
        raise ToolError("dinner_time must look like 19:00")
    merged.calendar_feeds = [u.strip() for u in merged.calendar_feeds if u.strip()]
    save_prefs(merged)
    return get_preferences()


# ─────────────────────────────────────────────────────────────
# Catalog, prices, pantry
# ─────────────────────────────────────────────────────────────

def _offer_view(o: dict) -> dict:
    return {"store": o["store"], "store_name": STORES.get(o["store"], o["store"]),
            "product": o["product"], "package": f"{format_qty(o['pkg_qty'])} {o['pkg_unit']}",
            "pkg_qty": o["pkg_qty"], "pkg_unit": o["pkg_unit"], "price": o["price"],
            "source": o["source"], "carried": bool(o["carried"])}


def _ingredient_view(ing: dict, *, pantry: set[str], stores: list[str] | None = None) -> dict:
    offers = sorted(ing["offers"].values(), key=lambda o: list(STORES).index(o["store"])
                    if o["store"] in STORES else 99)
    view = {
        "id": ing["id"], "name": ing["name"], "aisle": ing["aisle"],
        "perishable": ing["perishable"], "units": allowed_units(ing),
        "per_100g": {k: ing[k] for k in ("kcal", "protein", "carbs", "fat", "fiber")},
        "pantry": ing["id"] in pantry, "source": ing["source"],
        "offers": [_offer_view(o) for o in offers],
    }
    if stores is not None:
        store, offer = choose_offer(ing, stores)
        view["available"] = offer is not None
        view["buy_at"] = store
        if offer:
            pg = package_grams(ing, offer)
            view["price_per_100g"] = round(offer["price"] / pg * 100, 2)
    return view


def search_catalog(query: str = "", aisle: str | None = None, limit: int = 30,
                   stores: list[str] | None = None, available_only: bool = False,
                   plan_id: int | None = None) -> list[dict]:
    """Ingredients matching `query` (words in any order), with nutrition and offers.

    With `stores` (or a `plan_id`, meaning that plan's stores), each result says
    whether it can be bought there and where; `available_only` drops the ones
    that cannot.
    """
    if stores is None and plan_id is not None:
        stores = _stores_for(plan_id)
    pantry = db.pantry_ids()
    words = [w for w in re.split(r"[\s,]+", query.lower()) if w]
    out = []
    for ing in _catalog().values():
        hay = f"{ing['id']} {ing['name']} {ing['aisle']}".lower()
        if words and not all(w in hay or w.rstrip("s") in hay for w in words):
            continue
        if aisle and ing["aisle"] != aisle:
            continue
        view = _ingredient_view(ing, pantry=pantry, stores=stores)
        if available_only and not view.get("available"):
            continue
        out.append(view)
    out.sort(key=lambda v: (grocery._aisle_rank(v["aisle"]), v["name"]))
    return out[:limit] if limit else out


def get_ingredient(ingredient_id: str) -> dict:
    ing = db.get_ingredient(ingredient_id)
    if not ing:
        raise ToolError(_unknown_ingredient(ingredient_id))
    return _ingredient_view(ing, pantry=db.pantry_ids())


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]


_QUALIFIERS = {"fresh", "dried", "dry", "organic", "canned", "raw", "frozen", "and", "or",
               "of", "the", "a", "with", "drained", "cooked", "whole", "large", "small"}


def _words(name: str) -> set[str]:
    return set(re.sub(r"[^a-z ]", " ", name.lower()).split()) - _QUALIFIERS


def _near_duplicates(name: str) -> list[str]:
    """Catalog ingredients that look like the same thing as `name`.

    "Fish sauce, Thai" against "Fish sauce": one name's words contain the
    other's. A model asked to use only what the stores carry is tempted to
    add a twin of an unavailable item with a made-up price; this catches it.
    """
    mine = _words(name)
    out = []
    for iid, ing in _catalog().items():
        theirs = _words(ing["name"])
        similar = difflib.SequenceMatcher(None, name.lower(), ing["name"].lower()).ratio() > 0.85
        if theirs and mine and (theirs <= mine or mine <= theirs or similar):
            out.append(f"{iid} ({ing['name']})")
    return out


def add_ingredient(name: str, aisle: str, kcal: float, protein: float, carbs: float,
                   fat: float, fiber: float, offers: list[dict], perishable: bool = False,
                   g_per_cup: float | None = None, unit_g: dict | None = None,
                   not_duplicate: bool = False) -> dict:
    """Add an ingredient the catalog lacks, with estimated nutrition and prices.

    offers: [{store, product, pkg_qty, pkg_unit, price}]. Prices added this way
    are marked as estimates until you correct them. Something that looks like
    an existing ingredient is refused with the match named; `not_duplicate`
    says it really is different (brown sugar beside sugar).
    """
    iid = _slug(name)
    if not iid:
        raise ToolError("name is required")
    if db.get_ingredient(iid):
        raise ToolError(f"'{iid}' already exists — use it rather than adding a duplicate.")
    if not not_duplicate:
        twins = _near_duplicates(name)
        if twins:
            raise ToolError(
                f"The catalog already has {', '.join(twins[:4])}. Use that, even if the user's "
                "stores don't sell it — the app shows where to buy it. Only if this is "
                "genuinely a different ingredient, call again with not_duplicate=true.")
    if aisle not in grocery.AISLE_ORDER:
        raise ToolError(f"aisle must be one of: {', '.join(grocery.AISLE_ORDER)}")
    nums = {"kcal": kcal, "protein": protein, "carbs": carbs, "fat": fat, "fiber": fiber}
    if any(v is None or v < 0 for v in nums.values()):
        raise ToolError("nutrition values are per 100 g and cannot be negative")
    if protein + carbs + fat + fiber > 101:
        raise ToolError("protein + carbs + fat + fiber exceed 100 g per 100 g — check the values")
    ing = {"id": iid, "name": name.strip(), "aisle": aisle, "perishable": perishable,
           "g_per_cup": g_per_cup, "unit_g": {normalize_unit(k): float(v)
                                              for k, v in (unit_g or {}).items()},
           **nums, "source": "claude"}
    clean = []
    for o in offers or []:
        if o.get("store") not in STORES:
            raise ToolError(f"offer store must be one of {', '.join(STORES)}")
        try:
            to_grams(float(o["pkg_qty"]), o["pkg_unit"], ing)
        except (KeyError, ValueError, UnitError) as e:
            raise ToolError(f"offer at {o.get('store')}: {e}")
        if float(o.get("price", 0)) <= 0:
            raise ToolError("offer price must be positive")
        clean.append({**o, "pkg_qty": float(o["pkg_qty"]), "price": float(o["price"]),
                      "source": "claude"})
    if not clean:
        raise ToolError("give at least one store offer with an estimated price")
    db.insert_ingredient(ing, clean)
    return get_ingredient(iid)


def set_price(ingredient_id: str, store: str, price: float, pkg_qty: float | None = None,
              pkg_unit: str | None = None, product: str | None = None) -> dict:
    """Record what a store actually charges. Marks the price as yours."""
    ing = db.get_ingredient(ingredient_id)
    if not ing:
        raise ToolError(_unknown_ingredient(ingredient_id))
    if store not in STORES:
        raise ToolError(f"store must be one of {', '.join(STORES)}")
    if price is None or price <= 0:
        raise ToolError("price must be positive")
    current = ing["offers"].get(store) or next(iter(ing["offers"].values()), None)
    qty = pkg_qty if pkg_qty is not None else (current or {}).get("pkg_qty")
    unit = pkg_unit or (current or {}).get("pkg_unit")
    if not qty or not unit:
        raise ToolError("give the package size (pkg_qty and pkg_unit) for a new offer")
    try:
        to_grams(float(qty), unit, ing)
    except UnitError as e:
        raise ToolError(str(e))
    name = product or (ing["offers"].get(store) or {}).get("product") or ing["name"]
    db.upsert_offer(ingredient_id, store, price=float(price), pkg_qty=float(qty),
                    pkg_unit=unit, product=name, source="user")
    return get_ingredient(ingredient_id)


def set_carried(ingredient_id: str, store: str, carried: bool) -> dict:
    """Mark whether a store carries an ingredient at all."""
    if not db.set_carried(ingredient_id, store, carried):
        raise ToolError(f"No offer for {ingredient_id} at {STORES.get(store, store)} to change.")
    return get_ingredient(ingredient_id)


def get_pantry() -> list[dict]:
    cat = _catalog()
    return sorted(({"id": i, "name": cat[i]["name"], "aisle": cat[i]["aisle"]}
                   for i in db.pantry_ids() if i in cat), key=lambda x: x["name"])


def set_pantry(ingredient_id: str, have: bool = True) -> dict:
    if not db.get_ingredient(ingredient_id):
        raise ToolError(_unknown_ingredient(ingredient_id))
    db.set_pantry(ingredient_id, have)
    return {"id": ingredient_id, "pantry": have}


def _unknown_ingredient(iid: str) -> str:
    cat = _catalog()
    names = {i: f"{i} ({c['name']})" for i, c in cat.items()}
    close = difflib.get_close_matches(iid, list(cat), n=4, cutoff=0.5)
    words = [w for w in re.split(r"[-\s]+", iid.lower()) if len(w) > 2]
    close += [i for i, c in cat.items()
              if i not in close and any(w in c["name"].lower() for w in words)][:4]
    hint = f" Did you mean: {', '.join(names[c] for c in close[:6])}?" if close else ""
    return (f"No ingredient '{iid}'. Use search_catalog to find ids, or add_ingredient "
            f"for something genuinely missing.{hint}")


# ─────────────────────────────────────────────────────────────
# Recipes
# ─────────────────────────────────────────────────────────────

def _stores_for(plan_id: int | None) -> list[str]:
    if plan_id:
        plan = db.get_plan(plan_id)
        if plan:
            return plan["stores"]
    return _prefs().stores


def _summary(recipe: dict, costed: dict, prefs: Preferences) -> dict:
    return {
        "id": recipe["id"], "title": recipe["title"], "cuisine": recipe["cuisine"],
        "cuisine_name": CUISINES.get(recipe["cuisine"], recipe["cuisine"]),
        "summary": recipe["summary"], "tags": recipe["tags"], "source": recipe["source"],
        "in_library": recipe["in_library"],
        "base_servings": recipe["servings"], "servings": costed["servings"],
        "active_min": recipe["active_min"], "total_min": recipe["total_min"],
        "total_cost": costed["total_cost"], "cost_per_serving": costed["cost_per_serving"],
        "per_serving": costed["per_serving"], "targets": meets_targets(costed["per_serving"], prefs),
        "unavailable": costed["unavailable"], "stores": costed["stores"],
        "estimated": costed["estimated"], "problems": costed["problems"],
    }


def get_recipe(recipe_id: int, servings: int | None = None, plan_id: int | None = None) -> dict:
    """A full recipe scaled to `servings`: ingredient lines with amounts and cost,
    steps, cost per serving, and nutrition per serving."""
    r = db.get_recipe(int(recipe_id))
    if not r:
        raise ToolError(f"No recipe {recipe_id}.")
    if servings is not None and servings < 1:
        raise ToolError("servings must be at least 1")
    prefs = _prefs()
    costed = cost_recipe(r, _catalog(), servings=servings, stores=_stores_for(plan_id),
                         pantry=db.pantry_ids())
    return {**_summary(r, costed, prefs), "lines": costed["lines"], "steps": r["steps"]}


def list_recipes(query: str = "", cuisine: str | None = None,
                 include_suggestions: bool = False) -> list[dict]:
    """Your recipe library with cost per serving and nutrition, at default servings."""
    prefs = _prefs()
    cat, pantry, stores = _catalog(), db.pantry_ids(), prefs.stores
    words = query.lower().split()
    out = []
    for r in db.list_recipes(library_only=not include_suggestions):
        hay = f"{r['title']} {r['summary']} {' '.join(r['tags'])}".lower()
        if words and not all(w in hay for w in words):
            continue
        if cuisine and r["cuisine"] != cuisine:
            continue
        costed = cost_recipe(r, cat, servings=prefs.default_servings, stores=stores, pantry=pantry)
        out.append(_summary(r, costed, prefs))
    return out


def _validate_recipe(r: dict, cat: dict[str, dict]) -> dict:
    """Check a recipe the model (or an import) wrote. Errors name the fix."""
    errors: list[str] = []
    title = (r.get("title") or "").strip()
    if not title:
        errors.append("title is required")
    cuisine = r.get("cuisine") or "other"
    if cuisine not in CUISINES:
        errors.append(f"cuisine must be one of: {', '.join(CUISINES)}")
    try:
        servings = int(r.get("servings") or 0)
        active = int(r.get("active_min") or 0)
        total = int(r.get("total_min") or 0)
    except (TypeError, ValueError):
        raise ToolError("servings, active_min and total_min must be whole numbers")
    if not 1 <= servings <= 16:
        errors.append("servings must be between 1 and 16")
    if total < active or total <= 0:
        errors.append("total_min must be positive and at least active_min")
    steps = [str(s).strip() for s in (r.get("steps") or []) if str(s).strip()]
    if not steps:
        errors.append("steps must be a non-empty list of instructions")
    lines = []
    for n, item in enumerate(r.get("ingredients") or [], 1):
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        iid = item.get("id", "")
        ing = cat.get(iid)
        if not ing:
            errors.append(f"ingredient {n}: {_unknown_ingredient(iid)}")
            continue
        try:
            qty = float(item.get("qty", 0))
            to_grams(qty, item.get("unit"), ing)
        except (TypeError, ValueError) as e:
            errors.append(f"ingredient {n} ({iid}): {e}")
            continue
        lines.append({"id": iid, "qty": qty, "unit": normalize_unit(item.get("unit")),
                      "prep": (item.get("prep") or "").strip(),
                      "optional": bool(item.get("optional"))})
    if not lines and not any(e.startswith("ingredient") for e in errors):
        errors.append("ingredients must list at least one catalog ingredient")
    if errors:
        raise ToolError("Recipe not saved:\n- " + "\n- ".join(errors))
    return {"title": title, "cuisine": cuisine, "summary": (r.get("summary") or "").strip(),
            "servings": servings, "active_min": active, "total_min": total,
            "ingredients": lines, "steps": steps,
            "tags": [str(t).strip().lower() for t in (r.get("tags") or []) if str(t).strip()]}


def check_recipe(recipe: dict, servings: int | None = None, plan_id: int | None = None) -> dict:
    """Validate and cost a recipe without saving it."""
    clean = _validate_recipe(recipe, _catalog())
    prefs = _prefs()
    costed = cost_recipe(clean, _catalog(), servings=servings or prefs.default_servings,
                         stores=_stores_for(plan_id), pantry=db.pantry_ids())
    return {k: costed[k] for k in ("servings", "total_cost", "cost_per_serving", "per_serving",
                                   "unavailable", "stores", "estimated")} | {
        "targets": meets_targets(costed["per_serving"], prefs)}


def propose_recipe(recipe: dict, plan_id: int | None = None, origin: str = "claude") -> dict:
    """Save a recipe and, with `plan_id`, offer it as a candidate for that week.

    origin: claude (a fresh suggestion), craft (made to the user's request), or
    import (the user's own recipe, converted). Crafted and imported recipes go
    straight into the library; plain suggestions join it once chosen.
    """
    if origin not in ("claude", "craft", "import"):
        raise ToolError("origin must be claude, craft or import")
    if plan_id is not None and not db.get_plan(plan_id):
        raise ToolError(f"No plan {plan_id}.")
    clean = _validate_recipe(recipe, _catalog())
    existing, where = _same_title(clean["title"], plan_id)
    already = False
    if existing and _same_dish(existing, clean):
        # The same recipe saved again (a repeated request, a retried call):
        # offer the one that exists rather than a second copy of it.
        rid, already = existing["id"], True
        if origin != "claude":
            db.set_in_library(rid, True)
    elif existing:
        if origin == "claude":
            raise ToolError(f"'{existing['title']}' is already {where} (recipe {existing['id']}). "
                            "Propose a different dish.")
        raise ToolError(f"A different recipe called '{existing['title']}' is already {where} "
                        f"(recipe {existing['id']}). Give this one a title that tells them apart, "
                        "e.g. by its source or what makes it different.")
    else:
        rid = db.insert_recipe(clean, source=origin, in_library=origin != "claude")
    if plan_id is not None:
        db.add_candidate(plan_id, rid, origin)
    # Reported at the servings it was written for; cards scale it to the
    # user's default, and that difference is not something to fix.
    out = get_recipe(rid, plan_id=plan_id)
    result = {k: out[k] for k in ("id", "title", "servings", "total_cost", "cost_per_serving",
                                  "per_serving", "targets", "unavailable", "stores")}
    if already:
        result["already_saved"] = True
    return result


def title_key(title: str) -> str:
    """A title reduced to what makes two recipes "the same name"."""
    t = title.lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def _same_title(title: str, plan_id: int | None) -> tuple[dict | None, str]:
    """A recipe with this title already offered or chosen this week, or in the library."""
    key = title_key(title)
    if plan_id is not None:
        ids = ({c["recipe_id"] for c in db.candidates(plan_id, include_dismissed=True)}
               | {s["recipe_id"] for s in db.selections(plan_id)})
        for rid in sorted(ids):
            r = db.get_recipe(rid)
            if r and title_key(r["title"]) == key:
                return r, "offered this week"
    for r in db.list_recipes(library_only=True):
        if title_key(r["title"]) == key:
            return r, "in the library"
    return None, ""


def _same_dish(a: dict, b: dict) -> bool:
    def lines(r):
        return sorted((i["id"], normalize_unit(i["unit"]), round(float(i["qty"]), 3))
                      for i in r["ingredients"])
    return a["servings"] == b["servings"] and lines(a) == lines(b)


def find_duplicate_recipes() -> list[dict]:
    """Recipes sharing a title, grouped, with the copy to keep.

    The keeper is one chosen in some plan if there is one, then one in the
    library, then the oldest.
    """
    chosen = db.chosen_recipe_ids()
    groups: dict[str, list[dict]] = {}
    for r in db.list_recipes(library_only=False):
        groups.setdefault(title_key(r["title"]), []).append(r)
    out = []
    for copies in groups.values():
        if len(copies) < 2:
            continue
        copies.sort(key=lambda r: (r["id"] not in chosen, not r["in_library"], r["id"]))
        out.append({"title": copies[0]["title"], "keep": copies[0]["id"],
                    "remove": [r["id"] for r in copies[1:]]})
    return sorted(out, key=lambda g: g["title"].lower())


def merge_duplicate_recipes(apply: bool = False) -> list[dict]:
    """Fold each duplicate into the copy kept: its places in plans move over,
    then it is deleted. Without `apply`, only reports what would happen."""
    groups = find_duplicate_recipes()
    if apply:
        for g in groups:
            for dup in g["remove"]:
                db.merge_recipe(keep=g["keep"], dup=dup)
    return groups


def save_to_library(recipe_id: int, keep: bool = True) -> dict:
    if not db.get_recipe(recipe_id):
        raise ToolError(f"No recipe {recipe_id}.")
    db.set_in_library(recipe_id, keep)
    return {"id": recipe_id, "in_library": keep}


def delete_recipe(recipe_id: int) -> dict:
    if not db.delete_recipe(recipe_id):
        raise ToolError(f"No recipe {recipe_id}.")
    return {"id": recipe_id, "deleted": True}


# ─────────────────────────────────────────────────────────────
# Plans
# ─────────────────────────────────────────────────────────────

def _plan_or_raise(plan_id: int | None) -> dict:
    plan = db.get_plan(plan_id) if plan_id else db.latest_plan()
    if not plan:
        raise ToolError("No plan yet. Start one with new_plan." if not plan_id
                        else f"No plan {plan_id}.")
    return plan


def new_plan(week_start: str | None = None, n_recipes: int | None = None,
             stores: list[str] | None = None, meal_prep: bool = False) -> dict:
    """Start planning a week. Defaults come from preferences."""
    prefs = _prefs()
    start = _parse_date(week_start) if week_start else schedule.default_week_start()
    stores = stores or prefs.stores
    if any(s not in STORES for s in stores):
        raise ToolError(f"stores must be from {', '.join(STORES)}")
    pid = db.create_plan(start.isoformat(), int(n_recipes or prefs.n_recipes), stores, meal_prep)
    return get_plan(pid)


def update_plan(plan_id: int, week_start: str | None = None, n_recipes: int | None = None,
                stores: list[str] | None = None, meal_prep: bool | None = None) -> dict:
    """Change a plan's week, size, stores, or meal-prep mode.

    Meal-prep mode cooks the week in one batch session: suggestions favour
    recipes made for it, servings default to `prep_servings`, everything is
    cooked on the first free day, and servings past their keeping time go in
    the freezer instead of being written off. It works with any stores, but
    bulk packs are the point, so the plan says when Costco is not switched on.
    """
    _plan_or_raise(plan_id)
    fields: dict[str, Any] = {}
    if meal_prep is not None:
        fields["meal_prep"] = bool(meal_prep)
    if week_start:
        fields["week_start"] = _parse_date(week_start).isoformat()
    if n_recipes is not None:
        if not 1 <= n_recipes <= 14:
            raise ToolError("n_recipes must be between 1 and 14")
        fields["n_recipes"] = n_recipes
    if stores is not None:
        if not stores or any(s not in STORES for s in stores):
            raise ToolError(f"stores must be a non-empty list from {', '.join(STORES)}")
        fields["stores"] = stores
    db.update_plan(plan_id, **fields)
    return get_plan(plan_id)


def _default_servings(plan: dict, prefs: Preferences | None = None) -> int:
    prefs = prefs or _prefs()
    return prefs.prep_servings if plan.get("meal_prep") else prefs.default_servings


MEAL_PREP_TAGS = {"meal-prep", "freezer-friendly", "sheet-pan", "one-pot"}


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ToolError(f"'{s}' is not a date — use YYYY-MM-DD")


def _selected_pairs(plan: dict, recipes: dict[int, dict]) -> list[tuple[dict, int]]:
    return [(recipes[s["recipe_id"]], s["servings"]) for s in db.selections(plan["id"])
            if s["recipe_id"] in recipes]


def _recipes_by_id(ids: set[int]) -> dict[int, dict]:
    out = {}
    for rid in ids:
        r = db.get_recipe(rid)
        if r:
            out[rid] = r
    return out


def get_week(plan_id: int | None = None) -> dict:
    """The plan's seven days: calendar events, day status, cook nights, and which
    meals each recipe's servings cover."""
    plan = _plan_or_raise(plan_id)
    prefs = _prefs()
    start = date.fromisoformat(plan["week_start"])
    events, problems = ([], [])
    if prefs.calendar_feeds:
        events, problems = calendars.fetch_events(
            prefs.calendar_feeds, start, start + timedelta(days=7), prefs.timezone)
    sels = db.selections(plan["id"])
    recipes = _recipes_by_id({s["recipe_id"] for s in sels})
    cat = _catalog()
    items = [{"recipe_id": s["recipe_id"], "title": recipes[s["recipe_id"]]["title"],
              "servings": s["servings"], "total_min": recipes[s["recipe_id"]]["total_min"],
              "cook_date": s["cook_date"], "freshness": _freshness(recipes[s["recipe_id"]], cat)}
             for s in sels if s["recipe_id"] in recipes]
    week = schedule.plan_week(start, events, plan["day_status"], items, prefs,
                              meal_prep=plan["meal_prep"])
    week["calendar_problems"] = problems
    week["calendar_connected"] = bool(prefs.calendar_feeds)
    return week


def _freshness(recipe: dict, cat: dict[str, dict]) -> int:
    """How much a recipe wants cooking early in the week: fresh meat and fish
    count double, other perishables once."""
    score = 0
    for item in recipe["ingredients"]:
        ing = cat.get(item["id"])
        if ing and ing["perishable"] and not item.get("optional"):
            score += 2 if ing["aisle"] == "meat & seafood" else 1
    return score


def set_day_status(plan_id: int, day: str, status: str | None) -> dict:
    """Override a day's status (home, busy, out, away); None returns it to the calendar's."""
    plan = _plan_or_raise(plan_id)
    if status is not None and status not in schedule.STATUSES:
        raise ToolError(f"status must be one of {', '.join(schedule.STATUSES)}")
    overrides = dict(plan["day_status"])
    _parse_date(day)
    if status is None:
        overrides.pop(day, None)
    else:
        overrides[day] = status
    db.update_plan(plan["id"], day_status=overrides)
    return get_week(plan["id"])


def get_plan(plan_id: int | None = None) -> dict:
    """Everything about a week's plan: chosen recipes (scaled and costed),
    candidates with what each would add to the bill, the week, nutrition
    against targets, and the grocery total."""
    plan = _plan_or_raise(plan_id)
    prefs = _prefs()
    cat, pantry = _catalog(), db.pantry_ids()
    stores = plan["stores"]
    sels = db.selections(plan["id"])
    cands = db.candidates(plan["id"])
    recipes = _recipes_by_id({s["recipe_id"] for s in sels} | {c["recipe_id"] for c in cands})
    pairs = _selected_pairs(plan, recipes)
    week = get_week(plan["id"])

    selected = []
    for s in sels:
        r = recipes.get(s["recipe_id"])
        if not r:
            continue
        costed = cost_recipe(r, cat, servings=s["servings"], stores=stores, pantry=pantry)
        selected.append({**_summary(r, costed, prefs),
                         "cook_date": week["cook_dates"].get(r["id"]),
                         "cook_date_auto": week["auto_assigned"].get(r["id"], False)})

    chosen_ids = {s["recipe_id"] for s in sels}
    serve = _default_servings(plan, prefs)
    candidates = []
    for c in cands:
        r = recipes.get(c["recipe_id"])
        if not r or r["id"] in chosen_ids:
            continue
        costed = cost_recipe(r, cat, servings=serve, stores=stores, pantry=pantry)
        candidates.append({
            **_summary(r, costed, prefs), "origin": c["origin"],
            "adds_to_bill": grocery.marginal_cost((r, serve), pairs, cat,
                                                  stores=stores, pantry=pantry),
            "shares": grocery.shared_perishables(r, pairs, cat),
        })

    total_servings = sum(s["servings"] for s in selected)
    nutrition = _weighted_nutrition(selected)
    bill = grocery.bill(pairs, cat, stores=stores, pantry=pantry)
    used = sum(s["total_cost"] for s in selected)
    return {
        "id": plan["id"], "week_start": plan["week_start"], "n_recipes": plan["n_recipes"],
        "stores": stores, "store_names": {s: STORES[s] for s in STORES},
        "selected": selected, "candidates": candidates,
        "week": week,
        "totals": {
            "servings": total_servings, "grocery_bill": bill,
            "food_cost": round(used, 2),
            "cost_per_serving": round(used / total_servings, 2) if total_servings else 0,
            "bill_per_serving": round(bill / total_servings, 2) if total_servings else 0,
            "per_serving": nutrition,
            "targets": meets_targets(nutrition, prefs) if total_servings else None,
        },
        "targets": {"protein_g": prefs.protein_g, "fiber_g": prefs.fiber_g, "kcal": prefs.kcal},
        "household": prefs.household,
        "meal_prep": plan["meal_prep"], "default_servings": serve,
        # Bulk prep without the bulk store: worth a nudge, never a requirement.
        "costco_suggested": plan["meal_prep"] and "costco" not in stores,
    }


def _weighted_nutrition(selected: list[dict]) -> dict:
    total = sum(s["servings"] for s in selected)
    if not total:
        return {k: 0 for k in ("kcal", "protein", "carbs", "fat", "fiber")}
    return {k: round(sum(s["per_serving"][k] * s["servings"] for s in selected) / total, 1)
            for k in ("kcal", "protein", "carbs", "fat", "fiber")}


def suggest_from_library(plan_id: int, count: int = 4, replace: bool = True) -> dict:
    """Offer library recipes as candidates — the instant, free kind of suggestion.

    Picks recipes not already offered this week, buyable at the plan's stores,
    weighted toward your cuisines, not cooked in the last two weeks, and — once
    something is chosen — sharing its perishables. With `replace`, the current
    unchosen candidates from the library are cleared first (a refresh).
    """
    plan = _plan_or_raise(plan_id)
    prefs = _prefs()
    cat, pantry, stores = _catalog(), db.pantry_ids(), plan["stores"]
    seen = {c["recipe_id"] for c in db.candidates(plan["id"], include_dismissed=True)}
    chosen = {s["recipe_id"] for s in db.selections(plan["id"])}
    if replace:
        for c in db.candidates(plan["id"]):
            if c["origin"] == "library" and c["recipe_id"] not in chosen:
                db.dismiss_candidate(plan["id"], c["recipe_id"])
    recent_from = (date.fromisoformat(plan["week_start"]) - timedelta(days=14)).isoformat()
    recent = db.recently_cooked(recent_from) - chosen
    pairs = _selected_pairs(plan, _recipes_by_id(chosen))
    chosen_cuisines = [db.get_recipe(i)["cuisine"] for i in chosen if db.get_recipe(i)]
    prep = plan["meal_prep"]
    serve = _default_servings(plan, prefs)

    scored = []
    for r in db.list_recipes(library_only=True):
        if r["id"] in seen or r["id"] in chosen:
            continue
        # Meal prep wants food that is made in bulk and keeps: recipes written
        # for it first, then anything that freezes or cooks in one pan or pot.
        if prep and not MEAL_PREP_TAGS & set(r["tags"]):
            continue
        costed = cost_recipe(r, cat, servings=serve, stores=stores, pantry=pantry)
        if costed["unavailable"]:
            continue
        weight = prefs.cuisines.get(r["cuisine"], 1)
        if weight <= 0:
            continue
        t = meets_targets(costed["per_serving"], prefs)
        score = weight * 2.0
        score += 1.5 if t["protein"] else -t["protein_gap"] / 10
        score += 1.0 if t["fiber"] else -t["fiber_gap"] / 5
        score -= 3.0 if r["id"] in recent else 0
        score -= 1.5 * chosen_cuisines.count(r["cuisine"])          # variety
        score += 0.75 * len(grocery.shared_perishables(r, pairs, cat))  # overlap
        score -= costed["cost_per_serving"] / 4
        if prep:
            score += 4.0 if "meal-prep" in r["tags"] else 0
        elif r["total_min"] > prefs.max_weeknight_min:
            score -= 0.5
        scored.append((score, r["id"]))
    scored.sort(reverse=True)
    picked = _diverse([rid for _, rid in scored], count)
    for rid in picked:
        db.add_candidate(plan["id"], rid, "library")
    out = get_plan(plan["id"])
    out["library_exhausted"] = len(picked) < count
    return out


def _diverse(ranked: list[int], count: int) -> list[int]:
    """Top picks, but no cuisine twice until each has had a turn."""
    cuisine = {rid: db.get_recipe(rid)["cuisine"] for rid in ranked}
    out, used = [], set()
    for rid in ranked:
        if len(out) >= count:
            break
        if cuisine[rid] not in used:
            out.append(rid)
            used.add(cuisine[rid])
    for rid in ranked:
        if len(out) >= count:
            break
        if rid not in out:
            out.append(rid)
    return out


def dismiss_candidate(plan_id: int, recipe_id: int) -> dict:
    plan = _plan_or_raise(plan_id)
    db.dismiss_candidate(plan["id"], recipe_id)
    return {"plan_id": plan["id"], "recipe_id": recipe_id, "dismissed": True}


def select_recipe(plan_id: int, recipe_id: int, servings: int | None = None) -> dict:
    """Choose a recipe for the week at `servings` (default: the preference for
    the plan's mode — default_servings, or prep_servings in meal-prep mode)."""
    plan = _plan_or_raise(plan_id)
    if not db.get_recipe(recipe_id):
        raise ToolError(f"No recipe {recipe_id}.")
    servings = int(_default_servings(plan) if servings is None else servings)
    if not 1 <= servings <= 24:
        raise ToolError("servings must be between 1 and 24")
    db.select_recipe(plan["id"], recipe_id, servings)
    db.set_in_library(recipe_id, True)   # chosen once, kept for good
    return get_plan(plan["id"])


def deselect_recipe(plan_id: int, recipe_id: int) -> dict:
    plan = _plan_or_raise(plan_id)
    if not db.deselect_recipe(plan["id"], recipe_id):
        raise ToolError("That recipe is not in this week's plan.")
    db.add_candidate(plan["id"], recipe_id, "library")
    return get_plan(plan["id"])


def set_servings(plan_id: int, recipe_id: int, servings: int) -> dict:
    plan = _plan_or_raise(plan_id)
    if not 1 <= int(servings) <= 24:
        raise ToolError("servings must be between 1 and 24")
    if not db.update_selection(plan["id"], recipe_id, servings=int(servings)):
        raise ToolError("That recipe is not in this week's plan.")
    return get_plan(plan["id"])


def set_cook_date(plan_id: int, recipe_id: int, cook_date: str | None) -> dict:
    """Pin the night a recipe is cooked; None lets the planner choose."""
    plan = _plan_or_raise(plan_id)
    if cook_date:
        d = _parse_date(cook_date)
        start = date.fromisoformat(plan["week_start"])
        if not start <= d < start + timedelta(days=7):
            raise ToolError("cook_date must fall inside the plan's week")
        cook_date = d.isoformat()
    if not db.update_selection(plan["id"], recipe_id, cook_date=cook_date):
        raise ToolError("That recipe is not in this week's plan.")
    return get_plan(plan["id"])


def get_grocery_list(plan_id: int | None = None) -> dict:
    """The week's shopping list in whole packages, by store and aisle, with
    pantry items set aside and perishable leftovers called out."""
    plan = _plan_or_raise(plan_id)
    sels = db.selections(plan["id"])
    recipes = _recipes_by_id({s["recipe_id"] for s in sels})
    out = grocery.build_list(_selected_pairs(plan, recipes), _catalog(), stores=plan["stores"],
                             pantry=db.pantry_ids(), checked=db.grocery_checks(plan["id"]))
    out["plan_id"] = plan["id"]
    out["week_start"] = plan["week_start"]
    return out


def check_grocery_item(plan_id: int, ingredient_id: str, checked: bool = True) -> dict:
    plan = _plan_or_raise(plan_id)
    db.set_grocery_check(plan["id"], ingredient_id, checked)
    return {"ingredient_id": ingredient_id, "checked": checked}


def plan_context(plan_id: int | None = None) -> dict:
    """What a recipe chef needs before suggesting anything for this week:
    preferences and targets, the stores, how many dinners the calendar leaves,
    what is already chosen, perishables that will be left over, and titles
    already offered (so as not to repeat them)."""
    plan = _plan_or_raise(plan_id)
    prefs = _prefs()
    full = get_plan(plan["id"])
    groceries = get_grocery_list(plan["id"])
    week = full["week"]
    offered = {c["recipe_id"] for c in db.candidates(plan["id"], include_dismissed=True)}
    titles = sorted({db.get_recipe(i)["title"] for i in offered if db.get_recipe(i)}
                    | {s["title"] for s in full["selected"]})
    library = sorted(r["title"] for r in db.list_recipes(library_only=True))
    return {
        "plan_id": plan["id"], "week_start": plan["week_start"],
        "stores": [{"id": s, "name": STORES[s]} for s in plan["stores"]],
        "household": prefs.household, "default_servings": _default_servings(plan, prefs),
        "meal_prep": plan["meal_prep"],
        **({"meal_prep_brief": (
            f"Meal-prep mode: the week is cooked in one batch session. Write each recipe for "
            f"{prefs.prep_servings} servings, from food that keeps {prefs.leftover_days} days in "
            "the fridge and freezes and reheats well (stews, chilis, curries, grain bowls, "
            "sheet-pan proteins; not crisp or delicate things). Prefer hands-off methods — "
            "sheet pan, big pot, oven, rice cooker — and bulk-friendly ingredients"
            + (" from Costco, which is among the stores" if "costco" in plan["stores"] else "")
            + ". Tag each recipe 'meal-prep', and 'freezer-friendly' when it is.")}
           if plan["meal_prep"] else {}),
        "recipes_wanted": plan["n_recipes"], "recipes_chosen": len(full["selected"]),
        "targets_per_serving": {"protein_g": prefs.protein_g, "fiber_g": prefs.fiber_g,
                                "kcal": prefs.kcal},
        "cuisine_weights": {CUISINES[k]: v for k, v in prefs.cuisines.items() if k in CUISINES},
        "avoid": prefs.avoid, "equipment": prefs.equipment,
        "max_weeknight_min": prefs.max_weeknight_min, "notes": prefs.notes,
        "days": [{"date": d["date"], "dow": d["dow"], "status": d["status"],
                  "events": [e["summary"] for e in d["events"]]} for d in week["days"]],
        "chosen": [{"title": s["title"], "cuisine": s["cuisine_name"], "servings": s["servings"],
                    "protein": s["per_serving"]["protein"], "fiber": s["per_serving"]["fiber"]}
                   for s in full["selected"]],
        "week_nutrition_per_serving": full["totals"]["per_serving"],
        "use_up": [f"{x['name']} (~{round(x['fraction'] * 100)}% of the package left)"
                   for x in groceries["leftovers"]],
        "pantry": [p["name"] for p in get_pantry()],
        "already_offered": titles,
        "in_library": library,
    }


def calendar_ics(plan_id: int | None = None) -> str:
    """An .ics file with a cook block for each chosen recipe."""
    plan = _plan_or_raise(plan_id)
    week = get_week(plan["id"])
    starts = {c["recipe_id"]: (d["date"], c["start"]) for d in week["days"] for c in d["cook"]}
    events = []
    for s in db.selections(plan["id"]):
        if s["recipe_id"] not in starts:
            continue
        when, at = starts[s["recipe_id"]]
        r = get_recipe(s["recipe_id"], servings=s["servings"], plan_id=plan["id"])
        start = datetime.combine(date.fromisoformat(when), schedule.parse_hhmm(at))
        end = start + timedelta(minutes=r["total_min"])
        lines = "\n".join(f"• {l['amount']} {l['name']}" + (f", {l['prep']}" if l["prep"] else "")
                          + (" (optional)" if l["optional"] else "") for l in r["lines"])
        steps = "\n".join(f"{i}. {st}" for i, st in enumerate(r["steps"], 1))
        desc = (f"{r['servings']} servings · {r['active_min']} min active, {r['total_min']} min total"
                f" · ${r['cost_per_serving']:.2f}/serving · {r['per_serving']['protein']:.0f} g protein,"
                f" {r['per_serving']['fiber']:.0f} g fiber per serving\n\nIngredients\n{lines}"
                f"\n\nSteps\n{steps}")
        verb = "Meal prep" if plan["meal_prep"] else "Cook"
        events.append({"uid": f"sous-chef-{plan['id']}-{s['recipe_id']}@sous-chef",
                       "start": start, "end": end, "summary": f"{verb}: {r['title']}",
                       "description": desc})
    return calendars.cook_events_ics(events, calendar_name="Meal plan")
