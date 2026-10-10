import io
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps

from . import config, db, scheduler, worker

logger = logging.getLogger("bitewise")

WEIGHT_PATTERN = re.compile(
    r"^\s*体重\s*[::]?\s*(\d{2,3}(?:\.\d+)?)\s*(kg|公斤|斤)?\s*$", re.IGNORECASE
)

MAX_IMAGE_EDGE = 1600
JPEG_QUALITY = 82
# Formats kept as-is when Pillow cannot re-encode them; anything else is
# rejected so /uploads never serves attacker-chosen types like .html or .svg.
RAW_IMAGE_EXTS = {"heic", "heif", "jpg", "jpeg", "png", "webp", "gif"}
# Sanity check for the model setting, which is sent as-is to the API.
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]*")


def compress_image(raw: bytes) -> bytes:
    """Re-encode an upload as an upright, bounded-size JPEG."""
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


async def save_upload(image: UploadFile) -> str:
    """Store an uploaded photo under the upload dir; returns its DATA_DIR-relative path."""
    raw = await image.read()
    try:
        data, ext = compress_image(raw), "jpeg"
    except Exception:
        # Unparseable image format (e.g. HEIC): store as received rather than reject.
        suffix = (image.filename or "").rsplit(".", 1)
        ext = suffix[1].lower() if len(suffix) == 2 else ""
        if ext not in RAW_IMAGE_EXTS:
            raise HTTPException(400, f"unsupported image file: {image.filename!r}")
        logger.warning("cannot compress upload %r, storing original", image.filename)
        data = raw
    name = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.{ext}"
    (config.UPLOAD_DIR / name).write_bytes(data)
    return f"uploads/{name}"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config.ensure_dirs()
    db.init_db()
    worker.start()
    scheduler.start()
    yield


app = FastAPI(title="bitewise", lifespan=lifespan)


@app.middleware("http")
async def no_heuristic_cache(request, call_next):
    # Static responses carry ETags but no Cache-Control; without it browsers
    # cache heuristically and keep running stale frontend code after a deploy.
    # no-cache = store, but revalidate every time (304 when unchanged).
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


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

    image_path = await save_upload(image) if image is not None else None

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
        for key in ("kcal", *config.NUTRIENT_KEYS)
    }
    weights = db.list_weights(day)
    weight = next((w["weight_kg"] for w in reversed(weights) if w["measured_at"][:10] == day), None)
    advice = db.get_advice(day)
    return {
        "date": day,
        "meals": meals,
        "totals": totals,
        "target": int(db.get_setting("daily_kcal_target")),
        "nutrient_targets": db.get_nutrient_targets(),
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
        if key in ("kcal", *config.NUTRIENT_KEYS):
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


@app.post("/api/meals/{meal_id}/recalc")
def recalc_meal(meal_id: int):
    """Re-estimate a finished meal, taking its current description as the truth."""
    meal = db.get_meal(meal_id)
    if meal is None:
        raise HTTPException(404, "meal not found")
    if meal["status"] != "done":
        raise HTTPException(400, f"meal is {meal['status']}, only finished meals can be recalculated")
    db.update_meal(meal_id, {"status": "pending", "error": None})
    worker.enqueue_meal(meal_id)
    return {"id": meal_id, "status": "pending"}


# ---------- food checks ----------

VERDICT_LABELS = {"ok": "🟢 可以吃", "moderate": "🟡 适量", "avoid": "🔴 不建议"}


def _check_summary(check: dict) -> str | None:
    """Plain-text result for the iOS shortcut to show as-is."""
    if check["status"] != "done":
        return None
    est = check["estimate"]
    return (
        f"{VERDICT_LABELS[check['verdict']]} · 约 {est['kcal']:.0f} kcal\n"
        f"{est['description']}\n"
        f"蛋白质 {est['protein_g']:.0f}g · 碳水 {est['carbs_g']:.0f}g · 脂肪 {est['fat_g']:.0f}g\n\n"
        f"{check['advice']}"
    )


@app.post("/api/checks")
async def create_check(image: UploadFile = File(...)):
    image_path = await save_upload(image)
    check_id = db.insert_check(image_path)
    worker.enqueue_check(check_id)
    return {"id": check_id, "status": "pending"}


@app.get("/api/checks/{check_id}")
def get_check(check_id: int):
    check = db.get_check(check_id)
    if check is None:
        raise HTTPException(404, "check not found")
    return {**check, "summary": _check_summary(check)}


@app.post("/api/checks/{check_id}/eat")
def eat_check(check_id: int):
    check = db.get_check(check_id)
    if check is None:
        raise HTTPException(404, "check not found")
    if check["status"] != "done":
        raise HTTPException(400, f"check is {check['status']}, only finished checks can be eaten")
    meal_id = db.eat_check(check_id)
    if meal_id is None:
        raise HTTPException(409, "check already recorded as a meal")
    return db.get_meal(meal_id)


# ---------- calendar ----------

@app.get("/api/calendar/{ym}")
def calendar_month(ym: str):
    try:
        first = date.fromisoformat(f"{ym}-01")
    except ValueError:
        raise HTTPException(400, f"invalid month: {ym}")
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    last = next_month - timedelta(days=1)
    return {
        "month": ym,
        "days": db.daily_totals(first.isoformat(), last.isoformat()),
        "target": int(db.get_setting("daily_kcal_target")),
    }


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
    if "model" in values and not MODEL_PATTERN.fullmatch(str(values["model"])):
        raise HTTPException(400, "model must be a model name like claude-opus-5-5")
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
    if "nutrient_targets" in values:
        try:
            targets = values["nutrient_targets"]
            targets = json.loads(targets) if isinstance(targets, str) else targets
            assert set(targets) <= set(config.NUTRIENT_KEYS)
            values["nutrient_targets"] = json.dumps(
                {k: round(float(v), 1) for k, v in targets.items()}, ensure_ascii=False)
        except (ValueError, TypeError, AttributeError, AssertionError):
            raise HTTPException(400, "nutrient_targets must be {nutrient: number} JSON")
    db.set_settings(values)
    return db.get_all_settings()


@app.post("/api/nutrient-targets/generate")
def generate_nutrient_targets():
    job = db.get_targets_job()
    if job["status"] in ("pending", "processing"):
        return {"status": job["status"]}
    db.set_targets_job("pending")
    worker.enqueue_targets()
    return {"status": "pending"}


# ---------- frontend ----------

app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
# Meal photos; check_dir=False because the data dir is created at startup, after import.
app.mount("/uploads", StaticFiles(directory=config.UPLOAD_DIR, check_dir=False), name="uploads")


@app.get("/")
def index():
    return FileResponse(config.STATIC_DIR / "index.html")
