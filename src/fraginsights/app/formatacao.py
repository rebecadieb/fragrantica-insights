"""Funções de apresentação sem dependência do Streamlit (testáveis)."""
from __future__ import annotations

from typing import Optional

from ..llm.taxonomia import POR_CHAVE

CORES_CONFIANCA = {"alta": "#2e7d32", "média": "#f9a825", "baixa": "#ef6c00", "sem dados": "#9e9e9e"}


def rotulo_produto(p: dict) -> str:
    qtd = f"{p['n_reviews']} reviews" if p["n_reviews"] else "sem reviews ainda"
    return f"{p['nome']} — {p['designer']} ({qtd})"


def nota_formatada(nota: Optional[float]) -> str:
    return "—" if nota is None else f"{float(nota):.1f}"


def linhas_grafico(dimensoes: list[dict]) -> list[dict]:
    """Linhas para o gráfico de barras das notas (só dimensões com nota)."""
    return [
        {"Dimensão": POR_CHAVE[d["dimensao"]].nome, "Nota": float(d["nota"]),
         "Confiança": d["confianca"], "Menções": d["n_mencoes"]}
        for d in dimensoes if d.get("nota") is not None
    ]


def nome_dimensao(chave: str) -> str:
    return POR_CHAVE[chave].nome


def descricao_mencoes(d: dict) -> str:
    descartadas = d.get("descartadas") or 0
    extra = (f" · {descartadas} menções descartadas (apontavam review ou frase inexistente)" if descartadas else "")
    if not d["n_mencoes"]:
        return "Nenhuma review comenta este aspecto." + extra
    return (f"{d['n_mencoes']} reviews comentam · {d['positivas']} positivas · "
            f"{d['negativas']} negativas · {d['neutras']} neutras" + extra)


def lista_texto(itens: list[str]) -> str:
    return ", ".join(itens) if itens else "—"
