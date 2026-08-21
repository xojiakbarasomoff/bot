import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.faq_seeding import (
    FaqSeedingError,
    load_faqs,
    resolve_faq_tenant_id,
    seed_faqs_if_configured,
)
from tests.conftest import Seed


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "faqs.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# --- load_faqs: everything is validated before an embedding is ever paid for ---


def test_load_faqs_reads_questions_answers_and_categories(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [
            {"question": "Manzilingiz qayerda?", "answer": "Yunusobod", "category": "umumiy"},
            {"question": "Narxlar?", "answer": "100 000 so'm"},
        ],
    )

    faqs = load_faqs(path)

    assert [faq.question for faq in faqs] == ["Manzilingiz qayerda?", "Narxlar?"]
    assert faqs[0].category == "umumiy"
    assert faqs[1].category is None


def test_load_faqs_rejects_a_blank_answer(tmp_path: Path) -> None:
    """A blank answer would be embedded, stored, and then retrieved as
    grounding that says nothing -- worse than the FAQ simply being absent,
    because retrieval reports a hit.
    """
    path = _write(tmp_path, [{"question": "Manzilingiz qayerda?", "answer": "   "}])

    with pytest.raises(FaqSeedingError, match="Invalid FAQ entry"):
        load_faqs(path)


def test_load_faqs_rejects_a_json_object(tmp_path: Path) -> None:
    path = _write(tmp_path, {"question": "Manzilingiz qayerda?", "answer": "Yunusobod"})

    with pytest.raises(FaqSeedingError, match="must contain a JSON array"):
        load_faqs(path)


def test_load_faqs_reports_malformed_json_rather_than_raising_json_error(tmp_path: Path) -> None:
    path = tmp_path / "faqs.json"
    path.write_text('[{"question": "x",}]', encoding="utf-8")

    with pytest.raises(FaqSeedingError, match="not valid JSON"):
        load_faqs(path)


def test_load_faqs_reports_a_missing_file_by_name(tmp_path: Path) -> None:
    with pytest.raises(FaqSeedingError, match="No such file"):
        load_faqs(tmp_path / "absent.json")


# --- tenant resolution: through the channel, never a raw UUID ---


async def test_resolve_tenant_id_finds_the_tenant_behind_a_channel(
    db_session: AsyncSession, seed: Seed
) -> None:
    tenant_id = await resolve_faq_tenant_id(db_session, seed.a.channel.external_id)

    assert tenant_id == seed.tenant_a.id


async def test_resolve_tenant_id_refuses_to_guess_between_two_channels(
    db_session: AsyncSession, seed: Seed
) -> None:
    """Two clinics share this database. Picking either one silently would
    load one clinic's prices into the other's knowledge base.
    """
    with pytest.raises(FaqSeedingError, match="channels exist"):
        await resolve_faq_tenant_id(db_session, None)


async def test_resolve_tenant_id_rejects_an_unknown_account_id(
    db_session: AsyncSession, seed: Seed
) -> None:
    with pytest.raises(FaqSeedingError, match="No instagram channel found"):
        await resolve_faq_tenant_id(db_session, "ig-does-not-exist")


# --- the startup hook itself ---


def _settings(**overrides: object) -> Settings:
    base = {
        "database_url": "postgresql+asyncpg://test:test@localhost/test",
        "redis_url": "redis://localhost:6379/0",
        "webhook_verify_token": "test-verify-token",
        "meta_app_secret": "test-app-secret",
        "encryption_key": "Hq3_REB-V0twf7iBgCPCSUZQiG44egxyiZg9kOKRxUg=",
        "gemini_api_key": "test-gemini-key",
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


async def test_seeding_is_skipped_when_unconfigured() -> None:
    """Unset is the resting state, so this runs on every startup of every
    deployment that has already been seeded. It must not touch the database
    at all -- note there is no db_session fixture here, so a stray connection
    attempt would fail the test.
    """
    await seed_faqs_if_configured(_settings(seed_faqs_from=None))


async def test_a_broken_faq_file_does_not_stop_the_app_from_starting(
    tmp_path: Path,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """The webhook being up matters more than the knowledge base being
    loaded: the assistant can answer without FAQs, but a web process that
    refuses to boot answers nothing at all.
    """
    path = _write(tmp_path, [{"question": "Manzilingiz qayerda?", "answer": ""}])

    await seed_faqs_if_configured(_settings(seed_faqs_from=str(path)))


async def test_a_missing_faq_file_does_not_stop_the_app_from_starting(
    tmp_path: Path,
) -> None:
    await seed_faqs_if_configured(_settings(seed_faqs_from=str(tmp_path / "absent.json")))
