import asyncio
import os
import signal
import logging
import re
import traceback
from datetime import datetime, timezone
import httpx
from aiohttp import web
from telegram import Update, MenuButtonWebApp, WebAppInfo
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
    from telegram.error import TimedOut as TgTimedOut
    if isinstance(context.error, TgTimedOut):
        log.warning("Telegram TimedOut (network blip), ignoring")
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


def _clean_post_text(text: str) -> str:
    """Первая читаемая строка поста: убирает кастомные эмодзи Telegram и ведущие символы-буллеты."""
    # Убираем символы из Unicode Private Use Area (кастомные эмодзи Telegram)
    text = re.sub(r'[-]', '', text)
    text = re.sub(r'[\U000F0000-\U000FFFFF]', '', text)
    # Ищем первую непустую строку без ведущих спецсимволов
    for line in text.splitlines():
        line = line.strip()
        line = re.sub(r'^[\s■●•▪▸►→\-\*#🔹🔸▶️]+\s*', '', line).strip()
        if len(line) > 2:
            return line
    return text.strip()[:120]


async def _upload_post_photo(file_bytes: bytes, post_id: int) -> str | None:
    """Загружает фото поста в Supabase Storage, возвращает публичный URL."""
    if not settings.supabase_url or not settings.supabase_service_key:
        return None
    bucket = "post-photos"
    filename = f"post_{post_id}.jpg"
    headers = {
        "apikey": settings.supabase_service_key,
        "Authorization": f"Bearer {settings.supabase_service_key}",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            # Создаём бакет (если не существует — ошибка игнорируется)
            await http.post(
                f"{settings.supabase_url}/storage/v1/bucket",
                headers={**headers, "Content-Type": "application/json"},
                json={"id": bucket, "name": bucket, "public": True},
            )
            # Загружаем файл (upsert = перезапись при следующих постах)
            r = await http.post(
                f"{settings.supabase_url}/storage/v1/object/{bucket}/{filename}",
                headers={**headers, "Content-Type": "image/jpeg", "x-upsert": "true"},
                content=file_bytes,
            )
            if r.status_code in (200, 201):
                return f"{settings.supabase_url}/storage/v1/object/public/{bucket}/{filename}"
            log.warning("Storage upload status %d: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Channel post: photo upload failed: %s", e)
    return None


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Новый пост в канале → обновляем баннер в мини-апп через Supabase."""
    post = update.channel_post or update.edited_channel_post
    if not post:
        return

    raw_text = post.text or post.caption or ""
    text = _clean_post_text(raw_text)
    photo_url: str | None = None

    if post.photo:
        largest = max(post.photo, key=lambda p: p.file_size or 0)
        try:
            tg_file = await context.bot.get_file(largest.file_id)
            file_bytes = bytes(await tg_file.download_as_bytearray())
            photo_url = await _upload_post_photo(file_bytes, post.message_id)
        except Exception as e:
            log.warning("Channel post: failed to get photo: %s", e)

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
                    "photo_file_path": photo_url,
                    "date": int(post.date.timestamp()) if post.date else None,
                    "post_url": post_url,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        if r.status_code in (200, 204):
            log.info("Баннер обновлён: пост %d, фото=%s", post.message_id, "да" if photo_url else "нет")
    except Exception as e:
        log.error("Ошибка обновления Supabase: %s", e)


WEBHOOK_PATH = "/telegram-webhook"


async def health_check(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def telegram_webhook(request: web.Request) -> web.Response:
    tg_app = request.app["tg_app"]
    try:
        data = await request.json()
        update = Update.de_json(data, tg_app.bot)
        await tg_app.process_update(update)
    except Exception as e:
        log.error("Webhook processing error: %s", e)
    return web.Response(text="OK")


async def run_web_server(ms_client: MoySkladClient, tg_app) -> web.AppRunner:
    port = int(os.environ.get("PORT", 80))
    app = web.Application()
    app["ms_client"] = ms_client
    app["tg_app"] = tg_app
    app.router.add_get("/", health_check)
    app.router.add_post(WEBHOOK_PATH, telegram_webhook)
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
    return runner


def build_app():
    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .connect_timeout(60)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(build_notify_conv())
    app.add_handler(CallbackQueryHandler(notify_ack, pattern="^notify_ack$"))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))
    app.add_error_handler(error_handler)
    return app


async def main() -> None:
    ms_client = MoySkladClient(settings.moysklad_token)

    tg_app = build_app()

    # Start web server first so Amvera health checks pass immediately
    # Keep reference to runner so it's not garbage collected
    _runner = await run_web_server(ms_client, tg_app)

    for attempt in range(1, 6):
        try:
            await tg_app.initialize()
            break
        except Exception as e:
            log.warning("Telegram init attempt %d/5 failed: %s", attempt, e)
            if attempt == 5:
                raise
            await asyncio.sleep(10 * attempt)

    await tg_app.start()

    # Polling mode: Amvera blocks incoming Telegram connections (webhook doesn't work)
    await tg_app.bot.delete_webhook(drop_pending_updates=True)
    await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    log.info("Bot polling started")

    # Кнопка меню рядом с полем ввода — открывает мини-апп
    if settings.miniapp_origin:
        try:
            await tg_app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Каталог",
                    web_app=WebAppInfo(url=settings.miniapp_origin),
                )
            )
            log.info("Menu button set to Mini App")
        except Exception as e:
            log.warning("Could not set menu button: %s", e)

    if settings.staff_bot_token:
        try:
            staff = StaffBot(
                token=settings.staff_bot_token,
                supabase_url=settings.supabase_url,
                supabase_key=settings.supabase_service_key,
                ms_client=ms_client,
            )
            staff_app = staff.build()
            for attempt in range(1, 4):
                try:
                    await staff_app.initialize()
                    break
                except Exception as e:
                    log.warning("Staff bot init attempt %d/3 failed: %s", attempt, e)
                    if attempt == 3:
                        raise
                    await asyncio.sleep(10 * attempt)
            await staff_app.start()
            await staff_app.updater.start_polling(drop_pending_updates=True)
            log.info("Staff bot polling started")
        except Exception as e:
            log.error("Staff bot failed to start, continuing without it: %s", e)

    # Ждём сигнала остановки (SIGTERM от Amvera при деплое)
    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    log.info("Bot ready")
    await stop_event.wait()

    log.info("Shutting down...")
    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
