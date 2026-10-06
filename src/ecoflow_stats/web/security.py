"""Access control (design, "Access control" section; access-control
specification).

Two trust models, selected by whether `ECOFLOW_STATS_PASSWORD` is set:

- **No password (default)**: every route is open except the health
  check, but a LAN guard admits only clients inside
  `ECOFLOW_STATS_ALLOWED_NETWORKS`; anyone else is told to configure a
  password. This module currently implements only this half.
- **Password configured**: the LAN guard turns off and every route
  except the health check, the login page, and static assets requires a
  valid session, issued only by the exact configured password. Lands in
  a later commit on this same branch.

CSRF and security headers (the design's other half of this section) are
a separate later slice (Phase 14 PR ii) and are not implemented here;
the one dependency it introduces (`require_csrf`) composes independently
of everything below, so nothing here needs reshaping to add it.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Sequence

    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

_HEALTH_CHECK_PATH = "/healthz"


@dataclass(frozen=True, slots=True)
class SecurityContext:
    """Everything the access-control guard reads, attached to
    `app.state.security`."""

    password: str | None
    allowed_networks: tuple[str, ...]


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


class AccessControlMiddleware(BaseHTTPMiddleware):
    """Enforces the LAN guard ahead of every route except the health
    check, reading `request.app.state.security`."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: object) -> Response:
        if request.url.path == _HEALTH_CHECK_PATH:
            return await call_next(request)

        security: SecurityContext = request.app.state.security
        if security.password is not None:
            # Session enforcement lands in a later commit on this same
            # branch; the LAN guard only ever applies to the no-password
            # default (design, "Access control").
            return await call_next(request)

        host = request.client.host if request.client else None
        if not client_in_allowed_networks(host, security.allowed_networks):
            return lan_denied_response()
        return await call_next(request)


__all__ = [
    "AccessControlMiddleware",
    "SecurityContext",
    "client_in_allowed_networks",
    "lan_denied_response",
]
