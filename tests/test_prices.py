"""Prices found online: what gets checked, what may be recorded, and what never changes.

A price check is a model with web search writing into the price table, so the
tools hold the lines the model would otherwise be trusted to: your own prices
are never replaced, only offers a store already has can be updated, and a
price that jumps more than threefold per pound is sent back as a likely
package mix-up.
"""

import json
import stat
import sys
from datetime import datetime, timedelta, timezone

import pytest

from sous_chef import tools
from sous_chef.storage import db
from sous_chef.web import chef

URL = "https://www.traderjoes.com/home/products/pdp/chicken-thighs"


def offer(iid, store):
    return db.get_ingredient(iid)["offers"][store]


# ── what is worth checking ───────────────────────────────────

def test_only_estimates_at_your_stores_that_are_still_carried(fresh_db):
    tools.update_preferences({"stores": ["tj", "costco"]})
    tools.set_price("chicken-thigh", "tj", 7.99)                 # yours
    tools.set_carried("chicken-breast", "tj", False)             # not carried
    items = tools.prices_to_check(limit=0)
    keys = {(i["ingredient_id"], i["store"]) for i in items}
    assert ("chicken-thigh", "tj") not in keys
    assert ("chicken-breast", "tj") not in keys
    assert ("chicken-thigh", "costco") in keys
    assert {i["store"] for i in items} <= {"tj", "costco"}


def test_a_store_this_weeks_plan_added_is_checked_too(fresh_db):
    plan = tools.new_plan()
    tools.update_plan(plan["id"], stores=["tj", "costco"])
    assert "costco" in {i["store"] for i in tools.prices_to_check(limit=0)}


def test_this_weeks_ingredients_come_first(fresh_db):
    plan = tools.new_plan()
    lentil = next(r for r in tools.list_recipes() if "lentil" in r["title"].lower()
                  or "dal" in r["title"].lower())
    tools.select_recipe(plan["id"], lentil["id"])
    used = {line["id"] for line in db.get_recipe(lentil["id"])["ingredients"]}
    first = tools.prices_to_check(limit=3)
    assert all(i["ingredient_id"] in used for i in first)


def test_recently_checked_waits_and_never_checked_goes_first(fresh_db):
    tools.mark_price_checked("chicken-thigh", "tj")
    assert ("chicken-thigh", "tj") not in {(i["ingredient_id"], i["store"])
                                           for i in tools.prices_to_check(limit=0)}
    order = [(i["ingredient_id"], i["store"]) for i in tools.prices_to_check(limit=0, stale_days=0)]
    # both in starter recipes, both bought at Trader Joe's: the unchecked one first
    assert order.index(("chicken-thigh", "tj")) > order.index(("chicken-breast", "tj"))


def test_items_say_what_they_are_listed_as(fresh_db):
    item = tools.prices_to_check(limit=1)[0]
    assert {"ingredient_id", "store", "product", "price", "pkg_qty", "pkg_unit", "per_lb",
            "units"} <= set(item)


# ── recording ────────────────────────────────────────────────

def test_a_found_price_is_recorded_with_its_page(fresh_db):
    out = tools.record_price("chicken-thigh", "tj", 7.99, 1.5, "lb",
                             "Boneless Skinless Chicken Thighs", URL)
    assert out["old"]["price"] == 7.49 and out["new"]["price"] == 7.99
    assert out["change_pct"] == 7
    o = offer("chicken-thigh", "tj")
    assert (o["source"], o["source_url"], o["price"]) == ("web", URL, 7.99)
    assert o["checked_at"]
    view = next(v for v in tools.get_ingredient("chicken-thigh")["offers"] if v["store"] == "tj")
    assert view["source_url"] == URL and view["checked_at"]


def test_your_price_is_never_replaced(fresh_db):
    tools.set_price("chicken-thigh", "tj", 6.99)
    with pytest.raises(tools.ToolError, match="user's own"):
        tools.record_price("chicken-thigh", "tj", 7.99, 1.5, "lb", "Thighs", URL)
    assert offer("chicken-thigh", "tj")["price"] == 6.99


def test_a_receipt_price_is_yours_too(fresh_db):
    db.upsert_offer("chicken-thigh", "tj", price=7.29, pkg_qty=1.5, pkg_unit="lb",
                    product="Thighs", source="receipt")
    with pytest.raises(tools.ToolError, match="user's own"):
        tools.record_price("chicken-thigh", "tj", 7.99, 1.5, "lb", "Thighs", URL)


def test_an_edit_made_during_the_check_wins(fresh_db):
    """The guard is in the UPDATE itself, so a price you type while a check is
    running is not overwritten by the check finishing after you."""
    tools.set_price("chicken-thigh", "tj", 6.99)
    assert not db.record_web_price("chicken-thigh", "tj", price=7.99, pkg_qty=1.5,
                                   pkg_unit="lb", product="Thighs", source_url=URL)
    assert offer("chicken-thigh", "tj")["source"] == "user"


