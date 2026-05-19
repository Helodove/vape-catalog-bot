import asyncio
import os
import logging
import traceback
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, BadRequest) and "not modified" in str(context.error).lower():
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


async def health_check(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def run_web_server() -> None:
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health check server started on port %d", port)


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
    app.add_handler(CallbackQueryHandler(catalog_root_callback, pattern="^catalog:root:"))
    app.add_handler(CallbackQueryHandler(folder_callback, pattern="^folder:"))
    app.add_handler(CallbackQueryHandler(plist_callback, pattern="^plist:"))
    app.add_handler(CallbackQueryHandler(product_callback, pattern="^product:"))
    app.add_handler(CallbackQueryHandler(sproduct_callback, pattern="^sproduct:"))
    app.add_handler(CallbackQueryHandler(slist_callback, pattern="^slist:"))
    app.add_error_handler(error_handler)
    return app


async def main() -> None:
    # Health check стартует первым — Render сразу видит сервис живым
    await run_web_server()

    app = build_app()
    await app.initialize()
    await app.start()

    # Повтор при конфликте (два экземпляра во время деплоя)
    for attempt in range(10):
        try:
            await app.updater.start_polling(drop_pending_updates=True)
            log.info("Bot polling started")
            break
        except Conflict:
            log.warning("Conflict on polling start, retrying in 5s (attempt %d/10)", attempt + 1)
            await asyncio.sleep(5)

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
