import logging
import random
import re
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import CheckResult, Company, IssueType, Ticket, TicketStatus
from app.services.egrul import CompanySnapshot
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


# недостоверности, по которым пингуем Декстера
STALE_NAG_ISSUE_TYPES = (
    IssueType.ADDRESS,
    IssueType.DIRECTOR,
    IssueType.FOUNDER,
)


def days_word(n: int) -> str:
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 14:
        return "дней"
    last = n_abs % 10
    if last == 1:
        return "день"
    if 2 <= last <= 4:
        return "дня"
    return "дней"


def ticket_age_days(created_at: datetime | None, *, now: datetime | None = None) -> int:
    if created_at is None:
        return 0
    now = now or datetime.now(timezone.utc)
    created = created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0, (now - created).days)


def ticket_issue_start(ticket: Ticket) -> datetime | None:
    """Дата начала проблемы: из выписки (issue_since), иначе created_at тикета."""
    return ticket.issue_since or ticket.created_at


def issue_since_from_company(company: Company, issue_type: str) -> datetime | None:
    if issue_type == IssueType.ADDRESS:
        return company.unreliable_address_since
    if issue_type == IssueType.DIRECTOR:
        return company.unreliable_director_since
    if issue_type == IssueType.FOUNDER:
        return company.unreliable_founder_since
    return None


def issue_since_from_snap(snap: CompanySnapshot, issue_type: str) -> datetime | None:
    if issue_type == IssueType.ADDRESS:
        return snap.unreliable_address_since
    if issue_type == IssueType.DIRECTOR:
        return snap.unreliable_director_since
    if issue_type == IssueType.FOUNDER:
        return snap.unreliable_founder_since
    return None


def company_display(company: Company) -> str:
    name = company.short_name or company.name or "Без названия"
    inn = company.inn or "—"
    return f"{name}, ИНН {inn}"


def company_display_full(company: Company) -> str:
    return f"{company_display(company)}\nОГРН {company.ogrn}"


def snapshot_issues(snap: CompanySnapshot) -> dict[str, bool]:
    return {
        IssueType.ADDRESS: snap.unreliable_address,
        IssueType.DIRECTOR: snap.unreliable_director,
        IssueType.FOUNDER: snap.unreliable_founder,
        IssueType.LIQUIDATION: snap.is_liquidating or snap.is_liquidated,
    }


async def _prune_check_results(session: AsyncSession, company_id: int) -> None:
    """Оставляем только последние N проверок на лавку — не раздуваем БД."""
    keep = max(1, settings.keep_check_results)
    ids = (
        await session.scalars(
            select(CheckResult.id)
            .where(CheckResult.company_id == company_id)
            .order_by(CheckResult.id.desc())
        )
    ).all()
    if len(ids) <= keep:
        return
    stale = list(ids[keep:])
    await session.execute(delete(CheckResult).where(CheckResult.id.in_(stale)))
    logger.info("Pruned %s old check_results for company_id=%s", len(stale), company_id)


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
    company.unreliable_address_since = snap.unreliable_address_since if snap.unreliable_address else None
    company.unreliable_director_since = snap.unreliable_director_since if snap.unreliable_director else None
    company.unreliable_founder_since = snap.unreliable_founder_since if snap.unreliable_founder else None
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
    await _prune_check_results(session, company.id)

    notifications: list[str] = []

    for issue_type, is_active in curr.items():
        was_active = prev.get(issue_type, False)
        since = issue_since_from_snap(snap, issue_type)
        if is_active and not was_active:
            notifications.extend(await _open_issue(session, company, issue_type, since=since))
        elif is_active and was_active:
            await _refresh_open_ticket_since(session, company, issue_type, since=since)
        elif not is_active and was_active:
            notifications.extend(await _heal_issue(session, company, issue_type))

    await session.commit()
    return notifications


async def _refresh_open_ticket_since(
    session: AsyncSession,
    company: Company,
    issue_type: str,
    *,
    since: datetime | None,
) -> None:
    if since is None:
        return
    existing = await session.scalar(
        select(Ticket).where(
            Ticket.company_id == company.id,
            Ticket.issue_type == issue_type,
            Ticket.status == TicketStatus.IN_PROGRESS,
        )
    )
    if existing and existing.issue_since != since:
        existing.issue_since = since


