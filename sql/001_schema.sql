-- Esquema do Fragrantica Insights.
-- Executado automaticamente pelo container do PostgreSQL na primeira subida
-- (pasta montada em /docker-entrypoint-initdb.d).

-- Produtos (perfumes): 1 linha por perfume, atualizada a cada coleta.
CREATE TABLE IF NOT EXISTS produtos (
    id_perfume      BIGINT PRIMARY KEY,           -- id do Fragrantica (fim da URL)
    designer        TEXT NOT NULL,
    nome            TEXT NOT NULL,
    link            TEXT,
    genero          TEXT,
    ano             INTEGER,
    nota_media      NUMERIC(3, 2),                -- nota do site (0 a 5)
    votos           INTEGER,
    reviews_site    INTEGER,                      -- total de resenhas que o site informa
    perfumistas     TEXT[] NOT NULL DEFAULT '{}',
    acordes         TEXT[] NOT NULL DEFAULT '{}',
    notas_topo      TEXT[] NOT NULL DEFAULT '{}',
    notas_coracao   TEXT[] NOT NULL DEFAULT '{}',
    notas_base      TEXT[] NOT NULL DEFAULT '{}',
    fonte           TEXT NOT NULL,
    ultima_coleta   TIMESTAMPTZ,                  -- última vez que o scraper visitou a página
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
    atualizado_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_produtos_designer ON produtos (designer);

-- Reviews: 1 linha por review. Texto original preservado ao lado do texto limpo.
CREATE TABLE IF NOT EXISTS reviews (
    review_id       BIGINT PRIMARY KEY,           -- id do site (div#review_<id>), cresce com o tempo
    id_perfume      BIGINT NOT NULL REFERENCES produtos (id_perfume),
    autor           TEXT,                         -- pseudonimizado por padrão
    data_texto      TEXT,                         -- como aparece no site ("19 hours ago", "09/23/26 05:23")
    data_review     TIMESTAMPTZ,                  -- data interpretada
    texto_original  TEXT NOT NULL,
    texto_limpo     TEXT NOT NULL,
    n_palavras      INTEGER NOT NULL,
    informativa     BOOLEAN NOT NULL,             -- falso para reviews curtas demais para a análise
    hash_conteudo   TEXT NOT NULL UNIQUE,         -- deduplicação por conteúdo (produto + texto)
    fonte           TEXT NOT NULL,
    coletado_em     TIMESTAMPTZ NOT NULL,
    ingerido_em     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_reviews_perfume ON reviews (id_perfume, review_id DESC);
CREATE INDEX IF NOT EXISTS ix_reviews_ingerido ON reviews (ingerido_em);

-- Análises geradas pelo LLM (cache). Uma nova análise só é gerada quando o
-- conjunto de reviews usado muda (assinatura) ou quando o prompt/modelo muda.
CREATE TABLE IF NOT EXISTS analises (
    id                  BIGSERIAL PRIMARY KEY,
    id_perfume          BIGINT NOT NULL REFERENCES produtos (id_perfume),
    provedor            TEXT NOT NULL,
    modelo_extracao     TEXT NOT NULL,
    modelo_sintese      TEXT NOT NULL,
    versao_prompt       TEXT NOT NULL,
    assinatura          TEXT NOT NULL,            -- hash dos review_id usados
    n_reviews_usadas    INTEGER NOT NULL,
    ultimo_review_id    BIGINT,
    resultado           JSONB NOT NULL,           -- notas, resumos, evidências, perfil de cliente
    taxa_evidencias_ok  NUMERIC(4, 3),            -- % de evidências encontradas literalmente nas reviews
    duracao_s           NUMERIC(8, 2),
    criado_em           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id_perfume, provedor, modelo_sintese, versao_prompt, assinatura)
);

CREATE INDEX IF NOT EXISTS ix_analises_perfume ON analises (id_perfume, criado_em DESC);

-- Log de ingestão do consumer (observabilidade simples).
CREATE TABLE IF NOT EXISTS ingestao_log (
    id              BIGSERIAL PRIMARY KEY,
    executado_em    TIMESTAMPTZ NOT NULL DEFAULT now(),
    mensagens       INTEGER NOT NULL,
    produtos        INTEGER NOT NULL,
    reviews_novas   INTEGER NOT NULL,
    reviews_dup     INTEGER NOT NULL,
    rejeitadas      INTEGER NOT NULL
);

-- Visão usada pelo app: produtos com contagem de reviews no banco.
CREATE OR REPLACE VIEW vw_produtos AS
SELECT p.*,
       COALESCE(r.n_reviews, 0)      AS n_reviews,
       COALESCE(r.n_informativas, 0) AS n_informativas,
       r.ultima_review,
       r.max_review_id
FROM produtos p
LEFT JOIN (
    SELECT id_perfume,
           COUNT(*)                          AS n_reviews,
           COUNT(*) FILTER (WHERE informativa) AS n_informativas,
           MAX(data_review)                  AS ultima_review,
           MAX(review_id)                    AS max_review_id
    FROM reviews
    GROUP BY id_perfume
) r USING (id_perfume);
