from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.db.models import UserRole


BTN_STATUS = "📊 Сводка"
BTN_TICKETS = "🎫 Тикеты"
BTN_SHOPS = "🏪 Лавки"
BTN_CHECK = "🔍 Проверка"
BTN_USERS = "👥 Пользователи"
BTN_BACK = "⬅️ Назад"
BTN_CANCEL = "❌ Отмена"

BTN_ADD_INN = "➕ Добавить ИНН"
BTN_ADD_LIST = "📋 Список ИНН"
BTN_IMPORT_FILE = "📎 Файл ИНН"
BTN_REMOVE = "🗑 Удалить лавку"
BTN_LIST_SHOPS = "📜 Список лавок"

BTN_CHECK_ALL = "⚡ Проверить все"
BTN_CHECK_ONE = "🔎 Проверить одну"

BTN_ADD_USER = "➕ Выдать доступ"
BTN_REMOVE_USER = "🚫 Забрать доступ"
BTN_MAKE_ADMIN = "👑 Сделать админом"
BTN_REVOKE_ADMIN = "👤 Снять админа"
BTN_LIST_USERS = "📜 Список юзеров"


def main_menu(role: str) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_TICKETS)],
    ]
    if role == UserRole.ADMIN:
        rows.extend(
            [
                [KeyboardButton(text=BTN_SHOPS), KeyboardButton(text=BTN_CHECK)],
                [KeyboardButton(text=BTN_USERS)],
            ]
        )
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def shops_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD_INN), KeyboardButton(text=BTN_ADD_LIST)],
            [KeyboardButton(text=BTN_IMPORT_FILE), KeyboardButton(text=BTN_LIST_SHOPS)],
            [KeyboardButton(text=BTN_REMOVE)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def check_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CHECK_ALL), KeyboardButton(text=BTN_CHECK_ONE)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def users_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD_USER), KeyboardButton(text=BTN_REMOVE_USER)],
            [KeyboardButton(text=BTN_MAKE_ADMIN), KeyboardButton(text=BTN_REVOKE_ADMIN)],
            [KeyboardButton(text=BTN_LIST_USERS)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def user_role_inline(users: list) -> InlineKeyboardMarkup:
    """Кнопки смены роли прямо из списка."""
    rows = []
    for u in users[:25]:
        if not u.is_active:
            continue
        if u.role == UserRole.ADMIN:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"👤 Снять админа {u.telegram_id}",
                        callback_data=f"role:user:{u.telegram_id}",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"👑 В админы {u.telegram_id}",
                        callback_data=f"role:admin:{u.telegram_id}",
                    )
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
    )


TICKETS_PAGE_SIZE = 8


def tickets_page_kb(
    tickets: list,
    *,
    page: int,
    total_pages: int,
    is_admin: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if is_admin:
        for t in tickets:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"✅ Вылечить #{t.id}",
                        callback_data=f"heal:{t.id}:{page}",
                    )
                ]
            )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"tpage:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="tpage:noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"tpage:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)
