"""Contratos das mensagens que trafegam no Kafka.

Producer e consumer validam com os mesmos modelos: uma mensagem que não segue o
contrato vai para o tópico de DLQ em vez de quebrar a ingestão.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

VERSAO_SCHEMA = 1


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def _lista(valor) -> list[str]:
    """Aceita lista ou string separada por ';' (formato dos CSVs do Colab)."""
    if valor is None:
        return []
    if isinstance(valor, float):  # NaN vindo do pandas
        return []
    if isinstance(valor, str):
        return [p.strip() for p in valor.split(";") if p.strip()]
    return [str(p).strip() for p in valor if str(p).strip()]


class ProdutoEvento(BaseModel):
    """Um perfume. `tipo='catalogo'` traz só nome/link; `tipo='detalhes'` traz a ficha completa."""

    model_config = ConfigDict(extra="ignore")

    versao_schema: int = VERSAO_SCHEMA
    tipo: Literal["catalogo", "detalhes"] = "catalogo"
    id_perfume: int
    designer: str
    nome: str
    link: Optional[str] = None
    genero: Optional[str] = None
    ano: Optional[int] = None
    nota_media: Optional[float] = Field(default=None, ge=0, le=5)
    votos: Optional[int] = Field(default=None, ge=0)
    reviews_site: Optional[int] = Field(default=None, ge=0)
    perfumistas: list[str] = Field(default_factory=list)
    acordes: list[str] = Field(default_factory=list)
    notas_topo: list[str] = Field(default_factory=list)
    notas_coracao: list[str] = Field(default_factory=list)
    notas_base: list[str] = Field(default_factory=list)
    fonte: Literal["csv", "scraper", "exemplo"] = "csv"
    coletado_em: datetime = Field(default_factory=agora_utc)

    @field_validator("perfumistas", "acordes", "notas_topo", "notas_coracao", "notas_base", mode="before")
    @classmethod
    def _normalizar_listas(cls, v):
        return _lista(v)

    @field_validator("link", "genero", mode="before")
    @classmethod
    def _texto_opcional(cls, v):
        if v is None or (isinstance(v, float) and v != v):
            return None
        v = str(v).strip()
        return v or None

    @field_validator("ano", "votos", "reviews_site", "nota_media", mode="before")
    @classmethod
    def _numero_opcional(cls, v):
        if v is None or v == "" or (isinstance(v, float) and v != v):
            return None
        return v


class ReviewEvento(BaseModel):
    """Uma review, como coletada (dado bruto). A limpeza acontece no consumer."""

    model_config = ConfigDict(extra="ignore")

    versao_schema: int = VERSAO_SCHEMA
    review_id: int
    id_perfume: int
    designer: Optional[str] = None   # usados para criar o produto se a review chegar antes dele
    perfume: Optional[str] = None
    autor: Optional[str] = None
    data_texto: Optional[str] = None
    tags: Optional[str] = None
    texto: str = Field(min_length=1)
    fonte: Literal["csv", "scraper", "exemplo"] = "csv"
    coletado_em: datetime = Field(default_factory=agora_utc)

    @field_validator("autor", "data_texto", "tags", "designer", "perfume", mode="before")
    @classmethod
    def _vazio_para_none(cls, v):
        if v is None or (isinstance(v, float) and v != v):  # NaN
            return None
        v = str(v).strip()
        return v or None


class PedidoColeta(BaseModel):
    """Pedido do app para coletar um perfume agora (tópico fragrantica.pedidos)."""

    model_config = ConfigDict(extra="ignore")

    versao_schema: int = VERSAO_SCHEMA
    id_pedido: str = Field(default_factory=lambda: uuid.uuid4().hex)
    id_perfume: int
    link: str
    designer: Optional[str] = None
    nome: Optional[str] = None
    origem: str = "app"
    solicitado_em: datetime = Field(default_factory=agora_utc)


class StatusPedido(BaseModel):
    """Andamento de um pedido, publicado pelo coletor (tópico fragrantica.pedidos.status)."""

    model_config = ConfigDict(extra="ignore")

    versao_schema: int = VERSAO_SCHEMA
    id_pedido: str
    id_perfume: int
    status: Literal["na_fila", "coletando", "concluido", "erro"]
    reviews_coletadas: Optional[int] = None
    max_review_id: Optional[int] = None    # maior id publicado: o app espera ele chegar ao banco
    mensagem: Optional[str] = None
    em: datetime = Field(default_factory=agora_utc)
