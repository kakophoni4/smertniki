#!/usr/bin/env python3
"""Резолв ИНН → ОГРН через поиск Rusprofile (отдельно от мониторинга).

Примеры:
  python scripts/resolve_inn.py 9731112429 9726027672
  python scripts/resolve_inn.py --file inns.txt --out ogrns.txt
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.rusprofile_client import (  # noqa: E402
    RusprofileClient,
    normalize_inn,
)


def load_inns(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    inns: list[str] = []
    for line in text.replace(",", " ").replace(";", " ").split():
        inn = normalize_inn(line)
        if inn:
            inns.append(inn)
    return list(dict.fromkeys(inns))


async def run(inns: list[str], out: Path | None, csv_out: Path | None) -> int:
    client = RusprofileClient()
    await client.start()
    rows: list[dict[str, str]] = []
    ogrns: list[str] = []
    errors = 0

    print(f"Резолвлю {len(inns)} ИНН → ОГРН...\n")
    try:
        for i, inn in enumerate(inns, 1):
            result = await client.resolve_inn(inn)
            if result.ogrn:
                print(f"[{i}/{len(inns)}] ✅ {inn} → {result.ogrn}  {result.name or ''}")
                ogrns.append(result.ogrn)
                rows.append(
                    {
                        "inn": inn,
                        "ogrn": result.ogrn,
                        "name": result.name or "",
                        "url": f"https://www.rusprofile.ru/id/{result.ogrn}",
                        "error": "",
                    }
                )
            else:
                errors += 1
                print(f"[{i}/{len(inns)}] ❌ {inn} — {result.error}")
                rows.append(
                    {
                        "inn": inn,
                        "ogrn": "",
                        "name": "",
                        "url": "",
                        "error": result.error or "unknown",
                    }
                )
    finally:
        await client.close()

    if out:
        out.write_text("\n".join(ogrns) + ("\n" if ogrns else ""), encoding="utf-8")
        print(f"\nОГРН сохранены: {out} ({len(ogrns)} шт.)")

    if csv_out:
        with csv_out.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["inn", "ogrn", "name", "url", "error"])
            writer.writeheader()
            writer.writerows(rows)
        print(f"CSV: {csv_out}")

    print(f"\nИтого: {len(inns)} | ок: {len(ogrns)} | ошибок: {errors}")
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="ИНН → ОГРН (Rusprofile search)")
    parser.add_argument("inn", nargs="*", help="ИНН")
    parser.add_argument("--file", "-f", type=Path, help="Файл со списком ИНН")
    parser.add_argument("--out", "-o", type=Path, help="Файл только с ОГРН (для /import_file)")
    parser.add_argument("--csv", type=Path, help="CSV inn,ogrn,name,url,error")
    args = parser.parse_args()

    inns: list[str] = []
    if args.file:
        inns.extend(load_inns(args.file))
    for item in args.inn:
        inn = normalize_inn(item)
        if inn:
            inns.append(inn)
    inns = list(dict.fromkeys(inns))
    if not inns:
        parser.error("Передай ИНН аргументами или --file")

    raise SystemExit(asyncio.run(run(inns, args.out, args.csv)))


if __name__ == "__main__":
    main()
