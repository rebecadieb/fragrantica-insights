"""Analista em lote: gera as análises de IA sozinho, sem ninguém clicar no app.

    python -m fraginsights.analista          (serviço `analista` do docker compose)

Loop:
  1. busca a fila: perfumes com reviews suficientes e sem análise do modelo atual que
     cubra a review mais recente (os mais populares primeiro);
  2. analisa um perfume por vez e salva no banco (a mesma tabela `analises` que o app lê);
  3. fila vazia, fora da janela ou limite do dia atingido: dorme e tenta de novo.

Cede a vez ao app: antes de CADA chamada ao modelo, confere na tabela `uso_llm` se o app
está gerando uma análise pedida pelo usuário. Se estiver, espera ela terminar. Assim quem
está na tela nunca fica atrás da fila (a GPU local atende uma chamada por vez).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, time as hora, timedelta
from typing import Callable, Optional

from . import db
from .config import Config, carregar
from .llm.analise import max_reviews_analista, obter_ou_gerar
from .llm.clientes import criar_cliente, modelos_do_modo
from .llm.prompts import MODOS, versao

log = logging.getLogger("fraginsights.analista")

PAUSA_APP_S = 5            # intervalo entre as conferências enquanto o app usa o modelo
PULAR_FALHA_H = 24         # perfume que falhou só volta para a fila depois disso
PROVEDORES_PAGOS = {"anthropic"}


# --- Regras puras (testáveis) ------------------------------------------------

def _ler_hora(texto: str) -> Optional[hora]:
    texto = (texto or "").strip()
    if not texto:
        return None
    h, _, m = texto.partition(":")
    return hora(int(h), int(m or 0))


def dentro_da_janela(agora: datetime, inicio: str, fim: str) -> bool:
    """Janela vazia = sempre. Aceita janela que vira a noite (22:00 → 07:00)."""
    ini, fi = _ler_hora(inicio), _ler_hora(fim)
    if ini is None or fi is None or ini == fi:
        return True
    t = agora.time()
    return ini <= t < fi if ini < fi else (t >= ini or t < fi)


def permitido(cfg: Config, provedor: str) -> Optional[str]:
    """Motivo para NÃO rodar, ou None. Provedor pago só roda com autorização explícita."""
    if provedor in PROVEDORES_PAGOS and not cfg.analista_permitir_pago:
        return (f"provedor '{provedor}' é pago; o analista em lote fica desligado "
                "(defina ANALISTA_PERMITIR_PAGO=true para ligar)")
    if cfg.analista_max_por_dia <= 0:
        return "ANALISTA_MAX_POR_DIA=0"
    if cfg.modo_analise_analista not in MODOS:
        return f"MODO_ANALISE_ANALISTA={cfg.modo_analise_analista!r} (use {' ou '.join(MODOS)})"
    return None


class ClienteQueCede:
    """Embrulha o cliente LLM: antes de cada chamada, espera enquanto o app estiver usando."""

    def __init__(self, cliente, app_ocupado: Callable[[], bool],
                 dormir: Callable[[float], None] = time.sleep, pausa_s: float = PAUSA_APP_S):
        self.cliente = cliente
        self.provedor = cliente.provedor
        self.app_ocupado = app_ocupado
        self.dormir = dormir
        self.pausa_s = pausa_s
        self.esperas = 0

    def gerar(self, **kwargs):
        avisou = False
        while self.app_ocupado():
            if not avisou:
                log.info("App gerando uma análise: analista aguardando a vez")
                avisou = True
            self.esperas += 1
            self.dormir(self.pausa_s)
        if avisou:
            log.info("App liberou o modelo: analista continuando")
        return self.cliente.gerar(**kwargs)

    def __getattr__(self, nome):  # verificar(), etc.
        return getattr(self.cliente, nome)


def escolher(fila: list[dict], falhas: dict[int, datetime], agora: datetime) -> Optional[dict]:
    """Primeiro da fila que não falhou nas últimas PULAR_FALHA_H horas."""
    limite = agora - timedelta(hours=PULAR_FALHA_H)
    for item in fila:
        quando = falhas.get(item["id_perfume"])
        if quando is None or quando < limite:
            return item
    return None


# --- Loop --------------------------------------------------------------------

def _app_ocupado() -> bool:
    try:
        with db.conexao() as conn:
            return bool(db.uso_llm_ativo(conn, "app"))
    except Exception:  # banco fora do ar: não trava o analista por isso
        log.exception("Não consegui consultar o uso do modelo")
        return False


def rodada(cfg: Config, cliente, falhas: dict[int, datetime], agora: datetime) -> str:
    """Uma iteração. Devolve 'analisou', 'falhou', 'fila_vazia', 'limite' ou 'fora_da_janela'."""
    if not dentro_da_janela(agora, cfg.analista_inicio, cfg.analista_fim):
        return "fora_da_janela"
    modo = cfg.modo_analise_analista
    with db.conexao() as conn:
        hoje = db.consultar_um(conn, "SELECT COUNT(*) AS n FROM analises WHERE criado_em >= date_trunc('day', now())")
        if hoje and hoje["n"] >= cfg.analista_max_por_dia:
            return "limite"
        fila = db.consultar(conn, db.SQL_FILA_ANALISE, {
            "provedor": cliente.provedor, "modelo_sintese": modelos_do_modo(cfg, cliente.provedor, modo)[1],
            "versao_prompt": versao(modo), "min_reviews": cfg.min_reviews_analise, "limite": 50})
    item = escolher(fila, falhas, agora)
    if item is None:
        return "fila_vazia"

    nome = f"{item['nome']} ({item['designer']})"
    log.info("Analisando %s: %s reviews; %s na fila", nome, item["n_reviews"], len(fila))
    inicio = time.time()
    try:
        with db.usando_llm("analista", item["id_perfume"]), db.conexao() as conn:
            obter_ou_gerar(conn, item["id_perfume"], ClienteQueCede(cliente, _app_ocupado), cfg, modo=modo,
                           max_reviews=max_reviews_analista(cfg, cliente.provedor))
    except Exception as erro:
        falhas[item["id_perfume"]] = agora
        log.warning("Falhou %s: %s (tento de novo em %s h)", nome, erro, PULAR_FALHA_H)
        return "falhou"
    log.info("Pronto %s em %.0f s", nome, time.time() - inicio)
    return "analisou"


def executar(cfg: Optional[Config] = None) -> None:
    cfg = cfg or carregar()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with db.conexao() as conn:
        db.aplicar_migracoes(conn)
        # Só existe um analista: marcas "analista" que sobraram são de um container interrompido
        # no meio de uma análise, e fariam o app avisar que o analista está trabalhando.
        db.limpar_uso_llm(conn, "analista")

    cliente = criar_cliente(cfg.llm_provedor, cfg)
    motivo = permitido(cfg, cliente.provedor)
    if motivo:
        log.warning("Analista parado: %s", motivo)
        while True:  # fica de pé (sem reiniciar em loop no compose), sem fazer nada
            time.sleep(3600)

    janela = (f"{cfg.analista_inicio}–{cfg.analista_fim}" if cfg.analista_inicio and cfg.analista_fim
              else "o dia todo")
    log.info("Analista iniciado: provedor %s, modo %s, até %s reviews por perfume, janela %s, até %s análises por dia",
             cliente.provedor, cfg.modo_analise_analista, max_reviews_analista(cfg, cliente.provedor), janela,
             cfg.analista_max_por_dia)
    falhas: dict[int, datetime] = {}
    ultimo_estado = None
    while True:
        try:
            estado = rodada(cfg, cliente, falhas, datetime.now())
        except Exception:
            log.exception("Erro na rodada do analista")
            estado = "erro"
        if estado in {"analisou", "falhou"}:
            ultimo_estado = estado
            continue
        if estado != ultimo_estado:
            log.info("Analista em pausa (%s); confiro de novo a cada %s s", estado, cfg.analista_espera_s)
            ultimo_estado = estado
        time.sleep(cfg.analista_espera_s)


if __name__ == "__main__":
    executar()
