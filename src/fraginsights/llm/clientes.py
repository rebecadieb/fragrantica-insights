"""Provedores de LLM atrás de uma interface única (agnóstico de fornecedor).

- AnthropicCliente: Claude via API, saída estruturada por *tool use* forçado
  (o modelo é obrigado a "chamar" uma ferramenta cujo input_schema é o JSON Schema
  do modelo Pydantic) + validação Pydantic com uma nova tentativa em caso de erro.
- OllamaCliente: modelo open source rodando localmente no Ollama (ex.: Qwen 2.5),
  sem custo e sem chave. Saída estruturada pelo parâmetro `format` (JSON Schema)
  + a mesma validação Pydantic com nova tentativa.
- DemoCliente: heurístico, sem rede e sem custo. Serve para testes, para rodar o
  app sem LLM e como linha de base na validação. NÃO é um LLM.

Para outro provedor (OpenAI...), implemente `gerar` com a mesma assinatura.
"""
from __future__ import annotations

import html
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .schemas import (AnaliseRapida, AvaliacaoDimensao, DimensaoRapida, ExtracaoLote, MencaoLote, PerfilCliente,
                      SinteseProduto)
from .taxonomia import CHAVES, POR_CHAVE

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class ClienteLLM(Protocol):
    provedor: str

    def gerar(self, *, modelo: str, sistema: str, usuario: str, schema: type[T],
              max_tokens: int = 16000) -> T: ...


# ---------------------------------------------------------------------------
# Anthropic (Claude)
# ---------------------------------------------------------------------------

def schema_sem_refs(schema: type[BaseModel]) -> dict:
    """JSON Schema do Pydantic com os $ref resolvidos (input_schema mais simples para o modelo)."""
    bruto = schema.model_json_schema()
    defs = bruto.pop("$defs", {})

    def resolver(no):
        if isinstance(no, dict):
            if "$ref" in no:
                return resolver(defs[no["$ref"].split("/")[-1]])
            return {k: resolver(v) for k, v in no.items() if k != "title"}
        if isinstance(no, list):
            return [resolver(v) for v in no]
        return no

    return resolver(bruto)


PISO_TOKENS_API = 8000


class AnthropicCliente:
    provedor = "anthropic"

    def __init__(self, api_key: str | None = None):
        import anthropic

        # max_retries: contas novas têm limite baixo de requisições por minuto; o SDK espera e
        # tenta de novo sozinho quando a API responde 429 (devagar) ou 529 (sobrecarregada).
        self._cliente = anthropic.Anthropic(api_key=api_key, max_retries=6)  # usa ANTHROPIC_API_KEY se None

    def gerar(self, *, modelo: str, sistema: str, usuario: str, schema: type[T],
              max_tokens: int = 16000) -> T:
        ferramenta = {
            "name": "registrar_resultado",
            "description": "Registra o resultado estruturado da análise.",
            "input_schema": schema_sem_refs(schema),
        }
        # Os tetos de saída do projeto existem para um modelo local não "empacar"; na API eles só
        # cortariam a resposta no meio do JSON. Aqui vale um piso folgado.
        max_tokens = max(max_tokens, PISO_TOKENS_API)
        mensagens = [{"role": "user", "content": usuario}]
        for tentativa in (1, 2):
            resposta = self._cliente.messages.create(
                # Sem `temperature`: as versões recentes do SDK não aceitam mais esse parâmetro.
                model=modelo, max_tokens=max_tokens,
                system=sistema, messages=mensagens, tools=[ferramenta],
                tool_choice={"type": "tool", "name": "registrar_resultado"},
            )
            if resposta.stop_reason == "max_tokens":
                raise RuntimeError(f"A resposta do modelo passou de {max_tokens} tokens e foi cortada.")
            bloco = next((b for b in resposta.content if b.type == "tool_use"), None)
            if bloco is None:
                raise RuntimeError(f"Resposta sem tool_use (stop_reason={resposta.stop_reason})")
            try:
                return schema.model_validate(bloco.input)
            except ValidationError as erro:
                if tentativa == 2:
                    raise
                log.warning("Saída inválida do modelo, nova tentativa: %s", str(erro)[:300])
                mensagens += [
                    {"role": "assistant", "content": resposta.content},
                    {"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": bloco.id, "is_error": True,
                        "content": f"O resultado não segue o schema: {erro}. Envie novamente corrigido.",
                    }]},
                ]
        raise RuntimeError("inalcançável")


