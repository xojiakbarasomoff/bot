from collections.abc import Callable
from contextlib import AbstractContextManager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.embeddings import EMBEDDING_DIMENSIONS, EmbeddingProvider
from app.rag.llm import ChatMessage, LLMProvider
from app.repositories.knowledge_base import KnowledgeBaseRepository
from app.services.answer import NO_MATCH_RESPONSE, generate_answer
from app.services.guardrail import EMERGENCY_RESPONSE
from tests.conftest import Seed

# A fixed, non-zero direction. Distance to itself is 0.0, well inside the
# default 0.3 threshold, so any FAQ seeded with this embedding is a
# guaranteed match for a FakeEmbeddingProvider that returns it.
QUERY_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)


class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, vector: list[float]) -> None:
        self._vector = vector
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [self._vector for _ in texts]


class FakeLLMProvider(LLMProvider):
    def __init__(self, reply: str = "Sure, here's the answer.") -> None:
        self._reply = reply
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
        self.calls.append((system_prompt, messages))
        return self._reply


async def _make_faq(db_session: AsyncSession, question: str, answer: str) -> None:
    await KnowledgeBaseRepository(db_session).create(
        question=question, answer=answer, embedding=QUERY_VECTOR
    )


# --- normal flow: answer grounded in FAQ context ---


async def test_generate_answer_uses_faq_context_and_returns_llm_output(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider(reply="We're open 9 to 5, Monday to Saturday.")

    with as_tenant(seed.tenant_a.id):
        await _make_faq(db_session, "What are your hours?", "9 to 5, Mon-Sat.")

        result = await generate_answer(
            db_session,
            "What time do you open?",
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
        )

    assert result == "We're open 9 to 5, Monday to Saturday."
    assert embedding_provider.calls == [["What time do you open?"]]
    assert len(llm_provider.calls) == 1
    system_prompt, messages = llm_provider.calls[0]
    assert "Q: What are your hours?" in system_prompt
    assert "A: 9 to 5, Mon-Sat." in system_prompt
    assert messages == [{"role": "user", "content": "What time do you open?"}]


# --- empty retrieval: code-level short-circuit, LLM never called ---


async def test_generate_answer_returns_fixed_response_when_no_faq_matches(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    # No FAQ seeded with QUERY_VECTOR for this tenant, so search() finds
    # nothing within the default distance threshold (the `seed` fixture's
    # own placeholder FAQ has a zero-vector embedding, which is NaN distance
    # from any real query vector and gets filtered out).
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider()

    with as_tenant(seed.tenant_a.id):
        result = await generate_answer(
            db_session,
            "Do you offer teeth whitening?",
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
        )

    assert result == NO_MATCH_RESPONSE
    assert llm_provider.calls == []


# --- medical-advice: still goes through the LLM, with redirect framing enforced ---


async def test_generate_answer_medical_advice_message_gets_redirect_framing(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider(reply="Only a doctor can answer that at an appointment!")

    with as_tenant(seed.tenant_a.id):
        # Seeded so retrieval isn't empty — this test is about the
        # medical-advice reminder being added to the prompt, not about the
        # no-match short-circuit.
        await _make_faq(db_session, "What are your hours?", "9 to 5, Mon-Sat.")

        result = await generate_answer(
            db_session,
            "What antibiotic should I take for this?",
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
        )

    assert result == "Only a doctor can answer that at an appointment!"
    assert len(llm_provider.calls) == 1
    system_prompt, _messages = llm_provider.calls[0]
    assert "IMPORTANT: This message was flagged" in system_prompt


# --- emergency: fixed response, LLM (and embedding provider) never called ---


async def test_generate_answer_emergency_message_returns_fixed_response(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider()

    with as_tenant(seed.tenant_a.id):
        result = await generate_answer(
            db_session,
            "Severe pain and I can't stop bleeding, please help",
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
        )

    assert result == EMERGENCY_RESPONSE
    assert llm_provider.calls == []
    assert embedding_provider.calls == []


async def test_generate_answer_emergency_message_in_russian(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider()

    with as_tenant(seed.tenant_a.id):
        result = await generate_answer(
            db_session,
            "Не могу дышать, помогите!",
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
        )

    assert result == EMERGENCY_RESPONSE
    assert llm_provider.calls == []
    assert embedding_provider.calls == []


async def test_generate_answer_emergency_message_in_uzbek(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider()

    with as_tenant(seed.tenant_a.id):
        result = await generate_answer(
            db_session,
            "Qon to'xtamayapti, juda qo'rqinchli",
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
        )

    assert result == EMERGENCY_RESPONSE
    assert llm_provider.calls == []
    assert embedding_provider.calls == []
