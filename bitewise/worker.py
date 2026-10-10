import json
import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from . import ai, db

logger = logging.getLogger("bitewise.worker")

# Jobs are (kind, key), e.g. ("meal", meal_id), ("advice", date), ("targets", "")
# or ("check", check_id). Each kind runs on a lane (one queue + one thread):
# lanes run in parallel, jobs within a lane run in order.


@dataclass(frozen=True)
class _JobType:
    process: Callable[[int | str], None]
    fail: Callable[[int | str, str], None]
    lane: str


def enqueue_meal(meal_id: int) -> None:
    _enqueue("meal", meal_id)


def enqueue_advice(date: str) -> None:
    _enqueue("advice", date)


def enqueue_targets() -> None:
    _enqueue("targets", "")


def enqueue_check(check_id: int) -> None:
    _enqueue("check", check_id)


def _enqueue(kind: str, key: int | str) -> None:
    _lanes[_JOB_TYPES[kind].lane].put((kind, key))


def start() -> None:
    # Requeue anything left unfinished by a previous run.
    for meal_id in db.unfinished_meal_ids():
        enqueue_meal(meal_id)
    for advice_date in db.unfinished_advice_dates():
        enqueue_advice(advice_date)
    if db.get_targets_job()["status"] in ("pending", "processing"):
        enqueue_targets()
    for check_id in db.unfinished_check_ids():
        enqueue_check(check_id)
    for name, lane in _lanes.items():
        threading.Thread(target=_run, args=(lane,), daemon=True, name=f"bitewise-worker-{name}").start()


def _run(lane: queue.Queue) -> None:
    while True:
        kind, key = lane.get()
        job = _JOB_TYPES[kind]
        try:
            job.process(key)
        except ai.AIError as exc:
            logger.warning("%s %s failed: %s", kind, key, exc)
            job.fail(key, str(exc))
        except Exception:
            logger.exception("%s %s crashed", kind, key)
            job.fail(key, "internal error, see logs")


def _process_targets(_key: str) -> None:
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


def _process_check(check_id: int) -> None:
    check = db.get_check(check_id)
    if check is None or check["status"] == "done":
        return
    db.update_check(check_id, {"status": "processing", "error": None})
    result = ai.check_food(check["image_path"])
    db.update_check(check_id, {**result, "status": "done"})
    logger.info("check %s done: %s (%s kcal)", check_id, result["verdict"], result["estimate"]["kcal"])


# Meal, advice and targets share the main lane: advice and targets read what
# meal jobs write. Checks are independent and interactive, so they get their own.
_JOB_TYPES = {
    "meal": _JobType(
        _process_meal, lambda key, error: db.update_meal(key, {"status": "failed", "error": error}), "main"),
    "advice": _JobType(
        _process_advice, lambda key, error: db.update_advice(key, {"status": "failed", "error": error}), "main"),
    "targets": _JobType(
        _process_targets, lambda _key, error: db.set_targets_job("failed", error), "main"),
    "check": _JobType(
        _process_check, lambda key, error: db.update_check(key, {"status": "failed", "error": error}), "check"),
}
_lanes: dict[str, queue.Queue[tuple[str, int | str]]] = {
    lane: queue.Queue() for lane in dict.fromkeys(job.lane for job in _JOB_TYPES.values())
}
