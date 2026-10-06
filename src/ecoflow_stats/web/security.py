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

CSRF and security headers (the design's other half of this section,
Phase 14 PR ii) are implemented below: `require_csrf` is a separate
FastAPI dependency, attached per-POST-route rather than folded into
`AccessControlMiddleware`, and `SecurityHeadersMiddleware` is its own
middleware registered outermost in `app.py` -- neither needed any change
to the LAN-guard/session halves above.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from starlette.responses import Response
    from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

SESSION_COOKIE = "efs_session"
CSRF_COOKIE = "efs_csrf"
CSRF_HEADER = "x-csrf-token"
CSRF_FORM_FIELD = "csrf_token"
_HEALTH_CHECK_PATH = "/healthz"
_LOGIN_PATH = "/login"
_STATIC_PREFIX = "/static/"
_API_PREFIX = "/api/"
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)
_SECURITY_HEADERS = {
    "content-security-policy": _CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "x-frame-options": "DENY",
    # POLISH-01: defense-in-depth alongside the CSP's own
    # `frame-ancestors 'none'` above, for a legacy browser that does not
    # honor `frame-ancestors`.
}


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


def session_signing_key(app_secret: bytes, password: str, generation: int = 0) -> bytes:
    """`HMAC(app_state.secret, sha256(password) || generation)` (design,
    "Session"): changing the configured password changes this key,
    which silently invalidates every session token signed under the
    old one. `generation` (default `0`, SEC-03) does the exact same
    thing on purpose: `revoke_all_sessions` bumps it on logout, so every
    token issued under the previous generation stops verifying
    immediately, with no password change needed."""
    return hmac.new(
        app_secret, _sha256(password) + str(generation).encode("ascii"), hashlib.sha256
    ).digest()


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
    """ "At most 10 failures per 5 minutes per client address" (design,
    "Password set").

    **SEC-04 fix** (qa-report-data-01.md): a failed attempt no longer
    sleeps at all. The earlier version called a blocking `time.sleep`
    (or an injected `sleep`) directly inside `record_failure` -- since
    `login_submit` is a synchronous route, FastAPI runs it on its
    thread-pool executor, so that sleep held one of those worker
    threads hostage for its full duration; the QA report flagged this
    as a residual, untested thread-pool-exhaustion risk under enough
    concurrent bad attempts. `allow` is now the only signal the route
    needs -- once a client is over budget, the route rejects
    immediately with `429 Retry-After` (via `retry_after`) instead of
    ever blocking a worker.

    `now` is injectable so a test proves the counting logic
    deterministically, without waiting on a real clock; the production
    default (`time.monotonic`) is what the real login route uses.
    """

    now: Callable[[], float] = time.monotonic
    window_s: float = 300.0
    max_failures: int = 10
    _failures: dict[str, list[float]] = field(default_factory=dict)

    def allow(self, address: str) -> bool:
        """Whether `address` may attempt a login right now."""
        return len(self._recent_failures(address)) < self.max_failures

    def record_failure(self, address: str) -> None:
        recent = self._recent_failures(address)
        recent.append(self.now())
        self._failures[address] = recent

    def retry_after(self, address: str) -> float:
        """Seconds until `address`'s oldest counted failure falls out
        of the window and `allow` would return `True` again -- `0.0`
        when `address` isn't actually throttled right now."""
        recent = self._recent_failures(address)
        if len(recent) < self.max_failures:
            return 0.0
        return max(0.0, self.window_s - (self.now() - min(recent)))

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
    session_generation: int = 0
    """SEC-03 (qa-report-data-01.md): bumped by `revoke_all_sessions` on
    every `/logout`, and mixed into `session_signing_key` -- the only
    state that needs to change for every previously issued session
    token to stop verifying at once. Defaults to `0` (a fresh database
    that has never logged anything out), but `bootstrap.build` restores
    the persisted value on every call (SEC-07, `storage.state
    .get_session_generation`) -- `app_secret` is persisted the same
    way, so neither this field nor the secret may reset to a default on
    a restart without silently un-revoking an already-revoked
    session."""


def _signing_key(security: SecurityContext) -> bytes:
    assert security.password is not None  # only ever called once a password is configured
    return session_signing_key(security.app_secret, security.password, security.session_generation)


