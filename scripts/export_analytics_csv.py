"""
Выгрузка метрик продуктовой аналитики в CSV по месяцам — для таблиц ВКР.

  python scripts/export_analytics_csv.py --from 2026-06 --to 2026-12 --out exports

Каждый месяц считается той же функцией public.analytics_summary, что и GET /v1/analytics/summary,
поэтому цифры совпадают. Текущий месяц выгружается по сегодняшний день (МСК).
Рядом с CSV сохраняется сырой ответ (raw/<месяц>.json) — для воспроизводимости.

CSV по умолчанию в формате русского Excel: разделитель «;», десятичная запятая, UTF-8 с BOM.
Флаг --plain даёт обычный CSV (запятая, точка).

Переменные окружения (или .env): SUPABASE_URL, SUPABASE_SERVICE_KEY.
"""
import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from analytics.routes import MSK  # noqa: E402
from analytics.storage import AnalyticsStore  # noqa: E402

FUNNEL_LABELS = {"app_open": "Открытие", "product_view": "Просмотр товара",
                 "reserve_create": "Резерв", "redeemed": "Выкуп"}


def parse_month(value: str) -> date:
    return datetime.strptime(value, "%Y-%m").date().replace(day=1)


def month_ranges(first: date, last: date, today: date):
    m = first
    while m <= last:
        nxt = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = min(nxt - timedelta(days=1), today)
        if m <= today:
            yield m.strftime("%Y-%m"), m, end
        m = nxt


class Writer:
    def __init__(self, out: Path, plain: bool):
        self.out, self.plain = out, plain
        self.rows: dict[str, list[list]] = {}
        self.headers: dict[str, list[str]] = {}

    def add(self, name: str, header: list[str], row: list) -> None:
        self.headers.setdefault(name, header)
        self.rows.setdefault(name, []).append(row)

    def _fmt(self, v):
        if v is None:
            return ""
        if isinstance(v, float) and not self.plain:
            return f"{v:.4f}".rstrip("0").rstrip(".").replace(".", ",")
        return v

    def save(self) -> list[Path]:
        paths = []
        for name, rows in self.rows.items():
            path = self.out / f"{name}.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f, delimiter="," if self.plain else ";")
                w.writerow(self.headers[name])
                for row in rows:
                    w.writerow([self._fmt(v) for v in row])
            paths.append(path)
        return paths


def collect(w: Writer, month: str, s: dict) -> None:
    a, p, r = s["activity"], s["products"], s["reserves"]
    w.add("monthly_kpi",
          ["month", "days", "users_total", "dau_avg", "dau_max", "wau", "mau", "stickiness_dau_mau",
           "product_views", "out_of_stock_view_share", "reserves", "redeemed", "reserves_unverified_users"],
          [month, s["period"]["days"], a["users_total"], a["dau_avg"], a["dau_max"], a["wau"], a["mau"],
           a["stickiness_dau_mau"], p["views_total"], p["out_of_stock_view_share"],
           r["total"], r["redeemed"], r["unverified_users"]])

    for kind, label in (("strict", "строгая"), ("any_order", "нестрогая")):
        for step in s["funnel"][kind]:
            w.add("monthly_funnel",
                  ["month", "funnel", "step", "step_ru", "users", "conv_from_prev", "conv_from_start"],
                  [month, label, step["step"], FUNNEL_LABELS[step["step"]], step["users"],
                   step["conv_from_prev"], step["conv_from_start"]])

    for i, q in enumerate(s["search"]["top_queries"], 1):
        w.add("monthly_top_search", ["month", "rank", "query", "searches", "users", "avg_results"],
              [month, i, q["query"], q["searches"], q["users"], q["avg_results"]])
    for i, q in enumerate(s["search"]["top_zero_results"], 1):
        w.add("monthly_zero_result_search", ["month", "rank", "query", "searches", "users"],
              [month, i, q["query"], q["searches"], q["users"]])
    for i, t in enumerate(p["top_viewed"], 1):
        w.add("monthly_top_products",
              ["month", "rank", "product_id", "name", "views", "users", "views_out_of_stock"],
              [month, i, t["product_id"], t["name"], t["views"], t["users"], t["views_out_of_stock"]])
    for t in r["by_store"]:
        w.add("monthly_stores",
              ["month", "store_id", "store_name", "reserves", "redeemed", "redeemed_share", "reserves_sum"],
              [month, t["store_id"], t["store_name"], t["reserves"], t["redeemed"],
               t["redeemed_share"], t["reserves_sum"]])


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="first", required=True, help="первый месяц, ГГГГ-ММ")
    ap.add_argument("--to", dest="last", help="последний месяц, ГГГГ-ММ (по умолчанию — текущий)")
    ap.add_argument("--out", default="exports", help="папка для CSV (по умолчанию exports)")
    ap.add_argument("--plain", action="store_true", help="обычный CSV: запятая и десятичная точка")
    args = ap.parse_args()

    load_dotenv()
    url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        sys.exit("Нужны SUPABASE_URL и SUPABASE_SERVICE_KEY (в окружении или .env)")
    store = AnalyticsStore(url, key, timeout=60)

    today = datetime.now(MSK).date()
    first = parse_month(args.first)
    last = parse_month(args.last) if args.last else today.replace(day=1)
    out = Path(args.out)
    (out / "raw").mkdir(parents=True, exist_ok=True)

    w = Writer(out, args.plain)
    for month, d_from, d_to in month_ranges(first, last, today):
        summary = await store.rpc("analytics_summary", {"p_from": d_from.isoformat(), "p_to": d_to.isoformat()})
        (out / "raw" / f"{month}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        collect(w, month, summary)
        print(f"{month}: {d_from} … {d_to} — пользователей {summary['activity']['users_total']}, "
              f"резервов {summary['reserves']['total']}")

    for path in w.save():
        print("→", path)


if __name__ == "__main__":
    asyncio.run(main())
