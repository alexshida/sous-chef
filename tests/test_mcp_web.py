"""The MCP server and the web app — the two adapters over the tool layer."""

import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

from sous_chef import tools
from sous_chef.mcp.server import INSTRUCTIONS, mcp
from sous_chef.web import chef
from sous_chef.web.app import app


@pytest.fixture
def client(fresh_db):
    # A middleware serves only loopback and the tailnet; TestClient's default
    # client host is the string "testclient", which it would refuse.
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield c


def _tools():
    return asyncio.run(mcp.list_tools())


def _call(name, args):
    return asyncio.run(mcp.call_tool(name, args))


# ── MCP ──────────────────────────────────────────────────────

def test_allowed_chef_tools_match_the_mcp_server():
    exposed = {t.replace(chef.PREFIX, "") for t in chef._TOOLS + chef._PRICE_TOOLS}
    assert exposed == {t.name for t in _tools()}


def test_a_price_check_gets_the_web_and_no_recipe_tools():
    cmd, cfg = chef.build_command("prices", chef.build_prompt("prices", plan_id=None, count=5),
                                  "sonnet", 5)
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert {"WebSearch", "WebFetch"} <= set(allowed) and "WebSearch" not in denied
    assert not any("propose_recipe" in t or "add_ingredient" in t for t in allowed)
    assert {"Bash", "Read", "Skill"} <= set(denied)
    assert cmd[cmd.index("--append-system-prompt") + 1] == chef.PRICE_PROMPT
    os.unlink(cfg)

    cmd, cfg = chef.build_command("suggest", chef.build_prompt("suggest", plan_id=1), "sonnet")
    os.unlink(cfg)
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert not any(t in allowed for t in ("WebSearch", "WebFetch")) and \
        not any("record_price" in t for t in allowed)


def test_every_tool_is_described_for_the_model():
    for t in _tools():
        assert t.description and len(t.description) > 20, t.name


def test_propose_recipe_schema_spells_out_an_ingredient_line():
    schema = json.dumps(next(t for t in _tools() if t.name == "propose_recipe").input_schema)
    for field in ("qty", "unit", "optional", "east_asian", "steps"):
        assert field in schema


def test_instructions_say_the_tools_do_the_arithmetic():
    assert "never state numbers you worked out yourself" in INSTRUCTIONS


def test_tool_errors_reach_the_model_as_their_real_message(fresh_db):
    """A raised ValueError would reach the model as a bare "Error executing
    tool"; the SDK's ToolError carries the reason, and the fix, through."""
    with pytest.raises(Exception) as e:
        _call("plan_context", {"plan_id": 999})
    assert "No plan 999" in str(e.value)
    recipe = {"title": "x", "cuisine": "other", "summary": "", "servings": 4, "active_min": 5,
              "total_min": 10, "ingredients": [{"id": "salmon-fillet", "qty": 1, "unit": "lb"}],
              "steps": ["Cook."]}
    with pytest.raises(Exception) as e:
        _call("propose_recipe", {"recipe": recipe})
    assert "salmon-fillet" in str(e.value) and "salmon (Salmon fillet)" in str(e.value)


def test_search_reports_availability_for_a_plan(fresh_db):
    plan = tools.new_plan()
    result = _call("search_catalog", {"query": "paneer", "plan_id": plan["id"]})
    assert "available" in str(result) and "false" in str(result).lower()


# ── chef command ─────────────────────────────────────────────

def test_shell_file_and_skill_tools_are_denied(monkeypatch):
    monkeypatch.setattr(chef, "claude_cli_path", lambda: "/bin/true")
    cmd, cfg = chef.build_command("suggest", chef.build_prompt("suggest", plan_id=1), "sonnet")
    os.unlink(cfg)
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    for t in ("Bash", "Write", "Edit", "Read", "Skill", "WebFetch"):
        assert t in denied
    assert "--strict-mcp-config" in cmd


