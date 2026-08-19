# Dental Clinic Instagram Assistant

A multi-tenant SaaS AI assistant that talks to patients over Instagram on
behalf of dental clinics, handling conversations, scheduling context, and
retrieval-augmented answers grounded in each clinic's own data.

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

## Folder structure

```
app/
├── core/           # settings, config, shared infrastructure
├── api/            # FastAPI routers and endpoints
├── models/         # SQLAlchemy models
├── repositories/   # data access layer
├── services/       # business logic
├── rag/            # retrieval-augmented generation pipeline
├── workers/         # ARQ background task definitions
└── main.py         # FastAPI app entrypoint

migrations/         # Alembic migrations
infra/              # infrastructure config
tests/              # pytest test suite
```
