"""Acesso ao PostgreSQL (psycopg 3). Todo o SQL do projeto fica aqui."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .config import carregar

# psycopg é importado dentro das funções: os módulos que só usam as constantes de
# SQL (e os testes unitários) não precisam do driver instalado.

# ---------------------------------------------------------------------------
# Escrita (usada pelo consumer)
# ---------------------------------------------------------------------------

SQL_UPSERT_PRODUTO = """
INSERT INTO produtos (id_perfume, designer, nome, link, genero, ano, nota_media, votos,
                      reviews_site, perfumistas, acordes, notas_topo, notas_coracao,
                      notas_base, fonte, ultima_coleta)
VALUES (%(id_perfume)s, %(designer)s, %(nome)s, %(link)s, %(genero)s, %(ano)s, %(nota_media)s,
        %(votos)s, %(reviews_site)s, %(perfumistas)s, %(acordes)s, %(notas_topo)s,
        %(notas_coracao)s, %(notas_base)s, %(fonte)s, %(ultima_coleta)s)
ON CONFLICT (id_perfume) DO UPDATE SET
    designer      = EXCLUDED.designer,
    nome          = EXCLUDED.nome,
    link          = COALESCE(EXCLUDED.link, produtos.link),
    genero        = COALESCE(EXCLUDED.genero, produtos.genero),
    ano           = COALESCE(EXCLUDED.ano, produtos.ano),
    nota_media    = COALESCE(EXCLUDED.nota_media, produtos.nota_media),
    votos         = COALESCE(EXCLUDED.votos, produtos.votos),
    reviews_site  = COALESCE(EXCLUDED.reviews_site, produtos.reviews_site),
    perfumistas   = CASE WHEN cardinality(EXCLUDED.perfumistas) > 0 THEN EXCLUDED.perfumistas ELSE produtos.perfumistas END,
    acordes       = CASE WHEN cardinality(EXCLUDED.acordes) > 0 THEN EXCLUDED.acordes ELSE produtos.acordes END,
    notas_topo    = CASE WHEN cardinality(EXCLUDED.notas_topo) > 0 THEN EXCLUDED.notas_topo ELSE produtos.notas_topo END,
    notas_coracao = CASE WHEN cardinality(EXCLUDED.notas_coracao) > 0 THEN EXCLUDED.notas_coracao ELSE produtos.notas_coracao END,
    notas_base    = CASE WHEN cardinality(EXCLUDED.notas_base) > 0 THEN EXCLUDED.notas_base ELSE produtos.notas_base END,
    ultima_coleta = GREATEST(produtos.ultima_coleta, EXCLUDED.ultima_coleta),
    atualizado_em = now()
"""

# Garante que o produto existe antes da review (os tópicos não têm ordem entre si).
SQL_PRODUTO_STUB = """
INSERT INTO produtos (id_perfume, designer, nome, fonte)
VALUES (%(id_perfume)s, %(designer)s, %(nome)s, %(fonte)s)
ON CONFLICT (id_perfume) DO NOTHING
"""

# Idempotente: review repetida (mesmo id ou mesmo conteúdo) é ignorada.
SQL_INSERIR_REVIEW = """
INSERT INTO reviews (review_id, id_perfume, autor, data_texto, data_review, texto_original,
                     texto_limpo, n_palavras, informativa, hash_conteudo, fonte, coletado_em)
VALUES (%(review_id)s, %(id_perfume)s, %(autor)s, %(data_texto)s, %(data_review)s,
        %(texto_original)s, %(texto_limpo)s, %(n_palavras)s, %(informativa)s,
        %(hash_conteudo)s, %(fonte)s, %(coletado_em)s)
