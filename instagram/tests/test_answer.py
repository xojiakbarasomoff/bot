from collections.abc import Callable
from contextlib import AbstractContextManager
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.rag.embeddings import EMBEDDING_DIMENSIONS, EmbeddingProvider
from app.rag.llm import ChatMessage, LLMProvider
from app.repositories.knowledge_base import KnowledgeBaseRepository
from app.services.answer import NO_MATCH_RESPONSE, generate_answer
from app.services.guardrail import EMERGENCY_RESPONSE
from tests.conftest import Seed, isolated_settings

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


def _settings(**overrides: object) -> Settings:
    return isolated_settings(**overrides)


async def test_no_faq_match_asks_the_llm_when_answering_without_faq_is_enabled(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """ANSWER_WITHOUT_FAQ is what a deployment turns on before its knowledge
    base is populated, so that patients get a real reply instead of the same
    refusal to every message.
    """
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider()

    with as_tenant(seed.tenant_a.id):
        result = await generate_answer(
            db_session,
            "Do you offer teeth whitening?",
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            settings=_settings(answer_without_faq=True),
        )

    assert result != NO_MATCH_RESPONSE
    assert len(llm_provider.calls) == 1


async def test_answering_without_faq_still_forbids_clinic_details_and_medical_advice(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """The relaxed path drops "answer only from the FAQ" and nothing else:
    an assistant free to improvise opening hours or medication is the
    failure this setting must not introduce.
    """
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider()

    with as_tenant(seed.tenant_a.id):
        await generate_answer(
            db_session,
            "Do you offer teeth whitening?",
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            settings=_settings(answer_without_faq=True),
        )

    system_prompt = llm_provider.calls[0][0]
    assert "opening hours, prices" in system_prompt
    assert "Never state or guess any of" in system_prompt
    assert "You are not a medical professional" in system_prompt
    assert "Never claim or imply that you are a doctor" in system_prompt


async def test_disabled_by_default_so_an_unset_variable_cannot_relax_it(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider()

    with as_tenant(seed.tenant_a.id):
        result = await generate_answer(
            db_session,
            "Do you offer teeth whitening?",
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            settings=_settings(),
        )

    assert result == NO_MATCH_RESPONSE
    assert llm_provider.calls == []


async def test_default_reply_language_reaches_the_prompt(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """Patients open with "Salom", "Alik", "Nmagap" -- too short and too
    transliterated for the model to place, so it answers in English and the
    clinic looks like it is replying in the wrong language. The configured
    language is what it falls back to instead.
    """
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider()

    with as_tenant(seed.tenant_a.id):
        await _make_faq(db_session, "What are your hours?", "9 to 5, Mon-Sat.")
        await generate_answer(
            db_session,
            "Nmagap",
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            settings=_settings(default_reply_language="Uzbek"),
        )

    system_prompt = llm_provider.calls[0][0]
    assert "reply in Uzbek" in system_prompt
    # The instruction to mirror the patient still comes first: the fallback
    # applies only when the language is unclear, it does not override a
    # message that plainly is in another language.
    assert system_prompt.index("same language the patient wrote in") < system_prompt.index(
        "reply in Uzbek"
    )


async def test_default_language_also_applies_without_a_faq_match(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    # The no-FAQ path is the one a not-yet-populated deployment actually
    # runs, so the language fallback has to be in that prompt too.
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider()

    with as_tenant(seed.tenant_a.id):
        await generate_answer(
            db_session,
            "Nmagap",
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            settings=_settings(answer_without_faq=True, default_reply_language="Uzbek"),
        )

    assert "reply in Uzbek" in llm_provider.calls[0][0]


# --- how the reply must sound: alphabet, greeting, and the call-centre ask ---


async def _capture_system_prompt(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    *,
    with_faq: bool,
    **settings_overrides: object,
) -> str:
    """Run generate_answer far enough to grab the system prompt it built.

    Every rule below is checked on both paths on purpose: a clinic whose
    knowledge base is not populated yet runs the no-FAQ prompt for every
    single message, so a rule that only made it into the FAQ prompt would be
    missing exactly where nobody would think to look for it.
    """
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider()

    with as_tenant(seed.tenant_a.id):
        if with_faq:
            await _make_faq(db_session, "What are your hours?", "9 to 5, Mon-Sat.")
        await generate_answer(
            db_session,
            "Assalom alaykum",
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            settings=_settings(answer_without_faq=not with_faq, **settings_overrides),
        )

    return llm_provider.calls[0][0]


@pytest.mark.parametrize("with_faq", [True, False], ids=["with_faq", "without_faq"])
async def test_prompt_requires_replying_in_the_alphabet_the_patient_used(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    with_faq: bool,
) -> None:
    """Uzbek is written in both Latin and Cyrillic, and "same language" alone
    does not settle which one to answer in -- a Cyrillic patient answered in
    Latin has been replied to in their language and still cannot comfortably
    read it.
    """
    system_prompt = await _capture_system_prompt(db_session, seed, as_tenant, with_faq=with_faq)

    assert "same alphabet they" in system_prompt
    assert "answer a Cyrillic message in Cyrillic and a Latin message in Latin" in system_prompt
    assert "Never transliterate a patient into the" in system_prompt


@pytest.mark.parametrize("with_faq", [True, False], ids=["with_faq", "without_faq"])
async def test_prompt_carries_the_expected_greeting_for_each_language(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    with_faq: bool,
) -> None:
    """The greeting "Assalom alaykum" has one correct answer, and a model left
    to its own devices returns "Salom" or the English "Hello" instead -- which
    reads, to the patient, as a clinic that did not greet them back.
    """
    system_prompt = await _capture_system_prompt(db_session, seed, as_tenant, with_faq=with_faq)

    assert "Va alaykum assalom" in system_prompt
    assert "Ва алайкум ассалом" in system_prompt
    assert "Здравствуйте" in system_prompt


@pytest.mark.parametrize("with_faq", [True, False], ids=["with_faq", "without_faq"])
async def test_prompt_asks_for_a_phone_number_for_the_call_centre(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    with_faq: bool,
) -> None:
    """The number is the whole point of the conversation for the clinic: a
    call-centre colleague picks it up from here. Nothing in this pipeline
    persists it yet, so the prompt is currently the only thing that gets it
    asked for at all.
    """
    system_prompt = await _capture_system_prompt(db_session, seed, as_tenant, with_faq=with_faq)

    assert "asking for" in system_prompt
    assert "phone number so a colleague can call them back" in system_prompt
    # Asking must not displace the answer, or the bot reads as a lead-capture
    # form that ignores what the patient came to ask.
    assert "never let it crowd out the answer" in system_prompt


async def test_no_match_response_asks_for_a_phone_number(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """This path never reaches the LLM, so rule 4 in the prompt cannot apply
    to it -- and it is the case that needs a callback most, since the clinic
    has just failed to answer the patient at all.
    """
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider()

    with as_tenant(seed.tenant_a.id):
        result = await generate_answer(
            db_session,
            "Do you offer teeth whitening?",
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            settings=_settings(),
        )

    assert result == NO_MATCH_RESPONSE
    assert llm_provider.calls == []
    assert "phone number" in NO_MATCH_RESPONSE


# --- what the clinic does and does not offer, and what a price costs ---


async def test_faq_prompt_answers_service_questions_instead_of_deflecting(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """A question like "do you do implants?" is a yes/no question. Answering
    it with a booking offer reads as evasion, and the patient asks a
    competitor instead.
    """
    system_prompt = await _capture_system_prompt(db_session, seed, as_tenant, with_faq=True)

    assert "say plainly that yes, it is available" in system_prompt
    assert "Afsuski, bizda bunaqa xizmat hozircha yo'q" in system_prompt


async def test_faq_prompt_will_not_call_a_service_unavailable_on_silence(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """Retrieval returns the nearest FAQ entries, not the clinic's full
    service list, so a treatment missing from the context is unknown rather
    than absent. Announcing "we don't do that" from silence turns away a
    patient for a treatment the clinic may well perform.
    """
    system_prompt = await _capture_system_prompt(db_session, seed, as_tenant, with_faq=True)

    assert "does not mention the treatment either way, you do not know" in system_prompt
    assert "so do not say it is unavailable" in system_prompt


async def test_no_faq_prompt_claims_nothing_about_services_in_either_direction(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """With no knowledge base at all, every service question is the unknown
    case -- so this path must never confirm a treatment either.
    """
    system_prompt = await _capture_system_prompt(db_session, seed, as_tenant, with_faq=False)

    assert "never tell a patient that it does, and never tell" in system_prompt
    assert "Afsuski, bizda bunaqa xizmat hozircha yo'q" not in system_prompt


@pytest.mark.parametrize("with_faq", [True, False], ids=["with_faq", "without_faq"])
async def test_prompt_forbids_inventing_somewhere_else_to_go(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    with_faq: bool,
) -> None:
    """The no-FAQ path is the dangerous one here: it is explicitly allowed to
    use general knowledge, which is exactly what would produce a confident,
    fictional referral to a clinic across town.
    """
    system_prompt = await _capture_system_prompt(db_session, seed, as_tenant, with_faq=with_faq)

    assert "must NOT name another clinic" in system_prompt
    assert "Afsuski, bizda bunday ma'lumot yo'q" in system_prompt


@pytest.mark.parametrize("with_faq", [True, False], ids=["with_faq", "without_faq"])
async def test_configured_clinic_numbers_are_quoted_in_the_pricing_fallback(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    with_faq: bool,
) -> None:
    system_prompt = await _capture_system_prompt(
        db_session,
        seed,
        as_tenant,
        with_faq=with_faq,
        clinic_phone_numbers="+998 90 123 45 67",
    )

    assert "+998 90 123 45 67" in system_prompt
    assert "ushbu telefon raqamlariga qo'ng'iroq qiling" in system_prompt
    # The callback offer is not an either/or with the numbers: a patient who
    # is writing rather than calling is the one this whole rule exists for.
    assert "qachon gaplashish siz uchun qulay bo'lgan" in system_prompt


@pytest.mark.parametrize("with_faq", [True, False], ids=["with_faq", "without_faq"])
async def test_pricing_fallback_invents_no_number_when_none_is_configured(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    with_faq: bool,
) -> None:
    """CLINIC_PHONE_NUMBERS is unset by default, and a model asked to tell
    patients to "call these numbers" with no numbers given will produce a
    plausible +998 one. The prompt must drop that half of the offer instead.
    """
    system_prompt = await _capture_system_prompt(db_session, seed, as_tenant, with_faq=with_faq)

    assert "ushbu telefon raqamlariga qo'ng'iroq qiling" not in system_prompt
    assert "You have NOT been given a phone number" in system_prompt
    assert "never invent one" in system_prompt
    # The callback half survives -- it is the part that works without numbers.
    assert "qachon gaplashish siz uchun qulay bo'lgan" in system_prompt


@pytest.mark.parametrize("with_faq", [True, False], ids=["with_faq", "without_faq"])
async def test_prompt_forbids_dodging_a_price_with_an_estimate(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    with_faq: bool,
) -> None:
    """A quoted range the clinic never agreed to is worse than no answer: the
    patient arrives expecting it.
    """
    system_prompt = await _capture_system_prompt(db_session, seed, as_tenant, with_faq=with_faq)

    assert "do not give a range" in system_prompt
    assert "do not say a doctor will decide" in system_prompt


# --- clinic details that come from configuration, not the knowledge base ---

CLINIC_ADDRESS = "Toshkent, Yunusobod, Moyqo'rg'on 11A"
CLINIC_PHONES = "+998336677788"


@pytest.mark.parametrize("with_faq", [True, False], ids=["with_faq", "without_faq"])
async def test_configured_address_is_given_to_the_model_as_fact(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    with_faq: bool,
) -> None:
    """Both prompts otherwise forbid stating an address, which is right when
    retrieval is the only source. An operator-typed one is a different kind of
    fact, and a clinic with an empty knowledge base still has to be able to
    answer "qayerdasiz?".
    """
    system_prompt = await _capture_system_prompt(
        db_session,
        seed,
        as_tenant,
        with_faq=with_faq,
        clinic_address=CLINIC_ADDRESS,
        clinic_phone_numbers=CLINIC_PHONES,
    )

    assert f"Address: {CLINIC_ADDRESS}" in system_prompt
    assert f"Phone: {CLINIC_PHONES}" in system_prompt
    assert "never altered or added to" in system_prompt


@pytest.mark.parametrize("with_faq", [True, False], ids=["with_faq", "without_faq"])
async def test_configured_details_do_not_unlock_the_rest_of_the_clinic(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    with_faq: bool,
) -> None:
    """Being handed an address is not evidence of knowing the opening hours.
    The exemption has to stay scoped to exactly what was configured, or it
    becomes a licence to improvise every other clinic detail.
    """
    system_prompt = await _capture_system_prompt(
        db_session,
        seed,
        as_tenant,
        with_faq=with_faq,
        clinic_address=CLINIC_ADDRESS,
    )

    assert "the only details of this clinic you have been given" in system_prompt
    assert "Do not treat anything else about it as known" in system_prompt


@pytest.mark.parametrize("with_faq", [True, False], ids=["with_faq", "without_faq"])
async def test_no_facts_section_appears_when_nothing_is_configured(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    with_faq: bool,
) -> None:
    """An empty deployment must not be told it has details it does not have --
    an empty "Address:" line is an invitation to fill it in.
    """
    system_prompt = await _capture_system_prompt(db_session, seed, as_tenant, with_faq=with_faq)

    assert "These clinic details are given to you as fact" not in system_prompt
    assert "Address:" not in system_prompt


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
