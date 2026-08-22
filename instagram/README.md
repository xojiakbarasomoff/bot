# Dental Clinic Assistant

A multi-tenant SaaS AI assistant that talks to patients on behalf of dental
clinics, handling conversations, scheduling context, and retrieval-augmented
answers grounded in each clinic's own data.

This is the merged application. It serves **Instagram and Telegram** from
one codebase: one answer pipeline, one conversation store, one database.
Everything a platform knows about itself lives under `app/channels/`;
everything else is platform-neutral and shared. See
[Architecture](#architecture).

`../telegram/` still holds the original Telegram project. Its bot now runs
through the code here; what remains to port from it is the admin dashboard
and the Telegram Mini App, after which that directory goes away and this one
is renamed.

Everything below is relative to this directory — run `make`, `pytest`,
`alembic` and `docker compose` from `instagram/`, not from the repository
root. The one piece deliberately left at the root is
`.github/workflows/ci.yml`: GitHub reads workflows from there and nowhere
else, so it stays put and runs every step with
`working-directory: instagram`.

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

## Connecting a channel

A channel is a clinic's account on one platform. Its credentials live
encrypted in the database, never in the environment — one deployment serves
many clinics, so a single `BOT_TOKEN` variable could only ever be right for
one of them.

### Telegram

From this directory, with the bot token from @BotFather:

```powershell
$env:TELEGRAM_BOT_TOKEN = "<token from @BotFather>"
$env:PUBLIC_BASE_URL = "https://your-deployment.example.com"
$env:TENANT_NAME = "Smile Dental"
./../.venv/Scripts/python.exe scripts/setup_telegram_channel.py
Remove-Item Env:\TELEGRAM_BOT_TOKEN
```

That checks the token with `getMe`, creates the tenant and channel, and
registers the webhook at `/webhook/telegram/<bot id>` with a generated
secret. Re-running refreshes all three rather than duplicating anything.

The secret is what proves a delivery came from Telegram. It is stored in
`Channel.config` and verified on every update — a channel without one
**refuses every delivery**, deliberately: an endpoint that accepts
unauthenticated updates lets anyone write into a clinic's patient
transcript.

### Instagram

`scripts/bootstrap_tenant.py` creates the tenant and channel;
`scripts/set_channel_credentials.py` stores the access token once Meta
issues it. Meta's webhook is configured in the app dashboard, pointing at
`/webhook` and verified with `WEBHOOK_VERIFY_TOKEN` / `META_APP_SECRET`.

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
Instagram webhook   (app/api/webhook.py)          <- platform-specific
Telegram  webhook   (app/api/telegram_webhook.py)  <- platform-specific
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
channel id and a platform-issued user id, never against anything one
platform shaped. Both channels run this exact path; a third would too.

One thing does cross it: `reply_context`, an opaque mapping the inbound edge
captures and the adapter reads back, carried through the queue untouched by
everything in between. It exists because some platforms route a reply by
more than the recipient's id — a Telegram conversation reached through
Telegram Business must be answered over that same business connection, or
the reply goes out from the bot account instead of the clinic's own.

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
