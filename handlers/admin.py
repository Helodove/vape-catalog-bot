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

    # 4. Получаем папку и ассортимент через клиент, ищем варианты
    import json as _json
    variants = await client.get_product_variants(prod_id)
    await update.message.reply_text(f"🎨 get_product_variants вернул: {len(variants)} вариантов")

    # Показываем сырой ассортимент папки (первые строки с type=variant)
    prod_full = await client._get(f"/entity/product/{prod_id}")
    folder_href = (prod_full or {}).get("productFolder", {}).get("meta", {}).get("href", "")
    await update.message.reply_text(f"📁 Папка товара:\n<code>{folder_href}</code>", parse_mode="HTML")

    raw_assort = await client._get("/entity/assortment", {"filter": f"productFolder={folder_href}", "limit": 10})
    rows = (raw_assort or {}).get("rows", [])
    sample = [{"type": r.get("meta",{}).get("type"), "name": r.get("name"), "product": r.get("product",{}).get("meta",{}).get("href","")[-40:] if r.get("product") else None} for r in rows[:5]]
    await update.message.reply_text(f"📦 Ассортимент (первые 5):\n<pre>{_json.dumps(sample, ensure_ascii=False, indent=2)}</pre>", parse_mode="HTML")

    # 5. Получаем варианты через правильный endpoint
    prod_href = f"{BASE_URL}/entity/product/{prod_id}"
    var_url = f"{BASE_URL}/entity/variant?filter=product={prod_href}&limit=3"
    async with httpx.AsyncClient(timeout=15) as http:
        vr = await http.get(var_url, headers={"Authorization": f"Bearer {settings.moysklad_token}"})
    vdata = vr.json() if vr.status_code == 200 else {}
    vrows = vdata.get("rows", [])
    if not vrows:
        await update.message.reply_text(f"⚠️ Вариантов нет (HTTP {vr.status_code}): {vr.text[:200]}")
        return
    first_var = vrows[0]
    var_href = first_var.get("meta", {}).get("href", "")
    vnames = "\n".join(f"• {r.get('name','?')}" for r in vrows)
    await update.message.reply_text(f"✅ Вариантов (HTTP {vr.status_code}):\n{vnames}\n\nПервый href:\n<code>{var_href}</code>", parse_mode="HTML")

    # 5. Остатки по точкам для первого варианта
    stock_url = f"{BASE_URL}/report/stock/bystore?filter=assortment={var_href}&quantityMode=positiveOnly"
    async with httpx.AsyncClient(timeout=15) as http:
        sr2 = await http.get(stock_url, headers={"Authorization": f"Bearer {settings.moysklad_token}"})
    import json
    await update.message.reply_text(
        f"📊 bystore (HTTP {sr2.status_code}):\n<pre>{sr2.text[:800]}</pre>",
        parse_mode="HTML",
    )
