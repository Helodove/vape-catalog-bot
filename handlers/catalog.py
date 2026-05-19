import logging
from telegram import Update
from telegram.ext import ContextTypes
from moysklad.client import MoySkladClient, BASE_URL
from keyboards import folders_keyboard, products_keyboard

log = logging.getLogger(__name__)

ERROR_MSG = "Каталог временно недоступен, попробуйте позже 🙏"


def _folder_href(folder_id: str) -> str:
    return f"{BASE_URL}/entity/productfolder/{folder_id}"


async def catalog_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    client: MoySkladClient = context.bot_data["ms_client"]
    folders = await client.get_root_folders()
    if not folders:
        await update.message.reply_text(ERROR_MSG)
        return
    context.user_data["nav_back"] = "catalog:root:0"
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
    context.user_data["nav_back"] = "catalog:root:0"
    kb = folders_keyboard(folders, page, "catalog:root", None)
    await query.edit_message_text("📂 Категории:", reply_markup=kb)


async def folder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # format: folder:{id}:{page}
    parts = query.data.split(":")
    folder_id = parts[1]
    page = int(parts[2])
    folder_href = _folder_href(folder_id)
    client: MoySkladClient = context.bot_data["ms_client"]

    back_cb = context.user_data.get("nav_back", "catalog:root:0")

    subfolders = await client.get_subfolders(folder_href)
    if subfolders:
        context.user_data["nav_back"] = f"folder:{folder_id}:0"
        parent_cb = f"folder:{folder_id}"
        kb = folders_keyboard(subfolders, page, parent_cb, back_cb)
        await query.edit_message_text("📁 Подкатегории:", reply_markup=kb)
        return

    context.user_data["current_folder_id"] = folder_id
    products = await client.get_products(folder_href)
    if products is None:
        await query.edit_message_text(ERROR_MSG)
        return
    kb = products_keyboard(products, page, folder_id, True, back_cb)
    await query.edit_message_text(
        f"📦 Товаров в категории: {len(products)}",
        reply_markup=kb,
    )


async def plist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # format: plist:{folder_id}:{page}:{only_in_stock}
    parts = query.data.split(":")
    folder_id = parts[1]
    page = int(parts[2])
    only_in_stock = bool(int(parts[3]))
    folder_href = _folder_href(folder_id)
    client: MoySkladClient = context.bot_data["ms_client"]
    products = await client.get_products(folder_href)
    if products is None:
        await query.edit_message_text(ERROR_MSG)
        return
    back_cb = context.user_data.get("nav_back", "catalog:root:0")
    kb = products_keyboard(products, page, folder_id, only_in_stock, back_cb)
    await query.edit_message_reply_markup(reply_markup=kb)
