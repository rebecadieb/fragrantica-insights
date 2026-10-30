"""Orquestra a análise de um produto, em um de dois modos:

- detalhado: extração em lotes (cada menção aponta a frase que a sustenta) -> agregação
  feita pelo código -> síntese. Mais fiel; é o que o analista em lote usa.
- rapido: uma chamada só, que lê todas as reviews e devolve, por dimensão, quais reviews
  elogiam ou criticam, as frases de evidência, a nota e o texto. É o que o app usa.

Nos dois, as evidências são frases reais recortadas pelo código (o modelo só aponta o
número), e contagens, índice e confiança são calculados pelo código.
"""
from __future__ import annotations

import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from ..config import Config
from .clientes import ClienteLLM, limites, modelos_do_modo
from .frases import ReviewPreparada, ler_id_frase, preparar
from .prompts import (MODOS, SISTEMA_EXTRACAO, SISTEMA_RAPIDO, SISTEMA_SINTESE, montar_extracao, montar_rapido,
                      montar_sintese, versao)
from .schemas import POLARIDADE, AnaliseRapida, ExtracaoLote, SinteseProduto
from .taxonomia import CHAVES, POR_CHAVE, confianca

log = logging.getLogger(__name__)

MAX_CARACTERES_REVIEW = 1500
TOKENS_SINTESE = 1500
TOKENS_RAPIDO = 2500
RESERVA_CONTEXTO = 3500          # tokens do prompt de sistema + resposta no modo rápido
CARACTERES_POR_TOKEN = 3


class SemMencoes(ValueError):
    """O modelo não apontou nenhuma menção válida: a análise não é salva."""


def tokens_extracao(n_reviews: int) -> int:
    """Teto de saída do lote. Cada menção ocupa ~20 tokens; um teto justo evita que um
    modelo local que "empaque" fique minutos rodando até estourar o tempo."""
    return 120 * n_reviews + 300


# ---------------------------------------------------------------------------
# Seleção
# ---------------------------------------------------------------------------

def selecionar_reviews(reviews: list[dict], maximo: int) -> list[dict]:
    """Informativas primeiro, mais recentes primeiro (maior review_id), texto truncado."""
    candidatas = sorted(reviews, key=lambda r: (not r.get("informativa", True), -r["review_id"]))
    selecionadas = []
    for r in candidatas[:maximo]:
        r = dict(r)
        r["texto_limpo"] = r["texto_limpo"][:MAX_CARACTERES_REVIEW]
        selecionadas.append(r)
    return selecionadas


def orcamento_rapido(cfg: Config, provedor: str) -> Optional[int]:
    """Caracteres de review que cabem numa chamada só (só limita o modelo local)."""
    if provedor != "ollama":
        return None
    return max(4000, (cfg.ollama_num_ctx - RESERVA_CONTEXTO) * CARACTERES_POR_TOKEN)


def preparar_para(reviews: list[dict], cfg: Config, provedor: str, modo: str,
                  max_reviews: Optional[int] = None) -> list[ReviewPreparada]:
    """Reviews que entram na análise, já divididas em frases. Função pura: o app usa a mesma
    seleção para achar a análise em cache sem chamar o modelo."""
    selecionadas = selecionar_reviews(reviews, max_reviews or limites(cfg, provedor)[0])
    orcamento = orcamento_rapido(cfg, provedor) if modo == "rapido" else None
    return preparar(selecionadas, filtrar=cfg.filtrar_frases, orcamento_caracteres=orcamento)


def assinatura(review_ids: list[int]) -> str:
    base = ",".join(str(i) for i in sorted(review_ids))
    return hashlib.sha256(base.encode()).hexdigest()[:16]


def max_reviews_analista(cfg: Config, provedor: str) -> int:
    """O analista lê ANALISTA_MAX_REVIEWS, mas nunca menos que o app (com a API, o app já lê 200)."""
    return max(cfg.analista_max_reviews, limites(cfg, provedor)[0])


