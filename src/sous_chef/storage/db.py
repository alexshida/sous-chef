"""SQLite storage — the catalog, your prices and pantry, recipes, and weekly plans.

Two processes write here: the web app, and the MCP server that the `claude`
CLI starts while it proposes recipes. WAL mode lets them do so without
blocking each other's reads.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from sous_chef.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingredients (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    aisle TEXT NOT NULL,
    perishable INTEGER NOT NULL DEFAULT 0,
    g_per_cup REAL,
    unit_g_json TEXT NOT NULL DEFAULT '{}',
    kcal REAL, protein REAL, carbs REAL, fat REAL, fiber REAL,
    source TEXT NOT NULL DEFAULT 'seed',          -- seed | claude | user
    created_at TEXT
);

-- What a store sells an ingredient as. `source` says how much to trust the
-- price: seed and scaled are starting estimates, claude is the model's guess,
-- user is a price you entered. carried = 0 records "this store doesn't have
-- it" so that reseeding does not put it back.
CREATE TABLE IF NOT EXISTS offers (
    ingredient_id TEXT NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
    store TEXT NOT NULL,
    product TEXT,
    pkg_qty REAL NOT NULL,
    pkg_unit TEXT NOT NULL,
    price REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'seed',
    carried INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT,
    PRIMARY KEY (ingredient_id, store)
);

CREATE TABLE IF NOT EXISTS pantry (
    ingredient_id TEXT PRIMARY KEY REFERENCES ingredients(id) ON DELETE CASCADE,
    added_at TEXT
);

-- in_library = 0 for suggestions you have not chosen yet, so that asking for
-- fresh ideas a dozen times does not bury the recipes you actually cook.
CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed_key TEXT UNIQUE,
    title TEXT NOT NULL,
    cuisine TEXT NOT NULL DEFAULT 'other',
    summary TEXT NOT NULL DEFAULT '',
    servings INTEGER NOT NULL,
    active_min INTEGER NOT NULL DEFAULT 0,
    total_min INTEGER NOT NULL DEFAULT 0,
    ingredients_json TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL,                          -- library | claude | craft | import
    in_library INTEGER NOT NULL DEFAULT 1,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL,
    n_recipes INTEGER NOT NULL,
    stores_json TEXT NOT NULL,
    day_status_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS plan_candidates (
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    origin TEXT NOT NULL,                          -- library | claude | craft | import
    dismissed INTEGER NOT NULL DEFAULT 0,
    added_at TEXT,
    PRIMARY KEY (plan_id, recipe_id)
);

CREATE TABLE IF NOT EXISTS plan_recipes (
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    servings INTEGER NOT NULL,
    cook_date TEXT,
    added_at TEXT,
    PRIMARY KEY (plan_id, recipe_id)
);

CREATE TABLE IF NOT EXISTS grocery_checks (
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    ingredient_id TEXT NOT NULL,
    checked INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (plan_id, ingredient_id)
);

CREATE INDEX IF NOT EXISTS idx_offers_store ON offers(store);
CREATE INDEX IF NOT EXISTS idx_plans_week ON plans(week_start);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create the schema and bring the seeded catalog up to date."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _seed(conn)


def _seed(conn: sqlite3.Connection) -> None:
    from sous_chef.catalog import recipes as seed_recipes
    from sous_chef.catalog import seed

    fresh = conn.execute("SELECT COUNT(*) FROM ingredients").fetchone()[0] == 0
    now = _now()
    for iid, name, aisle, perishable, g_per_cup, unit_g, nut in seed.INGREDIENTS:
        conn.execute(
            """INSERT INTO ingredients (id, name, aisle, perishable, g_per_cup, unit_g_json,
                   kcal, protein, carbs, fat, fiber, source, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'seed',?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, aisle=excluded.aisle, perishable=excluded.perishable,
                   g_per_cup=excluded.g_per_cup, unit_g_json=excluded.unit_g_json,
                   kcal=excluded.kcal, protein=excluded.protein, carbs=excluded.carbs,
                   fat=excluded.fat, fiber=excluded.fiber
               WHERE ingredients.source = 'seed'""",
            (iid, name, aisle, int(perishable), g_per_cup, json.dumps(unit_g), *nut, now))
    for o in seed.offers():
        # A price you entered, the model's estimate, or "not carried here" all
        # outrank a seed — reseeding only refreshes what is still a seed.
        conn.execute(
            """INSERT INTO offers (ingredient_id, store, product, pkg_qty, pkg_unit, price,
                   source, carried, updated_at)
               VALUES (:ingredient_id, :store, :product, :pkg_qty, :pkg_unit, :price,
                   :source, 1, :now)
               ON CONFLICT(ingredient_id, store) DO UPDATE SET
                   product=excluded.product, pkg_qty=excluded.pkg_qty,
                   pkg_unit=excluded.pkg_unit, price=excluded.price, source=excluded.source
               WHERE offers.source IN ('seed', 'scaled') AND offers.carried = 1""",
            {**o, "now": now})
    # Checked first rather than INSERT OR IGNORE: an ignored insert still
    # advances AUTOINCREMENT, so every startup would burn sixteen ids.
    have = {r[0] for r in conn.execute("SELECT seed_key FROM recipes WHERE seed_key IS NOT NULL")}
    for r in seed_recipes.RECIPES:
        if r["key"] in have:
            continue
        conn.execute(
            """INSERT INTO recipes (seed_key, title, cuisine, summary, servings,
                   active_min, total_min, ingredients_json, steps_json, tags_json, source,
                   in_library, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,'library',1,?)""",
            (r["key"], r["title"], r["cuisine"], r["summary"], r["servings"],
             r["active_min"], r["total_min"],
             json.dumps([_line(t) for t in r["ingredients"]]),
             json.dumps(r["steps"]), json.dumps(r.get("tags", [])), now))
    if fresh:
        for iid in seed.DEFAULT_PANTRY:
            conn.execute("INSERT OR IGNORE INTO pantry (ingredient_id, added_at) VALUES (?,?)",
                         (iid, now))


def _line(t: tuple) -> dict:
    iid, qty, unit, prep, *rest = t
    return {"id": iid, "qty": qty, "unit": unit, "prep": prep, "optional": bool(rest and rest[0])}


# ── ingredients & offers ─────────────────────────────────────

def _ingredient(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["unit_g"] = json.loads(d.pop("unit_g_json") or "{}")
    d["perishable"] = bool(d["perishable"])
    return d


def all_ingredients() -> dict[str, dict]:
    """Every ingredient with its offers, keyed by id."""
    with get_conn() as conn:
        ings = {r["id"]: {**_ingredient(r), "offers": {}}
                for r in conn.execute("SELECT * FROM ingredients")}
        for o in conn.execute("SELECT * FROM offers"):
            if o["ingredient_id"] in ings:
                ings[o["ingredient_id"]]["offers"][o["store"]] = dict(o)
    return ings


def get_ingredient(iid: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM ingredients WHERE id = ?", (iid,)).fetchone()
        if not row:
            return None
        ing = _ingredient(row)
        ing["offers"] = {o["store"]: dict(o) for o in conn.execute(
            "SELECT * FROM offers WHERE ingredient_id = ?", (iid,))}
    return ing


def insert_ingredient(ing: dict, offers: list[dict]) -> None:
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO ingredients (id, name, aisle, perishable, g_per_cup, unit_g_json,
                   kcal, protein, carbs, fat, fiber, source, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ing["id"], ing["name"], ing["aisle"], int(ing.get("perishable", False)),
             ing.get("g_per_cup"), json.dumps(ing.get("unit_g") or {}),
             ing["kcal"], ing["protein"], ing["carbs"], ing["fat"], ing["fiber"],
             ing.get("source", "claude"), now))
        for o in offers:
            conn.execute(
                """INSERT INTO offers (ingredient_id, store, product, pkg_qty, pkg_unit, price,
                       source, carried, updated_at) VALUES (?,?,?,?,?,?,?,1,?)""",
                (ing["id"], o["store"], o.get("product") or ing["name"], o["pkg_qty"],
                 o["pkg_unit"], o["price"], o.get("source", "claude"), now))


def upsert_offer(iid: str, store: str, *, price: float, pkg_qty: float, pkg_unit: str,
                 product: str, source: str = "user") -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO offers (ingredient_id, store, product, pkg_qty, pkg_unit, price,
                   source, carried, updated_at) VALUES (?,?,?,?,?,?,?,1,?)
               ON CONFLICT(ingredient_id, store) DO UPDATE SET
                   product=excluded.product, pkg_qty=excluded.pkg_qty,
                   pkg_unit=excluded.pkg_unit, price=excluded.price,
                   source=excluded.source, carried=1, updated_at=excluded.updated_at""",
            (iid, store, product, pkg_qty, pkg_unit, price, source, _now()))


