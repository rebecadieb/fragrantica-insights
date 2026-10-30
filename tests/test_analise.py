import re

import pytest

from conftest import lote
from fraginsights.config import Config
from fraginsights.llm.analise import (Mencao, nota_final, agregar, analisar, completar_sintese, indice, ler_lote, ler_rapido,
                                      orcamento_rapido, preparar_para, selecionar_reviews)
from fraginsights.llm.clientes import DemoCliente, schema_sem_refs
from fraginsights.llm.frases import dividir, formatar, ler_id_frase, preparar, relevante
from fraginsights.llm.prompts import SISTEMA_EXTRACAO, versao
from fraginsights.llm.schemas import AnaliseRapida, ExtracaoLote, SinteseProduto
from fraginsights.llm.taxonomia import CHAVES, confianca

REVIEWS = [
    {"review_id": 10, "texto_limpo": "A fixação é excelente, dura o dia todo. Sempre me elogiam.", "informativa": True},
    {"review_id": 11, "texto_limpo": "Projeção fraca, fica colado na pele. Cheiro delicioso.", "informativa": True},
    {"review_id": 12, "texto_limpo": "Fixação fraca, some em 2 horas.", "informativa": True},
    {"review_id": 13, "texto_limpo": "Caro, mas vale a pena. Para quem gosta de doces.", "informativa": True},
    {"review_id": 14, "texto_limpo": "Uso no trabalho e à noite, muito versátil.", "informativa": True},
    {"review_id": 9, "texto_limpo": "ok", "informativa": False},
]
PRODUTO = {"id_perfume": 1, "nome": "Teste", "designer": "Marca"}


# --- frases -------------------------------------------------------------------

def test_dividir_junta_pedacos_curtos():
    assert dividir("Amei! Fixa o dia todo na pele. Ganhei da minha mãe.") == [
        "Amei! Fixa o dia todo na pele.", "Ganhei da minha mãe."]
    assert dividir("linha um com texto\nlinha dois com texto") == ["linha um com texto", "linha dois com texto"]


def test_filtro_tira_so_frases_sem_pista():
    assert relevante("Fixa umas 8h na pele")
    assert relevante("Meu marido sempre comenta")
    assert not relevante("Comprei na loja do shopping semana passada.")


def test_frases_numeradas_e_filtradas():
    prep = preparar([{"review_id": 50, "texto_limpo": "Comprei ontem na loja do shopping. Fixa o dia todo."}])
    assert prep[0].enviadas == [2]
    assert prep[0].frase(2) == "Fixa o dia todo." and prep[0].frase(1) is None  # não enviada = não vale
    assert formatar(prep) == '<review numero="1">\n[1.2] Fixa o dia todo.\n</review>'
    assert ler_id_frase("3.2") == (3, 2) and ler_id_frase("[10.1]") == (10, 1) and ler_id_frase("x") is None


def test_orcamento_corta_o_modo_rapido_no_modelo_local():
    longas = [{"review_id": i, "texto_limpo": "Fixa o dia todo na pele. " * 60, "informativa": True}
              for i in range(40)]
    cfg = Config(ollama_num_ctx=8192, ollama_max_reviews=40)
    rapido = preparar_para(longas, cfg, "ollama", "rapido")
    detalhado = preparar_para(longas, cfg, "ollama", "detalhado")
    assert len(detalhado) == 40 and 0 < len(rapido) < 40
    assert sum(len(" ".join(p.frases)) for p in rapido) <= orcamento_rapido(cfg, "ollama") + 1500


def test_prompt_de_extracao_sem_frases_copiaveis():
    assert "dura o dia todo" not in SISTEMA_EXTRACAO


# --- agregação ----------------------------------------------------------------

def test_indice_suavizado_e_confianca():
    assert indice(3, 1, 0) == 6.7
    assert indice(0, 3, 0) == 2.0          # 3 críticas não viram 0
    assert indice(40, 0, 0) == 9.8         # muitos relatos quase não mudam
    assert indice(0, 0, 0) is None
    assert confianca(0) == "sem dados" and confianca(5) == "média" and confianca(20) == "alta"


def test_selecao_prioriza_informativas_e_recentes():
    sel = selecionar_reviews(REVIEWS, 3)
    assert [r["review_id"] for r in sel] == [14, 13, 12]


