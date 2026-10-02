"""The recipe chef: the `claude` CLI with the sous-chef MCP server attached.

Routing through the CLI rather than the API means your existing Claude
subscription pays for it — no API key — and, as in Trainer, the model gets the
real tools: it searches the actual catalog, sees which ingredients your stores
carry, and saves recipes through a validated interface that computes their
cost and nutrition. It never hands back prose for this code to parse.

Three jobs, each one CLI run:

  suggest — propose N new recipes for the week
  craft   — write the one recipe you described ("something with miso and salmon")
  import  — convert a recipe you pasted (text, or a link) into the catalog

Progress is streamed to the browser as each tool call happens, and every
recipe appears the moment propose_recipe saves it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
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
   with honest nutrition and a realistic price for the plan's store.
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
7. Save each finished recipe with propose_recipe. If it reports a problem, fix
   exactly that and call it again.

Your final message is one or two short lines: the recipes show up as cards on
their own, with their numbers.
"""

_DENIED = ["Bash", "Edit", "Write", "Read", "NotebookEdit", "WebSearch", "Task",
           "Skill", "SlashCommand"]

_TOOLS = [
    f"{PREFIX}plan_context", f"{PREFIX}get_preferences",
    f"{PREFIX}search_catalog", f"{PREFIX}get_ingredient", f"{PREFIX}add_ingredient",
    f"{PREFIX}check_recipe", f"{PREFIX}propose_recipe",
    f"{PREFIX}list_recipes", f"{PREFIX}get_recipe",
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
    raise ChefError(f"Unknown task '{task}'.")


def _is_url(text: str) -> bool:
    return text.startswith(("http://", "https://")) and " " not in text and "\n" not in text


def build_command(task: str, prompt: str, model: str) -> tuple[list[str], str]:
    cfg_path = _mcp_config()
    allowed = list(_TOOLS)
    denied = list(_DENIED)
    if task == "import" and "<recipe>\nhttp" in prompt:
        allowed.append("WebFetch")        # only an import from a link may read the web
    else:
        denied.append("WebFetch")
    cmd = [
        claude_cli_path(), "-p",
        "--model", model,
        "--append-system-prompt", SYSTEM_PROMPT,
        "--mcp-config", cfg_path,
        "--strict-mcp-config",
        "--allowedTools", ",".join(allowed),
        "--disallowedTools", ",".join(denied),
        "--max-turns", str(MAX_TURNS),
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
    fallback: str = ""


def run(task: str, *, plan_id: int | None = None, count: int = 3, text: str = "",
        model: str | None = None) -> Iterator[dict]:
    """Run one chef job, yielding events for the browser.

    Event types: `status` (what it is doing), `recipe` (one saved: id, title),
    `retry` (a save was rejected and is being fixed), `text` (its closing words),
    `error`, and finally `done` with the ids saved.
    """
    prompt = build_prompt(task, plan_id=plan_id, count=count, text=text)
    cmd, cfg_path = build_command(task, prompt, model or DEFAULT_MODEL)
    yield {"type": "status", "text": "Starting the chef…"}

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
        if not state.saved:
            err = (proc.stderr.read() if proc.stderr else "").strip()
            closing = "".join(state.text).strip() or state.fallback
            yield {"type": "error",
                   "text": closing or err[:500] or
                   "The chef finished without saving a recipe. Check `claude -p hello` works."}
        else:
            closing = "".join(state.text).strip() or state.fallback
            if closing:
                yield {"type": "text", "text": closing}
        yield {"type": "done", "saved": [r["id"] for r in state.saved]}
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
            if name != "propose_recipe":
                continue
            body = _result_text(b.get("content"))
            if b.get("is_error"):
                first = next((ln for ln in body.splitlines() if ln.strip().startswith("-")), body)
                out.append({"type": "retry", "text": first.strip(" -")[:240]})
                continue
            try:
                saved = json.loads(body)
            except json.JSONDecodeError:
                continue
            if isinstance(saved, dict) and "id" in saved:
                state.saved.append(saved)
                out.append({"type": "recipe", "id": saved["id"], "title": saved.get("title", "")})
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
    if name == "WebFetch":
        return "Reading the recipe page"
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
