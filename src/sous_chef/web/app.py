"""FastAPI app: the planner's data and the recipe chef, over the shared tool layer.

The REST endpoints are deliberately thin — every one is a direct call into
`sous_chef.tools`, the same functions the MCP server exposes to Claude.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sous_chef import tools
from sous_chef.storage.db import init_db
from sous_chef.web import chef

STATIC = Path(__file__).parent / "static"

@asynccontextmanager
async def _lifespan(_app):
    init_db()
    yield


app = FastAPI(title="sous-chef", docs_url="/api/docs", lifespan=_lifespan)


@app.middleware("http")
async def _restrict_to_private_networks(request, call_next):
    """Serve loopback and the tailnet only — see web/network.py."""
    from sous_chef.web.network import is_allowed_client
    client = request.client.host if request.client else None
    if not is_allowed_client(client):
        return JSONResponse({"detail": "sous-chef serves loopback and your tailnet only."},
                            status_code=403)
    return await call_next(request)


def _call(fn, *args, **kwargs) -> Any:
    try:
        return fn(*args, **kwargs)
    except tools.ClashError as e:
        raise HTTPException(status_code=409, detail={"message": str(e), "clash": e.clash})
    except tools.ToolError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:  # surfaced rather than swallowed
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


# ── preferences ──────────────────────────────────────────────

def _with_environment(prefs: dict) -> dict:
    """Preferences plus what this machine can do, so the page can say whether
    Claude's ✨ ideas are available before anyone taps for them."""
    try:
        chef.claude_cli_path()
        prefs["claude_available"] = True
    except chef.ChefError:
        prefs["claude_available"] = False
    return prefs


@app.get("/api/prefs")
def api_prefs():
    return _with_environment(_call(tools.get_preferences))


@app.put("/api/prefs")
def api_update_prefs(changes: dict):
    changes = {k: v for k, v in changes.items()
               if k not in ("store_names", "cuisine_names", "claude_available")}
    return _with_environment(_call(tools.update_preferences, changes))


# ── plans ────────────────────────────────────────────────────

class NewPlan(BaseModel):
    week_start: str | None = None
    n_recipes: int | None = None
    stores: list[str] | None = None
    suggest: bool = True


@app.get("/api/plan")
def api_current_plan():
    """The latest plan, or null — which the page answers by starting one.

    Not a 404: a first visit is normal, and a 404 lands in the console as an error.
    """
    from sous_chef.storage import db
    latest = db.latest_plan()
    return _call(tools.get_plan, latest["id"]) if latest else None


@app.post("/api/plan")
def api_new_plan(req: NewPlan):
    plan = _call(tools.new_plan, req.week_start, req.n_recipes, req.stores)
    if req.suggest:
        count = max(tools.get_preferences()["batch_library"], plan["n_recipes"] + 1)
        plan = _call(tools.suggest_from_library, plan["id"], count)
    return plan


@app.get("/api/plan/{plan_id}")
def api_plan(plan_id: int):
    return _call(tools.get_plan, plan_id)


class PlanUpdate(BaseModel):
    week_start: str | None = None
    n_recipes: int | None = None
    stores: list[str] | None = None
    meal_prep: bool | None = None


@app.patch("/api/plan/{plan_id}")
def api_update_plan(plan_id: int, req: PlanUpdate):
    return _call(tools.update_plan, plan_id, req.week_start, req.n_recipes, req.stores,
                 req.meal_prep)


class Suggest(BaseModel):
    count: int = 4
    replace: bool = True


@app.post("/api/plan/{plan_id}/library")
def api_library_suggestions(plan_id: int, req: Suggest):
    return _call(tools.suggest_from_library, plan_id, req.count, req.replace)


class Fresh(BaseModel):
    library: int | None = None


@app.post("/api/plan/{plan_id}/fresh")
def api_fresh(plan_id: int, req: Fresh):
    """Clear the week's unchosen suggestions and refill them from the library;
    the page follows up with a chef run for Claude's share."""
    return _call(tools.fresh_suggestions, plan_id, req.library)


class RecipeRef(BaseModel):
    recipe_id: int
    servings: int | None = None


@app.post("/api/plan/{plan_id}/select")
def api_select(plan_id: int, req: RecipeRef):
    return _call(tools.select_recipe, plan_id, req.recipe_id, req.servings)


@app.post("/api/plan/{plan_id}/deselect")
def api_deselect(plan_id: int, req: RecipeRef):
    return _call(tools.deselect_recipe, plan_id, req.recipe_id)


@app.post("/api/plan/{plan_id}/dismiss")
def api_dismiss(plan_id: int, req: RecipeRef):
    _call(tools.dismiss_candidate, plan_id, req.recipe_id)
    return _call(tools.get_plan, plan_id)


