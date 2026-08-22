from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse

# Imported for the side effect of registering the built-in channel adapters
# (see app.channels), which app.services.delivery looks up by channel type.
from app import channels  # noqa: F401  - importing it registers the adapters
from app.api import (
    admin_router,
    auth_router,
    dashboard_router,
    telegram_webhook_router,
    webapp_router,
    webhook_router,
)
from app.api.auth import NotAuthenticatedError
from app.core.config import get_settings
from app.core.faq_seeding import seed_faqs_if_configured
from app.core.logging import configure_logging
from app.core.provisioning import provision_channel_if_configured

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Web only: the worker boots from the same image but never imports this
    # module, so the two processes cannot race to insert the same channel.
    settings = get_settings()
    await provision_channel_if_configured(settings)
    # After provisioning, not before: seeding resolves its tenant through the
    # channel row that provisioning is the thing responsible for creating.
    await seed_faqs_if_configured(settings)
    yield


app = FastAPI(title="Dental Clinic Assistant", lifespan=lifespan)
app.include_router(webhook_router)
app.include_router(telegram_webhook_router)
app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(admin_router)
app.include_router(webapp_router)


@app.exception_handler(NotAuthenticatedError)
async def _redirect_to_login(request: Request, exc: NotAuthenticatedError) -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# Relative to the working directory, matching app.api.dashboard's
# Jinja2Templates(directory="app/templates"): the container runs from the
# repo root, where the Dockerfile copies both app/ and docs/.
_PRIVACY_POLICY_PAGE = Path("docs/index.html")


@app.get("/privacy")
async def privacy_policy() -> FileResponse:
    """The service's public privacy policy.

    Meta will not publish an app without a reachable privacy-policy URL,
    and an unpublished app is exactly why Instagram delivers no webhooks
    to this deployment. Served from the API itself rather than GitHub
    Pages so the URL sits on the same origin as the webhook Meta already
    calls, and needs no second thing to keep alive.
    """
    return FileResponse(_PRIVACY_POLICY_PAGE, media_type="text/html")