ON CONFLICT DO NOTHING
RETURNING review_id
"""

SQL_LOG_INGESTAO = """
INSERT INTO ingestao_log (mensagens, produtos, reviews_novas, reviews_dup, rejeitadas)
VALUES (%(mensagens)s, %(produtos)s, %(reviews_novas)s, %(reviews_dup)s, %(rejeitadas)s)
"""

# Pedido do app: entra como "na_fila". Se o status do coletor chegou antes (tópicos
# diferentes não têm ordem entre si), só completa link e horário do pedido.
SQL_REGISTRAR_PEDIDO = """
INSERT INTO pedidos_coleta (id_pedido, id_perfume, link, status, solicitado_em, atualizado_em)
VALUES (%(id_pedido)s, %(id_perfume)s, %(link)s, 'na_fila', %(solicitado_em)s, %(solicitado_em)s)
ON CONFLICT (id_pedido) DO UPDATE SET
    link = EXCLUDED.link,
    solicitado_em = EXCLUDED.solicitado_em
"""

# Status só avança (na_fila < coletando < concluido/erro), mesmo fora de ordem.
SQL_STATUS_PEDIDO = """
INSERT INTO pedidos_coleta (id_pedido, id_perfume, status, reviews_coletadas, max_review_id,
                            mensagem, solicitado_em, atualizado_em)
VALUES (%(id_pedido)s, %(id_perfume)s, %(status)s, %(reviews_coletadas)s, %(max_review_id)s,
        %(mensagem)s, %(em)s, %(em)s)
ON CONFLICT (id_pedido) DO UPDATE SET
    status            = EXCLUDED.status,
    reviews_coletadas = COALESCE(EXCLUDED.reviews_coletadas, pedidos_coleta.reviews_coletadas),
    max_review_id     = COALESCE(EXCLUDED.max_review_id, pedidos_coleta.max_review_id),
    mensagem          = COALESCE(EXCLUDED.mensagem, pedidos_coleta.mensagem),
    atualizado_em     = EXCLUDED.atualizado_em
WHERE (CASE EXCLUDED.status WHEN 'na_fila' THEN 0 WHEN 'coletando' THEN 1 ELSE 2 END)
   >= (CASE pedidos_coleta.status WHEN 'na_fila' THEN 0 WHEN 'coletando' THEN 1 ELSE 2 END)
"""

SQL_PEDIDO = "SELECT * FROM pedidos_coleta WHERE id_pedido = %(id_pedido)s"

SQL_SALVAR_ANALISE = """
INSERT INTO analises (id_perfume, provedor, modelo_extracao, modelo_sintese, versao_prompt,
                      assinatura, n_reviews_usadas, ultimo_review_id, resultado,
                      taxa_evidencias_ok, duracao_s)
VALUES (%(id_perfume)s, %(provedor)s, %(modelo_extracao)s, %(modelo_sintese)s, %(versao_prompt)s,
        %(assinatura)s, %(n_reviews_usadas)s, %(ultimo_review_id)s, %(resultado)s,
        %(taxa_evidencias_ok)s, %(duracao_s)s)
ON CONFLICT (id_perfume, provedor, modelo_sintese, versao_prompt, assinatura) DO UPDATE SET
    resultado = EXCLUDED.resultado,
    taxa_evidencias_ok = EXCLUDED.taxa_evidencias_ok,
    duracao_s = EXCLUDED.duracao_s,
    criado_em = now()
