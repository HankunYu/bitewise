import base64
from types import SimpleNamespace

import pytest
from openai.lib._pydantic import to_strict_json_schema

from bitewise import ai, config, llm
from bitewise.db import Store
from bitewise.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from bitewise.llm.openai_provider import OpenAIProvider

TARGETS = ai.NutrientTargets(protein_g=1, carbs_g=2, fat_g=3, fiber_g=4, sugar_g=5, sodium_mg=6)


@pytest.fixture
def fresh_provider():
    llm.provider.cache_clear()
    yield
    llm.provider.cache_clear()


@pytest.fixture
def store(tmp_path):
    store = Store("tester", tmp_path)
    store.init()
    return store


class FakeProvider:
    name = "fake"
    default_model = "fake-default"

    def __init__(self, result):
        self.result = result
        self.calls = []

    def structured(self, model, parts, schema, effort):
        self.calls.append((model, parts, schema, effort))
        return self.result

    def text(self, model, parts, effort):
        self.calls.append((model, parts, None, effort))
        return self.result

    def chat(self, model, system, turns, effort):
        self.calls.append((model, system, turns, effort))
        return self.result


# ---------- provider selection ----------

@pytest.mark.parametrize("name, default_model, base_url", [
    ("anthropic", "claude-opus-5-5", None),
    ("openai", "gpt-5.5", None),
    ("google", "gemini-3.8-flash", "https://generativelanguage.googleapis.com/v1beta/openai/"),
])
def test_provider_selection(fresh_provider, monkeypatch, name, default_model, base_url):
    monkeypatch.setattr(config, "PROVIDER", name)
    provider = llm.provider()
    assert provider.name == name
    assert provider.default_model == default_model
    if base_url:
        assert provider.base_url == base_url


def test_unknown_provider_is_rejected(fresh_provider, monkeypatch):
    monkeypatch.setattr(config, "PROVIDER", "mistral")
    with pytest.raises(llm.AIError, match="BITEWISE_PROVIDER"):
        llm.provider()


