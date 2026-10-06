"""Page routes: the overview page (live-status surfaced for humans),
its HTMX-refreshed live partial, and `/preferences` for a manual
language override. Mirrors `web.routes.api.ApiContext`'s
plain-callables-and-live-objects style, attached to `app.state.pages`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ecoflow_stats.live_status.service import get_status
from ecoflow_stats.web.deps import (
    DEVICE_COOKIE,
    LANG_COOKIE,
    html_lang,
    negotiate_request_lang,
    select_device_id,
)
from ecoflow_stats.web.i18n import SUPPORTED_LANGS, load_catalogs, translator
from ecoflow_stats.web.security import (
    SecurityContext,
    clear_session_cookie,
    issue_session_cookie,
    safe_next_path,
    verify_password,
)
from ecoflow_stats.web.views import build_overview_view_model

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from ecoflow_stats.outages.model import DetectorConfig
    from ecoflow_stats.ports import OutageStore, SampleStore
    from ecoflow_stats.storage.devices import DeviceRecord

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))
_CATALOGS = load_catalogs()
_COOKIE_MAX_AGE_S = 365 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class PagesContext:
    """Everything the page routes read, attached to `app.state.pages`."""

    now: Callable[[], datetime]
    stale_threshold_s: int
    poll_interval_s: int
    detector_config: DetectorConfig
    sample_store: SampleStore
    outage_store: OutageStore
    device_records: tuple[DeviceRecord, ...]
    default_lang: str


router = APIRouter()


def _resolve_lang(request: Request, ctx: PagesContext) -> str:
    return negotiate_request_lang(
        cookie=request.cookies.get(LANG_COOKIE),
        accept_language=request.headers.get("accept-language"),
        default_lang=ctx.default_lang,
    )


def _resolve_device(request: Request, ctx: PagesContext, requested: int | None) -> int:
    device_ids = tuple(record.id for record in ctx.device_records)
    return select_device_id(
        device_ids, requested=requested, cookie_value=request.cookies.get(DEVICE_COOKIE)
    )


def _live_view(ctx: PagesContext, device_id: int) -> object:
    status = get_status(
        device_id,
        sample_store=ctx.sample_store,
        outage_store=ctx.outage_store,
        now=ctx.now(),
        stale_threshold_s=ctx.stale_threshold_s,
        poll_interval_s=ctx.poll_interval_s,
        config=ctx.detector_config,
    )
    return build_overview_view_model(
        device_records=ctx.device_records, selected_device_id=device_id, status=status
    )


@router.get("/", response_class=HTMLResponse)
def overview_page(request: Request, device: int | None = None) -> HTMLResponse:
    ctx: PagesContext = request.app.state.pages
    lang = _resolve_lang(request, ctx)
    selected_id = _resolve_device(request, ctx, device)
    view = _live_view(ctx, selected_id)
    response = TEMPLATES.TemplateResponse(
        request,
        "overview.html",
        {
            "t": translator(lang, _CATALOGS),
            "lang": lang,
            "html_lang": html_lang(lang),
            "view": view,
        },
    )
    response.set_cookie(DEVICE_COOKIE, str(selected_id), max_age=_COOKIE_MAX_AGE_S, samesite="lax")
    return response


@router.get("/partials/live", response_class=HTMLResponse)
def live_partial(request: Request, device: int | None = None) -> HTMLResponse:
    ctx: PagesContext = request.app.state.pages
    lang = _resolve_lang(request, ctx)
    selected_id = _resolve_device(request, ctx, device)
    view = _live_view(ctx, selected_id)
    return TEMPLATES.TemplateResponse(
        request, "partials/live.html", {"t": translator(lang, _CATALOGS), "view": view}
    )


@router.post("/preferences")
def set_preferences(request: Request, lang: str = Form(...)) -> RedirectResponse:
    """web-ui "Manual Language Override Persists": a POST here sets the
    `lang` cookie directly, which outranks the negotiated
    `Accept-Language` on every later request (design D12's documented
    negotiation order)."""
    ctx: PagesContext = request.app.state.pages
    effective_lang = lang if lang in SUPPORTED_LANGS else ctx.default_lang
    referer = request.headers.get("referer", "/")
    redirect = RedirectResponse(url=referer, status_code=303)
    redirect.set_cookie(LANG_COOKIE, effective_lang, max_age=_COOKIE_MAX_AGE_S, samesite="lax")
    return redirect


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None, error: bool = False) -> HTMLResponse:
    """access-control "Optional Password Guards Every Route Except the
    Health Check": the page `AccessControlMiddleware` redirects an
    unauthenticated request to."""
    ctx: PagesContext = request.app.state.pages
    lang = _resolve_lang(request, ctx)
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {
            "t": translator(lang, _CATALOGS),
            "lang": lang,
            "html_lang": html_lang(lang),
            "next": safe_next_path(next),
            "error": error,
        },
    )


@router.post("/login")
def login_submit(
    request: Request, password: str = Form(...), next: str = Form("/")
) -> RedirectResponse:
    security: SecurityContext = request.app.state.security
    target = safe_next_path(next)
    address = request.client.host if request.client else "unknown"

    if security.password is not None and security.throttle.allow(address):
        if verify_password(password, security.password):
            response = RedirectResponse(url=target, status_code=303)
            issue_session_cookie(response, security, secure=request.url.scheme == "https")
            return response
        security.throttle.record_failure(address)

    return RedirectResponse(url=f"/login?next={target}&error=1", status_code=303)


@router.post("/logout")
def logout_submit() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    return response


__all__ = ["PagesContext", "router"]
