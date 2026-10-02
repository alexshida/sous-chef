"""MCP server exposing the sous-chef tool layer to the recipe chef.

Every tool here is a thin wrapper over `sous_chef.tools`, which is also what
the web app calls — so Claude and the web UI always see the same numbers.
Nothing is reimplemented at this layer.

The web app starts this through the `claude` CLI when it asks for recipes. It
can also be attached to any Claude client:

    {"mcpServers": {"sous-chef": {"command": "sous-chef", "args": ["mcp"]}}}
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as MCPToolError
from pydantic import BaseModel, Field

from sous_chef import tools
from sous_chef.storage.db import init_db

INSTRUCTIONS = """\
You are the recipe chef for one person's weekly meal plan, with their grocery
stores' catalog, their pantry and their week through these tools.

You write recipes. The tools do the arithmetic: cost per serving, protein,
fiber and calories are computed from the catalog when you save or check a
recipe, so never state numbers you worked out yourself — read them from the
tool result.

Build recipes only from catalog ingredients available at the plan's stores
(search_catalog with the plan_id says which). Use each ingredient's listed
units. Add an ingredient only when something essential is genuinely missing.
"""

mcp = MCPServer(name="sous-chef", version="0.1.0", instructions=INSTRUCTIONS)


def _wrap(fn):
    """Re-raise ToolError as the MCP SDK's own ToolError, so the model reads the reason.

    Anything else raised from a tool counts as a crash: the SDK replaces it
    with a bare "Error executing tool <name>" and the model never learns that
    'salmon-fillet' should have been 'salmon'. Returning {"error": ...} is no
    better — it fails structured-output validation on tools that return lists.
    """
    def call(*args, **kwargs) -> Any:
        try:
            return fn(*args, **kwargs)
        except tools.ToolError as e:
            raise MCPToolError(str(e)) from None
    call.__name__ = fn.__name__
    call.__doc__ = fn.__doc__
    return call


class IngredientLine(BaseModel):
    id: str = Field(description="Catalog ingredient id, from search_catalog")
    qty: float = Field(description="Amount for the recipe's servings; 0 means 'to taste'")
    unit: str = Field(description="One of the units search_catalog lists for this ingredient")
    prep: str = Field("", description="e.g. 'diced', 'drained and rinsed'")
    optional: bool = Field(False, description="Garnish or serving extra, left out of totals")


class Recipe(BaseModel):
    title: str
    cuisine: Literal["east_asian", "indian", "pasta", "mediterranean", "other"]
    summary: str = Field(description="One appetising sentence")
    servings: int = Field(description="Servings this ingredient list makes (usually 4)")
    active_min: int = Field(description="Hands-on minutes")
    total_min: int = Field(description="Start to table, including rice or oven time")
    ingredients: list[IngredientLine]
    steps: list[str] = Field(description="One clear action per step, with heat, time and doneness cues")
    tags: list[str] = Field(default_factory=list,
                            description="e.g. vegetarian, sheet-pan, one-pot, freezer-friendly, quick")


# ── context ──────────────────────────────────────────────────

@mcp.tool()
def plan_context(plan_id: int) -> dict:
    """Read this first. The week you are cooking for: stores, household, per-serving
    protein/fiber/calorie targets, cuisine weights, things to avoid, equipment,
    the weeknight time limit, which nights are free, what is already chosen,
    perishables to use up, and titles already offered (do not repeat them)."""
    return _wrap(tools.plan_context)(plan_id)


@mcp.tool()
def get_preferences() -> dict:
    """The user's standing preferences: household, stores, cuisines, targets, avoid list."""
    return _wrap(tools.get_preferences)()


# ── catalog ──────────────────────────────────────────────────

@mcp.tool()
def search_catalog(query: str, plan_id: int | None = None, aisle: str | None = None,
                   limit: int = 25) -> list[dict]:
    """Find ingredients. Words match in any order ("chicken", "beans", "greens").

    Each result has the id to use in recipes, the units it can be measured in,
    nutrition per 100 g, price per 100 g, and — with plan_id — whether it is
    available at that plan's stores. Use only available ingredients.
    """
    return _wrap(tools.search_catalog)(query, aisle, limit, None, False, plan_id)


@mcp.tool()
def get_ingredient(ingredient_id: str) -> dict:
    """One ingredient's units, nutrition and store offers."""
    return _wrap(tools.get_ingredient)(ingredient_id)


@mcp.tool()
def add_ingredient(name: str, aisle: str, kcal: float, protein: float, carbs: float,
                   fat: float, fiber: float, offers: list[dict], perishable: bool = False,
                   g_per_cup: float | None = None, unit_g: dict[str, float] | None = None) -> dict:
    """Add an ingredient the catalog genuinely lacks. Search first.

    Nutrition is per 100 g. offers: [{store, product, pkg_qty, pkg_unit, price}]
    with store one of tj, qfc, pcc and a realistic price estimate. g_per_cup lets
    it be measured by volume; unit_g gives grams per counted unit, e.g.
    {"each": 120} or {"bunch": 60}. aisle: produce, meat & seafood, dairy & eggs,
    refrigerated, frozen, bakery, grains & pasta, canned & jarred,
    oils & condiments, baking, spices, nuts & seeds.
    """
    return _wrap(tools.add_ingredient)(name, aisle, kcal, protein, carbs, fat, fiber, offers,
                                       perishable, g_per_cup, unit_g)


# ── recipes ──────────────────────────────────────────────────

@mcp.tool()
def check_recipe(recipe: Recipe, plan_id: int | None = None) -> dict:
    """Validate and cost a recipe without saving it: cost per serving, nutrition
    per serving against the targets, and anything not available at the stores."""
    return _wrap(tools.check_recipe)(recipe.model_dump(), None, plan_id)


@mcp.tool()
def propose_recipe(recipe: Recipe, plan_id: int | None = None,
                   origin: Literal["claude", "craft", "import"] = "claude") -> dict:
    """Save a finished recipe; with plan_id it appears as a suggestion for that week.

    origin: claude for your own suggestions, craft when made to the user's
    request, import when converting a recipe the user supplied. Returns the
    computed cost and nutrition per serving. A validation error names the fix —
    correct it and call again.
    """
    return _wrap(tools.propose_recipe)(recipe.model_dump(), plan_id, origin)


@mcp.tool()
def list_recipes(query: str = "", cuisine: str | None = None) -> list[dict]:
    """The user's recipe library, with cost and nutrition per serving."""
    return _wrap(tools.list_recipes)(query, cuisine)


@mcp.tool()
def get_recipe(recipe_id: int, servings: int | None = None, plan_id: int | None = None) -> dict:
    """A full recipe with scaled ingredient lines and steps."""
    return _wrap(tools.get_recipe)(recipe_id, servings, plan_id)


def main(transport: str = "stdio") -> None:
    init_db()
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
