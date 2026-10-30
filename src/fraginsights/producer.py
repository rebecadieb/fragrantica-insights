"""Producer: publica produtos e reviews no Kafka.

Modos:
  catalogo    descobre os perfumes no site: marcas mais populares -> perfumes de
              cada marca (nome + link). É o ponto de partida sem CSV nenhum.
  coletar     uma rodada de coleta incremental: ficha + reviews novas de cada perfume
  agendado    serviço do coletor: atende na hora os pedidos de coleta feitos pelo app
              (tópico fragrantica.pedidos) e, todo dia no HORARIO_COLETA, monta/atualiza o
              catálogo e executa `coletar`
  carga-csv   opcional: aproveita os CSVs já gerados no Colab (perfumes.csv,
              detalhes_perfumes.csv, reviews.csv)

Exemplos:
  python -m fraginsights.producer catalogo --marcas 5
  python -m fraginsights.producer coletar --limite 20
  python -m fraginsights.producer agendado
  python -m fraginsights.producer carga-csv --pasta data/input
"""
from __future__ import annotations

import argparse
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import ValidationError

from .bus import KafkaPublicador, Publicador, garantir_topicos
from .config import Config, carregar, topicos
from .models import PedidoColeta, ProdutoEvento, ReviewEvento, StatusPedido

log = logging.getLogger("producer")


# ---------------------------------------------------------------------------
# Carga inicial (CSV do Colab)
# ---------------------------------------------------------------------------

def _data_do_arquivo(caminho: Path) -> datetime:
    """Datas relativas ('a day ago') são relativas ao momento da coleta: usamos o mtime do CSV."""
    return datetime.fromtimestamp(caminho.stat().st_mtime, tz=timezone.utc)


def eventos_produtos_csv(pasta: Path, fonte: str = "csv") -> Iterator[ProdutoEvento]:
    catalogo = pasta / "perfumes.csv"
    if catalogo.exists():
        quando = _data_do_arquivo(catalogo)
        for linha in pd.read_csv(catalogo).drop_duplicates("id_perfume").to_dict("records"):
            yield ProdutoEvento(tipo="catalogo", id_perfume=linha["id_perfume"], designer=linha["designer"],
                                nome=linha["perfume"], link=linha.get("link_perfume"), fonte=fonte,
                                coletado_em=quando)
    detalhes = pasta / "detalhes_perfumes.csv"
    if detalhes.exists():
        quando = _data_do_arquivo(detalhes)
        for linha in pd.read_csv(detalhes).drop_duplicates("id_perfume", keep="last").to_dict("records"):
            yield ProdutoEvento(
                tipo="detalhes", id_perfume=linha["id_perfume"], designer=linha["designer"],
                nome=linha["perfume"], link=linha.get("link_perfume"), genero=linha.get("genero"),
                ano=linha.get("ano"), nota_media=linha.get("nota_media"), votos=linha.get("votos"),
                reviews_site=linha.get("reviews"), perfumistas=linha.get("perfumistas"),
                acordes=linha.get("acordes"), notas_topo=linha.get("notas_topo"),
                notas_coracao=linha.get("notas_coracao"),
                notas_base=linha.get("notas_base") if pd.notna(linha.get("notas_base")) else linha.get("notas_gerais"),
                fonte=fonte, coletado_em=quando,
            )


def eventos_reviews_csv(pasta: Path, fonte: str = "csv") -> Iterator[ReviewEvento]:
    arquivo = pasta / "reviews.csv"
    if not arquivo.exists():
        return
    quando = _data_do_arquivo(arquivo)
    df = pd.read_csv(arquivo).drop_duplicates(["id_perfume", "review_id"])
    for linha in df.to_dict("records"):
        yield ReviewEvento(
            review_id=linha["review_id"], id_perfume=linha["id_perfume"], designer=linha.get("designer"),
            perfume=linha.get("perfume"), autor=linha.get("autor"), data_texto=linha.get("data_texto"),
            tags=linha.get("tags"), texto=linha.get("texto") if pd.notna(linha.get("texto")) else "",
            fonte=fonte, coletado_em=quando,
        )


