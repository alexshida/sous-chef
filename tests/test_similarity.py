"""Near-copy detection: the same dish under another name is still the same dish."""

import copy
from itertools import combinations

import pytest

from sous_chef import similarity, tools
from sous_chef.storage import db


def _recipe(title_start):
    return next(r for r in db.list_recipes() if r["title"].startswith(title_start))


def _variant(r, title, drop=0, swap=None, add=None, scale=1.0):
    """A recipe changed the way a model rewrites one: new name, a few edits."""
    v = copy.deepcopy(r)
    v.update(title=title, id=None)
    lines = v["ingredients"][:len(v["ingredients"]) - drop] if drop else v["ingredients"]
    for line in lines:
        line["qty"] = round(line["qty"] * scale, 2)
        if swap and line["id"] == swap[0]:
            line["id"] = swap[1]
    if add:
        lines.append({"prep": "", "optional": False, **add})
    v["ingredients"] = lines
    return v


def test_no_two_starter_recipes_look_alike(catalog):
    recipes = db.list_recipes()
    for a, b in combinations(recipes, 2):
        assert not similarity.compare(a, b, catalog)["same"], (a["title"], b["title"])


@pytest.mark.parametrize("start,variant", [
    ("Gochujang", dict(title="Spicy Gochujang Chicken Rice Bowls", drop=3,
                       swap=("chicken-thigh", "chicken-breast"))),
    ("Chana Masala", dict(title="Spinach Chickpea Curry", drop=4,
                          add={"id": "coconut-milk-light", "qty": 0.5, "unit": "can"})),
    ("Turkey Bolognese", dict(title="Beef Ragù with Lentil Pasta",
                              swap=("ground-turkey", "ground-beef"))),
    ("Rotisserie Chicken Burrito", dict(title="Chicken Burrito Bowl Meal Prep",
                                        swap=("jasmine-rice", "brown-rice"), scale=1.3)),
])
def test_near_copies_under_new_names_are_caught(catalog, start, variant):
    original = _recipe(start)
    hit = similarity.most_similar(_variant(original, **variant), db.list_recipes(), catalog)
    assert hit and hit[0]["id"] == original["id"]


def test_same_base_different_sauce_is_a_different_dinner(catalog):
    c = similarity.compare(_recipe("Gochujang"), _recipe("Teriyaki"), catalog)
    assert c["ingredients"] > 0.7 and not c["same"]


# ── in the tool layer ────────────────────────────────────────

@pytest.fixture
def plan(fresh_db):
    return tools.new_plan(week_start="2026-10-05")


def _as_proposal(r):
    return {k: r[k] for k in ("title", "cuisine", "summary", "servings", "active_min",
                              "total_min", "ingredients", "steps", "tags")}


def test_claude_cannot_propose_a_near_copy(plan):
    copy_of = _variant(_recipe("Turkey Bolognese"), "Beef Ragù with Lentil Pasta",
                       swap=("ground-turkey", "ground-beef"))
    with pytest.raises(tools.ToolError) as e:
        tools.propose_recipe(_as_proposal(copy_of), plan["id"])
    assert "Too close to 'Turkey Bolognese" in str(e.value) and "different dish" in str(e.value)


def test_a_crafted_near_copy_is_kept_but_flagged(plan):
    copy_of = _variant(_recipe("Turkey Bolognese"), "Weeknight Beef Ragù",
                       swap=("ground-turkey", "ground-beef"))
    out = tools.propose_recipe(_as_proposal(copy_of), plan["id"], "craft")
    card = next(c for c in tools.get_plan(plan["id"])["candidates"] if c["id"] == out["id"])
    assert card["clash"]["title"].startswith("Turkey Bolognese") and not card["in_library"]


def test_saving_a_near_copy_asks_first(plan):
    copy_of = _variant(_recipe("Chana Masala"), "Spinach Chickpea Curry", drop=4,
                       add={"id": "coconut-milk-light", "qty": 0.5, "unit": "can"})
    rid = tools.propose_recipe(_as_proposal(copy_of), plan["id"], "craft")["id"]
    with pytest.raises(tools.ClashError) as e:
        tools.save_to_library(rid)
    assert e.value.clash["title"].startswith("Chana Masala")
    assert not db.get_recipe(rid)["in_library"]


def test_save_as_new_keeps_both(plan):
    copy_of = _variant(_recipe("Chana Masala"), "Spinach Chickpea Curry", drop=4,
                       add={"id": "coconut-milk-light", "qty": 0.5, "unit": "can"})
    rid = tools.propose_recipe(_as_proposal(copy_of), plan["id"], "craft")["id"]
    tools.save_to_library(rid, on_clash="new")
    assert db.get_recipe(rid)["in_library"] and _recipe("Chana Masala")["in_library"]


def test_replacing_a_starter_recipe_hides_it_for_good(plan):
    original = _recipe("Chana Masala")
    copy_of = _variant(original, "Spinach Chickpea Curry", drop=4,
                       add={"id": "coconut-milk-light", "qty": 0.5, "unit": "can"})
    rid = tools.propose_recipe(_as_proposal(copy_of), plan["id"], "craft")["id"]
    assert tools.save_to_library(rid, on_clash="replace")["replaced"] == original["id"]
    db.init_db()                                     # reseeding does not bring it back
    titles = [r["title"] for r in db.list_recipes()]
    assert "Spinach Chickpea Curry" in titles and original["title"] not in titles


def test_replacing_a_saved_recipe_moves_its_place_in_plans(plan):
    starter = _recipe("Gochujang")
    tools.save_to_library(starter["id"], keep=False)  # so v1 is the only one like it
    first = tools.propose_recipe({**_as_proposal(starter), "title": "Gochujang Chicken v1"},
                                 None, "import")
    assert db.get_recipe(first["id"])["in_library"]
    tools.select_recipe(plan["id"], first["id"], 6)
    second = _variant(starter, "Gochujang Chicken v2", drop=1)
    rid = tools.propose_recipe(_as_proposal(second), None, "craft")["id"]
    tools.save_to_library(rid, on_clash="replace")
    assert db.get_recipe(first["id"]) is None
    assert [(s["id"], s["servings"]) for s in tools.get_plan(plan["id"])["selected"]] == [(rid, 6)]


def test_library_picks_never_offer_two_look_alikes(plan):
    original = _recipe("Turkey Bolognese")
    twin = _variant(original, "Beef Ragù with Lentil Pasta", swap=("ground-turkey", "ground-beef"))
    rid = tools.propose_recipe(_as_proposal(twin), None, "craft")["id"]
    tools.save_to_library(rid, on_clash="new")
    for _ in range(8):
        ids = {c["id"] for c in tools.fresh_suggestions(plan["id"], library=6)["candidates"]}
        assert not {original["id"], rid} <= ids


def test_dedupe_lists_look_alikes(plan):
    twin = _variant(_recipe("Turkey Bolognese"), "Beef Ragù with Lentil Pasta",
                    swap=("ground-turkey", "ground-beef"))
    rid = tools.propose_recipe(_as_proposal(twin), None, "craft")["id"]
    tools.save_to_library(rid, on_clash="new")
    pairs = tools.find_lookalike_recipes()
    assert any(rid in (p["a"]["id"], p["b"]["id"]) for p in pairs)
