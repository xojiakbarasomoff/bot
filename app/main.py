from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.api import auth_router, dashboard_router, webhook_router
from app.api.auth import NotAuthenticatedError

app = FastAPI(title="Dental Clinic Instagram Assistant")
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
