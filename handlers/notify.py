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

_CONFIRM_MARKUP = InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Отправить", callback_data="notify_yes"),
    InlineKeyboardButton("✏️ Изменить", callback_data="notify_edit"),
    InlineKeyboardButton("❌ Отмена", callback_data="notify_no"),
]])


def _is_admin(update: Update) -> bool:
    return update.effective_user.id == settings.admin_chat_id


async def notify_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update):
        return ConversationHandler.END
    context.user_data.pop("notify_text", None)
    context.user_data.pop("notify_photo", None)
    await update.message.reply_text(
        "📢 <b>Создание уведомления</b>\n\n"
        "Введите текст сообщения или отправьте фото с подписью.\n"
        "Будет отправлено всем, кто написал /start боту.",
        parse_mode="HTML",
    )
    return TYPING_TEXT


async def notify_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["notify_text"] = update.message.text
    context.user_data.pop("notify_photo", None)
    await update.message.reply_text(
        f"📋 <b>Предпросмотр:</b>\n\n{update.message.text}\n\nОтправить?",
        parse_mode="HTML",
        reply_markup=_CONFIRM_MARKUP,
    )
    return CONFIRMING


async def notify_got_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    photo = update.message.photo[-1]
    caption = update.message.caption or ""
    context.user_data["notify_text"] = caption
    context.user_data["notify_photo"] = photo.file_id
    preview_caption = (
        f"📋 Предпросмотр:\n\n{caption}\n\nОтправить?"
        if caption else
        "📋 Предпросмотр:\n(фото без подписи)\n\nОтправить?"
    )
    await update.message.reply_photo(
        photo=photo.file_id,
        caption=preview_caption,
        reply_markup=_CONFIRM_MARKUP,
    )
    return CONFIRMING


async def notify_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    context.user_data.pop("notify_photo", None)
    msg = update.callback_query.message
    prompt = "Введите новый текст или отправьте фото с подписью:"
    if msg.photo:
        await msg.edit_caption(prompt)
    else:
        await msg.edit_text(prompt)
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
    photo_id = context.user_data.get("notify_photo")

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
            if photo_id:
                await context.bot.send_photo(chat_id=chat_id, photo=photo_id, caption=text, reply_markup=markup)
            else:
                await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
            sent += 1
        except Exception as e:
            log.warning("Could not send notify to %s: %s", chat_id, e)

    msg = query.message
    result_text = f"✅ Уведомление отправлено {sent} из {len(users)} подписчиков."
    if msg.photo:
        await msg.edit_caption(result_text)
    else:
        await msg.edit_text(result_text)
    log.info("Notification sent to %d/%d users by admin", sent, len(users))
    return ConversationHandler.END


async def notify_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message
        if msg.photo:
            await msg.edit_caption("Отменено.")
        else:
            await msg.edit_text("Отменено.")
    elif update.message:
        await update.message.reply_text("Отменено.")
    context.user_data.pop("notify_photo", None)
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
                MessageHandler(filters.PHOTO, notify_got_photo),
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
            CommandHandler("notify", notify_start),
        ],
        allow_reentry=True,
        per_message=False,
    )
