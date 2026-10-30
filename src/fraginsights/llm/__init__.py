"""Camada de GenAI: taxonomia, prompts, provedores e análise por produto."""
from .clientes import (AnthropicCliente, ClienteLLM, DemoCliente, OllamaCliente,  # noqa: F401
                       criar_cliente, modelos_ativos)
