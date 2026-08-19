from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from aiohttp import web
from sqlalchemy import select

from app.config import settings
from app.db.models import Company, IssueType, Ticket, TicketStatus
from app.db.session import SessionLocal
from app.services.companies import upsert_company
from app.services.monitor import check_all_companies, check_company, ticket_age_days
from app.services.rusprofile_client import RusprofileClient, normalize_inn

logger = logging.getLogger(__name__)

_check_all_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "checked": 0,
    "total": 0,
    "error": None,
}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _company_payload(company: Company) -> dict[str, Any]:
    return {
        "id": company.id,
        "inn": company.inn,
        "ogrn": company.ogrn,
        "name": company.name,
        "short_name": company.short_name,
        "status_text": company.status_text,
        "unreliable_address": bool(company.unreliable_address),
        "unreliable_director": bool(company.unreliable_director),
        "unreliable_founder": bool(company.unreliable_founder),
        "is_liquidating": bool(company.is_liquidating),
        "is_liquidated": bool(company.is_liquidated),
        "is_active": bool(company.is_active),
        "last_checked_at": _iso(company.last_checked_at),
        "last_error": company.last_error,
        "rusprofile_url": company.rusprofile_url,
    }


def _ticket_payload(ticket: Ticket, company: Company | None) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "company_id": ticket.company_id,
        "company_name": (company.short_name or company.name) if company else None,
        "company_inn": company.inn if company else None,
        "issue_type": ticket.issue_type,
        "status": ticket.status,
        "title": ticket.title,
        "details": ticket.details,
        "age_days": ticket_age_days(ticket.created_at),
        "created_at": _iso(ticket.created_at),
        "closed_at": _iso(ticket.closed_at),
    }


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path in {"/health", "/"}:
        return await handler(request)
    expected = (settings.crm_api_token or "").strip()
    if not expected:
        return web.json_response({"detail": "CRM API token is not configured"}, status=503)
    auth = request.headers.get("Authorization", "")
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = (request.headers.get("X-CRM-Token") or "").strip()
    if token != expected:
        return web.json_response({"detail": "Unauthorized"}, status=401)
    return await handler(request)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def list_companies(_: web.Request) -> web.Response:
    async with SessionLocal() as session:
        rows = (
            await session.scalars(select(Company).order_by(Company.id.asc()))
        ).all()
    return web.json_response({"items": [_company_payload(row) for row in rows]})


async def add_companies(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"detail": "Invalid JSON"}, status=400)
    raw_inns = body.get("inns") or []
    if not isinstance(raw_inns, list) or not raw_inns:
        return web.json_response({"detail": "inns is required"}, status=422)
    check_new = bool(body.get("check_new", True))
    inns: list[str] = []
    for value in raw_inns:
        inn = normalize_inn(str(value))
        if inn and inn not in inns:
            inns.append(inn)
    if not inns:
        return web.json_response({"detail": "No valid INN"}, status=422)

    client: RusprofileClient = request.app["client"]
    ok = 0
    fail = 0
    added = 0
    errors: list[str] = []
    new_ids: list[int] = []
    async with SessionLocal() as session:
        for inn in inns:
            result = await client.resolve_inn(inn)
            if not result.ogrn:
                fail += 1
                errors.append(f"{inn}: {result.error or 'not found'}")
                continue
            company, is_new = await upsert_company(session, result.ogrn, inn=inn, name=result.name)
            await session.commit()
            await session.refresh(company)
            ok += 1
            if is_new:
                added += 1
                new_ids.append(company.id)
        if check_new:
            for company_id in new_ids:
                company = await session.get(Company, company_id)
                if company is None:
                    continue
                await check_company(session, client, company)
    return web.json_response(
        {"ok": ok, "fail": fail, "added": added, "errors": errors[:50]},
    )


async def patch_company(request: web.Request) -> web.Response:
    company_id = int(request.match_info["company_id"])
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"detail": "Invalid JSON"}, status=400)
    async with SessionLocal() as session:
        company = await session.get(Company, company_id)
        if company is None:
            return web.json_response({"detail": "Company not found"}, status=404)
        if "is_active" in body:
            company.is_active = bool(body["is_active"])
        if body.get("inn"):
            inn = normalize_inn(str(body["inn"]))
            if inn:
                company.inn = inn
        if body.get("name"):
            company.name = str(body["name"]).strip()
            company.short_name = company.name
        await session.commit()
        await session.refresh(company)
        return web.json_response(_company_payload(company))


async def check_one(request: web.Request) -> web.Response:
    company_id = int(request.match_info["company_id"])
    client: RusprofileClient = request.app["client"]
    async with SessionLocal() as session:
        company = await session.get(Company, company_id)
        if company is None:
            return web.json_response({"detail": "Company not found"}, status=404)
        alerts = await check_company(session, client, company)
        await session.refresh(company)
        return web.json_response({"company": _company_payload(company), "alerts": alerts})


