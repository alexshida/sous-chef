"""Prices off a receipt: your own evidence, so they become yours.

The photo reaches the CLI on stdin as an image in a stream-json user message
— the chef is never given file access — and each line Claude matches becomes
your price at that store, replacing whatever was there.
"""

import base64
import json
import stat
import sys

import pytest

from sous_chef import tools
from sous_chef.storage import db
from sous_chef.web import chef

# The smallest valid JPEG header is not needed: the server checks type,
# encoding and size, and the CLI is a stand-in here.
PHOTO = base64.b64encode(b"\xff\xd8\xff\xe0" + b"receipt" * 50).decode()


def offer(iid, store):
    return db.get_ingredient(iid)["offers"].get(store)


# ── recording ────────────────────────────────────────────────

def test_a_receipt_price_becomes_yours(fresh_db):
    out = tools.record_receipt_price("chicken-thigh", "tj", 8.47, "Chicken Thighs",
                                     "CHICKEN THIGHS BNLS SKNLS 8.47")
    o = offer("chicken-thigh", "tj")
    assert (o["source"], o["price"], o["pkg_qty"], o["pkg_unit"]) == ("receipt", 8.47, 1.5, "lb")
    assert out["package_assumed"] and out["old"]["price"] == 7.49 and out["change_pct"] == 13
    assert out["line"] == "CHICKEN THIGHS BNLS SKNLS 8.47"
    assert o["product"] == "Boneless Skinless Chicken Thighs (~1.5 lb)"     # not the abbreviation


def test_a_receipt_replaces_even_a_price_you_typed(fresh_db):
    tools.set_price("chicken-thigh", "tj", 6.99)
    tools.record_receipt_price("chicken-thigh", "tj", 8.47, "Thighs", "CHKN THGH 8.47")
    assert offer("chicken-thigh", "tj")["price"] == 8.47


def test_a_receipt_proves_the_store_sells_it(fresh_db):
    tools.set_carried("chicken-thigh", "tj", False)
    tools.record_receipt_price("chicken-thigh", "tj", 8.47, "Thighs", "CHKN THGH 8.47")
    assert offer("chicken-thigh", "tj")["carried"]
    ing = db.get_ingredient("rotisserie-chicken")
    missing = next(s for s in ("tj", "qfc", "pcc", "costco") if s not in ing["offers"])
    out = tools.record_receipt_price("rotisserie-chicken", missing, 6.99, "Rotisserie chicken",
                                     "ROTISSERIE CHKN 6.99")
    assert out["old"] is None and out["package_assumed"]
    assert offer("rotisserie-chicken", missing)["source"] == "receipt"


def test_a_printed_size_is_used(fresh_db):
    out = tools.record_receipt_price("jasmine-rice", "tj", 3.49, "Jasmine Rice",
                                     "JASMINE RICE 2LB 3.49", pkg_qty=2, pkg_unit="lb")
    assert not out["package_assumed"] and out["new"]["package"] == "2 lb"


def test_a_line_for_several_items_gets_a_second_look(fresh_db):
    # "AVOCADOS 4 @ 1.49  5.96" recorded as 5.96 for one avocado.
    assert offer("avocado", "tj")["price"] == 1.49
    with pytest.raises(tools.ToolError, match="several items"):
        tools.record_receipt_price("avocado", "tj", 5.96, "Avocados", "AVOCADOS 4 @ 1.49 5.96")
    assert offer("avocado", "tj")["source"] != "receipt"


def test_once_yours_an_online_check_leaves_it(fresh_db):
    tools.record_receipt_price("chicken-thigh", "tj", 8.47, "Thighs", "CHKN THGH 8.47")
    with pytest.raises(tools.ToolError, match="user's own"):
        tools.record_price("chicken-thigh", "tj", 7.99, 1.5, "lb", "Thighs", "https://x.example/p")


def test_unknown_store_and_ingredient_are_refused(fresh_db):
    with pytest.raises(tools.ToolError, match="store must be"):
        tools.record_receipt_price("chicken-thigh", "safeway", 8.47, "Thighs", "line")
    with pytest.raises(tools.ToolError, match="No ingredient"):
        tools.record_receipt_price("chicken-thighz", "tj", 8.47, "Thighs", "line")


# ── the run ──────────────────────────────────────────────────

