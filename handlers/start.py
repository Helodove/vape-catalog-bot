import logging
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ContextTypes
from config import settings

log = logging.getLogger(__name__)


async def _save_bot_user(user) -> None:
    if not settings.supabase_url or not settings.supabase_service_key:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            await http.post(
                f"{settings.supabase_url}/rest/v1/bot_users",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal,resolution=merge-duplicates",
                },
                json={
                    "telegram_id": user.id,
                    "username": user.username,
                    "first_name": user.first_name,
                },
            )
    except Exception as e:
        log.warning("Could not save bot user: %s", e)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _save_bot_user(update.effective_user)
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
