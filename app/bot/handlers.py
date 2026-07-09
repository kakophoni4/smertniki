import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import AllowedUser, Company, Ticket, TicketStatus, UserRole
from app.services.monitor import (
    check_all_companies,
    check_company,
    company_display,
    extract_ogrn_from_text,
    issue_label,
    rusprofile_url,
)
from app.services.rusprofile_client import RusprofileClient, normalize_inn

logger = logging.getLogger(__name__)

router = Router()


def is_config_admin(user_id: int) -> bool:
    return user_id in settings.admin_id_list


async def ensure_user(session: AsyncSession, message: Message) -> AllowedUser | None:
    tg_id = message.from_user.id
    user = await session.scalar(select(AllowedUser).where(AllowedUser.telegram_id == tg_id))
    if user and user.is_active:
        return user
    if is_config_admin(tg_id):
        if not user:
            user = AllowedUser(
                telegram_id=tg_id,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
                role=UserRole.ADMIN,
                is_active=True,
                notify=True,
            )
            session.add(user)
            await session.commit()
        elif not user.is_active:
            user.is_active = True
            user.role = UserRole.ADMIN
            await session.commit()
        return user
    return None


async def require_admin(message: Message, session: AsyncSession) -> AllowedUser | None:
    user = await ensure_user(session, message)
    if not user:
        await message.answer("⛔ Бот приватный. Доступ не выдан.")
        return None
    if user.role != UserRole.ADMIN and not is_config_admin(user.telegram_id):
        await message.answer("⛔ Только для администратора.")
        return None
    return user


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession) -> None:
    user = await ensure_user(session, message)
    if not user:
        await message.answer(
            "Привет. Это приватный монитор лавок (Rusprofile).\n"
            f"Твой Telegram ID: `{message.from_user.id}`\n"
            "Передай его админу для доступа.",
            parse_mode="Markdown",
        )
        return

    role = "админ" if user.role == UserRole.ADMIN else "пользователь"
    admin_block = ""
    if user.role == UserRole.ADMIN:
        admin_block = (
            "\nАдмин:\n"
            "/add_company ОГРН — добавить лавку\n"
            "/add_companies — пакет ОГРН\n"
            "/import_file — .txt со списком ОГРН\n"
            "/resolve_inn ИНН — ИНН→ОГРН (не добавляет в мониторинг)\n"
            "/resolve_inns — пакет ИНН→ОГРН\n"
            "/remove_company ОГРН — удалить\n"
            "/list_companies — список\n"
            "/add_user ID — выдать доступ\n"
            "/remove_user ID — забрать доступ\n"
            "/list_users — пользователи\n"
            "/heal TICKET_ID — тикет «Вылечена»\n"
            "/check_now — проверить все сейчас\n"
            "/check ОГРН — одну компанию"
        )

    await message.answer(
        f"Привет, {message.from_user.full_name or 'коллега'}!\n"
        f"Роль: {role}\n\n"
        "Команды:\n"
        "/status — сводка\n"
        "/tickets — открытые тикеты"
        f"{admin_block}"
    )


@router.message(Command("status"))
async def cmd_status(message: Message, session: AsyncSession) -> None:
    user = await ensure_user(session, message)
    if not user:
        await message.answer("⛔ Нет доступа.")
        return

    total = await session.scalar(select(func.count()).select_from(Company).where(Company.is_active.is_(True)))
    bad_addr = await session.scalar(
        select(func.count()).select_from(Company).where(
            Company.is_active.is_(True), Company.unreliable_address.is_(True)
        )
    )
    bad_dir = await session.scalar(
        select(func.count()).select_from(Company).where(
            Company.is_active.is_(True), Company.unreliable_director.is_(True)
        )
    )
    bad_found = await session.scalar(
        select(func.count()).select_from(Company).where(
            Company.is_active.is_(True), Company.unreliable_founder.is_(True)
        )
    )
    liq = await session.scalar(
        select(func.count()).select_from(Company).where(
            Company.is_active.is_(True),
            (Company.is_liquidating.is_(True)) | (Company.is_liquidated.is_(True)),
        )
    )
    open_tickets = await session.scalar(
        select(func.count()).select_from(Ticket).where(Ticket.status == TicketStatus.IN_PROGRESS)
    )

    await message.answer(
        f"📊 Мониторинг\n\n"
        f"Лавок в базе: {total}\n"
        f"Недостоверность адреса: {bad_addr}\n"
        f"Недостоверность ДЛ: {bad_dir}\n"
        f"Недостоверность учредителя: {bad_found}\n"
        f"Ликвидация: {liq}\n"
        f"Тикетов «В работе»: {open_tickets}\n"
        f"Расписание: `{settings.check_cron}` ({settings.timezone})",
        parse_mode="Markdown",
    )


