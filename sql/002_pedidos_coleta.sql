-- Coleta sob demanda: andamento dos pedidos feitos pelo app.
-- Idempotente: o consumer aplica todos os arquivos desta pasta ao iniciar, então
-- bancos criados antes desta versão também recebem a tabela.
CREATE TABLE IF NOT EXISTS pedidos_coleta (
    id_pedido          TEXT PRIMARY KEY,
    id_perfume         BIGINT NOT NULL,
    link               TEXT,
    status             TEXT NOT NULL,          -- na_fila -> coletando -> concluido | erro
    reviews_coletadas  INTEGER,
    max_review_id      BIGINT,
    mensagem           TEXT,
    solicitado_em      TIMESTAMPTZ NOT NULL,
    atualizado_em      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_pedidos_perfume ON pedidos_coleta (id_perfume, solicitado_em DESC);
