from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.embeddings import EmbeddingProvider, get_embedding_provider
from app.repositories.knowledge_base import KnowledgeBaseMatch, KnowledgeBaseRepository

# Cosine distance below which a match is considered worth showing. This is a
# starting heuristic, not a measured cutoff: for text-embedding-3-small,
# near-duplicate questions typically land well under ~0.15 distance, while
# genuinely unrelated FAQ pairs tend to sit well above ~0.4 (this model's
# embeddings aren't zero-mean, so "unrelated" isn't near the 1.0 you'd expect
# from uniformly random vectors). 0.3 sits in between, erring toward keeping
# a borderline match rather than silently dropping a real one — revisit once
# we have real query traffic to check it against.
DEFAULT_MAX_DISTANCE = 0.3


async def retrieve_relevant_faqs(
    session: AsyncSession,
    query_text: str,
    embedding_provider: EmbeddingProvider | None = None,
    limit: int = 5,
    max_distance: float | None = DEFAULT_MAX_DISTANCE,
) -> list[KnowledgeBaseMatch]:
    """Embed query_text and return the current tenant's top matching FAQs,
    closest first. This is what the message pipeline will call to ground a
    reply — it does not generate an answer itself.

    max_distance drops matches whose cosine distance exceeds it (lower
    distance = more similar); pass None to skip filtering and always return
    up to `limit` matches regardless of how weak they are.
    """
    provider = embedding_provider or get_embedding_provider()
    [query_embedding] = await provider.embed([query_text])

    repo = KnowledgeBaseRepository(session)
    matches = await repo.search(query_embedding, limit=limit)

    if max_distance is None:
        return matches
    return [match for match in matches if match.distance <= max_distance]
