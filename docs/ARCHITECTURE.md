# How sous-chef is put together

Orientation for someone about to change the code. For the *reasoning* behind
decisions and the traps that aren't visible from the code, read `CLAUDE.md`.

---

## The shape of it

```
 catalog/seed.py, recipes.py    starting ingredients, prices, 16 recipes
          │
          ▼
       SQLite                   storage/db.py — ~/.sous-chef/sous-chef.db
          │                     ingredients, offers, pantry, recipes, plans
          ▼
     computation                units.py      amounts → grams
          │                     costing.py    cost & nutrition per serving
          │                     grocery.py    whole packages, stores, overlap
          │                     schedule.py   day status, cook nights, leftovers
          │                     calendars.py  iCal feeds in, .ics out
          ▼
       tools.py                 ← the single definition of what can be done
          │
          ├────────────────┐
          ▼                ▼
   mcp/server.py      web/app.py ── web/static/index.html (one page, no build)
          ▲                │
          └── web/chef.py ─┘   runs `claude -p` with the MCP server attached
```

## The one rule that matters

**`tools.py` is the single definition of what can be done.** The MCP server (what
Claude uses) and the FastAPI routes (what the page uses) are thin adapters over it.
If either grew its own logic, the numbers Claude reasons about and the numbers on
screen would drift apart.

A new capability goes into `tools.py` first:

1. Put the computation in the right module (`costing`, `grocery`, `schedule`, …)
2. Add a function to `tools.py` that returns plain dicts and raises `ToolError`
3. If Claude should call it: register it in `mcp/server.py` with a docstring
   written for the model, and add it to `_TOOLS` in `web/chef.py` (a test
   checks the two match)
4. If the page needs it: add a route in `web/app.py`

### Computed versus written

**Arithmetic is computed; recipes are written.** Unit conversion, cost, nutrition,
package rounding, marginal cost, day status and leftovers coverage are deterministic
code, handed to the model as facts. A guessed protein figure is wrong in a way
nothing downstream catches.

What to cook is the *model's* job. It writes a recipe, saves it with
`propose_recipe`, and gets back the computed numbers. Keep culinary judgement out
of Python, apart from ranking the library (see `suggest_from_library`).

## What lives where

| Path | Holds |
|---|---|
| `catalog/seed.py` | Ingredients (nutrition per 100 g, grams per cup and per counted unit), Trader Joe's offers, QFC/PCC scaling, default pantry |
| `catalog/recipes.py` | The starter library, written for 4 servings from catalog ingredients only |
| `storage/db.py` | All SQLite access; schema and (re)seeding in `init_db()` |
| `units.py` | Amounts → grams, and grams → words ("1½ cups", "2 cloves") |
| `costing.py` | One recipe at N servings: cost of what is used, nutrition per serving |
| `grocery.py` | All chosen recipes: summed, rounded to packages, routed to stores |
| `schedule.py` | The week: day status, cook-night placement (or one prep session in meal-prep mode), servings walked through meals |
| `calendars.py` | Private iCal feeds parsed (recurrences expanded); `.ics` written by hand |
| `tools.py` | The tool layer |
| `mcp/server.py` | The MCP server |
| `web/chef.py` | The recipe chef: prompts, the `claude` command line, stream-json → events |
| `web/app.py` | FastAPI routes, loopback/tailnet-only middleware |
| `web/network.py` | Which address to bind (shared with trainer) |
| `web/static/` | `index.html`, manifest, service worker, icons |
| `main.py` | CLI: `web`, `doctor`, `install-service`, `restart`, `mcp`, `dedupe`, `prices export/import` |
| `doctor.py` | The setup checks behind `sous-chef doctor` |
| `install.sh` | One-command install and update (repo root) |
| `tools/` | Developer scripts: icons, phone-layout check |

## The web UI

One file, `web/static/index.html`: markup, CSS and JS together, with no build step
or framework. Plain JS with a `$()` helper, a single state object `S`, one
`render*()` per section, and event delegation on `data-action` attributes.

- It must work at **375 px**. `python tools/mobile_check.py` opens every tab at
  375/390/430 in light and dark and reports overflow and console errors.
- Colours come from CSS custom properties defined once per theme. Never hard-code one.
- Everything interpolated into HTML goes through `esc()`.
- The chef streams server-sent events over a `fetch` POST (EventSource can't POST).
  A recipe appears the moment `propose_recipe` saves it, and the run keeps going if
  the phone locks.

## Tests

```bash
pytest
```

`tests/conftest.py` points `SOUS_CHEF_DIR` at a temporary directory **before**
anything imports the package. Paths are read at import time. Use the `fresh_db`
fixture for anything that touches the database. Tests assert invariants: cost per
serving doesn't change with scaling, `bill()` equals the list total, the chef's
allowed tools equal the server's tools.
