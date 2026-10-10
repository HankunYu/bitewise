"""Request authentication: a user's token via Bearer header (shortcuts) or session cookie (browser)."""

from fastapi import Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from . import users
from .db import Store

SESSION_COOKIE = "bitewise_session"
SESSION_MAX_AGE = 365 * 24 * 3600


class NotAuthenticated(Exception):
    pass


def request_token(request: Request) -> str | None:
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and credentials:
        return credentials.strip()
    return request.cookies.get(SESSION_COOKIE)


def current_store(request: Request) -> Store:
    """FastAPI dependency: the authenticated user's store."""
    store = users.authenticate(request_token(request))
    if store is None:
        raise NotAuthenticated()
    return store


def not_authenticated_handler(request: Request, _exc: NotAuthenticated) -> Response:
    # Page loads go to the login form; API calls and images get a plain 401.
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": "not authenticated"}, status_code=401)


def set_session(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        # Lax keeps the cookie off cross-site POSTs (CSRF) but on normal links.
        samesite="lax",
        # Behind the TLS proxy the scheme comes from X-Forwarded-Proto.
        secure=request.url.scheme == "https",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="lax")