def revoke_all_sessions(security: SecurityContext) -> SecurityContext:
    """SEC-03: returns a new `SecurityContext` with its session
    generation bumped, so every session token issued before this call
    fails `verify_session_token` immediately afterward (`_signing_key`
    changes along with it) -- the caller swaps `request.app.state.security`
    to this returned value.

    **Single-user app, documented simplification**: this revokes every
    session for every client, not just the one that called `/logout` --
    there is only ever one real visitor to this app, so that distinction
    has no practical meaning, and bumping one shared generation is far
    simpler than tracking a revocation list per session token."""
    return replace(security, session_generation=security.session_generation + 1)


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
    """Only a same-site, local path (optionally with its own query
    string) is a safe redirect target (design's "local paths only"
    caveat on `next`) -- anything else collapses to `/`, never an open
    redirect to an attacker-controlled host.

    **UI-08 hardening**: rejects a protocol-relative "//host" as
    before, plus any backslash at all. A browser normalizes a leading
    backslash the same way it normalizes a second slash -- an
    attacker-controlled host smuggled past the "//" check alone -- and
    no real route in this app ever legitimately contains a backslash,
    so treating any as suspicious costs nothing real."""
    if not raw or not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return "/"
    return raw


def _is_exempt_from_session(path: str) -> bool:
    return path == _LOGIN_PATH or path.startswith(_STATIC_PREFIX)


def _referer_next_path(request: Request) -> str:
    """UI-09: a non-`GET` request (e.g. the language-switch form's own
    `POST /preferences`) can never safely become `next` -- re-visiting
    it with a `GET`, which is exactly what following a redirect does,
    404s or 405s instead of landing on a real page. Falls back to the
    referring page's own path and query string (the page that form was
    actually submitted from), or `/` when there is no usable,
    same-origin `Referer`."""
    referer = request.headers.get("referer")
    if not referer:
        return "/"
    parsed = urlsplit(referer)
    host = (request.headers.get("host") or "").lower()
    if parsed.netloc and parsed.netloc.lower() != host:
        return "/"  # a different origin's own page is never a safe landing target
    target = parsed.path
    if parsed.query:
        target = f"{target}?{parsed.query}"
    return safe_next_path(target)


def _next_target(request: Request) -> str:
    """UI-08/UI-09: the exact value this unauthenticated request's own
    login redirect should carry as `next`. A `GET` request's full
    path+query is always safe to replay later (whatever just rendered
    it will render it again); anything else falls back to the
    referring page instead of the mutating route itself."""
    if request.method != "GET":
        return _referer_next_path(request)
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return target


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

        if request.url.path.startswith(_API_PREFIX):
            # API-03: a JSON API client gets a `401` it can actually act
            # on, never the browser-oriented login redirect below -- a
            # page route (anything not under `/api/`) keeps that
            # redirect unchanged.
            return JSONResponse({"detail": "authentication required"}, status_code=401)

        next_target = quote(_next_target(request), safe="/")
        return RedirectResponse(url=f"{_LOGIN_PATH}?next={next_target}", status_code=303)


def new_csrf_secret() -> str:
    """A fresh random value for the `efs_csrf` cookie (design, "CSRF")."""
    return secrets.token_urlsafe(32)


def csrf_token(app_secret: bytes, cookie_value: str) -> str:
    """`HMAC(secret, cookie)` (design, "CSRF"): the value a page hands
    back to the client (a hidden field, or `hx-headers` on `<body>`) to
    prove it actually read the cookie's value -- something a forged
    cross-site page cannot do, since the cookie is `HttpOnly`."""
    return hmac.new(app_secret, cookie_value.encode("ascii"), hashlib.sha256).hexdigest()


def csrf_cookie_value(request: Request) -> tuple[str, bool]:
    """The request's existing `efs_csrf` cookie value, or a freshly
    minted one if it didn't carry one yet. The second element is whether
    a fresh value was minted, so the caller knows whether to actually set
    it on its response (`set_csrf_cookie`) -- split from that step so a
    route can compute the token for its template context before the
    `Response` object exists."""
    value = request.cookies.get(CSRF_COOKIE)
    if value is None:
        return new_csrf_secret(), True
    return value, False


def set_csrf_cookie(response: Response, value: str, *, secure: bool) -> None:
    response.set_cookie(CSRF_COOKIE, value, httponly=True, samesite="strict", secure=secure)


async def _submitted_csrf_token(request: Request) -> str | None:
    header = request.headers.get(CSRF_HEADER)
    if header is not None:
        return header
    form = await request.form()
    value = form.get(CSRF_FORM_FIELD)
    return value if isinstance(value, str) else None


