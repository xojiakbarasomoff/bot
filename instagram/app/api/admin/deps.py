"""Shared dependencies for the admin API.

The tenant is never a request parameter here. It comes from the logged-in
operator's own row (app.api.auth.get_current_operator sets it in context for
the lifetime of the request), and every repository read and write filters on
it. The Telegram admin API this replaces took `tenant_id: int = Query(1)` on
every endpoint, so any authenticated operator could read and modify another
clinic's patients, appointments and conversations by changing a number in
the URL.
"""

import secrets

from fastapi import Depends, Header, HTTPException, status

from app.api.auth import get_current_operator, get_current_session
from app.core.session import Session
from app.models.operator import Operator

# The header a browser fetch sends the session's CSRF token in. The dashboard
# templates use a hidden form field (app.api.auth.verify_csrf); a JSON API
# has no form to hide it in, so the same double-submit check reads a header
# instead. Both compare against the token bound into the signed session
# cookie, which a cross-site caller cannot read.
CSRF_HEADER = "X-CSRF-Token"


async def verify_csrf_header(
    x_csrf_token: str | None = Header(default=None),
    session: Session = Depends(get_current_session),
) -> None:
    if not x_csrf_token or not secrets.compare_digest(x_csrf_token, session.csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing or invalid {CSRF_HEADER}",
        )


async def require_manage_role(operator: Operator = Depends(get_current_operator)) -> Operator:
    """Gate for anything that changes clinic data.

    A `doctor` account gets read access through get_current_operator but not
    this — it is there to look at the day's schedule, not to rebook it.
    """
    if operator.role == "doctor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This account has view-only access"
        )
    return operator
