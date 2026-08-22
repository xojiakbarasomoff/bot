from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Literal, TypedDict, cast

from google import genai
from google.genai import types as genai_types
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.core.config import Settings, get_settings


class ChatMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str


class LLMProvider(ABC):
    """Abstraction over "turn a system prompt + conversation into a reply",
    mirroring EmbeddingProvider so the backend/model can change without
    touching callers, and so tests can inject a fake instead of hitting the
    network.
    """

    @abstractmethod
    async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
        """Generate a reply given a system prompt and the conversation so far."""


class OpenAILLMProvider(LLMProvider):
    def __init__(self, settings: Settings | None = None, model: str = "gpt-4o-mini") -> None:
        api_key = (settings or get_settings()).openai_api_key
        if api_key is None:
            raise ValueError("OPENAI_API_KEY is required to use OpenAILLMProvider")
        self._model = model
        self._client = AsyncOpenAI(api_key=api_key)

    async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
        # ChatMessage is deliberately narrower than the SDK's message union
        # (only user/assistant; system is handled separately by this
        # abstraction), so it doesn't structurally unify with
        # ChatCompletionMessageParam under mypy strict. Both sides are
        # simple {role, content} dicts at runtime, so the cast is safe.
        payload = cast(
            "list[ChatCompletionMessageParam]",
            [{"role": "system", "content": system_prompt}, *messages],
        )
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=payload,
        )
        content = response.choices[0].message.content
        if content is None:
            raise ValueError("OpenAI chat completion returned no text content")
        return content


class GeminiLLMProvider(LLMProvider):
    # gemini-2.0-flash (originally proposed) is deprecated/no longer served.
    # gemini-2.5-flash verified live against the real API: responds
    # reliably. Pinned to a concrete version rather than an alias like
    # "gemini-flash-latest" (also verified working) — an alias can shift
    # which model actually runs without any change on our side, which is
    # bad for reproducing behavior/debugging later. The newer
    # gemini-3.7-flash was tried too and returned a 503 (overloaded) at
    # verification time — not reliable enough to default to.
    def __init__(self, settings: Settings | None = None, model: str = "gemini-2.5-flash") -> None:
        api_key = (settings or get_settings()).gemini_api_key
        if api_key is None:
            raise ValueError("GEMINI_API_KEY is required to use GeminiLLMProvider")
        self._model = model
        self._client = genai.Client(api_key=api_key)

    async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
        # Gemini's turn roles are "user"/"model", not "user"/"assistant".
        contents = [
            genai_types.Content(
                role="model" if message["role"] == "assistant" else "user",
                parts=[genai_types.Part(text=message["content"])],
            )
            for message in messages
        ]
        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=contents,
            config=genai_types.GenerateContentConfig(system_instruction=system_prompt),
        )
        if response.text is None:
            raise ValueError("Gemini generate_content returned no text content")
        return response.text


def _select_llm_provider(settings: Settings) -> LLMProvider:
    if settings.model_provider == "openai":
        return OpenAILLMProvider(settings)
    return GeminiLLMProvider(settings)


@lru_cache
def get_llm_provider() -> LLMProvider:
    return _select_llm_provider(get_settings())
