import logging
import queue
import threading
from datetime import datetime

from . import ai, db

logger = logging.getLogger("bitewise.worker")

_queue: queue.Queue[int] = queue.Queue()


def enqueue(meal_id: int) -> None:
    _queue.put(meal_id)


def start() -> None:
    # Requeue anything left unfinished by a previous run.
    for meal_id in db.unfinished_meal_ids():
        _queue.put(meal_id)
    threading.Thread(target=_run, daemon=True, name="bitewise-worker").start()


def _run() -> None:
    while True:
        meal_id = _queue.get()
        try:
            _process(meal_id)
        except ai.AIError as exc:
            logger.warning("meal %s failed: %s", meal_id, exc)
            db.update_meal(meal_id, {"status": "failed", "error": str(exc)})
        except Exception:
            logger.exception("meal %s crashed", meal_id)
            db.update_meal(meal_id, {"status": "failed", "error": "internal error, see logs"})


def _process(meal_id: int) -> None:
    meal = db.get_meal(meal_id)
    if meal is None or meal["status"] == "done":
        return
    db.update_meal(meal_id, {"status": "processing", "error": None})

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