def test_missing_key_names_the_env_variable(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = OpenAIProvider("google", "Google", "GEMINI_API_KEY", None, "m")
    with pytest.raises(llm.AIError, match="GEMINI_API_KEY"):
        provider.text("m", [llm.Text("hi")], "low")


# ---------- ai.py stays provider-neutral ----------

def test_empty_model_setting_uses_provider_default(store, monkeypatch):
    fake = FakeProvider(TARGETS)
    monkeypatch.setattr(llm, "provider", lambda: fake)
    ai.nutrient_targets(store)
    store.set_settings({"model": "my-model"})
    ai.nutrient_targets(store)
    assert [call[0] for call in fake.calls] == ["fake-default", "my-model"]
    assert all(isinstance(part, llm.Text) for part in fake.calls[0][1])


def test_meal_photo_is_sent_as_image_part(store, monkeypatch):
    (store.upload_dir / "meal.jpeg").write_bytes(b"jpeg-bytes")
    estimate = ai.MealEstimate(
        description="d", items=[], kcal=1, protein_g=1, carbs_g=1, fat_g=1,
        fiber_g=1, sugar_g=1, sodium_mg=1, comment="c")
    fake = FakeProvider(estimate)
    monkeypatch.setattr(llm, "provider", lambda: fake)
    ai.estimate_meal(store, "uploads/meal.jpeg", "")
    image = fake.calls[0][1][0]
    assert image == llm.Image("image/jpeg", b"jpeg-bytes")


def test_chat_reply_sends_user_data_and_alternating_turns(store, monkeypatch):
    store.set_settings({"goal_prompt": "减脂", "daily_kcal_target": "1800"})
    meal_id = store.insert_meal(store.now_iso(), "", None)
    store.update_meal(meal_id, {"description": "牛肉面", "kcal": 650.0, "status": "done"})
    first = store.insert_chat_exchange("今天还能吃什么?")
    store.update_chat_message(first, {"status": "failed", "error": "boom"})
    # The failed reply is skipped, leaving two questions in a row to merge.
    second = store.insert_chat_exchange("晚饭呢?")
    fake = FakeProvider("")
    monkeypatch.setattr(llm, "provider", lambda: fake)
    with pytest.raises(llm.AIError, match="empty reply"):
        ai.chat_reply(store, second)
    _, system, turns, effort = fake.calls[0]
    assert "减脂" in system and "1800" in system and "牛肉面" in system
    assert turns == [llm.Turn("user", "今天还能吃什么?\n\n晚饭呢?")]
    assert effort == ai.CHAT_EFFORT


def test_chat_turns_start_with_the_user():
    history = [
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    assert ai._chat_turns(history) == [
        llm.Turn("user", "q1"), llm.Turn("assistant", "a1"), llm.Turn("user", "q2")]


@pytest.mark.parametrize("schema", [ai.MealEstimate, ai.TextAction, ai.FoodCheck, ai.NutrientTargets])
def test_schemas_convert_to_strict_json_schema(schema):
    # OpenAI-compatible structured outputs need every schema in strict form.
    to_strict_json_schema(schema)


# ---------- OpenAI-compatible provider ----------

def _openai_with_fake_client(parsed=None, refusal=None, finish_reason="stop", content=None):
    requests = []

    def respond(**kwargs):
        requests.append(kwargs)
        message = SimpleNamespace(parsed=parsed, refusal=refusal, content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])

    provider = OpenAIProvider("openai", "OpenAI", "OPENAI_API_KEY", None, "gpt-5.5")
    completions = SimpleNamespace(parse=respond, create=respond)
    provider.__dict__["_client"] = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return provider, requests


def test_openai_request_shape():
    provider, requests = _openai_with_fake_client(parsed=TARGETS)
    parts = [llm.Image("image/png", b"png"), llm.Text("hello")]
    assert provider.structured("gpt-x", parts, ai.NutrientTargets, "low") is TARGETS
    request = requests[0]
    assert request["model"] == "gpt-x"
    assert request["reasoning_effort"] == "low"
    assert request["response_format"] is ai.NutrientTargets
    image, text = request["messages"][0]["content"]
    data_url = "data:image/png;base64," + base64.b64encode(b"png").decode()
    assert image == {"type": "image_url", "image_url": {"url": data_url}}
    assert text == {"type": "text", "text": "hello"}


def test_openai_text_and_failures():
    provider, _ = _openai_with_fake_client(content="  advice  ")
    assert provider.text("m", [llm.Text("x")], "medium") == "advice"
    provider, _ = _openai_with_fake_client(refusal="no")
    with pytest.raises(llm.AIError, match="declined"):
        provider.structured("m", [llm.Text("x")], ai.NutrientTargets, "low")
    provider, _ = _openai_with_fake_client(content="partial", finish_reason="length")
    with pytest.raises(llm.AIError, match="cut off"):
        provider.text("m", [llm.Text("x")], "low")


def test_openai_chat_request_shape():
    provider, requests = _openai_with_fake_client(content=" reply ")
    turns = [llm.Turn("user", "q1"), llm.Turn("assistant", "a1"), llm.Turn("user", "q2")]
    assert provider.chat("gpt-x", "sys", turns, "low") == "reply"
    assert requests[0]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]


# ---------- Anthropic provider ----------

def _anthropic_with_fake_client(stop_reason="end_turn", parsed=None, content=()):
    requests = []

    def respond(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(stop_reason=stop_reason, stop_details=None, parsed_output=parsed, content=list(content))

    provider = AnthropicProvider()
    messages = SimpleNamespace(parse=respond, create=respond)
    provider.__dict__["_client"] = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return provider, requests


def test_anthropic_request_shape():
    provider, requests = _anthropic_with_fake_client(parsed=TARGETS)
    parts = [llm.Image("image/jpeg", b"jpg"), llm.Text("hello")]
    assert provider.structured("claude-x", parts, ai.NutrientTargets, "medium") is TARGETS
    request = requests[0]
    assert request["model"] == "claude-x"
    assert request["output_config"] == {"effort": "medium"}
    assert request["betas"] == [FALLBACK_BETA]
    assert request["output_format"] is ai.NutrientTargets
    image, text = request["messages"][0]["content"]
    data = base64.b64encode(b"jpg").decode()
    assert image["source"] == {"type": "base64", "media_type": "image/jpeg", "data": data}
    assert text == {"type": "text", "text": "hello"}


def test_anthropic_refusal_is_an_error():
    provider, _ = _anthropic_with_fake_client(stop_reason="refusal")
    with pytest.raises(llm.AIError, match="declined"):
        provider.text("m", [llm.Text("x")], "low")


def test_anthropic_chat_request_shape():
    reply = [SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=" reply ")]
    provider, requests = _anthropic_with_fake_client(content=reply)
    turns = [llm.Turn("user", "q1"), llm.Turn("assistant", "a1"), llm.Turn("user", "q2")]
    assert provider.chat("claude-x", "sys", turns, "low") == "reply"
    request = requests[0]
    assert request["system"] == "sys"
    assert request["output_config"] == {"effort": "low"}
    assert request["messages"] == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