def test_agregar_conta_uma_vez_por_review_e_nao_repete_frase():
    mencoes = [Mencao(1, "fixacao", "positiva", "Dura o dia todo."),
               Mencao(1, "fixacao", "positiva", "Fixa bem."),            # mesma review: conta uma vez
               Mencao(2, "fixacao", "positiva", "Dura o dia todo."),     # frase igual: não repete
               Mencao(3, "fixacao", "negativa", "Some rápido."),
               Mencao(4, "fixacao", "neutra")]                          # sem frase (modo rápido): conta
    fix = agregar(mencoes, 5)["dimensoes"]["fixacao"]
    assert (fix["n_mencoes"], fix["positivas"], fix["negativas"], fix["neutras"]) == (4, 2, 1, 1)
    assert [e["trecho"] for e in fix["evidencias"]] == ["Some rápido.", "Dura o dia todo.", "Fixa bem."]


def test_ler_lote_descarta_review_ou_frase_inexistente():
    prep = preparar([{"review_id": 360949, "texto_limpo": "Fixa muito bem. Parecido com o Coco."},
                     {"review_id": 354902, "texto_limpo": "Cheiro lindo demais."}])
    saida = ExtracaoLote.model_validate(lote((1, 1, "fixacao", "+"), (1, 2, "originalidade", "-"),
                                             (1, 9, "cheiro", "+"),       # frase 9 não existe
                                             (7, 1, "elogios", "+")))     # review 7 não existe
    ext = ler_lote(saida, prep)
    assert [(m.review_id, m.dimensao, m.polaridade, m.frase) for m in ext.mencoes] == [
        (360949, "fixacao", "positiva", "Fixa muito bem."),
        (360949, "originalidade", "negativa", "Parecido com o Coco.")]
    assert ext.descartadas["cheiro"] == 1 and ext.descartadas["elogios"] == 1
    assert (ext.apontadas, ext.validas) == (4, 2)


def test_ler_rapido_conta_pelas_listas_e_pega_frases_reais():
    prep = preparar([{"review_id": 1, "texto_limpo": "Fixa o dia todo na pele."},
                     {"review_id": 2, "texto_limpo": "Some em duas horas, pena."},
                     {"review_id": 3, "texto_limpo": "Cheiro lindo de rosas."}])
    saida = AnaliseRapida.model_validate({
        "dimensoes": [{"dimensao": "fixacao", "positivas": [1, 1], "negativas": [2], "neutras": [],
                       "evidencias": ["1.1", "2.1", "3.1", "9.9"], "nota": 6, "resumo": "Divide opiniões."},
                      {"dimensao": "cheiro", "positivas": [3, 8], "evidencias": [], "resumo": "Agrada."}],
        "perfil_cliente": {"descricao": "x"}, "resumo_geral": "y"})
    ext, por_dim = ler_rapido(saida, prep)
    fix = agregar(ext.mencoes, 3)["dimensoes"]["fixacao"]
    assert (fix["positivas"], fix["negativas"]) == (1, 1)
    # 3.1 é de uma review fora das listas de fixação e 9.9 não existe: não viram evidência
    assert {e["trecho"] for e in fix["evidencias"]} == {"Fixa o dia todo na pele.", "Some em duas horas, pena."}
    assert ext.descartadas["cheiro"] == 1  # review 8 não existe
    assert por_dim["fixacao"].nota == 6


def test_completar_sintese_preenche_o_que_o_modelo_deixou_vazio():
    agregado = {"ocasioes": ["trabalho", "noite"], "perfis": ["quem gosta de florais"], "dimensoes": {
        "fixacao": {"indice": 9.0, "n_mencoes": 10, "positivas": 9, "negativas": 0},
        "originalidade": {"indice": 2.0, "n_mencoes": 4, "positivas": 0, "negativas": 3},
        "projecao": {"indice": None, "n_mencoes": 0, "positivas": 0, "negativas": 0}}}
    s = completar_sintese({"perfil_cliente": {"ocasioes": []}, "pontos_fortes": [], "pontos_fracos": ["caro"]},
                          agregado)
    assert s["perfil_cliente"]["ocasioes"] == ["trabalho", "noite"]
    assert s["perfil_cliente"]["indicado_para"] == ["quem gosta de florais"]
    assert s["pontos_fortes"][0].startswith("Fixação")
    assert s["pontos_fracos"] == ["caro"]  # o que o modelo escreveu fica


# --- pipeline -----------------------------------------------------------------

