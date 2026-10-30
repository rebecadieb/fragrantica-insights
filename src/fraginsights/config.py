"""Configuração central, lida de variáveis de ambiente (ver .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(nome: str, padrao: bool) -> bool:
    valor = os.getenv(nome)
    if valor is None:
        return padrao
    return valor.strip().lower() in {"1", "true", "sim", "yes", "on"}


def _int(nome: str, padrao: int) -> int:
    valor = os.getenv(nome)
    return int(valor) if valor not in (None, "") else padrao


@dataclass(frozen=True)
class Config:
    # --- Kafka ---------------------------------------------------------------
    kafka_bootstrap: str = field(default_factory=lambda: os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:29092"))
    topico_produtos: str = field(default_factory=lambda: os.getenv("TOPICO_PRODUTOS", "fragrantica.produtos"))
    topico_reviews: str = field(default_factory=lambda: os.getenv("TOPICO_REVIEWS", "fragrantica.reviews"))
    topico_pedidos: str = field(default_factory=lambda: os.getenv("TOPICO_PEDIDOS", "fragrantica.pedidos"))
    topico_status: str = field(default_factory=lambda: os.getenv("TOPICO_STATUS", "fragrantica.pedidos.status"))
    topico_dlq: str = field(default_factory=lambda: os.getenv("TOPICO_DLQ", "fragrantica.dlq"))
    kafka_particoes: int = field(default_factory=lambda: _int("KAFKA_PARTICOES", 3))
    consumer_group: str = field(default_factory=lambda: os.getenv("KAFKA_CONSUMER_GROUP", "fraginsights-ingestao"))

    # --- PostgreSQL ----------------------------------------------------------
    database_url: str = field(default_factory=lambda: os.getenv(
        "DATABASE_URL", "postgresql://fragrantica:fragrantica@localhost:5432/fragrantica"))

    # --- ETL -----------------------------------------------------------------
    anonimizar_autores: bool = field(default_factory=lambda: _bool("ANONIMIZAR_AUTORES", True))
    min_palavras_informativa: int = field(default_factory=lambda: _int("MIN_PALAVRAS_INFORMATIVA", 5))

    # --- Coleta (scraper) ----------------------------------------------------
    horario_coleta: str = field(default_factory=lambda: os.getenv("HORARIO_COLETA", "03:00"))
    fuso_horario: str = field(default_factory=lambda: os.getenv("TZ", "America/Fortaleza"))
    coletar_ao_iniciar: bool = field(default_factory=lambda: _bool("COLETAR_AO_INICIAR", False))
    atualizar_catalogo_dias: int = field(default_factory=lambda: _int("ATUALIZAR_CATALOGO_DIAS", 7))
    max_marcas: int = field(default_factory=lambda: _int("MAX_MARCAS", 60))
    max_produtos_por_coleta: int = field(default_factory=lambda: _int("MAX_PRODUTOS_POR_COLETA", 200))
    max_cliques_reviews: int = field(default_factory=lambda: _int("MAX_CLIQUES_REVIEWS", 100))
    headless: bool = field(default_factory=lambda: _bool("SCRAPER_HEADLESS", False))
    # coleta sob demanda (pedidos do app)
    validade_coleta_dias: int = field(default_factory=lambda: _int("VALIDADE_COLETA_DIAS", 7))
    espera_coleta_s: int = field(default_factory=lambda: _int("ESPERA_COLETA_S", 600))
    pasta_entrada: str = field(default_factory=lambda: os.getenv("PASTA_ENTRADA", "data/input"))

    # --- LLM -----------------------------------------------------------------
    llm_provedor: str = field(default_factory=lambda: os.getenv("LLM_PROVEDOR", "anthropic"))
    llm_modelo_extracao: str = field(default_factory=lambda: os.getenv("LLM_MODELO_EXTRACAO", "claude-haiku-4-5-20251001"))
    llm_modelo_sintese: str = field(default_factory=lambda: os.getenv("LLM_MODELO_SINTESE", "claude-sonnet-5"))
    llm_paralelo: int = field(default_factory=lambda: _int("LLM_PARALELO", 4))
    # Ollama (modelo open source local)
    ollama_url: str = field(default_factory=lambda: os.getenv("OLLAMA_URL", "http://localhost:11434"))
    ollama_modelo: str = field(default_factory=lambda: os.getenv("OLLAMA_MODELO", "qwen2.5:3b"))
    # Modelo do estágio 2 (síntese: 1 chamada, é o texto que o usuário lê). Vazio = o mesmo
    # OLLAMA_MODELO. Dá para usar um modelo maior/melhor em português só aqui.
    ollama_modelo_sintese: str = field(default_factory=lambda: os.getenv("OLLAMA_MODELO_SINTESE", ""))
    ollama_num_ctx: int = field(default_factory=lambda: _int("OLLAMA_NUM_CTX", 8192))
    # um modelo 7B num notebook é bem mais lento que a API: lê menos reviews, em lotes menores, um por vez
    ollama_max_reviews: int = field(default_factory=lambda: _int("OLLAMA_MAX_REVIEWS", 40))
    ollama_reviews_por_lote: int = field(default_factory=lambda: _int("OLLAMA_REVIEWS_POR_LOTE", 10))
    ollama_paralelo: int = field(default_factory=lambda: _int("OLLAMA_PARALELO", 1))
    ollama_timeout_s: int = field(default_factory=lambda: _int("OLLAMA_TIMEOUT_S", 600))
    llm_reviews_por_lote: int = field(default_factory=lambda: _int("LLM_REVIEWS_POR_LOTE", 40))
    llm_max_reviews: int = field(default_factory=lambda: _int("LLM_MAX_REVIEWS", 100))
    min_reviews_analise: int = field(default_factory=lambda: _int("MIN_REVIEWS_ANALISE", 5))
    # Modo da análise: "detalhado" (lotes + síntese; ~2 a 4 min com 40 reviews no qwen2.5:3b) ou
    # "rapido" (uma chamada; com o 3B pulou reviews e às vezes não achou nada: só para modelos maiores).
    modo_analise_app: str = field(default_factory=lambda: os.getenv("MODO_ANALISE_APP", "detalhado"))
    modo_analise_analista: str = field(default_factory=lambda: os.getenv("MODO_ANALISE_ANALISTA", "detalhado"))
    # Manda ao modelo só as frases que tocam em alguma dimensão (menos texto, mais rápido).
    filtrar_frases: bool = field(default_factory=lambda: _bool("FILTRAR_FRASES", True))
    # Analista em lote (python -m fraginsights.analista): gera análises sozinho, um perfume
    # por vez, e cede a vez quando o app está gerando uma análise pedida pelo usuário.
    analista_inicio: str = field(default_factory=lambda: os.getenv("ANALISTA_INICIO", ""))  # "22:00"; vazio = sempre
    analista_fim: str = field(default_factory=lambda: os.getenv("ANALISTA_FIM", ""))        # "07:00"
    # O analista não tem pressa: lê mais reviews que o app (mais relatos -> mais confiança).
    analista_max_reviews: int = field(default_factory=lambda: _int("ANALISTA_MAX_REVIEWS", 100))
    analista_max_por_dia: int = field(default_factory=lambda: _int("ANALISTA_MAX_POR_DIA", 50))
    analista_espera_s: int = field(default_factory=lambda: _int("ANALISTA_ESPERA_S", 300))
    analista_permitir_pago: bool = field(default_factory=lambda: _bool("ANALISTA_PERMITIR_PAGO", False))


def topicos(cfg: "Config") -> list[str]:
    return [cfg.topico_produtos, cfg.topico_reviews, cfg.topico_pedidos, cfg.topico_status, cfg.topico_dlq]


def carregar() -> Config:
    return Config()
