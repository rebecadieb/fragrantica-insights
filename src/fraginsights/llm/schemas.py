"""Contratos de saída do LLM (validados com Pydantic).

Estágio 1 (extração, modo detalhado): em cada lote, quais reviews mencionam quais
dimensões, com que polaridade e em qual FRASE (pelo número).
Modo rápido: uma chamada só que já devolve, por dimensão, as reviews e a nota.
Estágio 2 (síntese): a partir das contagens agregadas e das evidências, a nota de
cada dimensão, o resumo e o perfil de cliente ideal.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .taxonomia import CHAVES

ChaveDimensao = Literal[tuple(CHAVES)]  # type: ignore[valid-type]
Polaridade = Literal["positiva", "negativa", "neutra"]


# --- Estágio 1 (modo detalhado): extração em lotes ---------------------------
# Saída compacta: só as menções que existem, cada uma apontando a FRASE que a sustenta
# ([3.2] -> r=3, f=2). Chaves curtas porque o tempo de um modelo local é proporcional ao
# número de tokens que ele escreve.

Sinal = Literal["+", "-", "0"]   # positiva, negativa, neutra
POLARIDADE = {"+": "positiva", "-": "negativa", "0": "neutra"}


class MencaoLote(BaseModel):
    r: int = Field(description="Número da review")
    f: int = Field(description="Número da frase dentro da review ([3.2] -> r=3, f=2)")
    d: ChaveDimensao = Field(description="Dimensão")
    p: Sinal = Field(description="+ elogia, - critica, 0 neutro")


class ExtracaoLote(BaseModel):
    mencoes: list[MencaoLote] = Field(default_factory=list)
    perfis: list[str] = Field(default_factory=list, description="Para quem as reviews dizem que serve (curto)")
    ocasioes: list[str] = Field(default_factory=list, description="Ocasiões, clima ou horário citados (curto)")


# --- Modo rápido: uma chamada só ----------------------------------------------
# O modelo lê todas as reviews e devolve, por dimensão, QUAIS reviews elogiam, criticam ou
# são neutras (o código conta e calcula índice e confiança) e as frases de evidência.
# As listas vêm antes da nota e do resumo para o modelo "olhar" as reviews antes de opinar.

class DimensaoRapida(BaseModel):
    dimensao: ChaveDimensao
    positivas: list[int] = Field(default_factory=list, description="Números das reviews que elogiam")
    negativas: list[int] = Field(default_factory=list, description="Números das reviews que criticam")
    neutras: list[int] = Field(default_factory=list, description="Números das reviews neutras")
    evidencias: list[str] = Field(default_factory=list, description="Até 3 frases, pelo número: \"3.2\"")
    nota: Optional[float] = Field(default=None, ge=0, le=10)
    resumo: str = Field(description="1 frase")


# --- Estágio 2 --------------------------------------------------------------

class AvaliacaoDimensao(BaseModel):
    dimensao: ChaveDimensao
    nota: Optional[float] = Field(default=None, ge=0, le=10,
                                  description="0 a 10; null se não houver menções suficientes")
    resumo: str = Field(description="1 frase explicando a nota com base nas reviews")


class PerfilCliente(BaseModel):
    descricao: str = Field(description="Parágrafo descrevendo o cliente ideal para o perfume")
    indicado_para: list[str] = Field(default_factory=list)
    nao_indicado_para: list[str] = Field(default_factory=list)
    ocasioes: list[str] = Field(default_factory=list)
    estacoes: list[str] = Field(default_factory=list)
    periodo: Optional[Literal["dia", "noite", "dia e noite"]] = None


class SinteseProduto(BaseModel):
    dimensoes: list[AvaliacaoDimensao]
    perfil_cliente: PerfilCliente
    pontos_fortes: list[str] = Field(default_factory=list)
    pontos_fracos: list[str] = Field(default_factory=list)
    resumo_geral: str = Field(description="2 a 3 frases")


class AnaliseRapida(BaseModel):
    dimensoes: list[DimensaoRapida]
    perfil_cliente: PerfilCliente
    pontos_fortes: list[str] = Field(default_factory=list)
    pontos_fracos: list[str] = Field(default_factory=list)
    resumo_geral: str = Field(description="2 a 3 frases")
