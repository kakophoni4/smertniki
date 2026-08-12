"""Переразбор уже скачанных PDF без запросов в ЕГРЮЛ.

Обновляет флаги компаний и автоматически «лечит»/открывает тикеты
через apply_snapshot (monitor).

Usage:
  python -m scripts.reparse_local_vypiski
  python -m scripts.reparse_local_vypiski --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from sqlalchemy import select

# allow `python scripts/reparse_local_vypiski.py`
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.db.models import Company, Ticket, TicketStatus
from app.db.session import SessionLocal, init_db
from app.services.egrul import extract_text_from_pdf, parse_vypiska_text
from app.services.monitor import apply_snapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reparse")


async def main(dry_run: bool) -> None:
    await init_db()
    vypiski = Path(settings.vypiski_dir)
    async with SessionLocal() as session:
        companies = (
            await session.scalars(select(Company).where(Company.is_active.is_(True)).order_by(Company.id))
        ).all()
        logger.info("active companies: %s, dir: %s", len(companies), vypiski)

        ok = missing = errors = changed = 0
        for company in companies:
            pdf_path = vypiski / f"{company.ogrn}.pdf"
            if not pdf_path.exists():
                missing += 1
                logger.warning("no pdf for %s inn=%s", company.ogrn, company.inn)
                continue
            try:
                text = extract_text_from_pdf(pdf_path.read_bytes())
                snap = parse_vypiska_text(text, company.ogrn)
            except Exception as exc:
                errors += 1
                logger.exception("parse failed %s: %s", company.ogrn, exc)
                continue

            prev = {
                "addr": bool(company.unreliable_address),
                "dir": bool(company.unreliable_director),
                "found": bool(company.unreliable_founder),
                "liq": bool(company.is_liquidating or company.is_liquidated),
            }
            new = {
                "addr": bool(snap.unreliable_address),
                "dir": bool(snap.unreliable_director),
                "found": bool(snap.unreliable_founder),
                "liq": bool(snap.is_liquidating or snap.is_liquidated),
            }
            if prev != new:
                changed += 1
                logger.info(
                    "FLAGS inn=%s ogrn=%s %s -> %s signals=%s since=%s",
                    company.inn,
                    company.ogrn,
                    prev,
                    new,
                    snap.signals,
                    {
                        "addr": snap.unreliable_address_since.date().isoformat()
                        if snap.unreliable_address_since
                        else None,
                        "dir": snap.unreliable_director_since.date().isoformat()
                        if snap.unreliable_director_since
                        else None,
                        "found": snap.unreliable_founder_since.date().isoformat()
                        if snap.unreliable_founder_since
                        else None,
                    },
                )
            elif snap.unreliable_address or snap.unreliable_director or snap.unreliable_founder:
                logger.info(
                    "SINCE inn=%s addr=%s dir=%s found=%s",
                    company.inn,
                    snap.unreliable_address_since.date().isoformat()
                    if snap.unreliable_address_since
                    else None,
                    snap.unreliable_director_since.date().isoformat()
                    if snap.unreliable_director_since
                    else None,
                    snap.unreliable_founder_since.date().isoformat()
                    if snap.unreliable_founder_since
                    else None,
                )

            if dry_run:
                ok += 1
                continue

            await apply_snapshot(session, company, snap)
            ok += 1

        from sqlalchemy import func

        open_by = {}
        for issue in ("address", "director", "founder", "liquidation"):
            open_by[issue] = await session.scalar(
                select(func.count())
                .select_from(Ticket)
                .where(Ticket.status == TicketStatus.IN_PROGRESS, Ticket.issue_type == issue)
            )
        open_all = await session.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.status == TicketStatus.IN_PROGRESS)
        )

        logger.info(
            "done ok=%s missing_pdf=%s errors=%s flag_changes=%s dry_run=%s",
            ok,
            missing,
            errors,
            changed,
            dry_run,
        )
        logger.info("tickets_open=%s by_type=%s", open_all, open_by)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