def chave_cache(id_perfume: int, reviews: list[dict], cfg: Config, provedor: str, modo: str,
                max_reviews: Optional[int] = None) -> dict:
    preparadas = preparar_para(reviews, cfg, provedor, modo, max_reviews)
    return {"id_perfume": id_perfume, "provedor": provedor, "modelo_sintese": modelos_do_modo(cfg, provedor, modo)[1],
            "versao_prompt": versao(modo), "assinatura": assinatura([p.review_id for p in preparadas])}


# ---------------------------------------------------------------------------
# Agregação (estatística, feita pelo código)
# ---------------------------------------------------------------------------

@dataclass
class Mencao:
    review_id: int
    dimensao: str
    polaridade: str                  # positiva | negativa | neutra
    frase: Optional[str] = None      # frase real da review, quando o modelo apontou uma


PESO_NEUTRO = 2        # "relatos imaginários" neutros somados a toda dimensão (suavização)
MIN_MENCOES_NOTA_MODELO = 5   # abaixo disso (confiança baixa) a nota é o índice, não a do modelo
DESVIO_MAX_MODELO = 2.0       # a nota do modelo fica a no máximo 2 pontos do índice


def indice(positivas: int, negativas: int, neutras: int) -> Optional[float]:
    """0 a 10: positivas valem 1, neutras 0,5, negativas 0, com suavização: soma PESO_NEUTRO
    relatos neutros, o que puxa para o meio as dimensões com poucos relatos (3 críticas dão
    2,0 e não 0) e quase não mexe nas que têm muitos (40 elogios dão 9,8)."""
    total = positivas + negativas + neutras
    if not total:
        return None
    return round(10 * (positivas + 0.5 * neutras + 0.5 * PESO_NEUTRO) / (total + PESO_NEUTRO), 1)


def nota_final(nota_modelo: Optional[float], n_mencoes: int, indice_calc: Optional[float]) -> tuple:
    """(nota, origem). O índice é a âncora: com poucos relatos vale ele; com muitos, vale a
    nota do modelo, limitada a DESVIO_MAX_MODELO pontos do índice (modelos pequenos exageram)."""
    if not n_mencoes or indice_calc is None:
        return None, "sem_dados"
    if nota_modelo is None:
        return indice_calc, "indice"
    if n_mencoes < MIN_MENCOES_NOTA_MODELO:
        return indice_calc, "poucos_relatos"
    limitada = min(max(nota_modelo, indice_calc - DESVIO_MAX_MODELO), indice_calc + DESVIO_MAX_MODELO)
    return round(limitada, 1), ("modelo" if limitada == nota_modelo else "modelo_ajustada")


def _intercalar(evidencias: list[dict]) -> list[dict]:
    """Alterna as polaridades para o leitor ver os dois lados (críticas primeiro, se houver)."""
    grupos: dict[str, list] = {}
    for e in sorted(evidencias, key=lambda e: {"negativa": 0, "positiva": 1, "neutra": 2}[e["polaridade"]]):
        grupos.setdefault(e["polaridade"], []).append(e)
    saida = []
    while any(grupos.values()):
        for g in list(grupos):
            if grupos[g]:
                saida.append(grupos[g].pop(0))
    return saida


def agregar(mencoes: list[Mencao], n_reviews: int, perfis: list[str] = (), ocasioes: list[str] = (),
            descartadas: Optional[dict[str, int]] = None) -> dict:
    """Conta cada review uma vez por dimensão e polaridade; evidências sem repetição."""
    dims = {c: {"positivas": set(), "negativas": set(), "neutras": set(), "evidencias": [], "frases": set()}
            for c in CHAVES}
    for m in mencoes:
        d = dims[m.dimensao]
        d[{"positiva": "positivas", "negativa": "negativas", "neutra": "neutras"}[m.polaridade]].add(m.review_id)
        if m.frase and m.frase not in d["frases"]:
            d["frases"].add(m.frase)
            d["evidencias"].append({"review_id": m.review_id, "trecho": m.frase, "polaridade": m.polaridade,
                                    "verificada": True})
    saida = {}
    for chave, d in dims.items():
        pos, neg, neu = len(d["positivas"]), len(d["negativas"]), len(d["neutras"])
        n = len(d["positivas"] | d["negativas"] | d["neutras"])
        saida[chave] = {
            "n_mencoes": n, "positivas": pos, "negativas": neg, "neutras": neu,
            "indice": indice(pos, neg, neu), "confianca": confianca(n),
            "evidencias": _intercalar(d["evidencias"])[:8],
            "descartadas": (descartadas or {}).get(chave, 0),
        }

    def ranking(itens):
        contagem: dict[str, int] = {}
        for i in itens:
            i = i.strip().lower()
            if i:
                contagem[i] = contagem.get(i, 0) + 1
        return [k for k, _ in sorted(contagem.items(), key=lambda kv: -kv[1])]

    return {"n_reviews": n_reviews, "dimensoes": saida, "perfis": ranking(perfis), "ocasioes": ranking(ocasioes)}


