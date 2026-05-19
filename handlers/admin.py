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
    stock_href = ""
    stock_val = None
    if sr.status_code == 200:
        srows = sr.json().get("rows", [])
        if srows:
            stock_href = srows[0].get("assortment", {}).get("meta", {}).get("href", "")
            stock_val = srows[0].get("stock")
    await update.message.reply_text(
        f"📊 Stock href:\n<code>{stock_href}</code>\n\nstock={stock_val}\n\n"
        f"{'✅ href совпадают' if product_href and product_href == stock_href else '❌ href НЕ совпадают'}",
        parse_mode="HTML",
    )
