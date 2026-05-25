import logging
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from config import settings

log = logging.getLogger(__name__)

TYPING_TEXT = 1
CONFIRMING = 2


def _is_admin(update: Update) -> bool:
    return update.effective_user.id == settings.admin_chat_id


async def notify_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update):
        return ConversationHandler.END

    await update.message.reply_text(
        "📢 <b>Создание уведомления</b>\n\n"
        "Введите текст сообщения.\n"
        "Будет отправлено всем, кто написал /start боту.",
        parse_mode="HTML",
    )
    return TYPING_TEXT


async def notify_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["notify_text"] = update.message.text
    await update.message.reply_text(
        f"📋 <b>Предпросмотр:</b>\n\n{update.message.text}\n\n"
        "Отправить?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Отправить", callback_data="notify_yes"),
            InlineKeyboardButton("✏️ Изменить", callback_data="notify_edit"),
            InlineKeyboardButton("❌ Отмена", callback_data="notify_no"),
        ]]),
    )
    return CONFIRMING


async def notify_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Введите новый текст уведомления:")
    return TYPING_TEXT


async def _get_all_bot_users() -> list[int]:
    if not settings.supabase_url or not settings.supabase_service_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(
                f"{settings.supabase_url}/rest/v1/bot_users",
                params={"select": "telegram_id"},
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                },
            )
            if r.status_code == 200:
                return [row["telegram_id"] for row in r.json()]
    except Exception as e:
        log.error("get_all_bot_users error: %s", e)
    return []


async def notify_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    text = context.user_data.get("notify_text", "")

    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Прочитал (удалить)", callback_data="notify_ack"),
    ]])

    users = await _get_all_bot_users()
    if not users:
        await query.edit_message_text("❌ Нет подписчиков. Попросите сотрудников написать /start боту.")
        return ConversationHandler.END

    sent = 0
    for chat_id in users:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
            sent += 1
        except Exception as e:
            log.warning("Could not send notify to %s: %s", chat_id, e)

    await query.edit_message_text(f"✅ Уведомление отправлено {sent} из {len(users)} подписчиков.")
    log.info("Notification sent to %d/%d users by admin", sent, len(users))
    return ConversationHandler.END


async def notify_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Отменено.")
    return ConversationHandler.END


async def notify_ack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сотрудник нажал 'Прочитал' — удаляем сообщение."""
    query = update.callback_query
    await query.answer("Отмечено ✅")
    try:
        await query.message.delete()
    except Exception as e:
        log.warning("Could not delete notify message: %s", e)


def build_notify_conv() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("notify", notify_start)],
        states={
            TYPING_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, notify_got_text),
            ],
            CONFIRMING: [
                CallbackQueryHandler(notify_send, pattern="^notify_yes$"),
                CallbackQueryHandler(notify_edit, pattern="^notify_edit$"),
                CallbackQueryHandler(notify_cancel, pattern="^notify_no$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", notify_cancel),
        ],
        per_message=False,
    )
