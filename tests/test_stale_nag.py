from datetime import datetime, timedelta, timezone

from app.services.monitor import DEXTER_NAG_LINES, _dexter_nag_opener, days_word, ticket_age_days


def test_days_word():
    assert days_word(1) == "день"
    assert days_word(2) == "дня"
    assert days_word(5) == "дней"
    assert days_word(11) == "дней"
    assert days_word(21) == "день"
    assert days_word(62) == "дня"
    assert days_word(100) == "дней"


def test_ticket_age_days():
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
    created = now - timedelta(days=75)
    assert ticket_age_days(created, now=now) == 75
    assert ticket_age_days(None, now=now) == 0


def test_dexter_nag_lines_unique_in_batch():
    used: set[int] = set()
    lines = [_dexter_nag_opener(75, used=used) for _ in range(min(10, len(DEXTER_NAG_LINES)))]
    assert len(lines) == len(set(lines))
    assert all("75 дней" in line for line in lines)
    assert all("Декстер" in line for line in lines)
