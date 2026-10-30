"""Testa o AnthropicCliente com um SDK falso: forma da chamada (tool use forçado),
validação Pydantic e nova tentativa quando a saída vem fora do schema."""
import sys
import types
from types import SimpleNamespace

from conftest import lote

from fraginsights.llm.schemas import ExtracaoLote


class MensagensFalsas:
    def __init__(self, respostas):
        self.respostas, self.chamadas = list(respostas), []

    def create(self, **kwargs):
        self.chamadas.append(kwargs)
        entrada = self.respostas.pop(0)
        bloco = SimpleNamespace(type="tool_use", id=f"t{len(self.chamadas)}", input=entrada)
        return SimpleNamespace(content=[bloco], stop_reason="tool_use")


def _cliente(monkeypatch, respostas):
    msgs = MensagensFalsas(respostas)
    sdk = types.ModuleType("anthropic")
    sdk.Anthropic = lambda api_key=None, **_: SimpleNamespace(messages=msgs)
    monkeypatch.setitem(sys.modules, "anthropic", sdk)
    from fraginsights.llm.clientes import AnthropicCliente

    return AnthropicCliente(), msgs


def test_tool_use_forcado_e_validacao(monkeypatch):
    ok = lote((1, 1, "fixacao", "+"))
    cliente, msgs = _cliente(monkeypatch, [ok])
    saida = cliente.gerar(modelo="m", sistema="s", usuario="u", schema=ExtracaoLote)
    assert saida.mencoes[0].d == "fixacao" and saida.mencoes[0].p == "+"
    chamada = msgs.chamadas[0]
    assert chamada["tool_choice"] == {"type": "tool", "name": "registrar_resultado"}
    assert "$defs" not in str(chamada["tools"][0]["input_schema"])


def test_nova_tentativa_quando_schema_invalido(monkeypatch):
    ruim = lote((1, 1, "fixacao", "otima"))   # opção fora da lista
    bom = lote()
    cliente, msgs = _cliente(monkeypatch, [ruim, bom])
    saida = cliente.gerar(modelo="m", sistema="s", usuario="u", schema=ExtracaoLote)
    assert saida.mencoes == []
    assert len(msgs.chamadas) == 2
    retorno = msgs.chamadas[1]["messages"][-1]["content"][0]
    assert retorno["type"] == "tool_result" and retorno["is_error"] is True


def test_piso_de_tokens_na_api(monkeypatch):
    cliente, msgs = _cliente(monkeypatch, [lote()])
    cliente.gerar(modelo="m", sistema="s", usuario="u", schema=ExtracaoLote, max_tokens=500)
    assert msgs.chamadas[0]["max_tokens"] >= 8000  # o teto apertado é só para o modelo local


def test_analista_le_ao_menos_o_que_o_app_le():
    from fraginsights.config import Config
    from fraginsights.llm.analise import max_reviews_analista

    cfg = Config(analista_max_reviews=100, llm_max_reviews=200)
    assert max_reviews_analista(cfg, "ollama") == 100
    assert max_reviews_analista(cfg, "anthropic") == 200


def test_nao_envia_temperature(monkeypatch):
    cliente, msgs = _cliente(monkeypatch, [lote()])
    cliente.gerar(modelo="m", sistema="s", usuario="u", schema=ExtracaoLote)
    assert "temperature" not in msgs.chamadas[0]  # o SDK atual recusa o parâmetro
