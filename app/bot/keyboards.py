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
            [KeyboardButton(text=BTN_LIST_USERS)],
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
