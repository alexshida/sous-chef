"""The recipe chef: the `claude` CLI with the sous-chef MCP server attached.

Routing through the CLI rather than the API means your existing Claude
subscription pays for it — no API key — and, as in Trainer, the model gets the
real tools: it searches the actual catalog, sees which ingredients your stores
carry, and saves recipes through a validated interface that computes their
cost and nutrition. It never hands back prose for this code to parse.

Four jobs, each one CLI run:

  suggest — propose N new recipes for the week
  craft   — write the one recipe you described ("something with miso and salmon")
  import  — convert a recipe you pasted (text, or a link) into the catalog
  prices  — look up current prices on the stores' own listings (web search)

Progress is streamed to the browser as each tool call happens, and every
recipe appears the moment propose_recipe saves it.

One run per plan. A run belongs to the server, not to the request that
started it: it is driven by a background thread into a `Job` that any number
of requests can follow, replaying from the start. A phone that locks mid-run
drops its stream, but the run carries on, and the page reconnects to it rather
than starting a second one — two runs on one week each read the same
"already offered" list and saved near-identical recipes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from sous_chef.config import DEFAULT_MODEL

PREFIX = "mcp__sous-chef__"

SYSTEM_PROMPT = """\
You are the recipe chef inside sous-chef, a weekly meal planner, with live
access to the user's grocery catalog, pantry and week through the `sous-chef`
MCP tools.

You write the recipes; the tools do the arithmetic. Never state a cost, calorie
count or protein figure you worked out yourself — propose_recipe and
check_recipe compute them from the catalog and return them.

Writing a recipe:
1. Use only catalog ingredients available at the plan's stores.
   search_catalog(query, plan_id) gives each one's id, its allowed units,
   nutrition per 100 g and `available`. Search broadly ("chicken", "beans",
   "greens") rather than one item at a time.
2. add_ingredient only for something essential the catalog genuinely lacks,
   with honest nutrition, and offers only at stores you are confident carry
   it. Never add a twin of an existing ingredient to make it look available:
   when converting the user's own recipe, use the existing ingredient even if
   their stores don't sell it — the app tells them where to buy it.
3. Quantities are for the recipe's own servings (write for 4 unless told
   otherwise), in a unit listed for that ingredient, measured the way an
   American home cook measures: lb or oz for meat and fish, cans, cups,
   tbsp and tsp, cloves, bunches and counts — not grams.
4. Hit the per-serving targets from plan_context — protein and fiber above all
   — without overshooting wildly: one main protein at a normal portion (about
   4–6 oz meat or fish per serving) plus plant protein beats stacking two
   meats. Beans, lentils, edamame, tofu, Greek yogurt, whole grains and
   vegetables are the levers. Respect `avoid` absolutely, keep weeknight
   recipes within max_weeknight_min total, and use only the listed equipment.
5. Prefer recipes that use up the `use_up` perishables and share fresh
   ingredients with what is already chosen — that is how whole packages get
   used rather than thrown out.
6. Steps: one action each, with heat levels, times and doneness cues, written
   for someone cooking after work.
   When plan_context has a meal_prep_brief, follow it: batch recipes for the
   stated servings that keep, freeze and reheat well.
7. Save each finished recipe with propose_recipe. If it reports a problem, fix
   exactly that and call it again.

Your final message is one or two short lines: the recipes show up as cards on
their own, with their numbers.
"""

PRICE_PROMPT = """\
You check grocery prices for sous-chef, a weekly meal planner, using web
search and the `sous-chef` MCP tools. A wrong price is worse than an old one.

1. Call prices_to_check first. Each item is one ingredient at one store, with
   the product and package it is listed as now and the current estimate.
