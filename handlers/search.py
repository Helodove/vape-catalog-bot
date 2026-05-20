import logging
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
from moysklad.client import MoySkladClient
from keyboards import search_results_keyboard

log = logging.getLogger(__name__)

WAITING_QUERY = 1


async def search_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🔍 Введите название товара для поиска:")
    return WAITING_QUERY


async def search_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("🔍 Введите название товара для поиска:")
    return WAITING_QUERY


async def search_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Введите название товара:")
        return WAITING_QUERY

    status = await update.message.reply_text("🔍 Ищу товары...")
    try:
        client: MoySkladClient = context.bot_data["ms_client"]
        products = await client.search_products(text)
    except Exception as e:
        log.error("Search error: %s", e)
        await status.edit_text("❌ Ошибка поиска, попробуйте позже.")
        return ConversationHandler.END

    if "search_results" not in context.bot_data:
        context.bot_data["search_results"] = {}
    context.bot_data["search_results"][update.effective_user.id] = products

    if not products:
        await status.edit_text("Товары не найдены 🔍")
        return ConversationHandler.END

    kb = search_results_keyboard(products, 0)
    await status.edit_text(f"🔍 Найдено товаров: {len(products)}", reply_markup=kb)
    return ConversationHandler.END


async def slist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    page = int(parts[1])
    products = context.bot_data.get("search_results", {}).get(query.from_user.id, [])
    if not products:
        await query.edit_message_text("Результаты поиска устарели. Выполните поиск заново.")
        return
    kb = search_results_keyboard(products, page)
    await query.edit_message_reply_markup(reply_markup=kb)
