from datetime import datetime, timedelta

from fraginsights.analista import ClienteQueCede, dentro_da_janela, escolher, permitido
from fraginsights.config import Config


def test_janela_vazia_e_sempre():
    assert dentro_da_janela(datetime(2026, 1, 1, 15), "", "")


def test_janela_no_mesmo_dia():
    assert dentro_da_janela(datetime(2026, 1, 1, 10), "08:00", "18:00")
    assert not dentro_da_janela(datetime(2026, 1, 1, 19), "08:00", "18:00")


def test_janela_que_vira_a_noite():
    assert dentro_da_janela(datetime(2026, 1, 1, 23, 30), "22:00", "07:00")
    assert dentro_da_janela(datetime(2026, 1, 1, 3), "22:00", "07:00")
    assert not dentro_da_janela(datetime(2026, 1, 1, 12), "22:00", "07:00")
    assert not dentro_da_janela(datetime(2026, 1, 1, 7), "22:00", "07:00")


def test_provedor_pago_so_com_autorizacao():
    assert permitido(Config(analista_permitir_pago=False), "anthropic")
    assert permitido(Config(analista_permitir_pago=True), "anthropic") is None
    assert permitido(Config(), "ollama") is None
    assert permitido(Config(analista_max_por_dia=0), "ollama")


class _Cliente:
    provedor = "ollama"

    def __init__(self):
        self.chamadas = 0

    def gerar(self, **kwargs):
        self.chamadas += 1
        return "ok"


def test_cliente_espera_o_app_terminar():
    ocupado = iter([True, True, False])
    dormidas = []
    base = _Cliente()
    cliente = ClienteQueCede(base, lambda: next(ocupado), dormir=dormidas.append, pausa_s=5)
    assert cliente.provedor == "ollama"
    assert cliente.gerar(modelo="m") == "ok"
    assert dormidas == [5, 5] and base.chamadas == 1


def test_cliente_nao_espera_com_app_livre():
    base = _Cliente()
    cliente = ClienteQueCede(base, lambda: False, dormir=lambda s: (_ for _ in ()).throw(AssertionError))
    cliente.gerar(modelo="m")
    assert base.chamadas == 1


def test_pula_quem_falhou_recentemente():
    agora = datetime(2026, 1, 2, 12)
    fila = [{"id_perfume": 1}, {"id_perfume": 2}]
    assert escolher(fila, {1: agora - timedelta(hours=1)}, agora)["id_perfume"] == 2
    assert escolher(fila, {1: agora - timedelta(hours=30)}, agora)["id_perfume"] == 1
    assert escolher(fila, {1: agora, 2: agora}, agora) is None