FAKE_RECEIPT_CLI = r'''#!PYTHON
import json, os, sys
turn = json.loads(sys.stdin.readline())
open(os.environ["FAKE_STDIN"], "w").write(json.dumps(turn))
def emit(o): print(json.dumps(o), flush=True)
def call(i, name, args): emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": i, "name": name, "input": args}]}})
def result(i, body, err=False): emit({"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": i, "is_error": err,
     "content": [{"type": "text", "text": body}]}]}})
call("t1", "mcp__sous-chef__search_catalog", {"query": "chicken"})
result("t1", "[]")
call("t2", "mcp__sous-chef__record_receipt_price", {"ingredient_id": "chicken-thigh", "store": "tj",
     "receipt_line": "CHICKEN THIGHS BNLS SKNLS 8.47"})
result("t2", json.dumps({"ingredient_id": "chicken-thigh", "name": "Chicken thighs", "store": "tj",
    "store_name": "Trader Joe's", "line": "CHICKEN THIGHS BNLS SKNLS 8.47",
    "old": {"price": 7.49, "package": "1½ lb"}, "new": {"price": 8.47, "package": "1½ lb"},
    "change_pct": 13, "package_assumed": True}))
call("t3", "mcp__sous-chef__receipt_unmatched", {"store": "tj", "lines": ["SPARKLING WATER LEMON"]})
result("t3", json.dumps({"store": "tj", "store_name": "Trader Joe's",
                          "lines": ["SPARKLING WATER LEMON"]}))
emit({"type": "stream_event", "event": {"type": "message_start"}})
emit({"type": "stream_event", "event": {"type": "content_block_delta",
      "delta": {"type": "text_delta", "text": "Updated 1 price from Trader Joe's."}}})
emit({"type": "result", "subtype": "success"})
'''


@pytest.fixture
def fake_receipt_cli(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_RECEIPT_CLI.replace("PYTHON", sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(chef, "claude_cli_path", lambda: str(path))
    monkeypatch.setenv("FAKE_STDIN", str(tmp_path / "stdin.json"))
    return tmp_path / "stdin.json"


def test_the_photo_goes_on_stdin_and_nowhere_else(fresh_db, fake_receipt_cli):
    big = base64.b64encode(b"\xff\xd8" + b"x" * 300_000).decode()   # bigger than a pipe buffer
    events = list(chef.run("receipt", text="the TJ's run",
                           images=[{"media_type": "image/jpeg", "data": big},
                                   {"media_type": "image/png", "data": PHOTO}]))
    turn = json.loads(fake_receipt_cli.read_text())
    content = turn["message"]["content"]
    assert turn["type"] == "user" and content[0]["type"] == "text"
    assert "the TJ's run" in content[0]["text"]
    assert [c["source"]["media_type"] for c in content[1:]] == ["image/jpeg", "image/png"]
    assert content[1]["source"]["data"] == big
    kinds = [e["type"] for e in events]
    assert kinds.count("price") == 1 and kinds.count("unmatched") == 1
    assert events[-1]["updated"] == 1 and events[-1]["unmatched"] == 1


def test_a_receipt_run_gets_no_web_and_no_recipe_tools(fresh_db):
    cmd, cfg = chef.build_command("receipt", chef.build_prompt("receipt", plan_id=None), "sonnet")
    import os
    os.unlink(cfg)
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert {"WebSearch", "WebFetch", "Read", "Bash"} <= set(denied)
    assert not any("propose_recipe" in t or "record_price" == t.split("__")[-1] for t in allowed)
    assert cmd[-2:] == ["--input-format", "stream-json"]       # prompt on stdin, not argv


def test_a_receipt_needs_a_photo(fresh_db):
    with pytest.raises(chef.ChefError, match="photo"):
        chef.start_or_attach("receipt", images=[])


def test_the_api_reads_a_receipt_and_remembers_it(fresh_db, fake_receipt_cli):
    from fastapi.testclient import TestClient

    from sous_chef.web.app import app
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        r = c.post("/api/receipt", json={"images": [f"data:image/jpeg;base64,{PHOTO}"]})
        events = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
        assert events[-1]["type"] == "done" and events[-1]["updated"] == 1
        scan = c.get("/api/prices").json()["receipt"]
        assert scan["store"] == "Trader Joe's" and scan["updated"] == 1
        assert scan["unmatched"] == ["SPARKLING WATER LEMON"]
        assert scan["closing"].startswith("Updated 1 price")


def test_the_api_refuses_what_is_not_a_photo(fresh_db):
    from fastapi.testclient import TestClient

    from sous_chef.web.app import app
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        assert c.post("/api/receipt", json={"images": []}).status_code == 422
        assert c.post("/api/receipt", json={"images": ["data:application/pdf;base64,AAAA"]}
                      ).status_code == 422
        assert c.post("/api/receipt", json={"images": ["data:image/jpeg;base64,not base64!"]}
                      ).status_code == 422
        huge = base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode()
        assert c.post("/api/receipt", json={"images": [huge]}).status_code == 422
