import httpx
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

    first = folders[0]
    folder_href = f"{BASE_URL}/entity/productfolder/{first.id}"

    # 2. Все товары без фильтра
    raw_all = await client._get("/entity/product", {"limit": 3})
    total_all = raw_all.get("meta", {}).get("size", "?") if raw_all else "❌ ошибка"
    await update.message.reply_text(f"ℹ️ Всего товаров (без фильтра): {total_all}")

    # 3. Тест через assortment (правильный эндпоинт для каталога)
    test_url = f"{BASE_URL}/entity/assortment?filter=productFolder={folder_href}&limit=3"
    await update.message.reply_text(f"🔄 Тестирую assortment:\n<code>{test_url}</code>", parse_mode="HTML")
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(
                test_url,
                headers={"Authorization": f"Bearer {settings.moysklad_token}"},
            )
        data = resp.json()
        total = data.get("meta", {}).get("size", "?")
        rows = data.get("rows", [])
        names = "\n".join(f"• {r.get('name','?')}" for r in rows[:3])
        await update.message.reply_text(
            f"HTTP {resp.status_code} | Найдено: {total}\n\n{names}" if resp.status_code == 200
            else f"HTTP {resp.status_code}\n<pre>{resp.text[:600]}</pre>",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Исключение: {e}")
