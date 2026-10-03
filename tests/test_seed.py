"""The seeded catalog and starter recipes hold together, and reseeding never
overwrites what you have set."""

from sous_chef.catalog import recipes as seed_recipes
from sous_chef.catalog import seed
from sous_chef.costing import cost_recipe
from sous_chef.storage import db
from sous_chef.units import to_grams

# Starter recipes that need something Trader Joe's does not carry. Kept as an
# explicit list so a catalog change that breaks TJ-only shopping is noticed.
NOT_TJ_ONLY = {"red-lentil-dal": {"Red lentils, dry"}, "palak-paneer": {"Paneer"}}


def test_nutrition_is_plausible(catalog):
    for ing in catalog.values():
        values = [ing[k] for k in ("kcal", "protein", "carbs", "fat", "fiber")]
        assert all(v >= 0 for v in values), ing["id"]
        assert ing["protein"] + ing["carbs"] + ing["fat"] <= 101, ing["id"]


def test_every_offer_converts_and_has_a_price(catalog):
    for ing in catalog.values():
        for o in ing["offers"].values():
            assert o["price"] > 0
            assert to_grams(o["pkg_qty"], o["pkg_unit"], ing) > 0, (ing["id"], o["store"])


def test_every_starter_recipe_uses_real_ingredients_and_units(catalog):
    for r in db.list_recipes():
        for line in r["ingredients"]:
            assert line["id"] in catalog, (r["title"], line["id"])
            to_grams(line["qty"], line["unit"], catalog[line["id"]])


def test_starter_recipes_can_be_shopped_at_trader_joes(catalog):
    for r in db.list_recipes():
        costed = cost_recipe(r, catalog, stores=["tj"])
        key = next(s["key"] for s in seed_recipes.RECIPES if s["title"] == r["title"])
        assert set(costed["unavailable"]) == NOT_TJ_ONLY.get(key, set()), r["title"]


def test_starter_recipes_are_sane_meals(catalog):
    for r in db.list_recipes():
        c = cost_recipe(r, catalog, stores=["tj", "qfc"])
        assert 300 <= c["per_serving"]["kcal"] <= 950, (r["title"], c["per_serving"])
        assert 1.5 <= c["cost_per_serving"] <= 8, (r["title"], c["cost_per_serving"])
        assert c["per_serving"]["protein"] >= 18, r["title"]


def test_the_library_covers_every_requested_cuisine():
    cuisines = {r["cuisine"] for r in seed_recipes.RECIPES}
    assert {"east_asian", "indian", "pasta", "mediterranean"} <= cuisines


def test_reseeding_keeps_your_prices_and_not_carried(fresh_db):
    db.upsert_offer("chickpeas", "tj", price=0.99, pkg_qty=1, pkg_unit="can",
                    product="Garbanzo", source="user")
    db.set_carried("miso", "tj", False)
    db.init_db()
    ings = db.all_ingredients()
    assert ings["chickpeas"]["offers"]["tj"]["price"] == 0.99
    assert ings["chickpeas"]["offers"]["tj"]["source"] == "user"
    assert ings["miso"]["offers"]["tj"]["carried"] == 0


def test_reseeding_does_not_refill_the_pantry(fresh_db):
    db.set_pantry("salt", False)
    db.init_db()
    assert "salt" not in db.pantry_ids()


def test_reseeding_does_not_burn_recipe_ids(fresh_db):
    before = max(r["id"] for r in db.list_recipes())
    db.init_db()
    db.init_db()
    rid = db.insert_recipe({"title": "x", "cuisine": "other", "servings": 1, "active_min": 1,
                            "total_min": 1, "ingredients": [], "steps": ["x"]},
                           source="craft", in_library=True)
    assert rid == before + 1


def test_scaled_offers_use_generic_names_not_trader_joes_labels():
    for o in seed.offers():
        if o["store"] != "tj":
            assert "Trader Joe" not in o["product"]


def test_costco_sells_bulk_packs_not_scaled_guesses(catalog):
    costco = {iid: ing["offers"]["costco"] for iid, ing in catalog.items() if "costco" in ing["offers"]}
    assert len(costco) >= 40
    assert all(o["source"] == "seed" for o in costco.values())
    # bulk is cheaper per gram than Trader Joe's for the staples meal prep runs on
    for iid in ("chicken-thigh", "chicken-breast", "quinoa", "black-beans"):
        ing = catalog[iid]
        per_g = {s: o["price"] / to_grams(o["pkg_qty"], o["pkg_unit"], ing)
                 for s, o in ing["offers"].items() if s in ("tj", "costco")}
        assert per_g["costco"] < per_g["tj"], iid


def test_rotisserie_chicken_is_a_costco_staple_not_a_trader_joes_one(catalog):
    offers = catalog["rotisserie-chicken"]["offers"]
    assert offers["costco"]["price"] == 4.99 and "tj" not in offers