async def _open_issue(
    session: AsyncSession,
    company: Company,
    issue_type: str,
    *,
    since: datetime | None = None,
) -> list[str]:
    existing = await session.scalar(
        select(Ticket).where(
            Ticket.company_id == company.id,
            Ticket.issue_type == issue_type,
            Ticket.status == TicketStatus.IN_PROGRESS,
        )
    )
    if existing:
        if since and existing.issue_since != since:
            existing.issue_since = since
        return []

    title = f"{issue_label(issue_type)} — {company_display(company)}"
    ticket = Ticket(
        company_id=company.id,
        issue_type=issue_type,
        status=TicketStatus.IN_PROGRESS,
        title=title,
        details=f"ИНН {company.inn or '—'}\nОГРН {company.ogrn}\n{rusprofile_url(company.ogrn)}",
        issue_since=since or issue_since_from_company(company, issue_type),
    )
    session.add(ticket)
    await session.flush()

    disp = company_display(company)
    ids = f"ИНН {company.inn or '—'} / ОГРН {company.ogrn}"
    if issue_type == IssueType.ADDRESS:
        msg = (
            f"🚨 Недостоверность адреса\n\n"
            f"{disp}\n{ids}\n"
            f"Создан тикет #{ticket.id} — статус «В работе».\n"
            f"{rusprofile_url(company.ogrn)}"
        )
    elif issue_type == IssueType.FOUNDER:
        msg = (
            f"🚨 Недостоверность учредителя\n\n"
            f"{disp}\n{ids}\n"
            f"⚠️ Требуется согласование с бухгалтерией — возможно снятие объёма.\n"
            f"{rusprofile_url(company.ogrn)}"
        )
    elif issue_type == IssueType.DIRECTOR:
        msg = (
            f"🚨 Недостоверность должностного лица\n\n"
            f"{disp}\n{ids}\n"
            f"Создан тикет #{ticket.id} — статус «В работе».\n"
            f"{rusprofile_url(company.ogrn)}"
        )
    elif issue_type == IssueType.LIQUIDATION:
        msg = (
            f"🚨 Ликвидация / исключение\n\n"
            f"{disp}\n{ids}\n"
            f"Статус: {company.status_text or 'см. выписку ЕГРЮЛ'}\n"
            f"Создан тикет #{ticket.id}.\n"
            f"{rusprofile_url(company.ogrn)}"
        )
    else:
        msg = f"🚨 {issue_label(issue_type)}\n\n{disp}\n{ids}\n{rusprofile_url(company.ogrn)}"

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
        # хотя бы имя/ИНН из поиска ЕГРЮЛ, если выписка не скачалась
        try:
            resolved = await client.egrul.search(company.inn or company.ogrn)
            if resolved.ogrn == company.ogrn:
                if resolved.name:
                    company.name = resolved.name
                    company.short_name = resolved.name
                if resolved.inn and len(str(resolved.inn)) in (10, 12):
                    company.inn = str(resolved.inn)
        except Exception:
            logger.exception("EGRUL name refresh failed for %s", company.ogrn)
        session.add(
            CheckResult(
                company_id=company.id,
                ok=False,
                error=str(exc),
            )
        )
        await _prune_check_results(session, company.id)
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


