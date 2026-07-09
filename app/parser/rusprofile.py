import html as html_lib
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup


@dataclass
class CompanySnapshot:
    ogrn: str
    inn: str | None = None
    name: str | None = None
    short_name: str | None = None
    address: str | None = None
    status_text: str | None = None
    unreliable_address: bool = False
    unreliable_director: bool = False
    unreliable_founder: bool = False
    is_liquidating: bool = False
    is_liquidated: bool = False
    signals: list[str] = field(default_factory=list)
    raw_summary: str | None = None

    def has_any_issue(self) -> bool:
        return any(
            [
                self.unreliable_address,
                self.unreliable_director,
                self.unreliable_founder,
                self.is_liquidating,
                self.is_liquidated,
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ogrn": self.ogrn,
            "inn": self.inn,
            "name": self.name,
            "short_name": self.short_name,
            "address": self.address,
            "status_text": self.status_text,
            "unreliable_address": self.unreliable_address,
            "unreliable_director": self.unreliable_director,
            "unreliable_founder": self.unreliable_founder,
            "is_liquidating": self.is_liquidating,
            "is_liquidated": self.is_liquidated,
            "signals": self.signals,
        }


def _norm(text: str | None) -> str:
    if not text:
        return ""
    text = html_lib.unescape(text)
    return " ".join(text.split()).strip()


def _contains_any(haystack: str, needles: list[str]) -> bool:
    low = haystack.lower()
    return any(n.lower() in low for n in needles)


def _extract_name(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    """Имя только из шапки карточки — не из «похожих» / топа отрасли."""
    header = soup.select_one(".company-header") or soup.select_one("#main.company-main") or soup

    short = None
    h1 = header.select_one("h1[itemprop='name']") or header.select_one(".company-header__title h1") or header.find("h1")
    if h1:
        short = _norm(h1.get_text(" ", strip=True))

    full = None
    for sel in (
        ".company-header__full-name",
        ".company-header__full",
        "[itemprop='legalName']",
    ):
        el = header.select_one(sel)
        if el:
            candidate = _norm(el.get_text(" ", strip=True))
            if candidate:
                full = candidate
                break

    # title как запасной вариант: 'ООО "Прорва" Москва (ИНН ...)'
    if not short:
        title = soup.find("title")
        if title:
            t = _norm(title.get_text())
            m = re.match(r'^(.+?)\s+Москва\b', t) or re.match(r'^(.+?)\s+\(ИНН', t)
            if m:
                short = _norm(m.group(1))

    return short or full, short or full


def _extract_inn(soup: BeautifulSoup, page_text: str) -> str | None:
    # точный clip_inn с карточки
    clip = soup.select_one("#clip_inn")
    if clip:
        digits = re.sub(r"\D", "", clip.get_text())
        if len(digits) in (10, 12):
            return digits

    m = re.search(r"ИНН[/КПП\s]*([0-9]{10,12})", page_text)
    if m:
        return m.group(1)[:12]
    return None


def parse_company_html(html: str, ogrn: str) -> CompanySnapshot:
    """Парсит карточку компании Rusprofile.

    Сигналы недостоверности ищем в трёх местах (как на скрине):
    1) блок «Надёжность»
    2) подписи у юр.адреса / руководителя («Сведения ... недостоверны»)
    3) «Реестры ФНС» — строки Недостоверность адреса/руководителя/учредителя = ДА
    """
    soup = BeautifulSoup(html, "lxml")
    snap = CompanySnapshot(ogrn=ogrn)
    page_text = _norm(soup.get_text(" ", strip=True))

    # страница должна быть именно этой карточкой
    if ogrn not in html and ogrn not in page_text:
        raise ValueError(f"В HTML нет ОГРН {ogrn} — похоже, не та страница / блок")

    snap.name, snap.short_name = _extract_name(soup)
    snap.inn = _extract_inn(soup, page_text)

    # --- адрес ---
    addr_el = soup.select_one("[itemprop='address'], .company-info__address, .tile-item__addr")
    if addr_el:
        snap.address = _norm(addr_el.get_text())
    else:
        import re

        m = re.search(r"(\d{6},\s*город[^.]+)", page_text)
        if m:
            snap.address = _norm(m.group(1))

    # --- статус ---
    status_candidates = []
    for sel in [".company-header__status", ".status", ".company-status", "[class*='status']"]:
        for el in soup.select(sel):
            t = _norm(el.get_text())
            if t and len(t) < 120:
                status_candidates.append(t)
    if status_candidates:
        snap.status_text = status_candidates[0]
    elif _contains_any(page_text, ["в процессе ликвидации", "организация в процессе ликвидации"]):
        snap.status_text = "В процессе ликвидации"
    elif _contains_any(page_text, ["действующая организация", "действующее"]):
        snap.status_text = "Действующая организация"
    elif _contains_any(page_text, ["ликвидирован", "прекратил деятельность"]):
        snap.status_text = "Ликвидирована"

    # --- ликвидация ---
    if _contains_any(
        page_text,
        [
            "в процессе ликвидации",
            "предстоящем исключении",
            "исключения юридического лица из егрюл",
            "скоро будет исключена",
        ],
    ):
        snap.is_liquidating = True
        snap.signals.append("ликвидация/исключение")
    if _contains_any(page_text, ["ликвидирована", "прекратило деятельность", "исключено из егрюл"]):
        # не путать с «в процессе»
        if not snap.is_liquidating or "ликвидирован" in page_text.lower():
            if "в процессе ликвидации" not in page_text.lower():
                snap.is_liquidated = True
                snap.signals.append("ликвидирована")

    # --- явные текстовые маркеры недостоверности ---
    if _contains_any(
        page_text,
        [
            "сведения об адресе недостоверны",
            "отметку фнс о недостоверности адреса",
            "недостоверности адреса",
        ],
    ):
        snap.unreliable_address = True
        snap.signals.append("текст: недостоверность адреса")

    if _contains_any(
        page_text,
        [
            "сведения о должностном лице недостоверны",
            "недостоверности руководителя",
            "недостоверность руководителя",
        ],
    ):
        snap.unreliable_director = True
        snap.signals.append("текст: недостоверность ДЛ/руководителя")

    if _contains_any(
        page_text,
        [
            "недостоверности учредителя",
            "недостоверность учредителя",
            "сведения об учредителе недостоверны",
        ],
    ):
        snap.unreliable_founder = True
        snap.signals.append("текст: недостоверность учредителя")

    # --- блок «Реестры ФНС»: ищем пары label -> да/нет ---
    _parse_fns_registers(soup, snap, page_text)

    # --- блок надёжности ---
    for el in soup.find_all(string=lambda t: isinstance(t, str) and "надёжность" in t.lower()):
        parent = el.find_parent(["div", "section", "aside", "article"])
        if parent:
            chunk = _norm(parent.get_text(" ", strip=True)).lower()
            if "недостоверн" in chunk and "адрес" in chunk:
                snap.unreliable_address = True
                snap.signals.append("надёжность: адрес")
            if "недостоверн" in chunk and ("руковод" in chunk or "должност" in chunk):
                snap.unreliable_director = True
                snap.signals.append("надёжность: руководитель")
            if "недостоверн" in chunk and "учредител" in chunk:
                snap.unreliable_founder = True
                snap.signals.append("надёжность: учредитель")

    snap.raw_summary = "; ".join(snap.signals) if snap.signals else "ok"
    return snap


def _parse_fns_registers(soup: BeautifulSoup, snap: CompanySnapshot, page_text: str) -> None:
    """Парсит секцию Реестры ФНС: 'Недостоверность X' + 'ДА'/'нет'."""
    import re

    # Ищем в HTML списки/таблицы рядом с заголовком
    headers = soup.find_all(
        string=lambda t: isinstance(t, str) and "реестры фнс" in t.lower()
    )
    chunks: list[str] = []
    for h in headers:
        parent = h.find_parent(["div", "section", "aside", "article", "ul", "table"])
        if parent:
            # берём родителя повыше, чтобы захватить список
            grand = parent.parent if parent.parent else parent
            chunks.append(_norm(grand.get_text(" ", strip=True)))

    if not chunks:
        # fallback: вырезаем кусок из page_text
        m = re.search(r"Реестры ФНС(.{0,800})", page_text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            chunks.append(m.group(0))

    mapping = [
        ("недостоверность адреса", "unreliable_address", "реестр ФНС: адрес=ДА"),
        ("недостоверность руководителя", "unreliable_director", "реестр ФНС: руководитель=ДА"),
        ("недостоверность учредителя", "unreliable_founder", "реестр ФНС: учредитель=ДА"),
    ]

    for chunk in chunks:
        low = chunk.lower()
        for label, attr, signal in mapping:
            # ищем label и ближайшее да/нет после него
            idx = low.find(label)
            if idx < 0:
                continue
            window = low[idx : idx + len(label) + 40]
            # «ДА» как положительный маркер; «нет» — отрицательный
            if re.search(r"\bда\b", window) and not re.search(r"\bнет\b", window[: window.find("да") + 2] if "да" in window else window):
                # если в окне есть и да и нет — смотрим что ближе к label
                da = window.find("да")
                net = window.find("нет")
                if da >= 0 and (net < 0 or da < net):
                    setattr(snap, attr, True)
                    if signal not in snap.signals:
                        snap.signals.append(signal)
            elif re.search(r"\bда\b", window):
                da = window.find("да")
                net = window.find("нет")
                if da >= 0 and (net < 0 or da < net):
                    setattr(snap, attr, True)
                    if signal not in snap.signals:
                        snap.signals.append(signal)

    # Доп. эвристика по всей странице: "Недостоверность адреса ДА"
    for label, attr, signal in mapping:
        if re.search(rf"{label}\s*да\b", page_text, flags=re.IGNORECASE):
            setattr(snap, attr, True)
            if signal not in snap.signals:
                snap.signals.append(signal)
