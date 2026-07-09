import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CheckResult, Company, IssueType, Ticket, TicketStatus
from app.parser.rusprofile import CompanySnapshot
from app.services.rusprofile_client import RusprofileClient, rusprofile_url

logger = logging.getLogger(__name__)

OGRE_RE = re.compile(r"^\d{13,15}$")
INN_RE = re.compile(r"^\d{10,12}$")
URL_RE = re.compile(r"rusprofile\.ru/(?:id|search)/(\d{13,15})")


def normalize_ogrn(value: str) -> str | None:
    digits = re.sub(r"\D", "", value.strip())
    return digits if OGRE_RE.match(digits) else None


def extract_ogrn_from_text(text: str) -> str | None:
    text = text.strip()
    m = URL_RE.search(text)
    if m:
        return m.group(1)
    digits = re.sub(r"\D", "", text)
    if OGRE_RE.match(digits):
        return digits
    return None


def issue_label(issue_type: str) -> str:
    return {
        IssueType.ADDRESS: "Недостоверность адреса",
        IssueType.DIRECTOR: "Недостоверность должностного лица",
        IssueType.FOUNDER: "Недостоверность учредителя",
        IssueType.LIQUIDATION: "Ликвидация / исключение из ЕГРЮЛ",
        IssueType.OTHER: "Прочее",
    }.get(issue_type, issue_type)


def company_display(company: Company) -> str:
    name = company.short_name or company.name or "Без названия"
    inn = company.inn or "—"
    return f"{name}, ИНН {inn}"


def snapshot_issues(snap: CompanySnapshot) -> dict[str, bool]:
    return {
        IssueType.ADDRESS: snap.unreliable_address,
        IssueType.DIRECTOR: snap.unreliable_director,
        IssueType.FOUNDER: snap.unreliable_founder,
        IssueType.LIQUIDATION: snap.is_liquidating or snap.is_liquidated,
    }


async def apply_snapshot(session: AsyncSession, company: Company, snap: CompanySnapshot) -> list[str]:
    """Обновляет компанию, пишет check_result, открывает/закрывает тикеты. Возвращает тексты уведомлений."""
    now = datetime.now(timezone.utc)
    prev = {
        IssueType.ADDRESS: company.unreliable_address,
        IssueType.DIRECTOR: company.unreliable_director,
        IssueType.FOUNDER: company.unreliable_founder,
        IssueType.LIQUIDATION: company.is_liquidating or company.is_liquidated,
    }
    curr = snapshot_issues(snap)

    company.inn = snap.inn or company.inn
    # имя всегда перезаписываем свежим с карточки (если распарсили)
    if snap.name:
        company.name = snap.name
    if snap.short_name:
        company.short_name = snap.short_name
    company.address = snap.address or company.address
    company.status_text = snap.status_text
    company.unreliable_address = snap.unreliable_address
    company.unreliable_director = snap.unreliable_director
    company.unreliable_founder = snap.unreliable_founder
    company.is_liquidating = snap.is_liquidating
    company.is_liquidated = snap.is_liquidated
    company.last_checked_at = now
    company.last_error = None

    session.add(
        CheckResult(
            company_id=company.id,
            checked_at=now,
            ok=True,
            unreliable_address=snap.unreliable_address,
            unreliable_director=snap.unreliable_director,
            unreliable_founder=snap.unreliable_founder,
            is_liquidating=snap.is_liquidating,
            is_liquidated=snap.is_liquidated,
            status_text=snap.status_text,
            raw_summary=snap.raw_summary,
        )
    )

    notifications: list[str] = []

    for issue_type, is_active in curr.items():
        was_active = prev.get(issue_type, False)
        if is_active and not was_active:
            notifications.extend(await _open_issue(session, company, issue_type))
        elif not is_active and was_active:
            notifications.extend(await _heal_issue(session, company, issue_type))

    await session.commit()
    return notifications


