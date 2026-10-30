"""Teste contra um PostgreSQL de verdade (pula se DATABASE_URL_TESTE não estiver definida).

    docker compose up -d postgres
    DATABASE_URL_TESTE=postgresql://fragrantica:fragrantica@localhost:5432/fragrantica pytest -k integracao

ATENÇÃO: o teste apaga as tabelas do banco indicado.
"""
import os

import pytest

from conftest import EXEMPLO, RAIZ

URL = os.getenv("DATABASE_URL_TESTE")
pytestmark = pytest.mark.skipif(not URL, reason="DATABASE_URL_TESTE não definida")


def test_fluxo_completo_no_postgres():
    from fraginsights import db
    from fraginsights.bus import MemoriaPublicador
    from fraginsights.config import Config
    from fraginsights.consumer import processar_lote
    from fraginsights.llm.analise import obter_ou_gerar
    from fraginsights.llm.clientes import DemoCliente
    from fraginsights.producer import carga_csv

    cfg = Config(database_url=URL)
    with db.conexao(URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP VIEW IF EXISTS vw_produtos; "
                        "DROP TABLE IF EXISTS analises, reviews, produtos, ingestao_log, pedidos_coleta, uso_llm")
        conn.commit()
        db.aplicar_migracoes(conn, RAIZ / "sql")
        db.aplicar_migracoes(conn, RAIZ / "sql")  # idempotente

        pub = MemoriaPublicador()
        carga_csv(pub, cfg, EXEMPLO, fonte="exemplo")
        assert processar_lote(conn, pub.mensagens, cfg).reviews_novas == 36
        assert processar_lote(conn, pub.mensagens, cfg).reviews_dup == 36

        assert db.produtos_para_coleta(conn, 10) == []  # dados fictícios não entram na coleta

        a1 = obter_ou_gerar(conn, 900002, DemoCliente(), cfg)
        a2 = obter_ou_gerar(conn, 900002, DemoCliente(), cfg)
        assert a1["id"] == a2["id"]  # cache
        assert db.para_json(a1["resultado"])["dimensoes"][0]["dimensao"] == "fixacao"

        # pedido sob demanda com o status chegando ANTES do pedido (tópicos sem ordem entre si)
        from fraginsights.bus import Mensagem, serializar
        from fraginsights.models import PedidoColeta, StatusPedido

        pedido = PedidoColeta(id_perfume=900002, link="https://x")
        fim = StatusPedido(id_pedido=pedido.id_pedido, id_perfume=900002, status="concluido",
                           reviews_coletadas=3, max_review_id=1024)
        coletando = StatusPedido(id_pedido=pedido.id_pedido, id_perfume=900002, status="coletando")
        processar_lote(conn, [Mensagem(cfg.topico_status, "1", serializar(fim.model_dump(mode="json")))], cfg)
        processar_lote(conn, [Mensagem(cfg.topico_pedidos, "1", serializar(pedido.model_dump(mode="json"))),
                              Mensagem(cfg.topico_status, "1", serializar(coletando.model_dump(mode="json")))], cfg)
        linha = db.consultar_um(conn, db.SQL_PEDIDO, {"id_pedido": pedido.id_pedido})
        assert linha["status"] == "concluido" and linha["reviews_coletadas"] == 3  # não regrediu
        assert linha["link"] == "https://x"


