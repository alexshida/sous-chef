"""The tool layer: the plan lifecycle, and recipes written by the model."""

import pytest

from sous_chef import tools
from sous_chef.storage import db

GOOD = {
    "title": "Chickpea & Spinach Stew", "cuisine": "mediterranean",
    "summary": "A quick stew.", "servings": 4, "active_min": 15, "total_min": 25,
    "ingredients": [{"id": "chickpeas", "qty": 2, "unit": "cans", "prep": "drained"},
                    {"id": "baby-spinach", "qty": 6, "unit": "oz"},
                    {"id": "olive-oil", "qty": 2, "unit": "tbsp"}],
    "steps": ["Warm the oil.", "Add chickpeas and spinach."], "tags": ["Vegetarian"],
}


@pytest.fixture
def plan(fresh_db):
    return tools.new_plan(week_start="2026-10-05", n_recipes=3)


def test_a_new_plan_uses_preferences(plan):
    assert plan["stores"] == ["tj"] and plan["n_recipes"] == 3
    assert len(plan["week"]["days"]) == 7 and plan["selected"] == []


def test_library_suggestions_fit_the_stores_and_vary_by_cuisine(plan):
    p = tools.suggest_from_library(plan["id"], 4)
    assert len(p["candidates"]) == 4
    assert all(not c["unavailable"] for c in p["candidates"])
    assert len({c["cuisine"] for c in p["candidates"]}) == 4


def test_refreshing_offers_different_recipes(plan):
    first = {c["id"] for c in tools.suggest_from_library(plan["id"], 4)["candidates"]}
    second = {c["id"] for c in tools.suggest_from_library(plan["id"], 4)["candidates"]}
    assert first.isdisjoint(second)


def test_the_library_runs_out_honestly(plan):
    for _ in range(6):
        p = tools.suggest_from_library(plan["id"], 4)
    assert p["library_exhausted"]


def test_selecting_and_scaling_flows_through_to_totals(plan):
    rid = tools.suggest_from_library(plan["id"], 3)["candidates"][0]["id"]
    p = tools.select_recipe(plan["id"], rid)
    assert p["selected"][0]["servings"] == 4 and p["totals"]["servings"] == 4
    bill4 = p["totals"]["grocery_bill"]
    p = tools.set_servings(plan["id"], rid, 8)
    assert p["totals"]["servings"] == 8 and p["totals"]["grocery_bill"] >= bill4
    assert p["selected"][0]["cook_date"] == "2026-10-05"
    assert all(c["id"] != rid for c in p["candidates"])


def test_candidates_report_what_they_add_to_the_bill(plan):
    p = tools.suggest_from_library(plan["id"], 4)
    tools.select_recipe(plan["id"], p["candidates"][0]["id"])
    p = tools.get_plan(plan["id"])
    for c in p["candidates"]:
        assert 0 < c["adds_to_bill"]


def test_cook_dates_must_be_in_the_week(plan):
    rid = tools.suggest_from_library(plan["id"], 1)["candidates"][0]["id"]
    tools.select_recipe(plan["id"], rid)
    assert tools.set_cook_date(plan["id"], rid, "2026-10-08")["selected"][0]["cook_date"] == "2026-10-08"
    with pytest.raises(tools.ToolError):
        tools.set_cook_date(plan["id"], rid, "2026-10-20")


def test_servings_are_bounded(plan):
    rid = tools.suggest_from_library(plan["id"], 1)["candidates"][0]["id"]
    with pytest.raises(tools.ToolError):
        tools.select_recipe(plan["id"], rid, 0)


def test_day_status_overrides_and_reverts(plan):
    w = tools.set_day_status(plan["id"], "2026-10-07", "out")
    assert w["days"][2]["status"] == "out"
    w = tools.set_day_status(plan["id"], "2026-10-07", None)
    assert w["days"][2]["status"] == "home"
    with pytest.raises(tools.ToolError):
        tools.set_day_status(plan["id"], "2026-10-07", "partying")


def test_proposed_recipe_is_costed_and_offered(plan):
    out = tools.propose_recipe(GOOD, plan["id"])
    assert out["cost_per_serving"] > 0 and out["per_serving"]["fiber"] > 5
    p = tools.get_plan(plan["id"])
    cand = next(c for c in p["candidates"] if c["id"] == out["id"])
    assert cand["origin"] == "claude" and cand["tags"] == ["vegetarian"]
    # a plain suggestion joins the library only once chosen
    assert not db.get_recipe(out["id"])["in_library"]
    tools.select_recipe(plan["id"], out["id"])
    assert db.get_recipe(out["id"])["in_library"]


def test_crafted_and_imported_recipes_go_straight_to_the_library(fresh_db):
    out = tools.propose_recipe(GOOD, None, "import")
    assert db.get_recipe(out["id"])["in_library"]


