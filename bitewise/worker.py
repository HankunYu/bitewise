import json
import logging
import queue
import threading
from datetime import datetime

from . import ai, db

logger = logging.getLogger("bitewise.worker")

# Jobs are (kind, key): ("meal", meal_id), ("advice", date) or ("targets", "").
_queue: queue.Queue[tuple[str, int | str]] = queue.Queue()


def enqueue_meal(meal_id: int) -> None:
    _queue.put(("meal", meal_id))


def enqueue_advice(date: str) -> None:
    _queue.put(("advice", date))


def enqueue_targets() -> None:
    _queue.put(("targets", ""))


def start() -> None:
    # Requeue anything left unfinished by a previous run.
    for meal_id in db.unfinished_meal_ids():
        _queue.put(("meal", meal_id))
    for advice_date in db.unfinished_advice_dates():
        _queue.put(("advice", advice_date))
    if db.get_targets_job()["status"] in ("pending", "processing"):
        _queue.put(("targets", ""))
    threading.Thread(target=_run, daemon=True, name="bitewise-worker").start()


def _run() -> None:
    while True:
        kind, key = _queue.get()
        try:
            if kind == "meal":
                _process_meal(key)
            elif kind == "advice":
                _process_advice(key)
            else:
                _process_targets()
        except ai.AIError as exc:
            logger.warning("%s %s failed: %s", kind, key, exc)
            _mark_failed(kind, key, str(exc))
        except Exception:
            logger.exception("%s %s crashed", kind, key)
            _mark_failed(kind, key, "internal error, see logs")


def _mark_failed(kind: str, key: int | str, error: str) -> None:
    if kind == "meal":
        db.update_meal(key, {"status": "failed", "error": error})
    elif kind == "advice":
        db.update_advice(key, {"status": "failed", "error": error})
    else:
        db.set_targets_job("failed", error)


def _process_targets() -> None:
    db.set_targets_job("processing")
    targets = ai.nutrient_targets()
    db.set_settings({"nutrient_targets": json.dumps(targets, ensure_ascii=False)})
    db.set_targets_job("done")
    logger.info("nutrient targets generated: %s", targets)


def _process_advice(date: str) -> None:
    row = db.get_advice(date)
    if row is None or row["status"] == "done":
        return
    db.update_advice(date, {"status": "processing", "error": None})
    db.save_advice(date, ai.daily_advice(date))
    logger.info("advice %s done", date)


def _process_meal(meal_id: int) -> None:
    meal = db.get_meal(meal_id)
    if meal is None or meal["status"] == "done":
        return
    db.update_meal(meal_id, {"status": "processing", "error": None})

    # kcal is only ever set together with status='done', so a non-null kcal means
    # this meal was estimated before: it is a user-triggered recalculation.
    if meal["kcal"] is not None:
        estimate = ai.estimate_meal(
            meal["image_path"], meal["source_text"], correction=meal["description"]
        )
        db.update_meal(meal_id, {**estimate, "status": "done"})
        logger.info("meal %s recalculated: %s (%s kcal)", meal_id, estimate["description"], estimate["kcal"])
        return

    if meal["image_path"]:
        estimate = ai.estimate_meal(meal["image_path"], meal["source_text"])
        db.update_meal(meal_id, {**estimate, "status": "done"})
        logger.info("meal %s done: %s (%s kcal)", meal_id, estimate["description"], estimate["kcal"])
        return

    action = ai.interpret_text(meal["source_text"])
    kind = action.get("action")

    if kind == "add_meal":
        fields = {**action["meal"], "status": "done"}
        eaten_at = action.get("eaten_at")
        if eaten_at:
            try:
                dt = datetime.fromisoformat(eaten_at)
                fields["eaten_at"] = dt.strftime("%Y-%m-%dT%H:%M:%S")
                fields["meal_slot"] = db.slot_for(dt)
            except ValueError:
                pass
        db.update_meal(meal_id, fields)
        logger.info("meal %s done: %s (%s kcal)", meal_id, fields["description"], fields["kcal"])
        return

    # The remaining actions are commands, not meals: apply them, then drop the
    # placeholder row that carried the command text.
    if kind == "update_meal":
        target = db.get_meal(int(action["id"]))
        if target is None:
            raise ai.AIError(f"update target meal {action['id']} not found")
        fields = {
            k: v for k, v in dict(action.get("fields", {})).items()
            if k in db.MEAL_EDITABLE_FIELDS
        }
        if not fields:
            raise ai.AIError("update_meal returned no editable fields")
        if "eaten_at" in fields and "meal_slot" not in fields:
            try:
                fields["meal_slot"] = db.slot_for(datetime.fromisoformat(fields["eaten_at"]))
            except ValueError:
                del fields["eaten_at"]
        db.update_meal(int(action["id"]), fields)
        db.delete_meal(meal_id)
        logger.info("meal %s updated via command: %s", action["id"], fields)
    elif kind == "delete_meal":
        db.delete_meal(int(action["id"]))
        db.delete_meal(meal_id)
        logger.info("meal %s deleted via command", action["id"])
    elif kind == "add_weight":
        db.insert_weight(float(action["weight_kg"]))
        db.delete_meal(meal_id)
        logger.info("weight %.1f kg recorded via command", float(action["weight_kg"]))
    else:
        raise ai.AIError(f"unknown action from model: {kind!r}")
