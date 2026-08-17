import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time as dtime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eaten_at TEXT NOT NULL,
    meal_slot TEXT NOT NULL DEFAULT 'snack',
    description TEXT NOT NULL DEFAULT '',
    items TEXT NOT NULL DEFAULT '[]',
    kcal REAL,
    protein_g REAL,
    carbs_g REAL,
    fat_g REAL,
    comment TEXT NOT NULL DEFAULT '',
    source_text TEXT NOT NULL DEFAULT '',
    image_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meals_eaten_at ON meals(eaten_at);

CREATE TABLE IF NOT EXISTS weights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    measured_at TEXT NOT NULL,
    weight_kg REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weights_measured_at ON weights(measured_at);

CREATE TABLE IF NOT EXISTS daily_advice (
    date TEXT PRIMARY KEY,
    advice TEXT NOT NULL,
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MEAL_EDITABLE_FIELDS = {
    "eaten_at", "meal_slot", "description", "items",
    "kcal", "protein_g", "carbs_g", "fat_g", "comment",
}


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for key, value in config.DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )


# ---------- settings ----------

def get_setting(key: str) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return config.DEFAULT_SETTINGS.get(key, "")
    return row["value"]


def get_all_settings() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    settings = dict(config.DEFAULT_SETTINGS)
    settings.update({r["key"]: r["value"] for r in rows})
    return settings


def set_settings(values: dict) -> None:
    with get_conn() as conn:
        for key, value in values.items():
            if key in config.DEFAULT_SETTINGS:
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)),
                )


def slot_for(dt: datetime) -> str:
    """Classify a datetime into a meal slot based on configured boundaries."""
    try:
        slots = json.loads(get_setting("meal_slots"))
    except (ValueError, TypeError):
        slots = json.loads(config.DEFAULT_SETTINGS["meal_slots"])
    t = dt.time()
    for name, (start, end) in slots.items():
        start_t = dtime.fromisoformat(start)
        end_t = dtime.fromisoformat(end)
        if start_t <= t < end_t:
            return name
    return "snack"


# ---------- meals ----------

def _meal_to_dict(row: sqlite3.Row) -> dict:
    meal = dict(row)
    try:
        meal["items"] = json.loads(meal["items"])
    except (ValueError, TypeError):
        meal["items"] = []
    return meal


def insert_meal(eaten_at: str, source_text: str, image_path: str | None) -> int:
    dt = datetime.fromisoformat(eaten_at)
    now = now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO meals (eaten_at, meal_slot, source_text, image_path, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (eaten_at, slot_for(dt), source_text, image_path, now, now),
        )
        return cur.lastrowid


def get_meal(meal_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
    return _meal_to_dict(row) if row else None


def update_meal(meal_id: int, fields: dict) -> None:
    fields = dict(fields)
    if "items" in fields and not isinstance(fields["items"], str):
        fields["items"] = json.dumps(fields["items"], ensure_ascii=False)
    fields["updated_at"] = now_iso()
    columns = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE meals SET {columns} WHERE id = ?",
            (*fields.values(), meal_id),
        )


def delete_meal(meal_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM meals WHERE id = ?", (meal_id,))


def list_meals(start: str, end: str) -> list[dict]:
    """Meals with eaten_at in [start, end), ISO datetime bounds."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM meals WHERE eaten_at >= ? AND eaten_at < ? ORDER BY eaten_at",
            (start, end),
        ).fetchall()
    return [_meal_to_dict(r) for r in rows]


def recent_done_meals(limit: int = 8) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM meals WHERE status = 'done' ORDER BY eaten_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_meal_to_dict(r) for r in rows]


def unfinished_meal_ids() -> list[int]:
    """Meals left pending/processing (e.g. after a restart)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM meals WHERE status IN ('pending', 'processing') ORDER BY id"
        ).fetchall()
    return [r["id"] for r in rows]


def daily_totals(start_date: str, end_date: str) -> list[dict]:
    """Per-day sums of done meals for date(eaten_at) in [start_date, end_date]."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date(eaten_at) AS date, SUM(kcal) AS kcal, SUM(protein_g) AS protein_g, "
            "SUM(carbs_g) AS carbs_g, SUM(fat_g) AS fat_g "
            "FROM meals WHERE status = 'done' AND date(eaten_at) BETWEEN ? AND ? "
            "GROUP BY date(eaten_at) ORDER BY date(eaten_at)",
            (start_date, end_date),
        ).fetchall()
    return [dict(r) for r in rows]


def day_has_done_meals(date: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM meals WHERE status = 'done' AND date(eaten_at) = ?",
            (date,),
        ).fetchone()
    return row["n"] > 0


# ---------- weights ----------

def insert_weight(weight_kg: float, measured_at: str | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO weights (measured_at, weight_kg) VALUES (?, ?)",
            (measured_at or now_iso(), weight_kg),
        )
        return cur.lastrowid


def list_weights(start_date: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM weights WHERE date(measured_at) >= ? ORDER BY measured_at",
            (start_date,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_weight(weight_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM weights WHERE id = ?", (weight_id,))


# ---------- daily advice ----------

def get_advice(date: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM daily_advice WHERE date = ?", (date,)).fetchone()
    return dict(row) if row else None


def save_advice(date: str, advice: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_advice (date, advice, generated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET advice = excluded.advice, generated_at = excluded.generated_at",
            (date, advice, now_iso()),
        )
