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

    # 3. Поиск XROS 4 и проверка вариантов
    search_data = await client._get("/entity/product", {"search": "XROS 4", "limit": 3})
    if not search_data or not search_data.get("rows"):
        await update.message.reply_text("⚠️ XROS 4 не найден в поиске")
        return

    prod = search_data["rows"][0]
    prod_id = prod.get("id", "")
    prod_name = prod.get("name", "")
    variants_count = prod.get("variantsCount", 0)
    await update.message.reply_text(
        f"🔍 Найден: <b>{prod_name}</b>\nID: <code>{prod_id}</code>\nvariantsCount: {variants_count}",
        parse_mode="HTML",
    )

    # 4. Пробуем получить варианты
    var_url = f"{BASE_URL}/entity/product/{prod_id}/variants?limit=5"
    async with httpx.AsyncClient(timeout=15) as http:
        vr = await http.get(var_url, headers={"Authorization": f"Bearer {settings.moysklad_token}"})
    vdata = vr.json() if vr.status_code == 200 else {}
    vrows = vdata.get("rows", [])
    if vrows:
        vnames = "\n".join(f"• {r.get('name','?')} (id: {r.get('id','')})" for r in vrows[:5])
        await update.message.reply_text(f"✅ Варианты (HTTP {vr.status_code}):\n{vnames}")
    else:
        await update.message.reply_text(
            f"⚠️ Вариантов нет (HTTP {vr.status_code})\n{vr.text[:300]}"
        )
