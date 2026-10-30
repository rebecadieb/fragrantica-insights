"""Leitura de links de perfume do Fragrantica (qualquer idioma do site)."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

BASE = "https://www.fragrantica.com.br"
_CAMINHO = re.compile(r"^/perfume/([^/]+)/([^/]+)-(\d+)\.html$")


def ler_link(url: str) -> Optional[dict]:
    """'https://www.fragrantica.com/perfume/Dior/Sauvage-31861.html' ->
    {'id_perfume': 31861, 'designer': 'Dior', 'nome': 'Sauvage', 'link': BASE + caminho}.
    Devolve None se não for um link de perfume do Fragrantica."""
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    partes = urlparse(url)
    if "fragrantica." not in (partes.hostname or ""):
        return None
    m = _CAMINHO.match(partes.path)
    if not m:
        return None
    designer, nome, id_perfume = m.groups()
    return {
        "id_perfume": int(id_perfume),
        "designer": designer.replace("-", " "),
        "nome": nome.replace("-", " "),
        "link": f"{BASE}{partes.path}",   # a versão em português do site
    }
