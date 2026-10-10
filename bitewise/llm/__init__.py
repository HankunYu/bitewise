"""Provider-neutral model interface used by ai.py.

ai.py builds prompts from Text/Image parts and asks the configured provider
for either structured output (a pydantic model) or plain text. Each provider
translates the parts into its own API format.
"""

import functools
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

from .. import config

REQUEST_TIMEOUT = 300.0
PROVIDER_NAMES = ("anthropic", "openai", "google")

Effort = Literal["low", "medium"]
TModel = TypeVar("TModel", bound=BaseModel)


class AIError(Exception):
    pass


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Image:
    media_type: str
    data: bytes


Part = Text | Image


class Provider(Protocol):
    name: str
    default_model: str

    def structured(self, model: str, parts: list[Part], schema: type[TModel], effort: Effort) -> TModel:
        """One user turn answered as an instance of schema."""
        ...

    def text(self, model: str, parts: list[Part], effort: Effort) -> str:
        """One user turn answered as plain text."""
        ...


@functools.cache
def provider() -> Provider:
    """The provider selected by BITEWISE_PROVIDER; raises AIError if unknown."""
    name = config.PROVIDER
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    if name == "openai":
        from .openai_provider import OpenAIProvider
        return OpenAIProvider(
            name="openai", label="OpenAI", api_key_env="OPENAI_API_KEY",
            base_url=None, default_model="gpt-5.5",
        )
    if name == "google":
        from .openai_provider import OpenAIProvider
        # Gemini through Google's OpenAI-compatible endpoint.
        return OpenAIProvider(
            name="google", label="Google", api_key_env="GEMINI_API_KEY",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            default_model="gemini-3.8-flash",
        )
    raise AIError(f"unknown BITEWISE_PROVIDER {name!r}, use one of: {', '.join(PROVIDER_NAMES)}")
