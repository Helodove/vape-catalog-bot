"""
Проверяет, что список событий во фронте совпадает с бэкендом.

  python scripts/check_events_sync.py [путь к events.json фронта]

По умолчанию ищет ../thevaper-miniapp/src/lib/analytics/events.json.
Код выхода 1 — если файлы расходятся.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "analytics" / "events.json"
FRONTEND = ROOT.parent / "thevaper-miniapp" / "src" / "lib" / "analytics" / "events.json"


def main() -> int:
    frontend = Path(sys.argv[1]) if len(sys.argv) > 1 else FRONTEND
    if not frontend.exists():
        print(f"Не найден {frontend}")
        return 1
    back = json.loads(BACKEND.read_text(encoding="utf-8"))
    front = json.loads(frontend.read_text(encoding="utf-8"))
    if back != front:
        print("Списки событий расходятся. Скопируйте analytics/events.json во фронт:")
        print(f"  {BACKEND} → {frontend}")
        return 1
    print(f"OK: {len(back['events'])} событий совпадают")
    return 0


if __name__ == "__main__":
    sys.exit(main())