def test_only_an_import_from_a_link_may_fetch_the_web(monkeypatch):
    monkeypatch.setattr(chef, "claude_cli_path", lambda: "/bin/true")
    prompt = chef.build_prompt("import", plan_id=1, text="https://example.com/dal")
    cmd, cfg = chef.build_command("import", prompt, "sonnet")
    os.unlink(cfg)
    assert "WebFetch" in cmd[cmd.index("--allowedTools") + 1]
    prompt = chef.build_prompt("import", plan_id=1, text="1 cup lentils\nSimmer.")
    cmd, cfg = chef.build_command("import", prompt, "sonnet")
    os.unlink(cfg)
    assert "WebFetch" not in cmd[cmd.index("--allowedTools") + 1]


def test_mcp_config_points_at_a_real_executable():
    path = chef._mcp_config()
    try:
        cfg = json.load(open(path))
    finally:
        os.unlink(path)
    server = cfg["mcpServers"]["sous-chef"]
    assert os.path.exists(server["command"])
    assert server["env"]["SOUS_CHEF_DB_PATH"] == os.environ["SOUS_CHEF_DB_PATH"]


@pytest.mark.parametrize("task,kwargs,needle", [
    ("suggest", dict(plan_id=3, count=2), "propose 2 new"),
    ("craft", dict(plan_id=3, text="miso salmon"), "«miso salmon»"),
    ("import", dict(plan_id=None, text="2 cups rice"), "origin='import'"),
])
def test_prompts_carry_the_request(task, kwargs, needle):
    assert needle in chef.build_prompt(task, **kwargs)


def test_empty_requests_are_refused_before_running_anything():
    with pytest.raises(chef.ChefError):
        chef.build_prompt("craft", plan_id=1, text="  ")
    with pytest.raises(chef.ChefError):
        chef.build_prompt("suggest", plan_id=None)


# ── web API ──────────────────────────────────────────────────

def test_index_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "sous" in r.text


def test_first_visit_creates_a_plan_with_suggestions(client):
    first = client.get("/api/plan")
    assert first.status_code == 200 and first.json() is None
    plan = client.post("/api/plan", json={}).json()
    assert plan["candidates"] and client.get("/api/plan").json()["id"] == plan["id"]


def test_plan_flow_through_the_api(client):
    plan = client.post("/api/plan", json={"week_start": "2026-10-05"}).json()
    rid = plan["candidates"][0]["id"]
    p = client.post(f"/api/plan/{plan['id']}/select", json={"recipe_id": rid}).json()
    assert p["selected"][0]["id"] == rid
    p = client.post(f"/api/plan/{plan['id']}/servings", json={"recipe_id": rid, "servings": 6}).json()
    assert p["selected"][0]["servings"] == 6
    g = client.get(f"/api/plan/{plan['id']}/grocery").json()
    assert g["total"] > 0
    first = g["stores"][0]["aisles"][0]["items"][0]["id"]
    client.post(f"/api/plan/{plan['id']}/grocery/check", json={"ingredient_id": first})
    g = client.get(f"/api/plan/{plan['id']}/grocery").json()
    assert g["stores"][0]["aisles"][0]["items"][0]["checked"]
    ics = client.get(f"/api/plan/{plan['id']}/calendar.ics")
    assert ics.headers["content-type"].startswith("text/calendar") and "BEGIN:VEVENT" in ics.text


def test_tool_errors_are_conflicts_not_crashes(client):
    plan = client.post("/api/plan", json={}).json()
    unchosen = plan["candidates"][0]["id"]
    r = client.post(f"/api/plan/{plan['id']}/cook-date",
                    json={"recipe_id": unchosen, "cook_date": plan["week_start"]})
    assert r.status_code == 409 and "not in this week's plan" in r.json()["detail"]
    r = client.post(f"/api/plan/{plan['id']}/cook-date", json={"recipe_id": unchosen, "cook_date": "2030-01-01"})
    assert r.status_code == 409 and "inside the plan's week" in r.json()["detail"]


def test_prices_and_pantry_through_the_api(client):
    r = client.put("/api/price", json={"ingredient_id": "eggs", "store": "tj", "price": 4.49})
    assert r.json()["offers"][0]["source"] == "user"
    client.post("/api/pantry", json={"ingredient_id": "cumin", "have": True})
    assert "cumin" in {p["id"] for p in client.get("/api/pantry").json()}


