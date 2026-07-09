#!/usr/bin/env python3
"""Скачать HTML карточки Rusprofile для отладки парсера."""

import sys
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"


def fetch(ogrn: str, out_dir: Path) -> None:
    url = f"https://www.rusprofile.ru/id/{ogrn}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "replace")
    path = out_dir / f"sample_{ogrn}.html"
    path.write_text(html, encoding="utf-8")
    print(f"Saved {path} ({len(html)} bytes)")


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent
    ids = sys.argv[1:] or ["1237700215290", "1227700761001"]
    for ogrn in ids:
        fetch(ogrn, out)
