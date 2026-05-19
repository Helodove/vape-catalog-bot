import logging
from telegram import Update
from telegram.ext import ContextTypes
from moysklad.client import MoySkladClient
from keyboards import folders_keyboard, products_keyboard

log = logging.getLogger(__name__)

ERROR_MSG = "Каталог временно недоступен, попробуйте позже 🙏"


async def catalog_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    client: MoySkladClient = context.bot_data["ms_client"]
    folders = await client.get_root_folders()
    if not folders:
        await update.message.reply_text(ERROR_MSG)
        return
    kb = folders_keyboard(folders, 0, "catalog:root", None)
    await update.message.reply_text("📂 Категории:", reply_markup=kb)


async def catalog_root_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, _, page_str = query.data.split(":")
    page = int(page_str)
    client: MoySkladClient = context.bot_data["ms_client"]
    folders = await client.get_root_folders()
    if not folders:
        await query.edit_message_text(ERROR_MSG)
        return
    kb = folders_keyboard(folders, page, "catalog:root", None)
    await query.edit_message_text("📂 Категории:", reply_markup=kb)


async def folder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # format: folder:{id}:{href}:{page}
    parts = query.data.split(":", 4)
    _, folder_id, folder_href, page_str = parts[0], parts[1], parts[2], parts[3]
    page = int(page_str)
    client: MoySkladClient = context.bot_data["ms_client"]

    subfolders = await client.get_subfolders(folder_href)
    if subfolders:
        parent_cb = f"folder:{folder_id}:{folder_href}"
        kb = folders_keyboard(subfolders, page, parent_cb, "catalog:root:0")
        await query.edit_message_text("📁 Подкатегории:", reply_markup=kb)
        return

    products = await client.get_products(folder_href)
    if products is None:
        await query.edit_message_text(ERROR_MSG)
        return
    back_cb = "catalog:root:0"
    kb = products_keyboard(products, page, folder_id, folder_href, False, back_cb)
    count = len(products)
    await query.edit_message_text(
        f"📦 Товаров: {count}",
        reply_markup=kb,
    )


async def plist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # format: plist:{folder_id}:{folder_href}:{page}:{only_in_stock}
    parts = query.data.split(":", 5)
    folder_id = parts[1]
    folder_href = parts[2]
    page = int(parts[3])
    only_in_stock = bool(int(parts[4]))
    client: MoySkladClient = context.bot_data["ms_client"]
    products = await client.get_products(folder_href)
    if products is None:
        await query.edit_message_text(ERROR_MSG)
        return
    back_cb = "catalog:root:0"
    kb = products_keyboard(products, page, folder_id, folder_href, only_in_stock, back_cb)
    count = len([p for p in products if (not only_in_stock or p.in_stock)])
    await query.edit_message_reply_markup(reply_markup=kb)
