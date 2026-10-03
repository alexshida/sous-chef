"""Configuration — paths from the environment, preferences from ~/.sous-chef/config.json."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SOUS_CHEF_DIR = Path(os.environ.get("SOUS_CHEF_DIR", "~/.sous-chef")).expanduser()
CONFIG_FILE = SOUS_CHEF_DIR / "config.json"
DB_PATH = Path(os.environ.get("SOUS_CHEF_DB_PATH", str(SOUS_CHEF_DIR / "sous-chef.db"))).expanduser()
DEFAULT_MODEL = os.environ.get("SOUS_CHEF_MODEL", "sonnet")

STORES = {"tj": "Trader Joe's", "qfc": "QFC", "pcc": "PCC", "costco": "Costco"}

CUISINES = {
    "east_asian": "East Asian",
    "indian": "Indian",
    "pasta": "Pasta",
    "mediterranean": "Mediterranean",
    "other": "Other",
}

# Words in a calendar event that mean dinner is taken care of elsewhere.
EAT_OUT_WORDS = [
    "dinner", "party", "potluck", "restaurant", "reservation", "drinks",
    "happy hour", "birthday", "bbq", "barbecue", "wedding", "date night",
]
# Words in an all-day event that mean you are not home to eat at all.
AWAY_WORDS = ["trip", "travel", "vacation", "flight", "conference", "retreat", "out of town"]


@dataclass
class Preferences:
    household: int = 1                    # people eating each meal
    stores: list[str] = field(default_factory=lambda: ["tj"])  # priority order
    cuisines: dict[str, int] = field(default_factory=lambda: {
        "east_asian": 3, "indian": 3, "pasta": 2, "mediterranean": 2,
    })
    n_recipes: int = 3
    default_servings: int = 4
    # Per serving — one serving is one meal for one person.
    protein_g: int = 35
    fiber_g: int = 10
    kcal: int = 650
    avoid: list[str] = field(default_factory=list)       # allergies, dislikes
    equipment: list[str] = field(default_factory=lambda: ["stovetop", "oven", "rice cooker"])
    max_weeknight_min: int = 45
    dinner_time: str = "19:00"
    lunches: int = 5                      # weekday lunches to cover with leftovers
    leftover_days: int = 4                # how long cooked food keeps in the fridge
    calendar_feeds: list[str] = field(default_factory=list)
    timezone: str = "America/Los_Angeles"
    notes: str = ""                       # anything else the chef should know

    @classmethod
    def from_dict(cls, d: dict) -> "Preferences":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def load_prefs() -> Preferences:
    SOUS_CHEF_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        return Preferences.from_dict(json.loads(CONFIG_FILE.read_text()))
    return Preferences()


def save_prefs(prefs: Preferences) -> None:
    SOUS_CHEF_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(asdict(prefs), indent=2))
