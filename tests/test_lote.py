from fraginsights.lote import estimativa_custo, ler_csv, selecionar


def _csv(tmp_path, conteudo):
    caminho = tmp_path / "populares.csv"
    caminho.write_text(conteudo, encoding="utf-8")
    return caminho


def test_le_links_em_qualquer_coluna_e_formato(tmp_path):
    caminho = _csv(tmp_path, (
        "ranking;marca;link\n"
        "1;Dior;https://www.fragrantica.com.br/perfume/Dior/Sauvage-31861.html\n"
        "2;Dior;https://www.fragrantica.com/perfume/Dior/Miss-Dior-2017-46612.html\n"
        "3;Dior;https://www.fragrantica.com.br/perfume/Dior/Sauvage-31861.html\n"     # repetido
        "4;Chanel;não é link\n"
        "5;Chanel;fragrantica.com.br/perfume/Chanel/Coco-Mademoiselle-611.html\n"))
    perfumes = ler_csv(caminho)
    assert [p["id_perfume"] for p in perfumes] == [31861, 46612, 611]
    assert perfumes[0]["designer"] == "Dior" and perfumes[2]["designer"] == "Chanel"
    assert all(p["link"].startswith("https://www.fragrantica.com.br/perfume/") for p in perfumes)


def test_csv_com_virgula_e_bom_do_excel(tmp_path):
    caminho = tmp_path / "x.csv"
    caminho.write_bytes("﻿link,nome\nhttps://www.fragrantica.com.br/perfume/Lattafa-Perfumes/Khamrah-75805.html,Khamrah\n"
                        .encode("utf-8"))
    assert [p["id_perfume"] for p in ler_csv(caminho)] == [75805]


def test_seleciona_n_por_marca_na_ordem_ate_o_total():
    perfumes = [{"id_perfume": i, "designer": d} for i, d in
                enumerate(["Dior", "Dior", "Dior", "Chanel", "chanel", "Chanel", "Creed", "Creed", "Lancôme"])]
    escolhidos = selecionar(perfumes, total=5, por_marca=2)
    assert [p["id_perfume"] for p in escolhidos] == [0, 1, 3, 4, 6]   # 2 Dior, 2 Chanel, 1 Creed: total 5
    assert len(selecionar(perfumes, total=80, por_marca=2)) == 7      # acaba o CSV antes do total


def test_estimativa_de_custo_proporcional_as_reviews():
    assert estimativa_custo(80, 100) == (4.0, 8.0)
    assert estimativa_custo(80, 200) == (8.0, 16.0)
