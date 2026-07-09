from app.services.egrul import normalize_inn, parse_egrul_rows


def test_normalize_inn():
    assert normalize_inn("9731112429") == "9731112429"
    assert normalize_inn("ИНН 9731112429") == "9731112429"
    assert normalize_inn("123") is None


def test_parse_egrul_rows():
    payload = {
        "rows": [
            {
                "c": 'ООО "ПИОНЕР"',
                "i": "9731112429",
                "k": "ul",
                "n": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ПИОНЕР"',
                "o": "1237700215290",
                "t": "TOKEN123",
            }
        ]
    }
    result = parse_egrul_rows(payload, "9731112429")
    assert result.ogrn == "1237700215290"
    assert "ПИОНЕР" in (result.name or "")
    assert result.row_token == "TOKEN123"


def test_parse_egrul_by_ogrn():
    payload = {
        "rows": [
            {
                "c": 'ООО "ПРОРВА"',
                "i": "9726027672",
                "k": "ul",
                "o": "1227700761001",
                "t": "TOK",
            }
        ]
    }
    result = parse_egrul_rows(payload, "1227700761001")
    assert result.ogrn == "1227700761001"
    assert result.inn == "9726027672"
    assert "ПРОРВА" in (result.name or "")


def test_parse_egrul_empty():
    result = parse_egrul_rows({"rows": []}, "9731112429")
    assert result.ogrn is None
