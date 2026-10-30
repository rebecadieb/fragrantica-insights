from fraginsights.scraper.fragrantica import filtrar_novas, interpretar_detalhes


def test_interpretar_detalhes():
    bruto = {"h1": "Sauvage Dior para homens",
             "corpo": "Avaliação 3.85 fora de 5 com 34,347 votos ... Resenhas (662) ... foi lançado em 2015",
             "acordes": ["fresco especiado"], "perfumistas": ["François Demachy"],
             "notas": {"topo": ["Bergamota"], "coracao": ["Pimenta"], "base": ["Ambroxan"], "gerais": []}}
    d = interpretar_detalhes(bruto, "Sauvage Dior perfume - a fragrância Masculino 2015")
    assert d["genero"] == "masculino" and d["ano"] == 2015
    assert d["nota_media"] == 3.85 and d["votos"] == 34347 and d["reviews_site"] == 662
    assert d["notas_base"] == ["Ambroxan"]


def test_piramide_plana_vira_base_e_titulo_da_o_ano():
    bruto = {"h1": "X", "corpo": "", "notas": {"topo": [], "coracao": [], "base": [], "gerais": ["Rosa"]}}
    d = interpretar_detalhes(bruto, "X Marca perfume - a fragrância Feminino 2022")
    assert d["notas_base"] == ["Rosa"] and d["ano"] == 2022 and d["genero"] == "feminino"


def test_filtrar_novas_usa_marca_d_agua_do_banco():
    brutas = [{"review_id": "105"}, {"review_id": "100"}, {"review_id": "99"}, {"review_id": "x"}]
    assert [r["review_id"] for r in filtrar_novas(brutas, 100)] == ["105"]
    assert len(filtrar_novas(brutas, None)) == 3


def test_marcas_populares_unicas_em_ordem_e_nome_cortado():
    from fraginsights.scraper.fragrantica import BASE, filtrar_marcas

    links = [
        {"href": f"{BASE}/desenhista/Lattafa-Perfumes.html", "texto": "1. Lattafa Perfumes"},
        {"href": f"{BASE}/desenhista/Dior.html", "texto": "Dior"},
        {"href": f"{BASE}/desenhista/Dior.html", "texto": "Dior"},                       # repetido
        {"href": f"{BASE}/desenhista/Maison-Martin-Margiela.html", "texto": "Maison Martin..."},
        {"href": f"{BASE}/perfume/Dior/Sauvage-31861.html", "texto": "Sauvage"},         # não é marca
    ]
    marcas = filtrar_marcas(links)
    assert [m["slug"] for m in marcas] == ["Lattafa-Perfumes", "Dior", "Maison-Martin-Margiela"]
    assert marcas[2]["designer"] == "Maison Martin Margiela" and marcas[0]["ranking"] == 1
    assert marcas[0]["designer"] == "Lattafa Perfumes"
    assert len(filtrar_marcas(links, limite=1)) == 1


def test_perfumes_so_da_propria_marca():
    from fraginsights.scraper.fragrantica import BASE, filtrar_perfumes

    links = [
        {"href": f"{BASE}/perfume/Dior/Sauvage-31861.html", "texto": "Sauvage\nDior"},
        {"href": f"{BASE}/perfume/Dior/Sauvage-31861.html", "texto": ""},                # mesma, sem texto
        {"href": f"{BASE}/perfume/Natura/Frescor-de-Andiroba-88599.html", "texto": "Frescor"},  # lateral
        {"href": f"{BASE}/perfume/Dior/Fahrenheit-228.html", "texto": "Fahrenheit"},
    ]
    perfumes = filtrar_perfumes(links, "Dior")
    assert [(p["id_perfume"], p["nome"]) for p in perfumes] == [(31861, "Sauvage"), (228, "Fahrenheit")]