async def _run_check_all(client: RusprofileClient) -> None:
    _check_all_state.update(
        {
            "running": True,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "checked": 0,
            "error": None,
        }
    )
    try:
        async with SessionLocal() as session:
            companies = (
                await session.scalars(select(Company).where(Company.is_active.is_(True)).order_by(Company.id))
            ).all()
            _check_all_state["total"] = len(companies)
            for company in companies:
                await check_company(session, client, company)
                _check_all_state["checked"] = int(_check_all_state["checked"]) + 1
    except Exception as exc:
        logger.exception("CRM check-all failed")
        _check_all_state["error"] = str(exc)
    finally:
        _check_all_state["running"] = False
        _check_all_state["finished_at"] = datetime.now(timezone.utc).isoformat()


async def check_all(request: web.Request) -> web.Response:
    if _check_all_state["running"]:
        return web.json_response({"started": False, "job": _check_all_state}, status=202)
    client: RusprofileClient = request.app["client"]
    asyncio.create_task(_run_check_all(client))
    return web.json_response({"started": True, "job": _check_all_state}, status=202)


async def check_all_status(_: web.Request) -> web.Response:
    return web.json_response({"job": _check_all_state})


async def list_tickets(request: web.Request) -> web.Response:
    issue_type = request.query.get("issue_type") or None
    status = request.query.get("status") or TicketStatus.IN_PROGRESS
    filters = []
    if status:
        filters.append(Ticket.status == status)
    if issue_type and issue_type != "all":
        filters.append(Ticket.issue_type == issue_type)
    async with SessionLocal() as session:
        stmt = select(Ticket)
        if filters:
            stmt = stmt.where(*filters)
        stmt = stmt.order_by(Ticket.created_at.asc())
        tickets = (await session.scalars(stmt)).all()
        company_ids = {ticket.company_id for ticket in tickets}
        companies = {}
        if company_ids:
            rows = (await session.scalars(select(Company).where(Company.id.in_(company_ids)))).all()
            companies = {row.id: row for row in rows}
        items = [_ticket_payload(ticket, companies.get(ticket.company_id)) for ticket in tickets]
    return web.json_response({"items": items})


async def create_ticket(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"detail": "Invalid JSON"}, status=400)
    company_id = body.get("company_id")
    issue_type = str(body.get("issue_type") or "").strip()
    title = str(body.get("title") or "").strip()
    details = (body.get("details") or None)
    if details is not None:
        details = str(details).strip() or None
    if not company_id or not title:
        return web.json_response({"detail": "company_id and title are required"}, status=422)
    if issue_type not in {item.value for item in IssueType}:
        return web.json_response({"detail": "Unknown issue_type"}, status=422)
    async with SessionLocal() as session:
        company = await session.get(Company, int(company_id))
        if company is None:
            return web.json_response({"detail": "Company not found"}, status=404)
        ticket = Ticket(
            company_id=company.id,
            issue_type=issue_type,
            status=TicketStatus.IN_PROGRESS,
            title=title,
            details=details,
            created_by=0,
        )
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        return web.json_response(_ticket_payload(ticket, company), status=201)


async def heal_ticket(request: web.Request) -> web.Response:
    ticket_id = int(request.match_info["ticket_id"])
    async with SessionLocal() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            return web.json_response({"detail": "Ticket not found"}, status=404)
        if ticket.status != TicketStatus.IN_PROGRESS:
            return web.json_response({"detail": "Already closed"}, status=409)
        ticket.status = TicketStatus.HEALED
        ticket.closed_by = 0
        ticket.closed_at = datetime.now(timezone.utc)
        await session.commit()
        company = await session.get(Company, ticket.company_id)
        return web.json_response(_ticket_payload(ticket, company))


def create_crm_app(client: RusprofileClient) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["client"] = client
    app.router.add_get("/health", health)
    app.router.add_get("/api/v1/companies", list_companies)
    app.router.add_post("/api/v1/companies/inns", add_companies)
    app.router.add_get("/api/v1/companies/check-all", check_all_status)
    app.router.add_post("/api/v1/companies/check-all", check_all)
    app.router.add_patch("/api/v1/companies/{company_id}", patch_company)
    app.router.add_post("/api/v1/companies/{company_id}/check", check_one)
    app.router.add_get("/api/v1/tickets", list_tickets)
    app.router.add_post("/api/v1/tickets", create_ticket)
    app.router.add_post("/api/v1/tickets/{ticket_id}/heal", heal_ticket)
    return app


async def start_crm_api(client: RusprofileClient) -> web.AppRunner | None:
    token = (settings.crm_api_token or "").strip()
    if not token:
        logger.info("CRM HTTP API disabled (CRM_API_TOKEN empty)")
        return None
    app = create_crm_app(client)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.crm_api_host, settings.crm_api_port)
    await site.start()
    logger.info("CRM HTTP API listening on %s:%s", settings.crm_api_host, settings.crm_api_port)
    return runner
