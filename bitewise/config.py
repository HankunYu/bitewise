import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("BITEWISE_DATA", BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "bitewise.db"
STATIC_DIR = Path(__file__).resolve().parent / "static"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8600

# Per-meal nutrient columns the AI estimates; kcal is tracked separately.
NUTRIENT_KEYS = ("protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg")

# Settings stored in the DB; these are the defaults on first run.
DEFAULT_SETTINGS = {
    "daily_kcal_target": "2000",
    "goal_prompt": "",
    "advice_time": "23:00",
    "model": "claude-opus-5",
    "meal_slots": '{"breakfast": ["05:00", "10:30"], "lunch": ["10:30", "15:00"], "dinner": ["17:00", "22:00"]}',
    # Daily nutrient targets: {nutrient: number}. protein/fiber are floors, the rest are caps.
    "nutrient_targets": "{}",
    "nutrient_targets_job": '{"status": "idle", "error": null}',
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
