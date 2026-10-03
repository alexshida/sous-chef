# sous-chef

Plan the week's meals around your calendar, from the stores you actually shop at.

You tell it how many recipes you want this week. It looks at your calendar to see
which evenings are free, which are busy and which have dinner plans, then suggests
recipes. You see each one's cook time, servings, **cost per serving** and **protein
and fiber per serving**. Pick the ones you like, set how many servings you want (the
extras become lunches and lazy-night dinners), and it builds the grocery list for
the stores you've switched on and puts the cook times on your calendar.

- **Suggestions** come in a **↻ Fresh batch**: a few from your recipe library at once,
  plus new ones from Claude streaming in (3 + 3 by default, set in Settings). You can
  also describe a dish ("something with miso and salmon"), or paste in a recipe or a
  link to one. Every card says where it came from (📚 your library, ✨ fresh from
  Claude). Tap 📚 Save to keep a Claude recipe, so future batches can reuse it for
  free. If it looks like one you already have (same main ingredients under another
  name), you choose: save as new, replace the old one, or don't save.
- **The numbers are computed, never guessed.** Claude writes the recipes. Code works
  out what they cost, what they deliver nutritionally, and what goes on the list.
- **One trip if possible.** Every item goes to the first store you've ticked that
  carries it. Choosing recipes that share fresh ingredients is rewarded: each
  suggestion shows what it would *add* to your grocery bill.
- **Leftovers are planned, not accidental.** Servings are walked through the week,
  covering dinner the night you cook and then lunches and non-cooking nights. Food
  that won't be eaten within four days is flagged to freeze.
- **Meal-prep mode** cooks the week in one batch session with Costco's bulk packs.
  See [below](#meal-prep-mode-and-costco).

It's a sibling of [trainer](https://github.com/alexshida/trainer) and is built the same
way: one Python package, SQLite on your Mac, a web app you add to your iPhone home
screen over Tailscale, and Claude through the `claude` CLI. That means your Claude
subscription pays for suggestions, with no API key and nothing to host.

---

## Install

```bash
git clone https://github.com/alexshida/sous-chef.git ~/sous-chef && cd ~/sous-chef
./install.sh
```

Then open **http://localhost:8766**. The installer finds Python 3.10+, sets everything
up inside the folder, checks the result, and on a Mac offers to keep sous-chef
running in the background. **New to Terminal?** [docs/INSTALL.md](docs/INSTALL.md)
walks through every step, plus the optional parts: Claude for ✨ ideas, your iPhone,
and your calendar.

## Everyday commands

```bash
source .venv/bin/activate  # inside the folder: puts `sous-chef` on your PATH, prompt shows (sous-chef)
sous-chef web              # run it in this terminal: http://localhost:8766 (trainer uses 8765)
sous-chef doctor           # check the setup and say what to fix
sous-chef install-service  # run in the background, start at login (macOS)
sous-chef restart          # restart that background service
sous-chef dedupe           # list recipes saved more than once; --apply merges them
git pull && ./install.sh   # update
```

## On your iPhone

Install Tailscale on the Mac and the phone. `sous-chef doctor` (or `sous-chef web`) prints a
`100.x.y.z:8766` address for the phone. Open it in **Safari**, then tap Share → **Add to
Home Screen**. It opens full screen like an app. The shopping list keeps a copy on the
phone, so it still opens in a store with no signal, and ticks sync when you reconnect.

## Calendar (optional)

**Reading your schedule:** Settings → Calendar takes private feed links, one per line:

- **Google Calendar:** calendar.google.com → Settings → *your calendar* → Integrate
  calendar → **Secret address in iCal format**.
- **iCloud:** Calendar app → right-click the calendar → Share → **Public Calendar** →
  copy the `webcal://` link.

Events overlapping dinner time mark that evening *busy* (eat at home, but leftovers
rather than cooking). Dinners, parties and drinks mark it *out*. All-day trips mark the
day *away*. Tap any day to override.

**Adding cook times:** the 📅 button on the Shop tab downloads an `.ics` file. On iPhone
it offers *Add All* to whichever calendar you choose. Re-importing updates the events
rather than duplicating them.

Full two-way calendar sync needs Apple's native calendar framework (EventKit). That's
the main reason for the planned native app (see Roadmap).

## Meal-prep mode and Costco

Flip **Meal-prep mode** on the Plan tab to cook the week in one session:

- **Suggestions** switch to batch recipes made to keep and reheat. The library has
  ten built from popular Costco meal preps: rotisserie-chicken burrito bowls and
  pesto pasta, sheet-pan chicken thighs, turkey chili, teriyaki chicken, Korean beef,
  coconut curry, Greek chicken bowls, pesto salmon and fried rice. ✨ ideas follow
  the same brief.
- **Servings** default to 8 per recipe. Change this in Settings → Meal prep.
- **Everything is cooked on the first free day**, back to back from 1 pm (also
  adjustable), longest recipe first. The calendar export makes those
  *Meal prep: …* blocks.
- **The freezer is part of the plan.** Servings that won't be eaten within four days
  are marked to freeze on prep day and still count toward the week, and meals rotate
  between batches instead of one dish for days.

While the mode is on without Costco, the app suggests **Shop Costco first**. That
puts Costco at the front of the week's stores; anything Costco doesn't sell comes
from your other stores. With Costco on, the batch recipes cost about $2–6 a serving,
about 30% less than buying the same food without it (estimated prices).

## Prices

The catalog starts from estimates: Trader Joe's priced product by product, QFC and PCC
scaled from those by aisle, Costco's bulk packs from 2025 warehouse prices. Each is
marked **est.** in the app until something better replaces it:

- **Online checks.** Every 30 days, Claude looks up current prices on the stores' own
  listings, starting with what your plans use. Each price it finds is marked **online**,
  with the date and a link to the listing. Tap **↻ Check now** on the Pantry tab to run
  one yourself. Settings → Prices sets how often, and how many prices per check.
- **Your corrections.** Tap an item on the Pantry tab to fix a price or package size.
  It becomes **yours**.

Nothing overwrites a price that's yours: not an online check, not an update. If a
store doesn't stock something, untick *carried* for that store, and that sticks too.
To update in bulk:

```bash
sous-chef prices export prices.csv    # edit in Numbers or Excel
sous-chef prices import prices.csv
```

Product availability is an estimate too, so the first trip is where the catalog learns.
When Claude needs something the catalog lacks, it adds it with an estimated price
marked **Claude est.**

## Development

```bash
pip install -e ".[dev]"
pytest                          # ~220 tests, ~30 s
python tools/mobile_check.py    # phone-width layout check (needs Playwright and a running server)
```

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is the map. [CLAUDE.md](CLAUDE.md) has the
reasoning behind decisions and the traps.

## Roadmap

- **Native iOS app (SwiftUI)** on the same API: EventKit for two-way iCloud and Google
  calendar sync, a nicer shopping list. Builds can run from Xcode on a free Apple
  account (they need re-signing every 7 days), or a $99/yr developer account for
  TestFlight.
- Calorie and macro tracking.
- Breakfast meal prep (egg bites, overnight oats). Plans cover lunches and dinners today.
- Ratings, and favourites brought back into rotation.
