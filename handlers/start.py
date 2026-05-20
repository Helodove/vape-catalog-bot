from telegram import Update
from telegram.ext import ContextTypes
from keyboards import main_menu_keyboard


def _welcome_text(store_name: str | None) -> str:
    loc = f"📍 Точка: <b>{store_name}</b>\n\n" if store_name else ""
    return f"👋 Добро пожаловать в каталог нашего магазина!\n\n{loc}Выберите действие:"


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_name = context.user_data.get("store_name")
    await update.message.reply_text(
        _welcome_text(store_name),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(store_name),
    )


async def home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    store_name = context.user_data.get("store_name")
    await query.message.reply_text(
        _welcome_text(store_name),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(store_name),
    )