def completar_sintese(sintese: dict, agregado: dict) -> dict:
    """Preenche pelo código o que o modelo deixou vazio (modelos pequenos às vezes devolvem
    listas vazias). Tudo vem das contagens e das citações das próprias reviews."""
    dims = agregado["dimensoes"]
    perfil = sintese.setdefault("perfil_cliente", {})
    if not perfil.get("ocasioes"):
        perfil["ocasioes"] = agregado["ocasioes"][:5]
    if not perfil.get("indicado_para"):
        perfil["indicado_para"] = agregado["perfis"][:5]
    com_dados = [(c, d) for c, d in dims.items() if d["indice"] is not None and d["n_mencoes"] >= 3]
    if not sintese.get("pontos_fortes"):
        sintese["pontos_fortes"] = [f"{POR_CHAVE[c].nome} ({d['positivas']} de {d['n_mencoes']} reviews elogiam)"
                                    for c, d in sorted(com_dados, key=lambda x: -x[1]["indice"])
                                    if d["indice"] >= 7][:3]
    if not sintese.get("pontos_fracos"):
        sintese["pontos_fracos"] = [f"{POR_CHAVE[c].nome} ({d['negativas']} de {d['n_mencoes']} reviews criticam)"
                                    for c, d in sorted(com_dados, key=lambda x: x[1]["indice"])
                                    if d["indice"] <= 4][:3]
    return sintese


# ---------------------------------------------------------------------------
# Modo detalhado: extração em lotes
# ---------------------------------------------------------------------------

def _lotes(itens: list, tamanho: int) -> list[list]:
    return [itens[i:i + tamanho] for i in range(0, len(itens), tamanho)]


@dataclass
class Extracao:
    mencoes: list[Mencao]
    perfis: list[str]
    ocasioes: list[str]
    descartadas: dict[str, int]
    apontadas: int                   # menções devolvidas pelo modelo
    validas: int                     # ... que apontam uma frase existente


def ler_lote(saida: ExtracaoLote, lote: list[ReviewPreparada]) -> Extracao:
    """Converte a saída do modelo em menções. Número de review ou de frase que não existe
    na entrada = menção inventada: descartada."""
    por_numero = {p.numero: p for p in lote}
    mencoes, descartadas, vistas = [], {c: 0 for c in CHAVES}, set()
    for m in saida.mencoes:
        prep = por_numero.get(m.r)
        frase = prep.frase(m.f) if prep else None
        if frase is None:
            descartadas[m.d] += 1
            continue
        chave = (prep.review_id, m.d, m.p, m.f)
        if chave not in vistas:
            vistas.add(chave)
            mencoes.append(Mencao(prep.review_id, m.d, POLARIDADE[m.p], frase))
    return Extracao(mencoes, list(saida.perfis), list(saida.ocasioes), descartadas,
                    apontadas=len(saida.mencoes), validas=len(saida.mencoes) - sum(descartadas.values()))


