from app.services.egrul import normalize_inn, parse_egrul_rows
from app.services.rusprofile_client import parse_ajax_search


def test_normalize_inn():
    assert normalize_inn("9731112429") == "9731112429"
    assert normalize_inn("ИНН 9731112429") == "9731112429"
    assert normalize_inn("123") is None


def test_parse_ajax_pioneer():
    payload = {
        "ul_count": 1,
        "ul": [
            {
                "name": 'ООО "Пионер"',
                "link": "/id/1237700215290",
                "ogrn": "1237700215290",
                "inn": "!~~9731112429~~!",
            }
        ],
        "success": True,
    }
    result = parse_ajax_search(payload, "9731112429")
    assert result.ogrn == "1237700215290"
    assert result.name == 'ООО "Пионер"'


def test_parse_egrul_rows():
    payload = {
        "rows": [
            {
                "c": 'ООО "ПИОНЕР"',
                "i": "9731112429",
                "k": "ul",
                "n": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ПИОНЕР"',
                "o": "1237700215290",
            }
        ]
    }
    result = parse_egrul_rows(payload, "9731112429")
    assert result.ogrn == "1237700215290"
    assert "ПИОНЕР" in (result.name or "")


def test_parse_egrul_empty():
    result = parse_egrul_rows({"rows": []}, "9731112429")
    assert result.ogrn is None
