"""Access control (design, "Access control" section; access-control
specification).

Two trust models, selected by whether `ECOFLOW_STATS_PASSWORD` is set:

- **No password (default)**: every route is open except the health
  check, but a LAN guard admits only clients inside
  `ECOFLOW_STATS_ALLOWED_NETWORKS`; anyone else is told to configure a
  password.
- **Password configured**: the LAN guard turns off and every route
  except the health check, the login page, and static assets requires a
  valid session, issued only by the exact configured password.

CSRF and security headers (the design's other half of this section) are
a separate later slice (Phase 14 PR ii) and are not implemented here;
the one dependency it introduces (`require_csrf`) composes independently
of everything below, so nothing here needs reshaping to add it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

SESSION_COOKIE = "efs_session"
_HEALTH_CHECK_PATH = "/healthz"
_LOGIN_PATH = "/login"
_STATIC_PREFIX = "/static/"


def client_in_allowed_networks(host: str | None, networks: Sequence[str]) -> bool:
    """Pure: whether `host` (a peer address string) belongs to any of
    `networks` (CIDR strings already validated by `config.load_settings`).

    A host that is missing, or is not a parseable IP address, cannot be
    matched against a CIDR range at all. In production uvicorn always
    supplies the real TCP peer address, so this only happens for a
    non-network ASGI transport (a test harness's own placeholder address
    when no real peer is simulated) — it is let through rather than
    guessing, so the guard only ever actively rejects a peer whose
    address it can parse and verify is outside every allowed range.
    """
    if not host:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return any(address in ipaddress.ip_network(network, strict=False) for network in networks)


def lan_denied_response() -> HTMLResponse:
    """The page scenario "A client outside ECOFLOW_STATS_ALLOWED_NETWORKS
    gets 403" promises: tells the operator what to do, never the
    protected content."""
    return HTMLResponse(
        "<!doctype html><html><body>"
        "<h1>Access denied</h1>"
        "<p>This client is outside the allowed network range. "
        "Set ECOFLOW_STATS_PASSWORD to allow access from other networks.</p>"
        "</body></html>",
        status_code=403,
    )


def _sha256(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def verify_password(candidate: str, configured: str) -> bool:
    """Constant-time comparison over digests, never the raw strings
    (design, "Password set": `hmac.compare_digest` on SHA-256
    digests)."""
    return hmac.compare_digest(_sha256(candidate), _sha256(configured))


def session_signing_key(app_secret: bytes, password: str) -> bytes:
    """`HMAC(app_state.secret, sha256(password))` (design, "Session"):
    changing the configured password changes this key, which silently
    invalidates every session token signed under the old one."""
    return hmac.new(app_secret, _sha256(password), hashlib.sha256).digest()


def issue_session_token(signing_key: bytes, *, issued_at: int, expires_at: int) -> str:
    """`base64 payload {"iat", "exp"} + HMAC-SHA256` (design,
    "Session")."""
    payload = json.dumps({"iat": issued_at, "exp": expires_at}, separators=(",", ":")).encode(
        "ascii"
    )
    payload_b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    signature = hmac.new(signing_key, payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{signature}"


def verify_session_token(signing_key: bytes, token: str, *, now: int) -> bool:
    """A token is valid only under this exact `signing_key`, with a
    correct signature, and not yet expired. A password change derives a
    different `signing_key` (via `session_signing_key`), so every token
    signed under the old one fails here afterward."""
    payload_b64, _, signature = token.partition(".")
    if not signature:
        return False
    expected = hmac.new(signing_key, payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except ValueError, UnicodeDecodeError:
        return False
    expires_at = payload.get("exp")
    return isinstance(expires_at, int) and now < expires_at


@dataclass
class LoginThrottle:
    """ "A login throttle delays 1 s per failure and at most 10 failures
    per 5 minutes per client address" (design, "Password set").

    `now` and `sleep` are injectable so a test proves the counting logic
    deterministically, without waiting on a real clock or a real
    second-long sleep; the production default (`time.monotonic`,
    `time.sleep`) is what the real login route actually uses.
    """

    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    window_s: float = 300.0
    max_failures: int = 10
    delay_s: float = 1.0
    _failures: dict[str, list[float]] = field(default_factory=dict)

    def allow(self, address: str) -> bool:
        """Whether `address` may attempt a login right now."""
        return len(self._recent_failures(address)) < self.max_failures

    def record_failure(self, address: str) -> None:
        recent = self._recent_failures(address)
        recent.append(self.now())
        self._failures[address] = recent
        self.sleep(self.delay_s)

    def _recent_failures(self, address: str) -> list[float]:
        now = self.now()
        recent = [t for t in self._failures.get(address, []) if now - t < self.window_s]
        self._failures[address] = recent
        return recent


@dataclass(frozen=True, slots=True)
class SecurityContext:
    """Everything the access-control guard and the login/logout routes
    read, attached to `app.state.security`."""

    password: str | None
    allowed_networks: tuple[str, ...]
    app_secret: bytes
    session_days: int
    now_s: Callable[[], int]
    throttle: LoginThrottle


def _signing_key(security: SecurityContext) -> bytes:
    assert security.password is not None  # only ever called once a password is configured
    return session_signing_key(security.app_secret, security.password)


def has_valid_session(request: Request, security: SecurityContext) -> bool:
    if security.password is None:
        return False
    token = request.cookies.get(SESSION_COOKIE)
    if token is None:
        return False
    return verify_session_token(_signing_key(security), token, now=security.now_s())


def issue_session_cookie(response: Response, security: SecurityContext, *, secure: bool) -> None:
    now_s = security.now_s()
    max_age = security.session_days * 86_400
    token = issue_session_token(_signing_key(security), issued_at=now_s, expires_at=now_s + max_age)
    response.set_cookie(
        SESSION_COOKIE, token, max_age=max_age, httponly=True, samesite="lax", secure=secure
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)


def safe_next_path(raw: str | None) -> str:
    """Only a same-site, local path is a safe redirect target (design's
    "local paths only" caveat on `next`) — anything else collapses to
    `/`, never an open redirect to an attacker-controlled host."""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


def _is_exempt_from_session(path: str) -> bool:
    return path == _LOGIN_PATH or path.startswith(_STATIC_PREFIX)


class AccessControlMiddleware(BaseHTTPMiddleware):
    """Enforces the LAN guard (no password) or the session requirement
    (password configured) ahead of every route except the health check,
    reading `request.app.state.security`."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: object) -> Response:
        if request.url.path == _HEALTH_CHECK_PATH:
            return await call_next(request)

        security: SecurityContext = request.app.state.security

        if security.password is None:
            host = request.client.host if request.client else None
            if not client_in_allowed_networks(host, security.allowed_networks):
                return lan_denied_response()
            return await call_next(request)

        if _is_exempt_from_session(request.url.path):
            return await call_next(request)

        if has_valid_session(request, security):
            return await call_next(request)

        return RedirectResponse(url=f"{_LOGIN_PATH}?next={request.url.path}", status_code=303)


__all__ = [
    "SESSION_COOKIE",
    "AccessControlMiddleware",
    "LoginThrottle",
    "SecurityContext",
    "clear_session_cookie",
    "client_in_allowed_networks",
    "has_valid_session",
    "issue_session_cookie",
    "issue_session_token",
    "lan_denied_response",
    "safe_next_path",
    "session_signing_key",
    "verify_password",
    "verify_session_token",
]