@pytest.mark.parametrize("modo", ["detalhado", "rapido"])
def test_analise_completa_no_modo_demo(modo):
    cfg = Config(min_reviews_analise=3, llm_reviews_por_lote=2)
    reg = analisar(PRODUTO, REVIEWS, DemoCliente(), cfg, modo)
    dims = {d["dimensao"]: d for d in reg["resultado"]["dimensoes"]}
    assert set(dims) == set(CHAVES)
    assert dims["fixacao"]["n_mencoes"] == 2
    assert {e["trecho"] for e in dims["fixacao"]["evidencias"]} == {
        "A fixação é excelente, dura o dia todo.", "Fixação fraca, some em 2 horas."}
    assert all(d["nota"] is None for d in dims.values() if d["n_mencoes"] == 0)
    assert reg["taxa_evidencias_ok"] == 1.0
    assert reg["versao_prompt"] == versao(modo) and reg["resultado"]["modo"] == modo
    assert reg["n_reviews_usadas"] == 6 and reg["ultimo_review_id"] == 14


def test_modo_rapido_faz_uma_chamada_e_detalhado_varias():
    class Contador(DemoCliente):
        def __init__(self):
            self.schemas = []

        def gerar(self, **kw):
            self.schemas.append(kw["schema"].__name__)
            return super().gerar(**kw)

    cfg = Config(min_reviews_analise=3, llm_reviews_por_lote=2)
    rapido, detalhado = Contador(), Contador()
    analisar(PRODUTO, REVIEWS, rapido, cfg, "rapido")
    analisar(PRODUTO, REVIEWS, detalhado, cfg, "detalhado")
    assert rapido.schemas == ["AnaliseRapida"]
    assert detalhado.schemas[-1] == "SinteseProduto" and detalhado.schemas.count("ExtracaoLote") >= 2


def test_nada_extraido_nao_gera_texto_inventado():
    class Preguicoso:
        provedor = "ollama"

        def gerar(self, *, schema, **_):
            assert schema is ExtracaoLote, "não deveria chegar à síntese"
            return ExtracaoLote.model_validate(lote())

    from fraginsights.llm.analise import SemMencoes

    with pytest.raises(SemMencoes, match="não identificou nenhuma menção"):
        analisar(PRODUTO, REVIEWS, Preguicoso(), Config(min_reviews_analise=3))


def test_modo_rapido_sem_listas_e_barrado():
    from fraginsights.llm.analise import SemMencoes

    class SoTexto:  # escreve os resumos mas não aponta nenhuma review (o que o 3B fez)
        provedor = "ollama"

        def gerar(self, *, schema, **_):
            return AnaliseRapida.model_validate({
                "dimensoes": [{"dimensao": c, "nota": 8, "resumo": "Muito bom."} for c in CHAVES],
                "perfil_cliente": {"descricao": "x"}, "resumo_geral": "y"})

    with pytest.raises(SemMencoes):
        analisar(PRODUTO, REVIEWS, SoTexto(), Config(min_reviews_analise=3), "rapido")


def test_schemas_para_tool_use_sem_refs():
    for schema in (SinteseProduto, AnaliseRapida):
        s = schema_sem_refs(schema)
        assert "$ref" not in str(s) and "$defs" not in str(s)
        assert s["properties"]["dimensoes"]["items"]["properties"]["dimensao"]["enum"] == CHAVES
    # listas antes da nota: o modelo aponta as reviews antes de dar a nota
    campos = list(AnaliseRapida.model_json_schema()["$defs"]["DimensaoRapida"]["properties"])
    assert campos.index("positivas") < campos.index("nota")


def test_nota_final_ancorada_no_indice():
    assert nota_final(None, 0, None) == (None, "sem_dados")
    assert nota_final(None, 8, 7.0) == (7.0, "indice")
    assert nota_final(0.0, 3, 2.0) == (2.0, "poucos_relatos")     # poucos relatos: vale o índice
    assert nota_final(8.0, 10, 7.5) == (8.0, "modelo")
    assert nota_final(10.0, 10, 6.0) == (8.0, "modelo_ajustada")  # exagero limitado a 2 pontos


def test_analista_le_mais_reviews_que_o_app():
    muitas = [{"review_id": i, "texto_limpo": "Fixa o dia todo na pele.", "informativa": True} for i in range(150)]
    cfg = Config(ollama_max_reviews=40, analista_max_reviews=100)
    assert len(preparar_para(muitas, cfg, "ollama", "detalhado")) == 40
    assert len(preparar_para(muitas, cfg, "ollama", "detalhado", cfg.analista_max_reviews)) == 100
