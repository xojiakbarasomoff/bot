# AI Medical Assistant — Klinikalar uchun sun'iy intellektli tibbiy operator

Multi-tenant AI Medical Assistant application built with Python 3.12, FastAPI, PostgreSQL (pgvector), Redis (Debounce/Queue), ARQ Worker, and OpenAI API (GPT-4o mini).

## Architecture & Technology Stack

- **Framework**: Python 3.12 + FastAPI (Async Web Framework)
- **Database**: PostgreSQL 16 with `pgvector` extension for semantic search (RAG)
- **Async Queue & Cache**: Redis 7 + ARQ for 15-40s message debouncing & background LLM workers
- **ORM & Migrations**: SQLAlchemy 2 (Async) + Alembic
- **Multi-Tenant Isolation**: Row-Level Tenant Isolation (`tenant_id`)
- **Security**: Sensitive token encryption at-rest via Fernet cryptography & Bearer Token Admin Auth
- **Connection Pooling**: Process-wide singleton `httpx.AsyncClient` with TCP connection reuse

### Data Flow Diagram

```
Telegram User -> Telegram Bot API -> Bot Polling Container -> Web (FastAPI) -> Redis Debounce Pipeline
                                                                                  |
                                                                              ARQ Worker
                                                                                  |
                                                           PG Vector (RAG) + OpenAI API
                                                                                  |
                                                                         Telegram Output
```

## Quick Start (Docker Compose)

1. Clone the repository and copy environment variables:
   ```bash
   cp .env.example .env
   ```

2. Configure critical parameters in `.env`:
   - `ENCRYPTION_KEY`: Generate via `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   - `OPENAI_API_KEY`: Your OpenAI or Gemini API key
   - `TELEGRAM_BOT_TOKEN`: Token obtained from Telegram @BotFather

3. Start all services via Docker Compose:
   ```bash
   docker compose up -d --build
   ```

4. Run database migrations:
   ```bash
   docker compose exec web alembic upgrade head
   ```

5. Seed the database with 100 Dental Clinic FAQs:
   ```bash
   docker compose exec web python scripts/seed_faq.py
   ```

6. Check API health:
   ```bash
   curl http://localhost:8001/health
   ```

## Admin Panel & WebApp

- **Admin Dashboard**: Accessible at `http://localhost:8001/admin/`
- **Visual Booking WebApp**: Accessible at `http://localhost:8001/webapp/booking.html`

## API Documentation

Interactive OpenAPI documentation is available once running at:
- Swagger UI: `http://localhost:8001/docs`
- ReDoc: `http://localhost:8001/redoc`

## Environment Variables Reference

| Variable | Description | Default / Example |
| :--- | :--- | :--- |
| `APP_NAME` | Service display name | `AI Medical Assistant` |
| `APP_ENV` | Environment (`development` / `production`) | `development` |
| `DEBUG` | Enable debug mode | `True` |
| `ENCRYPTION_KEY` | Fernet key for token encryption | 32-byte Base64 string |
| `DATABASE_URL` | PostgreSQL connection string | `postgresql+asyncpg://...` |
| `REDIS_URL` | Redis connection string | `redis://redis:6379/0` |
| `OPENAI_API_KEY` | LLM service provider API key | `sk-...` |
| `TELEGRAM_BOT_TOKEN` | Bot API Token from @BotFather | `123456789:ABC...` |
| `TELEGRAM_OPERATOR_CHANNEL_ID` | Telegram Channel ID for lead alerts | `-1001234567890` |
| `CORS_ORIGINS` | Allowed origins (comma-separated) | `http://localhost:3000` |

