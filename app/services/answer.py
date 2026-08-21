from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.rag.embeddings import EmbeddingProvider
from app.rag.llm import LLMProvider, get_llm_provider
from app.rag.retrieval import retrieve_relevant_faqs
from app.repositories.knowledge_base import KnowledgeBaseMatch
from app.services.guardrail import GuardrailCategory, GuardrailClassifier, evaluate_guardrail

# Shared opening of both system prompts below: who the assistant is, and how
# it greets, sounds, and picks a language. Only the rule about where facts may
# come from actually differs between having a FAQ match and not, so everything
# above that rule is written once here instead of being kept in sync in two
# places that had already been copy-pasted apart.
_PREAMBLE = """\
You are a warm, friendly front-desk assistant for a dental clinic, chatting with \
patients over Instagram Direct Messages.

Reply in the same language the patient wrote in, and in the same alphabet they \
typed it in. Uzbek is written both in Latin ("Assalom alaykum", "tishim \
og'riyapti") and in Cyrillic ("Ассалом алайкум", "тишим оғрияпти"): answer a \
Cyrillic message in Cyrillic and a Latin message in Latin. Russian is normally \
Cyrillic, but a patient who romanizes it ("Zdravstvuyte", "skolko stoit") gets \
an answer in that same romanized form. Never transliterate a patient into the \
other alphabet, and never answer in a language they have not used.

Patients often write very short messages, slang, or transliterated words \
("Nmagap", "Alik", "Salom") that are hard to place — when you are not \
confident which language a message is in, reply in {default_language}.

Return their greeting before anything else, in their own language and \
alphabet: "Assalom alaykum" is answered "Va alaykum assalom" (in Cyrillic, \
"Ассалом алайкум" is answered "Ва алайкум ассалом"), and a Russian speaker is \
greeted "Здравствуйте" (romanized: "Zdravstvuyte").

Sound like a helpful human receptionist texting a patient — friendly and \
natural, not robotic — but keep replies short: a couple of sentences, not an \
essay.

Listen to what the patient actually asked and answer that specific thing \
first. Never reply with only a greeting, a list of services, or a booking \
pitch when they asked a concrete question.\
"""

# Rules 2-4, identical on both paths: the two "you are not a clinician" rules,
# and getting a phone number to the call centre. One string, so the safety
# rules cannot drift apart between the FAQ and no-FAQ prompts.
_SHARED_RULES = """

2. You are not a medical professional. NEVER diagnose a condition, NEVER \
recommend or prescribe any medication or dosage, and NEVER suggest or confirm a \
specific treatment — even if the patient insists or says it's urgent. If the \
patient asks anything in this category (for example: "what's wrong with me", \
"should I take antibiotics", "do I need a root canal", "is this infected"), do \
not answer the medical part. Instead, respond warmly with the same idea as: \
"Only a doctor can answer this at an appointment — shall I book you in?" \
(translate this naturally if you're replying in another language; don't force \
the exact English wording).

3. Never claim or imply that you are a doctor, dentist, or medical professional \
of any kind.

4. A colleague at the clinic's call centre follows these conversations up by \
phone, so try to come away with the patient's number. Once you have answered \
as far as you can — they want to book, they asked something you cannot answer \
here, or you had to send them to a doctor — close your reply by asking for \
their phone number so a colleague can call them back. Write it as one short, \
natural closing sentence in the patient's own language and alphabet (the idea \
of "Telefon raqamingizni qoldirsangiz, hamkasbim siz bilan bog'lanadi"). If \
the patient has already written a number, do not ask for it again — warmly \
confirm that a colleague will call them on it. Ask once, keep it to that one \
sentence, and never let it crowd out the answer to what they actually asked or \
hang it off a bare greeting with nothing else in the message.\
"""

_FAQ_RULE_BLOCK = """

You must answer using ONLY the clinic FAQ information listed below. Never use \
outside knowledge, never guess, and never make up an answer that isn't in the FAQ \
context.

Clinic FAQ context:
{faq_context}

Rules you must always follow, without exception:

1. Answer only from the FAQ context above. If it doesn't contain the answer to \
the patient's question, say so honestly and warmly — do not invent an answer — \
and offer to book them an appointment instead.\
"""

# Used instead of _FAQ_RULE_BLOCK when retrieval found nothing and
# answer_without_faq is on. Rules 2-4 are carried over unchanged: not knowing
# the clinic's FAQ has no bearing on whether the assistant may give medical
# advice, or on the call centre wanting a number. Rule 1 replaces "answer only
# from the FAQ" with the part that still holds without one -- it may reason
# from general knowledge, but a clinic's hours, prices and services are facts
# it does not have and must not produce.
_NO_FAQ_RULE_BLOCK = """

The clinic has not given you its own FAQ information, so answer general \
questions from your own knowledge, within these limits:

1. You do not know this clinic's own details — its opening hours, prices, \
address, staff, or which treatments it offers. Never state or guess any of \
them. If the patient asks about one, say warmly that you'll check with the \
team, and offer to book them an appointment.\
"""

