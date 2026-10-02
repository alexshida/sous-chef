"""The shopping list: whole packages, one trip where possible, overlap rewarded."""

import pytest

from sous_chef import grocery

from .conftest import recipe

HALF_CILANTRO = recipe("A", ingredients=[("cilantro", 0.5, "bunch"), ("chickpeas", 1, "can")])
OTHER_HALF = recipe("B", ingredients=[("cilantro", 0.5, "bunch"), ("black-beans", 1, "can")])


def _items(gl):
    return {i["id"]: i for s in gl["stores"] for a in s["aisles"] for i in a["items"]}


def test_shared_ingredients_are_bought_once(catalog):
    gl = grocery.build_list([(HALF_CILANTRO, 4), (OTHER_HALF, 4)], catalog, stores=["tj"])
    cilantro = _items(gl)["cilantro"]
    assert cilantro["packages"] == 1 and cilantro["cost"] == pytest.approx(1.49)
    assert cilantro["need"] == "1 bunch"
    assert {u["recipe"] for u in cilantro["uses"]} == {"A", "B"}


def test_amounts_round_up_to_whole_packages(catalog):
    r = recipe(ingredients=[("chicken-thigh", 2, "lb")])
    item = _items(grocery.build_list([(r, 4)], catalog, stores=["tj"]))["chicken-thigh"]
    assert item["packages"] == 2           # sold in ~1.5 lb packs
    assert item["leftover_g"] == pytest.approx(453.592, abs=1)


def test_overlap_makes_a_recipe_cheaper_to_add(catalog):
    chosen = [(HALF_CILANTRO, 4)]
    alone = grocery.bill([(OTHER_HALF, 4)], catalog, stores=["tj"])
    added = grocery.marginal_cost((OTHER_HALF, 4), chosen, catalog, stores=["tj"])
    assert added == pytest.approx(alone - 1.49)
    assert grocery.shared_perishables(OTHER_HALF, chosen, catalog) == ["Cilantro"]


def test_bill_matches_the_list_total(catalog):
    from sous_chef.storage import db
    pairs = [(r, 4) for r in db.list_recipes()[:5]]
    gl = grocery.build_list(pairs, catalog, stores=["tj", "qfc"], pantry={"salt"})
    assert grocery.bill(pairs, catalog, stores=["tj", "qfc"], pantry={"salt"}) == pytest.approx(gl["total"])
    assert gl["total"] == pytest.approx(sum(s["total"] for s in gl["stores"]))


def test_pantry_items_stay_off_the_list(catalog):
    r = recipe(ingredients=[("olive-oil", 2, "tbsp"), ("chickpeas", 1, "can")])
    gl = grocery.build_list([(r, 4)], catalog, stores=["tj"], pantry={"olive-oil"})
    assert "olive-oil" not in _items(gl)
    assert [p["id"] for p in gl["pantry"]] == ["olive-oil"]
    assert gl["total"] == pytest.approx(1.29)


def test_each_item_goes_to_the_first_store_that_carries_it(catalog):
    r = recipe(ingredients=[("paneer", 12, "oz"), ("chickpeas", 1, "can")])
    gl = grocery.build_list([(r, 4)], catalog, stores=["tj", "qfc"])
    assert [s["store"] for s in gl["stores"]] == ["tj", "qfc"]
    assert _items(gl)["paneer"]["store"] == "qfc" and _items(gl)["chickpeas"]["store"] == "tj"


def test_unavailable_items_say_where_they_can_be_found(catalog):
    r = recipe(ingredients=[("paneer", 12, "oz")])
    gl = grocery.build_list([(r, 4)], catalog, stores=["tj"])
    assert gl["unavailable"][0]["name"] == "Paneer"
    assert set(gl["unavailable"][0]["elsewhere"]) == {"QFC", "PCC"}


def test_a_store_marked_not_carrying_is_skipped(fresh_db):
    from sous_chef.storage import db
    db.set_carried("miso", "tj", False)
    catalog = db.all_ingredients()
    r = recipe(ingredients=[("miso", 2, "tbsp")])
    gl = grocery.build_list([(r, 4)], catalog, stores=["tj", "qfc"])
    assert _items(gl)["miso"]["store"] == "qfc"


def test_perishable_leftovers_and_stock_ups_are_called_out(catalog):
    r = recipe(ingredients=[("cilantro", 0.25, "bunch"), ("cumin", 1, "tsp"),
                            ("chickpeas", 1, "can")])
    gl = grocery.build_list([(r, 4)], catalog, stores=["tj"])
    assert [x["id"] for x in gl["leftovers"]] == ["cilantro"]
    assert "cumin" in gl["stock_up"] and "chickpeas" not in gl["stock_up"]
    assert gl["stock_up_value"] > 2


def test_optional_items_are_listed_but_not_totalled(catalog):
    r = recipe(ingredients=[("chickpeas", 1, "can"), ("naan", 4, "each", True)])
    gl = grocery.build_list([(r, 4)], catalog, stores=["tj"])
    assert _items(gl)["naan"]["optional"]
    assert gl["total"] == pytest.approx(1.29)


def test_aisles_follow_a_walk_through_the_store(catalog):
    r = recipe(ingredients=[("cumin", 1, "tsp"), ("chickpeas", 1, "can"), ("onion", 1, "each")])
    aisles = [a["aisle"] for a in grocery.build_list([(r, 4)], catalog, stores=["tj"])["stores"][0]["aisles"]]
    assert aisles == ["produce", "canned & jarred", "spices"]