def _publicar_seguro(pub: Publicador, topico: str, gerador, contagem: dict) -> None:
    """Publica o que é válido; linhas inválidas são contadas e registradas, não derrubam a carga."""
    while True:
        try:
            evento = next(gerador)
        except StopIteration:
            return
        except (ValidationError, ValueError, KeyError) as erro:
            contagem["invalidas"] += 1
            log.warning("Linha ignorada (%s): %s", topico, str(erro).splitlines()[0])
            continue
        pub.publicar(topico, str(evento.id_perfume), evento.model_dump(mode="json"))
        contagem[topico] = contagem.get(topico, 0) + 1


def carga_csv(pub: Publicador, cfg: Config, pasta: Path, fonte: str = "csv") -> dict:
    if not pasta.exists():
        raise FileNotFoundError(f"Pasta não encontrada: {pasta}")
    contagem = {"invalidas": 0}
    _publicar_seguro(pub, cfg.topico_produtos, eventos_produtos_csv(pasta, fonte), contagem)
    _publicar_seguro(pub, cfg.topico_reviews, eventos_reviews_csv(pasta, fonte), contagem)
    pub.finalizar()
    log.info("Carga CSV publicada: %s", contagem)
    return contagem


# ---------------------------------------------------------------------------
# Catálogo (scraper): marcas populares -> perfumes
# ---------------------------------------------------------------------------

def _binario_chrome() -> Optional[str]:
    return "/usr/bin/google-chrome" if Path("/usr/bin/google-chrome").exists() else None


@contextmanager
def _navegador(cfg: Config, coletor=None):
    """Reaproveita um navegador já aberto (serviço) ou abre um para a tarefa avulsa."""
    if coletor is not None:
        yield coletor
        return
    from .scraper.fragrantica import ColetorFragrantica

    with ColetorFragrantica(headless=cfg.headless, max_cliques=cfg.max_cliques_reviews,
                            binario_chrome=_binario_chrome()) as novo:
        yield novo


def _nada() -> None:
    pass


def catalogo(pub: Publicador, cfg: Config, max_marcas: Optional[int] = None,
             coletor=None, entre: Callable[[], None] = _nada) -> dict:
    """Publica um ProdutoEvento(tipo='catalogo') para cada perfume das marcas populares.
    `entre` roda entre uma marca e outra (o serviço usa para atender pedidos do app)."""
    resumo = {"marcas": 0, "perfumes": 0, "falhas": 0}
    with _navegador(cfg, coletor) as nav:
        marcas = nav.listar_marcas_populares(max_marcas or cfg.max_marcas)
        log.info("Catálogo: %d marcas populares", len(marcas))
        for marca in marcas:
            entre()
            try:
                perfumes = nav.listar_perfumes_da_marca(marca)
            except Exception as erro:
                resumo["falhas"] += 1
                log.error("Marca %s: %s", marca["designer"], erro)
                continue
            agora = datetime.now(timezone.utc)
            for p in perfumes:
                evento = ProdutoEvento(tipo="catalogo", id_perfume=p["id_perfume"], designer=marca["designer"],
                                       nome=p["nome"] or f"Perfume {p['id_perfume']}", link=p["link"],
                                       fonte="scraper", coletado_em=agora)
                pub.publicar(cfg.topico_produtos, str(evento.id_perfume), evento.model_dump(mode="json"))
            pub.finalizar()
            resumo["marcas"] += 1
            resumo["perfumes"] += len(perfumes)
            log.info("[%d] %s: %d perfumes", marca["ranking"], marca["designer"], len(perfumes))
            time.sleep(2)
    log.info("Catálogo publicado: %s", resumo)
    return resumo


def _banco_sem_produtos(cfg: Config) -> bool:
    from . import db

    with db.conexao(cfg.database_url) as conn:
        return not db.produtos_para_coleta(conn, 1)


# ---------------------------------------------------------------------------
# Coleta de um perfume (usada pela rodada diária e pelos pedidos do app)
# ---------------------------------------------------------------------------

def publicar_coleta(pub: Publicador, cfg: Config, evento: ProdutoEvento, reviews: list[ReviewEvento]) -> None:
    """Reviews primeiro, ficha depois: quando a ficha marca a visita, as reviews já estão no tópico."""
    for r in reviews:
        pub.publicar(cfg.topico_reviews, str(r.id_perfume), r.model_dump(mode="json"))
    pub.publicar(cfg.topico_produtos, str(evento.id_perfume), evento.model_dump(mode="json"))
    pub.finalizar()


