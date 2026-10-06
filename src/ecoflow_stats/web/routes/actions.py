"""Gap and phantom review actions (web-ui "Gap and Phantom Review Flow",
Phase 16): the HTMX-driven list `outages.html`'s gap-review section
expands into, and the three mutating review routes the design's page
table documents.

Every route here reads `request.app.state.api` (`web.routes.api.ApiContext`,
already wired to the real `OutageStore`/`DecisionStore`/timezone) and
`request.app.state.application` (the composition root's `Application`,
for a `LegacyStore` -- `ApiContext` has no `legacy_store` field and
adding one would mean editing `web/app.py`'s `_build_api_context`, which
is outside this slice's declared edit surface) -- the same cross-context
read style `web.routes.pages.outages_page` already established for
`decision_store`/`tz` (apply-progress-batch14 design decision #4).

Decision logic itself is never duplicated here (task 16.3 REFACTOR):
every route calls `outages.service.record_decision`/`undo_decision`
directly and only translates its `ValueError` into an HTTP 400.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ecoflow_stats.outages.resolve import decided_gaps, unresolved_gaps
from ecoflow_stats.outages.service import record_decision, undo_decision
from ecoflow_stats.storage.legacy import LegacyStore
from ecoflow_stats.web.deps import LANG_COOKIE, negotiate_request_lang
from ecoflow_stats.web.i18n import load_catalogs, translator
from ecoflow_stats.web.security import (
    SecurityContext,
    csrf_cookie_value,
    csrf_token,
    require_csrf,
    set_csrf_cookie,
)
from ecoflow_stats.web.views import format_local_dt

if TYPE_CHECKING:
    from collections.abc import Callable

    from ecoflow_stats.outages.model import Gap
    from ecoflow_stats.storage.devices import DeviceRecord
    from ecoflow_stats.storage.legacy import LegacyOutage
    from ecoflow_stats.web.routes.api import ApiContext
    from ecoflow_stats.web.routes.pages import PagesContext

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))
TEMPLATES.env.globals["format_local_dt"] = format_local_dt
_CATALOGS = load_catalogs()
_DEFAULT_RANGE_S = 7 * 24 * 60 * 60
"""Trailing-7-days default when `from`/`to` are omitted, matching the
outages page's and the `/api/v1` routes' own default (duplicated here
since both modules are outside this slice's declared edit surface)."""


def _resolve_device_id(device_records: tuple[DeviceRecord, ...], requested: int | None) -> int:
    """An explicitly requested, configured device id wins; otherwise the
    first configured device (same fallback `web.routes.api._resolve_device_id`
    already uses -- duplicated here for the same reason as the range
    default above)."""
    device_ids = tuple(record.id for record in device_records)
    if requested is not None and requested in device_ids:
        return requested
    return device_ids[0]


def _require_device(device_records: tuple[DeviceRecord, ...], device_id: int) -> None:
    if device_id not in {record.id for record in device_records}:
        raise HTTPException(status_code=404, detail="unknown device")


def _resolve_lang(request: Request, default_lang: str) -> str:
    return negotiate_request_lang(
        cookie=request.cookies.get(LANG_COOKIE),
        accept_language=request.headers.get("accept-language"),
        default_lang=default_lang,
    )


def _translator_for(request: Request) -> Callable[[str], str]:
    pages_ctx: PagesContext = request.app.state.pages
    lang = _resolve_lang(request, pages_ctx.default_lang)
    return translator(lang, _CATALOGS)


def _render_gap_row(
    *,
    gap: Gap,
    device_id: int,
    tz: str,
    t: Callable[[str], str],
    decision: dict[str, object] | None,
    csrf_token: str,
) -> str:
    template = TEMPLATES.get_template("partials/gap_row.html")
    return template.render(
        gap=gap, device_id=device_id, tz=tz, t=t, decision=decision, csrf_token=csrf_token
    )


def _render_legacy_row(
    *,
    entry: LegacyOutage,
    device_id: int,
    tz: str,
    t: Callable[[str], str],
    decision: dict[str, object] | None,
    csrf_token: str,
) -> str:
    template = TEMPLATES.get_template("partials/legacy_row.html")
    return template.render(
        entry=entry, device_id=device_id, tz=tz, t=t, decision=decision, csrf_token=csrf_token
    )


def _find_gap(ctx: ApiContext, device_id: int, gap_start: int) -> Gap | None:
    """The one gap starting exactly at `gap_start`, or `None`.
    `OutageStore.gaps` is a range-overlap query (no exact-start lookup
    exists), so this fetches the narrow `[gap_start, gap_start]` window
    -- which may also return an earlier, still-open gap covering this
    instant -- and filters to the exact match."""
    candidates = ctx.outage_store.gaps(device_id, gap_start, gap_start)
    return next((gap for gap in candidates if gap.start_ts == gap_start), None)


def _legacy_store(request: Request) -> LegacyStore:
    application = request.app.state.application
    return LegacyStore(application.database.writer)


def _csrf_context(request: Request) -> tuple[str, str, bool]:
    """The real `efs_csrf` cookie value (minting one if the request
    doesn't carry one yet, exactly like `web.routes.pages`'s GET routes)
    plus the token derived from it (SEC-01) -- every route here re-
    renders a `gap_row.html`/`legacy_row.html` row, and each of those
    now has its own mutating forms that need a real token to submit
    again. Returns `(cookie_value, token, is_new)` so the caller can
    render with `token` before the `Response` object exists, and only
    then decide whether `set_csrf_cookie` needs to run on it."""
    security: SecurityContext = request.app.state.security
    cookie_value, is_new = csrf_cookie_value(request)
    return cookie_value, csrf_token(security.app_secret, cookie_value), is_new


def _set_csrf_cookie_if_new(
    request: Request, response: HTMLResponse, cookie_value: str, is_new: bool
) -> None:
    if is_new:
        set_csrf_cookie(response, cookie_value, secure=request.url.scheme == "https")


router = APIRouter()


@router.get("/outages/gaps", response_class=HTMLResponse)
def gap_review_list(
    request: Request,
    device: int | None = None,
    start: int | None = Query(None, alias="from"),
    end: int | None = Query(None, alias="to"),
) -> HTMLResponse:
    """web-ui "Gap and Phantom Review Flow" (scenario "A gap can be
    reviewed and resolved"): the HTMX fragment `outages.html`'s
    gap-review section expands into, listing every gap in range with
    the before/after evidence `outages.evidence.make_gap` already
    computed in Phase 11 -- reused via `resolve.unresolved_gaps`/
    `resolve.decided_gaps`, never recomputed.

    Lists a still-unresolved gap's confirm/reject forms AND a decided
    gap's recorded verdict with its own undo form (UI-12, qa-report-
    ui-01.md: a decided gap used to disappear from this very list the
    moment it was decided -- the route only ever fetched
    `unresolved_gaps` -- making its undo unreachable after a reload,
    since the undo form itself lives in this list's own rendered row,
    not anywhere else)."""
    ctx: ApiContext = request.app.state.api
    device_id = _resolve_device_id(ctx.device_records, device)
    range_end = end if end is not None else int(ctx.now().timestamp())
    range_start = start if start is not None else range_end - _DEFAULT_RANGE_S
    gaps = ctx.outage_store.gaps(device_id, range_start, range_end)
    decisions = ctx.decision_store.active(device_id) if ctx.decision_store is not None else []
    pending = unresolved_gaps(gaps, decisions, range_end)
    decided = decided_gaps(gaps, decisions, range_end)
    t = _translator_for(request)
    cookie_value, token, is_new = _csrf_context(request)
    if not pending and not decided:
        response = HTMLResponse(f"<p>{t('outages.gap_review.empty')}</p>")
    else:
        pending_rows = (
            _render_gap_row(
                gap=gap, device_id=device_id, tz=ctx.tz, t=t, decision=None, csrf_token=token
            )
            for gap in pending
        )
        decided_rows = (
            _render_gap_row(
                gap=gap,
                device_id=device_id,
                tz=ctx.tz,
                t=t,
                decision={"id": decision.id, "verdict": decision.verdict},
                csrf_token=token,
            )
            for gap, decision in decided
        )
        response = HTMLResponse("".join(pending_rows) + "".join(decided_rows))
    _set_csrf_cookie_if_new(request, response, cookie_value, is_new)
    return response


@router.post("/outages/gaps/{gap_start}/decision", dependencies=[Depends(require_csrf)])
def decide_gap(
    request: Request,
    gap_start: int,
    device: int = Form(...),
    verdict: str = Form(...),
) -> HTMLResponse:
    """Scenario "A gap can be reviewed and resolved": confirm (`verdict
    ="outage"`) or reject (`verdict="no_outage"`) one gap, delegating
    entirely to `outages.service.record_decision` (task 16.3 REFACTOR:
    no decision logic duplicated here -- an invalid verdict's
    `ValueError` is only translated to an HTTP 400, never re-validated)."""
    ctx: ApiContext = request.app.state.api
    _require_device(ctx.device_records, device)
    if ctx.decision_store is None:
        raise HTTPException(status_code=503, detail="decisions are not available")
    gap = _find_gap(ctx, device, gap_start)
    if gap is None:
        raise HTTPException(status_code=404, detail="unknown gap")
    end_ts = gap.end_ts if gap.end_ts is not None else int(ctx.now().timestamp())
    try:
        decision_id = record_decision(
            device,
            target="gap",
            start_ts=gap_start,
            end_ts=end_ts,
            verdict=verdict,  # type: ignore[arg-type]
            decision_store=ctx.decision_store,
            now=ctx.now(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    t = _translator_for(request)
    cookie_value, token, is_new = _csrf_context(request)
    html = _render_gap_row(
        gap=gap,
        device_id=device,
        tz=ctx.tz,
        t=t,
        decision={"id": decision_id, "verdict": verdict},
        csrf_token=token,
    )
    response = HTMLResponse(html, headers={"HX-Trigger": "outages-changed"})
    _set_csrf_cookie_if_new(request, response, cookie_value, is_new)
    return response


@router.post("/outages/legacy/{start}/decision", dependencies=[Depends(require_csrf)])
def decide_legacy(
    request: Request,
    start: int,
    device: int = Form(...),
    verdict: str = Form(...),
) -> HTMLResponse:
    """Scenario "A suspected phantom can be un-flagged": the single-row
    action flow the spec requires. `storage.legacy.LegacyStore` has no
    range query (tracked gap since apply-progress-batch13), so this
    route only ever needs the exact `get(device_id, start_ts)` lookup it
    already has -- there is deliberately no "browse every phantom" list
    here (see Known gaps in this batch's apply-progress for the full
    reasoning)."""
    ctx: ApiContext = request.app.state.api
    _require_device(ctx.device_records, device)
    if ctx.decision_store is None:
        raise HTTPException(status_code=503, detail="decisions are not available")
    entry = _legacy_store(request).get(device, start)
    if entry is None:
        raise HTTPException(status_code=404, detail="unknown legacy outage")
    end_ts = entry.end_ts if entry.end_ts is not None else entry.start_ts
    try:
        decision_id = record_decision(
            device,
            target="legacy",
            start_ts=entry.start_ts,
            end_ts=end_ts,
            verdict=verdict,  # type: ignore[arg-type]
            decision_store=ctx.decision_store,
            now=ctx.now(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    t = _translator_for(request)
    cookie_value, token, is_new = _csrf_context(request)
    html = _render_legacy_row(
        entry=entry,
        device_id=device,
        tz=ctx.tz,
        t=t,
        decision={"id": decision_id, "verdict": verdict},
        csrf_token=token,
    )
    response = HTMLResponse(html, headers={"HX-Trigger": "outages-changed"})
    _set_csrf_cookie_if_new(request, response, cookie_value, is_new)
    return response


@router.post("/decisions/{decision_id}/undo", dependencies=[Depends(require_csrf)])
def undo(
    request: Request,
    decision_id: int,
    device: int = Form(...),
    target: str = Form(...),
    start: int = Form(...),
) -> HTMLResponse:
    """`POST /decisions/{id}/undo`: reverts to the prior (unresolved)
    state, delegating to `outages.service.undo_decision`. `device`,
    `target`, and `start` ride along as hidden fields on the row that
    rendered the undo button -- the row already knows exactly what it is
    undoing, so this never needs a `DecisionStore.get(id)` lookup that
    does not exist."""
    ctx: ApiContext = request.app.state.api
    _require_device(ctx.device_records, device)
    if ctx.decision_store is None:
        raise HTTPException(status_code=503, detail="decisions are not available")
    undo_decision(decision_id, decision_store=ctx.decision_store)
    t = _translator_for(request)
    cookie_value, token, is_new = _csrf_context(request)
    if target == "gap":
        gap = _find_gap(ctx, device, start)
        if gap is None:
            raise HTTPException(status_code=404, detail="unknown gap")
        html = _render_gap_row(
            gap=gap, device_id=device, tz=ctx.tz, t=t, decision=None, csrf_token=token
        )
    elif target == "legacy":
        entry = _legacy_store(request).get(device, start)
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown legacy outage")
        html = _render_legacy_row(
            entry=entry, device_id=device, tz=ctx.tz, t=t, decision=None, csrf_token=token
        )
    else:
        raise HTTPException(status_code=400, detail=f"unknown undo target {target!r}")
    response = HTMLResponse(html, headers={"HX-Trigger": "outages-changed"})
    _set_csrf_cookie_if_new(request, response, cookie_value, is_new)
    return response


__all__ = ["decide_gap", "decide_legacy", "gap_review_list", "router", "undo"]
