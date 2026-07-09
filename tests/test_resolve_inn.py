from app.services.rusprofile_client import extract_ogrn_from_search_html, normalize_inn


def test_normalize_inn():
    assert normalize_inn("9731112429") == "9731112429"
    assert normalize_inn("ИНН 9731112429") == "9731112429"
    assert normalize_inn("123") is None


def test_extract_ogrn_from_redirect_url():
    html = "<html></html>"
    assert extract_ogrn_from_search_html(html, "https://www.rusprofile.ru/id/1237700215290") == "1237700215290"


def test_extract_ogrn_from_search_links():
    html = '''
    <a href="/id/1237700215290">ООО Пионер</a>
    <a href="/id/9999999999999">другое</a>
    '''
    assert extract_ogrn_from_search_html(html, "https://www.rusprofile.ru/search?query=9731112429") == "1237700215290"


def test_extract_ogrn_from_text_label():
    html = "ОГРН 1227700761001 от 16 ноября 2022"
    assert extract_ogrn_from_search_html(html, "") == "1227700761001"
