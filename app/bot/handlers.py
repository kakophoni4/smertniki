import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import (
    BTN_ADD_LIST,
    BTN_ADD_OGRN,
    BTN_ADD_USER,
    BTN_BACK,
    BTN_CANCEL,
    BTN_CHECK,
    BTN_CHECK_ALL,
    BTN_CHECK_ONE,
    BTN_IMPORT_FILE,
    BTN_LIST_SHOPS,
    BTN_LIST_USERS,
    BTN_REMOVE,
    BTN_REMOVE_USER,
    BTN_RESOLVE,
    BTN_RESOLVE_FILE,
    BTN_RESOLVE_LIST,
    BTN_RESOLVE_ONE,
    BTN_SHOPS,
    BTN_STATUS,
    BTN_TICKETS,
    BTN_USERS,
    after_resolve_batch_inline,
    after_resolve_inline,
    cancel_menu,
    check_menu,
    main_menu,
    resolve_menu,
    shops_menu,
    tickets_inline,
    users_menu,
)
from app.bot.states import Form
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

# временный кэш резолва для кнопки «добавить все» (user_id -> list[ogrn])
_resolved_cache: dict[int, list[str]] = {}


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
        "Жми кнопки внизу — команды помнить не надо.",
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
        f"Ликвидация: <b>{liq}</b>\n"
        f"Тикетов «В работе»: <b>{open_tickets}</b>\n"
        f"Расписание: <code>{settings.check_cron}</code> ({settings.timezone})",
        reply_markup=main_menu(user.role),
    )


@router.message(F.text == BTN_TICKETS)
@router.message(Command("tickets"))
async def on_tickets(message: Message, session: AsyncSession) -> None:
    user = await require_user(message, session)
    if not user:
        return

    tickets = (
        await session.scalars(
            select(Ticket)
            .where(Ticket.status == TicketStatus.IN_PROGRESS)
            .order_by(Ticket.created_at.desc())
            .limit(20)
        )
    ).all()
    if not tickets:
        await message.answer("Открытых тикетов нет ✅", reply_markup=main_menu(user.role))
        return

    lines = ["🎫 <b>Открытые тикеты</b>\n"]
    for t in tickets:
        company = await session.get(Company, t.company_id)
        disp = company_display(company) if company else f"company#{t.company_id}"
        lines.append(f"#{t.id} — {issue_label(t.issue_type)}\n{disp}\n")

    kb = tickets_inline(tickets) if user.role == UserRole.ADMIN else None
    await message.answer("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.startswith("heal:"))
async def cb_heal(callback: CallbackQuery, session: AsyncSession) -> None:
    if not callback.from_user or not callback.message:
        return
    # admin check
    user = await session.scalar(
        select(AllowedUser).where(AllowedUser.telegram_id == callback.from_user.id, AllowedUser.is_active.is_(True))
    )
    if not user or (user.role != UserRole.ADMIN and not is_config_admin(user.telegram_id)):
        await callback.answer("Только админ", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])
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
    await callback.message.answer(msg)


# ─── submenus ────────────────────────────────────────────────────────────────


