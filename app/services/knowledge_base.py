from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_base import KnowledgeBase
from app.rag.embeddings import EmbeddingProvider, get_embedding_provider
from app.repositories.knowledge_base import KnowledgeBaseRepository


class FAQImport(BaseModel):
    """One row of an FAQ import. Rejects missing/blank question or answer;
    category is optional since not every clinic buckets its FAQs.
    """

    question: str
    answer: str
    category: str | None = None

    @field_validator("question", "answer")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


async def ingest_faqs(
    session: AsyncSession,
    faqs: Sequence[FAQImport | dict[str, Any]],
    embedding_provider: EmbeddingProvider | None = None,
) -> list[KnowledgeBase]:
    """Load a clinic's FAQ list into knowledge_base, tenant-scoped via
    KnowledgeBaseRepository (which stamps tenant_id from the current
    request/task context — see set_current_tenant()).

    We embed the question only, not question+answer. Retrieval later matches
    an incoming user question against these vectors, so the embedding should
    capture question intent as precisely as possible; folding the answer text
    in would dilute the vector with wording the user's message will never
    contain, and would force a re-embed of the same question every time an
    answer is copyedited even though its retrieval target hasn't changed.

    Idempotent: a FAQ is treated as "the same" as an existing one when its
    question matches an existing row's question exactly (post-strip) for
    this tenant. On a match the existing row's answer/category/embedding are
    updated in place instead of inserting a duplicate. Exact match is a
    deliberate simplification for now — it won't catch a reworded question,
    which will insert a new row rather than update.

    All rows are validated before any embedding call is made, so a bad row
    fails fast without spending API calls on the valid ones ahead of it.
    """
    validated = [
        faq if isinstance(faq, FAQImport) else FAQImport.model_validate(faq) for faq in faqs
    ]
    if not validated:
        return []

    provider = embedding_provider or get_embedding_provider()
    embeddings = await provider.embed([faq.question for faq in validated])

    repo = KnowledgeBaseRepository(session)
    results: list[KnowledgeBase] = []
    for faq, embedding in zip(validated, embeddings, strict=True):
        existing = await repo.get_by_question(faq.question)
        if existing is not None:
            row = await repo.update(
                existing, answer=faq.answer, category=faq.category, embedding=embedding
            )
        else:
            row = await repo.create(
                question=faq.question,
                answer=faq.answer,
                category=faq.category,
                embedding=embedding,
            )
        results.append(row)
    return results
