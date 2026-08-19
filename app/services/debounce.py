import logging
import uuid
from collections.abc import Awaitable, Sequence
from pathlib import Path
from typing import cast

from arq.connections import ArqRedis

from app.core.config import get_settings
from app.services.guardrail import GuardrailCategory, GuardrailClassifier, evaluate_guardrail

logger = logging.getLogger(__name__)

# Registered job names — string literals rather than importing the functions
# from app.workers.tasks, to avoid a circular import (tasks.py imports
# pop_batch_if_current_generation from this module). Kept in sync by
# test_debounce.py, which asserts these match the real functions' __name__.
PROCESS_INBOUND_MESSAGE_JOB = "process_inbound_message"
FIRE_DEBOUNCE_WINDOW_JOB = "fire_debounce_window"

# Safety-net TTL padding only — NOT the firing mechanism (that's the
# deferred fire_debounce_window job + generation check below). Guards
# against pending state leaking forever if a scheduled job is somehow lost
# (worker crash, redeploy, etc). Comfortably longer than any realistic
# debounce window.
_PENDING_KEY_TTL_PADDING_SECONDS = 300

_LUA_DIR = Path(__file__).parent / "lua"


def _load_script(filename: str) -> str:
    return (_LUA_DIR / filename).read_text(encoding="utf-8")


# See app/services/lua/*.lua for the scripts themselves and their comments.
_POP_IF_CURRENT_GENERATION = _load_script("pop_if_current_generation.lua")
_CLEAR_IF_LENGTH_UNCHANGED = _load_script("clear_if_length_unchanged.lua")


def _messages_key(tenant_id: uuid.UUID, sender_igsid: str) -> str:
    return f"debounce:{tenant_id}:{sender_igsid}:messages"


def _generation_key(tenant_id: uuid.UUID, sender_igsid: str) -> str:
    return f"debounce:{tenant_id}:{sender_igsid}:generation"


def _decode(values: Sequence[bytes | str]) -> list[str]:
    return [v.decode("utf-8") if isinstance(v, bytes) else v for v in values]


def join_messages(messages: Sequence[str]) -> str:
    """The single formatting rule for turning buffered messages into one
    block of text — used both when classifying the accumulated buffer and
    when handing the final batch to process_inbound_message, so what gets
    classified and what gets processed are always the same text.
    """
    return "\n".join(messages)


async def pop_batch_if_current_generation(
    pool: ArqRedis, tenant_id: uuid.UUID, sender_igsid: str, generation: int
) -> list[str] | None:
    """Called by fire_debounce_window. Returns the claimed messages (and
    clears the buffer) if `generation` is still current, or None if this
    call is stale.
    """
    script = pool.register_script(_POP_IF_CURRENT_GENERATION)
    result = await script(
        keys=[_generation_key(tenant_id, sender_igsid), _messages_key(tenant_id, sender_igsid)],
        args=[str(generation)],
    )
    if not result:
        return None
    return _decode(result)


async def _try_clear_buffer(
    pool: ArqRedis, messages_key: str, generation_key: str, expected_length: int
) -> bool:
    """Best-effort, single attempt: clears the buffer and bumps the
    generation counter if its length still matches `expected_length` (the
    length observed a moment ago, before deciding to clear). Returns whether
    it succeeded. Never retried — see handle_inbound_message for why a
    failure here is fine to just log and move on from.
    """
    clear_script = pool.register_script(_CLEAR_IF_LENGTH_UNCHANGED)
    cleared = await clear_script(keys=[messages_key, generation_key], args=[str(expected_length)])
    return bool(cleared)


async def handle_inbound_message(
    pool: ArqRedis,
    tenant_id: uuid.UUID,
    sender_igsid: str,
    message_text: str,
    *,
    window_seconds: int | None = None,
    guardrail_classifier: GuardrailClassifier | None = None,
) -> None:
    """Entry point the webhook calls for every genuine inbound message.

    Always appends the message to this user's pending buffer first —
    appending is safe regardless of what happens next, so a message is
    never at risk of being lost, only (in a rare race) possibly handled
    twice. Then classifies the buffer's *combined* text, not just this one
    message in isolation: an emergency phrase split across two message
    bubbles ("chest" / "pain") only becomes visible once joined.

    An emergency unconditionally and immediately enqueues
    process_inbound_message (no debounce wait) — that enqueue never depends
    on anything else here succeeding. Everything else (including
    medical-advice — that redirect framing is still applied correctly later
    since generate_answer re-classifies the final joined batch at fire time)
    schedules/refreshes the normal debounce window.
    """
    messages_key = _messages_key(tenant_id, sender_igsid)
    generation_key = _generation_key(tenant_id, sender_igsid)
    window = (
        window_seconds if window_seconds is not None else get_settings().debounce_window_seconds
    )
    ttl_seconds = window + _PENDING_KEY_TTL_PADDING_SECONDS

    # redis-py's command-mixin stubs return a sync/async union that doesn't
    # narrow for the concrete async client — cast at this boundary (same
    # pattern as the OpenAI/ArqRedis boundaries elsewhere in this codebase).
    await cast(Awaitable[int], pool.rpush(messages_key, message_text))
    await pool.expire(messages_key, ttl_seconds)

    lrange_result = await cast(Awaitable[list[bytes]], pool.lrange(messages_key, 0, -1))
    pending = _decode(lrange_result)
    combined_text = join_messages(pending)

    guardrail = evaluate_guardrail(combined_text, guardrail_classifier)

    if guardrail.category is GuardrailCategory.EMERGENCY:
        await pool.enqueue_job(
            PROCESS_INBOUND_MESSAGE_JOB, str(tenant_id), sender_igsid, combined_text
        )
        if not await _try_clear_buffer(pool, messages_key, generation_key, len(pending)):
            logger.debug("debounce_emergency_buffer_clear_skipped_due_to_race")
        return

    generation = await pool.incr(generation_key)
    await pool.expire(generation_key, ttl_seconds)
    await pool.enqueue_job(
        FIRE_DEBOUNCE_WINDOW_JOB,
        str(tenant_id),
        sender_igsid,
        generation,
        _defer_by=window,
    )
