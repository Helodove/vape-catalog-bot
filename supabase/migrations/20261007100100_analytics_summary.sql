-- Отчёт продуктовой аналитики за период: public.analytics_summary(p_from, p_to) → jsonb.
-- Единственный источник метрик: его вызывают GET /v1/analytics/summary и scripts/export_analytics_csv.py.
-- Применение: Supabase → SQL Editor → вставить файл целиком → Run (после 20261007100000_analytics_events.sql).
--
-- Определения (для раздела 3.4 ВКР):
--   * Период — календарные даты по московскому времени (UTC+3, в РФ нет перехода на летнее время),
--     обе границы включительно.
--   * Пользователь — уникальный user_hash. Заказы без валидного Telegram initData
--     (user_hash = 'unverified') в пользовательских метриках не участвуют, но учитываются
--     в количестве резервов по точкам.
--   * DAU — среднее (и максимум) числа уникальных пользователей за день по всем дням периода,
--     включая дни без активности. WAU / MAU — уникальные пользователи за последние 7 / 30 дней
--     периода (окно заканчивается в p_to). users_total — уникальные пользователи за весь период.
--   * Воронка «строгая» — пользователь засчитывается на шаге, только если совершил его ПОСЛЕ
--     предыдущего шага (первые наступления событий). «Нестрогая» — пользователь совершил
--     событие шага в периоде хотя бы раз, без учёта порядка (товар можно добавить в корзину
--     прямо из списка, минуя карточку, поэтому строгая воронка занижает шаг «резерв»).
--   * Выкуп — заказ, у которого есть reserve_outcomes.status = 'redeemed'. Принятое допущение:
--     созданный заказ считается выкупленным (raw_state = 'assumed_on_create').

create or replace function public.analytics_ratio(num bigint, den bigint)
returns numeric
language sql
immutable
as $$ select round(num::numeric / nullif(den, 0), 4) $$;

create or replace function public.analytics_summary(p_from date, p_to date)
returns jsonb
language sql
stable
set search_path = public
as $$
with
bounds as (
  select (p_from::timestamp       at time zone interval '+03:00') as t0,
         ((p_to + 1)::timestamp   at time zone interval '+03:00') as t1,
         ((p_to - 6)::timestamp   at time zone interval '+03:00') as w0,
         ((p_to - 29)::timestamp  at time zone interval '+03:00') as m0
),
ev_all as (
  select e.* from events e, bounds b where e.ts >= b.t0 and e.ts < b.t1
),
ev as (
  select * from ev_all where user_hash <> 'unverified'
),
redeemed_orders as (
  select distinct order_id from reserve_outcomes where status = 'redeemed'
),

-- ── активность ──
daily as (
  select d::date as day, count(distinct ev.user_hash) as dau
  from generate_series(p_from::timestamp, p_to::timestamp, interval '1 day') d
  left join ev on (ev.ts at time zone interval '+03:00')::date = d::date
  group by d::date
),
activity as (
  select
    (select count(distinct user_hash) from ev) as users_total,
    (select round(avg(dau), 2) from daily)     as dau_avg,
    (select max(dau) from daily)               as dau_max,
    (select count(distinct e.user_hash) from events e, bounds b
      where e.ts >= b.w0 and e.ts < b.t1 and e.user_hash <> 'unverified') as wau,
    (select count(distinct e.user_hash) from events e, bounds b
      where e.ts >= b.m0 and e.ts < b.t1 and e.user_hash <> 'unverified') as mau
),

-- ── воронка ──
s1 as (select user_hash, min(ts) as t from ev where event = 'app_open' group by user_hash),
s2 as (select ev.user_hash, min(ev.ts) as t from ev join s1 on s1.user_hash = ev.user_hash
        where ev.event = 'product_view' and ev.ts >= s1.t group by ev.user_hash),
s3 as (select ev.user_hash, min(ev.ts) as t from ev join s2 on s2.user_hash = ev.user_hash
        where ev.event = 'reserve_create' and ev.ts >= s2.t group by ev.user_hash),
s4 as (select distinct ev.user_hash from ev
         join s3 on s3.user_hash = ev.user_hash
         join redeemed_orders r on r.order_id = ev.order_id
        where ev.event = 'reserve_create' and ev.ts >= s3.t),
strict_counts as (
  select (select count(*) from s1) as c1, (select count(*) from s2) as c2,
         (select count(*) from s3) as c3, (select count(*) from s4) as c4
),
any_counts as (
  select count(distinct ev.user_hash) filter (where ev.event = 'app_open')                           as c1,
         count(distinct ev.user_hash) filter (where ev.event = 'product_view')                       as c2,
         count(distinct ev.user_hash) filter (where ev.event = 'reserve_create')                     as c3,
         count(distinct ev.user_hash) filter (where ev.event = 'reserve_create' and r.order_id is not null) as c4
  from ev left join redeemed_orders r on r.order_id = ev.order_id
),

-- ── поиск ──
searches as (
  select lower(btrim(meta->>'query')) as query, (meta->>'results_count')::int as results, user_hash
  from ev where event = 'search' and meta ? 'query'
),
top_search as (
  select query, count(*) as searches, count(distinct user_hash) as users, round(avg(results), 1) as avg_results
  from searches group by query order by searches desc, query limit 20
),
top_zero as (
  select query, count(*) as searches, count(distinct user_hash) as users
  from searches where results = 0 group by query order by searches desc, query limit 20
),

