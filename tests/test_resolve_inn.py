from app.services.rusprofile_client import normalize_inn, parse_ajax_search


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
    assert result.error is None


def test_parse_ajax_empty():
    result = parse_ajax_search({"ul": [], "success": True}, "9731112429")
    assert result.ogrn is None
    assert "не найдена" in (result.error or "").lower()


def test_parse_ajax_picks_matching_inn():
    payload = {
        "ul": [
            {"name": "Чужая", "ogrn": "1111111111111", "inn": "7700000000"},
            {"name": "Нужная", "ogrn": "1237700215290", "inn": "9731112429"},
        ]
    }
    result = parse_ajax_search(payload, "9731112429")
    assert result.ogrn == "1237700215290"
    assert result.name == "Нужная"
