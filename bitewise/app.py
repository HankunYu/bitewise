import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, scheduler, worker

logger = logging.getLogger("bitewise")

WEIGHT_PATTERN = re.compile(
    r"^\s*体重\s*[::]?\s*(\d{2,3}(?:\.\d+)?)\s*(kg|公斤|斤)?\s*$", re.IGNORECASE
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config.ensure_dirs()
    db.init_db()
    worker.start()
    scheduler.start()
    yield


app = FastAPI(title="bitewise", lifespan=lifespan)


def _parse_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(400, f"invalid date: {value}")


# ---------- ingest ----------

@app.post("/api/upload")
async def upload(image: UploadFile | None = File(None), text: str = Form("")):
    text = text.strip()
    if image is None and not text:
        raise HTTPException(400, "need an image or some text")

    # Fast path: plain weight messages skip the AI entirely.
    if image is None:
        match = WEIGHT_PATTERN.match(text)
        if match:
            kg = float(match.group(1))
            if match.group(2) == "斤":
                kg /= 2
            weight_id = db.insert_weight(kg)
            return {"type": "weight", "id": weight_id, "weight_kg": kg}

    image_path = None
    if image is not None:
        suffix = (image.filename or "").rsplit(".", 1)
        ext = suffix[1].lower() if len(suffix) == 2 and len(suffix[1]) <= 5 else "jpg"
        name = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.{ext}"
        (config.UPLOAD_DIR / name).write_bytes(await image.read())
        image_path = f"uploads/{name}"

    meal_id = db.insert_meal(db.now_iso(), text, image_path)
    worker.enqueue_meal(meal_id)
    return {"type": "meal", "id": meal_id, "status": "pending"}


# ---------- meals ----------

@app.get("/api/day/{day}")
def get_day(day: str):
    day = _parse_date(day)
    meals = db.list_meals(f"{day}T00:00:00", f"{day}T23:59:59")
    done = [m for m in meals if m["status"] == "done"]
    totals = {
        key: round(sum(m[key] or 0 for m in done), 1)
        for key in ("kcal", "protein_g", "carbs_g", "fat_g")
    }
    weights = db.list_weights(day)
    weight = next((w["weight_kg"] for w in reversed(weights) if w["measured_at"][:10] == day), None)
    advice = db.get_advice(day)
    return {
        "date": day,
        "meals": meals,
        "totals": totals,
        "target": int(db.get_setting("daily_kcal_target")),
        "advice": {
            "text": advice["advice"] or None,
            "status": advice["status"],
            "error": advice["error"],
        } if advice else None,
        "weight": weight,
    }


@app.patch("/api/meals/{meal_id}")
def patch_meal(meal_id: int, fields: dict):
    if db.get_meal(meal_id) is None:
        raise HTTPException(404, "meal not found")
    clean = {}
    for key, value in fields.items():
        if key not in db.MEAL_EDITABLE_FIELDS:
            raise HTTPException(400, f"field not editable: {key}")
        if key in ("kcal", "protein_g", "carbs_g", "fat_g"):
            try:
                clean[key] = round(float(value), 1)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{key} must be a number")
        elif key == "eaten_at":
            try:
                dt = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                raise HTTPException(400, "eaten_at must be ISO datetime")
            clean["eaten_at"] = dt.strftime("%Y-%m-%dT%H:%M:%S")
            if "meal_slot" not in fields:
                clean["meal_slot"] = db.slot_for(dt)
        else:
            clean[key] = value
    if not clean:
        raise HTTPException(400, "no fields to update")
    db.update_meal(meal_id, clean)
    return db.get_meal(meal_id)


@app.delete("/api/meals/{meal_id}")
def remove_meal(meal_id: int):
    if db.get_meal(meal_id) is None:
        raise HTTPException(404, "meal not found")
    db.delete_meal(meal_id)
    return {"deleted": meal_id}


@app.post("/api/meals/{meal_id}/retry")
def retry_meal(meal_id: int):
    meal = db.get_meal(meal_id)
    if meal is None:
        raise HTTPException(404, "meal not found")
    if meal["status"] != "failed":
        raise HTTPException(400, f"meal is {meal['status']}, only failed meals can be retried")
    db.update_meal(meal_id, {"status": "pending", "error": None})
    worker.enqueue_meal(meal_id)
    return {"id": meal_id, "status": "pending"}


# ---------- trends ----------

@app.get("/api/trends")
def trends(days: int = 30):
    days = max(1, min(days, 365))
    end = date.today()
    start = end - timedelta(days=days - 1)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": db.daily_totals(start.isoformat(), end.isoformat()),
        "weights": db.list_weights(start.isoformat()),
        "target": int(db.get_setting("daily_kcal_target")),
    }


# ---------- weights ----------

@app.get("/api/weights")
def get_weights(days: int = 90):
    start = date.today() - timedelta(days=max(1, min(days, 3650)) - 1)
    return db.list_weights(start.isoformat())


@app.post("/api/weights")
def add_weight(body: dict):
    try:
        kg = float(body["weight_kg"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "weight_kg must be a number")
    measured_at = body.get("measured_at") or db.now_iso()
    weight_id = db.insert_weight(kg, measured_at)
    return {"id": weight_id, "weight_kg": kg, "measured_at": measured_at}


@app.delete("/api/weights/{weight_id}")
def remove_weight(weight_id: int):
    db.delete_weight(weight_id)
    return {"deleted": weight_id}


# ---------- advice ----------

@app.post("/api/advice/{day}/generate")
def generate_advice(day: str):
    day = _parse_date(day)
    if not db.day_has_done_meals(day):
        raise HTTPException(400, "no finished meals on that day")
    db.create_advice_job(day)
    worker.enqueue_advice(day)
    return {"date": day, "status": "pending"}


# ---------- settings ----------

@app.get("/api/settings")
def get_settings():
    return db.get_all_settings()


@app.put("/api/settings")
def put_settings(values: dict):
    unknown = set(values) - set(config.DEFAULT_SETTINGS)
    if unknown:
        raise HTTPException(400, f"unknown settings: {', '.join(sorted(unknown))}")
    if "daily_kcal_target" in values:
        try:
            values["daily_kcal_target"] = str(int(values["daily_kcal_target"]))
        except (TypeError, ValueError):
            raise HTTPException(400, "daily_kcal_target must be an integer")
    if "advice_time" in values and not re.fullmatch(r"\d{2}:\d{2}", str(values["advice_time"])):
        raise HTTPException(400, "advice_time must look like 23:00")
    if "meal_slots" in values:
        try:
            slots = values["meal_slots"]
            slots = json.loads(slots) if isinstance(slots, str) else slots
            assert all(len(v) == 2 for v in slots.values())
            values["meal_slots"] = json.dumps(slots, ensure_ascii=False)
        except (ValueError, AttributeError, AssertionError):
            raise HTTPException(400, "meal_slots must be {name: [start, end]} JSON")
    db.set_settings(values)
    return db.get_all_settings()


# ---------- frontend ----------

app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(config.STATIC_DIR / "index.html")