_SYSTEM_PROMPT_TEMPLATE = _PREAMBLE + _FAQ_RULE_BLOCK + _SHARED_RULES
_NO_FAQ_SYSTEM_PROMPT = _PREAMBLE + _NO_FAQ_RULE_BLOCK + _SHARED_RULES

_MEDICAL_ADVICE_REMINDER = """

IMPORTANT: This message was flagged as a possible request for medical advice, \
diagnosis, medication, or treatment guidance. Do not answer the medical \
substance of the question under any circumstances — follow rule 2 above and \
redirect to booking an appointment.\
"""

# Returned instead of asking the LLM anything, so it is the one reply that
# cannot follow the prompt rules above -- it can't mirror the patient's
# language or alphabet, and it can't tell whether a number was already given.
# It still asks for the number, because "we cannot answer this here" is exactly
# the case rule 4 exists for: the call centre is the only route by which this
# patient gets a real answer.
#
# TODO(IGB-?): like EMERGENCY_RESPONSE in guardrail.py, this is a single
# global English string. Move it onto the Tenant (or a per-tenant settings
# table) once clinics can configure their own wording, and pick a translation
# from the detected language instead of always replying in English.
NO_MATCH_RESPONSE = (
    "I don't have that information here — could you leave your phone number? "
    "A colleague from our team will call you back and help you directly."
)


def _format_faq_context(matches: Sequence[KnowledgeBaseMatch]) -> str:
    if not matches:
        return "(No matching FAQ entries were found for this question.)"
    return "\n\n".join(
        f"Q: {match.knowledge_base.question}\nA: {match.knowledge_base.answer}" for match in matches
    )


def _build_system_prompt(
    matches: Sequence[KnowledgeBaseMatch],
    flagged_as_medical_advice: bool,
    default_language: str,
) -> str:
    if matches:
        prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            faq_context=_format_faq_context(matches), default_language=default_language
        )
    else:
        prompt = _NO_FAQ_SYSTEM_PROMPT.format(default_language=default_language)
    if flagged_as_medical_advice:
        prompt += _MEDICAL_ADVICE_REMINDER
    return prompt


async def generate_answer(
    session: AsyncSession,
    user_message: str,
    embedding_provider: EmbeddingProvider | None = None,
    llm_provider: LLMProvider | None = None,
    guardrail_classifier: GuardrailClassifier | None = None,
    settings: Settings | None = None,
) -> str:
    """Turn an incoming patient message into a reply: guardrail check, then
    (unless it's an emergency) retrieve relevant FAQs and ask the LLM to
    answer from that context.

    When retrieval finds nothing, the reply depends on ANSWER_WITHOUT_FAQ.
    Off (the default), the LLM is never asked at all and NO_MATCH_RESPONSE
    is returned, so it cannot invent a clinic detail. On, it answers from
    general knowledge under _NO_FAQ_SYSTEM_PROMPT, which still forbids
    clinic specifics and medical advice — for a deployment whose knowledge
    base isn't populated yet, where one fixed refusal to every message is
    worse than a general reply.
    """
    guardrail = evaluate_guardrail(user_message, guardrail_classifier)
    if guardrail.fixed_response is not None:
        return guardrail.fixed_response

    matches = await retrieve_relevant_faqs(
        session, user_message, embedding_provider=embedding_provider
    )
    if not matches and not (settings or get_settings()).answer_without_faq:
        # Code-level guarantee, not just a prompt instruction: if retrieval
        # found nothing (no rows, or every candidate fell beyond
        # retrieve_relevant_faqs's distance threshold), we don't ask the LLM
        # to improvise. This holds even if the model ever fails to follow
        # rule 1 below. Rule 1 stays in the prompt anyway, for the case this
        # check *doesn't* cover: matches is non-empty but none of the
        # retrieved FAQs actually answer the specific thing the patient
        # asked — retrieval found something in the neighborhood, just not
        # the right thing.
        return NO_MATCH_RESPONSE

    system_prompt = _build_system_prompt(
        matches,
        flagged_as_medical_advice=guardrail.category is GuardrailCategory.MEDICAL_ADVICE,
        default_language=(settings or get_settings()).default_reply_language,
    )
    provider = llm_provider or get_llm_provider()
    return await provider.generate(system_prompt, [{"role": "user", "content": user_message}])
