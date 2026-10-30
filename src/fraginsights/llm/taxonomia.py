"""Dimensões (aspectos) analisadas em cada perfume.

Conjunto FECHADO: o LLM só pode classificar nestas chaves, o que torna a saída
comparável entre produtos e validável com enums. Para mudar a taxonomia, edite
esta lista e suba VERSAO_PROMPT em prompts.py (análises antigas deixam de valer).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Dimensao:
    chave: str
    nome: str
    definicao: str
    exemplos: str


DIMENSOES: list[Dimensao] = [
    Dimensao("fixacao", "Fixação / durabilidade",
             "Quanto tempo o perfume permanece perceptível na pele ou na roupa.",
             "'dura o dia todo', 'some em 2 horas', 'fixação de 8h'"),
    Dimensao("projecao", "Projeção e rastro",
             "O quanto o cheiro se espalha e é percebido por outras pessoas (sillage).",
             "'projeta muito', 'fica colado na pele', 'deixa rastro no elevador'"),
    Dimensao("cheiro", "Qualidade do cheiro",
             "O quanto o cheiro em si agrada: beleza, equilíbrio, sensação de qualidade.",
             "'cheiro maravilhoso', 'enjoativo', 'muito sintético'"),
    Dimensao("versatilidade", "Versatilidade",
             "Em quantas situações funciona: clima, horário, ocasiões, ambiente de trabalho.",
             "'serve para qualquer ocasião', 'só para noites frias'"),
    Dimensao("originalidade", "Originalidade",
             "O quanto é único versus genérico ou parecido com outros perfumes.",
             "'nunca senti nada igual', 'clone do Sauvage', 'mais um doce genérico'"),
    Dimensao("custo_beneficio", "Custo-benefício",
             "Relação entre preço, rendimento e o que o perfume entrega.",
             "'vale cada centavo', 'caro pelo que entrega', 'rende muito'"),
    Dimensao("elogios", "Elogios recebidos",
             "Reação de outras pessoas a quem usa: elogios, comentários, indiferença.",
             "'sempre me elogiam', 'ninguém nunca comentou'"),
]

CHAVES = [d.chave for d in DIMENSOES]
POR_CHAVE = {d.chave: d for d in DIMENSOES}

# Confiança calculada pelo código (não pedida ao modelo): depende de quantas
# reviews sustentam a dimensão. LLMs são mal calibrados quando se autoavaliam.
LIMIARES_CONFIANCA = [(15, "alta"), (5, "média"), (1, "baixa")]


def confianca(n_mencoes: int) -> str:
    for minimo, rotulo in LIMIARES_CONFIANCA:
        if n_mencoes >= minimo:
            return rotulo
    return "sem dados"


def texto_taxonomia(com_exemplos: bool = True) -> str:
    """Sem exemplos na extração: modelos pequenos copiavam as frases de exemplo ("dura o dia
    todo") como se fossem trechos das reviews."""
    return "\n".join(f"- {d.chave} ({d.nome}): {d.definicao}" + (f" Ex.: {d.exemplos}" if com_exemplos else "")
                     for d in DIMENSOES)
