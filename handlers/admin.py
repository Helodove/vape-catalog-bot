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

    # 3. Один товар из ассортимента — смотрим его href
    assort_url = f"{BASE_URL}/entity/assortment?filter=productFolder={folder_href}&limit=1"
    async with httpx.AsyncClient(timeout=15) as http:
        ar = await http.get(assort_url, headers={"Authorization": f"Bearer {settings.moysklad_token}"})
    product_href = ""
    if ar.status_code == 200:
        rows = ar.json().get("rows", [])
        if rows:
            product_href = rows[0].get("meta", {}).get("href", "")
    await update.message.reply_text(f"📦 Product href:\n<code>{product_href}</code>", parse_mode="HTML")

    # 4. Отчёт остатков — смотрим assortment.meta.href в ответе
    stock_url = f"{BASE_URL}/report/stock/all?filter=productFolder={folder_href}&limit=1"
    async with httpx.AsyncClient(timeout=15) as http:
        sr = await http.get(stock_url, headers={"Authorization": f"Bearer {settings.moysklad_token}"})
    if sr.status_code == 200:
        srows = sr.json().get("rows", [])
        if srows:
            import json
            raw = json.dumps(srows[0], ensure_ascii=False, indent=2)
            await update.message.reply_text(
                f"📊 Raw stock row:\n<pre>{raw[:1200]}</pre>",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("⚠️ Stock report вернул 0 строк")
    else:
        await update.message.reply_text(f"❌ Stock report HTTP {sr.status_code}: {sr.text[:300]}")
