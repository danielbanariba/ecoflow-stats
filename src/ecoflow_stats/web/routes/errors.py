"""Designed, translated HTML error pages for page routes (UI-13,
qa-report-ui-01.md): before this fix, a `404` on any page route
(anything not under `/api/`) returned FastAPI's raw JSON `{"detail":
...}` body, and an unhandled exception returned a bare `Internal
Server Error` plain-text `500` (`web.security.SecurityHeadersMiddleware`'s
own crash-catch, SEC-05) -- neither matched the rest of this app's own
design or language. `/api/v1/*` keeps its existing JSON/plain-text
error bodies unchanged, so a non-browser client still gets a body it
can parse.

Kept as its own small module, not inside `web.routes.pages` (which
already imports `web.security`): `SecurityHeadersMiddleware.dispatch`
needs to render the `500` page too (registering a FastAPI `Exception`
handler instead was already tried and reverted in this codebase --
see that class's own docstring for why), and importing
`web.routes.pages` from `web.security` would import `web.security`
right back.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse

from ecoflow_stats.web.deps import LANG_COOKIE, html_lang, negotiate_request_lang
from ecoflow_stats.web.i18n import load_catalogs, translator
from ecoflow_stats.web.security import (
    SecurityContext,
    csrf_cookie_value,
    csrf_token,
    has_valid_session,
    set_csrf_cookie,
)

if TYPE_CHECKING:
    from fastapi import Request

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))
_CATALOGS = load_catalogs()

_TITLE_KEYS = {400: "error.400.title", 404: "error.404.title", 500: "error.500.title"}
_MESSAGE_KEYS = {400: "error.400.message", 404: "error.404.message", 500: "error.500.message"}


def _show_logout(request: Request, security: SecurityContext) -> bool:
    """Mirrors `web.routes.pages._show_logout` exactly (UI-10's rule):
    duplicated rather than imported, the same small-resolver-duplication
    precedent `_resolve_device_id` already sets across `web.routes.api`/
    `web.routes.actions`/`web.deps` -- importing `web.routes.pages`
    itself from here would be the exact cycle this module's own
    docstring explains is unsafe."""
    return security.password is not None and has_valid_session(request, security)


def render_error_page(request: Request, status_code: int) -> HTMLResponse:
    """The one error page every page route's `404`/`500` renders
    through: registered as the `StarletteHTTPException` handler in
    `web.app.create_app` for `404`, and called directly from
    `web.security.SecurityHeadersMiddleware.dispatch`'s crash-catch for
    `500`."""
    security: SecurityContext = request.app.state.security
    default_lang = request.app.state.pages.default_lang
    lang = negotiate_request_lang(
        cookie=request.cookies.get(LANG_COOKIE),
        accept_language=request.headers.get("accept-language"),
        default_lang=default_lang,
    )
    t = translator(lang, _CATALOGS)
    cookie_value, is_new = csrf_cookie_value(request)
    response = TEMPLATES.TemplateResponse(
        request,
        "error.html",
        {
            "t": t,
            "lang": lang,
            "html_lang": html_lang(lang),
            "error_title": t(_TITLE_KEYS.get(status_code, "error.500.title")),
            "error_message": t(_MESSAGE_KEYS.get(status_code, "error.500.message")),
            "csrf_token": csrf_token(security.app_secret, cookie_value),
            "active_nav": "",
            "show_logout": _show_logout(request, security),
        },
        status_code=status_code,
    )
    if is_new:
        set_csrf_cookie(response, cookie_value, secure=request.url.scheme == "https")
    return response


__all__ = ["render_error_page"]
