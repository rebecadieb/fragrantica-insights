"""Camada fina sobre o Kafka (confluent-kafka).

Também oferece um barramento em memória com a mesma interface, usado nos testes
para exercitar producer -> consumer sem precisar de um broker.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Protocol

log = logging.getLogger(__name__)


def _json_default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Tipo não serializável: {type(obj)}")


def serializar(valor: dict) -> bytes:
    return json.dumps(valor, ensure_ascii=False, default=_json_default).encode("utf-8")


@dataclass
class Mensagem:
    topico: str
    chave: str | None
    valor: bytes


class Publicador(Protocol):
    def publicar(self, topico: str, chave: str, valor: dict) -> None: ...
    def finalizar(self) -> None: ...


# ---------------------------------------------------------------------------
# Kafka de verdade
# ---------------------------------------------------------------------------

def garantir_topicos(bootstrap: str, topicos: Iterable[str], particoes: int = 3) -> None:
    """Cria os tópicos se ainda não existirem (idempotente)."""
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient({"bootstrap.servers": bootstrap})
    existentes = set(admin.list_topics(timeout=15).topics)
    novos = [NewTopic(t, num_partitions=particoes, replication_factor=1)
             for t in topicos if t not in existentes]
    if not novos:
        return
    for topico, futuro in admin.create_topics(novos).items():
        try:
            futuro.result()
            log.info("Tópico criado: %s", topico)
        except Exception as erro:  # já existe (corrida entre serviços) não é problema
            log.warning("Tópico %s: %s", topico, erro)


class KafkaPublicador:
    """Producer idempotente: acks=all + enable.idempotence evita duplicar no broker."""

    def __init__(self, bootstrap: str):
        from confluent_kafka import Producer

        self._erros = 0
        self._enviadas = 0
        self._producer = Producer({
            "bootstrap.servers": bootstrap,
            "enable.idempotence": True,
            "acks": "all",
            "compression.type": "zstd",
            "linger.ms": 50,
        })

    def _callback(self, erro, msg):
        if erro is not None:
            self._erros += 1
            log.error("Falha ao entregar mensagem em %s: %s", msg.topic(), erro)
        else:
            self._enviadas += 1

    def publicar(self, topico: str, chave: str, valor: dict) -> None:
        while True:
            try:
                self._producer.produce(topico, key=chave, value=serializar(valor), on_delivery=self._callback)
                break
            except BufferError:  # fila local cheia: espera o broker esvaziar
                self._producer.poll(1)
        self._producer.poll(0)

    def finalizar(self) -> None:
        pendentes = self._producer.flush(60)
        log.debug("Kafka: %d mensagens entregues, %d erros, %d pendentes",
                 self._enviadas, self._erros, pendentes)
        if self._erros or pendentes:
            erros, self._erros = self._erros, 0  # o próximo lote começa limpo
            raise RuntimeError(f"{erros} mensagens falharam e {pendentes} ficaram pendentes")


# ---------------------------------------------------------------------------
# Barramento em memória (testes / demonstração sem broker)
# ---------------------------------------------------------------------------

class MemoriaPublicador:
    def __init__(self):
        self.mensagens: list[Mensagem] = []

    def publicar(self, topico: str, chave: str, valor: dict) -> None:
        self.mensagens.append(Mensagem(topico, chave, serializar(valor)))

    def finalizar(self) -> None:
        pass
