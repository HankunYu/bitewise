"""User registry: maps access tokens to per-user stores.

users.toml (path from config.USERS_FILE) holds one table per user:

    [users.alice]
    token = "..."

Each user's data lives in config.DATA_DIR/<name>/. The file is read once at
startup; restart the server after adding a user.
"""

import hmac
import os
import re
import secrets
import tomllib

from . import config
from .db import Store

NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
MIN_TOKEN_LENGTH = 32

_stores: dict[str, Store] = {}
_tokens: dict[str, str] = {}


class UsersError(Exception):
    pass


def _read_entries() -> dict:
    if not config.USERS_FILE.exists():
        return {}
    try:
        with config.USERS_FILE.open("rb") as f:
            return tomllib.load(f).get("users", {})
    except tomllib.TOMLDecodeError as exc:
        raise UsersError(f"{config.USERS_FILE} is not valid TOML: {exc}") from exc


def load() -> None:
    """Read the users file; raises UsersError if it is missing, invalid or empty."""
    stores, tokens = {}, {}
    for name, entry in _read_entries().items():
        if not NAME_PATTERN.fullmatch(name):
            raise UsersError(f"invalid user name {name!r}: use lowercase letters, digits, - and _")
        token = entry.get("token", "") if isinstance(entry, dict) else ""
        if len(token) < MIN_TOKEN_LENGTH:
            raise UsersError(f"token of user {name!r} must be at least {MIN_TOKEN_LENGTH} characters")
        if token in tokens:
            raise UsersError(f"users {tokens[token]!r} and {name!r} share a token")
        stores[name] = Store(name, config.DATA_DIR / name)
        tokens[token] = name
    if not stores:
        raise UsersError(f"no users in {config.USERS_FILE}, add one with: python -m bitewise add-user <name>")

    _stores.clear()
    _stores.update(stores)
    _tokens.clear()
    _tokens.update(tokens)


def authenticate(token: str | None) -> Store | None:
    """The store of the user owning this token, or None."""
    if not token:
        return None
    # Compare against every token so the match position does not leak through timing.
    match = None
    for known, name in _tokens.items():
        if hmac.compare_digest(known.encode(), token.encode()):
            match = name
    return _stores[match] if match else None


def all_stores() -> list[Store]:
    return list(_stores.values())


def add_user(name: str) -> str:
    """Append a new user with a fresh token to the users file; returns the token."""
    if not NAME_PATTERN.fullmatch(name):
        raise UsersError(f"invalid user name {name!r}: use lowercase letters, digits, - and _")
    if name in _read_entries():
        raise UsersError(f"user {name!r} already exists in {config.USERS_FILE}")

    token = secrets.token_urlsafe(32)
    # token_urlsafe only yields [A-Za-z0-9_-], safe inside a TOML basic string.
    entry = f'[users.{name}]\ntoken = "{token}"\n'
    if config.USERS_FILE.exists() and config.USERS_FILE.stat().st_size > 0:
        entry = "\n" + entry
    # The file holds secrets: create it owner-only.
    fd = os.open(config.USERS_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(entry)
    return token
