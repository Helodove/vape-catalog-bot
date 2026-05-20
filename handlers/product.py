import logging
from telegram import Update
from telegram.ext import ContextTypes
from moysklad.client import MoySkladClient, BASE_URL
from moysklad.models import Product
from keyboards import product_back_keyboard

log = logging.getLogger(__name__)


def _folder_href(folder_id: str) -> str:
    return f"{BASE_URL}/entity/productfolder/{folder_id}"


def _format_card(p: Product) -> str:
    lines = [f"<b>{p.name}</b>"]
    if p.code:
        lines.append(f"Артикул: <code>{p.code}</code>")
    price = p.retail_price
    if price is not None:
        lines.append(f"Цена: <b>{price:,.0f} ₽</b>")
    stock_str = "В наличии ✅" if p.in_stock else "Нет в наличии ❌"
    lines.append(f"Остаток: {stock_str}")
    if p.description:
        lines.append(f"\n{p.description}")
    attr_lines = []
    for a in p.attributes:
        if a.value is not None and str(a.value).strip():
            attr_lines.append(f"• {a.name}: {a.value}")
    if attr_lines:
        lines.append("\n" + "\n".join(attr_lines))
    return "\n".join(lines)


async def product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # format: product:{product_id}:{page}:{only_in_stock}
    parts = query.data.split(":")
    product_id = parts[1]
    page = parts[2]
    only_in_stock = parts[3]

    folder_id = context.user_data.get("current_folder_id")
    if not folder_id:
        await query.edit_message_text("Пожалуйста, вернитесь в каталог и откройте категорию заново.")
        return

    folder_href = _folder_href(folder_id)
    back_cb = f"plist:{folder_id}:{page}:{only_in_stock}"
    client: MoySkladClient = context.bot_data["ms_client"]

    products = await client.get_products(folder_href)
    product = next((p for p in products if p.id == product_id), None)
    if not product:
        await query.edit_message_text("Товар не найден.")
        return

    text = _format_card(product)
    kb = product_back_keyboard(back_cb)

    image_url = await client.get_product_image_url(product_id)
    if image_url:
        try:
            await query.message.reply_photo(
                photo=image_url,
                caption=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            await query.message.delete()
            return
        except Exception as e:
            log.error("Failed to send photo for product %s: %s", product_id, e)

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def sproduct_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # format: sproduct:{product_id}:{page}:{only_in_stock}
    parts = query.data.split(":")
    product_id = parts[1]
    page = parts[2]
    only_in_stock = parts[3]
    back_cb = f"slist:{page}:{only_in_stock}"
    client: MoySkladClient = context.bot_data["ms_client"]

    search_results: list[Product] = context.bot_data.get("search_results", {}).get(
        query.from_user.id, []
    )
    product = next((p for p in search_results if p.id == product_id), None)
    if not product:
        await query.edit_message_text("Товар не найден.")
        return

    text = _format_card(product)
    kb = product_back_keyboard(back_cb)

    image_url = await client.get_product_image_url(product_id)
    if image_url:
        try:
            await query.message.reply_photo(
                photo=image_url,
                caption=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            await query.message.delete()
            return
        except Exception as e:
            log.error("Failed to send photo for product %s: %s", product_id, e)

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