def set_carried(iid: str, store: str, carried: bool) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE offers SET carried = ?, updated_at = ? WHERE ingredient_id = ? AND store = ?",
            (int(carried), _now(), iid, store))
        return cur.rowcount > 0


# ── pantry ───────────────────────────────────────────────────

def pantry_ids() -> set[str]:
    with get_conn() as conn:
        return {r[0] for r in conn.execute("SELECT ingredient_id FROM pantry")}


def set_pantry(iid: str, have: bool) -> None:
    with get_conn() as conn:
        if have:
            conn.execute("INSERT OR IGNORE INTO pantry (ingredient_id, added_at) VALUES (?,?)",
                         (iid, _now()))
        else:
            conn.execute("DELETE FROM pantry WHERE ingredient_id = ?", (iid,))


# ── recipes ──────────────────────────────────────────────────

def _recipe(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["ingredients"] = json.loads(d.pop("ingredients_json"))
    d["steps"] = json.loads(d.pop("steps_json"))
    d["tags"] = json.loads(d.pop("tags_json") or "[]")
    d["in_library"] = bool(d["in_library"])
    return d


def get_recipe(rid: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM recipes WHERE id = ?", (rid,)).fetchone()
    return _recipe(row) if row else None


def list_recipes(*, library_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM recipes" + (" WHERE in_library = 1" if library_only else "")
    with get_conn() as conn:
        return [_recipe(r) for r in conn.execute(sql + " ORDER BY id")]


def insert_recipe(r: dict, *, source: str, in_library: bool) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO recipes (title, cuisine, summary, servings, active_min, total_min,
                   ingredients_json, steps_json, tags_json, source, in_library, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["title"], r["cuisine"], r.get("summary", ""), r["servings"], r["active_min"],
             r["total_min"], json.dumps(r["ingredients"]), json.dumps(r["steps"]),
             json.dumps(r.get("tags", [])), source, int(in_library), _now()))
        return int(cur.lastrowid)


def set_in_library(rid: int, in_library: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE recipes SET in_library = ? WHERE id = ?", (int(in_library), rid))


def delete_recipe(rid: int) -> bool:
    with get_conn() as conn:
        return conn.execute("DELETE FROM recipes WHERE id = ?", (rid,)).rowcount > 0


def recently_cooked(since_week: str) -> set[int]:
    """Recipes chosen in any plan whose week starts on or after `since_week`."""
    with get_conn() as conn:
        return {r[0] for r in conn.execute(
            """SELECT pr.recipe_id FROM plan_recipes pr JOIN plans p ON p.id = pr.plan_id
               WHERE p.week_start >= ?""", (since_week,))}


# ── plans ────────────────────────────────────────────────────

def _plan(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["stores"] = json.loads(d.pop("stores_json"))
    d["day_status"] = json.loads(d.pop("day_status_json") or "{}")
    return d


def create_plan(week_start: str, n_recipes: int, stores: list[str]) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO plans (week_start, n_recipes, stores_json, created_at) VALUES (?,?,?,?)",
            (week_start, n_recipes, json.dumps(stores), _now()))
        return int(cur.lastrowid)


def get_plan(pid: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM plans WHERE id = ?", (pid,)).fetchone()
    return _plan(row) if row else None


def latest_plan() -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    return _plan(row) if row else None


def update_plan(pid: int, **fields: Any) -> None:
    cols = {"week_start": "week_start", "n_recipes": "n_recipes",
            "stores": "stores_json", "day_status": "day_status_json"}
    sets, vals = [], []
    for k, v in fields.items():
        sets.append(f"{cols[k]} = ?")
        vals.append(json.dumps(v) if k in ("stores", "day_status") else v)
    if sets:
        with get_conn() as conn:
            conn.execute(f"UPDATE plans SET {', '.join(sets)} WHERE id = ?", (*vals, pid))


def add_candidate(pid: int, rid: int, origin: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO plan_candidates (plan_id, recipe_id, origin, dismissed, added_at)
               VALUES (?,?,?,0,?)
               ON CONFLICT(plan_id, recipe_id) DO UPDATE SET dismissed = 0""",
            (pid, rid, origin, _now()))


def candidates(pid: int, *, include_dismissed: bool = False) -> list[dict]:
    sql = "SELECT * FROM plan_candidates WHERE plan_id = ?"
    if not include_dismissed:
        sql += " AND dismissed = 0"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY added_at, recipe_id", (pid,))]


def dismiss_candidate(pid: int, rid: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE plan_candidates SET dismissed = 1 WHERE plan_id = ? AND recipe_id = ?",
                     (pid, rid))


def selections(pid: int) -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM plan_recipes WHERE plan_id = ? ORDER BY added_at, recipe_id", (pid,))]


def select_recipe(pid: int, rid: int, servings: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO plan_recipes (plan_id, recipe_id, servings, added_at) VALUES (?,?,?,?)
               ON CONFLICT(plan_id, recipe_id) DO UPDATE SET servings = excluded.servings""",
            (pid, rid, servings, _now()))


def deselect_recipe(pid: int, rid: int) -> bool:
    with get_conn() as conn:
        return conn.execute("DELETE FROM plan_recipes WHERE plan_id = ? AND recipe_id = ?",
                            (pid, rid)).rowcount > 0


def update_selection(pid: int, rid: int, **fields: Any) -> bool:
    sets = [f"{k} = ?" for k in fields]
    with get_conn() as conn:
        return conn.execute(
            f"UPDATE plan_recipes SET {', '.join(sets)} WHERE plan_id = ? AND recipe_id = ?",
            (*fields.values(), pid, rid)).rowcount > 0


def grocery_checks(pid: int) -> set[str]:
    with get_conn() as conn:
        return {r[0] for r in conn.execute(
            "SELECT ingredient_id FROM grocery_checks WHERE plan_id = ? AND checked = 1", (pid,))}


def set_grocery_check(pid: int, iid: str, checked: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO grocery_checks (plan_id, ingredient_id, checked) VALUES (?,?,?)
               ON CONFLICT(plan_id, ingredient_id) DO UPDATE SET checked = excluded.checked""",
            (pid, iid, int(checked)))
