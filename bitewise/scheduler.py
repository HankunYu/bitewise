import logging
import threading
import time
from datetime import date, datetime

from . import db, worker

logger = logging.getLogger("bitewise.scheduler")


def start() -> None:
    threading.Thread(target=_run, daemon=True, name="bitewise-scheduler").start()


def _run() -> None:
    while True:
        try:
            _tick()
        except Exception:
            logger.exception("scheduler tick failed")
        time.sleep(30)


def _tick() -> None:
    advice_time = db.get_setting("advice_time")
    if datetime.now().strftime("%H:%M") < advice_time:
        return
    today = date.today().isoformat()
    if db.get_advice(today) is not None:
        return
    if not db.day_has_done_meals(today):
        return
    logger.info("queueing daily advice for %s", today)
    db.create_advice_job(today)
    worker.enqueue_advice(today)
