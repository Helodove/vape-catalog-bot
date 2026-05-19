import asyncio
import os
import logging
from aiohttp import web
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
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
from handlers.admin import refresh_handler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


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


async def main() -> None:
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
    app.add_handler(search_conv)

    app.add_handler(CallbackQueryHandler(home_callback, pattern="^home$"))
    app.add_handler(CallbackQueryHandler(catalog_root_callback, pattern="^catalog:root:"))
    app.add_handler(CallbackQueryHandler(folder_callback, pattern="^folder:"))
    app.add_handler(CallbackQueryHandler(plist_callback, pattern="^plist:"))
    app.add_handler(CallbackQueryHandler(product_callback, pattern="^product:"))
    app.add_handler(CallbackQueryHandler(sproduct_callback, pattern="^sproduct:"))
    app.add_handler(CallbackQueryHandler(slist_callback, pattern="^slist:"))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    await run_web_server()
    log.info("Bot started")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
