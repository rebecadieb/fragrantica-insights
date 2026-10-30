-- Quem está usando o LLM agora. O app registra uma linha enquanto gera uma análise
-- pedida pelo usuário; o analista em lote consulta a tabela antes de cada chamada ao
-- modelo e espera enquanto houver uma do app (a GPU local atende uma chamada por vez).
CREATE TABLE IF NOT EXISTS uso_llm (
    id           BIGSERIAL PRIMARY KEY,
    origem       TEXT NOT NULL,              -- 'app' | 'analista'
    id_perfume   BIGINT,
    iniciado_em  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_uso_llm_origem ON uso_llm (origem, iniciado_em);