@app.post("/api/plan/{plan_id}/servings")
def api_servings(plan_id: int, req: RecipeRef):
    if req.servings is None:
        raise HTTPException(status_code=422, detail="servings is required")
    return _call(tools.set_servings, plan_id, req.recipe_id, req.servings)


class CookDate(BaseModel):
    recipe_id: int
    cook_date: str | None = None


@app.post("/api/plan/{plan_id}/cook-date")
def api_cook_date(plan_id: int, req: CookDate):
    return _call(tools.set_cook_date, plan_id, req.recipe_id, req.cook_date)


class DayStatus(BaseModel):
    date: str
    status: str | None = None


@app.post("/api/plan/{plan_id}/day")
def api_day(plan_id: int, req: DayStatus):
    _call(tools.set_day_status, plan_id, req.date, req.status)
    return _call(tools.get_plan, plan_id)


@app.get("/api/plan/{plan_id}/grocery")
def api_grocery(plan_id: int):
    return _call(tools.get_grocery_list, plan_id)


class Check(BaseModel):
    ingredient_id: str
    checked: bool = True


@app.post("/api/plan/{plan_id}/grocery/check")
def api_grocery_check(plan_id: int, req: Check):
    return _call(tools.check_grocery_item, plan_id, req.ingredient_id, req.checked)


@app.get("/api/plan/{plan_id}/calendar.ics")
def api_calendar(plan_id: int):
    """Cook blocks as an .ics file. Opened on an iPhone it offers "Add All"."""
    ics = _call(tools.calendar_ics, plan_id)
    return Response(content=ics, media_type="text/calendar; charset=utf-8", headers={
        "Content-Disposition": f'inline; filename="meal-plan-{plan_id}.ics"'})


# ── the chef ─────────────────────────────────────────────────

class ChefRequest(BaseModel):
    task: str                      # suggest | craft | import
    plan_id: int | None = None
    count: int = 3
    text: str = ""


def _sse(events) -> StreamingResponse:
    def stream():
        for event in events:
            # A comment line keeps an idle connection from being dropped.
            yield ": keepalive\n\n" if event is None else f"data: {json.dumps(event)}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _follow(job: chef.Job, attached: bool):
    if attached:
        yield {"type": "attached", "task": job.task}
    yield from job.follow()


@app.post("/api/chef")
def api_chef(req: ChefRequest):
    """Start a chef run and stream it — or, if one is already running for this
    plan, stream that one instead of starting a second."""
    try:
        job, attached = chef.start_or_attach(req.task, plan_id=req.plan_id, count=req.count,
                                             text=req.text)
    except chef.ChefError as e:
        return _sse([{"type": "error", "text": str(e)}, {"type": "done", "saved": []}])
    return _sse(_follow(job, attached))


@app.get("/api/chef/stream")
def api_chef_stream(plan_id: int | None = None, recent: bool = False, task: str | None = None):
    """Reconnect to the plan's chef run (or with task=prices, the price check)
    after a dropped stream.

    Replays it from the start. With `recent`, a run that finished while the
    page was away is replayed too, so it can show what was saved; otherwise
    an idle plan answers with a single `idle` event.
    """
    job = chef.current_job(plan_id, recent=recent, task=task)
    if not job:
        return _sse([{"type": "idle"}])
    return _sse(_follow(job, attached=True))


# ── prices online ────────────────────────────────────────────

@app.get("/api/prices")
def api_price_status():
    """When prices were last checked online, what changed, and what is due."""
    status = _call(tools.price_check_status)
    status["running"] = chef.current_job(None, task="prices") is not None
    return status


class PriceCheck(BaseModel):
    limit: int | None = None


@app.post("/api/prices/check")
def api_price_check(req: PriceCheck):
    """Check prices online now, streaming progress — or follow the check
    already running."""
    count = req.limit or tools.get_preferences()["price_check_items"]
    if not 1 <= count <= 40:
        raise HTTPException(status_code=422, detail="a price check looks up 1–40 prices")
    try:
        job, attached = chef.start_or_attach("prices", count=count)
    except chef.ChefError as e:
        return _sse([{"type": "error", "text": str(e)}, {"type": "done", "saved": []}])
    return _sse(_follow(job, attached))


# ── recipes ──────────────────────────────────────────────────

@app.get("/api/recipes")
def api_recipes(q: str = "", cuisine: str | None = None):
    return _call(tools.list_recipes, q, cuisine)


@app.get("/api/recipe/{recipe_id}")
def api_recipe(recipe_id: int, servings: int | None = None, plan_id: int | None = None):
    return _call(tools.get_recipe, recipe_id, servings, plan_id)


