# sous-chef

Plan the week's meals around your calendar, from the stores you actually shop at.

You tell it how many recipes you want this week. It looks at your calendar to see
which evenings are free, which are busy and which have dinner plans, then suggests
recipes. You see each one's cook time, servings, **cost per serving** and **protein
and fiber per serving**. Pick the ones you like, set how many servings you want (the
extras become lunches and lazy-night dinners), and it builds the grocery list for
the stores you've switched on and puts the cook times on your calendar.

- **Suggestions** come instantly from your recipe library, or from Claude when you
  want something new ("✨ New ideas"). You can also describe a dish ("something with
  miso and salmon") or paste in a recipe, or a link to one.
- **The numbers are computed, never guessed.** Claude writes the recipes. Code works
  out what they cost, what they deliver nutritionally, and what goes on the list.
- **One trip if possible.** Every item goes to the first store you've ticked that
  carries it. Choosing recipes that share fresh ingredients is rewarded: each
  suggestion shows what it would *add* to your grocery bill.
- **Leftovers are planned, not accidental.** Servings are walked through the week,
  covering dinner the night you cook and then lunches and non-cooking nights. Food
  that won't be eaten within four days is flagged to freeze.

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

## Prices

Trader Joe's and PCC publish no price data. The catalog starts from estimates: Trader
Joe's priced product by product, QFC and PCC scaled from those by aisle. Every
estimate is marked **est.** in the app. Correct the ones you buy on the Pantry tab and
they become **yours**, and reseeding never overwrites them. If a store doesn't stock
something, untick *carried* for that store. To update in bulk:

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
pytest                          # ~130 tests, ~20 s
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
- A weekend prep-ahead block (cook grains, marinate, chop) on the calendar.
- Ratings, and favourites brought back into rotation.