@router.message(F.text == BTN_SHOPS)
async def menu_shops(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.clear()
    await message.answer("🏪 Лавки — что сделать?", reply_markup=shops_menu())


@router.message(F.text == BTN_CHECK)
async def menu_check(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.clear()
    await message.answer("🔍 Проверка", reply_markup=check_menu())


@router.message(F.text == BTN_USERS)
async def menu_users(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.clear()
    await message.answer("👥 Пользователи", reply_markup=users_menu())


@router.message(F.text == BTN_RESOLVE)
async def menu_resolve(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.clear()
    await message.answer(
        "🔄 ИНН → ОГРН\nТолько резолв, в мониторинг само не добавит.\n"
        "После резолва будет кнопка «Добавить».",
        reply_markup=resolve_menu(),
    )


# ─── shops actions ───────────────────────────────────────────────────────────


@router.message(F.text == BTN_ADD_OGRN)
async def ask_add_ogrn(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_ogrn_one)
    await message.answer(
        "Пришли <b>ОГРН</b> или ссылку rusprofile.ru/id/…",
        reply_markup=cancel_menu(),
    )


@router.message(Form.wait_ogrn_one)
async def do_add_ogrn(message: Message, session: AsyncSession, state: FSMContext, client: RusprofileClient) -> None:
    if not await require_admin(message, session):
        return
    ogrn = extract_ogrn_from_text(message.text or "")
    if not ogrn:
        await message.answer("Не распознал ОГРН. Ещё раз или «Отмена».")
        return
    await state.clear()
    await _add_one_company(message, session, client, ogrn)
    user = await ensure_user(session, message)
    await message.answer("Готово.", reply_markup=shops_menu() if user and user.role == UserRole.ADMIN else None)


@router.message(F.text == BTN_ADD_LIST)
async def ask_add_list(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_ogrn_list)
    await message.answer(
        "Пришли список ОГРН — по одному на строку (или через пробел/запятую).",
        reply_markup=cancel_menu(),
    )


@router.message(Form.wait_ogrn_list)
async def do_add_list(message: Message, session: AsyncSession, state: FSMContext, client: RusprofileClient) -> None:
    if not await require_admin(message, session):
        return
    raw = (message.text or "").replace(",", " ").replace(";", " ")
    ids = list(dict.fromkeys(x for x in (extract_ogrn_from_text(p) for p in raw.split()) if x))
    if not ids:
        await message.answer("ОГРН не найдены. Ещё раз или «Отмена».")
        return
    await state.clear()
    await _add_many_companies(message, session, client, ids)
    await message.answer("Меню лавок", reply_markup=shops_menu())


@router.message(F.text == BTN_IMPORT_FILE)
async def ask_import_file(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_ogrn_file)
    await message.answer(
        "Пришли файл <b>.txt / .csv</b> со списком ОГРН.",
        reply_markup=cancel_menu(),
    )


@router.message(Form.wait_ogrn_file, F.document)
async def do_import_ogrn_file(
    message: Message, session: AsyncSession, state: FSMContext, client: RusprofileClient
) -> None:
    if not await require_admin(message, session):
        return
    text = await _read_document_text(message)
    if text is None:
        return
    ids = list(
        dict.fromkeys(x for x in (extract_ogrn_from_text(p) for p in text.replace(",", " ").split()) if x)
    )
    if not ids:
        await message.answer("В файле нет ОГРН.")
        return
    await state.clear()
    await _add_many_companies(message, session, client, ids)
    await message.answer("Меню лавок", reply_markup=shops_menu())


@router.message(F.text == BTN_REMOVE)
async def ask_remove(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_remove_ogrn)
    await message.answer("Пришли ОГРН лавки, которую убрать из мониторинга.", reply_markup=cancel_menu())


@router.message(Form.wait_remove_ogrn)
async def do_remove(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    ogrn = extract_ogrn_from_text(message.text or "")
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
    await state.clear()
    await broadcast(session, message.bot, [f"🗑 Лавка удалена из мониторинга:\n{disp}\nОГРН {ogrn}"])
    await message.answer("Удалено.", reply_markup=shops_menu())


@router.message(F.text == BTN_LIST_SHOPS)
async def on_list_shops(message: Message, session: AsyncSession) -> None:
    if not await require_admin(message, session):
        return
    companies = (
        await session.scalars(select(Company).where(Company.is_active.is_(True)).order_by(Company.id).limit(80))
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
        lines.append(f"• {c.ogrn} — {c.short_name or '—'}{flag_str}")
    await send_chunks(message, lines)
    await message.answer("Меню лавок", reply_markup=shops_menu())


# ─── check ───────────────────────────────────────────────────────────────────


@router.message(F.text == BTN_CHECK_ALL)
async def on_check_all(message: Message, session: AsyncSession, client: RusprofileClient) -> None:
    if not await require_admin(message, session):
        return
    await message.answer("⏳ Полная проверка…")
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
    await state.set_state(Form.wait_check_ogrn)
    await message.answer("Пришли ОГРН для проверки.", reply_markup=cancel_menu())


@router.message(Form.wait_check_ogrn)
async def do_check_one(message: Message, session: AsyncSession, state: FSMContext, client: RusprofileClient) -> None:
    if not await require_admin(message, session):
        return
    ogrn = extract_ogrn_from_text(message.text or "")
    if not ogrn:
        await message.answer("Неверный ОГРН.")
        return
    company = await session.scalar(select(Company).where(Company.ogrn == ogrn, Company.is_active.is_(True)))
    if not company:
        await message.answer("Лавка не в базе.", reply_markup=check_menu())
        await state.clear()
        return
    await state.clear()
    msgs = await check_company(session, client, company)
    if msgs:
        await broadcast(session, message.bot, msgs)
    await message.answer(
        f"Проверено: {company_display(company)}\n"
        f"addr={company.unreliable_address} dir={company.unreliable_director} "
        f"found={company.unreliable_founder} liq={company.is_liquidating or company.is_liquidated}",
        reply_markup=check_menu(),
    )


# ─── resolve INN ─────────────────────────────────────────────────────────────


@router.message(F.text == BTN_RESOLVE_ONE)
async def ask_resolve_one(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_inn_one)
    await message.answer("Пришли <b>ИНН</b> (10 или 12 цифр).", reply_markup=cancel_menu())


@router.message(Form.wait_inn_one)
async def do_resolve_one(
    message: Message, session: AsyncSession, state: FSMContext, client: RusprofileClient
) -> None:
    if not await require_admin(message, session):
        return
    inn = normalize_inn(message.text or "")
    if not inn:
        await message.answer("Некорректный ИНН.")
        return
    await message.answer(f"Ищу ОГРН для {inn}…")
    result = await client.resolve_inn(inn)
    await state.clear()
    if not result.ogrn:
        await message.answer(f"❌ {inn}: {result.error}", reply_markup=resolve_menu())
        return
    await message.answer(
        f"✅ ИНН {inn}\nОГРН: <code>{result.ogrn}</code>\n{result.name or '—'}\n"
        f"https://www.rusprofile.ru/id/{result.ogrn}",
        reply_markup=after_resolve_inline(result.ogrn),
        disable_web_page_preview=True,
    )
    await message.answer("Меню резолва", reply_markup=resolve_menu())


@router.message(F.text == BTN_RESOLVE_LIST)
async def ask_resolve_list(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_inn_list)
    await message.answer("Пришли список ИНН (по строке / через пробел).", reply_markup=cancel_menu())


@router.message(Form.wait_inn_list)
async def do_resolve_list(
    message: Message, session: AsyncSession, state: FSMContext, client: RusprofileClient
) -> None:
    if not await require_admin(message, session):
        return
    inns = list(
        dict.fromkeys(x for x in (normalize_inn(p) for p in (message.text or "").replace(",", " ").split()) if x)
    )
    if not inns:
        await message.answer("ИНН не найдены.")
        return
    await state.clear()
    await _resolve_many(message, client, inns)


@router.message(F.text == BTN_RESOLVE_FILE)
async def ask_resolve_file(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_inn_file)
    await message.answer("Пришли файл <b>.txt / .csv</b> со списком ИНН.", reply_markup=cancel_menu())


@router.message(Form.wait_inn_file, F.document)
async def do_resolve_file(
    message: Message, session: AsyncSession, state: FSMContext, client: RusprofileClient
) -> None:
    if not await require_admin(message, session):
        return
    text = await _read_document_text(message)
    if text is None:
        return
    inns = list(
        dict.fromkeys(x for x in (normalize_inn(p) for p in text.replace(",", " ").split()) if x)
    )
    if not inns:
        await message.answer("В файле нет ИНН.")
        return
    await state.clear()
    await _resolve_many(message, client, inns)


@router.callback_query(F.data.startswith("add_ogrn:"))
async def cb_add_ogrn(callback: CallbackQuery, session: AsyncSession, client: RusprofileClient) -> None:
    if not callback.from_user or not callback.message:
        return
    user = await session.scalar(
        select(AllowedUser).where(AllowedUser.telegram_id == callback.from_user.id, AllowedUser.is_active.is_(True))
    )
    if not user or (user.role != UserRole.ADMIN and not is_config_admin(user.telegram_id)):
        await callback.answer("Только админ", show_alert=True)
        return
    ogrn = callback.data.split(":", 1)[1]
    existing = await session.scalar(select(Company).where(Company.ogrn == ogrn))
    if existing and existing.is_active:
        await callback.answer("Уже в базе")
        return
    if existing:
        existing.is_active = True
        await session.commit()
        await callback.answer("Снова активна")
        await callback.message.answer(f"Лавка {ogrn} снова в мониторинге.")
        return
    company = Company(ogrn=ogrn, rusprofile_url=rusprofile_url(ogrn))
    session.add(company)
    await session.commit()
    await callback.answer("Добавляю…")
    await callback.message.answer(f"Добавлено. Проверяю {ogrn}…")
    msgs = await check_company(session, client, company)
    if msgs:
        await broadcast(session, callback.bot, msgs)
    else:
        await callback.message.answer(f"✅ {company_display(company)} — ок.")


@router.callback_query(F.data == "add_resolved_batch")
async def cb_add_resolved_batch(
    callback: CallbackQuery, session: AsyncSession, client: RusprofileClient
) -> None:
    if not callback.from_user or not callback.message:
        return
    user = await session.scalar(
        select(AllowedUser).where(AllowedUser.telegram_id == callback.from_user.id, AllowedUser.is_active.is_(True))
    )
    if not user or (user.role != UserRole.ADMIN and not is_config_admin(user.telegram_id)):
        await callback.answer("Только админ", show_alert=True)
        return
    ids = _resolved_cache.get(callback.from_user.id) or []
    if not ids:
        await callback.answer("Кэш пуст — сделай резолв ещё раз", show_alert=True)
        return
    await callback.answer("Добавляю…")
    # emulate message for helper
    await _add_many_companies(callback.message, session, client, ids)
    _resolved_cache.pop(callback.from_user.id, None)


# ─── users ───────────────────────────────────────────────────────────────────


@router.message(F.text == BTN_ADD_USER)
async def ask_add_user(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_add_user)
    await message.answer(
        "Пришли Telegram ID пользователя.\n"
        "Он увидит свой ID, если напишет боту /start без доступа.",
        reply_markup=cancel_menu(),
    )


@router.message(Form.wait_add_user)
async def do_add_user(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Нужен числовой Telegram ID.")
        return
    tg_id = int(text)
    u = await session.scalar(select(AllowedUser).where(AllowedUser.telegram_id == tg_id))
    if u:
        u.is_active = True
        u.notify = True
    else:
        session.add(AllowedUser(telegram_id=tg_id, role=UserRole.USER, is_active=True, notify=True))
    await session.commit()
    await state.clear()
    await message.answer(f"✅ Доступ выдан: {tg_id}", reply_markup=users_menu())


@router.message(F.text == BTN_REMOVE_USER)
async def ask_remove_user(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    await state.set_state(Form.wait_remove_user)
    await message.answer("Пришли Telegram ID, у кого забрать доступ.", reply_markup=cancel_menu())


@router.message(Form.wait_remove_user)
async def do_remove_user(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await require_admin(message, session):
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Нужен числовой Telegram ID.")
        return
    tg_id = int(text)
    u = await session.scalar(select(AllowedUser).where(AllowedUser.telegram_id == tg_id))
    if not u:
        await message.answer("Не найден.")
        return
    u.is_active = False
    await session.commit()
    await state.clear()
    await message.answer(f"Доступ отозван: {tg_id}", reply_markup=users_menu())


@router.message(F.text == BTN_LIST_USERS)
async def on_list_users(message: Message, session: AsyncSession) -> None:
    if not await require_admin(message, session):
        return
    users = (await session.scalars(select(AllowedUser).order_by(AllowedUser.id))).all()
    lines = ["👥 Пользователи:\n"]
    for u in users:
        status = "✅" if u.is_active else "❌"
        lines.append(f"{status} {u.telegram_id} @{u.username or '—'} ({u.role})")
    await message.answer("\n".join(lines), reply_markup=users_menu())


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


async def _add_one_company(
    message: Message, session: AsyncSession, client: RusprofileClient, ogrn: str
) -> None:
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


async def _add_many_companies(
    message: Message, session: AsyncSession, client: RusprofileClient, ids: list[str]
) -> None:
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
        f"📥 Найдено {len(ids)} ОГРН, новых {added}.\n"
        f"Проверка ~{max(1, int(len(ids) * settings.request_delay_sec / 60))} мин…"
    )
    msgs = await check_all_companies(session, client)
    if msgs:
        await broadcast(session, message.bot, msgs)
        await message.answer(f"Готово. Алертов: {len(msgs)}")
    else:
        await message.answer("Готово. Новых проблем нет.")


async def _resolve_many(message: Message, client: RusprofileClient, inns: list[str]) -> None:
    await message.answer(
        f"Резолвлю {len(inns)} ИНН… (~{max(1, int(len(inns) * settings.request_delay_sec / 60))} мин)"
    )
    lines: list[str] = []
    ogrns: list[str] = []
    for inn in inns:
        result = await client.resolve_inn(inn)
        if result.ogrn:
            lines.append(f"✅ {inn} → {result.ogrn}")
            ogrns.append(result.ogrn)
        else:
            lines.append(f"❌ {inn} — {result.error}")
    await send_chunks(message, lines)
    if ogrns and message.from_user:
        _resolved_cache[message.from_user.id] = ogrns
        await message.answer(
            f"Готово: {len(ogrns)} ОГРН из {len(inns)}.\nМожно сразу добавить в мониторинг:",
            reply_markup=after_resolve_batch_inline(len(ogrns)),
        )
    await message.answer("Меню резолва", reply_markup=resolve_menu())


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
