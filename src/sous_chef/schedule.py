"""The week: which nights you cook, which meals leftovers cover, and what it leaves.

Each day gets a status from your calendar (or from you, which wins):

  home  — free evening, a good night to cook
  busy  — something on in the evening; eat at home, but leftovers, not cooking
  out   — dinner is taken care of (a dinner, a party, drinks)
  away  — not home at all that day (a trip)

Then the chosen recipes are put on cook nights and their servings are walked
through the week: dinner on the night they are cooked, then the oldest
leftovers first for lunches and non-cooking dinners. Anything still in the
fridge past `leftover_days` is flagged to freeze.

Meal-prep mode cooks everything in one session on the first free day, back to
back from `prep_start`, and treats the freezer as part of the plan: portions
past their keeping time are frozen and still eaten later in the week, rather
than written off.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sous_chef.config import AWAY_WORDS, EAT_OUT_WORDS

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
STATUSES = ("home", "busy", "out", "away")
_RANK = {"home": 0, "busy": 1, "out": 2, "away": 3}


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def default_week_start(today: date | None = None) -> date:
    """This Monday early in the week; next Monday from Thursday on."""
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    return monday if today.weekday() <= 2 else monday + timedelta(days=7)


def classify_day(day: date, events: list[dict], dinner_time: str = "19:00") -> tuple[str, list[str]]:
    """A day's status from its calendar events, and the events that decided it."""
    dinner = datetime.combine(day, parse_hhmm(dinner_time))
    window = (dinner - timedelta(hours=2), dinner + timedelta(hours=1, minutes=30))
    status, reasons = "home", []

    def bump(s: str, why: str) -> None:
        nonlocal status
        if _RANK[s] > _RANK[status]:
            status = s
        reasons.append(why)

    for ev in events:
        text = (ev.get("summary") or "").lower()
        if ev.get("all_day"):
            first = date.fromisoformat(ev["start"][:10])
            last = date.fromisoformat(ev["end"][:10]) - timedelta(days=1)
            if first <= day <= max(first, last) and any(w in text for w in AWAY_WORDS):
                bump("away", ev["summary"])
            continue
        start = datetime.fromisoformat(ev["start"]).replace(tzinfo=None)
        end = datetime.fromisoformat(ev["end"]).replace(tzinfo=None)
        if start.date() != day and end.date() != day:
            continue
        overlaps = start < window[1] and end > window[0]
        eat_out = any(w in text for w in EAT_OUT_WORDS)
        if eat_out and (overlaps or start.hour >= 17):
            bump("out", ev["summary"])
        elif overlaps:
            bump("busy", ev["summary"])
    return status, reasons


def assign_cook_days(recipes: list[dict], days: list[dict], *, max_weeknight_min: int,
                     household: int, lunches: int, leftover_days: int) -> dict[int, str]:
    """Pick a cook night for each recipe that does not have one yet.

    Cook when the fridge runs out: walk the week with what is already placed,
    find the first meal nothing covers, and cook on the latest free night that
    can still cover it. Weeknights get recipes that fit `max_weeknight_min`,
    freshest first (fresh fish and greens early in the week); weekends get the
    long ones. Recipes beyond what the week needs go on the free nights
    furthest from any other cooking.
    """
    placed = {r["recipe_id"]: r["cook_date"] for r in recipes if r.get("cook_date")}
    todo = [r for r in recipes if not r.get("cook_date")]
    index = {d["date"]: i for i, d in enumerate(days)}
    free = [i for i, d in enumerate(days) if d["status"] == "home"]
    out: dict[int, str] = {}

    def cooks_now() -> dict[str, list[dict]]:
        cooks: dict[str, list[dict]] = {}
        for r in recipes:
            when = placed.get(r["recipe_id"])
            if when:
                cooks.setdefault(when, []).append(r)
        return cooks

    def pick(day_idx: int) -> dict:
        weekend = date.fromisoformat(days[day_idx]["date"]).weekday() >= 5
        if weekend:
            return max(todo, key=lambda r: (r.get("total_min", 0), r.get("freshness", 0)))
        fits = [r for r in todo if r.get("total_min", 0) <= max_weeknight_min] or todo
        return max(fits, key=lambda r: (r.get("freshness", 0), -r.get("total_min", 0)))

    while todo:
        taken = {index[d] for d in placed.values() if d in index}
        open_days = [i for i in free if i not in taken]
        if not open_days:
            break
        trial = [dict(d) for d in days]
        gaps = simulate(trial, cooks_now(), household=household, lunches=lunches,
                        leftover_days=leftover_days)["gaps"]
        target = None
        for idx, meal in gaps:
            latest = idx if meal == "dinner" else idx - 1
            # Cooking more than a couple of days ahead of a gap only moves it.
            usable = [i for i in open_days if latest - 2 <= i <= latest]
            if usable:
                target = max(usable)
                break
        if target is None:
            cook_idx = sorted(taken)
            target = max(open_days, key=lambda i: min((abs(i - c) for c in cook_idx), default=7))
        r = pick(target)
        todo.remove(r)
        placed[r["recipe_id"]] = days[target]["date"]
        out[r["recipe_id"]] = days[target]["date"]
    return out


