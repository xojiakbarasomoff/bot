import logging
import time
import uuid

import pytest
from arq.connections import ArqRedis
from arq.jobs import Job

import app.services.debounce as debounce_module
from app.services.debounce import (
    FIRE_DEBOUNCE_WINDOW_JOB,
    PROCESS_INBOUND_MESSAGE_JOB,
    _try_clear_buffer,
    handle_inbound_message,
    join_messages,
    pop_batch_if_current_generation,
)
from app.workers.tasks import fire_debounce_window, process_inbound_message


def test_job_name_constants_match_the_real_functions() -> None:
    """debounce.py references these jobs by string literal (to avoid a
    circular import with app.workers.tasks) — this is the guard against that
    string silently drifting from the real function name.
    """
    assert process_inbound_message.__name__ == PROCESS_INBOUND_MESSAGE_JOB
    assert fire_debounce_window.__name__ == FIRE_DEBOUNCE_WINDOW_JOB


# --- timer reset via generation counter ---


async def test_handle_inbound_message_buffers_and_schedules_deferred_job(
    redis_pool: ArqRedis,
) -> None:
    tenant_id = uuid.uuid4()
    sender_igsid = "sender-1"

    await handle_inbound_message(
        redis_pool, tenant_id, sender_igsid, "What are your hours?", window_seconds=25
    )

    messages = await redis_pool.lrange(f"debounce:{tenant_id}:{sender_igsid}:messages", 0, -1)
    assert [m.decode() for m in messages] == ["What are your hours?"]
    generation = await redis_pool.get(f"debounce:{tenant_id}:{sender_igsid}:generation")
    assert generation == b"1"

    job_ids = await redis_pool.zrange("arq:queue", 0, -1)
    assert len(job_ids) == 1
    # Score is the scheduled fire time in ms; confirm it's ~25s out, not now.
    score = await redis_pool.zscore("arq:queue", job_ids[0])
    now_ms = time.time() * 1000
    assert 24_000 < (score - now_ms) < 26_000


async def test_second_message_resets_timer_via_generation_counter(redis_pool: ArqRedis) -> None:
    tenant_id = uuid.uuid4()
    sender_igsid = "sender-1"

    await handle_inbound_message(
        redis_pool, tenant_id, sender_igsid, "first message", window_seconds=25
    )
    await handle_inbound_message(
        redis_pool, tenant_id, sender_igsid, "second message", window_seconds=25
    )

    generation = await redis_pool.get(f"debounce:{tenant_id}:{sender_igsid}:generation")
    assert generation == b"2"

    messages = await redis_pool.lrange(f"debounce:{tenant_id}:{sender_igsid}:messages", 0, -1)
    assert [m.decode() for m in messages] == ["first message", "second message"]

    # Two messages -> two scheduled deferred jobs (the first becomes a
    # no-op via the generation check when it eventually fires; see
    # test_pop_batch_if_current_generation_returns_none_for_stale_generation).
    job_ids = await redis_pool.zrange("arq:queue", 0, -1)
    assert len(job_ids) == 2


# --- batch joins in order ---


def test_join_messages_preserves_order_with_newlines() -> None:
    assert join_messages(["first", "second", "third"]) == "first\nsecond\nthird"


# --- window fires once after quiet period / stale generation no-ops ---


async def test_pop_batch_if_current_generation_pops_and_clears_when_current(
    redis_pool: ArqRedis,
) -> None:
    tenant_id = uuid.uuid4()
    sender_igsid = "sender-1"
    messages_key = f"debounce:{tenant_id}:{sender_igsid}:messages"
    generation_key = f"debounce:{tenant_id}:{sender_igsid}:generation"

    await redis_pool.rpush(messages_key, "hello", "again")
    await redis_pool.set(generation_key, "2")

    result = await pop_batch_if_current_generation(redis_pool, tenant_id, sender_igsid, 2)

    assert result == ["hello", "again"]
    assert await redis_pool.exists(messages_key) == 0
    assert await redis_pool.exists(generation_key) == 0


async def test_pop_batch_if_current_generation_returns_none_for_stale_generation(
    redis_pool: ArqRedis,
) -> None:
    tenant_id = uuid.uuid4()
    sender_igsid = "sender-1"
    messages_key = f"debounce:{tenant_id}:{sender_igsid}:messages"
    generation_key = f"debounce:{tenant_id}:{sender_igsid}:generation"

    await redis_pool.rpush(messages_key, "hello", "again")
    await redis_pool.set(generation_key, "2")

    # generation=1 is stale — a later message (which set generation to 2)
    # arrived after this (hypothetical) job was scheduled.
    result = await pop_batch_if_current_generation(redis_pool, tenant_id, sender_igsid, 1)

    assert result is None
    # Must not have touched anything — a newer message is still
    # accumulating into this buffer.
    messages = await redis_pool.lrange(messages_key, 0, -1)
    assert [m.decode() for m in messages] == ["hello", "again"]
    assert await redis_pool.get(generation_key) == b"2"


async def test_pop_batch_if_current_generation_returns_none_when_nothing_pending(
    redis_pool: ArqRedis,
) -> None:
    result = await pop_batch_if_current_generation(redis_pool, uuid.uuid4(), "sender-1", 1)
    assert result is None


# --- emergency bypasses debounce and fires immediately ---


