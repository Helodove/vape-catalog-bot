"""
Запись событий и чтение отчётов через Supabase REST (PostgREST) — как и остальной проект.
Нужен service_role ключ: на таблицах аналитики включён RLS без политик,
публичный (anon) ключ к ним доступа не имеет.
"""
import httpx


class StorageError(RuntimeError):
    pass


class AnalyticsStore:
    def __init__(self, supabase_url: str, service_key: str, timeout: float = 10):
        self.base = supabase_url.rstrip("/") + "/rest/v1"
        self.timeout = timeout
        self._headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }

    @property
    def configured(self) -> bool:
        return self.base != "/rest/v1" and bool(self._headers["apikey"])

    async def _post(self, path: str, body, prefer: str | None = None) -> httpx.Response:
        headers = dict(self._headers)
        if prefer:
            headers["Prefer"] = prefer
        async with httpx.AsyncClient(timeout=self.timeout) as http:
            r = await http.post(self.base + path, json=body, headers=headers)
        if r.status_code >= 300:
            raise StorageError(f"{path}: HTTP {r.status_code} {r.text[:300]}")
        return r

    async def insert_events(self, rows: list[dict]) -> None:
        """Пакетная вставка. Повтор той же пачки (тот же eid) молча игнорируется."""
        if rows:
            await self._post("/events?on_conflict=eid", rows,
                             prefer="return=minimal,resolution=ignore-duplicates")

    async def insert_outcome(self, row: dict) -> None:
        await self._post("/reserve_outcomes", row, prefer="return=minimal")

    async def rpc(self, function: str, params: dict):
        r = await self._post(f"/rpc/{function}", params)
        return r.json()
