import base64
import functools
import os

import openai
import pydantic

from . import REQUEST_TIMEOUT, AIError, Effort, Image, Part, Text, TModel


class OpenAIProvider:
    """OpenAI Chat Completions API; also serves any OpenAI-compatible endpoint (e.g. Gemini)."""

    def __init__(self, name: str, label: str, api_key_env: str, base_url: str | None, default_model: str):
        self.name = name
        self.label = label
        self.api_key_env = api_key_env
        self.base_url = base_url
        self.default_model = default_model

    @functools.cached_property
    def _client(self) -> openai.OpenAI:
        # Built on first use so the app starts (and non-AI pages work) without a key.
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise AIError(f"{self.api_key_env} is not set, fill it in .env")
        return openai.OpenAI(api_key=api_key, base_url=self.base_url, timeout=REQUEST_TIMEOUT)

    def structured(self, model: str, parts: list[Part], schema: type[TModel], effort: Effort) -> TModel:
        message = self._send(self._client.chat.completions.parse, model, parts, effort, response_format=schema)
        if message.parsed is None:
            raise AIError("model returned no structured output")
        return message.parsed

    def text(self, model: str, parts: list[Part], effort: Effort) -> str:
        message = self._send(self._client.chat.completions.create, model, parts, effort)
        return (message.content or "").strip()

    def _send(self, request, model: str, parts: list[Part], effort: Effort, **extra):
        try:
            response = request(
                model=model,
                reasoning_effort=effort,
                messages=[{"role": "user", "content": [_content(part) for part in parts]}],
                **extra,
            )
        except openai.LengthFinishReasonError as exc:
            raise AIError("model response was cut off at the token limit") from exc
        except openai.ContentFilterFinishReasonError as exc:
            raise AIError("model response was blocked by the content filter") from exc
        except openai.AuthenticationError as exc:
            raise AIError(f"{self.label} API key missing or invalid, check {self.api_key_env} in .env") from exc
        except openai.RateLimitError as exc:
            raise AIError(f"rate limited by the {self.label} API, retry later") from exc
        except openai.APIStatusError as exc:
            raise AIError(f"{self.label} API error {exc.status_code}: {exc.message}"[:500]) from exc
        except openai.APIConnectionError as exc:
            raise AIError(f"cannot reach the {self.label} API: {exc}") from exc
        except openai.OpenAIError as exc:
            raise AIError(f"{self.label} client error: {exc}"[:500]) from exc
        except pydantic.ValidationError as exc:
            # Compatible endpoints may not enforce the JSON schema strictly.
            raise AIError(f"model output did not match the expected schema: {exc}"[:500]) from exc

        choice = response.choices[0]
        if choice.message.refusal:
            raise AIError(f"model declined the request: {choice.message.refusal}"[:500])
        if choice.finish_reason == "length":
            raise AIError("model response was cut off at the token limit")
        return choice.message


def _content(part: Part) -> dict:
    if isinstance(part, Text):
        return {"type": "text", "text": part.text}
    if isinstance(part, Image):
        data = base64.standard_b64encode(part.data).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{part.media_type};base64,{data}"}}
    raise TypeError(f"unsupported part: {part!r}")