RETURNING id
"""

# Uso do LLM (o analista cede a vez ao app). Linhas com mais de 1 h são de processos
# que morreram sem limpar e são ignoradas.
SQL_INICIAR_USO_LLM = """
INSERT INTO uso_llm (origem, id_perfume) VALUES (%(origem)s, %(id_perfume)s) RETURNING id
"""
SQL_FINALIZAR_USO_LLM = "DELETE FROM uso_llm WHERE id = %(id)s"
SQL_LIMPAR_USO_LLM = "DELETE FROM uso_llm WHERE origem = %(origem)s"   # marcas de um processo que morreu
SQL_USO_LLM_ATIVO = """
SELECT origem, id_perfume, iniciado_em FROM uso_llm
WHERE origem = %(origem)s AND iniciado_em > now() - interval '1 hour'
ORDER BY iniciado_em
"""

# ---------------------------------------------------------------------------
# Leitura (producer e app)
# ---------------------------------------------------------------------------

# Rodízio: primeiro os nunca visitados, depois os visitados há mais tempo; entre
# eles, os mais populares (mais reviews) primeiro.
SQL_PRODUTOS_PARA_COLETA = """
SELECT id_perfume, designer, nome, link, max_review_id
FROM vw_produtos
WHERE link IS NOT NULL AND fonte <> 'exemplo'      -- dados fictícios nunca vão para o site
ORDER BY ultima_coleta ASC NULLS FIRST, n_reviews DESC, reviews_site DESC NULLS LAST
LIMIT %(limite)s
"""

# Fila do analista: perfumes com reviews suficientes e sem análise do modelo atual
# que cubra a review informativa mais recente. Os mais populares primeiro.
SQL_FILA_ANALISE = """
SELECT p.id_perfume, p.nome, p.designer, p.n_reviews
FROM vw_produtos p
JOIN LATERAL (
    SELECT MAX(review_id) AS max_informativa FROM reviews r
    WHERE r.id_perfume = p.id_perfume AND r.informativa
) ri ON true
LEFT JOIN LATERAL (
    SELECT MAX(ultimo_review_id) AS ultimo FROM analises a
    WHERE a.id_perfume = p.id_perfume AND a.provedor = %(provedor)s
      AND a.modelo_sintese = %(modelo_sintese)s AND a.versao_prompt = %(versao_prompt)s
) a ON true
WHERE p.n_informativas >= %(min_reviews)s
  AND p.fonte <> 'exemplo'
  AND (a.ultimo IS NULL OR ri.max_informativa > a.ultimo)
ORDER BY p.n_reviews DESC, p.id_perfume
LIMIT %(limite)s
"""

SQL_ANALISES_DIARIAS = """
SELECT date_trunc('day', criado_em)::date AS dia, COUNT(*) AS analises
FROM analises
GROUP BY 1
ORDER BY 1 DESC
LIMIT 30
"""

SQL_DESIGNERS = "SELECT DISTINCT designer FROM produtos ORDER BY designer"

SQL_LISTAR_PRODUTOS = """
SELECT id_perfume, designer, nome, genero, ano, n_reviews, n_informativas
FROM vw_produtos
WHERE (%(designer)s::text IS NULL OR designer = %(designer)s)
  AND (%(genero)s::text IS NULL OR genero = %(genero)s)
  AND (%(busca)s::text IS NULL OR nome ILIKE '%%' || %(busca)s || '%%')
  AND n_informativas >= %(min_reviews)s
ORDER BY n_reviews DESC, nome
LIMIT 500
"""

SQL_PRODUTO = "SELECT * FROM vw_produtos WHERE id_perfume = %(id_perfume)s"

SQL_REVIEWS_PRODUTO = """
SELECT review_id, autor, data_texto, data_review, texto_limpo, n_palavras, informativa
FROM reviews
WHERE id_perfume = %(id_perfume)s
  AND (NOT %(somente_informativas)s OR informativa)
ORDER BY review_id DESC
LIMIT %(limite)s
"""

SQL_ULTIMA_ANALISE = """
SELECT * FROM analises
WHERE id_perfume = %(id_perfume)s AND versao_prompt = ANY(%(versoes)s)   -- versões antigas do prompt não valem mais
ORDER BY criado_em DESC
LIMIT 1
"""

SQL_ANALISE_POR_ASSINATURA = """
SELECT * FROM analises
WHERE id_perfume = %(id_perfume)s AND provedor = %(provedor)s AND modelo_sintese = %(modelo_sintese)s
  AND versao_prompt = %(versao_prompt)s AND assinatura = %(assinatura)s
"""

SQL_INGESTAO_DIARIA = """
SELECT date_trunc('day', ingerido_em)::date AS dia, COUNT(*) AS reviews_novas
FROM reviews
GROUP BY 1
ORDER BY 1 DESC
LIMIT 30
"""

SQL_TOTAIS = """
SELECT (SELECT COUNT(*) FROM produtos)  AS produtos,
       (SELECT COUNT(*) FROM reviews)   AS reviews,
       (SELECT COUNT(*) FROM analises)  AS analises,
       (SELECT MAX(executado_em) FROM ingestao_log) AS ultima_ingestao
