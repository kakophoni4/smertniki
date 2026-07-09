import logging
import re
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import (
    BTN_ADD_INN,
    BTN_ADD_LIST,
    BTN_ADD_USER,
    BTN_BACK,
    BTN_CANCEL,
    BTN_CHECK,
    BTN_CHECK_ALL,
    BTN_CHECK_ONE,
    BTN_IMPORT_FILE,
    BTN_LIST_SHOPS,
    BTN_LIST_USERS,
    BTN_MAKE_ADMIN,
    BTN_REMOVE,
    BTN_REMOVE_USER,
    BTN_REVOKE_ADMIN,
    BTN_SHOPS,
    BTN_STATUS,
    BTN_TICKETS,
    BTN_USERS,
    cancel_menu,
    check_menu,
    main_menu,
    shops_menu,
    tickets_page_kb,
    TICKETS_PAGE_SIZE,
    user_role_inline,
    users_menu,
)
from app.bot.states import Form
from app.config import settings
from app.db.models import AllowedUser, Company, Ticket, TicketStatus, UserRole
from app.services.monitor import (
    check_all_companies,
    check_company,
    company_display,
    issue_label,
    rusprofile_url,
)
from app.services.rusprofile_client import RusprofileClient, normalize_inn

logger = logging.getLogger(__name__)

router = Router()

PROGRESS_EVERY = 5


def is_config_admin(user_id: int) -> bool:
    return user_id in settings.admin_id_list


def extract_inns_from_text(text: str) -> list[str]:
    """Достаёт ИНН (10/12 цифр) из текста/файла. ОГРН (13/15) игнорируем как вход."""
    raw = text.replace(",", " ").replace(";", " ").replace("\t", " ")
    found: list[str] = []
    for token in raw.split():
        inn = normalize_inn(token)
        if inn:
            found.append(inn)
            continue
        # иногда ИНН внутри строки
        for m in re.finditer(r"(?<!\d)(\d{10}|\d{12})(?!\d)", token):
            cand = normalize_inn(m.group(1))
            if cand:
                found.append(cand)
    return list(dict.fromkeys(found))


async def ensure_user(session: AsyncSession, message: Message) -> AllowedUser | None:
    tg_id = message.from_user.id
    user = await session.scalar(select(AllowedUser).where(AllowedUser.telegram_id == tg_id))
    if user and user.is_active:
        # диалог есть — можно снова слать алерты
        changed = False
        if not user.notify:
            user.notify = True
            changed = True
        if message.from_user.username and user.username != message.from_user.username:
            user.username = message.from_user.username
            changed = True
        if changed:
            await session.commit()
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
            user.notify = True
            await session.commit()
        return user
    return None


async def require_user(message: Message, session: AsyncSession) -> AllowedUser | None:
    user = await ensure_user(session, message)
    if not user:
        await message.answer(
            "⛔ Бот приватный.\n"
            f"Твой Telegram ID: <code>{message.from_user.id}</code>\n"
            "Передай его админу.",
        )
        return None
    return user


async def require_admin(message: Message, session: AsyncSession) -> AllowedUser | None:
    user = await require_user(message, session)
    if not user:
        return None
    if user.role != UserRole.ADMIN and not is_config_admin(user.telegram_id):
        await message.answer("⛔ Только для администратора.")
        return None
    return user


async def send_chunks(message: Message, lines: list[str], limit: int = 3500) -> None:
    chunk: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > limit:
            await message.answer("\n".join(chunk))
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        await message.answer("\n".join(chunk))


async def find_company_by_inn(session: AsyncSession, inn: str) -> Company | None:
    return await session.scalar(
        select(Company).where(Company.inn == inn, Company.is_active.is_(True))
    )


# ─── start / cancel / back ───────────────────────────────────────────────────


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    user = await require_user(message, session)
    if not user:
        return
    role = "админ" if user.role == UserRole.ADMIN else "пользователь"
    await message.answer(
        f"Привет, {message.from_user.full_name or 'коллега'}!\n"
        f"Роль: <b>{role}</b>\n\n"
        "Источник: <b>ЕГРЮЛ ФНС</b>.\n"
        "Рабочий идентификатор — <b>ИНН</b> (ОГРН бот достаёт сам).\n"
        "Жми кнопки внизу.",
        reply_markup=main_menu(user.role),
    )


