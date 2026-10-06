-- Продуктовая аналитика Mini App: события и итоги резервов.
-- Применение: Supabase → SQL Editor → вставить файл целиком → Run. Повторный запуск безопасен.

create table if not exists public.events (
  id         bigserial primary key,
  ts         timestamptz not null default now(),  -- время события (сервер, с поправкой на часы клиента)
  user_hash  text not null,                        -- HMAC-SHA256(telegram user id, ANALYTICS_SALT)
  event      text not null,
  product_id text,
  store_id   text,
  order_id   text,
  meta       jsonb,
  eid        uuid                                   -- id события с клиента: защита от двойной доставки
);

create index if not exists events_event_ts_idx  on public.events (event, ts);
create index if not exists events_user_hash_idx on public.events (user_hash);
create index if not exists events_order_id_idx  on public.events (order_id);
create unique index if not exists events_eid_uidx on public.events (eid);

comment on table public.events is 'Продуктовая аналитика Mini App. Список событий: analytics/events.json';

create table if not exists public.reserve_outcomes (
  id         bigserial primary key,
  order_id   text not null,
  status     text not null check (status in ('redeemed', 'cancelled', 'pending')),  -- выкуплен / отменён / в ожидании
  outcome_ts timestamptz,
  sum        numeric(12, 2),
  raw_state  text,                       -- источник статуса; 'assumed_on_create' = допущение «создан ⇒ выкуплен»
  checked_at timestamptz not null default now()
);

create index if not exists reserve_outcomes_order_id_idx on public.reserve_outcomes (order_id);

-- RLS без политик: доступ только у service_role (бэкенд). Публичный anon-ключ ничего не увидит.
alter table public.events           enable row level security;
alter table public.reserve_outcomes enable row level security;

-- Явные права для бэкенда (в Supabase выдаются и по умолчанию — здесь для надёжности).
grant select, insert, update on public.events, public.reserve_outcomes to service_role;
grant usage, select on sequence public.events_id_seq, public.reserve_outcomes_id_seq to service_role;
