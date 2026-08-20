import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.api import auth_router, dashboard_router, webhook_router
from app.api.auth import NotAuthenticatedError
from app.core.config import get_settings
from app.core.provisioning import provision_channel_if_configured

# Without this, nothing installs a log handler and Python's fallback emits
# WARNING and above only -- every logger.info in this codebase (the webhook's
# per-message trail among them) is discarded before it reaches the platform's
# log stream, which is the one place anyone reads it.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Web only: the worker boots from the same image but never imports this
    # module, so the two processes cannot race to insert the same channel.
    await provision_channel_if_configured(get_settings())
    yield


app = FastAPI(title="Dental Clinic Instagram Assistant", lifespan=lifespan)
app.include_router(webhook_router)
app.include_router(auth_router)
app.include_router(dashboard_router)


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
