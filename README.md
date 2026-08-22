# Dental Clinic Assistant — Instagram channel

A multi-tenant SaaS AI assistant that talks to patients on behalf of dental
clinics, handling conversations, scheduling context, and retrieval-augmented
answers grounded in each clinic's own data.

This repository serves the **Instagram** channel. It is one half of a single
product: a Telegram bot is being prepared on its own branch, and the two are
intended to become one deployment with one shared core. The code here is laid
out for that merge — see [Architecture](#architecture) below. In short:
everything a platform knows about itself lives under `app/channels/`, and
everything else (the answer pipeline, the conversation store, the database)
is platform-neutral and meant to be reached by both bots without a second
copy.

## Prerequisites

- Python 3.12
- Docker Desktop (with Docker Compose)

## Local setup

1. Clone the repo and create a virtual environment:

   ```bash
   python3.12 -m venv .venv
   .venv\Scripts\activate      # Windows
   source .venv/bin/activate   # macOS/Linux
   ```

2. Install the project with dev dependencies:

   ```bash
   make install
   ```

3. Copy the environment template and fill in real values:

   ```bash
   cp .env.example .env
   ```

## Running with Docker

Bring up the full stack (api, worker, postgres with pgvector, redis):

```bash
make up
```

Docker Compose reads its own env file, `.env.docker` (copy it from
`.env.docker.example`) — separate from the `.env` you set up above, since
containers need `DATABASE_URL`/`REDIS_URL` to point at the `postgres`/`redis`
service names rather than `localhost`:

```bash
cp .env.docker.example .env.docker
```

Check the API is up:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

Tear the stack down:

```bash
make down
```

## Running tests

```bash
make test
```

Lint, format, and type-check:

```bash
make lint
make format
make typecheck
```

## Architecture

The codebase is split along one line: **what a messaging platform knows about
itself** versus **what the clinic's assistant does**. Only the first half is
Instagram-specific.

```
app/
├── channels/       # PLATFORM-SPECIFIC — the only code that names a platform
│   ├── base.py     #   ChannelAdapter contract + registry
│   └── instagram/  #   Graph API client, 24h window, placeholder tokens
├── api/            # FastAPI routers; webhook.py is Instagram's inbound edge
├── core/           # settings, db, encryption, logging, tenant context
├── models/         # SQLAlchemy models
├── repositories/   # tenant-scoped data access
├── services/       # SHARED business logic (see below)
├── rag/            # retrieval-augmented generation pipeline
├── workers/        # ARQ background jobs
└── main.py         # FastAPI app entrypoint

migrations/         # Alembic migrations
infra/              # infrastructure config
tests/              # pytest test suite
```

### The inbound path

```
Instagram webhook  (app/api/webhook.py)      <- platform-specific
        |   verify signature, parse payload, resolve channel
        v
idempotency.claim_event                      <- shared: drop redeliveries
        |
        v
conversation.register_inbound_message        <- shared: user/conversation/message
        |   stops here if an operator has taken the conversation over
        v
debounce.handle_inbound_message              <- shared: batch bubbles, catch emergencies
        |
        v
workers.tasks.process_inbound_message        <- shared: the ARQ job
        |
        +--> answer.generate_answer          <- shared: guardrail, RAG, prompt, LLM
        |
        +--> delivery.send_reply             <- shared: dispatch by channel type
                     |
                     v
             ChannelAdapter.send_text        <- platform-specific again
```

Everything between the two platform-specific ends is written against a
channel id and a platform-issued user id, never against anything Instagram-
shaped. Adding Telegram means writing a `ChannelAdapter` and an inbound route
that calls the same shared services — not a second answer pipeline, a second
debounce, or a second conversation store.

### Adding a channel

1. Implement `ChannelAdapter` (`app/channels/base.py`): `send_text`, plus
   `delivery_block_reason` if the platform restricts unsolicited replies.
2. Register it in `app/channels/__init__.py`.
3. Add an inbound route that verifies the platform's own authentication,
   resolves the channel with `tenant_resolution.resolve_channel`, claims the
   event id with `idempotency.claim_event`, records it with
   `conversation.register_inbound_message`, and hands it to
   `debounce.handle_inbound_message`.

Nothing else changes. `Channel.type` already carries the value, the ARQ jobs
already take a channel id, and the Redis keys are already namespaced per
channel.

### Multi-tenancy

Every tenant-scoped read and write goes through a repository that filters on
the tenant bound to the current request or job (`app/core/tenant_context.py`).
A tenant is established from the channel an inbound event names, or from the
logged-in operator — never from anything the caller supplies. See
`app/repositories/base.py` and `tests/test_tenant_isolation.py`.