def extrair(produto: dict, preparadas: list[ReviewPreparada], cliente: ClienteLLM, cfg: Config) -> Extracao:
    _, por_lote, paralelo = limites(cfg, cliente.provedor)
    modelo = modelos_do_modo(cfg, cliente.provedor, "detalhado")[0]
    com_texto = [p for p in preparadas if p.enviadas]

    def um_lote(lote: list[ReviewPreparada]) -> Extracao:
        saida = cliente.gerar(modelo=modelo, sistema=SISTEMA_EXTRACAO, usuario=montar_extracao(produto, lote),
                              schema=ExtracaoLote, max_tokens=tokens_extracao(len(lote)))
        return ler_lote(saida, lote)

    with ThreadPoolExecutor(max_workers=max(1, paralelo)) as pool:
        partes = list(pool.map(um_lote, _lotes(com_texto, por_lote)))
    total = Extracao([], [], [], {c: 0 for c in CHAVES}, 0, 0)
    for p in partes:
        total.mencoes += p.mencoes
        total.perfis += p.perfis
        total.ocasioes += p.ocasioes
        total.apontadas += p.apontadas
        total.validas += p.validas
        for c in CHAVES:
            total.descartadas[c] += p.descartadas[c]
    return total


# ---------------------------------------------------------------------------
# Modo rápido: uma chamada
# ---------------------------------------------------------------------------

