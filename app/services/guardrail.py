import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class GuardrailCategory(StrEnum):
    NONE = "none"
    MEDICAL_ADVICE = "medical_advice"
    EMERGENCY = "emergency"


_APOSTROPHE_CHARS = "'’‘ʻʼ`"
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase, strip apostrophe-like characters, and collapse whitespace
    (including newlines) to single spaces.

    Apostrophe-stripping is so Uzbek Latin words like "og'riq" match
    regardless of which apostrophe variant (', ', `, ʻ) the patient typed,
    or whether they typed one at all. Whitespace collapsing matters for the
    debounce batching layer: buffered messages get joined with newlines
    (app.services.debounce), so a phrase split across two message bubbles
    ("chest" / "pain") must still read as "chest pain" here, not
    "chest\\npain", or the emergency check run against the combined buffer
    would miss it. Applied to both the keyword lists (at classifier
    construction) and the incoming message, so both sides normalize the same
    way.
    """
    lowered = text.lower()
    stripped = "".join(ch for ch in lowered if ch not in _APOSTROPHE_CHARS)
    return _WHITESPACE_RE.sub(" ", stripped)


# TODO(IGB-?): this is a global, English/Russian/Uzbek starter list, not a
# clinical or linguistically verified classifier. It WILL have false
# negatives (any phrasing, dialect, or language not represented here) and
# false positives (e.g. "is this normal after whitening?" reads as
# medical-advice). That asymmetry is deliberate: a false positive just means
# an extra "let's book you in" redirect, which is always safe, so the lists
# lean inclusive rather than precise.
#
# UZBEK BLOCK PENDING NATIVE-SPEAKER REVIEW: every Uzbek Latin and Uzbek
# Cyrillic entry below (including the Cyrillic ў/қ/ғ/ҳ diacritics) was
# drafted in good faith but has NOT been verified by a native speaker.
# Do not treat this block as verified, complete, or dialect-covered until
# that review lands — it's the least trustworthy part of this list.
#
# Once clinics can configure their own settings, move these onto the Tenant
# (or a per-tenant settings table) so each clinic can extend/localize its
# own list instead of every tenant sharing this one. KeywordGuardrailClassifier
# already accepts custom keyword sequences via its constructor for exactly
# this reason.
DEFAULT_EMERGENCY_KEYWORDS: Sequence[str] = (
    # --- English ---
    "severe pain",
    "excruciating pain",
    "unbearable pain",
    "can't stop bleeding",
    "won't stop bleeding",
    "heavy bleeding",
    "bleeding a lot",
    "fainted",
    "fainting",
    "passed out",
    "lost consciousness",
    "can't breathe",
    "difficulty breathing",
    "chest pain",
    "throat is swelling",
    "swelling in my throat",
    # --- Russian (Cyrillic) ---
    "сильная боль",
    "нестерпимая боль",
    "невыносимая боль",
    "сильное кровотечение",
    "кровь не останавливается",
    "не могу остановить кровь",
    "потерял сознание",
    "потеряла сознание",
    "обморок",
    "упал в обморок",
    "упала в обморок",
    "не могу дышать",
    "трудно дышать",
    "тяжело дышать",
    "боль в груди",
    "боли в груди",
    # --- Russian, romanized (common in casual/mixed-script texting) ---
    "silno bolit",
    "silnaya bol",
    "krov ne ostanavlivaetsya",
    "poteryal soznanie",
    "poteryala soznanie",
    "ne mogu dyshat",
    "trudno dyshat",
    "bol v grudi",
    # --- Uzbek (Latin) — PENDING NATIVE-SPEAKER REVIEW ---
    "kuchli og'riq",
    "chidab bo'lmas og'riq",
    "chidab bo'lmaydigan og'riq",
    "og'rivotti",
    "qon to'xtamayapti",
    "qon ketishi to'xtamayapti",
    "qattiq qon ketyapti",
    "qon ketvotti",
    "qon kelyapti",
    "hushidan ketdi",
    "hushini yo'qotdi",
    "behush bo'lib qoldi",
    "behol",
    "boshim aylanyapti",
    "nafas ololmayapman",
    "nafas olib bo'lmayapti",
    "nafas qisilmoqda",
    "ko'krak og'rig'i",
    "yurak sohasida og'riq",
    # --- Uzbek (Cyrillic) — PENDING NATIVE-SPEAKER REVIEW ---
    "кучли оғриқ",
    "чидаб бўлмас оғриқ",
    "қон тўхтамаяпти",
    "ҳушидан кетди",
    "ҳушини йўқотди",
    "беҳуш бўлиб қолди",
    "беҳол",
    "бошим айланяпти",
    "нафас ололмаяпман",
    "кўкрак оғриғи",
)

DEFAULT_MEDICAL_ADVICE_KEYWORDS: Sequence[str] = (
    # --- English ---
    "diagnose",
    "diagnosis",
    "what's wrong with me",
    "what is wrong with me",
    "do i have",
    "is this normal",
    "is it infected",
    "is this infected",
    "prescribe",
    "prescription",
    "what medication",
    "what antibiotic",
    "which antibiotic",
    "can i take",
    "dosage",
    "how much should i take",
    "what treatment",
    "which treatment",
    "do i need a root canal",
    "should i get a root canal",
    "do i need surgery",
    # --- Russian (Cyrillic) ---
    "диагноз",
    "поставьте диагноз",
    "что со мной",
    "что у меня",
    "выпишите",
    "пропишите",
    "какое лекарство",
    "какой антибиотик",
    "антибиотик",
    "дозировка",
    "сколько принимать",
    "какое лечение",
    "нужна ли операция",
    # --- Russian, romanized ---
    "kakoy antibiotik",
    "propishite",
    "vypishite",
    "kakoe lechenie",
    "dozirovka",
    # --- Uzbek (Latin) — PENDING NATIVE-SPEAKER REVIEW ---
    "diagnoz",
    "menda nima kasallik",
    "nima kasallik bu",
    "dori yozib bering",
    "retsept yozing",
    "qanday dori",
    "qaysi antibiotik",
    "antibiotik",
    "qancha ichishim kerak",
    "qanday davo",
    "davolash kerakmi",
    "operatsiya kerakmi",
    # --- Uzbek (Cyrillic) — PENDING NATIVE-SPEAKER REVIEW ---
    "диагноз",
    "менда нима касаллик",
    "дори ёзиб беринг",
    "қандай дори",
    "қандай даво",
)

# TODO(IGB-?): exact wording AND the emergency phone number (103) must be
# confirmed per clinic/tenant — the TZ lists this as an open question — and
# translated per language rather than always replying in English.
EMERGENCY_RESPONSE = (
    "This sounds like it could be a medical emergency. Please call 103 right away, "
    "or come to the clinic immediately — please don't wait for a reply here. "
    "If someone can come with you, that's even better."
)


class GuardrailClassifier(ABC):
    """Abstraction over "does this message need special handling before we
    let the LLM near it", so a keyword classifier can later be swapped for
    something smarter (e.g. an LLM-based or ML classifier) without touching
    callers — same shape as EmbeddingProvider/LLMProvider.
    """

    @abstractmethod
    def classify(self, text: str) -> GuardrailCategory: ...


class KeywordGuardrailClassifier(GuardrailClassifier):
    def __init__(
        self,
        emergency_keywords: Sequence[str] = DEFAULT_EMERGENCY_KEYWORDS,
        medical_advice_keywords: Sequence[str] = DEFAULT_MEDICAL_ADVICE_KEYWORDS,
    ) -> None:
        self._emergency_keywords = tuple(_normalize(k) for k in emergency_keywords)
        self._medical_advice_keywords = tuple(_normalize(k) for k in medical_advice_keywords)

    def classify(self, text: str) -> GuardrailCategory:
        normalized = _normalize(text)
        # Emergency checked first: "heavy bleeding, is that normal?" should
        # short-circuit to the emergency response, not the advice redirect.
        if any(keyword in normalized for keyword in self._emergency_keywords):
            return GuardrailCategory.EMERGENCY
        if any(keyword in normalized for keyword in self._medical_advice_keywords):
            return GuardrailCategory.MEDICAL_ADVICE
        return GuardrailCategory.NONE


_DEFAULT_CLASSIFIER = KeywordGuardrailClassifier()


@dataclass(frozen=True)
class GuardrailResult:
    category: GuardrailCategory
    # Set only for EMERGENCY: callers must return this directly and skip the
    # LLM entirely, not treat it as a suggestion.
    fixed_response: str | None


def evaluate_guardrail(
    user_message: str, classifier: GuardrailClassifier | None = None
) -> GuardrailResult:
    category = (classifier or _DEFAULT_CLASSIFIER).classify(user_message)
    if category is GuardrailCategory.EMERGENCY:
        logger.warning("guardrail_emergency_triggered")
        return GuardrailResult(category=category, fixed_response=EMERGENCY_RESPONSE)
    if category is GuardrailCategory.MEDICAL_ADVICE:
        logger.info("guardrail_medical_advice_flagged")
    return GuardrailResult(category=category, fixed_response=None)
