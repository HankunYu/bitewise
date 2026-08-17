import logging
import threading
import time
from datetime import date, datetime

from . import ai, db

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
    logger.info("generating daily advice for %s", today)
    generate(today)


def generate(day: str) -> str:
    advice = ai.daily_advice(day)
    db.save_advice(day, advice)
    return advice
