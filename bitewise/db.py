import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time as dtime
from pathlib import Path

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
    fiber_g REAL,
    sugar_g REAL,
    sodium_mg REAL,
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
    advice TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    generated_at TEXT NOT NULL
);

-- "Can I eat this?" photo checks; not counted as intake until turned into a meal.
CREATE TABLE IF NOT EXISTS food_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    verdict TEXT,
    advice TEXT NOT NULL DEFAULT '',
    estimate TEXT,
    meal_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MEAL_EDITABLE_FIELDS = {
    "eaten_at", "meal_slot", "description", "items",
    "kcal", *config.NUTRIENT_KEYS, "comment",
}


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _migrate(conn: sqlite3.Connection) -> None:
    advice_cols = {r["name"] for r in conn.execute("PRAGMA table_info(daily_advice)")}
    if "status" not in advice_cols:
        # Pre-status rows only exist for successfully generated advice.
        conn.execute("ALTER TABLE daily_advice ADD COLUMN status TEXT NOT NULL DEFAULT 'done'")
    if "error" not in advice_cols:
        conn.execute("ALTER TABLE daily_advice ADD COLUMN error TEXT")
    meal_cols = {r["name"] for r in conn.execute("PRAGMA table_info(meals)")}
    for col in ("fiber_g", "sugar_g", "sodium_mg"):
        if col not in meal_cols:
            # NULL on old rows = "never estimated", distinct from an estimated 0.
            conn.execute(f"ALTER TABLE meals ADD COLUMN {col} REAL")


def _meal_to_dict(row: sqlite3.Row) -> dict:
    meal = dict(row)
    try:
        meal["items"] = json.loads(meal["items"])
    except (ValueError, TypeError):
        meal["items"] = []
    return meal


def _check_to_dict(row: sqlite3.Row) -> dict:
    check = dict(row)
    try:
        check["estimate"] = json.loads(check["estimate"]) if check["estimate"] else None
    except (ValueError, TypeError):
        check["estimate"] = None
    return check


