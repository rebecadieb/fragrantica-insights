import math

import pytest
from pydantic import ValidationError

from fraginsights.models import ProdutoEvento, ReviewEvento


def test_produto_aceita_listas_em_texto_e_nan_do_pandas():
    p = ProdutoEvento(id_perfume=1, designer="X", nome="Y", acordes="doce; amadeirado ;",
                      notas_topo=math.nan, ano=2015.0, votos=math.nan, genero=math.nan)
    assert p.acordes == ["doce", "amadeirado"]
    assert p.notas_topo == [] and p.votos is None and p.genero is None and p.ano == 2015


def test_review_sem_texto_ou_id_invalido_e_rejeitada():
    with pytest.raises(ValidationError):
        ReviewEvento(review_id=1, id_perfume=1, texto="")
    with pytest.raises(ValidationError):
        ReviewEvento(review_id="abc", id_perfume=1, texto="ok")


def test_ida_e_volta_json():
    r = ReviewEvento(review_id=5, id_perfume=9, texto="ótimo", autor=" ")
    assert r.autor is None
    assert ReviewEvento.model_validate_json(r.model_dump_json()) == r
