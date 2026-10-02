# sous-chef — notes for whoever changes this next

`docs/ARCHITECTURE.md` is the map. This file is the reasoning and the traps.

## Conventions

- Python 3.10+, `src/` layout, one package. `pip install -e ".[dev]"`, then `pytest`.
- Same shape as trainer (github.com/alexshida/trainer). Where a problem was already
  solved there (tailnet binding, launchd, the CLI-subprocess chat), the code is
  shared or mirrored. Keep it that way.
- Default port 8766, so it can run beside trainer on 8765.
- Commits are small and self-contained. The owner reviews push by push.

## Decisions

**Claude through the `claude` CLI, not the API.** The user's subscription pays and
the model gets real tools. `web/chef.py` runs `claude -p` with `--strict-mcp-config`
(only our server, not whatever else the user has configured), our MCP tools allowed,
and shell, file and skill tools denied. It runs from the temp directory so this
file isn't loaded into the chef's context. WebFetch is allowed only for imports
from a link.

**Grams underneath everything.** A recipe line is (ingredient, qty, unit). Each
ingredient has `g_per_cup` (density) and `unit_g` (grams per clove, can, bunch …).
Canned beans are their *drained* weight, because that's what the nutrition describes.
A unit an ingredient can't convert is a validation error that names the units it can.

**Two costs, deliberately.** `costing.cost_recipe` prices the food a recipe *uses*
(a quarter bunch of cilantro is a quarter of its price). `grocery.build_list` prices
whole packages. The gap is mostly staples that last for weeks, shown as
`stock_up_value` so a $90 first shop doesn't look like a $90 week.

**Overlap is a price, not a score.** `grocery.marginal_cost` is what adding a
candidate would add to the bill given what's already chosen. Shared perishables
make it smaller, so the reward for overlap falls out of the arithmetic.

**Cook when the fridge runs out.** `schedule.assign_cook_days` walks the week with
what's placed, finds the first uncovered meal and cooks on the latest free night
that still covers it. Coverage beats the weekend preference for long recipes.
Fresh fish and greens get the earliest nights.

**Suggestions join the library only when chosen.** Claude's plain suggestions are
stored with `in_library = 0`, so asking for new ideas ten times doesn't bury the
recipes you cook. Crafted and imported recipes go straight in.

**Prices are estimates until they're yours.** Offer `source` is `seed`, `scaled`,
`claude` or `user`. Reseeding refreshes only `seed`/`scaled` rows that are still
carried. A user price and a "not carried here" both survive it.

## Traps

- **A chef run outlives the request that started it, so there is one per plan.**
  A locking phone drops the stream, but the CLI keeps going (it should: its
  recipes still land). The page used to treat the drop as the end and re-enable
  its buttons, so the next tap started a second run on the same week. Both read
  the same `already_offered` and saved near-identical recipes. Now `chef.Job`
  is driven by a background thread, any request for that plan follows it from
  the start, `/api/chef/stream` reconnects, and the page keeps retrying instead
  of giving up. `propose_recipe` also refuses a title already offered this week
  or in the library, and `sous-chef dedupe` merges copies saved before that.

- **MCP tool errors must raise `mcp.server.mcpserver.exceptions.ToolError`.** In mcp
  2.x anything else is treated as a crash, and the model sees only "Error executing
  tool <name>". It never learns that `salmon-fillet` should have been `salmon`. An
  `{"error": ...}` return is worse: it fails output validation on list-returning
  tools. `mcp/server.py::_wrap` does this. A test calls `propose_recipe` through the
  MCP server and checks the close-match hint arrives.
- **The model will invent availability.** Asked to use only what the user's stores
  carry, it once added "Fish sauce, Thai" with a made-up Trader Joe's price to dodge
  an unavailable "Fish sauce". `add_ingredient` refuses near-duplicates (one name's
  words contain the other's) unless `not_duplicate=true`. The prompt says to reuse
  existing ingredients and list offers only where it's confident.
- **A tag that cannot wrap widens the whole page.** `.pill` is `nowrap`, so one
  listing a dozen shared ingredients made a 375 px phone lay out 1087 px wide;
  the browser zoomed out to fit and the tab bar stopped taking taps. Tags that
  list things carry `.pill.list` (wraps) and go through `few()` ("+9 more").
  `tests/test_mobile_layout.py` serves deliberately awkward data to catch it.
- **`x or default` turns 0 into the default.** Servings use `default if x is None
  else x`, then validate.
- **`INSERT OR IGNORE` burns AUTOINCREMENT ids.** Seeding checks for existing
  `seed_key`s first.
- **Paths are read at import.** Tests set `SOUS_CHEF_DIR` in `conftest.py` before
  importing the package. The chef passes `SOUS_CHEF_DIR`/`SOUS_CHEF_DB_PATH` into the
  MCP server's environment, so both processes use the same database.
- **launchd's PATH** is `/usr/bin:/bin:/usr/sbin:/sbin`, which won't find `claude`.
  `install-service` writes the current PATH into the plist.
- **`TestClient(app)` needs `client=("127.0.0.1", ...)`.** The middleware refuses
  anything outside loopback and the tailnet, and TestClient's default host is the
  string `"testclient"`.
- **Calendar feeds are fetched on every week view.** Results are cached for 10
  minutes and failures for 2, so a dead feed doesn't cost a timeout per screen.
- **`.ics` times are floating local time**, so a 6:15 pm cook block stays 6:15 pm
  wherever the phone is. UIDs are `sous-chef-<plan>-<recipe>`, so re-importing
  updates the event.
