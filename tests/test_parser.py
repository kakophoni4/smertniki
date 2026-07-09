"""Тесты парсера Rusprofile на сохранённых HTML или синтетических фрагментах."""

from app.parser.rusprofile import parse_company_html

ADDRESS_SAMPLE = """
<html><body>
<h1>ООО "Пионер"</h1>
<div class="company-header__status">Действующая организация</div>
<p>ИНН/КПП 9731112429 773101001</p>
<p>121471, город Москва, Можайское ш, д. 29, кв. 54</p>
<p>Сведения об адресе недостоверны</p>
<section>Надёжность Компания имеет отметку ФНС о недостоверности адреса</section>
<section>Реестры ФНС Недостоверность адреса ДА Недостоверность руководителя нет</section>
</body></html>
"""

LIQUIDATION_SAMPLE = """
<html><body>
<h1>ООО "Прорва"</h1>
<div>Организация в процессе ликвидации</div>
<p>ИНН/КПП 9726027672 772601001</p>
<p>117639, город Москва, Балаклавский пр-кт, д. 2 к. 4</p>
<p>Сведения об адресе недостоверны</p>
<p>Сведения о должностном лице недостоверны</p>
<section>Ликвидация предстоящем исключении юридического лица из ЕГРЮЛ</section>
<section>Реестры ФНС Недостоверность адреса ДА Недостоверность руководителя ДА</section>
</body></html>
"""


def test_address_unreliable():
    snap = parse_company_html(ADDRESS_SAMPLE, "1237700215290")
    assert snap.unreliable_address is True
    assert snap.unreliable_director is False
    assert snap.unreliable_founder is False


def test_liquidation_and_director():
    snap = parse_company_html(LIQUIDATION_SAMPLE, "1227700761001")
    assert snap.unreliable_address is True
    assert snap.unreliable_director is True
    assert snap.is_liquidating is True


def test_clean_company():
    html = '<html><body><h1>ООО "Чистая"</h1><p>Действующая организация</p></body></html>'
    snap = parse_company_html(html, "1000000000001")
    assert snap.has_any_issue() is False
