"""OllamaCliente contra um servidor HTTP falso que imita a API /api/chat do Ollama."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import lote

from fraginsights.llm.clientes import OllamaCliente, modelos_ativos
from fraginsights.llm.schemas import ExtracaoLote


def _servidor(respostas, recebidos, status=200):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            recebidos.append(corpo)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            conteudo = respostas.pop(0) if respostas else "{}"
            self.wfile.write(json.dumps({"message": {"role": "assistant", "content": conteudo}}).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_saida_estruturada_contexto_e_nova_tentativa():
    ruim = json.dumps(lote((1, 1, "projecao", "forte")))
    bom = json.dumps(lote((1, 1, "fixacao", "+")))
    recebidos = []
    srv = _servidor([ruim, bom], recebidos)
    try:
        cliente = OllamaCliente(url=f"http://127.0.0.1:{srv.server_port}", num_ctx=8192)
        saida = cliente.gerar(modelo="qwen2.5:7b", sistema="s", usuario="u", schema=ExtracaoLote)
    finally:
        srv.shutdown()
    assert saida.mencoes[0].d == "fixacao" and saida.mencoes[0].p == "+"
    assert len(recebidos) == 2
    primeiro = recebidos[0]
    assert primeiro["model"] == "qwen2.5:7b" and primeiro["stream"] is False
    assert primeiro["options"]["num_ctx"] == 8192
    assert "$ref" not in json.dumps(primeiro["format"])
    assert "schema" in recebidos[1]["messages"][-1]["content"]  # o erro volta para o modelo


def test_modelo_nao_baixado_da_mensagem_clara():
    srv = _servidor([], [], status=404)
    try:
        with pytest.raises(RuntimeError, match="ollama pull qwen2.5:7b"):
            OllamaCliente(url=f"http://127.0.0.1:{srv.server_port}").gerar(
                modelo="qwen2.5:7b", sistema="s", usuario="u", schema=ExtracaoLote)
    finally:
        srv.shutdown()


def test_ollama_fora_do_ar():
    with pytest.raises(RuntimeError, match="Ele está aberto"):
        OllamaCliente(url="http://127.0.0.1:9", timeout_s=2).gerar(
            modelo="m", sistema="s", usuario="u", schema=ExtracaoLote)


def test_modelos_por_provedor():
    from fraginsights.config import Config

    cfg = Config(ollama_modelo="llama3.1:8b")
    assert modelos_ativos(cfg, "ollama") == ("llama3.1:8b", "llama3.1:8b")
    assert modelos_ativos(cfg, "demo") == ("heuristica", "heuristica")
    assert modelos_ativos(cfg, "anthropic") == (cfg.llm_modelo_extracao, cfg.llm_modelo_sintese)


def test_modelos_por_modo():
    from fraginsights.config import Config
    from fraginsights.llm.clientes import modelos_do_modo

    cfg = Config(ollama_modelo="qwen2.5:3b", ollama_modelo_sintese="qwen2.5:7b")
    assert modelos_do_modo(cfg, "ollama", "detalhado") == ("qwen2.5:3b", "qwen2.5:7b")
    assert modelos_do_modo(cfg, "ollama", "rapido") == ("qwen2.5:3b", "qwen2.5:3b")  # local: o rápido
    assert modelos_do_modo(cfg, "anthropic", "rapido") == (cfg.llm_modelo_sintese,) * 2


def test_limites_mais_leves_para_modelo_local():
    from fraginsights.config import Config
    from fraginsights.llm.clientes import limites

    cfg = Config()
    assert limites(cfg, "ollama") == (40, 10, 1)
    assert limites(cfg, "anthropic") == (100, 40, 4)


def test_verificar_modelo_baixado():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"models": [{"name": "qwen2.5:7b"}]}).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cliente = OllamaCliente(url=f"http://127.0.0.1:{srv.server_port}")
        assert cliente.verificar("qwen2.5:7b") is None
        assert "ollama pull llama3.1:8b" in cliente.verificar("llama3.1:8b")
    finally:
        srv.shutdown()
    assert "não respondeu" in OllamaCliente(url="http://127.0.0.1:9").verificar("x")


def test_teto_de_saida_e_timeout_claro():
    import time as _t

    from fraginsights.llm.analise import tokens_extracao

    assert tokens_extracao(10) == 1500 and tokens_extracao(40) == 5100

    class Lento(BaseHTTPRequestHandler):
        def do_POST(self):
            _t.sleep(3)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Lento)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(RuntimeError, match="modelo menor"):
            OllamaCliente(url=f"http://127.0.0.1:{srv.server_port}", timeout_s=1).gerar(
                modelo="m", sistema="s", usuario="u", schema=ExtracaoLote, max_tokens=100)
    finally:
        srv.shutdown()
