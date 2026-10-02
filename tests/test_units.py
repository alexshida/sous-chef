"""Unit conversion: every amount becomes grams before it is summed or priced."""

import pytest

from sous_chef.units import (UnitError, allowed_units, format_amount, format_qty,
                             normalize_unit, to_grams)

CHICKPEAS = {"name": "Chickpeas", "g_per_cup": 164, "unit_g": {"can": 250}}
CHICKEN = {"name": "Chicken thighs", "g_per_cup": None, "unit_g": {"each": 115}}


def test_mass_converts_directly():
    assert to_grams(1, "lb", CHICKEN) == pytest.approx(453.592)
    assert to_grams(8, "oz", CHICKEN) == pytest.approx(226.796)


def test_volume_uses_the_ingredients_density():
    assert to_grams(1, "cup", CHICKPEAS) == pytest.approx(164)
    assert to_grams(2, "tbsp", CHICKPEAS) == pytest.approx(164 / 8, rel=1e-3)


def test_counted_units_use_the_ingredients_own_weight():
    assert to_grams(2, "can", CHICKPEAS) == 500
    assert to_grams(2, "cans", CHICKPEAS) == 500


def test_volume_without_density_is_refused_with_the_units_that_work():
    with pytest.raises(UnitError) as e:
        to_grams(1, "cup", CHICKEN)
    assert "each" in str(e.value) and "lb" in str(e.value)


def test_unknown_unit_names_the_allowed_ones():
    with pytest.raises(UnitError) as e:
        to_grams(1, "handful", CHICKPEAS)
    assert "can" in str(e.value)


@pytest.mark.parametrize("raw,unit", [
    ("Tablespoons", "tbsp"), ("cloves", "clove"), ("lbs", "lb"), ("", "each"),
    ("bunches", "bunch"), ("fluid ounces", "fl oz"), ("tsp.", "tsp"),
])
def test_unit_aliases(raw, unit):
    assert normalize_unit(raw) == unit


def test_allowed_units_follow_what_the_ingredient_can_convert():
    assert "cup" in allowed_units(CHICKPEAS) and "can" in allowed_units(CHICKPEAS)
    assert "cup" not in allowed_units(CHICKEN)


@pytest.mark.parametrize("q,text", [
    (1.5, "1½"), (0.333, "⅓"), (0.25, "¼"), (2.0, "2"), (0.66, "⅔"), (12.4, "12"), (2.97, "3"),
])
def test_quantities_read_like_a_recipe(q, text):
    assert format_qty(q) == text


def test_amounts_pluralise_counted_units_only():
    assert format_amount(2, "clove") == "2 cloves"
    assert format_amount(1, "clove") == "1 clove"
    assert format_amount(0.5, "bunch") == "½ bunch"
    assert format_amount(1.5, "cup") == "1½ cup"
    assert format_amount(3, "each") == "3"
    assert format_amount(0, "tsp") == "to taste"
