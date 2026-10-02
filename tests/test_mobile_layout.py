"""Regression test for horizontal overflow at phone widths.

Drives a real browser through tools/mobile_check.py. Skipped unless Playwright
can launch Chromium and a server is running on :8766, so it guards the machine
this is developed on without making the suite depend on either.
"""

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
CHECK = ROOT / "tools" / "mobile_check.py"
CHROMIUM = os.environ.get("SOUS_CHEF_CHROMIUM", "/opt/pw-browsers/chromium")


def _server_up(port: int = 8766) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _playwright() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


pytestmark = [
    pytest.mark.skipif(not _playwright(), reason="needs Playwright"),
    pytest.mark.skipif(not _server_up(), reason="needs `sous-chef web` running on :8766"),
]


def test_no_horizontal_overflow_or_script_errors():
    args = [sys.executable, str(CHECK)]
    if Path(CHROMIUM).exists():
        args += ["--executable", CHROMIUM]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=300, cwd=ROOT)
    assert proc.returncode == 0, "The page misbehaves on a phone:\n" + proc.stdout + proc.stderr
