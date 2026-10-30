import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("validacao", Path(__file__).parents[1] / "scripts" / "validacao.py")
validacao = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validacao)


def test_kappa():
    assert validacao.kappa([("a", "a"), ("b", "b")]) == 1.0
    assert validacao.kappa([("a", "b"), ("b", "a")]) == -1.0
    assert validacao.kappa([]) is None


def test_avaliar_imprime_metricas(tmp_path, capsys):
    from fraginsights.llm.taxonomia import CHAVES

    arq = tmp_path / "v.csv"
    cab = ["review_id", "texto"] + [f"humano_{c}" for c in CHAVES] + [f"llm_{c}" for c in CHAVES]
    vazio = {c: "" for c in cab}
    linhas = [
        {**vazio, "review_id": 1, "humano_fixacao": "positiva", "llm_fixacao": "positiva"},
        {**vazio, "review_id": 2, "humano_fixacao": "negativa", "llm_fixacao": "positiva"},
        {**vazio, "review_id": 3, "llm_fixacao": "neutra"},
    ]
    import csv
    with arq.open("w", newline="") as f:
        w = csv.DictWriter(f, cab)
        w.writeheader()
        w.writerows(linhas)
    validacao.avaliar(arq)
    saida = capsys.readouterr().out
    assert "fixacao" in saida and "0.67" in saida  # precisão 2/3
