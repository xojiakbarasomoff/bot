import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KnowledgeBase(Base):
    __tablename__ = "knowledge_base"
    __table_args__ = (
        # HNSW, not IVFFlat: better recall/speed for our scale (no training
        # step needed, degrades more gracefully as rows are added). cosine
        # ops to match KnowledgeBaseRepository.search()'s use of
        # Vector.cosine_distance() (the `<=>` operator) — an index built
        # with a different op class wouldn't be used by that query.
        Index(
            "ix_knowledge_base_embedding_hnsw_cosine",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