def ler_rapido(saida: AnaliseRapida, preparadas: list[ReviewPreparada]) -> tuple[Extracao, dict]:
    """Menções a partir das listas de reviews por dimensão + frases de evidência."""
    por_numero = {p.numero: p for p in preparadas if p.enviadas}
    mencoes, descartadas = [], {c: 0 for c in CHAVES}
    apontadas = validas = 0
    for d in saida.dimensoes:
        polaridade_da_review: dict[int, str] = {}
        for pol, numeros in (("negativa", d.negativas), ("positiva", d.positivas), ("neutra", d.neutras)):
            for n in dict.fromkeys(numeros):
                apontadas += 1
                if n not in por_numero:
                    descartadas[d.dimensao] += 1
                    continue
                validas += 1
                polaridade_da_review.setdefault(n, pol)
                mencoes.append(Mencao(por_numero[n].review_id, d.dimensao, pol))
        for ident in d.evidencias[:3]:
            lido = ler_id_frase(ident)
            prep = por_numero.get(lido[0]) if lido else None
            frase = prep.frase(lido[1]) if prep else None
            if frase and lido[0] in polaridade_da_review:  # evidência de review fora das listas não vale
                mencoes.append(Mencao(prep.review_id, d.dimensao, polaridade_da_review[lido[0]], frase))
    por_dim = {d.dimensao: d for d in saida.dimensoes}
    return Extracao(mencoes, [], [], descartadas, apontadas, validas), por_dim


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def analisar(produto: dict, reviews: list[dict], cliente: ClienteLLM, cfg: Config, modo: str = "detalhado",
             max_reviews: Optional[int] = None) -> dict:
    """Roda a análise e devolve o registro pronto para a tabela `analises`."""
    if modo not in MODOS:
        raise ValueError(f"Modo de análise desconhecido: {modo!r} (use {' ou '.join(MODOS)})")
    inicio = time.time()
    preparadas = preparar_para(reviews, cfg, cliente.provedor, modo, max_reviews)
    if len(preparadas) < cfg.min_reviews_analise:
        raise ValueError(f"São necessárias ao menos {cfg.min_reviews_analise} reviews (há {len(preparadas)}).")
    modelo_extracao, modelo_sintese = modelos_do_modo(cfg, cliente.provedor, modo)
    log.info("Análise %s de %s: %s reviews, %s frases enviadas ao modelo", modo, produto.get("nome"),
             len(preparadas), sum(len(p.enviadas) for p in preparadas))

    if modo == "rapido":
        saida: AnaliseRapida = cliente.gerar(modelo=modelo_sintese, sistema=SISTEMA_RAPIDO,
                                             usuario=montar_rapido(produto, preparadas), schema=AnaliseRapida,
                                             max_tokens=TOKENS_RAPIDO)
        extracao, por_dim = ler_rapido(saida, preparadas)
        texto = saida
    else:
        extracao = extrair(produto, preparadas, cliente, cfg)
        por_dim = None

    agregado = agregar(extracao.mencoes, len(preparadas), extracao.perfis, extracao.ocasioes, extracao.descartadas)
    if not any(d["n_mencoes"] for d in agregado["dimensoes"].values()):
        # nada extraído: um texto em cima de nada seria inventado. Não salva.
        log.warning("Modo %s sem menções válidas: %s números apontados, %s válidos. Resposta do modelo: %s",
                    modo, extracao.apontadas, extracao.validas,
                    texto.model_dump_json()[:3000] if modo == "rapido" else "(lotes)")
        raise SemMencoes(
            f"O modelo ({modelo_extracao}) não identificou nenhuma menção às dimensões em "
            f"{len(preparadas)} reviews. Modelos pequenos às vezes falham nessa tarefa: tente gerar de novo, "
            "use o modo detalhado ou um modelo maior.")

    if modo == "detalhado":
        texto = cliente.gerar(modelo=modelo_sintese, sistema=SISTEMA_SINTESE,
                              usuario=montar_sintese(produto, agregado), schema=SinteseProduto,
                              max_tokens=TOKENS_SINTESE)
        por_dim = {a.dimensao: a for a in texto.dimensoes}

    dimensoes = []
    for chave in CHAVES:
        est = agregado["dimensoes"][chave]
        av = por_dim.get(chave)
        nota, origem_nota = nota_final(av.nota if av else None, est["n_mencoes"], est["indice"])
        resumo = av.resumo if av and est["n_mencoes"] else (
            "As reviews analisadas não comentam este aspecto." if not est["n_mencoes"] else "")
        dimensoes.append({
            "dimensao": chave, "nota": nota, "origem_nota": origem_nota, "resumo": resumo,
            "descartadas": est["descartadas"],
            "n_mencoes": est["n_mencoes"], "positivas": est["positivas"], "negativas": est["negativas"],
            "neutras": est["neutras"], "indice_calculado": est["indice"], "confianca": est["confianca"],
            "evidencias": est["evidencias"],
        })

    completo = completar_sintese(texto.model_dump(), agregado)
    resultado = {
        "modo": modo,
        "resumo_geral": texto.resumo_geral,
        "perfil_cliente": completo["perfil_cliente"],
        "pontos_fortes": completo["pontos_fortes"],
        "pontos_fracos": completo["pontos_fracos"],
        "dimensoes": dimensoes,
        "perfis_citados": agregado["perfis"][:15],
        "ocasioes_citadas": agregado["ocasioes"][:15],
        "evidencias_total": extracao.apontadas,
        "evidencias_verificadas": extracao.validas,
    }
    ids = [p.review_id for p in preparadas]
    return {
        "id_perfume": produto["id_perfume"],
        "provedor": cliente.provedor,
        "modelo_extracao": modelo_extracao,
        "modelo_sintese": modelo_sintese,
        "versao_prompt": versao(modo),
        "assinatura": assinatura(ids),
        "n_reviews_usadas": len(ids),
        "ultimo_review_id": max(ids),
        "resultado": resultado,
        "taxa_evidencias_ok": round(extracao.validas / extracao.apontadas, 3) if extracao.apontadas else None,
        "duracao_s": round(time.time() - inicio, 2),
    }


def obter_ou_gerar(conn, id_perfume: int, cliente: ClienteLLM, cfg: Config, forcar: bool = False,
                   modo: str = "detalhado", max_reviews: Optional[int] = None) -> dict:
    """Devolve a análise em cache para o conjunto atual de reviews, ou gera uma nova."""
    from .. import db

    produto = db.consultar_um(conn, db.SQL_PRODUTO, {"id_perfume": id_perfume})
    if produto is None:
        raise ValueError(f"Produto {id_perfume} não encontrado")
    reviews = db.consultar(conn, db.SQL_REVIEWS_PRODUTO,
                           {"id_perfume": id_perfume, "somente_informativas": False, "limite": 5000})
    chave = chave_cache(id_perfume, reviews, cfg, cliente.provedor, modo, max_reviews)
    if not forcar:
        existente = db.consultar_um(conn, db.SQL_ANALISE_POR_ASSINATURA, chave)
        if existente:
            return existente
    registro = analisar(produto, reviews, cliente, cfg, modo, max_reviews)
    db.salvar_analise(conn, registro)
    return db.consultar_um(conn, db.SQL_ANALISE_POR_ASSINATURA, chave)
