from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.db.models import UserRole


# --- Reply menu labels (stable text matchers) ---
BTN_STATUS = "📊 Сводка"
BTN_TICKETS = "🎫 Тикеты"
BTN_SHOPS = "🏪 Лавки"
BTN_CHECK = "🔍 Проверка"
BTN_USERS = "👥 Пользователи"
BTN_RESOLVE = "🔄 ИНН → ОГРН"
BTN_BACK = "⬅️ Назад"
BTN_CANCEL = "❌ Отмена"

BTN_ADD_OGRN = "➕ Добавить ОГРН"
BTN_ADD_LIST = "📋 Список ОГРН"
BTN_IMPORT_FILE = "📎 Файл ОГРН"
BTN_REMOVE = "🗑 Удалить лавку"
BTN_LIST_SHOPS = "📜 Список лавок"

BTN_CHECK_ALL = "⚡ Проверить все"
BTN_CHECK_ONE = "🔎 Проверить одну"

BTN_ADD_USER = "➕ Выдать доступ"
BTN_REMOVE_USER = "🚫 Забрать доступ"
BTN_LIST_USERS = "📜 Список юзеров"

BTN_RESOLVE_ONE = "1️⃣ Один ИНН"
BTN_RESOLVE_LIST = "📋 Список ИНН"
BTN_RESOLVE_FILE = "📎 Файл ИНН"


def main_menu(role: str) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_TICKETS)],
    ]
    if role == UserRole.ADMIN:
        rows.extend(
            [
                [KeyboardButton(text=BTN_SHOPS), KeyboardButton(text=BTN_CHECK)],
                [KeyboardButton(text=BTN_RESOLVE), KeyboardButton(text=BTN_USERS)],
            ]
        )
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def shops_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD_OGRN), KeyboardButton(text=BTN_ADD_LIST)],
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
            [KeyboardButton(text=BTN_LIST_USERS)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def resolve_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_RESOLVE_ONE), KeyboardButton(text=BTN_RESOLVE_LIST)],
            [KeyboardButton(text=BTN_RESOLVE_FILE)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
    )


def tickets_inline(tickets: list) -> InlineKeyboardMarkup:
    rows = []
    for t in tickets[:20]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ Вылечить #{t.id}",
                    callback_data=f"heal:{t.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def after_resolve_inline(ogrn: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Добавить в мониторинг",
                    callback_data=f"add_ogrn:{ogrn}",
                )
            ]
        ]
    )


def after_resolve_batch_inline(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"➕ Добавить все ({count}) в мониторинг",
                    callback_data="add_resolved_batch",
                )
            ]
        ]
    )
