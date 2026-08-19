FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir .

USER app

EXPOSE 8000

# Shell form (not exec-array) so ${PORT:-8000} actually expands: Railway
# injects PORT at runtime and expects the container to bind to it, while
# local `docker run`/docker-compose usage (no PORT set) falls back to 8000.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
