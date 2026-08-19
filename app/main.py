from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

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
