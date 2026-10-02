"""Regression tests for horizontal overflow at phone widths.

Both drive a real browser through tools/mobile_check.py and are skipped
unless Playwright can launch Chromium.

- The stress test serves its own data, built to be awkward: a dozen shared
  ingredients, a long "not at your stores" list, long titles. Real plans grow
  tags like these, and a tag that cannot wrap pushes the whole page sideways.
- The other checks whatever `sous-chef web` on :8766 is showing, when it runs.
"""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
CHECK = ROOT / "tools" / "mobile_check.py"
CHROMIUM = os.environ.get("SOUS_CHEF_CHROMIUM", "/opt/pw-browsers/chromium")


def _server_up(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _playwright() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            kw = {"executable_path": CHROMIUM} if Path(CHROMIUM).exists() else {}
            pw.chromium.launch(**kw).close()
        return True
    except Exception:
        return False


needs_browser = pytest.mark.skipif(not _playwright(), reason="needs Playwright and Chromium")


def _check(url: str) -> subprocess.CompletedProcess:
    args = [sys.executable, str(CHECK), "--url", url]
    if Path(CHROMIUM).exists():
        args += ["--executable", CHROMIUM]
    return subprocess.run(args, capture_output=True, text=True, timeout=300, cwd=ROOT)


@pytest.fixture
def awkward_plan(fresh_db):
    from sous_chef import tools
    produce = ["cilantro", "scallions", "baby-spinach", "bell-pepper", "cucumber",
               "cherry-tomatoes", "zucchini", "mushrooms", "parsley", "basil",
               "bok-choy", "broccoli"]
    lines = [{"id": i, "qty": 0.25, "unit": "cup"} for i in produce]
    base = {"cuisine": "other", "summary": "A deliberately awkward recipe for layout tests.",
            "servings": 4, "active_min": 20, "total_min": 30, "steps": ["Cook."]}
    plan = tools.new_plan(week_start="2026-10-05")
    chosen = tools.propose_recipe({**base, "title": "Every Green Thing Skillet",
                                   "ingredients": lines}, plan["id"], "craft")
    tools.select_recipe(plan["id"], chosen["id"])
    tools.propose_recipe({**base, "title": "An Extraordinarily Long Recipe Title That Keeps "
                                           "Going Well Past What Fits On One Line Of A Phone",
                          "ingredients": lines + [{"id": "paneer", "qty": 8, "unit": "oz"},
                                                  {"id": "red-lentils", "qty": 1, "unit": "cup"},
                                                  {"id": "fish-sauce", "qty": 1, "unit": "tbsp"},
                                                  {"id": "jalapeno", "qty": 2, "unit": "each"}]},
                         plan["id"])
    return plan


@needs_browser
def test_long_tags_wrap_instead_of_widening_the_page(awkward_plan):
    import uvicorn

    from sous_chef.web.app import app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    try:
        for _ in range(50):
            if _server_up(port):
                break
            time.sleep(0.1)
        proc = _check(f"http://127.0.0.1:{port}")
    finally:
        server.should_exit = True
    assert proc.returncode == 0, "The page overflows on a phone:\n" + proc.stdout + proc.stderr


@needs_browser
@pytest.mark.skipif(not _server_up(8766), reason="needs `sous-chef web` running on :8766")
def test_the_running_app_fits_a_phone():
    proc = _check("http://localhost:8766")
    assert proc.returncode == 0, "The page misbehaves on a phone:\n" + proc.stdout + proc.stderr
