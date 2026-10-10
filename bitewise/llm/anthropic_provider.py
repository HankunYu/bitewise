import base64
import functools
import os

import anthropic

from . import REQUEST_TIMEOUT, AIError, Effort, Image, Part, Text, TModel

MAX_TOKENS = 16000
# Server-side refusal fallback: a classifier-declined request is re-run on the
# model Anthropic recommends for that refusal category, inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"
    default_model = "claude-opus-5-5"

    @functools.cached_property
    def _client(self) -> anthropic.Anthropic:
        # Built on first use so the app starts (and non-AI pages work) without a key.
        # The client is thread-safe and shared by all worker lanes.
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise AIError("ANTHROPIC_API_KEY is not set, fill it in .env")
        return anthropic.Anthropic(timeout=REQUEST_TIMEOUT)

    def structured(self, model: str, parts: list[Part], schema: type[TModel], effort: Effort) -> TModel:
        response = self._send(
            lambda params: self._client.beta.messages.parse(**params, output_format=schema),
            model, parts, effort,
        )
        if response.parsed_output is None:
            raise AIError("model returned no structured output")
        return response.parsed_output

    def text(self, model: str, parts: list[Part], effort: Effort) -> str:
        response = self._send(lambda params: self._client.beta.messages.create(**params), model, parts, effort)
        return "".join(block.text for block in response.content if block.type == "text").strip()

    def _send(self, request, model: str, parts: list[Part], effort: Effort):
        params = dict(
            model=model,
            max_tokens=MAX_TOKENS,
            output_config={"effort": effort},
            betas=[FALLBACK_BETA],
            fallbacks="default",
            messages=[{"role": "user", "content": [_block(part) for part in parts]}],
        )
        try:
            response = request(params)
        except anthropic.AuthenticationError as exc:
            raise AIError("Anthropic API key missing or invalid, check ANTHROPIC_API_KEY in .env") from exc
        except anthropic.RateLimitError as exc:
            raise AIError("rate limited by the Anthropic API, retry later") from exc
        except anthropic.APIStatusError as exc:
            raise AIError(f"Anthropic API error {exc.status_code}: {exc.message}"[:500]) from exc
        except anthropic.APIConnectionError as exc:
            raise AIError(f"cannot reach the Anthropic API: {exc}") from exc
        except anthropic.AnthropicError as exc:
            # e.g. no credentials configured at all
            raise AIError(f"Anthropic client error: {exc}"[:500]) from exc

        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise AIError(f"model declined the request (category: {category})")
        if response.stop_reason == "max_tokens":
            raise AIError("model response was cut off at max_tokens")
        return response


def _block(part: Part) -> dict:
    if isinstance(part, Text):
        return {"type": "text", "text": part.text}
    if isinstance(part, Image):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": part.media_type,
                "data": base64.standard_b64encode(part.data).decode("ascii"),
            },
        }
    raise TypeError(f"unsupported part: {part!r}")
