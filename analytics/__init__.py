"""
Продуктовая аналитика Mini App: события пользователей и воронка
«открытие → просмотр → резерв → выкуп».

Состав:
  events.json      — единый список событий и их схема (копия во фронте)
  schema.py        — валидация событий по events.json
  telegram_auth.py — проверка подписи Telegram initData
  hashing.py       — user_hash = HMAC-SHA256(user.id, ANALYTICS_SALT)
  rate_limit.py    — простой лимит запросов по user_hash
  storage.py       — запись/чтение через Supabase REST
  routes.py        — POST /v1/events, GET /v1/analytics/summary, трекинг заказов
"""