def test_an_unknown_ingredient_names_close_matches(plan):
    bad = {**GOOD, "ingredients": [{"id": "garbanzo-beans", "qty": 1, "unit": "can"},
                                   {"id": "spinach", "qty": 1, "unit": "cup"}]}
    with pytest.raises(tools.ToolError) as e:
        tools.propose_recipe(bad, plan["id"])
    msg = str(e.value)
    assert "garbanzo-beans" in msg and "baby-spinach" in msg


def test_a_wrong_unit_names_the_units_that_work(plan):
    bad = {**GOOD, "ingredients": [{"id": "chicken-thigh", "qty": 2, "unit": "cup"}]}
    with pytest.raises(tools.ToolError) as e:
        tools.propose_recipe(bad, plan["id"])
    assert "lb" in str(e.value) and "each" in str(e.value)


def test_recipe_shape_is_validated(plan):
    with pytest.raises(tools.ToolError) as e:
        tools.propose_recipe({**GOOD, "servings": 0, "steps": [], "total_min": 5,
                              "active_min": 10, "cuisine": "martian"}, plan["id"])
    msg = str(e.value)
    for part in ("servings", "steps", "total_min", "cuisine"):
        assert part in msg


def test_check_recipe_does_not_save(plan):
    before = len(db.list_recipes(library_only=False))
    out = tools.check_recipe(GOOD, plan_id=plan["id"])
    assert out["per_serving"]["fiber"] > 5 and "fiber" in out["targets"]
    assert len(db.list_recipes(library_only=False)) == before


def test_a_missing_ingredient_can_be_added_then_used(plan):
    ing = tools.add_ingredient("Doubanjiang (chili bean paste)", "oils & condiments",
                               180, 10, 20, 7, 5,
                               [{"store": "qfc", "product": "Lee Kum Kee Chili Bean Sauce",
                                 "pkg_qty": 13, "pkg_unit": "oz", "price": 4.99}],
                               g_per_cup=270)
    assert ing["id"] == "doubanjiang-chili-bean-paste" and ing["offers"][0]["source"] == "claude"
    with pytest.raises(tools.ToolError):
        tools.add_ingredient("Doubanjiang (chili bean paste)", "oils & condiments", 1, 1, 1, 1, 1,
                             [{"store": "qfc", "pkg_qty": 1, "pkg_unit": "oz", "price": 1}])
    recipe = {**GOOD, "ingredients": [{"id": ing["id"], "qty": 2, "unit": "tbsp"}]}
    assert tools.propose_recipe(recipe, plan["id"])["unavailable"] == ["Doubanjiang (chili bean paste)"]


@pytest.mark.parametrize("kwargs,needle", [
    (dict(aisle="garage"), "aisle"),
    (dict(protein=80, carbs=40), "exceed"),
    (dict(offers=[]), "offer"),
    (dict(offers=[{"store": "walmart", "pkg_qty": 1, "pkg_unit": "lb", "price": 3}]), "store"),
])
def test_bad_new_ingredients_are_refused(fresh_db, kwargs, needle):
    args = dict(name="Mystery", aisle="produce", kcal=50, protein=2, carbs=10, fat=1, fiber=2,
                offers=[{"store": "tj", "pkg_qty": 1, "pkg_unit": "lb", "price": 2}])
    args.update(kwargs)
    with pytest.raises(tools.ToolError) as e:
        tools.add_ingredient(**args)
    assert needle in str(e.value)


def test_your_price_changes_what_recipes_cost(plan):
    before = tools.check_recipe(GOOD)["total_cost"]
    tools.set_price("chickpeas", "tj", 0.79)
    after = tools.check_recipe(GOOD)["total_cost"]
    assert after == pytest.approx(before - 2 * 0.50, abs=0.01)
    assert tools.get_ingredient("chickpeas")["offers"][0]["source"] == "user"


def test_not_carried_moves_the_item_to_the_next_store(plan):
    tools.update_plan(plan["id"], stores=["tj", "qfc"])
    tools.set_carried("baby-spinach", "tj", False)
    out = tools.check_recipe(GOOD, plan_id=plan["id"])
    assert out["stores"] == ["tj", "qfc"]


def test_preferences_are_validated(fresh_db):
    with pytest.raises(tools.ToolError):
        tools.update_preferences({"stores": ["walmart"]})
    with pytest.raises(tools.ToolError):
        tools.update_preferences({"dinner_time": "late"})
    with pytest.raises(tools.ToolError):
        tools.update_preferences({"favourite_colour": "orange"})
    assert tools.update_preferences({"household": 2, "stores": ["qfc", "tj"]})["household"] == 2


def test_plan_context_tells_the_chef_what_it_needs(plan):
    p = tools.suggest_from_library(plan["id"], 3)
    tools.select_recipe(plan["id"], p["candidates"][0]["id"])
    ctx = tools.plan_context(plan["id"])
    assert ctx["recipes_wanted"] == 3 and ctx["recipes_chosen"] == 1
    assert ctx["targets_per_serving"]["protein_g"] == 35
    assert len(ctx["days"]) == 7 and ctx["already_offered"]
    assert isinstance(ctx["use_up"], list) and "Kosher salt" in ctx["pantry"]


