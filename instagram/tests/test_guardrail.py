import pytest

from app.services.guardrail import (
    EMERGENCY_RESPONSES,
    GuardrailCategory,
    KeywordGuardrailClassifier,
    evaluate_guardrail,
)

classifier = KeywordGuardrailClassifier()


# --- emergency detection ---


def test_classifies_english_emergency_message() -> None:
    assert classifier.classify("I have severe pain and can't stop bleeding") is (
        GuardrailCategory.EMERGENCY
    )


def test_classifies_russian_emergency_message() -> None:
    assert classifier.classify("Не могу дышать, помогите!") is GuardrailCategory.EMERGENCY


def test_classifies_uzbek_emergency_message() -> None:
    assert classifier.classify("Qon to'xtamayapti, juda qo'rqinchli") is (
        GuardrailCategory.EMERGENCY
    )


def test_classifies_uzbek_emergency_message_with_missing_apostrophe() -> None:
    # Same phrase as above but typed without the apostrophe in "to'xtamayapti"
    # — a very common way patients actually type Uzbek Latin on a phone
    # keyboard. Confirms normalization, not just the exact keyword spelling.
    assert classifier.classify("Qon toxtamayapti") is GuardrailCategory.EMERGENCY


def test_emergency_takes_priority_over_overlapping_medical_advice_keyword() -> None:
    # "is it infected" alone would classify as MEDICAL_ADVICE, but combined
    # with an emergency signal, emergency must win.
    assert classifier.classify("Heavy bleeding, is it infected?") is GuardrailCategory.EMERGENCY


# --- medical-advice detection ---


def test_classifies_english_medical_advice_message() -> None:
    assert classifier.classify("What antibiotic should I take?") is (
        GuardrailCategory.MEDICAL_ADVICE
    )


def test_classifies_russian_medical_advice_message() -> None:
    assert classifier.classify("Какой антибиотик мне выпишите?") is (
        GuardrailCategory.MEDICAL_ADVICE
    )


def test_classifies_uzbek_medical_advice_message() -> None:
    assert classifier.classify("Menda nima kasallik, diagnoz qo'yib bering") is (
        GuardrailCategory.MEDICAL_ADVICE
    )


# --- ordinary messages ---


def test_classifies_ordinary_faq_style_message_as_none() -> None:
    assert classifier.classify("What are your opening hours?") is GuardrailCategory.NONE


# --- evaluate_guardrail() ---


def test_evaluate_guardrail_returns_fixed_response_for_emergency() -> None:
    result = evaluate_guardrail("I fainted and can't breathe")
    assert result.category is GuardrailCategory.EMERGENCY
    assert result.fixed_response == EMERGENCY_RESPONSES["uz-latn"]


def test_evaluate_guardrail_returns_no_fixed_response_for_medical_advice() -> None:
    result = evaluate_guardrail("What antibiotic should I take?")
    assert result.category is GuardrailCategory.MEDICAL_ADVICE
    assert result.fixed_response is None


def test_evaluate_guardrail_returns_no_fixed_response_for_ordinary_message() -> None:
    result = evaluate_guardrail("Do you accept walk-ins?")
    assert result.category is GuardrailCategory.NONE
    assert result.fixed_response is None


# --- what an emergency is, and what is just a toothache ---------------------


@pytest.mark.parametrize(
    "message",
    [
        "ong tomondagi jag tishim og'rivotti",
        "tishim ogrivotti",
        "тишим оғрияпти",
        "tishim og'riyapti, nima qilay?",
        "boshim aylanyapti",
    ],
)
def test_a_toothache_is_not_an_ambulance(message: str) -> None:
    """From production: "ong tomondagi jag tishim og'rivotti" was answered
    with "call 103 immediately", in English. "og'rivotti" is the plain verb
    "it hurts" — the single most common thing anybody writes to a dental
    clinic, and the reason they are writing at all.
    """
    assert evaluate_guardrail(message).category is not GuardrailCategory.EMERGENCY


@pytest.mark.parametrize(
    "message",
    [
        "qon to'xtamayapti",
        "nafas ololmayapman",
        "hushidan ketdi",
        "кровь не останавливается",
        "can't stop bleeding",
    ],
)
def test_a_real_emergency_still_is_one(message: str) -> None:
    """Narrowing the list must not have emptied it: bleeding that will not
    stop, losing consciousness and trouble breathing are not things a
    dentist handles in a chat.
    """
    assert evaluate_guardrail(message).category is GuardrailCategory.EMERGENCY


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("qon to'xtamayapti", "uz-latn"),
        ("қон тўхтамаяпти", "uz-cyrl"),
        ("кровь не останавливается", "ru"),
        ("can't stop bleeding", "uz-latn"),
    ],
)
def test_the_emergency_line_is_written_where_the_patient_can_read_it(
    message: str, expected: str
) -> None:
    """A real patient in Tashkent was told, in English, to call 103. In an
    emergency a message somebody cannot read is the same as no message.

    Anything unrecognised falls back to Uzbek Latin — the clinic's own
    language, and a safer default than English.
    """
    assert evaluate_guardrail(message).fixed_response == EMERGENCY_RESPONSES[expected]


def test_every_emergency_translation_names_the_ambulance_number() -> None:
    for text in EMERGENCY_RESPONSES.values():
        assert "103" in text
