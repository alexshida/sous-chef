"""Recipe cost and nutrition: what the food you use costs, per serving."""

import pytest

from sous_chef.costing import cost_recipe

from .conftest import recipe


def test_hand_computed_example(catalog):
    r = recipe(ingredients=[("chickpeas", 2, "can")], servings=2)
    c = cost_recipe(r, catalog, stores=["tj"])
    assert c["total_cost"] == pytest.approx(2.58)
    assert c["cost_per_serving"] == pytest.approx(1.29)
    # 2 cans × 250 g drained × 7 g protein per 100 g, over 2 servings
    assert c["per_serving"]["protein"] == pytest.approx(17.5)


def test_scaling_keeps_per_serving_numbers(catalog):
    r = recipe(ingredients=[("chicken-thigh", 1.5, "lb"), ("jasmine-rice", 1.5, "cup"),
                            ("broccoli", 12, "oz")])
    four = cost_recipe(r, catalog, servings=4, stores=["tj"])
    eight = cost_recipe(r, catalog, servings=8, stores=["tj"])
    assert eight["total_cost"] == pytest.approx(four["total_cost"] * 2, abs=0.02)
    assert eight["cost_per_serving"] == pytest.approx(four["cost_per_serving"], abs=0.01)
    assert eight["per_serving"] == pytest.approx(four["per_serving"], abs=0.2)
    assert eight["lines"][0]["amount"] == "3 lb"


def test_part_of_a_package_costs_part_of_its_price(catalog):
    r = recipe(ingredients=[("cilantro", 0.5, "bunch")], servings=1)
    assert cost_recipe(r, catalog, stores=["tj"])["total_cost"] == pytest.approx(1.49 / 2, abs=0.006)


def test_optional_items_are_priced_but_left_out_of_totals(catalog):
    r = recipe(ingredients=[("chickpeas", 1, "can"), ("naan", 4, "each", True)])
    c = cost_recipe(r, catalog, stores=["tj"])
    naan = next(l for l in c["lines"] if l["id"] == "naan")
    assert naan["cost"] > 0 and naan["optional"]
    assert c["total_cost"] == pytest.approx(1.29)


def test_pantry_items_count_toward_cost_and_are_flagged(catalog):
    r = recipe(ingredients=[("olive-oil", 2, "tbsp")], servings=1)
    c = cost_recipe(r, catalog, stores=["tj"], pantry={"olive-oil"})
    assert c["lines"][0]["pantry"] and c["total_cost"] > 0


def test_unavailable_ingredients_are_named(catalog):
    r = recipe(ingredients=[("paneer", 12, "oz")])
    assert cost_recipe(r, catalog, stores=["tj"])["unavailable"] == ["Paneer"]
    assert cost_recipe(r, catalog, stores=["tj", "qfc"])["unavailable"] == []


def test_store_priority_decides_the_price(catalog):
    r = recipe(ingredients=[("chickpeas", 1, "can")], servings=1)
    tj_first = cost_recipe(r, catalog, stores=["tj", "pcc"])
    pcc_first = cost_recipe(r, catalog, stores=["pcc", "tj"])
    assert tj_first["lines"][0]["store"] == "tj" and pcc_first["lines"][0]["store"] == "pcc"
    assert pcc_first["total_cost"] > tj_first["total_cost"]


def test_seed_prices_are_marked_as_estimates(catalog):
    r = recipe(ingredients=[("chickpeas", 1, "can")])
    assert cost_recipe(r, catalog, stores=["tj"])["estimated"]


def test_zero_servings_is_refused(catalog):
    with pytest.raises(ValueError):
        cost_recipe(recipe(ingredients=[("eggs", 2, "each")]), catalog, servings=0, stores=["tj"])
