import pytest

from bitewise import ai, worker

from .conftest import ALICE_TOKEN, BOB_TOKEN, bearer
from .test_auth import store_of


@pytest.fixture
def chat(client, monkeypatch):
    """Alice's chat API with the worker queue captured instead of run."""
    queued = []
    monkeypatch.setattr(worker, "enqueue_chat", lambda store, message_id: queued.append((store, message_id)))
    client.delete("/api/chat", headers=bearer(ALICE_TOKEN))
    yield queued
    client.delete("/api/chat", headers=bearer(ALICE_TOKEN))


def _messages(client, token=ALICE_TOKEN):
    return client.get("/api/chat", headers=bearer(token)).json()["messages"]


def test_question_is_answered_by_the_worker(client, chat, monkeypatch):
    res = client.post("/api/chat", json={"text": " 今天还能吃什么? "}, headers=bearer(ALICE_TOKEN))
    assert res.status_code == 200
    reply_id = res.json()["id"]
    assert [(m["role"], m["content"], m["status"]) for m in _messages(client)] == [
        ("user", "今天还能吃什么?", "done"), ("assistant", "", "pending")]
    # One reply at a time.
    assert client.post("/api/chat", json={"text": "再问"}, headers=bearer(ALICE_TOKEN)).status_code == 409

    monkeypatch.setattr(ai, "chat_reply", lambda store, message_id: "吃点鸡胸肉")
    store, queued_id = chat[0]
    assert (store.user, queued_id) == ("alice", reply_id)
    worker._process_chat(store, queued_id)
    reply = _messages(client)[-1]
    assert (reply["content"], reply["status"]) == ("吃点鸡胸肉", "done")
    # The conversation is per user.
    assert _messages(client, BOB_TOKEN) == []


def test_failed_reply_can_be_retried(client, chat):
    reply_id = client.post("/api/chat", json={"text": "hi"}, headers=bearer(ALICE_TOKEN)).json()["id"]
    assert client.post(f"/api/chat/{reply_id}/retry", headers=bearer(ALICE_TOKEN)).status_code == 400
    worker._JOB_TYPES["chat"].fail(store_of("alice"), reply_id, "boom")
    assert _messages(client)[-1]["error"] == "boom"
    assert client.post(f"/api/chat/{reply_id}/retry", headers=bearer(ALICE_TOKEN)).status_code == 200
    assert _messages(client)[-1]["status"] == "pending"
    assert chat[-1][1] == reply_id


def test_clearing_drops_a_queued_reply(client, chat):
    reply_id = client.post("/api/chat", json={"text": "hi"}, headers=bearer(ALICE_TOKEN)).json()["id"]
    assert client.delete("/api/chat", headers=bearer(ALICE_TOKEN)).status_code == 200
    worker._process_chat(store_of("alice"), reply_id)
    assert _messages(client) == []


def test_empty_and_oversized_messages_are_rejected(client, chat):
    assert client.post("/api/chat", json={"text": "  "}, headers=bearer(ALICE_TOKEN)).status_code == 400
    assert client.post("/api/chat", json={"text": "x" * 2001}, headers=bearer(ALICE_TOKEN)).status_code == 400
    assert chat == []
