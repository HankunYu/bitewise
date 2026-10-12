import os
import stat

import pytest
from fastapi.routing import APIRoute

from bitewise import auth, config, users
from bitewise.app import app

from .conftest import ALICE_TOKEN, BOB_TOKEN, bearer

PUBLIC_ROUTES = {
    ("GET", "/login"),
    ("POST", "/api/session"),
    ("DELETE", "/api/session"),
    ("GET", "/favicon.ico"),
    ("GET", "/apple-touch-icon.png"),
    ("GET", "/apple-touch-icon-precomposed.png"),
}


def _dependency_calls(dependant) -> set:
    calls = set()
    for dep in dependant.dependencies:
        calls.add(dep.call)
        calls |= _dependency_calls(dep)
    return calls


def store_of(name: str):
    return next(s for s in users.all_stores() if s.user == name)


# ---------- default deny ----------

def test_every_non_public_route_requires_auth(client):
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if (method, route.path) in PUBLIC_ROUTES:
                continue
            assert auth.current_store in _dependency_calls(route.dependant), (
                f"{method} {route.path} is reachable without authentication")


def test_root_icons_are_public(client):
    for path in ("/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
        res = client.get(path)
        assert res.status_code == 200
        assert res.headers["content-type"] == "image/png"


def test_api_docs_are_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path, headers=bearer(ALICE_TOKEN)).status_code == 404


def test_unauthenticated_requests_are_rejected(client):
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/settings", headers=bearer("x" * 43)).status_code == 401
    assert client.post("/api/upload", data={"text": "体重 70"}).status_code == 401
    assert client.get("/uploads/anything.jpeg").status_code == 401


def test_page_load_redirects_to_login(client):
    res = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/login"
    assert client.get("/login").status_code == 200


# ---------- sessions ----------

def test_login_sets_cookie_and_logout_clears_it(client):
    client.cookies.clear()
    assert client.post("/api/session", json={"token": "wrong"}).status_code == 401

    res = client.post("/api/session", json={"token": BOB_TOKEN})
    assert res.status_code == 200
    assert res.json() == {"user": "bob"}
    cookie = res.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert client.get("/api/me").json()["user"] == "bob"

    client.delete("/api/session")
    client.cookies.clear()
    assert client.get("/api/me").status_code == 401


def test_bearer_identifies_user(client):
    assert client.get("/api/me", headers=bearer(ALICE_TOKEN)).json()["user"] == "alice"
    assert client.get("/api/me", headers=bearer(BOB_TOKEN)).json()["user"] == "bob"


# ---------- isolation ----------

def test_weights_are_per_user(client):
    res = client.post("/api/upload", data={"text": "体重 70.5"}, headers=bearer(ALICE_TOKEN))
    assert res.json()["type"] == "weight"
    alice = client.get("/api/weights", headers=bearer(ALICE_TOKEN)).json()
    bob = client.get("/api/weights", headers=bearer(BOB_TOKEN)).json()
    assert [w["weight_kg"] for w in alice] == [70.5]
    assert bob == []


def test_meals_of_other_users_are_not_found(client):
    meal_id = store_of("alice").insert_meal(store_of("alice").now_iso(), "test", None)
    headers = bearer(BOB_TOKEN)
    assert client.patch(f"/api/meals/{meal_id}", json={"kcal": 1}, headers=headers).status_code == 404
    assert client.delete(f"/api/meals/{meal_id}", headers=headers).status_code == 404
    assert store_of("alice").get_meal(meal_id) is not None


def test_uploads_are_per_user(client):
    alice = store_of("alice")
    (alice.upload_dir / "photo.jpeg").write_bytes(b"jpeg")
    assert client.get("/uploads/photo.jpeg", headers=bearer(ALICE_TOKEN)).content == b"jpeg"
    assert client.get("/uploads/photo.jpeg", headers=bearer(BOB_TOKEN)).status_code == 404
    assert client.get("/uploads/..%2Fbitewise.db", headers=bearer(ALICE_TOKEN)).status_code == 404


def test_settings_are_per_user(client):
    client.put("/api/settings", json={"daily_kcal_target": 1500}, headers=bearer(ALICE_TOKEN))
    alice = client.get("/api/settings", headers=bearer(ALICE_TOKEN)).json()
    bob = client.get("/api/settings", headers=bearer(BOB_TOKEN)).json()
    assert alice["daily_kcal_target"] == "1500"
    assert bob["daily_kcal_target"] == config.DEFAULT_SETTINGS["daily_kcal_target"]


# ---------- users file ----------

@pytest.fixture
def users_file(tmp_path, monkeypatch):
    path = tmp_path / "users.toml"
    monkeypatch.setattr(config, "USERS_FILE", path)
    return path


def test_add_user_writes_private_file_with_working_token(users_file, monkeypatch):
    # Keep the shared registry intact for the app tests.
    monkeypatch.setattr(users, "_stores", {})
    monkeypatch.setattr(users, "_tokens", {})
    token_a = users.add_user("carol")
    token_b = users.add_user("dave")
    assert stat.S_IMODE(os.stat(users_file).st_mode) == 0o600
    users.load()
    assert users.authenticate(token_a).user == "carol"
    assert users.authenticate(token_b).user == "dave"
    assert users.authenticate("nope") is None


def test_add_user_rejects_duplicates_and_bad_names(users_file):
    users.add_user("carol")
    with pytest.raises(users.UsersError):
        users.add_user("carol")
    for bad in ("", "Carol", "../x", "a b"):
        with pytest.raises(users.UsersError):
            users.add_user(bad)


def test_load_rejects_weak_or_shared_tokens(users_file, monkeypatch):
    monkeypatch.setattr(users, "_stores", {})
    monkeypatch.setattr(users, "_tokens", {})
    with pytest.raises(users.UsersError):
        users.load()  # missing file
    users_file.write_text('[users.x]\ntoken = "short"\n')
    with pytest.raises(users.UsersError):
        users.load()
    users_file.write_text(f'[users.x]\ntoken = "{"t" * 40}"\n[users.y]\ntoken = "{"t" * 40}"\n')
    with pytest.raises(users.UsersError):
        users.load()
