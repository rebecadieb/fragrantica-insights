"""Carga em lote: coleta e analisa os perfumes de uma lista (CSV com links do Fragrantica).

Pensado para popular o catálogo de uma vez com os perfumes mais populares: o CSV vem
ordenado pelo ranking das marcas, e a seleção pega N perfumes de cada marca, na ordem do
arquivo, até completar o total.

    python -m fraginsights.lote ver       populares.csv            # mostra a seleção, sem fazer nada
    python -m fraginsights.lote coletar   populares.csv            # coleta (precisa do Chrome: rode no coletor)
    python -m fraginsights.lote analisar  populares.csv --sim      # gera as análises (custa créditos na API)
    python -m fraginsights.lote remover-exemplo                    # apaga os perfumes fictícios do banco

Opções: --total 80 --por-marca 2. Os comandos podem ser repetidos: a coleta pula quem foi
coletado há pouco e a análise pula quem já tem análise em cache (não paga de novo).
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import Config, carregar
from .links import ler_link

log = logging.getLogger("fraginsights.lote")

# Custo estimado por análise na API do Claude (Haiku na extração + Sonnet na síntese), por
# 100 reviews lidas. É uma referência para o aviso antes de gastar; o valor real fica no
# painel da Anthropic.
CUSTO_POR_100_REVIEWS = (0.05, 0.10)


# ---------------------------------------------------------------------------
# Leitura e seleção (funções puras, testadas)
# ---------------------------------------------------------------------------

def ler_csv(caminho: Path) -> list[dict]:
    """Perfumes do CSV, na ordem do arquivo. A coluna de links é descoberta sozinha (a
    primeira com links de perfume do Fragrantica); linhas sem link válido são ignoradas."""
    with caminho.open(encoding="utf-8-sig", newline="") as f:
        amostra = f.read(4096)
        f.seek(0)
        try:
            dialeto = csv.Sniffer().sniff(amostra, delimiters=",;\t")
        except csv.Error:
            dialeto = csv.excel
        linhas = list(csv.reader(f, dialeto))
    perfumes, vistos = [], set()
    for linha in linhas:
        for celula in linha:
            p = ler_link(celula) if "fragrantica" in celula.lower() else None
            if p:
                if p["id_perfume"] not in vistos:
                    vistos.add(p["id_perfume"])
                    perfumes.append(p)
                break
    return perfumes


def selecionar(perfumes: list[dict], total: int = 80, por_marca: int = 2) -> list[dict]:
    """Na ordem do CSV, até `por_marca` perfumes de cada marca, até completar `total`."""
    escolhidos, por_designer = [], {}
    for p in perfumes:
        chave = p["designer"].casefold()
        if por_designer.get(chave, 0) >= por_marca:
            continue
        por_designer[chave] = por_designer.get(chave, 0) + 1
        escolhidos.append(p)
        if len(escolhidos) >= total:
            break
    return escolhidos


def estimativa_custo(n_analises: int, reviews_por_analise: int) -> tuple[float, float]:
    fator = reviews_por_analise / 100
    return (round(n_analises * CUSTO_POR_100_REVIEWS[0] * fator, 2),
            round(n_analises * CUSTO_POR_100_REVIEWS[1] * fator, 2))


def _selecao(args) -> list[dict]:
    perfumes = ler_csv(Path(args.csv))
    if not perfumes:
        sys.exit(f"Nenhum link de perfume do Fragrantica encontrado em {args.csv}.")
    escolhidos = selecionar(perfumes, args.total, args.por_marca)
    marcas = len({p["designer"].casefold() for p in escolhidos})
    log.info("%d links no CSV -> %d perfumes selecionados de %d marcas (%d por marca)",
             len(perfumes), len(escolhidos), marcas, args.por_marca)
    return escolhidos


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

def ver(args, cfg: Config) -> None:
    for i, p in enumerate(_selecao(args), start=1):
        print(f"{i:>3}. {p['designer']:<25} {p['nome']:<40} {p['link']}")


def coletar(args, cfg: Config) -> None:  # pragma: no cover - depende do Chrome e do Kafka
    """Ficha + reviews de cada perfume selecionado, publicadas no Kafka (o consumer grava).
    Limita às LLM_MAX_REVIEWS mais recentes, que é o que a análise lê."""
    from . import db
    from .bus import KafkaPublicador
    from .producer import _navegador, publicar_coleta

    selecao = _selecao(args)
    pub = KafkaPublicador(cfg.kafka_bootstrap)
    limite_recente = datetime.now(timezone.utc) - timedelta(days=cfg.validade_coleta_dias)
    resumo = {"coletados": 0, "pulados": 0, "falhas": 0, "reviews": 0}
    with _navegador(cfg) as nav:
        for i, p in enumerate(selecao, start=1):
            with db.conexao(cfg.database_url) as conn:
                no_banco = db.consultar_um(conn, db.SQL_PRODUTO, {"id_perfume": p["id_perfume"]}) or {}
            if not args.forcar and no_banco.get("ultima_coleta") and no_banco["ultima_coleta"] > limite_recente:
                resumo["pulados"] += 1
                log.info("[%d/%d] %s: coletado há menos de %d dias, pulando", i, len(selecao), p["nome"],
                         cfg.validade_coleta_dias)
                continue
            produto = {"id_perfume": p["id_perfume"], "link": no_banco.get("link") or p["link"],
                       "designer": no_banco.get("designer") or p["designer"],
                       "nome": no_banco.get("nome") or p["nome"], "max_review_id": no_banco.get("max_review_id")}
            try:
                evento, reviews = nav.coletar(produto, max_reviews=cfg.llm_max_reviews)
                publicar_coleta(pub, cfg, evento, reviews)
            except Exception as erro:
                resumo["falhas"] += 1
                log.error("[%d/%d] %s: %s", i, len(selecao), p["nome"], erro)
                continue
            resumo["coletados"] += 1
            resumo["reviews"] += len(reviews)
            log.info("[%d/%d] %s (%s): %d reviews novas", i, len(selecao), produto["nome"], produto["designer"],
                     len(reviews))
            time.sleep(1.5)
    log.info("Coleta do lote concluída: %s", resumo)
    if resumo["falhas"]:
        log.info("Rode o mesmo comando de novo para tentar só os que falharam (os coletados são pulados).")


def analisar(args, cfg: Config) -> None:  # pragma: no cover - depende do banco e do LLM
    from . import db
    from .llm.analise import chave_cache, obter_ou_gerar
    from .llm.clientes import criar_cliente, limites

    selecao = _selecao(args)
    provedor = cfg.llm_provedor
    max_reviews = limites(cfg, provedor)[0]
    pendentes, sem_reviews, prontos = [], [], 0
    with db.conexao(cfg.database_url) as conn:
        for p in selecao:
            produto = db.consultar_um(conn, db.SQL_PRODUTO, {"id_perfume": p["id_perfume"]})
            if not produto or produto["n_informativas"] < cfg.min_reviews_analise:
                sem_reviews.append(p)
                continue
            regs = db.consultar(conn, db.SQL_REVIEWS_PRODUTO,
                                {"id_perfume": p["id_perfume"], "somente_informativas": False, "limite": 5000})
            if db.consultar_um(conn, db.SQL_ANALISE_POR_ASSINATURA,
                               chave_cache(p["id_perfume"], regs, cfg, provedor, "detalhado")):
                prontos += 1
            else:
                pendentes.append(produto)

    log.info("%d já analisados (sem custo), %d a analisar, %d sem reviews suficientes no banco",
             prontos, len(pendentes), len(sem_reviews))
    for p in sem_reviews[:10]:
        log.info("  sem reviews: %s (%s). Rode a coleta do lote antes.", p["nome"], p["designer"])
    if not pendentes:
        return
    if provedor == "anthropic":
        minimo, maximo = estimativa_custo(len(pendentes), max_reviews)
        log.info("Estimativa: US$ %.2f a %.2f na API (%d análises, até %d reviews cada)",
                 minimo, maximo, len(pendentes), max_reviews)
        if not args.sim:
            log.info("Nada foi gerado. Para gerar, rode de novo com --sim.")
            return

    cliente = criar_cliente(provedor, cfg)
    feitos = falhas = 0
    for i, produto in enumerate(pendentes, start=1):
        inicio = time.time()
        try:
            with db.usando_llm("app", produto["id_perfume"], cfg.database_url), \
                    db.conexao(cfg.database_url) as conn:
                obter_ou_gerar(conn, produto["id_perfume"], cliente, cfg, modo="detalhado")
        except Exception as erro:
            falhas += 1
            log.error("[%d/%d] %s: %s", i, len(pendentes), produto["nome"], erro)
            continue
        feitos += 1
        log.info("[%d/%d] %s: análise pronta em %.0f s", i, len(pendentes), produto["nome"], time.time() - inicio)
    log.info("Análises do lote: %d geradas, %d falharam", feitos, falhas)


def remover_exemplo(args, cfg: Config) -> None:  # pragma: no cover - depende do banco
    from . import db

    with db.conexao(cfg.database_url) as conn:
        apagados = db.remover_exemplo(conn)
    log.info("Dados fictícios removidos: %s", apagados)


def main(argv: Optional[list[str]] = None) -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="comando", required=True)
    for nome, ajuda in (("ver", "mostra a seleção"), ("coletar", "coleta ficha e reviews"),
                        ("analisar", "gera as análises")):
        p = sub.add_parser(nome, help=ajuda)
        p.add_argument("csv", help="CSV com os links dos perfumes, na ordem de prioridade")
        p.add_argument("--total", type=int, default=80)
        p.add_argument("--por-marca", type=int, default=2)
        if nome == "coletar":
            p.add_argument("--forcar", action="store_true", help="coleta mesmo os coletados há pouco")
        if nome == "analisar":
            p.add_argument("--sim", action="store_true", help="confirma o gasto na API paga")
    sub.add_parser("remover-exemplo", help="apaga os perfumes fictícios (dados de demonstração)")
    args = parser.parse_args(argv)
    cfg = carregar()
    {"ver": ver, "coletar": coletar, "analisar": analisar, "remover-exemplo": remover_exemplo}[args.comando](args, cfg)


if __name__ == "__main__":
    main()
