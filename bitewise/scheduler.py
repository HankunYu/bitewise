import logging
import threading
import time
from datetime import date, datetime

from . import users, worker
from .db import Store

logger = logging.getLogger("bitewise.scheduler")


def start() -> None:
    threading.Thread(target=_run, daemon=True, name="bitewise-scheduler").start()


def _run() -> None:
    while True:
        for store in users.all_stores():
            try:
                _tick(store)
            except Exception:
                logger.exception("[%s] scheduler tick failed", store.user)
        time.sleep(30)


def _tick(store: Store) -> None:
    advice_time = store.get_setting("advice_time")
    if datetime.now().strftime("%H:%M") < advice_time:
        return
    today = date.today().isoformat()
    if store.get_advice(today) is not None:
        return
    if not store.day_has_done_meals(today):
        return
    logger.info("[%s] queueing daily advice for %s", store.user, today)
    store.create_advice_job(today)
    worker.enqueue_advice(store, today)
