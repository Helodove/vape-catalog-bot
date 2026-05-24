import asyncio
import os
import logging
import traceback
from datetime import datetime, timezone
import httpx
from aiohttp import web
from telegram import Update
from telegram.error import BadRequest, Conflict
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from config import settings
from moysklad.client import MoySkladClient
from handlers.start import start_handler, home_callback
from handlers.catalog import (
    catalog_handler,
    catalog_root_callback,
    folder_callback,
    plist_callback,
)
from handlers.product import product_callback, sproduct_callback
from handlers.search import (
    search_start_command,
    search_start_callback,
    search_query_handler,
    slist_callback,
    WAITING_QUERY,
)
from handlers.admin import refresh_handler, debug_handler
from handlers.store import store_list_callback, store_select_callback
from miniapp_api import register_miniapp_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err_str = str(context.error).lower()
    if isinstance(context.error, BadRequest) and any(s in err_str for s in (
        "not modified", "query is too old", "query id is invalid"
    )):
        return
    if isinstance(context.error, Conflict):
        log.warning("Telegram Conflict error (duplicate instance), ignoring")
        return
    err = "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))
    log.error("Unhandled exception:\n%s", err)
    try:
        await context.bot.send_message(
            chat_id=settings.admin_chat_id,
            text=f"⚠️ Ошибка бота:\n<pre>{err[-3000:]}</pre>",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Новый пост в канале → обновляем баннер в мини-апп через Supabase."""
    post = update.channel_post or update.edited_channel_post
    if not post:
        return

    text = post.text or post.caption or ""
    photo_file_path: str | None = None

    if post.photo:
        largest = max(post.photo, key=lambda p: p.file_size or 0)
        try:
            file = await context.bot.get_file(largest.file_id)
            photo_file_path = file.file_path
        except Exception as e:
            log.warning("Channel post: failed to get photo path: %s", e)

    channel_id = str(post.chat.id)  # например -1001234567890
    channel_short = str(abs(int(channel_id)))[3:]  # убираем -100
    post_url = f"https://t.me/c/{channel_short}/{post.message_id}"

    if not settings.supabase_url or not settings.supabase_service_key:
        log.warning("Supabase не настроен, баннер не обновлён")
        return

    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.patch(
                f"{settings.supabase_url}/rest/v1/latest_post?id=eq.1",
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json={
                    "text": text,
                    "photo_file_path": photo_file_path,
                    "date": int(post.date.timestamp()) if post.date else None,
                    "post_url": post_url,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        if r.status_code in (200, 204):
            log.info("Баннер обновлён: пост %d из канала", post.message_id)
        else:
            log.error("Supabase PATCH ошибка: %d %s", r.status_code, r.text[:200])
    except Exception as e:
        log.error("Ошибка обновления Supabase: %s", e)


async def health_check(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def run_web_server(ms_client: MoySkladClient) -> None:
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app["ms_client"] = ms_client
    app.router.add_get("/", health_check)
    register_miniapp_routes(
        app,
        ms_token=settings.moysklad_token,
        bot_base_url=settings.bot_base_url,
        miniapp_origin=settings.miniapp_origin,
        supabase_url=settings.supabase_url,
        supabase_key=settings.supabase_service_key,
        bot_token=settings.telegram_bot_token,
        admin_chat_id=str(settings.admin_chat_id),
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Web server started on port %d (health + mini app API)", port)


def build_app():
    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    app.bot_data["ms_client"] = MoySkladClient(settings.moysklad_token)

    search_conv = ConversationHandler(
        entry_points=[
            CommandHandler("search", search_start_command),
            CallbackQueryHandler(search_start_callback, pattern="^search:start$"),
        ],
        states={
            WAITING_QUERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_query_handler),
            ],
        },
        fallbacks=[CommandHandler("start", start_handler)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("catalog", catalog_handler))
    app.add_handler(CommandHandler("refresh", refresh_handler))
    app.add_handler(CommandHandler("debug", debug_handler))
    app.add_handler(search_conv)

    app.add_handler(CallbackQueryHandler(home_callback, pattern="^home$"))
    app.add_handler(CallbackQueryHandler(store_list_callback, pattern="^store:list$"))
    app.add_handler(CallbackQueryHandler(store_select_callback, pattern="^store_pick:"))
    app.add_handler(CallbackQueryHandler(catalog_root_callback, pattern="^catalog:root:"))
    app.add_handler(CallbackQueryHandler(folder_callback, pattern="^folder:"))
    app.add_handler(CallbackQueryHandler(plist_callback, pattern="^plist:"))
    app.add_handler(CallbackQueryHandler(product_callback, pattern="^product:"))
    app.add_handler(CallbackQueryHandler(sproduct_callback, pattern="^sproduct:"))
    app.add_handler(CallbackQueryHandler(slist_callback, pattern="^slist:"))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))
    app.add_error_handler(error_handler)
    return app


async def main() -> None:
    ms_client = MoySkladClient(settings.moysklad_token)
    await run_web_server(ms_client)

    tg_app = build_app()
    tg_app.bot_data["ms_client"] = ms_client
    await tg_app.initialize()
    await tg_app.start()

    # Повтор при конфликте (два экземпляра во время деплоя)
    for attempt in range(10):
        try:
            await tg_app.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
            )
            log.info("Bot polling started")
            break
        except Conflict:
            log.warning("Conflict on polling start, retrying in 5s (attempt %d/10)", attempt + 1)
            await asyncio.sleep(5)

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