def coletar(pub: Publicador, cfg: Config, limite: Optional[int] = None,
            coletor=None, entre: Callable[[], None] = _nada) -> dict:
    """Rodada incremental: ficha + reviews novas dos perfumes da fila de rodízio."""
    from . import db

    with db.conexao(cfg.database_url) as conn:
        fila = db.produtos_para_coleta(conn, limite or cfg.max_produtos_por_coleta)
    log.info("Coleta incremental: %d produtos na fila", len(fila))
    resumo = {"produtos": 0, "reviews_novas": 0, "falhas": 0}
    if not fila:
        log.warning("Nenhum produto com link no banco. Rode antes o modo `catalogo` (ou a carga-csv).")
        return resumo

    with _navegador(cfg, coletor) as nav:
        for i, produto in enumerate(fila, start=1):
            entre()  # pedidos do app têm prioridade sobre a rodada
            try:
                evento, reviews = nav.coletar(produto)
                publicar_coleta(pub, cfg, evento, reviews)
            except Exception as erro:
                resumo["falhas"] += 1
                log.error("[%d/%d] %s: %s", i, len(fila), produto["nome"], erro)
                continue
            resumo["produtos"] += 1
            resumo["reviews_novas"] += len(reviews)
            log.info("[%d/%d] %s: %d reviews novas", i, len(fila), produto["nome"], len(reviews))
            time.sleep(1.5)
    log.info("Coleta concluída: %s", resumo)
    return resumo


def atender_pedido(nav, pedido: PedidoColeta, pub: Publicador, cfg: Config,
                   produto_no_banco: Optional[dict]) -> StatusPedido:
    """Coleta um perfume pedido pelo app e publica o andamento.

    Usa a marca d'água do banco (só reviews novas) e limita às `llm_max_reviews`
    mais recentes, que é o que a análise usa: a espera fica em 1-2 minutos mesmo
    para perfumes com milhares de reviews."""
    def status(nome: str, **extra) -> StatusPedido:
        st = StatusPedido(id_pedido=pedido.id_pedido, id_perfume=pedido.id_perfume, status=nome, **extra)
        pub.publicar(cfg.topico_status, str(pedido.id_perfume), st.model_dump(mode="json"))
        pub.finalizar()
        return st

    idade = (datetime.now(timezone.utc) - pedido.solicitado_em).total_seconds()
    if idade > cfg.espera_coleta_s:
        return status("erro", mensagem="Pedido expirado: o coletor estava fora do ar quando ele foi feito.")

    status("coletando")
    base = produto_no_banco or {}
    produto = {
        "id_perfume": pedido.id_perfume,
        "link": base.get("link") or pedido.link,
        "designer": base.get("designer") or pedido.designer or "desconhecido",
        "nome": base.get("nome") or pedido.nome or f"Perfume {pedido.id_perfume}",
        "max_review_id": base.get("max_review_id"),
    }
    try:
        evento, reviews = nav.coletar(produto, max_reviews=cfg.llm_max_reviews)
        publicar_coleta(pub, cfg, evento, reviews)
    except Exception as erro:
        log.error("Pedido %s (%s): %s", pedido.id_pedido, produto["nome"], erro)
        return status("erro", mensagem=f"Não foi possível coletar: {str(erro)[:300]}")
    maior = max((r.review_id for r in reviews), default=None)
    log.info("Pedido %s atendido: %s, %d reviews novas", pedido.id_pedido, produto["nome"], len(reviews))
    return status("concluido", reviews_coletadas=len(reviews), max_review_id=maior)


def proxima_execucao(agora: datetime, horario: str) -> datetime:
    """Próximo HH:MM a partir de `agora` (no fuso de `agora`)."""
    hora, minuto = (int(p) for p in horario.split(":"))
    alvo = agora.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    return alvo if alvo > agora else alvo + timedelta(days=1)


