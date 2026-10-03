"""`sous-chef doctor`: is everything set up, and if not, what to do about it.

Each check returns a status — ok, warn (optional and missing), or fail
(sous-chef cannot work) — with one line of what was found and, when it is not
ok, one line of what to do. The install script runs it at the end, and it is
the first thing to try when something does not work.
"""

from __future__ import annotations

import subprocess
import sys
import urllib.request
from dataclasses import dataclass

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    found: str
    fix: str = ""


def check_python() -> Check:
    v = sys.version_info
    found = f"Python {v.major}.{v.minor}.{v.micro}"
    if v < (3, 10):
        return Check("Python", FAIL, found, "Install Python 3.10 or newer from python.org, then run ./install.sh again.")
    return Check("Python", OK, found)


def check_data() -> Check:
    from sous_chef.config import DB_PATH, SOUS_CHEF_DIR
    try:
        from sous_chef.storage import db
        db.init_db()
        n = len(db.list_recipes())
    except Exception as e:
        return Check("Data", FAIL, f"{SOUS_CHEF_DIR}: {type(e).__name__}: {e}",
                     f"Make sure {SOUS_CHEF_DIR} is a folder you can write to.")
    return Check("Data", OK, f"{DB_PATH} ({n} recipes in your library)")


def check_claude() -> Check:
    from sous_chef.web.chef import ChefError, claude_cli_path
    try:
        path = claude_cli_path()
    except ChefError:
        return Check("Claude", WARN, "the `claude` command is not installed",
                     "Optional, for ✨ suggestions: run  curl -fsSL https://claude.ai/install.sh | bash  "
                     "then run  claude  once to sign in.")
    try:
        version = subprocess.run([path, "--version"], capture_output=True, text=True,
                                 timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        version = ""
    return Check("Claude", OK, f"{path} {version}".strip(),
                 "If ✨ suggestions say you're signed out, run  claude  once in a terminal.")


def check_tailscale(port: int) -> Check:
    from sous_chef.web.network import NoTailnet, tailscale_ip
    try:
        addr = tailscale_ip()
    except NoTailnet as e:
        return Check("Phone access", WARN, str(e).split(" — ")[0].split(". See")[0],
                     "Optional, to use it from your phone: install Tailscale on this computer and "
                     "your phone (tailscale.com/download) and sign in to the same account on both.")
    return Check("Phone access", OK, f"open http://{addr}:{port} on your phone (Tailscale)")


def check_server(port: int) -> Check:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/prefs", timeout=2) as r:
            if r.status == 200:
                return Check("Server", OK, f"running at http://localhost:{port}")
    except OSError:
        pass
    return Check("Server", WARN, f"nothing answering on port {port}",
                 "Start it with  sous-chef web  (or keep it running with  sous-chef install-service).")


def check_service() -> Check | None:
    if sys.platform != "darwin":
        return None
    from sous_chef.main import _plist_path
    if _plist_path().exists():
        return Check("Background service", OK, "installed — starts at login (sous-chef restart after updates)")
    return Check("Background service", WARN, "not installed",
                 "Optional: sous-chef install-service keeps it running without a terminal open.")


def run_checks(port: int) -> list[Check]:
    checks = [check_python(), check_data(), check_claude(), check_server(port),
              check_tailscale(port), check_service()]
    return [c for c in checks if c is not None]
