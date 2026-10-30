"""Regras do fluxo híbrido usadas pelo app (sem Streamlit, testáveis).

- `precisa_coletar`: decide se o perfume escolhido pode ser analisado com o que já
  está no banco ou se precisa de uma coleta sob demanda antes.
- `estado_da_coleta`: interpreta o andamento de um pedido (tabela pedidos_coleta)
  e diz quando as reviews já chegaram ao banco.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ..bus import Publicador
from ..config import Config
from ..models import PedidoColeta


def precisa_coletar(produto: Optional[dict], agora: datetime, cfg: Config) -> tuple[bool, str]:
    if produto is None:
        return True, "Este perfume ainda não está no banco."
    if not produto.get("link") or produto.get("fonte") == "exemplo":
        return False, ""  # sem link ou dado fictício: não há o que coletar no site
    if produto.get("n_informativas", 0) < cfg.min_reviews_analise:
        return True, f"O banco tem só {produto.get('n_reviews', 0)} reviews deste perfume."
    ultima = produto.get("ultima_coleta")
    if ultima is not None and agora - ultima > timedelta(days=cfg.validade_coleta_dias):
        return True, f"As reviews foram coletadas em {ultima:%d/%m/%Y}, há mais de {cfg.validade_coleta_dias} dias."
    return False, ""


def solicitar_coleta(pub: Publicador, cfg: Config, *, id_perfume: int, link: str,
                     designer: Optional[str] = None, nome: Optional[str] = None) -> PedidoColeta:
    pedido = PedidoColeta(id_perfume=id_perfume, link=link, designer=designer, nome=nome)
    pub.publicar(cfg.topico_pedidos, str(id_perfume), pedido.model_dump(mode="json"))
    pub.finalizar()
    return pedido


MENSAGENS = {
    None: "Enviando o pedido ao coletor...",
    "na_fila": "Pedido na fila do coletor...",
    "coletando": "Coletando as reviews no Fragrantica (1 a 2 minutos)...",
    "concluido": "Coleta concluída, gravando no banco...",
}


def estado_da_coleta(pedido: Optional[dict], produto: Optional[dict]) -> tuple[str, str]:
    """('aguardando' | 'pronto' | 'erro', mensagem para a tela)."""
    status = pedido["status"] if pedido else None
    if status == "erro":
        return "erro", pedido.get("mensagem") or "A coleta falhou."
    if status == "concluido":
        chegou_ficha = (produto is not None and produto.get("ultima_coleta") is not None
                        and produto["ultima_coleta"] >= pedido["solicitado_em"])
        maior = pedido.get("max_review_id")
        chegaram_reviews = maior is None or (produto is not None and (produto.get("max_review_id") or 0) >= maior)
        if chegou_ficha and chegaram_reviews:
            n = pedido.get("reviews_coletadas") or 0
            return "pronto", f"Coleta concluída: {n} reviews novas."
    return "aguardando", MENSAGENS.get(status, MENSAGENS[None])