class Keep(BaseModel):
    keep: bool = True
    on_clash: str | None = None          # new | replace, once the user has chosen


@app.post("/api/recipe/{recipe_id}/library")
def api_keep(recipe_id: int, req: Keep):
    """Save to (or remove from) the library. A near-copy of a recipe already
    there answers 409 with the clash, for the page to ask what to do."""
    return _call(tools.save_to_library, recipe_id, req.keep, req.on_clash)


@app.delete("/api/recipe/{recipe_id}")
def api_delete_recipe(recipe_id: int):
    return _call(tools.delete_recipe, recipe_id)


# ── catalog, prices, pantry ──────────────────────────────────

@app.get("/api/catalog")
def api_catalog(q: str = "", aisle: str | None = None, limit: int = 0):
    return _call(tools.search_catalog, q, aisle, limit)


class Price(BaseModel):
    ingredient_id: str
    store: str
    price: float
    pkg_qty: float | None = None
    pkg_unit: str | None = None
    product: str | None = None


@app.put("/api/price")
def api_price(req: Price):
    return _call(tools.set_price, req.ingredient_id, req.store, req.price, req.pkg_qty,
                 req.pkg_unit, req.product)


class Carried(BaseModel):
    ingredient_id: str
    store: str
    carried: bool


@app.post("/api/carried")
def api_carried(req: Carried):
    return _call(tools.set_carried, req.ingredient_id, req.store, req.carried)


class PantryItem(BaseModel):
    ingredient_id: str
    have: bool = True


@app.get("/api/pantry")
def api_pantry():
    return _call(tools.get_pantry)


@app.post("/api/pantry")
def api_set_pantry(req: PantryItem):
    return _call(tools.set_pantry, req.ingredient_id, req.have)


class PantryBatch(BaseModel):
    ingredient_ids: list[str]


@app.post("/api/pantry/stock")
def api_stock_pantry(req: PantryBatch):
    """After a shop: everything mostly left over goes into the pantry at once."""
    return [_call(tools.set_pantry, iid, True) for iid in req.ingredient_ids]


# ── static UI ────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def start_price_schedule(interval: float = 3600.0, first: float = 300.0) -> None:
    """Check prices online every `price_refresh_days` (Settings), while the server runs.

    Looks hourly whether a check is due — cheap, and an hour is the most a
    laptop that slept through the due time waits. Started by the server, not
    at import, so tests and the MCP server never schedule anything.
    """
    import logging
    import threading
    import time

    log = logging.getLogger("sous_chef.prices")

    def loop() -> None:
        time.sleep(first)
        while True:
            try:
                if tools.price_check_due() and not chef.current_job(None, task="prices"):
                    chef.claude_cli_path()
                    count = tools.get_preferences()["price_check_items"]
                    chef.start_or_attach("prices", count=count, scheduled=True)
                    log.info("Checking %d prices online", count)
            except chef.ChefError:
                pass                      # no claude CLI here: nothing to run
            except Exception:
                log.exception("price schedule")
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True, name="price-schedule").start()


def serve(host: str = "127.0.0.1", port: int = 8766, reload: bool = False) -> None:
    import uvicorn
    if not reload:
        start_price_schedule()
    uvicorn.run("sous_chef.web.app:app" if reload else app,
                host=host, port=port, reload=reload, log_level="info")


def serve_sockets(sockets: list, port: int = 8766, await_tailnet: bool = False,
                  log_level: str = "info") -> None:
    """Serve on already-open sockets — loopback plus the tailnet when present.

    With `await_tailnet`, a background thread keeps looking for the tailnet and
    starts serving on it the moment it appears, since at login the app usually
    wins the race against Tailscale.
    """
    import uvicorn

    if await_tailnet:
        _watch_for_tailnet(port, log_level)
    start_price_schedule()
    uvicorn.Server(uvicorn.Config(app, log_level=log_level)).run(sockets=sockets)


def _watch_for_tailnet(port: int, log_level: str, interval: float = 20.0) -> None:
    import logging
    import threading
    import time

    from sous_chef.web.network import NoTailnet, _listen, tailscale_ip

    log = logging.getLogger("sous_chef.tailnet")

    def serve_on(sock) -> None:
        import uvicorn
        uvicorn.Server(uvicorn.Config(app, log_level=log_level)).run(sockets=[sock])

    def watch() -> None:
        while True:
            time.sleep(interval)
            try:
                addr = tailscale_ip()
            except NoTailnet:
                continue
            try:
                sock = _listen(addr, port)
            except OSError:
                continue
            log.info("Tailscale is up — now also serving on http://%s:%d", addr, port)
            threading.Thread(target=serve_on, args=(sock,), daemon=True).start()
            return

    threading.Thread(target=watch, daemon=True).start()
