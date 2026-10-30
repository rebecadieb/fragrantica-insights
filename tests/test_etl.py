from datetime import datetime, timezone

from fraginsights.etl import (hash_conteudo, interpretar_data, limpar_texto, pseudonimizar,
                              tratar_review)
from fraginsights.models import ReviewEvento

REF = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def test_limpar_texto_remove_html_espacos_e_ruido_de_interface():
    bruto = "  <p>Cheiro   ótimo&amp; fixa bem</p>\n\n\n Mostrar mais"
    assert limpar_texto(bruto) == "Cheiro ótimo& fixa bem"


def test_hash_ignora_caixa_e_espacos_mas_separa_produtos():
    assert hash_conteudo(1, "Muito Bom  perfume") == hash_conteudo(1, "muito bom perfume")
    assert hash_conteudo(1, "muito bom") != hash_conteudo(2, "muito bom")


def test_pseudonimo_estavel_e_sem_o_nome():
    apelido = pseudonimizar("Paulo Muller")
    assert apelido == pseudonimizar("paulo muller ")
    assert "paulo" not in apelido.lower()
    assert pseudonimizar(None) is None


def test_datas_absolutas_e_relativas():
    assert interpretar_data("09/23/26 05:23", REF) == datetime(2026, 9, 23, 5, 23, tzinfo=timezone.utc)
    assert interpretar_data("19 hours ago", REF) == datetime(2026, 10, 28, 17, 0, tzinfo=timezone.utc)
    assert interpretar_data("a day ago", REF) == datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    assert interpretar_data("há 2 anos", REF).year == 2024
    assert interpretar_data("há um mês", REF).month == 9
    assert interpretar_data("ontem talvez", REF) is None
    assert interpretar_data(None, REF) is None


def test_tratar_review_marca_curtas_como_nao_informativas():
    ev = ReviewEvento(review_id=1, id_perfume=10, texto="Bom", autor="Ana", coletado_em=REF)
    reg = tratar_review(ev, anonimizar=True, min_palavras=5)
    assert reg["informativa"] is False
    assert reg["texto_original"] == "Bom"
    assert reg["autor"].startswith("avaliador_")