def test_fila_do_analista_e_uso_do_llm():
    from fraginsights import db
    from fraginsights.bus import MemoriaPublicador
    from fraginsights.config import Config
    from fraginsights.consumer import processar_lote
    from fraginsights.llm.analise import obter_ou_gerar
    from fraginsights.llm.clientes import DemoCliente, modelos_ativos
    from fraginsights.llm.prompts import versao
    from fraginsights.producer import carga_csv

    cfg = Config(database_url=URL)
    params = {"provedor": "demo", "modelo_sintese": modelos_ativos(cfg, "demo")[1],
              "versao_prompt": versao("detalhado"), "min_reviews": 5, "limite": 50}
    with db.conexao(URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP VIEW IF EXISTS vw_produtos; "
                        "DROP TABLE IF EXISTS analises, reviews, produtos, ingestao_log, pedidos_coleta, uso_llm")
        conn.commit()
        db.aplicar_migracoes(conn, RAIZ / "sql")
        pub = MemoriaPublicador()
        carga_csv(pub, cfg, EXEMPLO, fonte="exemplo")
        processar_lote(conn, pub.mensagens, cfg)

        assert db.consultar(conn, db.SQL_FILA_ANALISE, params) == []  # fictícios ficam fora
        with conn.cursor() as cur:
            cur.execute("UPDATE produtos SET fonte = 'scraper'")
        conn.commit()
        fila = [f["id_perfume"] for f in db.consultar(conn, db.SQL_FILA_ANALISE, params)]
        assert sorted(fila) == [900001, 900002, 900003]

        obter_ou_gerar(conn, 900002, DemoCliente(), cfg)
        fila = [f["id_perfume"] for f in db.consultar(conn, db.SQL_FILA_ANALISE, params)]
        assert 900002 not in fila  # já analisado
        # outro modelo não conta como analisado
        outro = dict(params, modelo_sintese="outro-modelo")
        assert 900002 in [f["id_perfume"] for f in db.consultar(conn, db.SQL_FILA_ANALISE, outro)]

        # review nova e informativa: volta para a fila
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO reviews (review_id, id_perfume, texto_original, texto_limpo,
                                                hash_conteudo, informativa, n_palavras, fonte, coletado_em)
                           SELECT 99999999, 900002, t, t, 'h-nova', true, 8, 'scraper', now()
                           FROM (SELECT 'Fixa o dia inteiro e recebo muitos elogios' AS t) x""")
        conn.commit()
        assert 900002 in [f["id_perfume"] for f in db.consultar(conn, db.SQL_FILA_ANALISE, params)]

        # marca de uso do modelo
        assert db.uso_llm_ativo(conn, "app") == []
        with db.usando_llm("app", 900002, URL):
            ativos = db.uso_llm_ativo(conn, "app")
            assert [a["id_perfume"] for a in ativos] == [900002]
            assert db.uso_llm_ativo(conn, "analista") == []
        assert db.uso_llm_ativo(conn, "app") == []
        db.iniciar_uso_llm(conn, "analista", 900001)          # sobra de um container interrompido
        db.limpar_uso_llm(conn, "analista")
        assert db.uso_llm_ativo(conn, "analista") == []
        assert db.consultar(conn, db.SQL_ANALISES_DIARIAS)[0]["analises"] == 1


def test_remover_exemplo_apaga_so_os_ficticios():
    from fraginsights import db
    from fraginsights.bus import MemoriaPublicador
    from fraginsights.config import Config
    from fraginsights.consumer import processar_lote
    from fraginsights.llm.analise import obter_ou_gerar
    from fraginsights.llm.clientes import DemoCliente
    from fraginsights.producer import carga_csv

    cfg = Config(database_url=URL)
    with db.conexao(URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP VIEW IF EXISTS vw_produtos; "
                        "DROP TABLE IF EXISTS analises, reviews, produtos, ingestao_log, pedidos_coleta, uso_llm")
        conn.commit()
        db.aplicar_migracoes(conn, RAIZ / "sql")
        pub = MemoriaPublicador()
        carga_csv(pub, cfg, EXEMPLO, fonte="exemplo")
        processar_lote(conn, pub.mensagens, cfg)
        obter_ou_gerar(conn, 900001, DemoCliente(), cfg)
        with conn.cursor() as cur:  # um perfume real que não pode ser apagado
            cur.execute("INSERT INTO produtos (id_perfume, designer, nome, fonte) VALUES (39681, 'Carolina Herrera', "
                        "'Good Girl', 'scraper')")
        conn.commit()

        apagados = db.remover_exemplo(conn)
        assert apagados["produtos"] == 3 and apagados["reviews"] == 36 and apagados["analises"] == 1 and apagados["pedidos_coleta"] == 0
        restantes = db.consultar(conn, "SELECT id_perfume FROM produtos")
        assert [r["id_perfume"] for r in restantes] == [39681]
