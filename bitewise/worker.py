import json
import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from . import ai, db, users
from .db import Store

logger = logging.getLogger("bitewise.worker")

# Jobs are (store, kind, key), e.g. (store, "meal", meal_id), (store, "advice", date),
# (store, "targets", "") or (store, "check", check_id). Each kind runs on a lane
# (one queue + one thread) shared by all users: lanes run in parallel, jobs
# within a lane run in order.


@dataclass(frozen=True)
class _JobType:
    process: Callable[[Store, int | str], None]
    fail: Callable[[Store, int | str, str], None]
    lane: str


def enqueue_meal(store: Store, meal_id: int) -> None:
    _enqueue(store, "meal", meal_id)


def enqueue_advice(store: Store, date: str) -> None:
    _enqueue(store, "advice", date)


def enqueue_targets(store: Store) -> None:
    _enqueue(store, "targets", "")


def enqueue_check(store: Store, check_id: int) -> None:
    _enqueue(store, "check", check_id)


def _enqueue(store: Store, kind: str, key: int | str) -> None:
    _lanes[_JOB_TYPES[kind].lane].put((store, kind, key))


def start() -> None:
    # Requeue anything left unfinished by a previous run.
    for store in users.all_stores():
        for meal_id in store.unfinished_meal_ids():
            enqueue_meal(store, meal_id)
        for advice_date in store.unfinished_advice_dates():
            enqueue_advice(store, advice_date)
        if store.get_targets_job()["status"] in ("pending", "processing"):
            enqueue_targets(store)
        for check_id in store.unfinished_check_ids():
            enqueue_check(store, check_id)
    for name, lane in _lanes.items():
        threading.Thread(target=_run, args=(lane,), daemon=True, name=f"bitewise-worker-{name}").start()


def _run(lane: queue.Queue) -> None:
    while True:
        store, kind, key = lane.get()
        job = _JOB_TYPES[kind]
        try:
            job.process(store, key)
        except ai.AIError as exc:
            logger.warning("[%s] %s %s failed: %s", store.user, kind, key, exc)
            job.fail(store, key, str(exc))
        except Exception:
            logger.exception("[%s] %s %s crashed", store.user, kind, key)
            job.fail(store, key, "internal error, see logs")


def _process_targets(store: Store, _key: str) -> None:
    store.set_targets_job("processing")
    targets = ai.nutrient_targets(store)
    store.set_settings({"nutrient_targets": json.dumps(targets, ensure_ascii=False)})
    store.set_targets_job("done")
    logger.info("[%s] nutrient targets generated: %s", store.user, targets)


def _process_advice(store: Store, date: str) -> None:
    row = store.get_advice(date)
    if row is None or row["status"] == "done":
        return
    store.update_advice(date, {"status": "processing", "error": None})
    store.save_advice(date, ai.daily_advice(store, date))
    logger.info("[%s] advice %s done", store.user, date)


def _process_meal(store: Store, meal_id: int) -> None:
    meal = store.get_meal(meal_id)
    if meal is None or meal["status"] == "done":
        return
    store.update_meal(meal_id, {"status": "processing", "error": None})

    # kcal is only ever set together with status='done', so a non-null kcal means
    # this meal was estimated before: it is a user-triggered recalculation.
    if meal["kcal"] is not None:
        estimate = ai.estimate_meal(
            store, meal["image_path"], meal["source_text"], correction=meal["description"]
        )
        store.update_meal(meal_id, {**estimate, "status": "done"})
        logger.info("[%s] meal %s recalculated: %s (%s kcal)",
                    store.user, meal_id, estimate["description"], estimate["kcal"])
        return

    if meal["image_path"]:
        estimate = ai.estimate_meal(store, meal["image_path"], meal["source_text"])
        store.update_meal(meal_id, {**estimate, "status": "done"})
        logger.info("[%s] meal %s done: %s (%s kcal)",
                    store.user, meal_id, estimate["description"], estimate["kcal"])
        return

    action = ai.interpret_text(store, meal["source_text"])
    kind = action.get("action")

    if kind == "add_meal":
        fields = {**action["meal"], "status": "done"}
        eaten_at = action.get("eaten_at")
        if eaten_at:
            try:
                dt = datetime.fromisoformat(eaten_at)
                fields["eaten_at"] = dt.strftime("%Y-%m-%dT%H:%M:%S")
                fields["meal_slot"] = store.slot_for(dt)
            except ValueError:
                pass
        store.update_meal(meal_id, fields)
        logger.info("[%s] meal %s done: %s (%s kcal)",
                    store.user, meal_id, fields["description"], fields["kcal"])
        return

    # The remaining actions are commands, not meals: apply them, then drop the
    # placeholder row that carried the command text.
    if kind == "update_meal":
        target = store.get_meal(int(action["id"]))
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
                fields["meal_slot"] = store.slot_for(datetime.fromisoformat(fields["eaten_at"]))
            except ValueError:
                del fields["eaten_at"]
        store.update_meal(int(action["id"]), fields)
        store.delete_meal(meal_id)
        logger.info("[%s] meal %s updated via command: %s", store.user, action["id"], fields)
    elif kind == "delete_meal":
        store.delete_meal(int(action["id"]))
        store.delete_meal(meal_id)
        logger.info("[%s] meal %s deleted via command", store.user, action["id"])
    elif kind == "add_weight":
        store.insert_weight(float(action["weight_kg"]))
        store.delete_meal(meal_id)
        logger.info("[%s] weight %.1f kg recorded via command", store.user, float(action["weight_kg"]))
    else:
        raise ai.AIError(f"unknown action from model: {kind!r}")


def _process_check(store: Store, check_id: int) -> None:
    check = store.get_check(check_id)
    if check is None or check["status"] == "done":
        return
    store.update_check(check_id, {"status": "processing", "error": None})
    result = ai.check_food(store, check["image_path"])
    store.update_check(check_id, {**result, "status": "done"})
    logger.info("[%s] check %s done: %s (%s kcal)",
                store.user, check_id, result["verdict"], result["estimate"]["kcal"])


# Meal, advice and targets share the main lane: advice and targets read what
# meal jobs write. Checks are independent and interactive, so they get their own.
_JOB_TYPES = {
    "meal": _JobType(
        _process_meal,
        lambda store, key, error: store.update_meal(key, {"status": "failed", "error": error}),
        "main"),
    "advice": _JobType(
        _process_advice,
        lambda store, key, error: store.update_advice(key, {"status": "failed", "error": error}),
        "main"),
    "targets": _JobType(
        _process_targets,
        lambda store, _key, error: store.set_targets_job("failed", error),
        "main"),
    "check": _JobType(
        _process_check,
        lambda store, key, error: store.update_check(key, {"status": "failed", "error": error}),
        "check"),
}
_lanes: dict[str, queue.Queue[tuple[Store, str, int | str]]] = {
    lane: queue.Queue() for lane in dict.fromkeys(job.lane for job in _JOB_TYPES.values())
}
