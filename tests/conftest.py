import os
import tempfile
from pathlib import Path

import pytest

# Point bitewise at a throwaway data dir before it is imported; an empty API key
# keeps tests from ever reaching the real Anthropic API through .env.
_TMP = Path(tempfile.mkdtemp(prefix="bitewise-test-"))
os.environ["BITEWISE_DATA"] = str(_TMP / "data")
os.environ["BITEWISE_USERS"] = str(_TMP / "users.toml")
os.environ["ANTHROPIC_API_KEY"] = ""

ALICE_TOKEN = "a" * 43
BOB_TOKEN = "b" * 43
Path(os.environ["BITEWISE_USERS"]).write_text(
    f'[users.alice]\ntoken = "{ALICE_TOKEN}"\n\n[users.bob]\ntoken = "{BOB_TOKEN}"\n'
)


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from bitewise.app import app

    with TestClient(app) as test_client:
        yield test_client


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