async def require_csrf(request: Request) -> None:
    """FastAPI dependency: the CSRF half of the design's "Access control"
    section (Named Defect "CSRF gap"). Deliberately a dependency, not
    part of `AccessControlMiddleware` (per the slice-21 handoff note), so
    it composes independently of the LAN-guard/session halves above --
    attach it to each POST route instead.

    Rejects a present `Origin` whose host differs from `Host`, and a
    `Sec-Fetch-Site` other than `same-origin`/`none` -- the two signals a
    modern browser already attaches to a genuine cross-site request,
    regardless of any token. A token (the `X-CSRF-Token` header or a
    `csrf_token` form field) is now always required and must match
    `csrf_token(app_secret, <the efs_csrf cookie>)` exactly.

    **SEC-01 fix** (qa-report-data-01.md): the earlier version let a
    request through whenever it submitted *no* token at all, falling
    back entirely to the Origin/Sec-Fetch-Site heuristic above -- which
    itself fails open when a request carries neither header, exactly
    what a bare `curl -X POST` (no browser involved) looks like. The QA
    report proved this actually writes a real decision row. Failing
    closed here cannot block any genuine same-origin submission: every
    template that drives a state-changing route now renders the real
    token into a hidden `csrf_token` field (`login.html`, `base.html`'s
    two language-switch forms, `gap_row.html`, `legacy_row.html`), and a
    forged cross-site page can never compute a matching value -- it
    cannot read the `HttpOnly` `efs_csrf` cookie to do so.
    """
    security: SecurityContext = request.app.state.security
    host = (request.headers.get("host") or "").lower()
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc.lower() != host:
        raise HTTPException(status_code=403, detail="cross-site request rejected")

    sec_fetch_site = request.headers.get("sec-fetch-site")
    if sec_fetch_site is not None and sec_fetch_site not in ("same-origin", "none"):
        raise HTTPException(status_code=403, detail="cross-site request rejected")

    submitted = await _submitted_csrf_token(request)
    cookie_value = request.cookies.get(CSRF_COOKIE)
    if (
        submitted is None
        or cookie_value is None
        or not hmac.compare_digest(submitted, csrf_token(security.app_secret, cookie_value))
    ):
        raise HTTPException(status_code=403, detail="invalid CSRF token")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attaches the CSP and related headers (design, "Headers") to every
    response, including the LAN-guard/login-redirect responses
    `AccessControlMiddleware` itself returns -- kept as its own
    middleware, registered outermost in `app.py`, rather than reshaping
    that existing middleware."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: object) -> Response:
        try:
            response = await call_next(request)
        except Exception:
            # SEC-05: an unhandled exception deep inside a route
            # propagates straight past every other middleware's own
            # response-decoration step to Starlette's bare default 500,
            # which carries none of this app's security headers.
            # `SecurityHeadersMiddleware` is registered outermost
            # specifically so this `except` is the LAST point before
            # the response leaves the app -- catching here, rather than
            # with a registered FastAPI exception handler, avoids that
            # handler's response being sent once deep inside the stack
            # and then this same exception still unwinding through
            # every enclosing `BaseHTTPMiddleware`'s own task group
            # regardless (a real interaction in this Starlette version,
            # observed while first wiring this fix as a handler
            # instead). Body/status kept identical to Starlette's own
            # previous default -- still never a stack trace or
            # internals.
            logger.exception("unhandled exception handling %s %s", request.method, request.url.path)
            if request.url.path.startswith(_API_PREFIX):
                response = PlainTextResponse("Internal Server Error", status_code=500)
            else:
                # UI-13 (qa-report-ui-01.md): a page route's own crash
                # renders the same designed, translated HTML error page
                # a 404 does, never a bare "Internal Server Error"
                # plain-text body. Deferred import, not a module-level
                # one: `web.routes.errors` itself imports this module
                # for `SecurityContext`/`csrf_token`/`has_valid_session`,
                # so importing it at module scope here would be a real
                # cycle; by the time `dispatch` actually runs, both
                # modules are already fully loaded. Falls back to the
                # same plain-text body if rendering the error page
                # itself somehow fails -- this is the last point before
                # the response leaves the app, so it must not raise.
                try:
                    from ecoflow_stats.web.routes.errors import render_error_page

                    response = render_error_page(request, 500)
                except Exception:
                    logger.exception("rendering the 500 error page itself failed")
                    response = PlainTextResponse("Internal Server Error", status_code=500)
        for name, value in _SECURITY_HEADERS.items():
            response.headers[name] = value
        if not request.url.path.startswith(_STATIC_PREFIX):
            # SEC-06: every page and API response gets `no-store` --
            # combined with SEC-03 (session not revoked on logout), a
            # cacheable response risks a shared machine's browser
            # back/forward cache still showing it after logout.
            # `/static/*` is exempt: those assets are genuinely safe,
            # and meant, to cache.
            response.headers["cache-control"] = "no-store"
        return response


__all__ = [
    "CSRF_COOKIE",
    "CSRF_FORM_FIELD",
    "CSRF_HEADER",
    "SESSION_COOKIE",
    "AccessControlMiddleware",
    "LoginThrottle",
    "SecurityContext",
    "SecurityHeadersMiddleware",
    "clear_session_cookie",
    "client_in_allowed_networks",
    "csrf_cookie_value",
    "csrf_token",
    "has_valid_session",
    "issue_session_cookie",
    "issue_session_token",
    "lan_denied_response",
    "new_csrf_secret",
    "require_csrf",
    "revoke_all_sessions",
    "safe_next_path",
    "session_signing_key",
    "set_csrf_cookie",
    "verify_password",
    "verify_session_token",
]
