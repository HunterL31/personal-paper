"""HTTP Basic auth for the web page.

One reader, one password, taken from the container's `WEB_PASSWORD`. The
username is ignored. Leaving `WEB_PASSWORD` unset turns the login off
entirely: the page is then open to anyone on the network, and the status
strip says so. The list endpoints are open: `POST /lists/<slug>` and its
older alias `POST /tasks` (the iPhone Shortcut carries its own bearer
token, checked in `app.main`), and so is `GET /healthz`.
"""
from __future__ import annotations

import base64
import binascii
import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.settings import Env

#: Paths that never ask for the browser password.
OPEN_ROUTES: set[tuple[str, str]] = {("POST", "/tasks"), ("GET", "/healthz")}
#: Path prefixes that never ask for it either: the phone posts a list to
#: `/lists/<slug>` with a bearer token of its own.
OPEN_PREFIXES: set[tuple[str, str]] = {("POST", "/lists/")}

_CHALLENGE = {"WWW-Authenticate": 'Basic realm="Personal Paper: any username, password is WEB_PASSWORD"'}


def is_open(method: str, path: str) -> bool:
    method = method.upper()
    path = path.rstrip("/") or "/"
    if (method, path) in OPEN_ROUTES:
        return True
    return any(method == m and path.startswith(prefix) for m, prefix in OPEN_PREFIXES)


def _password() -> str | None:
    return Env.get(Env.WEB_PASSWORD)


def check(header: str | None) -> bool:
    """True when `header` is Basic auth whose password matches WEB_PASSWORD."""
    password = _password()
    if not password or not header:
        return False
    scheme, _, payload = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        decoded = base64.b64decode(payload.strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False
    _, _, given = decoded.partition(":")
    return secrets.compare_digest(given.encode("utf-8"), password.encode("utf-8"))


def enabled() -> bool:
    """True when a password is set and the page asks for it."""
    return bool(_password())


def unauthorized() -> Response:
    return JSONResponse({"detail": "Not authenticated"}, status_code=401, headers=_CHALLENGE)


async def middleware(request: Request, call_next):
    """Basic auth in front of everything but the two open routes."""
    # The route's own path: `/p/<id>/lists/<slug>` is another paper's list
    # endpoint, and just as open as the main paper's.
    path = request.url.path
    prefix = request.scope.get("root_path") or ""
    if prefix and path.startswith(prefix + "/"):
        path = path[len(prefix):]
    if not enabled() or is_open(request.method, path) or request.method == "OPTIONS":
        return await call_next(request)
    if not check(request.headers.get("authorization")):
        return unauthorized()
    return await call_next(request)