def simulate(days: list[dict], cooks: dict[str, list[dict]], *, household: int,
             lunches: int, leftover_days: int, freezer: bool = False) -> dict:
    """Walk the servings through the week. Mutates each day with its meals.

    With `freezer`, servings that outlast the keeping time are frozen and stay
    available (eaten thawed, marked "from the freezer"); without it they are
    flagged and taken out of the count.
    """
    fridge: list[dict] = []          # {recipe_id, title, cooked_idx, left}
    lunch_days = {d["date"] for d in days
                  if date.fromisoformat(d["date"]).weekday() < 5 and d["status"] != "away"}
    lunch_days = set(sorted(lunch_days)[:max(0, lunches)])
    needed = covered = frozen = 0
    warnings: list[str] = []
    gaps: list[tuple[int, str]] = []

    def eat(idx: int, prefer: int | None = None) -> list[str]:
        nonlocal covered
        want, got = household, []
        pool = sorted(fridge, key=lambda p: (p["recipe_id"] != prefer, p["cooked_idx"]))
        for p in pool:
            if want == 0:
                break
            if p["left"] <= 0 or (prefer is None and p["cooked_idx"] >= idx):
                continue
            take = min(want, p["left"])
            p["left"] -= take
            want -= take
            covered += take
            got.append(p["title"] + (" (from the freezer)" if p.get("frozen") else ""))
        return got

    for idx, d in enumerate(days):
        # Food past its keeping time is frozen (meal prep) or flagged and dropped.
        for p in fridge:
            if p["left"] > 0 and not p.get("frozen") and idx - p["cooked_idx"] == leftover_days:
                n = p["left"]
                frozen += n
                if freezer:
                    p["frozen"] = True
                    warnings.append(f"Freeze {n} serving{'s' if n != 1 else ''} of {p['title']} "
                                    f"on {days[p['cooked_idx']]['dow']} — "
                                    f"{'they are' if n != 1 else 'it is'} eaten after day "
                                    f"{leftover_days}; thaw the night before.")
                else:
                    warnings.append(f"{n} serving{'s' if n != 1 else ''} of {p['title']} won't be "
                                    f"eaten within {leftover_days} days — freeze "
                                    f"{'them' if n != 1 else 'it'} the night you cook.")
                    p["left"] = 0

        d["lunch"] = None
        if d["date"] in lunch_days:
            needed += household
            got = eat(idx)
            d["lunch"] = {"titles": got, "covered": bool(got)}
            if not got:
                gaps.append((idx, "lunch"))

        d["dinner"] = None
        d["cook"] = cooks.get(d["date"], [])
        if d["status"] in ("home", "busy"):
            needed += household
            for c in d["cook"]:
                fridge.append({"recipe_id": c["recipe_id"], "title": c["title"],
                               "cooked_idx": idx, "left": c["servings"]})
            prefer = d["cook"][0]["recipe_id"] if d["cook"] else None
            got = eat(idx, prefer)
            d["dinner"] = {"titles": got, "covered": bool(got), "fresh": bool(d["cook"])}
            if not got:
                gaps.append((idx, "dinner"))
        elif d["cook"]:
            # Cooking on a night out is allowed (meal prep); it just feeds later meals.
            for c in d["cook"]:
                fridge.append({"recipe_id": c["recipe_id"], "title": c["title"],
                               "cooked_idx": idx, "left": c["servings"]})

    spare = sum(p["left"] for p in fridge if p["left"] > 0)
    return {"meals_needed": needed, "meals_covered": covered, "spare_servings": spare,
            "frozen_servings": frozen, "warnings": warnings, "gaps": gaps}