def test_calendar_export_has_one_block_per_recipe(plan):
    p = tools.suggest_from_library(plan["id"], 2)
    for c in p["candidates"]:
        tools.select_recipe(plan["id"], c["id"])
    ics = tools.calendar_ics(plan["id"])
    assert ics.count("BEGIN:VEVENT") == 2 and "SUMMARY:Cook: " in ics


def test_no_plan_is_a_readable_error(fresh_db):
    with pytest.raises(tools.ToolError) as e:
        tools.get_plan()
    assert "new_plan" in str(e.value)


def test_a_twin_of_an_existing_ingredient_is_refused(fresh_db):
    offers = [{"store": "tj", "pkg_qty": 8, "pkg_unit": "fl oz", "price": 3.99}]
    with pytest.raises(tools.ToolError) as e:
        tools.add_ingredient("Fish sauce, Thai", "oils & condiments", 35, 5, 4, 0, 0, offers,
                             g_per_cup=288)
    assert "fish-sauce (Fish sauce)" in str(e.value) and "not_duplicate" in str(e.value)
    # brown sugar really is different from sugar, and says so
    with pytest.raises(tools.ToolError):
        tools.add_ingredient("Brown sugar", "baking", 380, 0, 98, 0, 0,
                             [{"store": "tj", "pkg_qty": 1, "pkg_unit": "lb", "price": 2.99}])
    added = tools.add_ingredient("Brown sugar", "baking", 380, 0, 98, 0, 0,
                                 [{"store": "tj", "pkg_qty": 1, "pkg_unit": "lb", "price": 2.99}],
                                 not_duplicate=True)
    assert added["id"] == "brown-sugar"


def test_a_proposed_recipe_reports_the_servings_it_was_written_for(plan):
    out = tools.propose_recipe({**GOOD, "servings": 3}, plan["id"])
    assert out["servings"] == 3


# ── duplicates ───────────────────────────────────────────────

def test_the_same_suggestion_saved_twice_is_one_recipe(plan):
    first = tools.propose_recipe(GOOD, plan["id"])
    again = tools.propose_recipe(GOOD, plan["id"])
    assert again["id"] == first["id"] and again["already_saved"]
    titles = [c["title"] for c in tools.get_plan(plan["id"])["candidates"]]
    assert titles.count(GOOD["title"]) == 1


def test_a_reused_title_is_refused_with_advice_for_the_origin(plan):
    tools.propose_recipe(GOOD, plan["id"])
    variant = {**GOOD, "title": "chickpea and spinach stew!",
               "ingredients": GOOD["ingredients"][:2]}
    with pytest.raises(tools.ToolError) as e:
        tools.propose_recipe(variant, plan["id"])
    assert "already offered this week" in str(e.value) and "different dish" in str(e.value)
    with pytest.raises(tools.ToolError) as e:
        tools.propose_recipe({**variant}, plan["id"], "import")
    assert "title that tells them apart" in str(e.value)


def test_a_title_in_the_library_counts(plan):
    seeded = db.list_recipes()[0]
    clash = {**GOOD, "title": seeded["title"]}
    with pytest.raises(tools.ToolError) as e:
        tools.propose_recipe(clash, plan["id"])
    assert "in the library" in str(e.value)


def test_existing_duplicates_merge_into_the_chosen_copy(plan):
    clean = tools._validate_recipe(GOOD, db.all_ingredients())
    a = db.insert_recipe(clean, source="claude", in_library=False)
    b = db.insert_recipe(clean, source="claude", in_library=False)
    c = db.insert_recipe(clean, source="claude", in_library=False)
    db.add_candidate(plan["id"], a, "claude")
    db.add_candidate(plan["id"], b, "claude")
    db.dismiss_candidate(plan["id"], a)
    tools.select_recipe(plan["id"], c, 6)

    found = tools.find_duplicate_recipes()
    assert found == [{"title": GOOD["title"], "keep": c, "remove": [a, b]}]
    assert db.get_recipe(a)                              # a dry run changes nothing

    tools.merge_duplicate_recipes(apply=True)
    assert db.get_recipe(a) is None and db.get_recipe(b) is None
    p = tools.get_plan(plan["id"])
    assert [s["id"] for s in p["selected"]] == [c] and p["selected"][0]["servings"] == 6
    assert tools.find_duplicate_recipes() == []


def test_merging_keeps_a_copy_visible_if_either_was(plan):
    clean = tools._validate_recipe(GOOD, db.all_ingredients())
    a = db.insert_recipe(clean, source="claude", in_library=True)
    b = db.insert_recipe(clean, source="claude", in_library=False)
    db.add_candidate(plan["id"], a, "claude")
    db.add_candidate(plan["id"], b, "claude")
    db.dismiss_candidate(plan["id"], a)
    tools.merge_duplicate_recipes(apply=True)
    assert [c["id"] for c in tools.get_plan(plan["id"])["candidates"]] == [a]
