from datetime import datetime, timedelta, timezone

from app.services.monitor import days_word, ticket_age_days


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
