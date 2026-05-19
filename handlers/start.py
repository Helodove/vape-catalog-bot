from telegram import Update
from telegram.ext import ContextTypes
from keyboards import main_menu_keyboard

WELCOME = (
    "👋 Добро пожаловать в каталог нашего магазина!\n\n"
    "Выберите действие:"
)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, reply_markup=main_menu_keyboard())


async def home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(WELCOME, reply_markup=main_menu_keyboard())
