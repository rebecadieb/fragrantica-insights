"""Limpeza e enriquecimento das reviews (camada de tratamento do pipeline).

Funções puras: recebem o evento bruto e devolvem o registro pronto para o banco.
O texto original é sempre preservado ao lado do texto limpo.
"""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Optional

from .models import ReviewEvento

_TAGS_HTML = re.compile(r"<[^>]+>")
_ESPACOS = re.compile(r"[ \t ]+")
_LINHAS = re.compile(r"\n\s*\n+")
# restos de interface que às vezes vêm grudados no texto
_RUIDO_UI = re.compile(r"\b(mostrar mais|mostrar menos|leia mais|read more|compartilhar)\s*$", re.I)

_DATA_ABSOLUTA = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})(?:\s+(\d{1,2}):(\d{2}))?")
_DATA_RELATIVA = re.compile(r"\b(\d+|an?|um|uma)\s+([a-zêç]+)", re.I)
_UNIDADES = [
    (re.compile(r"^(minute|minuto)"), timedelta(minutes=1)),
    (re.compile(r"^(hour|hora)"), timedelta(hours=1)),
    (re.compile(r"^(day|dia)"), timedelta(days=1)),
    (re.compile(r"^(week|semana)"), timedelta(weeks=1)),
    (re.compile(r"^(month|m[eê]s)"), timedelta(days=30)),
    (re.compile(r"^(year|ano)"), timedelta(days=365)),
]


def limpar_texto(texto: str) -> str:
    """Normaliza encoding, remove HTML residual e espaços repetidos."""
    t = unicodedata.normalize("NFC", html.unescape(texto or ""))
    t = _TAGS_HTML.sub(" ", t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = "\n".join(_ESPACOS.sub(" ", linha).strip() for linha in t.split("\n"))
    t = _LINHAS.sub("\n", t).strip()
    t = _RUIDO_UI.sub("", t).strip()
    return t


def hash_conteudo(id_perfume: int, texto_limpo: str) -> str:
    """Chave de deduplicação por conteúdo (produto + texto normalizado)."""
    normalizado = re.sub(r"\s+", " ", texto_limpo.lower()).strip()
    base = f"{id_perfume}|{normalizado}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def pseudonimizar(autor: Optional[str]) -> Optional[str]:
    """Troca o nome do avaliador por um apelido estável (mesmo autor -> mesmo apelido)."""
    if not autor:
        return None
    return "avaliador_" + hashlib.sha256(autor.strip().lower().encode("utf-8")).hexdigest()[:10]


def interpretar_data(data_texto: Optional[str], referencia: datetime) -> Optional[datetime]:
    """'09/23/26 05:23' (MM/DD/AA, formato do site) ou relativa ('19 hours ago', 'há 3 dias')."""
    if not data_texto:
        return None
    m = _DATA_ABSOLUTA.search(data_texto)
    if m:
        mes, dia, ano = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ano = ano + 2000 if ano < 100 else ano
        hora, minuto = int(m.group(4) or 0), int(m.group(5) or 0)
        try:
            return datetime(ano, mes, dia, hora, minuto, tzinfo=referencia.tzinfo)
        except ValueError:
            return None
    m = _DATA_RELATIVA.search(data_texto)
    if m:
        qtd = int(m.group(1)) if m.group(1).isdigit() else 1
        unidade = m.group(2).lower()
        for padrao, delta in _UNIDADES:
            if padrao.match(unidade):
                return (referencia - qtd * delta).replace(second=0, microsecond=0)
    return None


def tratar_review(evento: ReviewEvento, anonimizar: bool = True, min_palavras: int = 5) -> dict:
    """Evento bruto -> registro da tabela `reviews`."""
    texto_limpo = limpar_texto(evento.texto)
    n_palavras = len(texto_limpo.split())
    return {
        "review_id": evento.review_id,
        "id_perfume": evento.id_perfume,
        "autor": pseudonimizar(evento.autor) if anonimizar else evento.autor,
        "data_texto": evento.data_texto,
        "data_review": interpretar_data(evento.data_texto, evento.coletado_em),
        "texto_original": evento.texto,
        "texto_limpo": texto_limpo,
        "n_palavras": n_palavras,
        "informativa": n_palavras >= min_palavras,
        "hash_conteudo": hash_conteudo(evento.id_perfume, texto_limpo),
        "fonte": evento.fonte,
        "coletado_em": evento.coletado_em,
    }
