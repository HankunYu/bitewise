import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# Server-wide settings (provider, API keys, BITEWISE_HOST, BITEWISE_PORT).
# Variables already set in the environment win over the file.
load_dotenv(BASE_DIR / ".env")
# Root of all user data; each user gets data/<name>/ (SQLite + uploaded photos).
DATA_DIR = Path(os.environ.get("BITEWISE_DATA", BASE_DIR / "data"))
# User name -> access token; see users.py.
USERS_FILE = Path(os.environ.get("BITEWISE_USERS", BASE_DIR / "users.toml"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Model provider for all users: anthropic | openai | google (see llm/).
PROVIDER = os.environ.get("BITEWISE_PROVIDER", "anthropic").strip().lower()

# Loopback by default: public access goes through a TLS reverse proxy.
DEFAULT_HOST = os.environ.get("BITEWISE_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("BITEWISE_PORT", "8600"))

# Per-meal nutrient columns the AI estimates; kcal is tracked separately.
NUTRIENT_KEYS = ("protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg")

# Settings stored in the DB; these are the defaults on first run.
DEFAULT_SETTINGS = {
    "daily_kcal_target": "2000",
    "goal_prompt": "",
    "advice_time": "23:00",
    # IANA name; "now", "today", meal slots and advice_time all follow it.
    "timezone": "Europe/London",
    # Empty = the provider's default model.
    "model": "",
    "meal_slots": '{"breakfast": ["05:00", "10:30"], "lunch": ["10:30", "15:00"], "dinner": ["17:00", "22:00"]}',
    # Daily nutrient targets: {nutrient: number}. protein/fiber are floors, the rest are caps.
    "nutrient_targets": "{}",
    "nutrient_targets_job": '{"status": "idle", "error": null}',
}