def agendado(cfg: Config) -> None:  # pragma: no cover - laço infinito, depende de Kafka e Chrome
    """Serviço do coletor: atende pedidos do app a qualquer hora e faz a rodada diária.

    Um único navegador fica aberto. Durante a rodada diária (que pode levar horas),
    os pedidos do app são atendidos entre um perfume e outro."""
    from confluent_kafka import Consumer

    from . import db
    from .scraper.fragrantica import ColetorFragrantica

    fuso = ZoneInfo(cfg.fuso_horario)
    pub = KafkaPublicador(cfg.kafka_bootstrap)
    pedidos = Consumer({"bootstrap.servers": cfg.kafka_bootstrap, "group.id": "fraginsights-coletor",
                        "auto.offset.reset": "earliest", "enable.auto.commit": False})
    pedidos.subscribe([cfg.topico_pedidos])
    ultimo_catalogo: Optional[datetime] = None

    with ColetorFragrantica(headless=cfg.headless, max_cliques=cfg.max_cliques_reviews,
                            binario_chrome=_binario_chrome()) as nav:

        def atender(espera: float) -> None:
            for msg in pedidos.consume(num_messages=10, timeout=espera):
                if msg.error():
                    continue
                try:
                    pedido = PedidoColeta.model_validate_json(msg.value())
                    with db.conexao(cfg.database_url) as conn:
                        no_banco = db.consultar_um(conn, db.SQL_PRODUTO, {"id_perfume": pedido.id_perfume})
                    atender_pedido(nav, pedido, pub, cfg, no_banco)
                except Exception:
                    log.exception("Pedido inválido ou falha inesperada")
                pedidos.commit(message=msg, asynchronous=False)

        def rodada() -> None:
            nonlocal ultimo_catalogo
            agora = datetime.now(fuso)
            vencido = (ultimo_catalogo is not None
                       and agora - ultimo_catalogo >= timedelta(days=cfg.atualizar_catalogo_dias))
            if vencido or (ultimo_catalogo is None and _banco_sem_produtos(cfg)):
                catalogo(pub, cfg, coletor=nav, entre=lambda: atender(0))
                ultimo_catalogo = agora
                time.sleep(30)  # dá tempo do consumer gravar o catálogo antes da coleta
            elif ultimo_catalogo is None:
                ultimo_catalogo = agora  # banco já tinha catálogo: próxima atualização em N dias
            coletar(pub, cfg, coletor=nav, entre=lambda: atender(0))

        proxima = datetime.now(fuso) if cfg.coletar_ao_iniciar else proxima_execucao(datetime.now(fuso), cfg.horario_coleta)
        log.info("Coletor no ar: atendendo pedidos do app; rodada diária em %s", proxima.isoformat())
        while True:
            atender(5.0)
            if datetime.now(fuso) >= proxima:
                try:
                    rodada()
                except Exception:
                    log.exception("Rodada diária falhou; nova tentativa no próximo horário")
                proxima = proxima_execucao(datetime.now(fuso), cfg.horario_coleta)
                log.info("Próxima rodada diária: %s", proxima.isoformat())


def main(argv: Optional[list[str]] = None) -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="modo", required=True)
    p_csv = sub.add_parser("carga-csv", help="publica os CSVs do Colab no Kafka")
    p_csv.add_argument("--pasta", default=None, help="pasta com perfumes.csv / detalhes_perfumes.csv / reviews.csv")
    p_csv.add_argument("--fonte", default="csv", choices=["csv", "exemplo"])
    p_cat = sub.add_parser("catalogo", help="descobre marcas populares e seus perfumes no site")
    p_cat.add_argument("--marcas", type=int, default=None, help="quantas marcas do ranking (padrão MAX_MARCAS=60)")
    p_col = sub.add_parser("coletar", help="uma rodada de coleta incremental")
    p_col.add_argument("--limite", type=int, default=None)
    sub.add_parser("agendado", help="serviço: pedidos do app a qualquer hora + rodada diária no HORARIO_COLETA")
    args = parser.parse_args(argv)

    cfg = carregar()
    garantir_topicos(cfg.kafka_bootstrap, topicos(cfg), cfg.kafka_particoes)
    if args.modo == "catalogo":
        catalogo(KafkaPublicador(cfg.kafka_bootstrap), cfg, args.marcas)
    elif args.modo == "carga-csv":
        carga_csv(KafkaPublicador(cfg.kafka_bootstrap), cfg, Path(args.pasta or cfg.pasta_entrada), args.fonte)
    elif args.modo == "coletar":
        coletar(KafkaPublicador(cfg.kafka_bootstrap), cfg, args.limite)
    else:
        agendado(cfg)


if __name__ == "__main__":  # pragma: no cover
    main()
