from telegram import Update
from telegram.ext import ContextTypes
from moysklad.cache import cache
from moysklad.client import MoySkladClient, BASE_URL
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
    await update.message.reply_text("🔄 Проверяю МойСклад...")

    # 1. Папки
    folders = await client.get_root_folders()
    if not folders:
        await update.message.reply_text("❌ Папки не найдены.")
        return
    names = "\n".join(f"• {f.name} (id: {f.id})" for f in folders[:5])
    await update.message.reply_text(f"✅ Папок: {len(folders)}\n\n{names}")

    # 2. Товары в первой папке напрямую
    first = folders[0]
    folder_href = f"{BASE_URL}/entity/productfolder/{first.id}"
    await update.message.reply_text(f"🔄 Ищу товары в «{first.name}»...")
    raw = await client._get("/entity/product", {"filter": f"productFolder={folder_href}", "limit": 5})
    if raw is None:
        await update.message.reply_text("❌ Ошибка запроса товаров.")
        return
    total = raw.get("meta", {}).get("size", "?")
    rows = raw.get("rows", [])
    if rows:
        items = "\n".join(f"• {r.get('name', '?')}" for r in rows[:5])
        await update.message.reply_text(f"✅ Товаров в «{first.name}»: {total}\n\n{items}")
    else:
        await update.message.reply_text(
            f"⚠️ В папке «{first.name}» товаров нет (total={total}).\n\n"
            f"Проверьте: в МойСклад товары привязаны к папкам?"
        )

    # 3. Все товары без фильтра
    raw_all = await client._get("/entity/product", {"limit": 5})
    total_all = raw_all.get("meta", {}).get("size", "?") if raw_all else "?"
    await update.message.reply_text(f"ℹ️ Всего товаров в МойСклад (без фильтра): {total_all}")