def assign_prep_day(recipes: list[dict], days: list[dict]) -> dict[int, str]:
    """Meal prep: every recipe without a day goes to the first free day."""
    free = [d for d in days if d["status"] == "home"]
    if not free:
        return {}
    return {r["recipe_id"]: free[0]["date"] for r in recipes if not r.get("cook_date")}


def plan_week(week_start: date, events: list[dict], overrides: dict[str, str],
              selections: list[dict], prefs, *, meal_prep: bool = False) -> dict:
    """The whole week view. `selections` items: recipe_id, title, servings,
    total_min, cook_date (or None), and freshness (how much it wants cooking early)."""
    days = []
    for i in range(7):
        day = week_start + timedelta(days=i)
        todays = [e for e in events if _touches(e, day)]
        auto, reasons = classify_day(day, todays, prefs.dinner_time)
        iso = day.isoformat()
        days.append({"date": iso, "dow": DOW[day.weekday()], "status": overrides.get(iso, auto),
                     "auto_status": auto, "reasons": reasons, "overridden": iso in overrides,
                     "events": [_event_label(e) for e in todays]})

    in_week = {d["date"] for d in days}
    placed = [dict(s, cook_date=s["cook_date"] if s.get("cook_date") in in_week else None)
              for s in selections]
    if meal_prep:
        auto = assign_prep_day(placed, days)
    else:
        auto = assign_cook_days(placed, days, max_weeknight_min=prefs.max_weeknight_min,
                                household=prefs.household, lunches=prefs.lunches,
                                leftover_days=prefs.leftover_days)
    cooks: dict[str, list[dict]] = {}
    for s in placed:
        when = s["cook_date"] or auto.get(s["recipe_id"])
        s["cook_date_effective"] = when
        s["auto_assigned"] = not s["cook_date"] and when is not None
        if when:
            cooks.setdefault(when, []).append({
                "recipe_id": s["recipe_id"], "title": s["title"], "servings": s["servings"],
                "total_min": s["total_min"], "auto": s["auto_assigned"]})
    _set_start_times(cooks, prefs, meal_prep)
    coverage = simulate(days, cooks, household=prefs.household, lunches=prefs.lunches,
                        leftover_days=prefs.leftover_days, freezer=meal_prep)
    coverage["gaps"] = [{"date": days[i]["date"], "dow": days[i]["dow"], "meal": m}
                        for i, m in coverage["gaps"]]
    unplaced = [s["title"] for s in placed if not s["cook_date_effective"]]
    if unplaced:
        coverage["warnings"].append("No free night left for: " + ", ".join(unplaced)
                                    + ". Mark a day as home or pick a cook day.")
    return {"days": days, "coverage": coverage, "meal_prep": meal_prep,
            "cook_dates": {s["recipe_id"]: s["cook_date_effective"] for s in placed},
            "auto_assigned": {s["recipe_id"]: s["auto_assigned"] for s in placed}}


def _set_start_times(cooks: dict[str, list[dict]], prefs, meal_prep: bool) -> None:
    """When each cook starts, as HH:MM.

    Normally a recipe is timed to be ready at dinner. A meal-prep session runs
    its recipes back to back from `prep_start` — longest first, so the slow
    oven or simmering pot is going while the quick ones are made.
    """
    for when, entries in cooks.items():
        day = date.fromisoformat(when)
        if meal_prep:
            entries.sort(key=lambda c: -c["total_min"])
            t = datetime.combine(day, parse_hhmm(prefs.prep_start))
            for c in entries:
                c["start"] = t.strftime("%H:%M")
                t += timedelta(minutes=c["total_min"])
        else:
            dinner = datetime.combine(day, parse_hhmm(prefs.dinner_time))
            for c in entries:
                c["start"] = (dinner - timedelta(minutes=c["total_min"])).strftime("%H:%M")


def _touches(ev: dict, day: date) -> bool:
    if ev.get("all_day"):
        first = date.fromisoformat(ev["start"][:10])
        last = date.fromisoformat(ev["end"][:10]) - timedelta(days=1)
        return first <= day <= max(first, last)
    start = datetime.fromisoformat(ev["start"]).date()
    end = datetime.fromisoformat(ev["end"]).date()
    return start <= day <= end


def _event_label(ev: dict) -> dict:
    if ev.get("all_day"):
        return {"summary": ev["summary"], "time": "all day"}
    start = datetime.fromisoformat(ev["start"])
    return {"summary": ev["summary"], "time": start.strftime("%-I:%M %p").lower()}
