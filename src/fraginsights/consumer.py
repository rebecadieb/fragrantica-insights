"""Consumer: lê os tópicos do Kafka e grava no PostgreSQL de forma incremental.

Garantias:
- at-least-once no Kafka (offset só é confirmado depois do COMMIT no banco);
- idempotência no banco (ON CONFLICT), então reprocessar uma mensagem não duplica nada.
Juntas, as duas dão o efeito de "exatamente uma vez" no resultado final.

Uso:  python -m fraginsights.consumer
"""
from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass, field

from pydantic import ValidationError

from . import db
from .bus import Mensagem, garantir_topicos, serializar
from .config import Config, carregar, topicos
from .etl import tratar_review
from .models import PedidoColeta, ProdutoEvento, ReviewEvento, StatusPedido

log = logging.getLogger("consumer")

TAMANHO_LOTE = 500
ESPERA_LOTE_S = 2.0


@dataclass
class Estatisticas:
    mensagens: int = 0
    produtos: int = 0
    reviews_novas: int = 0
    reviews_dup: int = 0
    rejeitadas: int = 0
    pedidos: int = 0
    dlq: list[dict] = field(default_factory=list)

    def como_dict(self) -> dict:
        return {"mensagens": self.mensagens, "produtos": self.produtos,
                "reviews_novas": self.reviews_novas, "reviews_dup": self.reviews_dup,
                "rejeitadas": self.rejeitadas, "pedidos": self.pedidos}


def _produto_params(evento: ProdutoEvento) -> dict:
    p = evento.model_dump()
    # só a visita completa (ficha + reviews) conta como coleta; o catálogo não
    visita = evento.fonte == "scraper" and evento.tipo == "detalhes"
    p["ultima_coleta"] = evento.coletado_em if visita else None
    return p


def processar_lote(conn, mensagens: list[Mensagem], cfg: Config) -> Estatisticas:
    """Valida, trata e grava um lote em UMA transação. Não confirma offsets."""
    est = Estatisticas(mensagens=len(mensagens))
    produtos: list[ProdutoEvento] = []
    reviews: list[ReviewEvento] = []
    pedidos: list[PedidoColeta] = []
    status: list[StatusPedido] = []

    for msg in mensagens:
        try:
            if msg.topico == cfg.topico_produtos:
                produtos.append(ProdutoEvento.model_validate_json(msg.valor))
            elif msg.topico == cfg.topico_reviews:
                reviews.append(ReviewEvento.model_validate_json(msg.valor))
            elif msg.topico == cfg.topico_pedidos:
                pedidos.append(PedidoColeta.model_validate_json(msg.valor))
            elif msg.topico == cfg.topico_status:
                status.append(StatusPedido.model_validate_json(msg.valor))
            else:
                raise ValueError(f"tópico desconhecido: {msg.topico}")
        except (ValidationError, ValueError) as erro:
            est.rejeitadas += 1
            est.dlq.append({"topico_origem": msg.topico, "chave": msg.chave,
                            "valor": msg.valor.decode("utf-8", "replace"),
                            "erro": str(erro)[:2000]})

    with conn.cursor() as cur:
        # produtos primeiro, para que as reviews do mesmo lote encontrem o pai
        for evento in produtos:
            cur.execute(db.SQL_UPSERT_PRODUTO, _produto_params(evento))
            est.produtos += 1
        for evento in reviews:
            cur.execute(db.SQL_PRODUTO_STUB, {
                "id_perfume": evento.id_perfume,
                "designer": evento.designer or "desconhecido",
                "nome": evento.perfume or f"Perfume {evento.id_perfume}",
                "fonte": evento.fonte,
            })
            registro = tratar_review(evento, cfg.anonimizar_autores, cfg.min_palavras_informativa)
            cur.execute(db.SQL_INSERIR_REVIEW, registro)
            if cur.fetchone():
                est.reviews_novas += 1
            else:
                est.reviews_dup += 1
        # andamento dos pedidos sob demanda (por último: o app só lê depois do commit)
        for pedido in pedidos:
            cur.execute(db.SQL_REGISTRAR_PEDIDO, pedido.model_dump())
            est.pedidos += 1
        for st in status:
            cur.execute(db.SQL_STATUS_PEDIDO, st.model_dump())
        cur.execute(db.SQL_LOG_INGESTAO, est.como_dict())
    conn.commit()
    return est


def executar() -> None:  # pragma: no cover - laço de produção, depende de Kafka real
    from confluent_kafka import Consumer, KafkaError, Producer

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = carregar()
    garantir_topicos(cfg.kafka_bootstrap, topicos(cfg), cfg.kafka_particoes)
    with db.conexao(cfg.database_url) as conn:
        db.aplicar_migracoes(conn)  # bancos de versões anteriores ganham as tabelas novas
    assinados = [cfg.topico_produtos, cfg.topico_reviews, cfg.topico_pedidos, cfg.topico_status]

    consumer = Consumer({
        "bootstrap.servers": cfg.kafka_bootstrap,
        "group.id": cfg.consumer_group,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    consumer.subscribe(assinados)
    dlq = Producer({"bootstrap.servers": cfg.kafka_bootstrap})

    rodando = True

    def parar(*_):
        nonlocal rodando
        rodando = False

    signal.signal(signal.SIGTERM, parar)
    signal.signal(signal.SIGINT, parar)

    conn = db.conectar()
    log.info("Consumindo %s (grupo %s)", ", ".join(assinados), cfg.consumer_group)
    try:
        while rodando:
            lote = consumer.consume(num_messages=TAMANHO_LOTE, timeout=ESPERA_LOTE_S)
            mensagens = []
            for m in lote:
                if m.error():
                    if m.error().code() != KafkaError._PARTITION_EOF:
                        log.error("Erro do Kafka: %s", m.error())
                    continue
                chave = m.key().decode() if m.key() else None
                mensagens.append(Mensagem(m.topic(), chave, m.value()))
            if not mensagens:
                continue
            try:
                est = processar_lote(conn, mensagens, cfg)
            except Exception:
                conn.rollback()
                log.exception("Falha ao gravar o lote; offsets não confirmados, nova tentativa em 5 s")
                time.sleep(5)
                conn.close()
                conn = db.conectar()
                consumer.close()  # força rebalanceamento e releitura a partir do último commit
                consumer = Consumer({
                    "bootstrap.servers": cfg.kafka_bootstrap, "group.id": cfg.consumer_group,
                    "auto.offset.reset": "earliest", "enable.auto.commit": False,
                })
                consumer.subscribe(assinados)
                continue
            for item in est.dlq:
                dlq.produce(cfg.topico_dlq, key=item["chave"], value=serializar(item))
            dlq.flush(10)
            consumer.commit(asynchronous=False)
            log.info("Lote gravado: %s", est.como_dict())
    finally:
        consumer.close()
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    executar()