def test_not_carried_stays_not_carried(fresh_db):
    tools.set_carried("chicken-thigh", "tj", False)
    with pytest.raises(tools.ToolError, match="not carried"):
        tools.record_price("chicken-thigh", "tj", 7.99, 1.5, "lb", "Thighs", URL)
    assert not offer("chicken-thigh", "tj")["carried"]


def test_a_check_cannot_add_a_store_that_had_no_listing(fresh_db):
    ing = db.get_ingredient("rotisserie-chicken")
    missing = next(s for s in ("tj", "qfc", "pcc", "costco") if s not in ing["offers"])
    with pytest.raises(tools.ToolError, match="no listing"):
        tools.record_price("rotisserie-chicken", missing, 5.99, 1, "each", "Rotisserie", URL)


def test_a_threefold_jump_gets_a_second_look(fresh_db):
    # $7.99 read as the price of one pound instead of a 1.5 lb pack is fine;
    # a 6 lb family pack's price recorded against 1.5 lb is not.
    with pytest.raises(tools.ToolError, match="package-size mix-up"):
        tools.record_price("chicken-thigh", "tj", 23.96, 1.5, "lb", "Thighs", URL)
    with pytest.raises(tools.ToolError, match="package-size mix-up"):
        tools.record_price("chicken-thigh", "tj", 1.99, 1.5, "lb", "Thighs", URL)
    assert offer("chicken-thigh", "tj")["source"] == "seed"
    tools.record_price("chicken-thigh", "tj", 23.96, 1.5, "lb", "Thighs", URL, large_change=True)
    assert offer("chicken-thigh", "tj")["price"] == 23.96


def test_the_package_must_be_measurable(fresh_db):
    with pytest.raises(tools.ToolError, match="lb"):
        tools.record_price("chicken-thigh", "tj", 7.99, 2, "bunch", "Thighs", URL)
    with pytest.raises(tools.ToolError, match="source_url"):
        tools.record_price("chicken-thigh", "tj", 7.99, 1.5, "lb", "Thighs", "traderjoes.com")


def test_no_listing_keeps_the_price_and_moves_the_date(fresh_db):
    before = offer("chicken-thigh", "tj")
    tools.mark_price_checked("chicken-thigh", "tj")
    after = offer("chicken-thigh", "tj")
    assert after["checked_at"] and (after["price"], after["source"]) == (before["price"], "seed")


def test_reseeding_keeps_prices_found_online(fresh_db):
    tools.record_price("chicken-thigh", "tj", 7.99, 1.5, "lb", "Thighs", URL)
    db.init_db()
    assert offer("chicken-thigh", "tj")["price"] == 7.99


def test_typing_over_an_online_price_makes_it_yours(fresh_db):
    tools.record_price("chicken-thigh", "tj", 7.99, 1.5, "lb", "Thighs", URL)
    tools.set_price("chicken-thigh", "tj", 7.49)
    o = offer("chicken-thigh", "tj")
    assert (o["source"], o["source_url"]) == ("user", None)


def test_online_prices_still_count_as_estimates(fresh_db):
    tools.record_price("chicken-thigh", "tj", 7.99, 1.5, "lb", "Thighs", URL)
    r = tools.check_recipe({"title": "T", "cuisine": "other", "summary": "", "servings": 2,
                            "active_min": 5, "total_min": 20, "steps": ["Cook."],
                            "ingredients": [{"id": "chicken-thigh", "qty": 1, "unit": "lb"}]})
    assert r["estimated"]
    db.upsert_offer("chicken-thigh", "tj", price=7.29, pkg_qty=1.5, pkg_unit="lb",
                    product="Thighs", source="receipt")
    r = tools.check_recipe({"title": "T", "cuisine": "other", "summary": "", "servings": 2,
                            "active_min": 5, "total_min": 20, "steps": ["Cook."],
                            "ingredients": [{"id": "chicken-thigh", "qty": 1, "unit": "lb"}]})
    assert not r["estimated"]


# ── when ─────────────────────────────────────────────────────

def _ran(days_ago, **kw):
    started = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.set_meta(tools.PRICE_RUN, {"started_at": started.isoformat(timespec="seconds"), **kw})


def test_due_every_n_days_and_a_day_after_a_failure(fresh_db):
    assert tools.price_check_due()                       # never run
    _ran(3)
    assert not tools.price_check_due()
    _ran(31)
    assert tools.price_check_due()
    _ran(2, error="not logged in")
    assert tools.price_check_due()
    tools.update_preferences({"price_refresh_days": 0})
    assert not tools.price_check_due()


def test_status_reports_the_last_run(fresh_db):
    tools.note_price_run(started=True, scheduled=False, limit=5)
    tools.note_price_run(updated=2, unlisted=1, error=None, changes=[])
    st = tools.price_check_status()
    assert st["last"]["updated"] == 2 and st["last"]["finished_at"]
    assert st["refresh_days"] == 30 and st["next_due"] > st["last"]["started_at"]
    assert st["waiting"] > 0


