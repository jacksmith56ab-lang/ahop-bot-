from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from menu_config import get_menu_labels


def user_home() -> ReplyKeyboardMarkup:
    # Persistent reply keyboard styled like the reference screenshot.
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎁 Бонус"), KeyboardButton(text="🛒 Купить голду")],
            [KeyboardButton(text="📤 Вывести Gold"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="ℹ️ Информация")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите раздел меню",
    )


def admin_home() -> InlineKeyboardMarkup:
    rows = [
        [("📊 Статистика", "adm:stats"), ("👥 Пользователи", "adm:users")],
        [("🛒 Товары", "adm:products"), ("📦 Заказы", "adm:orders")],
        [("💳 Платежи", "adm:payments"), ("🎁 Бонусы", "adm:bonuses")],
        [("🏆 Розыгрыши", "adm:giveaways"), ("📤 Выдача голды", "adm:withdrawals")],
        [("🧩 Задания", "adm:tasks"), ("👥 Рефералы", "adm:referrals")],
        [("📢 Рассылка", "adm:broadcast"), ("⚙️ Настройки", "adm:settings")],
        [("📜 Журнал действий", "adm:logs")],
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


def back_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← Админ-меню", callback_data="adm:home")]]
    )


def back_user() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="u:home")]]
    )
