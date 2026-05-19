from telegram import Update
from telegram.ext import ContextTypes
from moysklad.cache import cache
from moysklad.client import MoySkladClient
from config import settings


async def refresh_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != settings.admin_chat_id:
        await update.message.reply_text("⛔ Нет доступа.")
        return
    cache.clear()
    await update.message.reply_text("Кэш очищен ✅")


async def debug_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != settings.admin_chat_id:
        await update.message.reply_text("⛔ Нет доступа.")
        return
    client: MoySkladClient = context.bot_data["ms_client"]
    await update.message.reply_text("🔄 Проверяю соединение с МойСклад...")
    try:
        folders = await client.get_root_folders()
        if folders:
            names = "\n".join(f"• {f.name}" for f in folders[:10])
            await update.message.reply_text(f"✅ МойСклад работает!\nНайдено папок: {len(folders)}\n\n{names}")
        else:
            data = await client._get("/entity/productfolder", {"limit": 5})
            await update.message.reply_text(
                f"⚠️ Папки не найдены (фильтр pathName=).\n\nОтвет API:\n<pre>{str(data)[:1000]}</pre>",
                parse_mode="HTML",
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