def test_settings_bound_the_schedule(fresh_db):
    with pytest.raises(tools.ToolError):
        tools.update_preferences({"price_check_items": 0})
    with pytest.raises(tools.ToolError):
        tools.update_preferences({"price_refresh_days": -1})


# ── the run ──────────────────────────────────────────────────

FAKE_PRICE_CLI = r'''#!PYTHON
import json
def emit(o): print(json.dumps(o), flush=True)
def call(i, name, args): emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": i, "name": name, "input": args}]}})
def result(i, body, err=False): emit({"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": i, "is_error": err,
     "content": [{"type": "text", "text": body}]}]}})
call("t1", "mcp__sous-chef__prices_to_check", {"limit": 2})
result("t1", "[]")
call("t2", "WebSearch", {"query": "trader joe's chicken thighs price"})
call("t3", "WebFetch", {"url": "https://www.traderjoes.com/home/products/pdp/x", "prompt": "price"})
call("t4", "mcp__sous-chef__record_price", {"ingredient_id": "chicken-thigh", "store": "tj"})
result("t4", "That is 3.2× the current price — usually a package-size mix-up", err=True)
call("t5", "mcp__sous-chef__record_price", {"ingredient_id": "chicken-thigh", "store": "tj"})
result("t5", json.dumps({"ingredient_id": "chicken-thigh", "name": "Chicken thighs", "store": "tj",
    "store_name": "Trader Joe's", "old": {"price": 7.49, "package": "1½ lb"},
    "new": {"price": 7.99, "package": "1½ lb"}, "change_pct": 7}))
call("t6", "mcp__sous-chef__mark_price_checked", {"ingredient_id": "tofu", "store": "tj"})
result("t6", json.dumps({"ingredient_id": "tofu", "name": "Tofu", "store": "tj",
    "store_name": "Trader Joe's", "checked": True}))
emit({"type": "stream_event", "event": {"type": "message_start"}})
emit({"type": "stream_event", "event": {"type": "content_block_delta",
      "delta": {"type": "text_delta", "text": "1 price changed, 1 had no listing."}}})
emit({"type": "result", "subtype": "success"})
'''


@pytest.fixture
def fake_price_cli(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_PRICE_CLI.replace("PYTHON", sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(chef, "claude_cli_path", lambda: str(path))
    return path


def test_a_price_check_streams_what_it_changed(fresh_db, fake_price_cli):
    events = list(chef.run("prices", count=2))
    kinds = [e["type"] for e in events]
    assert kinds.count("price") == 1 and kinds.count("unlisted") == 1 and "retry" in kinds
    statuses = [e["text"] for e in events if e["type"] == "status"]
    assert any(s.startswith("Searching: trader joe") for s in statuses)
    assert "Reading traderjoes.com" in statuses
    price = next(e for e in events if e["type"] == "price")
    assert price["old"]["price"] == 7.49 and price["new"]["price"] == 7.99
    assert events[-1] == {"type": "done", "saved": [], "updated": 1, "unlisted": 1, "unmatched": 0}


def test_a_price_check_is_recorded_for_the_schedule(fresh_db, fake_price_cli):
    job, attached = chef.start_or_attach("prices", count=2, scheduled=True)
    assert not attached and job.key == "prices"
    events = [e for e in job.follow(keepalive=5) if e]
    assert events[-1]["type"] == "done"
    last = tools.price_check_status()["last"]
    assert last["scheduled"] and last["updated"] == 1 and last["unlisted"] == 1
    assert last["changes"][0]["name"] == "Chicken thighs" and not last["error"]
    assert not tools.price_check_due()


def test_nothing_due_means_no_cli_at_all(fresh_db, monkeypatch):
    for item in tools.prices_to_check(limit=0):
        db.mark_checked(item["ingredient_id"], item["store"])

    def no_cli():
        raise AssertionError("the CLI should not be started")
    monkeypatch.setattr(chef, "claude_cli_path", no_cli)
    events = list(chef.run("prices", count=5))
    assert [e["type"] for e in events] == ["text", "done"]


def test_the_api_runs_and_reconnects_to_a_price_check(fresh_db, fake_price_cli):
    from fastapi.testclient import TestClient

    from sous_chef.web.app import app
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        assert c.get("/api/prices").json()["running"] is False
        r = c.post("/api/prices/check", json={"limit": 2})
        events = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
        assert events[-1]["updated"] == 1
        again = c.get("/api/chef/stream?task=prices&recent=true")
        replay = [json.loads(l[6:]) for l in again.text.splitlines() if l.startswith("data: ")]
        assert replay[0]["type"] == "attached" and replay[-1]["type"] == "done"
        status = c.get("/api/prices").json()
        assert status["last"]["updated"] == 1 and status["running"] is False
        assert c.post("/api/prices/check", json={"limit": 99}).status_code == 422
