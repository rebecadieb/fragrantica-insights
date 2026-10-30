"""Prompts. Modo detalhado: extração em lotes + síntese. Modo rápido: uma chamada só.
Mudou o texto? Suba VERSAO_PROMPT: o cache de análises é separado por versão (e por modo),
então as antigas não se misturam às novas."""
from __future__ import annotations

import json

from .frases import formatar
from .taxonomia import texto_taxonomia

VERSAO_PROMPT = "v5"  # v2: formato plano; v3: sem frases copiáveis; v4: evidência pelo número da frase;
                      # v5: índice suavizado e nota do modelo ancorada no índice
MODOS = ("detalhado", "rapido")


def versao(modo: str) -> str:
    """Versão gravada no cache: as análises dos dois modos ficam separadas."""
    return f"{VERSAO_PROMPT}-{modo}"


_DIMENSOES = texto_taxonomia(com_exemplos=False)

_PISTAS = """Pistas: comparação com outro perfume ou clone -> originalidade; preço e rendimento ->
custo_beneficio; horas, duração, longevidade -> fixacao; alcance, rastro, sillage -> projecao;
clima, horário, ocasiões -> versatilidade; reação de outras pessoas -> elogios; o cheiro em si
(bonito, enjoativo, sintético) -> cheiro."""

_FORMATO_REVIEWS = """As reviews vêm divididas em frases numeradas: [3.2] é a frase 2 da review 3."""

_EXEMPLO_LOTE_ENTRADA = """<review numero="1">
[1.1] Cheiro lindo de baunilha, mas some em 2 horas.
[1.2] Parecido demais com outro perfume da moda.
[1.3] Recebo elogios no trabalho.
</review>"""
_EXEMPLO_LOTE_SAIDA = {
    "mencoes": [
        {"r": 1, "f": 1, "d": "cheiro", "p": "+"},
        {"r": 1, "f": 1, "d": "fixacao", "p": "-"},
        {"r": 1, "f": 2, "d": "originalidade", "p": "-"},
        {"r": 1, "f": 3, "d": "elogios", "p": "+"},
    ],
    "perfis": [], "ocasioes": ["trabalho"],
}

SISTEMA_EXTRACAO = f"""Você é um analista de reviews de perfumes. Encontre, em cada review, as frases
que falam de alguma das dimensões abaixo.

Dimensões:
{_DIMENSOES}

{_FORMATO_REVIEWS}

Para cada menção, devolva um item em "mencoes" com:
- r: número da review; f: número da frase; d: a dimensão;
- p: "+" se elogia, "-" se critica, "0" se só descreve sem elogiar nem criticar.

Regras:
1. Liste SÓ o que a review realmente diz. Dimensão não mencionada não entra. Na dúvida, deixe de fora.
2. Uma frase pode gerar várias menções (dimensões diferentes).
3. Use apenas números de review e de frase que aparecem na entrada.
4. "perfis" e "ocasioes": expressões curtas, só quando as reviews disserem.
{_PISTAS}

Exemplo
Entrada:
{_EXEMPLO_LOTE_ENTRADA}
Resposta: {json.dumps(_EXEMPLO_LOTE_SAIDA, ensure_ascii=False)}"""

SISTEMA_SINTESE = f"""Você escreve o "raio-x de uso real" de um perfume para ajudar um consumidor a
decidir a compra. Você recebe contagens agregadas de menções por dimensão e trechos reais
de reviews. Baseie-se somente nesse material.

Dimensões:
{texto_taxonomia()}

Regras para a NOTA (0 a 10) de cada dimensão:
- Devolva as 7 dimensões. Toda dimensão com 1 menção ou mais TEM nota (número, nunca null).
- Reflita o equilíbrio entre menções positivas e negativas e a intensidade dos trechos.
  O "indice_calculado" é uma referência estatística; afaste-se dele só com motivo claro nos trechos.
- Dimensão com 0 menções: nota = null e resumo dizendo que as reviews não falam disso.
- Com poucas menções (1 a 4), seja conservador e diga no resumo que há poucos relatos.

Regras para o texto:
- Português do Brasil, direto, sem exageros de marketing.
- Resumo de cada dimensão: 1 frase curta, só sobre AQUELA dimensão (não misture fixação com
  projeção), citando o que os consumidores relatam.
- Escreva frases simples e confira a ortografia de cada palavra.
- pontos_fortes e pontos_fracos: 2 a 4 itens curtos cada (pode ficar vazio só se não houver).
- perfil_cliente.ocasioes, estacoes e periodo: preencha a partir de "ocasioes_citadas" e dos trechos.
- perfil_cliente.descricao: 2 a 3 frases dizendo para quem o perfume é ideal (gosto olfativo,
  estilo, ocasiões, intensidade desejada) e para quem não é.
- resumo_geral: 2 a 3 frases.
- Não invente notas olfativas, preços ou números que não estejam no material."""


