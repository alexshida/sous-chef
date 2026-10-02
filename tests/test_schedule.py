"""The week: day status from the calendar, cook nights, and leftovers coverage."""

from datetime import date

import pytest

from sous_chef import schedule
from sous_chef.config import Preferences

MON = date(2026, 10, 5)


def ev(summary, start, end, all_day=False):
    return {"summary": summary, "start": start, "end": end, "all_day": all_day}


@pytest.mark.parametrize("event,status", [
    (ev("Dinner with Sam", "2026-10-05T19:00:00", "2026-10-05T21:00:00"), "out"),
    (ev("Lab party", "2026-10-05T17:30:00", "2026-10-05T20:00:00"), "out"),
    (ev("Journal club", "2026-10-05T17:30:00", "2026-10-05T19:00:00"), "busy"),
    (ev("Group meeting", "2026-10-05T10:00:00", "2026-10-05T11:00:00"), "home"),
    (ev("Trip to Portland", "2026-10-04", "2026-10-07", all_day=True), "away"),
    (ev("Mom's birthday", "2026-10-05", "2026-10-06", all_day=True), "home"),
])
def test_day_status_from_an_event(event, status):
    assert schedule.classify_day(MON, [event])[0] == status


def test_the_strongest_status_wins():
    events = [ev("Seminar", "2026-10-05T17:00:00", "2026-10-05T18:30:00"),
              ev("Drinks with the lab", "2026-10-05T19:30:00", "2026-10-05T22:00:00")]
    status, reasons = schedule.classify_day(MON, events)
    assert status == "out" and len(reasons) == 2


@pytest.mark.parametrize("today,start", [
    (date(2026, 10, 2), date(2026, 10, 5)),    # Friday → next Monday
    (date(2026, 10, 6), date(2026, 10, 5)),    # Tuesday → this Monday
    (date(2026, 10, 8), date(2026, 10, 12)),   # Thursday → next Monday
])
def test_default_week_start(today, start):
    assert schedule.default_week_start(today) == start


def sel(rid, servings=4, total=30, cook=None, fresh=0):
    return {"recipe_id": rid, "title": f"R{rid}", "servings": servings, "total_min": total,
            "cook_date": cook, "freshness": fresh}


def week(selections, overrides=None, events=(), **prefs):
    return schedule.plan_week(MON, list(events), overrides or {}, selections, Preferences(**prefs))


def test_three_recipes_cover_the_week_but_the_first_lunch():
    w = week([sel(1), sel(2), sel(3)])
    cov = w["coverage"]
    assert cov["meals_needed"] == 12            # 7 dinners + 5 lunches
    assert cov["meals_covered"] == 11
    assert cov["gaps"] == [{"date": "2026-10-05", "dow": "Mon", "meal": "lunch"}]
    assert all(d["dinner"]["covered"] for d in w["days"])


def test_cooking_waits_until_the_fridge_runs_out():
    w = week([sel(1), sel(2), sel(3)])
    assert sorted(w["cook_dates"].values()) == ["2026-10-05", "2026-10-07", "2026-10-09"]


def test_a_night_out_needs_no_dinner():
    w = week([sel(1), sel(2), sel(3)], overrides={"2026-10-08": "out"})
    thu = next(d for d in w["days"] if d["date"] == "2026-10-08")
    assert thu["dinner"] is None and thu["status"] == "out"
    assert w["coverage"]["meals_needed"] == 11


def test_calendar_events_set_the_status_and_overrides_win():
    dinner = ev("Dinner at Canlis", "2026-10-06T19:00:00", "2026-10-06T21:00:00")
    w = week([sel(1)], events=[dinner])
    tue = w["days"][1]
    assert tue["status"] == "out" and tue["auto_status"] == "out"
    w = week([sel(1)], events=[dinner], overrides={"2026-10-06": "home"})
    assert w["days"][1]["status"] == "home" and w["days"][1]["overridden"]


def test_no_cooking_on_busy_or_away_days():
    w = week([sel(1), sel(2)], overrides={"2026-10-05": "busy", "2026-10-06": "away"})
    assert set(w["cook_dates"].values()).isdisjoint({"2026-10-05", "2026-10-06"})


def test_a_pinned_cook_day_is_kept():
    w = week([sel(1, cook="2026-10-10"), sel(2)])
    assert w["cook_dates"][1] == "2026-10-10" and not w["auto_assigned"][1]


def test_a_long_recipe_lands_on_the_weekend_when_it_can():
    # Starting on a Thursday, the fridge runs out over the weekend — so the
    # long recipe waits for Sunday and the quick ones take the weeknights.
    w = schedule.plan_week(date(2026, 10, 8), [], {},
                           [sel(1), sel(2, total=120), sel(3)], Preferences(max_weeknight_min=45))
    assert w["cook_dates"][2] == "2026-10-11"
    assert all(date.fromisoformat(w["cook_dates"][r]).weekday() < 5 for r in (1, 3))


def test_covering_meals_comes_before_the_weekend_preference():
    # From a Monday the third cook night is Friday: a long recipe goes there
    # rather than leave Friday's dinner empty.
    w = week([sel(1), sel(2), sel(3, total=120)], max_weeknight_min=45)
    assert w["cook_dates"][3] == "2026-10-09"
    assert all(d["dinner"]["covered"] for d in w["days"])


def test_fresh_fish_is_cooked_first():
    w = week([sel(1, fresh=0), sel(2, fresh=5)])
    assert w["cook_dates"][2] == "2026-10-05"


def test_leftovers_past_their_keeping_time_are_flagged():
    w = week([sel(1, servings=12)], lunches=0, leftover_days=4)
    cov = w["coverage"]
    assert cov["frozen_servings"] > 0
    assert "freeze" in cov["warnings"][0]


def test_a_bigger_household_eats_more_servings():
    w = week([sel(1, servings=4)], household=2, lunches=0)
    assert w["coverage"]["meals_needed"] == 14
    assert w["days"][0]["dinner"]["covered"] and w["days"][1]["dinner"]["covered"]
    assert not w["days"][2]["dinner"]["covered"]


def test_cook_start_time_is_dinner_minus_total_time():
    w = week([sel(1, total=45)], dinner_time="19:00")
    assert w["days"][0]["cook"][0]["start"] == "18:15"


def test_no_free_night_is_reported():
    w = week([sel(1)], overrides={d: "busy" for d in
                                  [f"2026-10-{n:02d}" for n in range(5, 12)]})
    assert any("No free night" in x for x in w["coverage"]["warnings"])
