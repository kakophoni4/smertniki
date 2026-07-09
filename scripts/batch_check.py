#!/usr/bin/env python3
"""Пакетная проверка через ЕГРЮЛ ФНС."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.egrul import CompanySnapshot  # noqa: E402
from app.services.monitor import extract_ogrn_from_text  # noqa: E402
from app.services.rusprofile_client import RusprofileClient  # noqa: E402


def load_ids_from_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    ids: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ogrn = extract_ogrn_from_text(line.split(";")[0].split(",")[0].split("\t")[0])
        if ogrn:
            ids.append(ogrn)
    return list(dict.fromkeys(ids))


def fmt_snap(snap: CompanySnapshot, error: str | None = None) -> str:
    if error:
        return f"❌ {snap.ogrn} — ERROR: {error}"
    flags = []
    if snap.unreliable_address:
        flags.append("addr")
    if snap.unreliable_director:
        flags.append("dir")
    if snap.unreliable_founder:
        flags.append("found")
    if snap.is_liquidating:
        flags.append("liq")
    if snap.is_liquidated:
        flags.append("dead")
    flag_str = ",".join(flags) if flags else "ok"
    name = snap.short_name or snap.name or "—"
    return (
        f"{'⚠️' if snap.has_any_issue() else '✅'} {snap.ogrn} | {name}\n"
        f"   ИНН: {snap.inn or '—'} | flags: [{flag_str}] | {snap.status_text or '—'}\n"
        f"   signals: {snap.raw_summary}"
    )


async def run(ids: list[str], json_out: Path | None) -> int:
    client = RusprofileClient()
    await client.start()
    results: list[dict] = []
    errors = 0
    print(f"Проверяю {len(ids)} компаний через ЕГРЮЛ...\n")
    try:
        for i, ogrn in enumerate(ids, 1):
            print(f"[{i}/{len(ids)}] {ogrn}")
            try:
                snap = await client.get_snapshot(ogrn)
                print(fmt_snap(snap))
                results.append({"ok": True, **snap.to_dict()})
            except Exception as exc:
                errors += 1
                print(fmt_snap(CompanySnapshot(ogrn=ogrn), error=str(exc)))
                results.append({"ok": False, "ogrn": ogrn, "error": str(exc)})
            print()
    finally:
        await client.close()

    ok = sum(1 for r in results if r.get("ok"))
    print("=" * 50)
    print(f"Итого: {len(ids)} | успешно: {ok} | ошибок: {errors}")
    if json_out:
        json_out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON: {json_out}")
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ogrn", nargs="*")
    parser.add_argument("--file", "-f", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    ids: list[str] = []
    if args.file:
        ids.extend(load_ids_from_file(args.file))
    for item in args.ogrn:
        ogrn = extract_ogrn_from_text(item)
        if ogrn:
            ids.append(ogrn)
    ids = list(dict.fromkeys(ids))
    if not ids:
        parser.error("Нужен ОГРН или --file")
    raise SystemExit(asyncio.run(run(ids, args.json)))


if __name__ == "__main__":
    main()