@router.message(Command("tickets"))
async def cmd_tickets(message: Message, session: AsyncSession) -> None:
    user = await ensure_user(session, message)
    if not user:
        await message.answer("⛔ Нет доступа.")
        return

    tickets = (
        await session.scalars(
            select(Ticket)
            .where(Ticket.status == TicketStatus.IN_PROGRESS)
            .order_by(Ticket.created_at.desc())
            .limit(30)
        )
    ).all()
    if not tickets:
        await message.answer("Открытых тикетов нет ✅")
        return

    lines = ["🎫 Открытые тикеты:\n"]
    for t in tickets:
        company = await session.get(Company, t.company_id)
        disp = company_display(company) if company else f"company#{t.company_id}"
        lines.append(f"#{t.id} — {issue_label(t.issue_type)}\n{disp}\n")
    await message.answer("\n".join(lines))


@router.message(Command("resolve_inn"))
async def cmd_resolve_inn(message: Message, session: AsyncSession, client: RusprofileClient) -> None:
    user = await require_admin(message, session)
    if not user:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование: /resolve_inn ИНН\n\n"
            "Только резолв ИНН→ОГРН. В мониторинг не добавляет.\n"
            "Потом: /add_company ОГРН"
        )
        return

    inn = normalize_inn(parts[1])
    if not inn:
        await message.answer("Некорректный ИНН (нужно 10 или 12 цифр).")
        return

    await message.answer(f"Ищу ОГРН для ИНН {inn}…")
    result = await client.resolve_inn(inn)
    if not result.ogrn:
        await message.answer(f"❌ ИНН {inn}: {result.error}")
        return

    name = result.name or "—"
    await message.answer(
        f"✅ ИНН {inn}\n"
        f"ОГРН: `{result.ogrn}`\n"
        f"{name}\n"
        f"https://www.rusprofile.ru/id/{result.ogrn}\n\n"
        f"Добавить: /add_company {result.ogrn}",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


@router.message(Command("resolve_inns"))
async def cmd_resolve_inns(message: Message, session: AsyncSession, client: RusprofileClient) -> None:
    user = await require_admin(message, session)
    if not user:
        return

    text = (message.text or "").split(maxsplit=1)
    if len(text) < 2:
        await message.answer(
            "Отправь:\n/resolve_inns\n9731112429\n9726027672\n…\n\n"
            "Вернёт список ОГРН. В мониторинг не добавляет."
        )
        return

    raw = text[1].replace(",", " ").replace(";", " ")
    inns = [normalize_inn(x) for x in raw.split()]
    inns = list(dict.fromkeys(x for x in inns if x))
    if not inns:
        await message.answer("ИНН не найдены.")
        return

    await message.answer(f"Резолвлю {len(inns)} ИНН… (~{int(len(inns) * settings.request_delay_sec / 60)} мин)")
    lines: list[str] = []
    ogrn_lines: list[str] = []
    for inn in inns:
        result = await client.resolve_inn(inn)
        if result.ogrn:
            lines.append(f"✅ {inn} → {result.ogrn}")
            ogrn_lines.append(result.ogrn)
        else:
            lines.append(f"❌ {inn} — {result.error}")

    # Telegram лимит ~4096, режем
    chunk: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > 3500:
            await message.answer("\n".join(chunk))
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        await message.answer("\n".join(chunk))

    if ogrn_lines:
        await message.answer(
            "ОГРН для импорта (скопируй в /add_companies):\n\n" + "\n".join(ogrn_lines[:100])
            + ("\n…" if len(ogrn_lines) > 100 else "")
        )


@router.message(Command("add_company"))
async def cmd_add_company(message: Message, session: AsyncSession, client: RusprofileClient) -> None:
    user = await require_admin(message, session)
    if not user:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /add_company ОГРН\nили /add_company https://www.rusprofile.ru/id/...")
        return

    ogrn = extract_ogrn_from_text(parts[1])
    if not ogrn:
        await message.answer("Не удалось распознать ОГРН.")
        return

    existing = await session.scalar(select(Company).where(Company.ogrn == ogrn))
    if existing:
        if not existing.is_active:
            existing.is_active = True
            await session.commit()
            await message.answer(f"Лавка {ogrn} снова активна.")
        else:
            await message.answer(f"Уже в базе: {company_display(existing)}")
        return

    company = Company(ogrn=ogrn, rusprofile_url=rusprofile_url(ogrn))
    session.add(company)
    await session.commit()

    await message.answer(f"Добавлено. Проверяю {ogrn}…")
    msgs = await check_company(session, client, company)
    if msgs:
        await broadcast(session, message.bot, msgs)
    else:
        await message.answer(f"✅ {company_display(company)} — проблем не найдено.")


@router.message(Command("import_file"))
async def cmd_import_file(message: Message) -> None:
    await message.answer(
        "Отправь текстовый файл (.txt) со списком ОГРН — по одному на строку.\n"
        "Можно вставить ссылки Rusprofile."
    )


@router.message(lambda m: m.document is not None)
async def on_document(message: Message, session: AsyncSession, client: RusprofileClient) -> None:
    user = await require_admin(message, session)
    if not user:
        return

    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith((".txt", ".csv")):
        await message.answer("Нужен .txt или .csv файл со списком ОГРН.")
        return

    file = await message.bot.get_file(doc.file_id)
    buf = await message.bot.download_file(file.file_path)
    text = buf.read().decode("utf-8", errors="replace")

    ids = [extract_ogrn_from_text(x) for x in text.replace(",", " ").split()]
    ids = list(dict.fromkeys(x for x in ids if x))
    if not ids:
        await message.answer("В файле не найдено ни одного ОГРН.")
        return

    added = 0
    for ogrn in ids:
        existing = await session.scalar(select(Company).where(Company.ogrn == ogrn))
        if existing:
            existing.is_active = True
            continue
        session.add(Company(ogrn=ogrn, rusprofile_url=rusprofile_url(ogrn)))
        added += 1
    await session.commit()

    await message.answer(
        f"📥 Импорт: найдено {len(ids)} ОГРН, новых {added}.\n"
        f"Полная проверка займёт ~{int(len(ids) * 3 / 60)} мин (пауза {settings.request_delay_sec}с)."
    )
    msgs = await check_all_companies(session, client)
    if msgs:
        await broadcast(session, message.bot, msgs)


@router.message(Command("add_companies"))
async def cmd_add_companies(message: Message, session: AsyncSession, client: RusprofileClient) -> None:
    user = await require_admin(message, session)
    if not user:
        return

    text = (message.text or "").split(maxsplit=1)
    if len(text) < 2:
        await message.answer(
            "Отправь:\n/add_companies\n1237700215290\n1227700761001\n…\n\n"
            "Или одной строкой через пробел/запятую."
        )
        return

    raw = text[1].replace(",", " ").replace(";", " ")
    ids = [extract_ogrn_from_text(x) for x in raw.split()]
    ids = [x for x in ids if x]
    if not ids:
        await message.answer("ОГРН не найдены.")
        return

    added = 0
    for ogrn in ids:
        existing = await session.scalar(select(Company).where(Company.ogrn == ogrn))
        if existing:
            existing.is_active = True
            continue
        session.add(Company(ogrn=ogrn, rusprofile_url=rusprofile_url(ogrn)))
        added += 1
    await session.commit()
    await message.answer(f"Добавлено новых: {added}. Всего в пакете: {len(ids)}. Запускаю проверку…")
    msgs = await check_all_companies(session, client)
    if msgs:
        await broadcast(session, message.bot, msgs)


@router.message(Command("remove_company"))
async def cmd_remove_company(message: Message, session: AsyncSession) -> None:
    user = await require_admin(message, session)
    if not user:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /remove_company ОГРН")
        return
    ogrn = extract_ogrn_from_text(parts[1])
    if not ogrn:
        await message.answer("Неверный ОГРН.")
        return
    company = await session.scalar(select(Company).where(Company.ogrn == ogrn))
    if not company:
        await message.answer("Не найдено.")
        return
    disp = company_display(company)
    company.is_active = False
    await session.commit()
    await broadcast(session, message.bot, [f"🗑 Лавка удалена из мониторинга:\n{disp}\nОГРН {ogrn}"])
    await message.answer("Готово.")


@router.message(Command("list_companies"))
async def cmd_list_companies(message: Message, session: AsyncSession) -> None:
    user = await require_admin(message, session)
    if not user:
        return

    companies = (
        await session.scalars(select(Company).where(Company.is_active.is_(True)).order_by(Company.id).limit(50))
    ).all()
    if not companies:
        await message.answer("Список пуст.")
        return
    lines = [f"📋 Лавки (показано {len(companies)}):\n"]
    for c in companies:
        flags = []
        if c.unreliable_address:
            flags.append("addr")
        if c.unreliable_director:
            flags.append("dir")
        if c.unreliable_founder:
            flags.append("found")
        if c.is_liquidating or c.is_liquidated:
            flags.append("liq")
        flag_str = f" ⚠️[{','.join(flags)}]" if flags else ""
        lines.append(f"• {c.ogrn} — {c.short_name or '—'}{flag_str}")
    if len(companies) == 50:
        lines.append("\n… обрезано, всего может быть больше")
    await message.answer("\n".join(lines))


@router.message(Command("add_user"))
async def cmd_add_user(message: Message, session: AsyncSession) -> None:
    user = await require_admin(message, session)
    if not user:
        return

    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /add_user TELEGRAM_ID")
        return
    tg_id = int(parts[1])
    u = await session.scalar(select(AllowedUser).where(AllowedUser.telegram_id == tg_id))
    if u:
        u.is_active = True
        u.notify = True
    else:
        u = AllowedUser(telegram_id=tg_id, role=UserRole.USER, is_active=True, notify=True)
        session.add(u)
    await session.commit()
    await message.answer(f"✅ Доступ выдан: {tg_id}")


@router.message(Command("remove_user"))
async def cmd_remove_user(message: Message, session: AsyncSession) -> None:
    user = await require_admin(message, session)
    if not user:
        return

    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /remove_user TELEGRAM_ID")
        return
    tg_id = int(parts[1])
    u = await session.scalar(select(AllowedUser).where(AllowedUser.telegram_id == tg_id))
    if not u:
        await message.answer("Не найден.")
        return
    u.is_active = False
    await session.commit()
    await message.answer(f"Доступ отозван: {tg_id}")


@router.message(Command("list_users"))
async def cmd_list_users(message: Message, session: AsyncSession) -> None:
    user = await require_admin(message, session)
    if not user:
        return

    users = (await session.scalars(select(AllowedUser).order_by(AllowedUser.id))).all()
    lines = ["👥 Пользователи:\n"]
    for u in users:
        status = "✅" if u.is_active else "❌"
        lines.append(f"{status} {u.telegram_id} @{u.username or '—'} ({u.role}) notify={u.notify}")
    await message.answer("\n".join(lines))


@router.message(Command("heal"))
async def cmd_heal(message: Message, session: AsyncSession) -> None:
    user = await require_admin(message, session)
    if not user:
        return

    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /heal TICKET_ID")
        return
    ticket_id = int(parts[1])
    ticket = await session.get(Ticket, ticket_id)
    if not ticket:
        await message.answer("Тикет не найден.")
        return
    company = await session.get(Company, ticket.company_id)
    ticket.status = TicketStatus.HEALED
    ticket.closed_by = message.from_user.id
    ticket.closed_at = datetime.now(timezone.utc)
    await session.commit()

    disp = company_display(company) if company else f"#{ticket.company_id}"
    issue = issue_label(ticket.issue_type)
    msg = f"✅ Тикет #{ticket.id} закрыт («Вылечена»).\n{disp}\n{issue}"
    await broadcast(session, message.bot, [msg])
    await message.answer("Статус обновлён, уведомление отправлено.")


@router.message(Command("check_now"))
async def cmd_check_now(message: Message, session: AsyncSession, client: RusprofileClient) -> None:
    user = await require_admin(message, session)
    if not user:
        return

    await message.answer("⏳ Запускаю полную проверку…")
    msgs = await check_all_companies(session, client)
    if msgs:
        await broadcast(session, message.bot, msgs)
        await message.answer(f"Готово. Алертов: {len(msgs)}")
    else:
        await message.answer("Готово. Новых проблем нет.")


@router.message(Command("check"))
async def cmd_check_one(message: Message, session: AsyncSession, client: RusprofileClient) -> None:
    user = await require_admin(message, session)
    if not user:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /check ОГРН")
        return
    ogrn = extract_ogrn_from_text(parts[1])
    if not ogrn:
        await message.answer("Неверный ОГРН.")
        return
    company = await session.scalar(select(Company).where(Company.ogrn == ogrn, Company.is_active.is_(True)))
    if not company:
        await message.answer("Лавка не в базе.")
        return
    msgs = await check_company(session, client, company)
    if msgs:
        await broadcast(session, message.bot, msgs)
    await message.answer(
        f"Проверено: {company_display(company)}\n"
        f"addr={company.unreliable_address} dir={company.unreliable_director} "
        f"found={company.unreliable_founder} liq={company.is_liquidating or company.is_liquidated}"
    )


async def broadcast(session: AsyncSession, bot: Bot, messages: list[str]) -> None:
    users = (
        await session.scalars(
            select(AllowedUser).where(AllowedUser.is_active.is_(True), AllowedUser.notify.is_(True))
        )
    ).all()
    for text in messages:
        for u in users:
            try:
                await bot.send_message(u.telegram_id, text, disable_web_page_preview=True)
            except Exception:
                logger.exception("Failed to notify %s", u.telegram_id)


def setup_dispatcher(dp: Dispatcher) -> None:
    dp.include_router(router)
