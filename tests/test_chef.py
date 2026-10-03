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
if os.environ.get("FAKE_LOG"):
    open(os.environ["FAKE_LOG"], "a").write("launch\n")
if mode == "silent":
    print("not logged in", file=sys.stderr); sys.exit(1)
emit({"type": "system", "subtype": "init", "session_id": "s1"})
emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "t1", "name": "mcp__sous-chef__plan_context", "input": {"plan_id": 1}}]}})
emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}]}})
import time; time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
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
    assert events[-1]["type"] == "done" and events[-1]["saved"] == []


def test_the_mcp_config_is_cleaned_up(fake_cli, tmp_path, monkeypatch):
    made = []
    real = chef._mcp_config
    monkeypatch.setattr(chef, "_mcp_config", lambda: made.append(real()) or made[-1])
    list(chef.run("suggest", plan_id=1))
    assert made and not os.path.exists(made[0])


def test_unparseable_lines_are_ignored():
    state = chef._Run()
    assert chef.ingest("not json", state) == [] and chef.ingest("", state) == []


# ── one run per plan ─────────────────────────────────────────

@pytest.fixture
def slow_cli(fake_cli, tmp_path, monkeypatch):
    log = tmp_path / "launches"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_DELAY", "1.0")
    monkeypatch.setattr(chef, "_jobs", {})
    return log


def _drain(job):
    return [e for e in job.follow(keepalive=0.2) if e is not None]


def test_a_second_request_joins_the_running_job(slow_cli):
    """The bug: a phone that dropped its stream and tapped again started a
    second CLI run on the same week, and both saved recipes."""
    first, attached1 = chef.start_or_attach("suggest", plan_id=7)
    second, attached2 = chef.start_or_attach("suggest", plan_id=7)
    assert second is first and not attached1 and attached2
    events = _drain(first)
    assert events[-1]["type"] == "done"
    assert slow_cli.read_text().count("launch") == 1


def test_following_late_replays_the_whole_run(slow_cli):
    job, _ = chef.start_or_attach("suggest", plan_id=7)
    early = _drain(job)
    late = _drain(job)            # a reconnect after it finished
    assert late == early and any(e["type"] == "recipe" for e in late)


def test_different_plans_run_independently(slow_cli):
    a, _ = chef.start_or_attach("suggest", plan_id=1)
    b, _ = chef.start_or_attach("suggest", plan_id=2)
    assert a is not b
    _drain(a), _drain(b)
    assert slow_cli.read_text().count("launch") == 2


def test_a_finished_job_starts_fresh_next_time(slow_cli):
    first, _ = chef.start_or_attach("suggest", plan_id=7)
    _drain(first)
    second, attached = chef.start_or_attach("suggest", plan_id=7)
    assert second is not first and not attached
    _drain(second)


def test_reconnecting_finds_running_and_recent_jobs(slow_cli):
    assert chef.current_job(7) is None
    job, _ = chef.start_or_attach("suggest", plan_id=7)
    assert chef.current_job(7) is job
    _drain(job)
    assert chef.current_job(7) is None                 # idle unless asking for recent
    assert chef.current_job(7, recent=True) is job


def test_an_idle_follower_gets_keepalives(slow_cli):
    job, _ = chef.start_or_attach("suggest", plan_id=7)
    seen = list(job.follow(keepalive=0.2))
    assert None in seen and seen[-1]["type"] == "done"


def test_a_bad_request_is_refused_before_anything_starts(slow_cli):
    with pytest.raises(chef.ChefError):
        chef.start_or_attach("craft", plan_id=7, text="")
    assert chef.current_job(7) is None and not slow_cli.exists()
