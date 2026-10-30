"""Producer (carga CSV) -> barramento em memória -> consumer, com um banco falso
que reproduz as regras de conflito do PostgreSQL. O teste com banco real fica em
test_integracao_postgres.py."""
from fraginsights import db
from fraginsights.bus import MemoriaPublicador, Mensagem
from fraginsights.config import Config
from fraginsights.consumer import processar_lote
from fraginsights.producer import carga_csv, proxima_execucao

from conftest import EXEMPLO


class BancoFalso:
    def __init__(self):
        self.produtos, self.reviews, self.hashes, self.logs = {}, {}, set(), []
        self.commits = 0

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def execute(self, sql, p):
        self._linha = None
        if sql is db.SQL_UPSERT_PRODUTO:
            atual = self.produtos.get(p["id_perfume"], {})
            self.produtos[p["id_perfume"]] = {**atual, **{k: v for k, v in p.items() if v not in (None, [])}}
        elif sql is db.SQL_PRODUTO_STUB:
            self.produtos.setdefault(p["id_perfume"], dict(p))
        elif sql is db.SQL_INSERIR_REVIEW:
            assert p["id_perfume"] in self.produtos, "review gravada antes do produto"
            if p["review_id"] in self.reviews or p["hash_conteudo"] in self.hashes:
                return
            self.reviews[p["review_id"]] = p
            self.hashes.add(p["hash_conteudo"])
            self._linha = {"review_id": p["review_id"]}
        elif sql is db.SQL_LOG_INGESTAO:
            self.logs.append(p)

    def fetchone(self):
        return self._linha

    def commit(self):
        self.commits += 1


def _carregar():
    cfg = Config()
    pub = MemoriaPublicador()
    contagem = carga_csv(pub, cfg, EXEMPLO, fonte="exemplo")
    return cfg, pub, contagem


def test_carga_csv_publica_produtos_e_reviews_com_chave_do_produto():
    cfg, pub, contagem = _carregar()
    assert contagem[cfg.topico_produtos] == 6      # 3 de catálogo + 3 de detalhes
    assert contagem[cfg.topico_reviews] == 36
    assert all(m.chave.isdigit() for m in pub.mensagens)


def test_consumer_e_idempotente_mesmo_com_reviews_antes_dos_produtos():
    cfg, pub, _ = _carregar()
    banco = BancoFalso()
    est1 = processar_lote(banco, list(reversed(pub.mensagens)), cfg)
    est2 = processar_lote(banco, pub.mensagens, cfg)  # reentrega (at-least-once)
    assert est1.reviews_novas == 36 and est1.reviews_dup == 0
    assert est2.reviews_novas == 0 and est2.reviews_dup == 36
    assert len(banco.reviews) == 36 and len(banco.produtos) == 3
    assert banco.commits == 2 and len(banco.logs) == 2


def test_mensagem_invalida_vai_para_dlq_sem_derrubar_o_lote():
    cfg, pub, _ = _carregar()
    lixo = Mensagem(cfg.topico_reviews, "1", b'{"review_id": "x"}')
    est = processar_lote(BancoFalso(), pub.mensagens[:5] + [lixo], cfg)
    assert est.rejeitadas == 1 and len(est.dlq) == 1
    assert est.produtos == 5


def test_agendamento_diario():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    fuso = ZoneInfo("America/Fortaleza")
    antes = datetime(2026, 10, 29, 2, 0, tzinfo=fuso)
    depois = datetime(2026, 10, 29, 4, 0, tzinfo=fuso)
    assert proxima_execucao(antes, "03:00") == datetime(2026, 10, 29, 3, 0, tzinfo=fuso)
    assert proxima_execucao(depois, "03:00") == datetime(2026, 10, 30, 3, 0, tzinfo=fuso)


def test_catalogo_do_scraper_nao_conta_como_visita_mas_a_ficha_sim():
    from datetime import datetime, timezone

    from fraginsights.consumer import _produto_params
    from fraginsights.models import ProdutoEvento

    quando = datetime(2026, 10, 30, tzinfo=timezone.utc)
    cat = ProdutoEvento(tipo="catalogo", id_perfume=1, designer="D", nome="N", fonte="scraper", coletado_em=quando)
    ficha = ProdutoEvento(tipo="detalhes", id_perfume=1, designer="D", nome="N", fonte="scraper", coletado_em=quando)
    assert _produto_params(cat)["ultima_coleta"] is None
    assert _produto_params(ficha)["ultima_coleta"] == quando