-- ── товары ──
views as (
  select product_id, user_hash, meta from ev where event = 'product_view' and product_id is not null
),
top_products as (
  select product_id, max(meta->>'name') as name, count(*) as views, count(distinct user_hash) as users,
         count(*) filter (where meta->>'in_stock' = 'false') as views_out_of_stock
  from views group by product_id order by views desc, product_id limit 20
),
views_total as (
  select count(*) as views, count(*) filter (where meta->>'in_stock' = 'false') as out_of_stock from views
),

-- ── резервы по точкам (все заказы, включая unverified) ──
reserves as (
  select distinct on (order_id) order_id, store_id, user_hash,
         meta->>'store_name' as store_name, (meta->>'sum')::numeric as sum
  from ev_all where event = 'reserve_create' and order_id is not null
  order by order_id, ts
),
by_store as (
  select r.store_id, max(r.store_name) as store_name, count(*) as reserves,
         count(*) filter (where ro.order_id is not null) as redeemed,
         analytics_ratio(count(*) filter (where ro.order_id is not null), count(*)) as redeemed_share,
         sum(r.sum) as reserves_sum
  from reserves r left join redeemed_orders ro on ro.order_id = r.order_id
  group by r.store_id
),
event_counts as (
  select event, count(*) as events, count(distinct user_hash) as users from ev_all group by event
),
reserve_errors as (
  select coalesce(meta->>'src', 'client') as src, count(*) as errors
  from ev_all where event = 'reserve_error' group by 1
)

select jsonb_build_object(
  'period', jsonb_build_object('from', p_from, 'to', p_to, 'timezone', 'UTC+03:00 (МСК)',
                               'days', p_to - p_from + 1),
  'activity', (select to_jsonb(a) || jsonb_build_object(
                  'stickiness_dau_mau', round(a.dau_avg / nullif(a.mau, 0), 4)) from activity a),
  'daily', coalesce((select jsonb_agg(to_jsonb(d) order by d.day) from daily d), '[]'::jsonb),
  'funnel', (
    select jsonb_build_object(
      'strict', jsonb_build_array(
        jsonb_build_object('step', 'app_open',       'users', s.c1, 'conv_from_prev', null,                     'conv_from_start', analytics_ratio(s.c1, s.c1)),
        jsonb_build_object('step', 'product_view',   'users', s.c2, 'conv_from_prev', analytics_ratio(s.c2, s.c1), 'conv_from_start', analytics_ratio(s.c2, s.c1)),
        jsonb_build_object('step', 'reserve_create', 'users', s.c3, 'conv_from_prev', analytics_ratio(s.c3, s.c2), 'conv_from_start', analytics_ratio(s.c3, s.c1)),
        jsonb_build_object('step', 'redeemed',       'users', s.c4, 'conv_from_prev', analytics_ratio(s.c4, s.c3), 'conv_from_start', analytics_ratio(s.c4, s.c1))
      ),
      'any_order', jsonb_build_array(
        jsonb_build_object('step', 'app_open',       'users', a.c1, 'conv_from_prev', null,                     'conv_from_start', analytics_ratio(a.c1, a.c1)),
        jsonb_build_object('step', 'product_view',   'users', a.c2, 'conv_from_prev', analytics_ratio(a.c2, a.c1), 'conv_from_start', analytics_ratio(a.c2, a.c1)),
        jsonb_build_object('step', 'reserve_create', 'users', a.c3, 'conv_from_prev', analytics_ratio(a.c3, a.c2), 'conv_from_start', analytics_ratio(a.c3, a.c1)),
        jsonb_build_object('step', 'redeemed',       'users', a.c4, 'conv_from_prev', analytics_ratio(a.c4, a.c3), 'conv_from_start', analytics_ratio(a.c4, a.c1))
      ),
      'redeem_rule', 'assumed_on_create'
    )
    from strict_counts s, any_counts a
  ),
  'search', jsonb_build_object(
    'top_queries',      coalesce((select jsonb_agg(to_jsonb(t) order by t.searches desc, t.query) from top_search t), '[]'::jsonb),
    'top_zero_results', coalesce((select jsonb_agg(to_jsonb(t) order by t.searches desc, t.query) from top_zero t), '[]'::jsonb)
  ),
  'products', jsonb_build_object(
    'top_viewed', coalesce((select jsonb_agg(to_jsonb(t) order by t.views desc, t.product_id) from top_products t), '[]'::jsonb),
    'views_total', (select views from views_total),
    'views_out_of_stock', (select out_of_stock from views_total),
    'out_of_stock_view_share', (select analytics_ratio(out_of_stock, views) from views_total)
  ),
  'reserves', jsonb_build_object(
    'total', (select count(*) from reserves),
    'unverified_users', (select count(*) from reserves where user_hash = 'unverified'),
    'redeemed', (select count(*) from reserves r join redeemed_orders ro on ro.order_id = r.order_id),
    'by_store', coalesce((select jsonb_agg(to_jsonb(t) order by t.reserves desc, t.store_id) from by_store t), '[]'::jsonb),
    'errors_by_source', coalesce((select jsonb_object_agg(src, errors) from reserve_errors), '{}'::jsonb)
  ),
  'events', coalesce((select jsonb_agg(to_jsonb(t) order by t.event) from event_counts t), '[]'::jsonb)
);
$$;

-- Supabase по умолчанию даёт EXECUTE роли anon — закрываем: отчёт только для бэкенда.
revoke execute on function public.analytics_summary(date, date) from public, anon, authenticated;
revoke execute on function public.analytics_ratio(bigint, bigint) from public, anon, authenticated;
grant  execute on function public.analytics_summary(date, date) to service_role;
grant  execute on function public.analytics_ratio(bigint, bigint) to service_role;
