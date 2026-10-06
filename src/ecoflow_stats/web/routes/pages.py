"""Page routes: the overview page (live-status surfaced for humans),
its HTMX-refreshed live partial, and `/preferences` for a manual
language override. Mirrors `web.routes.api.ApiContext`'s
plain-callables-and-live-objects style, attached to `app.state.pages`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ecoflow_stats.live_status.service import get_status
from ecoflow_stats.outages.aggregates import compute_aggregates
from ecoflow_stats.outages.resolve import resolve, unresolved_gaps
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
    csrf_cookie_value,
    csrf_token,
    issue_session_cookie,
    require_csrf,
    safe_next_path,
    set_csrf_cookie,
    verify_password,
)
from ecoflow_stats.web.views import (
    build_outages_view_model,
    build_overview_view_model,
    format_local_dt,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from ecoflow_stats.outages.model import DetectorConfig
    from ecoflow_stats.ports import OutageStore, SampleStore
    from ecoflow_stats.storage.devices import DeviceRecord

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))
TEMPLATES.env.globals["format_local_dt"] = format_local_dt
_CATALOGS = load_catalogs()
_COOKIE_MAX_AGE_S = 365 * 24 * 60 * 60
_OUTAGES_DEFAULT_RANGE_S = 7 * 24 * 60 * 60
"""Trailing-7-days default when `from`/`to` are omitted -- a dashboard-
sensible default, not a domain rule (matches `web.routes.api`'s own
`_DEFAULT_RANGE_S`, duplicated here since that module is outside this
slice's declared edit surface)."""


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
    security: SecurityContext = request.app.state.security
    lang = _resolve_lang(request, ctx)
    selected_id = _resolve_device(request, ctx, device)
    view = _live_view(ctx, selected_id)
    csrf_cookie, csrf_cookie_is_new = csrf_cookie_value(request)
    response = TEMPLATES.TemplateResponse(
        request,
        "overview.html",
        {
            "t": translator(lang, _CATALOGS),
            "lang": lang,
            "html_lang": html_lang(lang),
            "view": view,
            "csrf_token": csrf_token(security.app_secret, csrf_cookie),
        },
    )
    response.set_cookie(DEVICE_COOKIE, str(selected_id), max_age=_COOKIE_MAX_AGE_S, samesite="lax")
    if csrf_cookie_is_new:
        set_csrf_cookie(response, csrf_cookie, secure=request.url.scheme == "https")
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


@router.get("/outages", response_class=HTMLResponse)
def outages_page(
    request: Request,
    device: int | None = None,
    start: int | None = Query(None, alias="from"),
    end: int | None = Query(None, alias="to"),
) -> HTMLResponse:
    """outages page (design part 3 "Web"): summary, server-rendered
    weekday/hour heatmap, events table, and the gap-review queue count.

    Reuses the same pure reconciliation pipeline `web.routes.api`'s
    aggregate routes already call (`resolve`, `compute_aggregates`,
    `unresolved_gaps`) so the page and the JSON API never disagree.
    `decision_store`/`tz` are read from `request.app.state.api` --
    already wired to the real `DecisionStore`/configured timezone by
    `web.app._build_api_context` -- rather than duplicating that wiring
    onto `PagesContext`, the same cross-context read style this module
    already uses for `request.app.state.security`."""
    ctx: PagesContext = request.app.state.pages
    api_ctx = request.app.state.api
    lang = _resolve_lang(request, ctx)
    selected_id = _resolve_device(request, ctx, device)

    range_end = end if end is not None else int(ctx.now().timestamp())
    range_start = start if start is not None else range_end - _OUTAGES_DEFAULT_RANGE_S

    events = ctx.outage_store.events(selected_id, range_start, range_end)
    gaps = ctx.outage_store.gaps(selected_id, range_start, range_end)
    decisions = (
        api_ctx.decision_store.active(selected_id) if api_ctx.decision_store is not None else []
    )
    view = resolve(detected=events, gaps=gaps, legacy=(), decisions=decisions, range_end=range_end)
    aggregates = compute_aggregates(
        outages=view.outages, range_start=range_start, range_end=range_end, tz=api_ctx.tz
    )
    pending_gaps = unresolved_gaps(gaps, decisions, range_end)

    view_model = build_outages_view_model(
        device_records=ctx.device_records,
        selected_device_id=selected_id,
        aggregates=aggregates,
        outages=view.outages,
        briefs=view.briefs,
        gaps=gaps,
        gap_review_count=len(pending_gaps),
        range_start=range_start,
        range_end=range_end,
        mains_strip_src=f"/api/v1/mains-strip?device={selected_id}&from={range_start}&to={range_end}",
    )
    response = TEMPLATES.TemplateResponse(
        request,
        "outages.html",
        {
            "t": translator(lang, _CATALOGS),
            "lang": lang,
            "html_lang": html_lang(lang),
            "view": view_model,
            "tz": api_ctx.tz,
        },
    )
    response.set_cookie(DEVICE_COOKIE, str(selected_id), max_age=_COOKIE_MAX_AGE_S, samesite="lax")
    return response


@router.post("/preferences", dependencies=[Depends(require_csrf)])
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
    security: SecurityContext = request.app.state.security
    lang = _resolve_lang(request, ctx)
    csrf_cookie, csrf_cookie_is_new = csrf_cookie_value(request)
    response = TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {
            "t": translator(lang, _CATALOGS),
            "lang": lang,
            "html_lang": html_lang(lang),
            "next": safe_next_path(next),
            "error": error,
            "csrf_token": csrf_token(security.app_secret, csrf_cookie),
        },
    )
    if csrf_cookie_is_new:
        set_csrf_cookie(response, csrf_cookie, secure=request.url.scheme == "https")
    return response


@router.post("/login", dependencies=[Depends(require_csrf)])
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


@router.post("/logout", dependencies=[Depends(require_csrf)])
def logout_submit() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    return response


__all__ = ["PagesContext", "router"]