async def test_handle_inbound_message_emergency_enqueues_immediately_not_deferred(
    redis_pool: ArqRedis,
) -> None:
    tenant_id = uuid.uuid4()
    sender_igsid = "sender-1"

    await handle_inbound_message(
        redis_pool,
        tenant_id,
        sender_igsid,
        "Severe pain and I can't stop bleeding",
        window_seconds=25,
    )

    job_ids = await redis_pool.zrange("arq:queue", 0, -1)
    assert len(job_ids) == 1
    score = await redis_pool.zscore("arq:queue", job_ids[0])
    now_ms = time.time() * 1000
    # Immediate: score is ~now, nowhere near now + 25s.
    assert abs(score - now_ms) < 2_000

    job = Job(job_ids[0].decode(), redis=redis_pool, _queue_name="arq:queue")
    info = await job.info()
    assert info is not None
    assert info.function == PROCESS_INBOUND_MESSAGE_JOB
    assert info.args == (str(tenant_id), sender_igsid, "Severe pain and I can't stop bleeding")

    # Buffer cleared (best-effort clear succeeded — nothing raced with it
    # in this single-threaded test).
    assert await redis_pool.exists(f"debounce:{tenant_id}:{sender_igsid}:messages") == 0


async def test_handle_inbound_message_emergency_does_not_leave_buffer_for_normal_debounce(
    redis_pool: ArqRedis,
) -> None:
    tenant_id = uuid.uuid4()
    sender_igsid = "sender-1"

    await handle_inbound_message(
        redis_pool, tenant_id, sender_igsid, "chest pain, help", window_seconds=25
    )

    # Only the immediate job — no deferred fire_debounce_window also queued
    # for this buffer.
    job_ids = await redis_pool.zrange("arq:queue", 0, -1)
    assert len(job_ids) == 1


# --- split-across-messages emergency is caught ---


async def test_emergency_phrase_split_across_two_messages_is_caught(
    redis_pool: ArqRedis,
) -> None:
    tenant_id = uuid.uuid4()
    sender_igsid = "sender-1"

    # Neither message alone contains "chest pain" or "can't breathe" as a
    # contiguous phrase — only the joined buffer does.
    await handle_inbound_message(
        redis_pool, tenant_id, sender_igsid, "I've been having some chest", window_seconds=25
    )
    job_ids_after_first = await redis_pool.zrange("arq:queue", 0, -1)
    assert len(job_ids_after_first) == 1  # normal deferred job, not emergency

    await handle_inbound_message(
        redis_pool, tenant_id, sender_igsid, "pain and can't breathe", window_seconds=25
    )

    job_ids = await redis_pool.zrange("arq:queue", 0, -1)
    # first (now-stale) deferred job + the new immediate emergency job
    assert len(job_ids) == 2
    scores = {job_id: await redis_pool.zscore("arq:queue", job_id) for job_id in job_ids}
    now_ms = time.time() * 1000
    immediate = [job_id for job_id, score in scores.items() if abs(score - now_ms) < 2_000]
    assert len(immediate) == 1

    # The buffer was cleared as part of the emergency's best-effort clear —
    # confirms the unrelated-looking first fragment was folded into the
    # emergency context rather than left to fire separately later.
    assert await redis_pool.exists(f"debounce:{tenant_id}:{sender_igsid}:messages") == 0


# --- best-effort buffer clear: losing the race is safe, not silent data loss ---


async def test_try_clear_buffer_returns_false_and_leaves_state_when_length_changed(
    redis_pool: ArqRedis,
) -> None:
    tenant_id = uuid.uuid4()
    sender_igsid = "sender-1"
    messages_key = f"debounce:{tenant_id}:{sender_igsid}:messages"
    generation_key = f"debounce:{tenant_id}:{sender_igsid}:generation"

    # Simulates a message arriving between handle_inbound_message's LRANGE
    # peek (which would have observed length 1) and the clear attempt.
    await redis_pool.rpush(messages_key, "first")
    await redis_pool.rpush(messages_key, "raced in after the peek")
    await redis_pool.set(generation_key, "1")

    cleared = await _try_clear_buffer(redis_pool, messages_key, generation_key, expected_length=1)

    assert cleared is False
    # Nothing was touched — the raced-in message is preserved, not lost.
    messages = await redis_pool.lrange(messages_key, 0, -1)
    assert [m.decode() for m in messages] == ["first", "raced in after the peek"]
    assert await redis_pool.get(generation_key) == b"1"


async def test_try_clear_buffer_returns_true_and_clears_when_length_matches(
    redis_pool: ArqRedis,
) -> None:
    tenant_id = uuid.uuid4()
    sender_igsid = "sender-1"
    messages_key = f"debounce:{tenant_id}:{sender_igsid}:messages"
    generation_key = f"debounce:{tenant_id}:{sender_igsid}:generation"

    await redis_pool.rpush(messages_key, "first")
    await redis_pool.set(generation_key, "1")

    cleared = await _try_clear_buffer(redis_pool, messages_key, generation_key, expected_length=1)

    assert cleared is True
    assert await redis_pool.exists(messages_key) == 0
    # Generation bumped (invalidates any already-scheduled deferred job).
    assert await redis_pool.get(generation_key) == b"2"


async def test_handle_inbound_message_logs_debug_when_clear_loses_the_race(
    redis_pool: ArqRedis,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A genuine concurrent race is impractical to reproduce through the
    public function, so this isolates the one behavior that depends on
    it — the DEBUG log — by forcing _try_clear_buffer's documented "lost the
    race" return value directly.
    """

    async def _always_loses_the_race(*args: object, **kwargs: object) -> bool:
        return False

    monkeypatch.setattr(debounce_module, "_try_clear_buffer", _always_loses_the_race)

    with caplog.at_level(logging.DEBUG, logger="app.services.debounce"):
        await handle_inbound_message(
            redis_pool, uuid.uuid4(), "sender-1", "severe pain", window_seconds=25
        )

    assert "debounce_emergency_buffer_clear_skipped_due_to_race" in caplog.text
