"""The chef run: CLI stream-json in, browser events out.

A stand-in `claude` emits the same shapes the real CLI does — tool calls, a
rejected save, an accepted save, closing words — so the event mapping is
tested without a model.
"""

import json
import os
import stat
import sys

import pytest

from sous_chef.web import chef

FAKE_CLI = r'''#!PYTHON
import json, os, sys
def emit(o): print(json.dumps(o), flush=True)
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "silent":
    print("not logged in", file=sys.stderr); sys.exit(1)
emit({"type": "system", "subtype": "init", "session_id": "s1"})
emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "t1", "name": "mcp__sous-chef__plan_context", "input": {"plan_id": 1}}]}})
emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}]}})
emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "t2", "name": "mcp__sous-chef__search_catalog", "input": {"query": "salmon"}}]}})
emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "t3", "name": "mcp__sous-chef__propose_recipe",
     "input": {"recipe": {"title": "Miso Salmon"}}}]}})
emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t3", "is_error": True,
    "content": [{"type": "text", "text": "Recipe not saved:\n- ingredient 2: No ingredient 'salmon-fillet'."}]}]}})
emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "t4", "name": "mcp__sous-chef__propose_recipe",
     "input": {"recipe": {"title": "Miso Salmon"}}}]}})
emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t4",
    "content": [{"type": "text", "text": json.dumps({"id": 42, "title": "Miso Salmon"})}]}]}})
emit({"type": "stream_event", "event": {"type": "message_start"}})
for part in ("Saved one ", "salmon dinner."):
    emit({"type": "stream_event", "event": {"type": "content_block_delta",
          "delta": {"type": "text_delta", "text": part}}})
emit({"type": "result", "subtype": "success"})
'''


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLI.replace("PYTHON", sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(chef, "claude_cli_path", lambda: str(path))
    return path


def test_a_run_streams_progress_retries_and_saved_recipes(fake_cli):
    events = list(chef.run("suggest", plan_id=1, count=1))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "status" and kinds[-1] == "done"
    labels = [e["text"] for e in events if e["type"] == "status"]
    assert "Reading your week, targets and stores" in labels
    assert "Checking the stores for salmon" in labels
    retry = next(e for e in events if e["type"] == "retry")
    assert "salmon-fillet" in retry["text"]
    assert [e for e in events if e["type"] == "recipe"] == [{"type": "recipe", "id": 42, "title": "Miso Salmon"}]
    assert next(e for e in events if e["type"] == "text")["text"] == "Saved one salmon dinner."
    assert events[-1]["saved"] == [42]


def test_a_cli_that_says_nothing_reports_why(fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "silent")
    events = list(chef.run("suggest", plan_id=1))
    err = next(e for e in events if e["type"] == "error")
    assert "not logged in" in err["text"]
    assert events[-1] == {"type": "done", "saved": []}


def test_the_mcp_config_is_cleaned_up(fake_cli, tmp_path, monkeypatch):
    made = []
    real = chef._mcp_config
    monkeypatch.setattr(chef, "_mcp_config", lambda: made.append(real()) or made[-1])
    list(chef.run("suggest", plan_id=1))
    assert made and not os.path.exists(made[0])


def test_unparseable_lines_are_ignored():
    state = chef._Run()
    assert chef.ingest("not json", state) == [] and chef.ingest("", state) == []