async def _open_issue(session: AsyncSession, company: Company, issue_type: str) -> list[str]:
    existing = await session.scalar(
        select(Ticket).where(
            Ticket.company_id == company.id,
            Ticket.issue_type == issue_type,
            Ticket.status == TicketStatus.IN_PROGRESS,
        )
    )
    if existing:
        return []

    title = f"{issue_label(issue_type)} — {company_display(company)}"
    ticket = Ticket(
        company_id=company.id,
        issue_type=issue_type,
        status=TicketStatus.IN_PROGRESS,
        title=title,
        details=f"ОГРН {company.ogrn}\n{rusprofile_url(company.ogrn)}",
    )
    session.add(ticket)
    await session.flush()

    disp = company_display(company)
    if issue_type == IssueType.ADDRESS:
        msg = (
            f"🚨 Недостоверность адреса\n\n"
            f"{disp}\nОГРН: {company.ogrn}\n"
            f"Создан тикет #{ticket.id} — статус «В работе».\n"
            f"{rusprofile_url(company.ogrn)}"
        )
    elif issue_type == IssueType.FOUNDER:
        msg = (
            f"🚨 Недостоверность учредителя\n\n"
            f"{disp}\nОГРН: {company.ogrn}\n"
            f"⚠️ Требуется согласование с бухгалтерией — возможно снятие объёма.\n"
            f"{rusprofile_url(company.ogrn)}"
        )
    elif issue_type == IssueType.DIRECTOR:
        msg = (
            f"🚨 Недостоверность должностного лица\n\n"
            f"{disp}\nОГРН: {company.ogrn}\n"
            f"Создан тикет #{ticket.id} — статус «В работе».\n"
            f"{rusprofile_url(company.ogrn)}"
        )
    elif issue_type == IssueType.LIQUIDATION:
        msg = (
            f"🚨 Ликвидация / исключение\n\n"
            f"{disp}\nОГРН: {company.ogrn}\n"
            f"Статус: {company.status_text or 'см. Rusprofile'}\n"
            f"Создан тикет #{ticket.id}.\n"
            f"{rusprofile_url(company.ogrn)}"
        )
    else:
        msg = f"🚨 {issue_label(issue_type)}\n\n{disp}\n{rusprofile_url(company.ogrn)}"

    return [msg]


async def _heal_issue(session: AsyncSession, company: Company, issue_type: str) -> list[str]:
    tickets = (
        await session.scalars(
            select(Ticket).where(
                Ticket.company_id == company.id,
                Ticket.issue_type == issue_type,
                Ticket.status == TicketStatus.IN_PROGRESS,
            )
        )
    ).all()
    if not tickets:
        return []

    now = datetime.now(timezone.utc)
    for t in tickets:
        t.status = TicketStatus.HEALED
        t.closed_at = now

    disp = company_display(company)
    if issue_type == IssueType.ADDRESS:
        msg = f"✅ {disp} — адрес восстановлен (недостоверность снята)."
    elif issue_type == IssueType.DIRECTOR:
        msg = f"✅ {disp} — недостоверность должностного лица снята."
    elif issue_type == IssueType.FOUNDER:
        msg = f"✅ {disp} — недостоверность учредителя снята."
    elif issue_type == IssueType.LIQUIDATION:
        msg = f"✅ {disp} — признаки ликвидации сняты."
    else:
        msg = f"✅ {disp} — {issue_label(issue_type).lower()} снята."

    return [msg]


async def check_company(session: AsyncSession, client: RusprofileClient, company: Company) -> list[str]:
    try:
        snap = await client.get_snapshot(company.ogrn)
        return await apply_snapshot(session, company, snap)
    except Exception as exc:
        logger.exception("Check failed for %s", company.ogrn)
        company.last_error = str(exc)
        company.last_checked_at = datetime.now(timezone.utc)
        # Rusprofile недоступен (403 и т.п.) — имя хотя бы из ЕГРЮЛ
        try:
            query = company.inn or company.ogrn
            resolved = await client.egrul.resolve_inn(query)
            if resolved.ogrn == company.ogrn and resolved.name:
                company.name = resolved.name
                company.short_name = resolved.name
            elif resolved.name and not company.short_name:
                company.name = resolved.name
                company.short_name = resolved.name
        except Exception:
            logger.exception("EGRUL name refresh failed for %s", company.ogrn)
        session.add(
            CheckResult(
                company_id=company.id,
                ok=False,
                error=str(exc),
            )
        )
        await session.commit()
        return []


async def check_all_companies(session: AsyncSession, client: RusprofileClient) -> list[str]:
    companies = (
        await session.scalars(select(Company).where(Company.is_active.is_(True)).order_by(Company.id))
    ).all()
    all_msgs: list[str] = []
    for company in companies:
        msgs = await check_company(session, client, company)
        all_msgs.extend(msgs)
    return all_msgs