def montar_extracao(produto: dict, preparadas: list) -> str:
    return (f"Perfume: {produto['nome']} ({produto['designer']})\n\n"
            f"<reviews>\n{formatar(preparadas)}\n</reviews>\n\nListe as menções.")


def montar_sintese(produto: dict, agregado: dict) -> str:
    ficha = {
        "nome": produto.get("nome"),
        "designer": produto.get("designer"),
        "genero_no_site": produto.get("genero"),
        "ano": produto.get("ano"),
        "acordes": produto.get("acordes"),
        "notas_topo": produto.get("notas_topo"),
        "notas_coracao": produto.get("notas_coracao"),
        "notas_base": produto.get("notas_base"),
    }
    material = {
        "ficha_tecnica": ficha,
        "reviews_analisadas": agregado["n_reviews"],
        "dimensoes": {
            chave: {
                "mencoes": d["n_mencoes"],
                "positivas": d["positivas"],
                "negativas": d["negativas"],
                "neutras": d["neutras"],
                "indice_calculado": d["indice"],
                "trechos": [e["trecho"] for e in d["evidencias"]][:6],
            }
            for chave, d in agregado["dimensoes"].items()
        },
        "perfis_citados": agregado["perfis"][:15],
        "ocasioes_citadas": agregado["ocasioes"][:15],
    }
    return ("Material agregado das reviews (JSON):\n<material>\n"
            f"{json.dumps(material, ensure_ascii=False, indent=1, default=str)}\n</material>\n\n"
            "Gere a síntese do produto.")


SISTEMA_RAPIDO = f"""Você analisa as reviews de um perfume para ajudar um consumidor a decidir a compra.
Leia TODAS as reviews e avalie cada dimensão.

Dimensões:
{_DIMENSOES}

{_FORMATO_REVIEWS}

Para CADA uma das 7 dimensões:
- positivas / negativas / neutras: os NÚMEROS das reviews que elogiam, criticam ou só descrevem
  aquele aspecto. Percorra as reviews uma por uma; review que não fala do aspecto não entra.
- evidencias: até 3 frases que sustentam a avaliação, pelo número ("3.2"). Use só números que
  aparecem na entrada.
- nota: 0 a 10, refletindo o equilíbrio entre elogios e críticas. Sem nenhuma review: null.
- resumo: 1 frase curta, só sobre aquela dimensão, dizendo o que os consumidores relatam.
{_PISTAS}

Depois:
- perfil_cliente: descricao em 2 a 3 frases (para quem é ideal e para quem não é); indicado_para,
  nao_indicado_para, ocasioes, estacoes e periodo só com o que as reviews sustentam.
- pontos_fortes e pontos_fracos: 2 a 4 itens curtos cada.
- resumo_geral: 2 a 3 frases.

Português do Brasil, frases simples, confira a ortografia. Não invente notas olfativas, preços
ou números que não estejam nas reviews."""


def montar_rapido(produto: dict, preparadas: list) -> str:
    ficha = {k: produto.get(k) for k in ("nome", "designer", "genero", "ano", "acordes")}
    return (f"Ficha do perfume: {json.dumps(ficha, ensure_ascii=False, default=str)}\n\n"
            f"<reviews>\n{formatar(preparadas)}\n</reviews>\n\nAvalie as 7 dimensões.")
