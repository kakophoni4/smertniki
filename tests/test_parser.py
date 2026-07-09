"""Тесты парсера Rusprofile на сохранённых HTML или синтетических фрагментах."""

from app.parser.rusprofile import parse_company_html

ADDRESS_SAMPLE = """
<html><body>
<div class="company-header">
  <div class="company-header__title"><h1 itemprop="name">ООО "Пионер"</h1></div>
  <div class="company-header__status">Действующая организация</div>
</div>
<span id="clip_ogrn">1237700215290</span>
<span id="clip_inn">9731112429</span>
<p>121471, город Москва, Можайское ш, д. 29, кв. 54</p>
<p>Сведения об адресе недостоверны</p>
<section>Надёжность Компания имеет отметку ФНС о недостоверности адреса</section>
<section>Реестры ФНС Недостоверность адреса ДА Недостоверность руководителя нет</section>
</body></html>
"""

LIQUIDATION_SAMPLE = """
<html><body>
<div class="company-header">
  <h1 itemprop="name">ООО "Прорва"</h1>
</div>
<span id="clip_ogrn">1227700761001</span>
<span id="clip_inn">9726027672</span>
<div>Организация в процессе ликвидации</div>
<p>117639, город Москва, Балаклавский пр-кт, д. 2 к. 4</p>
<p>Сведения об адресе недостоверны</p>
<p>Сведения о должностном лице недостоверны</p>
<section>Ликвидация предстоящем исключении юридического лица из ЕГРЮЛ</section>
<section>Реестры ФНС Недостоверность адреса ДА Недостоверность руководителя ДА</section>
<!-- чужие названия на странице не должны перетирать h1 -->
<div class="company-name">АО "Эфти Косметикс"</div>
<div class="company-name">ООО "Кадзама"</div>
</body></html>
"""


def test_address_unreliable():
    snap = parse_company_html(ADDRESS_SAMPLE, "1237700215290")
    assert snap.short_name == 'ООО "Пионер"'
    assert snap.inn == "9731112429"
    assert snap.unreliable_address is True
    assert snap.unreliable_director is False
    assert snap.unreliable_founder is False


def test_liquidation_and_director():
    snap = parse_company_html(LIQUIDATION_SAMPLE, "1227700761001")
    assert snap.short_name == 'ООО "Прорва"'
    assert "Эфти" not in (snap.name or "")
    assert "Кадзама" not in (snap.name or "")
    assert snap.unreliable_address is True
    assert snap.unreliable_director is True
    assert snap.is_liquidating is True


def test_clean_company():
    html = '<html><body><h1>ООО "Чистая"</h1><span id="clip_ogrn">1000000000001</span><p>Действующая организация</p></body></html>'
    snap = parse_company_html(html, "1000000000001")
    assert snap.has_any_issue() is False
    assert snap.short_name == 'ООО "Чистая"'


def test_rejects_wrong_page():
    html = '<html><body><h1>Чужая</h1><span id="clip_ogrn">9999999999999</span></body></html>'
    try:
        parse_company_html(html, "1227700761001")
        assert False, "expected ValueError"
    except ValueError:
        pass
