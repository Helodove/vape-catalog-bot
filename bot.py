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
    MessageHandler,
    filters,
    ContextTypes,
)
from config import settings
from moysklad.client import MoySkladClient
from handlers.start import start_handler
from handlers.notify import build_notify_conv, notify_ack
from miniapp_api import register_miniapp_routes
from staff_bot import StaffBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err_str = str(context.error).lower()
    if isinstance(context.error, BadRequest) and any(s in err_str for s in (
        "not modified", "query is too old", "query id is invalid",
        "message to delete not found", "message can't be deleted",
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

    channel_id = str(post.chat.id)
    channel_short = str(abs(int(channel_id)))[3:]
    post_url = f"https://t.me/c/{channel_short}/{post.message_id}"

    if not settings.supabase_url or not settings.supabase_service_key:
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
            log.info("Баннер обновлён: пост %d", post.message_id)
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
        staff_bot_token=settings.staff_bot_token,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Web server started on port %d", port)


def build_app():
    app = ApplicationBuilder().token(settings.telegram_bot_token).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(build_notify_conv())
    app.add_handler(CallbackQueryHandler(notify_ack, pattern="^notify_ack$"))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))
    app.add_error_handler(error_handler)
    return app


async def main() -> None:
    ms_client = MoySkladClient(settings.moysklad_token)
    await run_web_server(ms_client)

    tg_app = build_app()
    await tg_app.initialize()
    await tg_app.start()

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

    if settings.staff_bot_token:
        staff = StaffBot(
            token=settings.staff_bot_token,
            supabase_url=settings.supabase_url,
            supabase_key=settings.supabase_service_key,
            ms_client=ms_client,
        )
        staff_app = staff.build()
        await staff_app.initialize()
        await staff_app.start()
        await staff_app.updater.start_polling(drop_pending_updates=True)
        log.info("Staff bot polling started")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
