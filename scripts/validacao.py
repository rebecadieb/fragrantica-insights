"""Validação da extração do LLM contra rótulos humanos (Fase 4 do documento técnico).

1) Gerar a planilha de rotulagem (o LLM classifica uma amostra de reviews):
     python scripts/validacao.py amostra --id-perfume 31861 --n 100 --saida validacao.csv

   --modo rapido|detalhado escolhe o modo da análise (padrão: detalhado). Para comparar os
   dois, gere a planilha num modo, rotule, e depois rode `reclassificar` no outro modo: as
   colunas humano_* são mantidas e as llm_* refeitas.
     python scripts/validacao.py reclassificar validacao.csv --modo rapido --saida validacao_rapido.csv

   Abra o CSV, preencha as colunas `humano_<dimensao>` com positiva / negativa /
   neutra (ou deixe vazio quando a review não fala daquela dimensão) SEM olhar as
   colunas `llm_<dimensao>`.

2) Calcular as métricas:
     python scripts/validacao.py avaliar validacao.csv

   Para cada dimensão: precisão e recall da DETECÇÃO (a review fala ou não da
   dimensão), acurácia da POLARIDADE quando ambos detectaram, e o kappa de Cohen
   (concordância além do acaso, 4 classes: vazio/positiva/negativa/neutra).
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fraginsights import db  # noqa: E402
from fraginsights.config import carregar  # noqa: E402
from fraginsights.llm.analise import (AnaliseRapida, TOKENS_RAPIDO, extrair, ler_rapido,  # noqa: E402
                                      orcamento_rapido)
from fraginsights.llm.clientes import criar_cliente, modelos_do_modo  # noqa: E402
from fraginsights.llm.frases import preparar  # noqa: E402
from fraginsights.llm.prompts import SISTEMA_RAPIDO, montar_rapido  # noqa: E402
from fraginsights.llm.taxonomia import CHAVES  # noqa: E402


def classificar(produto: dict, reviews: list[dict], modo: str) -> dict[int, dict[str, str]]:
    """review_id -> {dimensão: polaridade} segundo o LLM, no modo pedido."""
    cfg = carregar()
    cliente = criar_cliente(cfg.llm_provedor, cfg)
    previsto: dict[int, dict[str, str]] = {r["review_id"]: {} for r in reviews}
    if modo == "detalhado":
        mencoes = extrair(produto, preparar(reviews, filtrar=cfg.filtrar_frases), cliente, cfg).mencoes
    else:  # várias chamadas, cada uma com o que cabe no contexto
        mencoes, resto = [], list(reviews)
        while resto:
            parte = preparar(resto, filtrar=cfg.filtrar_frases, orcamento_caracteres=orcamento_rapido(cfg, cliente.provedor))
            saida = cliente.gerar(modelo=modelos_do_modo(cfg, cliente.provedor, "rapido")[1], sistema=SISTEMA_RAPIDO,
                                  usuario=montar_rapido(produto, parte), schema=AnaliseRapida, max_tokens=TOKENS_RAPIDO)
            mencoes += ler_rapido(saida, parte)[0].mencoes
            resto = resto[len(parte):]
    for m in mencoes:
        previsto[m.review_id].setdefault(m.dimensao, m.polaridade)
    return previsto


def _dados(id_perfume: int):
    with db.conexao() as conn:
        produto = db.consultar_um(conn, db.SQL_PRODUTO, {"id_perfume": id_perfume})
        reviews = db.consultar(conn, db.SQL_REVIEWS_PRODUTO,
                               {"id_perfume": id_perfume, "somente_informativas": True, "limite": 5000})
    return produto, reviews


def _escrever(saida: Path, linhas: list[dict], previsto: dict[int, dict[str, str]]) -> None:
    with saida.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["review_id", "texto"] + [f"humano_{c}" for c in CHAVES] + [f"llm_{c}" for c in CHAVES])
        for r in linhas:
            p = previsto.get(int(r["review_id"]), {})
            w.writerow([r["review_id"], r["texto"]] + [r.get(f"humano_{c}", "") for c in CHAVES]
                       + [p.get(c, "") for c in CHAVES])
    print(f"Planilha com {len(linhas)} reviews salva em {saida}")


def gerar_amostra(id_perfume: int, n: int, saida: Path, modo: str, semente: int = 42) -> None:
    produto, reviews = _dados(id_perfume)
    random.Random(semente).shuffle(reviews)
    amostra = reviews[:n]
    previsto = classificar(produto, amostra, modo)
    _escrever(saida, [{"review_id": r["review_id"], "texto": r["texto_limpo"]} for r in amostra], previsto)


def reclassificar(arquivo: Path, id_perfume: int, modo: str, saida: Path) -> None:
    with arquivo.open(encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))
    produto, _ = _dados(id_perfume)
    reviews = [{"review_id": int(l["review_id"]), "texto_limpo": l["texto"]} for l in linhas]
    _escrever(saida, linhas, classificar(produto, reviews, modo))


def kappa(pares: list[tuple[str, str]]) -> float | None:
    if not pares:
        return None
    n = len(pares)
    observada = sum(a == b for a, b in pares) / n
    ca, cb = Counter(a for a, _ in pares), Counter(b for _, b in pares)
    esperada = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return 1.0 if esperada == 1 else round((observada - esperada) / (1 - esperada), 3)


def avaliar(arquivo: Path) -> None:
    with arquivo.open(encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))
    print(f"{len(linhas)} reviews rotuladas\n")
    print(f"{'dimensão':<16}{'precisão':>10}{'recall':>9}{'polaridade':>12}{'kappa':>8}{'n_humano':>10}")
    for c in CHAVES:
        pares = [((l[f"humano_{c}"] or "").strip().lower(), (l[f"llm_{c}"] or "").strip().lower()) for l in linhas]
        vp = sum(1 for h, m in pares if h and m)
        fp = sum(1 for h, m in pares if not h and m)
        fn = sum(1 for h, m in pares if h and not m)
        ambos = [(h, m) for h, m in pares if h and m]
        prec = vp / (vp + fp) if vp + fp else None
        rec = vp / (vp + fn) if vp + fn else None
        pol = sum(h == m for h, m in ambos) / len(ambos) if ambos else None
        fmt = lambda x: "—" if x is None else f"{x:.2f}"  # noqa: E731
        print(f"{c:<16}{fmt(prec):>10}{fmt(rec):>9}{fmt(pol):>12}{fmt(kappa(pares)):>8}{vp + fn:>10}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("amostra")
    a.add_argument("--id-perfume", type=int, required=True)
    a.add_argument("--n", type=int, default=100)
    a.add_argument("--saida", type=Path, default=Path("validacao.csv"))
    a.add_argument("--modo", choices=["detalhado", "rapido"], default="detalhado")
    c = sub.add_parser("reclassificar")
    c.add_argument("arquivo", type=Path)
    c.add_argument("--id-perfume", type=int, required=True)
    c.add_argument("--modo", choices=["detalhado", "rapido"], required=True)
    c.add_argument("--saida", type=Path, required=True)
    b = sub.add_parser("avaliar")
    b.add_argument("arquivo", type=Path)
    args = parser.parse_args()
    if args.cmd == "amostra":
        gerar_amostra(args.id_perfume, args.n, args.saida, args.modo)
    elif args.cmd == "reclassificar":
        reclassificar(args.arquivo, args.id_perfume, args.modo, args.saida)
    else:
        avaliar(args.arquivo)


if __name__ == "__main__":
    main()
