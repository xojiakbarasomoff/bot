"""What the assistant can already tell about a conversation before it answers.

The prompt used to state its two social rules unconditionally: return the
greeting, and close by asking for a phone number. Applied to every turn,
that produces a correspondent who says hello five times and ends every
single message asking for a number nobody is refusing to give -- which reads
as a machine running a script, and makes the request itself easy to ignore.

Both rules are really conditional, and the conditions are things code can
establish more reliably than a model reading its own history: is this the
first message, has a number already been given, and have we asked before.
Establishing them here keeps the prompt free of "work out whether you have
already asked", which is exactly the kind of self-inspection models are
worst at.

None of this is a hard gate. The signals are rendered into the prompt as
facts, and the rules that use them are written so the failure mode is
asking too little rather than too much -- a patient who is never asked can
still be asked next turn, while a patient asked in every message has already
decided what this is.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.rag.llm import ChatMessage

# Nine digits is the shortest real Uzbek subscriber number (90 123 45 67),
# and twelve covers +998 with the country code. The separators people type
# between the groups are stripped first.
#
# Deliberately not matching shorter runs: a clinic that quotes "3 500 000
# so'm" or answers "09:00 - 20:00" would otherwise look like a patient who
# has already left a number, and the assistant would stop asking.
_SEPARATORS = re.compile(r"[\s\-()+.]")
_PHONE = re.compile(r"\d{9,12}")

# Words an assistant turn asking for a number almost always contains, in the
# languages and alphabets this deployment answers in.
#
# The callback words matter as much as the phone ones. Rule 7 asks for the
# number by offering the thing the patient wants -- "tell me a time that
# suits you and a colleague will call" -- and that sentence need never
# contain "raqam" at all. A list of only phone words would therefore miss
# exactly the phrasing the prompt recommends, leave the counter at zero, and
# let the assistant ask again in the very next message: the behaviour this
# module exists to stop.
#
# Still a heuristic, and known to be one. It is allowed to be rough because
# of which way it is wrong: over-counting holds back a request that can be
# made on any later turn, while under-counting produces the repetition.
_ASK_MARKERS = (
    # the number itself
    "raqam",
    "nomer",
    "номер",
    "телефон",
    "telefon",
    "phone",
    # ...and asking for it by promising the call
    "qongiroq",
    "bogla",
    "звон",
    "свяж",
    "call you",
    "call back",
)

# Apostrophes are written at least four ways in Uzbek Latin ("qo'ng'iroq",
# "qoʻngʻiroq", "qo‘ng‘iroq", "qo`ng`iroq"), and which one arrives depends on
# the patient's keyboard and on the model. Removing them entirely, on both
# sides of the comparison, is what makes one spelling of the marker enough.
_APOSTROPHES = re.compile(r"['‘’ʻʼ`´]")


def _normalise(text: str) -> str:
    return _APOSTROPHES.sub("", text.lower())


@dataclass(frozen=True)
class ConversationSignals:
    """Facts about the conversation so far, from the assistant's side."""

    is_opening: bool
    patient_left_number: bool
    times_asked_for_number: int


def looks_like_a_phone_number(text: str) -> bool:
    return find_phone_number(text) is not None


def find_phone_number(text: str) -> str | None:
    """The number itself, normalised, or None.

    Normalised because the same patient writes "+998 90 123 45 67" today and
    "998901234567" next week, and the clinic's spreadsheet is keyed on this
    string — two spellings would be two rows for one person.
    """
    match = _PHONE.search(_SEPARATORS.sub("", text))
    return match.group(0) if match else None


def read_signals(
    history: Sequence[ChatMessage] | None, user_message: str | None = None
) -> ConversationSignals:
    """Read the conversation's earlier turns, plus the message being answered.

    `history` excludes the message being answered right now (see
    app.services.conversation.context_for_reply), so an empty history is
    precisely "this is the first thing they have said to us".

    `user_message` is that excluded message, and it counts towards whether a
    number has been given. A patient who types their number unprompted has
    given it, and a facts block still saying "they have not given you a
    phone number yet" would be contradicting the message printed directly
    below it — which is exactly the disagreement that gets a patient asked
    for something they just typed.

    It deliberately does not count towards `is_opening` or towards the ask
    count: the first thing somebody says is still the opening, and the
    patient's own message is not us asking.
    """
    turns = list(history or [])
    patient_left_number = any(
        turn["role"] == "user" and looks_like_a_phone_number(turn["content"]) for turn in turns
    ) or bool(user_message and looks_like_a_phone_number(user_message))
    times_asked = sum(
        1
        for turn in turns
        if turn["role"] == "assistant"
        and any(m in _normalise(turn["content"]) for m in _ASK_MARKERS)
    )
    return ConversationSignals(
        is_opening=not turns,
        patient_left_number=patient_left_number,
        times_asked_for_number=times_asked,
    )


def render(signals: ConversationSignals) -> str:
    """The signals as a prompt section.

    Written as plain statements of fact rather than as instructions: the
    rules that act on them live in the prompt proper, so that reading the
    rules shows the whole policy in one place instead of half of it here.
    """
    lines = [
        (
            "This is the patient's first message to you."
            if signals.is_opening
            else "You are already in the middle of this conversation — "
            "they have written to you before."
        ),
        (
            "They have already given you a phone number."
            if signals.patient_left_number
            else "They have not given you a phone number yet."
        ),
    ]
    if signals.times_asked_for_number == 0:
        lines.append("You have not asked them for their number in this conversation.")
    elif signals.times_asked_for_number == 1:
        lines.append("You have already asked them for their number once.")
    else:
        lines.append(
            f"You have already asked them for their number "
            f"{signals.times_asked_for_number} times."
        )
    body = "\n".join(f"- {line}" for line in lines)
    return f"\n\nWHERE THIS CONVERSATION STANDS\n{body}"