# шаблоны нагоняя: {age_html} = "<b>75 дней</b>"
DEXTER_NAG_LINES = (
    "Декстер, хватит хуи пинать, блядь — иди нахуй решай вопросы, прошло уже {age_html}",
    "Декстер, ты ебанутый? Тикет гниёт {age_html}. Въебись уже и закрой эту хуйню",
    "Декстер, сука, недостоверка висит {age_html}. Хватит дрочить воздух — делай",
    "Йоу Декстер, очнись нахуй: {age_html} без движения. Пошёл чини, пидорас ленивый",
    "Декстер, блядина, календарь орёт {age_html}. Вопросы сами себя в рот не засунут",
    "Декстер, хватит пиздеть «потом» — это уже {age_html}. Шевелись, уёбок",
    "Эй Декстер, тикет протух нахуй на {age_html}. Иди ебись с ЕГРЮЛ, а не с кофе",
    "Декстер, ФНС тебе в жопу дышит: {age_html} недостоверки. Разгребай, сука",
    "Декстер, легенда ебучего застоя: {age_html} и всё ещё «в работе». Закрой нахуй",
    "Декстер, пинг под жопу железный: {age_html} ноль прогресса. Двигай булки",
    "Декстер, хватит кормить тикет говном — висит {age_html}. Решай, блядь",
    "Декстер, ау нахуй: {age_html} тикет ждёт тебя как коллектор. Закрой пиздец",
    "Декстер, сервис заебался ждать: {age_html}. Иди сука работай, не выёбывайся",
    "Декстер, не спи, хуйло — {age_html} недостоверки на тебе. Погнали нахуй",
    "Декстер, жопа уже в огне {age_html}. Туши делом, а не статусом «увидел», блядь",
    "Декстер, тикет древнее твоей лени: {age_html}. В музей или в «вылечено», нахуй",
    "Декстер, не игнорь, сука: {age_html} и счётчик растёт. Сделай уже эту хуйню",
    "Декстер, weekly kick в сраку: {age_html} без прогресса. К ЕГРЮЛ марш",
    "Декстер, это не дзен, это заёбанный тикет на {age_html}. Разберись, пидор",
    "Декстер, хватит ебать мозги себе и нам — прошло {age_html}. Закрой вопрос",
    "Декстер, блядь, опять напоминаю: {age_html}. Хватит тянуть резину хуёвую",
    "Декстер, проснись и пой нахуй: висит {age_html}. Иди решай, пока не прилетело",
    "Декстер, ты тикет забыл как гандон под кроватью — {age_html}. Выкинь проблему",
    "Декстер, сука ленивая, {age_html} недостоверки. Хватит пинать хуи — в бой",
    "Декстер, пиздец какой возраст у тикета: {age_html}. Закрой уже эту ёбаную тему",
    "Декстер, нахуй твои отмазки — {age_html}. Встал, сделал, отписал «вылечено»",
    "Декстер, ебаный стыд: {age_html} и тикет ещё дышит. Убей проблему",
    "Декстер, хватит сидеть жопой в кресле — {age_html}. Иди сука закрывай",
)


def _dexter_nag_opener(age: int, *, used: set[int]) -> str:
    """Случайная уникальная строка в рамках одной рассылки."""
    age_html = f"<b>{age} {days_word(age)}</b>"
    free = [i for i in range(len(DEXTER_NAG_LINES)) if i not in used]
    if not free:
        used.clear()
        free = list(range(len(DEXTER_NAG_LINES)))
    idx = random.choice(free)
    used.add(idx)
    return DEXTER_NAG_LINES[idx].format(age_html=age_html)


async def build_stale_ticket_nags(session: AsyncSession) -> list[str]:
    """Еженедельный пинг: недостоверность висит дольше STALE_TICKET_DAYS."""
    now = datetime.now(timezone.utc)
    threshold = max(1, settings.stale_ticket_days)
    tickets = (
        await session.scalars(
            select(Ticket)
            .where(
                Ticket.status == TicketStatus.IN_PROGRESS,
                Ticket.issue_type.in_(STALE_NAG_ISSUE_TYPES),
            )
            .order_by(Ticket.created_at.asc())
        )
    ).all()

    msgs: list[str] = []
    used_lines: set[int] = set()
    for t in tickets:
        start = ticket_issue_start(t)
        age = ticket_age_days(start, now=now)
        if age < threshold:
            continue
        company = await session.get(Company, t.company_id)
        if company is not None and not company.is_active:
            continue
        disp = company_display(company) if company else f"company#{t.company_id}"
        inn = company.inn if company else "—"
        ogrn = company.ogrn if company else "—"
        since_s = start.date().isoformat() if start else "—"
        opener = _dexter_nag_opener(age, used=used_lines)
        msgs.append(
            f"{opener}\n\n"
            f"{issue_label(t.issue_type)}\n"
            f"{disp}\n"
            f"ИНН {inn} / ОГРН {ogrn}\n"
            f"Тикет #{t.id} · недостоверность с {since_s}\n"
            f"{rusprofile_url(ogrn) if ogrn != '—' else ''}"
        )
    return msgs
