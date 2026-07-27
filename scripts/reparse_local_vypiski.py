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
from app.db.session import SessionLocal
from app.services.egrul import extract_text_from_pdf, parse_vypiska_text
from app.services.monitor import apply_snapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reparse")


async def main(dry_run: bool) -> None:
    vypiski = Path(settings.vypiski_dir)
    async with SessionLocal() as session:
        companies = (
            await session.scalars(select(Company).where(Company.is_active.is_(True)).order_by(Company.id))
        ).all()
        logger.info("active companies: %s, dir: %s", len(companies), vypiski)

        ok = missing = errors = changed_liq = 0
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

            prev_liq = bool(company.is_liquidating or company.is_liquidated)
            new_liq = bool(snap.is_liquidating or snap.is_liquidated)
            if prev_liq != new_liq:
                changed_liq += 1
                logger.info(
                    "LIQ change inn=%s ogrn=%s %s -> %s signals=%s",
                    company.inn,
                    company.ogrn,
                    prev_liq,
                    new_liq,
                    snap.signals,
                )

            if dry_run:
                ok += 1
                continue

            await apply_snapshot(session, company, snap)
            ok += 1

        from sqlalchemy import func

        open_liq_n = await session.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.status == TicketStatus.IN_PROGRESS, Ticket.issue_type == "liquidation")
        )
        open_all = await session.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.status == TicketStatus.IN_PROGRESS)
        )

        logger.info(
            "done ok=%s missing_pdf=%s errors=%s liq_flag_changes=%s dry_run=%s",
            ok,
            missing,
            errors,
            changed_liq,
            dry_run,
        )
        logger.info("tickets_open_liquidation=%s tickets_open_all=%s", open_liq_n, open_all)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
