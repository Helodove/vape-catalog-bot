"""
Бот для сотрудников TheVaper.
Позволяет каждому сотруднику выбрать магазин(ы) и получать уведомления о заказах.
"""
import re
import logging
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

log = logging.getLogger(__name__)
TABLE = "staff_subscriptions"


def _fmt_store(raw: str) -> str:
    """'г Липецк ул Космонавтов, 100' → 'Липецк · Космонавтов, 100'"""
    m = re.match(r'^г\s+(\S+)\s+(.+)$', raw.strip())
    if m:
        city = m.group(1)
        addr = re.sub(r'^(ул|пр|пл|пер|бул|наб|ш|пр-т)\s+', '', m.group(2), flags=re.IGNORECASE)
        return f"{city} · {addr}"
    return raw


class StaffBot:
    def __init__(self, token: str, supabase_url: str, supabase_key: str, ms_client):
        self.token = token
        self.supabase_url = supabase_url.rstrip("/")
        self.supabase_key = supabase_key
        self.ms_client = ms_client

    def _headers(self) -> dict:
        return {
            "apikey": self.supabase_key,
            "Authorization": f"Bearer {self.supabase_key}",
            "Content-Type": "application/json",
        }

    async def _get_subs(self, telegram_id: int) -> set[str]:
        try:
            async with httpx.AsyncClient(timeout=5) as http:
                r = await http.get(
                    f"{self.supabase_url}/rest/v1/{TABLE}",
                    params={"telegram_id": f"eq.{telegram_id}", "select": "store_id"},
                    headers=self._headers(),
                )
                if r.status_code == 200:
                    return {row["store_id"] for row in r.json()}
        except Exception as e:
            log.error("get_subs error: %s", e)
        return set()

    async def _toggle(self, telegram_id: int, store_id: str, store_name: str) -> bool:
        """Переключает подписку. Возвращает True если теперь подписан."""
        subs = await self._get_subs(telegram_id)
        async with httpx.AsyncClient(timeout=5) as http:
            if store_id in subs:
                await http.delete(
                    f"{self.supabase_url}/rest/v1/{TABLE}",
                    params={"telegram_id": f"eq.{telegram_id}", "store_id": f"eq.{store_id}"},
                    headers=self._headers(),
                )
                return False
            else:
                await http.post(
                    f"{self.supabase_url}/rest/v1/{TABLE}",
                    json={"telegram_id": telegram_id, "store_id": store_id, "store_name": store_name},
                    headers={**self._headers(), "Prefer": "return=minimal,resolution=merge-duplicates"},
                )
                return True

    async def _show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        stores = await self.ms_client.get_stores()
        subs = await self._get_subs(user_id)

        buttons = [
            [InlineKeyboardButton(
                f"{'✅' if s.id in subs else '⬜'} {_fmt_store(s.name)}",
                callback_data=f"sub:{s.id}",
            )]
            for s in stores
        ]
        markup = InlineKeyboardMarkup(buttons)
        text = (
            "🏪 Выберите магазины для получения уведомлений о заказах.\n\n"
            "✅ — уведомления включены\n⬜ — уведомления выключены"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=markup)
        else:
            await update.effective_message.reply_text(text, reply_markup=markup)

    async def _handle_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        store_id = query.data.split(":")[1]
        stores = await self.ms_client.get_stores()
        store = next((s for s in stores if s.id == store_id), None)
        store_name = _fmt_store(store.name) if store else store_id

        subscribed = await self._toggle(query.from_user.id, store_id, store_name)
        log.info("Staff %d → %s store '%s'",
                 query.from_user.id, "sub" if subscribed else "unsub", store_name)

        await self._show_menu(update, context)

    def build(self) -> Application:
        app = ApplicationBuilder().token(self.token).build()
        app.add_handler(CommandHandler("start", self._show_menu))
        app.add_handler(CommandHandler("shops", self._show_menu))
        app.add_handler(CallbackQueryHandler(self._handle_toggle, pattern="^sub:"))
        return app


async def notify_store_subscribers(
    store_id: str,
    message: str,
    supabase_url: str,
    supabase_key: str,
    staff_bot_token: str,
) -> None:
    """Отправляет уведомление о заказе всем сотрудникам, подписанным на магазин."""
    if not staff_bot_token or not supabase_url or not supabase_key:
        return
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(
                f"{supabase_url.rstrip('/')}/rest/v1/{TABLE}",
                params={"store_id": f"eq.{store_id}", "select": "telegram_id"},
                headers=headers,
            )
            if r.status_code != 200:
                log.warning("Failed to get subscribers: %s %s", r.status_code, r.text)
                return
            subscribers = r.json()
            if not subscribers:
                log.info("No staff subscribed to store %s", store_id)
                return
            for row in subscribers:
                try:
                    await http.post(
                        f"https://api.telegram.org/bot{staff_bot_token}/sendMessage",
                        json={"chat_id": row["telegram_id"], "text": message},
                    )
                except Exception as e:
                    log.error("Failed to notify staff %s: %s", row["telegram_id"], e)
            log.info("Notified %d staff for store %s", len(subscribers), store_id)
    except Exception as e:
        log.error("notify_store_subscribers error: %s", e)