def test_preferences_round_trip(client):
    prefs = client.get("/api/prefs").json()
    prefs["household"] = 2
    assert client.put("/api/prefs", json=prefs).json()["household"] == 2


def test_chef_errors_stream_as_events(client, monkeypatch):
    def broken():
        raise chef.ChefError("The `claude` CLI was not found.")
    monkeypatch.setattr(chef, "claude_cli_path", broken)
    plan = client.post("/api/plan", json={"suggest": False}).json()
    r = client.post("/api/chef", json={"task": "suggest", "plan_id": plan["id"]})
    events = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
    assert events[0]["type"] == "error" and "not found" in events[0]["text"]
    assert events[-1]["type"] == "done"


def test_strangers_are_refused():
    with TestClient(app, client=("203.0.113.9", 50000)) as c:
        assert c.get("/api/prefs").status_code == 403


def test_tailnet_clients_are_served(fresh_db):
    with TestClient(app, client=("100.101.102.103", 50000)) as c:
        assert c.get("/api/prefs").status_code == 200


def test_reconnecting_with_nothing_running_says_idle(client):
    plan = client.post("/api/plan", json={"suggest": False}).json()
    r = client.get(f"/api/chef/stream?plan_id={plan['id']}")
    events = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
    assert events == [{"type": "idle"}]


def test_a_second_chef_request_attaches_and_replays(client, monkeypatch):
    """Stream the job a running request would have joined: `attached`, then
    the run from its start."""
    job = chef.Job(key="plan-1", task="suggest")
    for e in ({"type": "status", "text": "Reading your week"},
              {"type": "recipe", "id": 5, "title": "Dal"}, {"type": "done", "saved": [5]}):
        job.add(e)
    job.finish()
    monkeypatch.setattr(chef, "start_or_attach", lambda *a, **k: (job, True))
    r = client.post("/api/chef", json={"task": "suggest", "plan_id": 1})
    events = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
    assert [e["type"] for e in events] == ["attached", "status", "recipe", "done"]
    monkeypatch.setattr(chef, "current_job", lambda *a, **k: job)
    r = client.get("/api/chef/stream?plan_id=1&recent=true")
    assert "Dal" in r.text


def test_fresh_batch_through_the_api(client):
    plan = client.post("/api/plan", json={}).json()
    old = {c["id"] for c in plan["candidates"]}
    p = client.post(f"/api/plan/{plan['id']}/fresh", json={"library": 2}).json()
    assert len(p["candidates"]) == 2 and old.isdisjoint({c["id"] for c in p["candidates"]})


def test_prefs_say_whether_claude_is_available(client, monkeypatch):
    assert client.get("/api/prefs").json()["claude_available"] in (True, False)
    def missing():
        raise chef.ChefError("no")
    monkeypatch.setattr(chef, "claude_cli_path", missing)
    prefs = client.get("/api/prefs").json()
    assert prefs["claude_available"] is False
    prefs["batch_claude"] = 2
    assert client.put("/api/prefs", json=prefs).json()["batch_claude"] == 2



def test_a_save_clash_is_a_structured_conflict(client):
    from sous_chef.storage import db as store
    plan = client.post("/api/plan", json={"suggest": False}).json()
    chana = next(r for r in store.list_recipes() if r["title"].startswith("Chana"))
    twin = {k: chana[k] for k in ("cuisine", "summary", "servings", "active_min", "total_min",
                                  "ingredients", "steps", "tags")}
    rid = tools.propose_recipe({**twin, "title": "Chickpea Spinach Curry"}, plan["id"], "craft")["id"]
    r = client.post(f"/api/recipe/{rid}/library", json={"keep": True})
    assert r.status_code == 409
    assert r.json()["detail"]["clash"]["title"].startswith("Chana") and "looks like" in r.json()["detail"]["message"]
    assert client.post(f"/api/recipe/{rid}/library", json={"keep": True, "on_clash": "new"}).json()["in_library"]