"""


# ---------------------------------------------------------------------------
# Conexão e helpers
# ---------------------------------------------------------------------------

def conectar(url: Optional[str] = None):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(url or carregar().database_url, row_factory=dict_row)


@contextmanager
def conexao(url: Optional[str] = None) -> Iterator:
    conn = conectar(url)
    try:
        yield conn
    finally:
        conn.close()


def consultar(conn, sql: str, params: Optional[dict] = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        return cur.fetchall()


def consultar_um(conn, sql: str, params: Optional[dict] = None) -> Optional[dict]:
    linhas = consultar(conn, sql, params)
    return linhas[0] if linhas else None


def aplicar_migracoes(conn, pasta: Optional[Path] = None) -> None:
    """Executa os sql/*.sql em ordem. Todos são idempotentes (IF NOT EXISTS), então
    bancos criados por versões anteriores ganham as tabelas novas sem perder dados."""
    pasta = pasta or Path(__file__).resolve().parents[2] / "sql"
    with conn.cursor() as cur:
        for arquivo in sorted(pasta.glob("*.sql")):
            cur.execute(arquivo.read_text(encoding="utf-8"))
    conn.commit()


def iniciar_uso_llm(conn, origem: str, id_perfume: Optional[int]) -> int:
    with conn.cursor() as cur:
        cur.execute(SQL_INICIAR_USO_LLM, {"origem": origem, "id_perfume": id_perfume})
        novo = cur.fetchone()["id"]
    conn.commit()
    return novo


def finalizar_uso_llm(conn, uso_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(SQL_FINALIZAR_USO_LLM, {"id": uso_id})
    conn.commit()


@contextmanager
def usando_llm(origem: str, id_perfume: Optional[int], url: Optional[str] = None):
    """Marca o uso do LLM durante o bloco, com conexão própria (sempre limpa ao sair)."""
    with conexao(url) as conn:
        uso = iniciar_uso_llm(conn, origem, id_perfume)
        try:
            yield
        finally:
            finalizar_uso_llm(conn, uso)


# Dados fictícios de demonstração (fonte 'exemplo', ids 900001-900003): saem de todas as tabelas.
_FICTICIOS = "SELECT id_perfume FROM produtos WHERE fonte = 'exemplo'"
SQL_REMOVER_EXEMPLO = [   # (tabela, filtro) na ordem que respeita as chaves estrangeiras
    ("analises", f"id_perfume IN ({_FICTICIOS})"),
    ("reviews", f"id_perfume IN ({_FICTICIOS})"),
    ("pedidos_coleta", f"id_perfume IN ({_FICTICIOS})"),
    ("produtos", "fonte = 'exemplo'"),
]


def remover_exemplo(conn) -> dict:
    """Apaga os perfumes fictícios e tudo ligado a eles. Devolve quantas linhas saíram de cada tabela."""
    apagados = {}
    for tabela, filtro in SQL_REMOVER_EXEMPLO:
        apagados[tabela] = consultar_um(conn, f"SELECT count(*) AS n FROM {tabela} WHERE {filtro}")["n"]
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {tabela} WHERE {filtro}")
    conn.commit()
    return apagados


def limpar_uso_llm(conn, origem: str) -> None:
    with conn.cursor() as cur:
        cur.execute(SQL_LIMPAR_USO_LLM, {"origem": origem})
    conn.commit()


def uso_llm_ativo(conn, origem: str) -> list[dict]:
    return consultar(conn, SQL_USO_LLM_ATIVO, {"origem": origem})


def produtos_para_coleta(conn, limite: int) -> list[dict]:
    return consultar(conn, SQL_PRODUTOS_PARA_COLETA, {"limite": limite})


def salvar_analise(conn, registro: dict) -> int:
    from psycopg.types.json import Jsonb

    params = dict(registro)
    params["resultado"] = Jsonb(params["resultado"])
    with conn.cursor() as cur:
        cur.execute(SQL_SALVAR_ANALISE, params)
        novo_id = cur.fetchone()["id"]
    conn.commit()
    return novo_id


def para_json(valor) -> dict:
    """JSONB pode voltar como dict (psycopg) ou str (outros drivers)."""
    return valor if isinstance(valor, dict) else json.loads(valor)