@router.message(F.text == BTN_CANCEL)
@router.message(Command("cancel"))
async def on_cancel(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    user = await require_user(message, session)
    if not user:
        return
    await message.answer("Ок, отменил.", reply_markup=main_menu(user.role))


@router.message(F.text == BTN_BACK)
async def on_back(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    user = await require_user(message, session)
    if not user:
        return
    await message.answer("Главное меню", reply_markup=main_menu(user.role))


# ─── status / tickets ────────────────────────────────────────────────────────


@router.message(F.text == BTN_STATUS)
@router.message(Command("status"))
async def on_status(message: Message, session: AsyncSession) -> None:
    user = await require_user(message, session)
    if not user:
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
        f"📊 <b>Мониторинг</b>\n\n"
        f"Лавок в базе: <b>{total}</b>\n"
        f"Недостоверность адреса: <b>{bad_addr}</b>\n"
        f"Недостоверность ДЛ: <b>{bad_dir}</b>\n"
        f"Недостоверность учредителя: <b>{bad_found}</b>\n"
        f"Ликвидация/исключение: <b>{liq}</b>\n"
        f"Тикетов «В работе»: <b>{open_tickets}</b>\n"
        f"Расписание: <code>{settings.check_cron}</code> ({settings.timezone})",
        reply_markup=main_menu(user.role),
    )


async def _tickets_page_payload(
    session: AsyncSession,
    *,
    page: int,
    is_admin: bool,
) -> tuple[str, InlineKeyboardMarkup | None, int]:
    """Возвращает (text, keyboard, total_open). page 0-based."""
    total = await session.scalar(
        select(func.count()).select_from(Ticket).where(Ticket.status == TicketStatus.IN_PROGRESS)
    ) or 0
    if total == 0:
        return "Открытых тикетов нет ✅", None, 0

    page_size = TICKETS_PAGE_SIZE
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    offset = page * page_size

    tickets = (
        await session.scalars(
            select(Ticket)
            .where(Ticket.status == TicketStatus.IN_PROGRESS)
            .order_by(Ticket.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
    ).all()

    lines = [
        f"🎫 <b>Открытые тикеты</b> — {total} шт.\n"
        f"Страница <b>{page + 1}/{total_pages}</b>\n"
    ]
    for t in tickets:
        company = await session.get(Company, t.company_id)
        disp = company_display(company) if company else f"company#{t.company_id}"
        inn = company.inn if company else "—"
        lines.append(f"#{t.id} — {issue_label(t.issue_type)}\n{disp}\nИНН {inn}\n")

    kb = tickets_page_kb(tickets, page=page, total_pages=total_pages, is_admin=is_admin)
    return "\n".join(lines), kb, total


@router.message(F.text == BTN_TICKETS)
@router.message(Command("tickets"))
async def on_tickets(message: Message, session: AsyncSession) -> None:
    user = await require_user(message, session)
    if not user:
        return

    is_admin = user.role == UserRole.ADMIN or is_config_admin(user.telegram_id)
    text, kb, total = await _tickets_page_payload(session, page=0, is_admin=is_admin)
    if total == 0:
        await message.answer(text, reply_markup=main_menu(user.role))
        return
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("tpage:"))
async def cb_tickets_page(callback: CallbackQuery, session: AsyncSession) -> None:
    if not callback.from_user or not callback.message or not callback.data:
        return
    if callback.data == "tpage:noop":
        await callback.answer()
        return

    user = await session.scalar(
        select(AllowedUser).where(AllowedUser.telegram_id == callback.from_user.id, AllowedUser.is_active.is_(True))
    )
    if not user and not is_config_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        page = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer()
        return

    is_admin = bool(
        user and (user.role == UserRole.ADMIN or is_config_admin(user.telegram_id))
    ) or is_config_admin(callback.from_user.id)
    text, kb, total = await _tickets_page_payload(session, page=page, is_admin=is_admin)
    if total == 0:
        await callback.message.edit_text(text)
        await callback.answer()
        return
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("heal:"))
async def cb_heal(callback: CallbackQuery, session: AsyncSession) -> None:
    if not callback.from_user or not callback.message or not callback.data:
        return
    user = await session.scalar(
        select(AllowedUser).where(AllowedUser.telegram_id == callback.from_user.id, AllowedUser.is_active.is_(True))
    )
    if not user or (user.role != UserRole.ADMIN and not is_config_admin(user.telegram_id)):
        await callback.answer("Только админ", show_alert=True)
        return

    parts = callback.data.split(":")
    ticket_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0

    ticket = await session.get(Ticket, ticket_id)
    if not ticket:
        await callback.answer("Тикет не найден", show_alert=True)
        return
    if ticket.status != TicketStatus.IN_PROGRESS:
        await callback.answer("Уже закрыт", show_alert=True)
        return

    company = await session.get(Company, ticket.company_id)
    ticket.status = TicketStatus.HEALED
    ticket.closed_by = callback.from_user.id
    ticket.closed_at = datetime.now(timezone.utc)
    await session.commit()

    disp = company_display(company) if company else f"#{ticket.company_id}"
    issue = issue_label(ticket.issue_type)
    msg = f"✅ Тикет #{ticket.id} закрыт («Вылечена»).\n{disp}\n{issue}"
    await broadcast(session, callback.bot, [msg])
    await callback.answer("Вылечено")

    is_admin = True
    text, kb, total = await _tickets_page_payload(session, page=page, is_admin=is_admin)
    if total == 0:
        await callback.message.edit_text(text)
    else:
        await callback.message.edit_text(text, reply_markup=kb)


# ─── submenus ────────────────────────────────────────────────────────────────


@router.message(F.text == BTN_SHOPS)
async def menu_shops(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.clear()
    await message.answer(
        "🏪 Лавки\nВезде работаем по <b>ИНН</b>. ОГРН бот сам достаёт из ЕГРЮЛ.",
        reply_markup=shops_menu(),
    )


@router.message(F.text == BTN_CHECK)
async def menu_check(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.clear()
    await message.answer("🔍 Проверка по выпискам ЕГРЮЛ", reply_markup=check_menu())


@router.message(F.text == BTN_USERS)
async def menu_users(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.clear()
    await message.answer("👥 Пользователи", reply_markup=users_menu())


# ─── shops by INN ────────────────────────────────────────────────────────────


@router.message(F.text == BTN_ADD_INN)
async def ask_add_inn(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_inn_one)
    await message.answer("Пришли <b>ИНН</b> (10 или 12 цифр).", reply_markup=cancel_menu())


@router.message(Form.wait_inn_one)
async def do_add_inn(
    message: Message, session: AsyncSession, state: FSMContext, client: RusprofileClient
) -> None:
    if not await require_admin(message, session):
        return
    inns = extract_inns_from_text(message.text or "")
    if len(inns) != 1:
        await message.answer("Нужен один ИНН. Ещё раз или «Отмена».")
        return
    await state.clear()
    await _import_inns_and_add(message, session, client, inns, check_new=True)
    await message.answer("Меню лавок", reply_markup=shops_menu())


@router.message(F.text == BTN_ADD_LIST)
async def ask_add_list(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_inn_list)
    await message.answer(
        "Пришли список ИНН (по строке / через пробел/запятую).\n"
        "Сразу в мониторинг + прогресс каждые 5 шт.",
        reply_markup=cancel_menu(),
    )


@router.message(Form.wait_inn_list)
async def do_add_list(
    message: Message, session: AsyncSession, state: FSMContext, client: RusprofileClient
) -> None:
    if not await require_admin(message, session):
        return
    inns = extract_inns_from_text(message.text or "")
    if not inns:
        await message.answer("ИНН не найдены.")
        return
    await state.clear()
    await _import_inns_and_add(message, session, client, inns, check_new=True)
    await message.answer("Меню лавок", reply_markup=shops_menu())


@router.message(F.text == BTN_IMPORT_FILE)
async def ask_import_file(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_inn_file)
    await message.answer(
        "Пришли файл <b>.txt / .csv</b> со списком ИНН.",
        reply_markup=cancel_menu(),
    )


@router.message(Form.wait_inn_file, F.document)
async def do_import_inn_file(
    message: Message, session: AsyncSession, state: FSMContext, client: RusprofileClient
) -> None:
    if not await require_admin(message, session):
        return
    text = await _read_document_text(message)
    if text is None:
        return
    inns = extract_inns_from_text(text)
    if not inns:
        await message.answer("В файле нет ИНН.")
        return
    await state.clear()
    await _import_inns_and_add(message, session, client, inns, check_new=True)
    await message.answer("Меню лавок", reply_markup=shops_menu())


@router.message(F.text == BTN_REMOVE)
async def ask_remove(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_remove_inn)
    await message.answer("Пришли <b>ИНН</b> лавки для удаления из мониторинга.", reply_markup=cancel_menu())


@router.message(Form.wait_remove_inn)
async def do_remove(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    inns = extract_inns_from_text(message.text or "")
    if len(inns) != 1:
        await message.answer("Нужен один ИНН.")
        return
    inn = inns[0]
    company = await find_company_by_inn(session, inn)
    if not company:
        # fallback: inactive too
        company = await session.scalar(select(Company).where(Company.inn == inn))
    if not company:
        await message.answer("Не найдено по этому ИНН.")
        return
    disp = company_display(company)
    ogrn = company.ogrn
    company.is_active = False
    await session.commit()
    await state.clear()
    await broadcast(
        session,
        message.bot,
        [f"🗑 Лавка удалена из мониторинга:\n{disp}\nИНН {inn}\nОГРН {ogrn}"],
    )
    await message.answer("Удалено.", reply_markup=shops_menu())


@router.message(F.text == BTN_LIST_SHOPS)
async def on_list_shops(message: Message, session: AsyncSession) -> None:
    if not await require_admin(message, session):
        return
    companies = (
        await session.scalars(select(Company).where(Company.is_active.is_(True)).order_by(Company.id))
    ).all()
    if not companies:
        await message.answer("Список пуст.", reply_markup=shops_menu())
        return
    lines = [f"📋 Лавки ({len(companies)}):\n"]
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
        lines.append(f"• ИНН {c.inn or '—'} — {c.short_name or '—'}{flag_str}")
    await send_chunks(message, lines)
    await message.answer("Меню лавок", reply_markup=shops_menu())


# ─── check ───────────────────────────────────────────────────────────────────


@router.message(F.text == BTN_CHECK_ALL)
async def on_check_all(message: Message, session: AsyncSession, client: RusprofileClient) -> None:
    if not await require_admin(message, session):
        return
    total = await session.scalar(select(func.count()).select_from(Company).where(Company.is_active.is_(True)))
    await message.answer(
        f"⏳ Полная проверка {total} лавок по выпискам ЕГРЮЛ…\n"
        f"~{max(1, int((total or 1) * 8 / 60))} мин."
    )
    msgs = await check_all_companies(session, client)
    if msgs:
        await broadcast(session, message.bot, msgs)
        await message.answer(f"Готово. Алертов: {len(msgs)}", reply_markup=check_menu())
    else:
        await message.answer("Готово. Новых проблем нет.", reply_markup=check_menu())


@router.message(F.text == BTN_CHECK_ONE)
async def ask_check_one(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_check_inn)
    await message.answer("Пришли <b>ИНН</b> для проверки.", reply_markup=cancel_menu())


@router.message(Form.wait_check_inn)
async def do_check_one(message: Message, session: AsyncSession, state: FSMContext, client: RusprofileClient) -> None:
    if not await require_admin(message, session):
        return
    inns = extract_inns_from_text(message.text or "")
    if len(inns) != 1:
        await message.answer("Нужен один ИНН.")
        return
    inn = inns[0]
    company = await find_company_by_inn(session, inn)
    if not company:
        await message.answer("Лавка с таким ИНН не в мониторинге.", reply_markup=check_menu())
        await state.clear()
        return
    await state.clear()
    await message.answer(f"Проверяю {company_display(company)}…")
    msgs = await check_company(session, client, company)
    if msgs:
        await broadcast(session, message.bot, msgs)
    if company.last_error:
        await message.answer(
            f"⚠️ {company_display(company)}\n"
            f"ИНН <code>{inn}</code> / ОГРН <code>{company.ogrn}</code>\n"
            f"Ошибка: <code>{company.last_error}</code>",
            reply_markup=check_menu(),
        )
    else:
        await message.answer(
            f"Проверено: {company_display(company)}\n"
            f"ИНН {inn} / ОГРН {company.ogrn}\n"
            f"addr={company.unreliable_address} dir={company.unreliable_director} "
            f"found={company.unreliable_founder} liq={company.is_liquidating or company.is_liquidated}",
            reply_markup=check_menu(),
        )


# ─── users ───────────────────────────────────────────────────────────────────


def _parse_tg_id(text: str | None) -> int | None:
    raw = (text or "").strip()
    return int(raw) if raw.isdigit() else None


async def _set_user_role(
    session: AsyncSession,
    tg_id: int,
    role: str,
    *,
    actor_id: int,
) -> str:
    if role == UserRole.USER and tg_id == actor_id:
        return "Нельзя снять админа с самого себя."

    u = await session.scalar(select(AllowedUser).where(AllowedUser.telegram_id == tg_id))
    if role == UserRole.ADMIN:
        if u:
            u.is_active = True
            u.notify = True
            u.role = UserRole.ADMIN
        else:
            session.add(
                AllowedUser(
                    telegram_id=tg_id,
                    role=UserRole.ADMIN,
                    is_active=True,
                    notify=True,
                )
            )
        await session.commit()
        return f"👑 {tg_id} теперь админ. Пусть нажмёт /start."

    if not u:
        return "Пользователь не найден."
    u.role = UserRole.USER
    await session.commit()
    note = ""
    if is_config_admin(tg_id):
        note = (
            "\n⚠️ Он ещё в ADMIN_IDS (.env) — пока ID там, /start снова сделает его админом. "
            "Убери из .env и перезапусти контейнер."
        )
    return f"👤 {tg_id} больше не админ (обычный доступ сохранён).{note}"


@router.message(F.text == BTN_ADD_USER)
async def ask_add_user(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_add_user)
    await message.answer(
        "Пришли Telegram ID.\nБудет обычный доступ (уведомления/сводка/тикеты).",
        reply_markup=cancel_menu(),
    )


@router.message(Form.wait_add_user)
async def do_add_user(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    tg_id = _parse_tg_id(message.text)
    if tg_id is None:
        await message.answer("Нужен числовой Telegram ID.")
        return
    u = await session.scalar(select(AllowedUser).where(AllowedUser.telegram_id == tg_id))
    if u:
        u.is_active = True
        u.notify = True
        if u.role != UserRole.ADMIN:
            u.role = UserRole.USER
    else:
        session.add(AllowedUser(telegram_id=tg_id, role=UserRole.USER, is_active=True, notify=True))
    await session.commit()
    await state.clear()
    await message.answer(f"✅ Доступ выдан: {tg_id} (user)", reply_markup=users_menu())


@router.message(F.text == BTN_REMOVE_USER)
async def ask_remove_user(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_remove_user)
    await message.answer("Пришли Telegram ID — заберём доступ полностью.", reply_markup=cancel_menu())


@router.message(Form.wait_remove_user)
async def do_remove_user(message: Message, session: AsyncSession, state: FSMContext) -> None:
    actor = await require_admin(message, session)
    if not actor:
        return
    tg_id = _parse_tg_id(message.text)
    if tg_id is None:
        await message.answer("Нужен числовой Telegram ID.")
        return
    if tg_id == message.from_user.id:
        await message.answer("Нельзя забрать доступ у самого себя.")
        return
    u = await session.scalar(select(AllowedUser).where(AllowedUser.telegram_id == tg_id))
    if not u:
        await message.answer("Не найден.")
        return
    u.is_active = False
    await session.commit()
    await state.clear()
    await message.answer(f"Доступ отозван: {tg_id}", reply_markup=users_menu())


@router.message(F.text == BTN_MAKE_ADMIN)
async def ask_make_admin(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_make_admin)
    await message.answer(
        "Пришли Telegram ID — сделаю <b>админом</b>.\n"
        "Человек должен один раз написать боту /start (хотя бы без доступа), чтобы узнать ID.",
        reply_markup=cancel_menu(),
    )


@router.message(Form.wait_make_admin)
async def do_make_admin(message: Message, session: AsyncSession, state: FSMContext) -> None:
    actor = await require_admin(message, session)
    if not actor:
        return
    tg_id = _parse_tg_id(message.text)
    if tg_id is None:
        await message.answer("Нужен числовой Telegram ID.")
        return
    msg = await _set_user_role(session, tg_id, UserRole.ADMIN, actor_id=message.from_user.id)
    await state.clear()
    await message.answer(msg, reply_markup=users_menu())


@router.message(F.text == BTN_REVOKE_ADMIN)
async def ask_revoke_admin(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_revoke_admin)
    await message.answer(
        "Пришли Telegram ID админа — сниму роль (останется обычный доступ).",
        reply_markup=cancel_menu(),
    )


@router.message(Form.wait_revoke_admin)
async def do_revoke_admin(message: Message, session: AsyncSession, state: FSMContext) -> None:
    actor = await require_admin(message, session)
    if not actor:
        return
    tg_id = _parse_tg_id(message.text)
    if tg_id is None:
        await message.answer("Нужен числовой Telegram ID.")
        return
    if tg_id == message.from_user.id:
        await message.answer("Нельзя снять админа с самого себя.")
        return
    msg = await _set_user_role(session, tg_id, UserRole.USER, actor_id=message.from_user.id)
    await state.clear()
    await message.answer(msg, reply_markup=users_menu())


@router.message(F.text == BTN_LIST_USERS)
async def on_list_users(message: Message, session: AsyncSession) -> None:
    if not await require_admin(message, session):
        return
    users = (await session.scalars(select(AllowedUser).order_by(AllowedUser.id))).all()
    lines = ["👥 Пользователи:\n"]
    for u in users:
        status = "✅" if u.is_active else "❌"
        crown = "👑 " if u.role == UserRole.ADMIN else ""
        env = " [.env]" if is_config_admin(u.telegram_id) else ""
        lines.append(f"{status} {crown}{u.telegram_id} @{u.username or '—'} ({u.role}){env}")
    await message.answer("\n".join(lines), reply_markup=users_menu())
    active = [u for u in users if u.is_active]
    if active:
        await message.answer("Смена роли одной кнопкой:", reply_markup=user_role_inline(active))


@router.callback_query(F.data.startswith("role:"))
async def cb_change_role(callback: CallbackQuery, session: AsyncSession) -> None:
    if not callback.from_user or not callback.message:
        return
    actor = await session.scalar(
        select(AllowedUser).where(
            AllowedUser.telegram_id == callback.from_user.id,
            AllowedUser.is_active.is_(True),
        )
    )
    if not actor or (actor.role != UserRole.ADMIN and not is_config_admin(actor.telegram_id)):
        await callback.answer("Только админ", show_alert=True)
        return

    # role:admin:123 / role:user:123
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Битые данные", show_alert=True)
        return
    role = parts[1]
    tg_id = int(parts[2])
    if role not in (UserRole.ADMIN, UserRole.USER):
        await callback.answer("Неизвестная роль", show_alert=True)
        return
    if role == UserRole.USER and tg_id == callback.from_user.id:
        await callback.answer("Нельзя снять админа с себя", show_alert=True)
        return

    msg = await _set_user_role(session, tg_id, role, actor_id=callback.from_user.id)
    await callback.answer("Готово")
    await callback.message.answer(msg)


# ─── helpers ─────────────────────────────────────────────────────────────────


async def _read_document_text(message: Message) -> str | None:
    doc = message.document
    if not doc or not doc.file_name:
        await message.answer("Нужен файл.")
        return None
    if not doc.file_name.lower().endswith((".txt", ".csv")):
        await message.answer("Нужен .txt или .csv")
        return None
    file = await message.bot.get_file(doc.file_id)
    buf = await message.bot.download_file(file.file_path)
    return buf.read().decode("utf-8", errors="replace")


async def _upsert_company(
    session: AsyncSession,
    ogrn: str,
    inn: str | None = None,
    name: str | None = None,
) -> tuple[Company, bool]:
    existing = None
    if inn:
        existing = await session.scalar(select(Company).where(Company.inn == inn))
    if not existing:
        existing = await session.scalar(select(Company).where(Company.ogrn == ogrn))
    if existing:
        existing.is_active = True
        existing.ogrn = ogrn
        if inn:
            existing.inn = inn
        if name:
            existing.name = name
            existing.short_name = name
        existing.rusprofile_url = rusprofile_url(ogrn)
        return existing, False
    company = Company(
        ogrn=ogrn,
        inn=inn,
        name=name,
        short_name=name,
        rusprofile_url=rusprofile_url(ogrn),
    )
    session.add(company)
    return company, True


async def _import_inns_and_add(
    message: Message,
    session: AsyncSession,
    client: RusprofileClient,
    inns: list[str],
    check_new: bool = True,
) -> None:
    total = len(inns)
    eta = max(1, int(total * settings.request_delay_sec / 60))
    await message.answer(
        f"🚀 Старт: {total} ИНН\n"
        f"ЕГРЮЛ: резолв → мониторинг.\n"
        f"~{eta}+ мин. Прогресс каждые {PROGRESS_EVERY} шт."
    )
    logger.info("INN import started: %s items", total)

    ok = 0
    fail = 0
    added = 0
    errors: list[str] = []
    new_ogrns: list[str] = []

    for i, inn in enumerate(inns, 1):
        logger.info("INN import %s/%s: %s", i, total, inn)
        result = await client.resolve_inn(inn)
        if not result.ogrn:
            fail += 1
            err = result.error or "unknown"
            errors.append(f"❌ {inn} — {err}")
            logger.warning("INN import fail %s: %s", inn, err)
        else:
            _, is_new = await _upsert_company(session, result.ogrn, inn=inn, name=result.name)
            if is_new:
                added += 1
                new_ogrns.append(result.ogrn)
            ok += 1
            await session.commit()
            logger.info("INN import ok %s -> %s (%s, new=%s)", inn, result.ogrn, result.name, is_new)

        if i % PROGRESS_EVERY == 0 or i == total:
            await message.answer(f"⏳ {i}/{total} | ок {ok} | ошибок {fail} | новых {added}")

    await message.answer(
        f"🏁 Готово: {total}\n✅ резолв: {ok}\n❌ ошибок: {fail}\n🆕 новых: {added}"
    )
    logger.info("INN import done: ok=%s fail=%s added=%s", ok, fail, added)

    if errors:
        await send_chunks(message, ["Ошибки:"] + errors[:50] + (["…"] if len(errors) > 50 else []))

    if check_new and new_ogrns:
        await message.answer(f"Проверяю выписки по {len(new_ogrns)} новым…")
        for ogrn in new_ogrns:
            company = await session.scalar(select(Company).where(Company.ogrn == ogrn))
            if not company:
                continue
            msgs = await check_company(session, client, company)
            if msgs:
                await broadcast(session, message.bot, msgs)
            elif company.last_error:
                await message.answer(
                    f"⚠️ {company_display(company)}\n"
                    f"ИНН {company.inn} / ОГРН {company.ogrn}\n"
                    f"{company.last_error}"
                )
        await message.answer("Проверка новых завершена.")


async def broadcast(session: AsyncSession, bot: Bot, messages: list[str]) -> None:
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

    users = (
        await session.scalars(
            select(AllowedUser).where(AllowedUser.is_active.is_(True), AllowedUser.notify.is_(True))
        )
    ).all()
    for text in messages:
        for u in users:
            try:
                await bot.send_message(u.telegram_id, text, disable_web_page_preview=True)
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                # chat not found / bot blocked / user never pressed /start
                msg = str(exc).lower()
                if "chat not found" in msg or "blocked" in msg or "forbidden" in msg or "user is deactivated" in msg:
                    logger.warning(
                        "Disable notify for %s: %s (нужен /start у бота или неверный ID)",
                        u.telegram_id,
                        exc,
                    )
                    u.notify = False
                    await session.commit()
                else:
                    logger.warning("Failed to notify %s: %s", u.telegram_id, exc)
            except Exception:
                logger.exception("Failed to notify %s", u.telegram_id)


def setup_dispatcher(dp: Dispatcher) -> None:
    dp.include_router(router)
