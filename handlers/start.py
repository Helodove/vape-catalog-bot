from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ContextTypes
from config import settings


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🛍 Открыть каталог",
            web_app=WebAppInfo(url=settings.miniapp_origin),
        )
    ]])
    await update.message.reply_text(
        "Добро пожаловать в TheVaper!\n\nНажмите кнопку ниже, чтобы открыть каталог:",
        reply_markup=markup,
    )
