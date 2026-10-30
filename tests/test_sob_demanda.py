"""Fluxo híbrido: link de qualquer perfume, decisão de coletar, pedido pelo Kafka,
atendimento no coletor e detecção de que as reviews chegaram ao banco."""
from datetime import datetime, timedelta, timezone

from fraginsights.app.fluxo import estado_da_coleta, precisa_coletar, solicitar_coleta
from fraginsights.bus import MemoriaPublicador
from fraginsights.config import Config
from fraginsights.links import ler_link
from fraginsights.models import PedidoColeta, ProdutoEvento, ReviewEvento, StatusPedido
from fraginsights.producer import atender_pedido

AGORA = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
CFG = Config(min_reviews_analise=5, validade_coleta_dias=7, espera_coleta_s=600, llm_max_reviews=200)


def test_ler_link_de_qualquer_idioma_do_site():
    d = ler_link("https://www.fragrantica.com/perfume/Lattafa-Perfumes/Khamrah-75805.html?x=1")
    assert d == {"id_perfume": 75805, "designer": "Lattafa Perfumes", "nome": "Khamrah",
                 "link": "https://www.fragrantica.com.br/perfume/Lattafa-Perfumes/Khamrah-75805.html"}
    assert ler_link("fragrantica.com.br/perfume/Dior/Sauvage-31861.html")["id_perfume"] == 31861
    assert ler_link("https://www.google.com/perfume/Dior/Sauvage-31861.html") is None
    assert ler_link("https://www.fragrantica.com.br/desenhista/Dior.html") is None
    assert ler_link("") is None


def test_quando_coletar_antes_de_analisar():
    base = {"link": "x", "n_reviews": 50, "n_informativas": 40, "ultima_coleta": AGORA - timedelta(days=2)}
    assert precisa_coletar(None, AGORA, CFG)[0]                                        # não está no banco
    assert precisa_coletar({**base, "n_reviews": 0, "n_informativas": 0}, AGORA, CFG)[0]  # só catálogo
    assert precisa_coletar({**base, "ultima_coleta": AGORA - timedelta(days=30)}, AGORA, CFG)[0]  # velho
    assert not precisa_coletar(base, AGORA, CFG)[0]                                    # fresco
    assert not precisa_coletar({**base, "ultima_coleta": None}, AGORA, CFG)[0]         # veio do CSV
    assert not precisa_coletar({**base, "fonte": "exemplo", "n_informativas": 0}, AGORA, CFG)[0]


def test_app_publica_pedido_no_topico_de_pedidos():
    pub = MemoriaPublicador()
    pedido = solicitar_coleta(pub, CFG, id_perfume=1, link="https://x", nome="N")
    assert [m.topico for m in pub.mensagens] == [CFG.topico_pedidos]
    assert PedidoColeta.model_validate_json(pub.mensagens[0].valor).id_pedido == pedido.id_pedido


class NavFalso:
    def __init__(self, erro=None):
        self.erro, self.chamadas = erro, []

    def coletar(self, produto, max_reviews=None):
        self.chamadas.append((produto, max_reviews))
        if self.erro:
            raise self.erro
        ev = ProdutoEvento(tipo="detalhes", id_perfume=produto["id_perfume"], designer=produto["designer"],
                           nome=produto["nome"], link=produto["link"], fonte="scraper")
        revs = [ReviewEvento(review_id=i, id_perfume=produto["id_perfume"], texto=f"r{i}", fonte="scraper")
                for i in (101, 102)]
        return ev, revs


def _status(pub):
    return [StatusPedido.model_validate_json(m.valor).status for m in pub.mensagens if m.topico == CFG.topico_status]


def test_coletor_atende_pedido_com_marca_d_agua_e_limite():
    pub, nav = MemoriaPublicador(), NavFalso()
    pedido = PedidoColeta(id_perfume=7, link="https://l", designer="D", nome="N")
    final = atender_pedido(nav, pedido, pub, CFG, {"link": "https://l", "designer": "D", "nome": "N",
                                                   "max_review_id": 100})
    produto, limite = nav.chamadas[0]
    assert produto["max_review_id"] == 100 and limite == 200
    assert final.status == "concluido" and final.reviews_coletadas == 2 and final.max_review_id == 102
    topicos = [m.topico for m in pub.mensagens]
    # status "coletando" -> reviews -> ficha -> status "concluido"
    assert topicos == [CFG.topico_status, CFG.topico_reviews, CFG.topico_reviews,
                       CFG.topico_produtos, CFG.topico_status]
    assert _status(pub) == ["coletando", "concluido"]


def test_pedido_de_perfume_fora_do_banco_e_falhas():
    pub, nav = MemoriaPublicador(), NavFalso()
    atender_pedido(nav, PedidoColeta(id_perfume=8, link="https://l", designer="D", nome="N"), pub, CFG, None)
    assert nav.chamadas[0][0]["max_review_id"] is None  # primeira coleta: sem marca d'água

    pub = MemoriaPublicador()
    assert atender_pedido(NavFalso(RuntimeError("Cloudflare")), PedidoColeta(id_perfume=8, link="l"),
                          pub, CFG, None).status == "erro"

    pub, nav = MemoriaPublicador(), NavFalso()
    velho = PedidoColeta(id_perfume=8, link="l", solicitado_em=datetime.now(timezone.utc) - timedelta(hours=2))
    assert atender_pedido(nav, velho, pub, CFG, None).status == "erro" and not nav.chamadas


def test_app_so_libera_quando_ficha_e_reviews_chegaram():
    pedido = {"status": "concluido", "solicitado_em": AGORA, "max_review_id": 102, "reviews_coletadas": 2}
    antes = {"ultima_coleta": AGORA - timedelta(days=9), "max_review_id": 100}
    so_ficha = {"ultima_coleta": AGORA + timedelta(seconds=40), "max_review_id": 100}
    tudo = {"ultima_coleta": AGORA + timedelta(seconds=40), "max_review_id": 102}
    assert estado_da_coleta(None, None)[0] == "aguardando"
    assert estado_da_coleta({"status": "coletando"}, antes)[0] == "aguardando"
    assert estado_da_coleta(pedido, antes)[0] == "aguardando"
    assert estado_da_coleta(pedido, so_ficha)[0] == "aguardando"
    assert estado_da_coleta(pedido, tudo)[0] == "pronto"
    assert estado_da_coleta({**pedido, "max_review_id": None}, so_ficha)[0] == "pronto"  # nada novo no site
    assert estado_da_coleta({"status": "erro", "mensagem": "bloqueado"}, None) == ("erro", "bloqueado")
