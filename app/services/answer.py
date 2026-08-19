from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.embeddings import EmbeddingProvider
from app.rag.llm import LLMProvider, get_llm_provider
from app.rag.retrieval import retrieve_relevant_faqs
from app.repositories.knowledge_base import KnowledgeBaseMatch
from app.services.guardrail import GuardrailCategory, GuardrailClassifier, evaluate_guardrail

_SYSTEM_PROMPT_TEMPLATE = """\
You are a warm, friendly front-desk assistant for a dental clinic, chatting with \
patients over Instagram Direct Messages.

Reply in the same language the patient wrote in. Sound like a helpful human \
receptionist texting a patient — friendly and natural, not robotic — but keep \
replies short: a couple of sentences, not an essay.

You must answer using ONLY the clinic FAQ information listed below. Never use \
outside knowledge, never guess, and never make up an answer that isn't in the FAQ \
context.

Clinic FAQ context:
{faq_context}

Rules you must always follow, without exception:

1. Answer only from the FAQ context above. If it doesn't contain the answer to \
the patient's question, say so honestly and warmly — do not invent an answer — \
and offer to book them an appointment instead.

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
of any kind.\
"""

_MEDICAL_ADVICE_REMINDER = """

IMPORTANT: This message was flagged as a possible request for medical advice, \
diagnosis, medication, or treatment guidance. Do not answer the medical \
substance of the question under any circumstances — follow rule 2 above and \
redirect to booking an appointment.\
"""

# TODO(IGB-?): like EMERGENCY_RESPONSE in guardrail.py, this is a single
# global English string. Move it onto the Tenant (or a per-tenant settings
# table) once clinics can configure their own wording, and consider
# detecting the patient's language to pick the right translation instead of
# always replying in English.
NO_MATCH_RESPONSE = (
    "I don't have that information here — would you like me to book you an "
    "appointment so our team can help directly?"
)


def _format_faq_context(matches: Sequence[KnowledgeBaseMatch]) -> str:
    if not matches:
        return "(No matching FAQ entries were found for this question.)"
    return "\n\n".join(
        f"Q: {match.knowledge_base.question}\nA: {match.knowledge_base.answer}" for match in matches
    )


def _build_system_prompt(
    matches: Sequence[KnowledgeBaseMatch], flagged_as_medical_advice: bool
) -> str:
    prompt = _SYSTEM_PROMPT_TEMPLATE.format(faq_context=_format_faq_context(matches))
    if flagged_as_medical_advice:
        prompt += _MEDICAL_ADVICE_REMINDER
    return prompt


async def generate_answer(
    session: AsyncSession,
    user_message: str,
    embedding_provider: EmbeddingProvider | None = None,
    llm_provider: LLMProvider | None = None,
    guardrail_classifier: GuardrailClassifier | None = None,
) -> str:
    """Turn an incoming patient message into a reply: guardrail check, then
    (unless it's an emergency) retrieve relevant FAQs and ask the LLM to
    answer strictly from that context.
    """
    guardrail = evaluate_guardrail(user_message, guardrail_classifier)
    if guardrail.fixed_response is not None:
        return guardrail.fixed_response

    matches = await retrieve_relevant_faqs(
        session, user_message, embedding_provider=embedding_provider
    )
    if not matches:
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
        matches, flagged_as_medical_advice=guardrail.category is GuardrailCategory.MEDICAL_ADVICE
    )
    provider = llm_provider or get_llm_provider()
    return await provider.generate(system_prompt, [{"role": "user", "content": user_message}])
