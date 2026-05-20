from telegram import Update
from telegram.ext import ContextTypes
from moysklad.client import MoySkladClient
from keyboards import stores_keyboard, main_menu_keyboard

ERROR_MSG = "Не удалось загрузить список точек 🙏"


async def store_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    client: MoySkladClient = context.bot_data["ms_client"]
    stores = await client.get_stores()
    if not stores:
        await query.edit_message_text(ERROR_MSG)
        return
    current = context.user_data.get("store_id")
    kb = stores_keyboard(stores, current)
    await query.edit_message_text("📍 Выберите точку продаж:", reply_markup=kb)


async def store_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # format: store_pick:{store_id}  OR  store_pick:all
    parts = query.data.split(":", 1)
    store_id = parts[1] if len(parts) > 1 else "all"

    if store_id == "all":
        context.user_data.pop("store_id", None)
        context.user_data.pop("store_href", None)
        context.user_data.pop("store_name", None)
        store_label = "Все точки"
    else:
        client: MoySkladClient = context.bot_data["ms_client"]
        stores = await client.get_stores()
        store = next((s for s in stores if s.id == store_id), None)
        if store:
            context.user_data["store_id"] = store.id
            context.user_data["store_href"] = store.href
            context.user_data["store_name"] = store.name
            store_label = store.name
        else:
            store_label = "Неизвестная точка"

    await query.edit_message_text(
        f"✅ Точка: <b>{store_label}</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(context.user_data.get("store_name")),
    )
