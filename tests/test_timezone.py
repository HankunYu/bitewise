from datetime import datetime, timezone

import pytest

from bitewise import db
from bitewise.db import Store

from .conftest import ALICE_TOKEN, bearer

# 2026-10-12 22:30 UTC: still the 12th in London, already the 13th in Berlin.
FIXED_UTC = datetime(2026, 10, 12, 22, 30, tzinfo=timezone.utc)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_UTC.astimezone(tz) if tz else FIXED_UTC.replace(tzinfo=None)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "datetime", FixedDatetime)
    store = Store("tester", tmp_path)
    store.init()
    return store


def test_clock_follows_user_timezone(store):
    assert store.get_setting("timezone") == "Europe/London"
    assert store.now_iso() == "2026-10-12T23:30:00"
    assert store.today().isoformat() == "2026-10-12"

    store.set_settings({"timezone": "Europe/Berlin"})
    assert store.now_iso() == "2026-10-13T00:30:00"
    assert store.today().isoformat() == "2026-10-13"


def test_new_meals_use_user_local_time(store):
    store.set_settings({"timezone": "UTC"})
    meal = store.get_meal(store.insert_meal(store.now_iso(), "late snack", None))
    assert meal["eaten_at"] == "2026-10-12T22:30:00"
    assert meal["meal_slot"] == "snack"


def test_timezone_setting_is_validated(client):
    headers = bearer(ALICE_TOKEN)
    for bad in ("Mars/Olympus", "../etc/passwd", ""):
        res = client.put("/api/settings", json={"timezone": bad}, headers=headers)
        assert res.status_code == 400, bad
    res = client.put("/api/settings", json={"timezone": " Europe/Berlin "}, headers=headers)
    assert res.json()["timezone"] == "Europe/Berlin"
    client.put("/api/settings", json={"timezone": "Europe/London"}, headers=headers)
