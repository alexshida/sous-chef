"""Shared test setup.

Paths are read from the environment when `sous_chef.config` is imported, so
they are set here, before any test module imports the package — tests get
their own SQLite file and config and never touch ~/.sous-chef.
"""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="sous-chef-test-")
os.environ["SOUS_CHEF_DIR"] = _tmp
os.environ["SOUS_CHEF_DB_PATH"] = os.path.join(_tmp, "test.db")

import pytest  # noqa: E402


@pytest.fixture
def fresh_db():
    """A newly seeded database and default preferences, for each test that asks."""
    from sous_chef import config
    from sous_chef.calendars import _CACHE
    from sous_chef.storage import db

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(str(config.DB_PATH) + suffix)
        except FileNotFoundError:
            pass
    if config.CONFIG_FILE.exists():
        config.CONFIG_FILE.unlink()
    _CACHE.clear()
    db.init_db()
    yield


@pytest.fixture
def catalog(fresh_db):
    from sous_chef.storage import db
    return db.all_ingredients()


def recipe(title="Test", servings=4, ingredients=(), steps=("Cook it.",), **kw):
    """A recipe dict in the stored shape: ingredients as (id, qty, unit[, optional])."""
    lines = []
    for t in ingredients:
        iid, qty, unit, *rest = t
        lines.append({"id": iid, "qty": qty, "unit": unit, "prep": "",
                      "optional": bool(rest and rest[0])})
    return {"id": kw.pop("id", None), "title": title, "cuisine": kw.pop("cuisine", "other"),
            "summary": "", "servings": servings, "active_min": kw.pop("active_min", 20),
            "total_min": kw.pop("total_min", 30), "ingredients": lines, "steps": list(steps),
            "tags": [], **kw}