2. For each item, find that store's own current listing for a matching
   product. Search first: a result from the store's own product page that
   shows the price and size is a listing — use that page's URL. Read a page
   only when the result does not show the price. Store sites: Trader Joe's
   (traderjoes.com), QFC (a Kroger store, qfc.com), Costco (costco.com, whose
   online prices can run above the warehouse's), PCC Community Markets. A
   delivery listing for that same store, such as Instacart, is acceptable
   when the store publishes none. Articles, blogs and other stores are not.
3. Record it with record_price, exactly as listed: the price, the package
   that price buys (count, weight or volume as printed), the product name and
   the listing's URL. Stay close to the current product — organic stays
   organic, a similar package size.
4. If you searched and there is no clear, current listing for that store,
   call mark_price_checked and move on. Never estimate, average, add tax or
   borrow another store's price.
5. If searching itself fails (errors, not empty results), stop and say so —
   don't mark items checked that you could not look up.
6. Work through the list in order, a few tool calls per item at most.

Your final message is one short line: how many prices changed and how many
had no listing.
"""

_DENIED = ["Bash", "Edit", "Write", "Read", "NotebookEdit", "WebSearch", "Task",
           "Skill", "SlashCommand"]

_TOOLS = [
    f"{PREFIX}plan_context", f"{PREFIX}get_preferences",
    f"{PREFIX}search_catalog", f"{PREFIX}get_ingredient", f"{PREFIX}add_ingredient",
    f"{PREFIX}check_recipe", f"{PREFIX}propose_recipe",
    f"{PREFIX}list_recipes", f"{PREFIX}get_recipe",
]

# A price check gets the price tools and the web, and none of the recipe tools.
_PRICE_TOOLS = [
    f"{PREFIX}prices_to_check", f"{PREFIX}record_price", f"{PREFIX}mark_price_checked",
    f"{PREFIX}get_ingredient",
]

_EXIT_GRACE = 30
MAX_TURNS = 60


class ChefError(RuntimeError):
    """The chef backend is unavailable or misconfigured."""


def claude_cli_path() -> str:
    path = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    if not os.path.exists(path):
        raise ChefError("The `claude` CLI was not found. Install Claude Code and sign in "
                        "(`claude` once in a terminal), then try again.")
    return path


def sous_chef_command() -> list[str]:
    """How to launch this project's MCP server from another process.

    `sous-chef` may be a shell alias rather than a PATH entry, so which() finds
    nothing and a bare name fails to exec — silently, from the client's side.
    The console script beside the running interpreter is the venv this code is
    already executing in, so it is tried first.
    """
    sibling = Path(sys.executable).parent / "sous-chef"
    if sibling.exists():
        return [str(sibling), "mcp"]
    found = shutil.which("sous-chef")
    if found:
        return [found, "mcp"]
    return [sys.executable, "-m", "sous_chef.mcp.server"]


def _mcp_config() -> str:
    command, *args = sous_chef_command()
    env = {k: os.environ[k] for k in ("SOUS_CHEF_DIR", "SOUS_CHEF_DB_PATH") if k in os.environ}
    cfg = {"mcpServers": {"sous-chef": {"command": command, "args": args, "env": env}}}
    fd, path = tempfile.mkstemp(prefix="sous-chef-mcp-", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(cfg, fh)
    return path


def build_prompt(task: str, *, plan_id: int | None, count: int = 3, text: str = "") -> str:
    text = (text or "").strip()
    if task == "suggest":
        if not plan_id:
            raise ChefError("Suggestions need a plan.")
        extra = f"\nThe user adds: «{text}»" if text else ""
        return (f"Plan {plan_id}: propose {count} new dinner recipes for this week. "
                f"Call plan_context(plan_id={plan_id}) first. Spread them across the user's "
                f"cuisine weights, and do not repeat anything in already_offered or "
                f"in_library.{extra}\nSave each with propose_recipe(plan_id={plan_id}, "
                f"origin='claude').")
    if task == "craft":
        if not text:
            raise ChefError("Describe the recipe you'd like.")
        where = (f"Call plan_context(plan_id={plan_id}) first. " if plan_id
                 else "Call get_preferences first. ")
        save = f"propose_recipe(plan_id={plan_id}, origin='craft')" if plan_id \
            else "propose_recipe(origin='craft')"
        return (f"The user wants a recipe for: «{text}». {where}Write the one recipe that "
                f"best matches the request within their stores and targets. If the request "
                f"conflicts with a target, honour the request and say so in one line. "
                f"Save it with {save}.")
    if task == "import":
        if not text:
            raise ChefError("Paste a recipe or a link to one.")
        save = f"propose_recipe(plan_id={plan_id}, origin='import')" if plan_id \
            else "propose_recipe(origin='import')"
        source = ("Fetch this page and convert the recipe on it" if _is_url(text)
                  else "Convert this recipe")
        return (f"{source} for the user's library. Keep the author's dish, proportions, "
                f"servings and method; map each ingredient to the closest catalog "
                f"ingredient (search_catalog{f' with plan_id={plan_id}' if plan_id else ''}), "
                f"and add_ingredient only where nothing reasonable matches. Rewrite steps "
                f"only for clarity. Save it with {save}.\n\n<recipe>\n{text[:20000]}\n</recipe>")
    if task == "prices":
        return (f"Check current prices: call prices_to_check(limit={count}) and look up "
                f"each item it returns.")
    raise ChefError(f"Unknown task '{task}'.")


def _is_url(text: str) -> bool:
    return text.startswith(("http://", "https://")) and " " not in text and "\n" not in text


def max_turns(task: str, count: int) -> int:
    # A price check takes a search, a page and a record per item.
    return min(20 + 5 * count, 220) if task == "prices" else MAX_TURNS


def build_command(task: str, prompt: str, model: str, count: int = 3) -> tuple[list[str], str]:
    cfg_path = _mcp_config()
    if task == "prices":
        allowed = [*_PRICE_TOOLS, "WebSearch", "WebFetch"]
        denied = [t for t in _DENIED if t != "WebSearch"]
    else:
        allowed = list(_TOOLS)
        denied = list(_DENIED)
        if task == "import" and "<recipe>\nhttp" in prompt:
            allowed.append("WebFetch")        # only an import from a link may read the web
        else:
            denied.append("WebFetch")
    cmd = [
        claude_cli_path(), "-p",
        "--model", model,
        "--append-system-prompt", PRICE_PROMPT if task == "prices" else SYSTEM_PROMPT,
        "--mcp-config", cfg_path,
        "--strict-mcp-config",
        "--allowedTools", ",".join(allowed),
        "--disallowedTools", ",".join(denied),
        "--max-turns", str(max_turns(task, count)),
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        prompt,
    ]
    return cmd, cfg_path


@dataclass
class _Run:
    tool_names: dict[str, str] = field(default_factory=dict)   # tool_use id → name
    text: list[str] = field(default_factory=list)
    saved: list[dict] = field(default_factory=list)
    prices: list[dict] = field(default_factory=list)       # recorded by record_price
    unlisted: list[dict] = field(default_factory=list)     # mark_price_checked
    fallback: str = ""


def run(task: str, *, plan_id: int | None = None, count: int = 3, text: str = "",
        model: str | None = None) -> Iterator[dict]:
    """Run one chef job, yielding events for the browser.

    Event types: `status` (what it is doing), `recipe` (one saved: id, title),
    `price` (one updated: name, store, old and new), `unlisted` (no listing
    found), `retry` (a save was rejected and is being fixed), `text` (its
    closing words), `error`, and finally `done` with the ids saved and the
    number of prices updated and unlisted.
    """
    prompt = build_prompt(task, plan_id=plan_id, count=count, text=text)
    if task == "prices":
        from sous_chef import tools
        if not tools.prices_to_check(limit=1):
            yield {"type": "text", "text": "Every estimate was checked in the last day — "
                                           "nothing to look up yet."}
            yield {"type": "done", "saved": [], "updated": 0, "unlisted": 0}
            return
    cmd, cfg_path = build_command(task, prompt, model or DEFAULT_MODEL, count)
    yield {"type": "status",
           "text": "Starting the price check…" if task == "prices" else "Starting the chef…"}

    # Run outside the project so this repo's CLAUDE.md is not loaded: the chef
    # should work from tool output, not developer notes.
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1,
                            cwd=tempfile.gettempdir())
    assert proc.stdout is not None
    state = _Run()
    try:
        for line in proc.stdout:
            for event in ingest(line, state):
                yield event
        _reap(proc)
        closing = "".join(state.text).strip() or state.fallback
        if not (state.saved or state.prices or state.unlisted):
            err = (proc.stderr.read() if proc.stderr else "").strip()
            what = "checking a price" if task == "prices" else "saving a recipe"
            yield {"type": "error",
                   "text": closing or err[:500] or
                   f"The chef finished without {what}. Check `claude -p hello` works."}
        elif closing:
            yield {"type": "text", "text": closing}
        yield {"type": "done", "saved": [r["id"] for r in state.saved],
               "updated": len(state.prices), "unlisted": len(state.unlisted)}
    finally:
        _finish(proc, cfg_path)


def ingest(line: str, state: _Run) -> list[dict]:
    """Turn one line of CLI stream-json into browser events."""
    line = line.strip()
    if not line:
        return []
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    out: list[dict] = []
    kind = event.get("type")

    if kind == "stream_event":
        ev = event.get("event", {})
        if ev.get("type") == "content_block_delta":
            d = ev.get("delta", {})
            if d.get("type") == "text_delta" and d.get("text"):
                state.text.append(d["text"])
        elif ev.get("type") == "message_start":
            state.text.clear()        # only the last message's words are the closing line
        return out

    if kind == "assistant":
        blocks = event.get("message", {}).get("content", []) or []
        texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        if texts:
            state.fallback = "".join(texts).strip()
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                name = (b.get("name") or "").replace(PREFIX, "")
                state.tool_names[b.get("id", "")] = name
                label = describe_tool(name, b.get("input") or {})
                if label:
                    out.append({"type": "status", "text": label, "tool": name})
        return out

    if kind == "user":
        for b in event.get("message", {}).get("content", []) or []:
            if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                continue
            name = state.tool_names.get(b.get("tool_use_id", ""), "")
            if name not in ("propose_recipe", "record_price", "mark_price_checked"):
                continue
            body = _result_text(b.get("content"))
            if b.get("is_error"):
                first = next((ln for ln in body.splitlines() if ln.strip().startswith("-")), body)
                out.append({"type": "retry", "text": first.strip(" -")[:240]})
                continue
            try:
                result = json.loads(body)
            except json.JSONDecodeError:
                continue
            if not isinstance(result, dict):
                continue
            if name == "propose_recipe" and "id" in result:
                state.saved.append(result)
                out.append({"type": "recipe", "id": result["id"], "title": result.get("title", "")})
            elif name == "record_price" and "new" in result:
                state.prices.append(result)
                out.append({"type": "price", **result})
            elif name == "mark_price_checked":
                state.unlisted.append(result)
                out.append({"type": "unlisted", **result})
        return out
    return out


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def describe_tool(name: str, args: dict) -> str | None:
    if name == "plan_context":
        return "Reading your week, targets and stores"
    if name == "get_preferences":
        return "Reading your preferences"
    if name == "search_catalog":
        q = args.get("query") or "the catalog"
        return f"Checking the stores for {q}"
    if name == "add_ingredient":
        return f"Adding {args.get('name', 'an ingredient')} to the catalog (estimated price)"
    if name == "check_recipe":
        title = (args.get("recipe") or {}).get("title", "a recipe")
        return f"Checking the numbers on {title}"
    if name == "propose_recipe":
        title = (args.get("recipe") or {}).get("title", "a recipe")
        return f"Writing up {title}"
    if name in ("list_recipes", "get_recipe", "get_ingredient"):
        return "Looking through your library"
    if name == "prices_to_check":
        return "Picking which prices to check"
    if name == "WebSearch":
        return f"Searching: {args.get('query', '')}"[:120]
    if name == "WebFetch":
        from urllib.parse import urlparse
        host = urlparse(args.get("url", "")).hostname or "the page"
        return f"Reading {host.removeprefix('www.')}"
    return None


def _reap(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=_EXIT_GRACE)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _finish(proc: subprocess.Popen, cfg_path: str) -> None:
    """Let the CLI finish even if the browser went away.

    A locked phone drops the event stream and closes this generator, but the
    chef keeps going and every recipe it saves still lands in the plan, where
    the next load shows it. So the output is drained, not abandoned.
    """
    try:
        if proc.poll() is None and proc.stdout is not None:
            for _ in proc.stdout:
                pass
    except (ValueError, OSError):
        pass
    finally:
        _reap(proc)
        try:
            os.unlink(cfg_path)
        except OSError:
            pass


# ── one run per plan ─────────────────────────────────────────

# How long a finished run stays available to a page reconnecting after a
# dropped stream, so it can still show what was saved while it was away.
RECENT_SECONDS = 600
_KEEPALIVE = 15.0


@dataclass
class Job:
    """One chef run, shared by every request following it."""

    key: str
    task: str
    events: list[dict] = field(default_factory=list)
    done: bool = False
    finished_at: float | None = None
    cond: threading.Condition = field(default_factory=threading.Condition)

    def add(self, event: dict) -> None:
        with self.cond:
            self.events.append(event)
            self.cond.notify_all()

    def finish(self) -> None:
        with self.cond:
            self.done = True
            self.finished_at = time.time()
            self.cond.notify_all()

    def follow(self, keepalive: float = _KEEPALIVE) -> Iterator[dict | None]:
        """Every event from the start, then new ones as they come, until done.

        Yields None when nothing has happened for `keepalive` seconds, so the
        caller can send something that keeps an idle phone connection open.
        """
        i = 0
        while True:
            with self.cond:
                if i >= len(self.events) and not self.done:
                    self.cond.wait(timeout=keepalive)
                new, done = self.events[i:], self.done
            if not new and not done:
                yield None
            for event in new:
                yield event
            i += len(new)
            if done and i >= len(self.events):
                return


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _key(plan_id: int | None, task: str | None = None) -> str:
    if task == "prices":
        return "prices"           # one price check at a time, whatever the plan
    return f"plan-{plan_id}" if plan_id else "library"


def start_or_attach(task: str, *, plan_id: int | None = None, count: int = 3,
                    text: str = "", model: str | None = None,
                    scheduled: bool = False) -> tuple[Job, bool]:
    """The plan's running job if there is one (attached=True), else a new one.

    A bad request (no text to craft from, say) raises ChefError here, before
    anything is started.
    """
    key = _key(plan_id, task)
    with _jobs_lock:
        job = _jobs.get(key)
        if job and not job.done:
            return job, True
        build_prompt(task, plan_id=plan_id, count=count, text=text)
        job = Job(key=key, task=task)
        _jobs[key] = job
    threading.Thread(target=_drive, args=(job, task, plan_id, count, text, model, scheduled),
                     daemon=True, name=f"chef-{key}").start()
    return job, False


def _drive(job: Job, task: str, plan_id: int | None, count: int, text: str,
           model: str | None, scheduled: bool = False) -> None:
    if task == "prices":
        from sous_chef import tools
        tools.note_price_run(started=True, scheduled=scheduled, limit=count)
    try:
        for event in run(task, plan_id=plan_id, count=count, text=text, model=model):
            job.add(event)
    except ChefError as e:
        job.add({"type": "error", "text": str(e)})
        job.add({"type": "done", "saved": []})
    except Exception as e:  # surfaced to whoever is following, not swallowed
        job.add({"type": "error", "text": f"{type(e).__name__}: {e}"})
        job.add({"type": "done", "saved": []})
    finally:
        if task == "prices":
            _note_prices(job)
        job.finish()


def _note_prices(job: Job) -> None:
    """What the price check found, for the Pantry tab and the next schedule."""
    from sous_chef import tools
    try:
        changes = [{k: e.get(k) for k in ("name", "store_name", "old", "new", "change_pct")}
                   for e in job.events if e["type"] == "price"]
        errors = [e["text"] for e in job.events if e["type"] == "error"]
        tools.note_price_run(updated=len(changes), changes=changes[:60],
                             unlisted=sum(e["type"] == "unlisted" for e in job.events),
                             error=errors[-1][:300] if errors else None)
    except Exception:  # bookkeeping must not stop the job from finishing
        pass


def current_job(plan_id: int | None, *, recent: bool = False, task: str | None = None) -> Job | None:
    """The plan's running job, or with `recent` one that finished lately."""
    job = _jobs.get(_key(plan_id, task))
    if not job:
        return None
    if not job.done:
        return job
    if recent and job.finished_at and time.time() - job.finished_at < RECENT_SECONDS:
        return job
    return None