class Store:
    """One user's data: a SQLite database plus uploaded photos under data_dir."""

    def __init__(self, user: str, data_dir: Path):
        self.user = user
        self.data_dir = data_dir
        self.db_path = data_dir / "bitewise.db"
        self.upload_dir = data_dir / "uploads"

    def __repr__(self) -> str:
        return f"Store({self.user!r})"

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        """Create the data dirs and schema; safe to call on every startup."""
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            _migrate(conn)
            for key, value in config.DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                )

    # ---------- settings ----------

    def get_setting(self, key: str) -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return config.DEFAULT_SETTINGS.get(key, "")
        return row["value"]

    def get_all_settings(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        settings = dict(config.DEFAULT_SETTINGS)
        settings.update({r["key"]: r["value"] for r in rows})
        return settings

    def set_settings(self, values: dict) -> None:
        with self._conn() as conn:
            for key, value in values.items():
                if key in config.DEFAULT_SETTINGS:
                    conn.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, str(value)),
                    )

    def get_nutrient_targets(self) -> dict:
        try:
            return json.loads(self.get_setting("nutrient_targets"))
        except (ValueError, TypeError):
            return {}

    def get_targets_job(self) -> dict:
        try:
            return json.loads(self.get_setting("nutrient_targets_job"))
        except (ValueError, TypeError):
            return {"status": "idle", "error": None}

    def set_targets_job(self, status: str, error: str | None = None) -> None:
        job = json.dumps({"status": status, "error": error}, ensure_ascii=False)
        self.set_settings({"nutrient_targets_job": job})

    def slot_for(self, dt: datetime) -> str:
        """Classify a datetime into a meal slot based on configured boundaries."""
        try:
            slots = json.loads(self.get_setting("meal_slots"))
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

    def insert_meal(self, eaten_at: str, source_text: str, image_path: str | None) -> int:
        dt = datetime.fromisoformat(eaten_at)
        now = now_iso()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO meals (eaten_at, meal_slot, source_text, image_path, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (eaten_at, self.slot_for(dt), source_text, image_path, now, now),
            )
            return cur.lastrowid

    def get_meal(self, meal_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
        return _meal_to_dict(row) if row else None

    def update_meal(self, meal_id: int, fields: dict) -> None:
        fields = dict(fields)
        if "items" in fields and not isinstance(fields["items"], str):
            fields["items"] = json.dumps(fields["items"], ensure_ascii=False)
        fields["updated_at"] = now_iso()
        columns = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE meals SET {columns} WHERE id = ?",
                (*fields.values(), meal_id),
            )

    def delete_meal(self, meal_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM meals WHERE id = ?", (meal_id,))

    def list_meals(self, start: str, end: str) -> list[dict]:
        """Meals with eaten_at in [start, end), ISO datetime bounds."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM meals WHERE eaten_at >= ? AND eaten_at < ? ORDER BY eaten_at",
                (start, end),
            ).fetchall()
        return [_meal_to_dict(r) for r in rows]

    def recent_done_meals(self, limit: int = 8) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM meals WHERE status = 'done' ORDER BY eaten_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_meal_to_dict(r) for r in rows]

    def unfinished_meal_ids(self) -> list[int]:
        """Meals left pending/processing (e.g. after a restart)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM meals WHERE status IN ('pending', 'processing') ORDER BY id"
            ).fetchall()
        return [r["id"] for r in rows]

    def daily_totals(self, start_date: str, end_date: str) -> list[dict]:
        """Per-day sums of done meals for date(eaten_at) in [start_date, end_date]."""
        sums = ", ".join(f"SUM({k}) AS {k}" for k in ("kcal", *config.NUTRIENT_KEYS))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT date(eaten_at) AS date, {sums} "
                "FROM meals WHERE status = 'done' AND date(eaten_at) BETWEEN ? AND ? "
                "GROUP BY date(eaten_at) ORDER BY date(eaten_at)",
                (start_date, end_date),
            ).fetchall()
        return [dict(r) for r in rows]

    def day_has_done_meals(self, date: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM meals WHERE status = 'done' AND date(eaten_at) = ?",
                (date,),
            ).fetchone()
        return row["n"] > 0

    # ---------- weights ----------

    def insert_weight(self, weight_kg: float, measured_at: str | None = None) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO weights (measured_at, weight_kg) VALUES (?, ?)",
                (measured_at or now_iso(), weight_kg),
            )
            return cur.lastrowid

    def latest_weight(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM weights ORDER BY measured_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def list_weights(self, start_date: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM weights WHERE date(measured_at) >= ? ORDER BY measured_at",
                (start_date,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_weight(self, weight_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM weights WHERE id = ?", (weight_id,))

    # ---------- daily advice ----------

    def get_advice(self, date: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM daily_advice WHERE date = ?", (date,)).fetchone()
        return dict(row) if row else None

    def create_advice_job(self, date: str) -> None:
        """Insert or reset the row to pending; previous advice text is kept while regenerating."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO daily_advice (date, advice, status, generated_at) VALUES (?, '', 'pending', ?) "
                "ON CONFLICT(date) DO UPDATE SET status = 'pending', error = NULL",
                (date, now_iso()),
            )

    def update_advice(self, date: str, fields: dict) -> None:
        columns = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE daily_advice SET {columns} WHERE date = ?",
                (*fields.values(), date),
            )

    def save_advice(self, date: str, advice: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO daily_advice (date, advice, status, error, generated_at) "
                "VALUES (?, ?, 'done', NULL, ?) "
                "ON CONFLICT(date) DO UPDATE SET advice = excluded.advice, status = 'done', error = NULL, "
                "generated_at = excluded.generated_at",
                (date, advice, now_iso()),
            )

    def unfinished_advice_dates(self) -> list[str]:
        """Advice jobs left pending/processing (e.g. after a restart)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT date FROM daily_advice WHERE status IN ('pending', 'processing') ORDER BY date"
            ).fetchall()
        return [r["date"] for r in rows]

    # ---------- food checks ----------

    def insert_check(self, image_path: str) -> int:
        now = now_iso()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO food_checks (image_path, status, created_at, updated_at) "
                "VALUES (?, 'pending', ?, ?)",
                (image_path, now, now),
            )
            return cur.lastrowid

    def get_check(self, check_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM food_checks WHERE id = ?", (check_id,)).fetchone()
        return _check_to_dict(row) if row else None

    def update_check(self, check_id: int, fields: dict) -> None:
        fields = dict(fields)
        if "estimate" in fields and not isinstance(fields["estimate"], str):
            fields["estimate"] = json.dumps(fields["estimate"], ensure_ascii=False)
        fields["updated_at"] = now_iso()
        columns = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE food_checks SET {columns} WHERE id = ?",
                (*fields.values(), check_id),
            )

    def unfinished_check_ids(self) -> list[int]:
        """Checks left pending/processing (e.g. after a restart)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM food_checks WHERE status IN ('pending', 'processing') ORDER BY id"
            ).fetchall()
        return [r["id"] for r in rows]

    def eat_check(self, check_id: int) -> int | None:
        """Record a finished check as a done meal eaten now.

        Returns the new meal id, or None if the check was already eaten. Claiming
        the check and inserting the meal share one transaction, so a double tap
        cannot log the meal twice.
        """
        eaten_at = now_iso()
        slot = self.slot_for(datetime.fromisoformat(eaten_at))
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT image_path, estimate, meal_id FROM food_checks WHERE id = ?", (check_id,)
            ).fetchone()
            if row is None or row["meal_id"] is not None:
                return None
            estimate = json.loads(row["estimate"])
            columns = ("description", "kcal", *config.NUTRIENT_KEYS, "comment")
            cur = conn.execute(
                f"INSERT INTO meals (eaten_at, meal_slot, items, image_path, status, created_at, updated_at, "
                f"{', '.join(columns)}) VALUES (?, ?, ?, ?, 'done', ?, ?, {', '.join('?' for _ in columns)})",
                (
                    eaten_at, slot, json.dumps(estimate["items"], ensure_ascii=False),
                    row["image_path"], eaten_at, eaten_at,
                    *(estimate[k] for k in columns),
                ),
            )
            conn.execute(
                "UPDATE food_checks SET meal_id = ?, updated_at = ? WHERE id = ?",
                (cur.lastrowid, eaten_at, check_id),
            )
            return cur.lastrowid