# ---------------------------------------------------------------------------
# Ollama (modelo open source local)
# ---------------------------------------------------------------------------

class OllamaCliente:
    """Chama a API local do Ollama (POST /api/chat) com saída restrita a um JSON Schema.

    Sem dependências extras: usa urllib. `num_ctx` precisa ser alto o bastante para
    o lote de reviews caber no contexto (o padrão do Ollama é pequeno e truncaria)."""

    provedor = "ollama"

    def __init__(self, url: str = "http://localhost:11434", num_ctx: int = 8192,
                 temperatura: float = 0.1, timeout_s: int = 600):
        self._url = url.rstrip("/")
        self._num_ctx = num_ctx
        self._temperatura = temperatura
        self._timeout = timeout_s

    def _chat(self, corpo: dict) -> dict:
        req = urllib.request.Request(f"{self._url}/api/chat", data=json.dumps(corpo).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        inicio = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                dados = json.loads(resp.read().decode("utf-8"))
            log.info("Ollama (%s): resposta em %.0f s, %s tokens gerados", corpo["model"],
                     time.time() - inicio, dados.get("eval_count", "?"))
            return dados
        except (TimeoutError, socket.timeout):
            raise RuntimeError(
                f"O modelo local levou mais de {self._timeout // 60} min para responder. A máquina pode estar "
                "sem memória livre: feche outros programas ou use um modelo menor "
                "(OLLAMA_MODELO=qwen2.5:3b e `ollama pull qwen2.5:3b`).") from None
        except urllib.error.HTTPError as erro:
            detalhe = erro.read().decode("utf-8", "replace")[:300]
            if erro.code == 404:
                raise RuntimeError(f"Modelo não encontrado no Ollama. Rode `ollama pull {corpo['model']}`. ({detalhe})")
            raise RuntimeError(f"Ollama respondeu {erro.code}: {detalhe}") from erro
        except urllib.error.URLError as erro:
            if isinstance(erro.reason, (TimeoutError, socket.timeout)):
                raise RuntimeError(f"O modelo local levou mais de {self._timeout // 60} min para responder. "
                                   "Use um modelo menor (OLLAMA_MODELO=qwen2.5:3b).") from None
            raise RuntimeError(f"Não foi possível falar com o Ollama em {self._url}. Ele está aberto? ({erro.reason})")

    def verificar(self, modelo: str) -> str | None:
        """None se o Ollama responde e o modelo está baixado; senão, o que fazer."""
        try:
            with urllib.request.urlopen(f"{self._url}/api/tags", timeout=3) as resp:
                nomes = {m.get("name", "") for m in json.loads(resp.read().decode("utf-8")).get("models", [])}
        except Exception:
            return f"O Ollama não respondeu em {self._url}. Abra o app do Ollama na sua máquina."
        if modelo not in nomes and f"{modelo}:latest" not in nomes:
            return f"O modelo {modelo} ainda não foi baixado. No terminal: ollama pull {modelo}"
        return None

    def gerar(self, *, modelo: str, sistema: str, usuario: str, schema: type[T],
              max_tokens: int = 16000) -> T:
        mensagens = [
            {"role": "system", "content": sistema},
            {"role": "user", "content": usuario + "\n\nResponda somente com o JSON pedido, em português."},
        ]
        for tentativa in (1, 2):
            resposta = self._chat({
                "model": modelo, "messages": mensagens, "stream": False, "keep_alive": "30m",
                "format": schema_sem_refs(schema),
                "options": {"temperature": self._temperatura, "num_ctx": self._num_ctx,
                            "num_predict": max_tokens},
            })
            conteudo = resposta.get("message", {}).get("content", "")
            try:
                return schema.model_validate_json(conteudo)
            except ValidationError as erro:
                if tentativa == 2:
                    raise
                log.warning("Saída inválida do modelo local, nova tentativa: %s", str(erro)[:300])
                mensagens += [
                    {"role": "assistant", "content": conteudo},
                    {"role": "user", "content": f"O JSON não segue o schema: {erro}. Envie novamente corrigido."},
                ]
        raise RuntimeError("inalcançável")


# ---------------------------------------------------------------------------
# Demonstração (heurística, sem LLM)
# ---------------------------------------------------------------------------

_LEXICO = {
    "fixacao": r"fixa|fixação|dura|duração|durabilidade|horas|longa duração|evapora|some r[aá]pido",
    "projecao": r"proje|rastro|sillage|espalha|colado na pele|perto da pele|discreto",
    "cheiro": r"cheiro|cheiroso|perfume lindo|maravilhos|delicios|enjoativ|sint[eé]tico|horr[ií]vel|gostoso",
    "versatilidade": r"vers[aá]til|qualquer ocasi|dia a dia|trabalho|noite|ver[aã]o|inverno|calor|frio",
    "originalidade": r"[uú]nico|original|diferente|clone|parecido|lembra o|gen[eé]rico|igual ao",
    "custo_beneficio": r"pre[cç]o|barato|caro|custo|vale (a pena|cada)|rende|centavo",
    "elogios": r"elogi|comentam|perguntam|notaram|ningu[eé]m (nota|percebe|comenta)",
}
_NEGATIVO = re.compile(r"\b(n[aã]o|nunca|pouc[ao]|fraca?|baixa|ruim|caro|some|evapora|enjoativ|"
                       r"sint[eé]tic|horr[ií]vel|decepcion|gen[eé]rico|clone|discreto|perto da pele)", re.I)
_POSITIVO = re.compile(r"\b(ótim|otim|excelente|maravilh|incr[ií]vel|forte|longa|muito bom|boa|"
                       r"delicios|lind|vale|perfeit|elogi|amo|adoro|top|bomba|original|[uú]nic|diferente|vers[aá]til)", re.I)
_FRASE = re.compile(r"^\[(\d+)\.(\d+)\] (.+)$", re.M)
_PERFIS = re.compile(r"(para quem [^.,;!?]{3,60}?(?= [ée] |[.,;!?]|$)|uso (?:noturno|diurno|di[aá]rio)|"
                     r"(?:no |para o )?(?:inverno|ver[aã]o|trabalho|balada|encontros?))", re.I)


class DemoCliente:
    provedor = "demo"

    def gerar(self, *, modelo: str, sistema: str, usuario: str, schema: type[T],
              max_tokens: int = 16000) -> T:
        if schema is ExtracaoLote:
            return self._extrair(usuario)  # type: ignore[return-value]
        if schema is SinteseProduto:
            return self._sintetizar(usuario)  # type: ignore[return-value]
        if schema is AnaliseRapida:
            return self._rapido(usuario)  # type: ignore[return-value]
        raise NotImplementedError(schema.__name__)

    @staticmethod
    def _classificar(usuario: str):
        """(review, frase, dimensão, sinal) por palavras-chave, frase a frase."""
        for r, f, texto in _FRASE.findall(usuario):
            texto = html.unescape(texto)
            for chave, padrao in _LEXICO.items():
                if re.search(padrao, texto, re.I):
                    neg, pos = bool(_NEGATIVO.search(texto)), bool(_POSITIVO.search(texto))
                    yield int(r), int(f), chave, ("-" if neg and not pos else "+" if pos else "0")

    def _extrair(self, usuario: str) -> ExtracaoLote:
        mencoes = [MencaoLote(r=r, f=f, d=d, p=p) for r, f, d, p in self._classificar(usuario)]
        achados = [m.strip().lower() for m in _PERFIS.findall(html.unescape(usuario))]
        return ExtracaoLote(mencoes=mencoes, perfis=[p for p in achados if p.startswith("para quem")][:5],
                            ocasioes=[p for p in achados if not p.startswith("para quem")][:5])

    def _rapido(self, usuario: str) -> AnaliseRapida:
        por_dim: dict[str, dict] = {c: {"+": [], "-": [], "0": [], "ev": []} for c in CHAVES}
        for r, f, d, p in self._classificar(usuario):
            if r not in por_dim[d][p]:
                por_dim[d][p].append(r)
            por_dim[d]["ev"].append(f"{r}.{f}")
        dims = []
        for c in CHAVES:
            d = por_dim[c]
            total = len(d["+"]) + len(d["-"]) + len(d["0"])
            nota = round(10 * (len(d["+"]) + 0.5 * len(d["0"])) / total, 1) if total else None
            dims.append(DimensaoRapida(dimensao=c, positivas=d["+"], negativas=d["-"], neutras=d["0"],
                                       evidencias=d["ev"][:3], nota=nota,
                                       resumo=f"{len(d['+'])} reviews elogiam e {len(d['-'])} criticam."))
        return AnaliseRapida(dimensoes=dims, perfil_cliente=PerfilCliente(
            descricao="Modo demonstração: classificação por palavras-chave, sem LLM."),
            resumo_geral="Resumo heurístico (modo demonstração, sem LLM).")

    def _sintetizar(self, usuario: str) -> SinteseProduto:
        material = json.loads(re.search(r"<material>\n(.*)\n</material>", usuario, re.S).group(1))
        dims = material["dimensoes"]
        avaliacoes, fortes, fracos = [], [], []
        for chave in CHAVES:
            d = dims[chave]
            nome = POR_CHAVE[chave].nome
            if not d["mencoes"]:
                avaliacoes.append(AvaliacaoDimensao(dimensao=chave, nota=None,
                                                    resumo=f"As reviews analisadas não comentam {nome.lower()}."))
                continue
            nota = d["indice_calculado"]
            avaliacoes.append(AvaliacaoDimensao(
                dimensao=chave, nota=nota,
                resumo=(f"{d['positivas']} menções positivas, {d['negativas']} negativas e "
                        f"{d['neutras']} neutras em {d['mencoes']} reviews.")))
            if nota is not None and d["mencoes"] >= 2:
                (fortes if nota >= 7 else fracos if nota <= 4 else []).append(nome)
        ficha = material["ficha_tecnica"]
        perfis = material.get("perfis_citados", [])
        ocasioes = material.get("ocasioes_citadas", [])
        return SinteseProduto(
            resumo_geral=(f"Resumo heurístico (modo demonstração, sem LLM) de {ficha['nome']}, "
                          f"com base em {material['reviews_analisadas']} reviews."),
            dimensoes=avaliacoes,
            perfil_cliente=PerfilCliente(
                descricao=("Modo demonstração: o perfil abaixo junta as expressões mais citadas nas reviews. "
                           "Configure LLM_PROVEDOR=anthropic para um texto gerado por IA."),
                indicado_para=perfis[:5], ocasioes=ocasioes[:5]),
            pontos_fortes=fortes, pontos_fracos=fracos,
        )


def criar_cliente(provedor: str, cfg=None) -> ClienteLLM:
    provedor = (provedor or "").lower()
    if provedor == "anthropic":
        return AnthropicCliente()
    if provedor == "ollama":
        from ..config import carregar

        cfg = cfg or carregar()
        return OllamaCliente(url=cfg.ollama_url, num_ctx=cfg.ollama_num_ctx, timeout_s=cfg.ollama_timeout_s)
    if provedor in {"demo", "mock"}:
        return DemoCliente()
    raise ValueError(f"LLM_PROVEDOR desconhecido: {provedor!r} (use 'anthropic', 'ollama' ou 'demo')")


def modelos_do_modo(cfg, provedor: str, modo: str) -> tuple[str, str]:
    """No modo rápido (uma chamada) o modelo local é o de extração, que é o rápido; com a API,
    o de síntese, que escreve melhor e responde em segundos de qualquer jeito."""
    extracao, sintese = modelos_ativos(cfg, provedor)
    if modo == "rapido":
        unico = extracao if provedor == "ollama" else sintese
        return unico, unico
    return extracao, sintese


def limites(cfg, provedor: str) -> tuple[int, int, int]:
    """(máx. de reviews lidas, reviews por lote, chamadas em paralelo) para o provedor."""
    if provedor == "ollama":
        return cfg.ollama_max_reviews, cfg.ollama_reviews_por_lote, cfg.ollama_paralelo
    return cfg.llm_max_reviews, cfg.llm_reviews_por_lote, cfg.llm_paralelo


def modelos_ativos(cfg, provedor: str) -> tuple[str, str]:
    """(modelo do estágio 1, modelo do estágio 2) para o provedor em uso."""
    if provedor == "ollama":
        return cfg.ollama_modelo, (cfg.ollama_modelo_sintese or cfg.ollama_modelo)
    if provedor in {"demo", "mock"}:
        return "heuristica", "heuristica"
    return cfg.llm_modelo_extracao, cfg.llm_modelo_sintese
