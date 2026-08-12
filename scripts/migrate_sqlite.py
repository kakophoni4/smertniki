"""Прогон ensure_columns для уже существующей SQLite БД."""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import init_db


async def main() -> None:
    await init_db()
    print("migrate ok")


if __name__ == "__main__":
    asyncio.run(main())
