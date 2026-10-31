"""Fragrantica Insights — "raio-x de uso real" de perfumes a partir das reviews.

Executar:  streamlit run src/fraginsights/app/streamlit_app.py
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import altair as alt
import pandas as pd
import streamlit as st

from fraginsights import db
from fraginsights.app.fluxo import estado_da_coleta, precisa_coletar, solicitar_coleta
from fraginsights.bus import KafkaPublicador
from fraginsights.config import carregar
from fraginsights.links import ler_link
from fraginsights.llm.clientes import criar_cliente, limites, modelos_ativos, modelos_do_modo
from fraginsights.llm.analise import SemMencoes, chave_cache, max_reviews_analista, obter_ou_gerar
from fraginsights.llm.prompts import MODOS, versao
from fraginsights.app.formatacao import (CORES_CONFIANCA, descricao_mencoes, linhas_grafico,
                                         lista_texto, nome_dimensao, nota_formatada, rotulo_produto)

def analista_ocupado() -> bool:
    try:
        with db.conexao() as conn:
            return bool(db.uso_llm_ativo(conn, "analista"))
    except Exception:
        return False


st.set_page_config(page_title="Fragrantica Insights", page_icon="🧴", layout="wide")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")  # docker compose logs app
cfg = carregar()


# ---------------------------------------------------------------------------
# Dados (com cache curto: a ingestão é contínua)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def designers() -> list[str]:
    with db.conexao() as conn:
        return [r["designer"] for r in db.consultar(conn, db.SQL_DESIGNERS)]


@st.cache_data(ttl=60, show_spinner=False)
def produtos(designer, genero, busca, min_reviews) -> list[dict]:
    with db.conexao() as conn:
        return db.consultar(conn, db.SQL_LISTAR_PRODUTOS, {
            "designer": designer, "genero": genero, "busca": busca or None, "min_reviews": min_reviews})


@st.cache_data(ttl=60, show_spinner=False)
def produto(id_perfume: int) -> dict:
    with db.conexao() as conn:
        return db.consultar_um(conn, db.SQL_PRODUTO, {"id_perfume": id_perfume})


@st.cache_data(ttl=60, show_spinner=False)
def reviews(id_perfume: int) -> list[dict]:
    with db.conexao() as conn:
        return db.consultar(conn, db.SQL_REVIEWS_PRODUTO,
                            {"id_perfume": id_perfume, "somente_informativas": False, "limite": 5000})


@st.cache_data(ttl=30, show_spinner=False)
def verificar_ollama() -> str | None:
    cliente = criar_cliente("ollama", cfg)
    for modelo in dict.fromkeys(modelos_ativos(cfg, "ollama")):
        aviso = cliente.verificar(modelo)
        if aviso:
            return aviso
    return None


def analise_em_cache(id_perfume: int, provedor: str, regs: list[dict]):
    """(análise do conjunto atual de reviews, última análise qualquer). Vale a de qualquer
    modo; a detalhada (feita pelo analista em lote) tem preferência, por ser mais fiel."""
    atual = None
    # Ordem de preferência: a do analista (detalhada, com mais reviews), a detalhada do app, a rápida.
    candidatas = [("detalhado", max_reviews_analista(cfg, provedor)), ("detalhado", None), ("rapido", None)]
    with db.conexao() as conn:
        for modo, maximo in candidatas:
            atual = db.consultar_um(conn, db.SQL_ANALISE_POR_ASSINATURA,
                                    chave_cache(id_perfume, regs, cfg, provedor, modo, maximo))
            if atual:
                break
        ultima = db.consultar_um(conn, db.SQL_ULTIMA_ANALISE, {"id_perfume": id_perfume,
                                                                "versoes": [versao(m) for m in MODOS]})
    return atual, ultima


# ---------------------------------------------------------------------------
# Barra lateral: provedor de IA e filtros
# ---------------------------------------------------------------------------

st.sidebar.title("🧴 Fragrantica Insights")
provedor = cfg.llm_provedor
# Sem chave da API: modo leitura. O app mostra as análises já salvas no banco (por exemplo, as
# que vêm na cópia do repositório, `make restaurar`) e coleta reviews, mas não gera análises.
somente_leitura = provedor == "anthropic" and not os.getenv("ANTHROPIC_API_KEY")
st.sidebar.caption(f"Provedor de IA: **{provedor}**"
                   + (f" · {modelos_ativos(cfg, provedor)[1]}" if provedor != "demo" else "")
                   + (" · modo leitura" if somente_leitura else ""))
if somente_leitura:
    st.sidebar.info("Sem ANTHROPIC_API_KEY: o app mostra as análises já salvas e coleta reviews, "
                    "mas não gera análises novas. Para gerar, coloque a chave no .env.")
if provedor == "ollama":
    aviso_ollama = verificar_ollama()
    if aviso_ollama:
        st.sidebar.warning(aviso_ollama)
    st.sidebar.caption("Modelo open source local: a primeira análise de um perfume leva alguns minutos.")

try:
    lista_designers = designers()
except Exception as erro:  # banco fora do ar
    st.error(f"Não foi possível conectar ao banco: {erro}")
    st.stop()

# Duas formas de escolher: o catálogo (perfumes das marcas populares, coletados ou não)
# ou o link de QUALQUER perfume do Fragrantica.
modo = st.sidebar.radio("Escolher perfume", ["Buscar no catálogo", "Colar link do Fragrantica"])
via_link: dict | None = None

if modo == "Buscar no catálogo":
    designer = st.sidebar.selectbox("Marca", ["Todas"] + lista_designers)
    genero = st.sidebar.selectbox("Gênero", ["Todos", "feminino", "masculino", "compartilhável"])
    busca = st.sidebar.text_input("Nome do perfume", placeholder="ex.: Sauvage")
    so_com_reviews = st.sidebar.checkbox("Só perfumes que já têm reviews no banco", value=False)
    opcoes = produtos(None if designer == "Todas" else designer, None if genero == "Todos" else genero,
                      busca.strip(), cfg.min_reviews_analise if so_com_reviews else 0)
    if not opcoes:
        st.info("Nenhum perfume encontrado. Tente outro nome, rode o catálogo (`make catalogo`) "
                "ou cole o link do perfume do site.")
        st.stop()
    escolhido = st.sidebar.selectbox("Perfume", opcoes, format_func=rotulo_produto,
                                     help="Mostra até 500 resultados; digite parte do nome para filtrar.")
    id_escolhido = escolhido["id_perfume"]
else:
    url = st.sidebar.text_input("Link do perfume", placeholder="https://www.fragrantica.com.br/perfume/Dior/Sauvage-31861.html")
    if not url:
        st.info("Cole na barra lateral o link de qualquer perfume do Fragrantica (de qualquer idioma do site).")
        st.stop()
    via_link = ler_link(url)
    if via_link is None:
        st.sidebar.error("Isso não parece um link de perfume do Fragrantica.")
        st.stop()
    id_escolhido = via_link["id_perfume"]

p = produto(id_escolhido)            # None = perfume ainda não está no banco
regs = reviews(id_escolhido) if p else []
ficha = p or {"id_perfume": id_escolhido, "nome": via_link["nome"], "designer": via_link["designer"],
              "link": via_link["link"], "n_reviews": 0, "n_informativas": 0}

# ---------------------------------------------------------------------------
# Cabeçalho do produto
# ---------------------------------------------------------------------------

st.title(ficha["nome"])
st.caption(" · ".join(x for x in [ficha["designer"], ficha.get("genero") or "",
                                  str(ficha["ano"]) if ficha.get("ano") else ""] if x))
c1, c2, c3, c4 = st.columns(4)
c1.metric("Nota no site", f"{float(ficha['nota_media']):.2f} / 5" if ficha.get("nota_media") is not None else "—")
c2.metric("Votos no site", f"{ficha['votos']:,}".replace(",", ".") if ficha.get("votos") else "—")
c3.metric("Reviews no banco", f"{ficha['n_reviews']}" + (f" de {ficha['reviews_site']}" if ficha.get("reviews_site") else ""))
c4.metric("Coletado em", ficha["ultima_coleta"].strftime("%d/%m/%Y") if ficha.get("ultima_coleta") else "—")

if ficha.get("acordes"):
    st.markdown("**Acordes:** " + " · ".join(ficha["acordes"]))
if any(ficha.get(k) for k in ("notas_topo", "notas_coracao", "notas_base")):
    n1, n2, n3 = st.columns(3)
    n1.markdown(f"**Topo:** {lista_texto(ficha['notas_topo'])}")
    n2.markdown(f"**Coração:** {lista_texto(ficha['notas_coracao'])}")
    n3.markdown(f"**Base:** {lista_texto(ficha['notas_base'])}")

aba_ia, aba_reviews, aba_pipeline = st.tabs(["✨ Análise de uso real (IA)", "💬 Reviews", "⚙️ Pipeline"])


def coletar_agora() -> bool:
    """Publica um pedido no Kafka e acompanha até as reviews chegarem ao banco."""
    pedido = solicitar_coleta(KafkaPublicador(cfg.kafka_bootstrap), cfg, id_perfume=ficha["id_perfume"],
                              link=ficha["link"], designer=ficha["designer"], nome=ficha["nome"])
    with st.status("Pedido de coleta enviado", expanded=True) as caixa:
        limite = time.time() + cfg.espera_coleta_s
        mensagem_anterior = None
        while time.time() < limite:
            with db.conexao() as conn:
                linha = db.consultar_um(conn, db.SQL_PEDIDO, {"id_pedido": pedido.id_pedido})
                atual_p = db.consultar_um(conn, db.SQL_PRODUTO, {"id_perfume": ficha["id_perfume"]})
            estado, mensagem = estado_da_coleta(linha, atual_p)
            if mensagem != mensagem_anterior:
                caixa.write(mensagem)
                mensagem_anterior = mensagem
            if estado == "pronto":
                caixa.update(label=mensagem, state="complete", expanded=False)
                st.cache_data.clear()
                return True
            if estado == "erro":
                caixa.update(label="A coleta falhou", state="error")
                st.error(mensagem)
                return False
            time.sleep(3)
        caixa.update(label="O coletor não respondeu a tempo", state="error")
        st.error("O pedido não foi concluído. Confira se o serviço está no ar: `docker compose logs coletor`.")
        return False


# ---------------------------------------------------------------------------
# Aba 1: análise com IA
# ---------------------------------------------------------------------------

with aba_ia:
    agora = datetime.now(timezone.utc)
    coletar_antes, motivo = precisa_coletar(p, agora, cfg)
    atual, ultima = analise_em_cache(ficha["id_perfume"], provedor, regs) if p else (None, None)
    analise = None if coletar_antes else (atual or ultima)

    col_a, col_b = st.columns([3, 1])
    if somente_leitura:
        analise, gerar, coletar_antes = atual or ultima, False, False
        with col_b:
            atualizar = (bool(ficha.get("link")) and ficha.get("fonte") != "exemplo"
                         and st.button("Coletar reviews agora", use_container_width=True,
                                       help="Busca no site as reviews mais recentes (não precisa de chave)"))
        aviso = col_a.empty()
        with aviso.container():
            if not analise:
                st.info("Este perfume ainda não tem análise salva. Gerar análises precisa da ANTHROPIC_API_KEY "
                        "no .env; sem ela, dá para coletar as reviews e vê-las na aba Reviews.")
            elif not atual:
                st.caption(f"Análise salva em {analise['criado_em']:%d/%m/%Y}, com {analise['n_reviews_usadas']} "
                           "reviews. Reviews coletadas depois dela não estão incluídas.")
    else:
        with col_b:
            gerar = st.button("Gerar análise" if not atual or coletar_antes else "Gerar novamente",
                              type="primary", use_container_width=True)
            atualizar = (not coletar_antes and bool(ficha.get("link")) and ficha.get("fonte") != "exemplo"
                         and st.button("Atualizar reviews", use_container_width=True,
                                       help="Busca no site as reviews publicadas desde a última coleta"))
        aviso = col_a.empty()  # apagado quando a análise fica pronta, para não ficar um aviso velho na tela
        with aviso.container():
            if coletar_antes:
                st.info(f"{motivo} Ao clicar em **Gerar análise**, as reviews serão coletadas no Fragrantica "
                        "agora (1 a 2 minutos) e a análise sai em seguida.")
            elif not atual and ultima:
                modelos_atuais = {modelos_do_modo(cfg, provedor, m)[1] for m in MODOS}
                if ultima["provedor"] != provedor or ultima["modelo_sintese"] not in modelos_atuais:
                    motivo_antiga = (f"feita com outro modelo ({ultima['provedor']} · {ultima['modelo_sintese']}). "
                                     "Gere de novo para usar o modelo atual.")
                else:
                    motivo_antiga = (f"feita com {ultima['n_reviews_usadas']} reviews. "
                                     "Chegaram reviews novas: gere de novo para atualizar.")
                st.warning(f"Mostrando a análise de {ultima['criado_em']:%d/%m/%Y}, {motivo_antiga}")
            elif not analise:
                st.info("Este perfume ainda não tem análise. Clique em **Gerar análise** "
                        f"(usa até {limites(cfg, provedor)[0]} reviews mais recentes).")

    if atualizar and coletar_agora():
        st.rerun()

    if gerar:
        if coletar_antes:
            if not coletar_agora():
                st.stop()
            regs = reviews(ficha["id_perfume"])  # a aba Reviews já mostra o que acabou de chegar
        with db.conexao() as conn:
            p = db.consultar_um(conn, db.SQL_PRODUTO, {"id_perfume": ficha["id_perfume"]})
        if not p or p["n_informativas"] < cfg.min_reviews_analise:
            st.warning(f"O Fragrantica tem só {p['n_reviews'] if p else 0} reviews deste perfume; são necessárias "
                       f"ao menos {cfg.min_reviews_analise} com 5 palavras ou mais para uma análise confiável.")
            st.stop()
        with st.spinner("Lendo as reviews e gerando a análise..."):
            nota_analista = st.empty()
            if analista_ocupado():
                nota_analista.caption("O analista automático está processando outro perfume: ele pausa assim "
                                      "que terminar o trecho atual e esta análise passa na frente.")
            try:
                # Marca o uso: o analista em lote vê a marca e espera esta análise terminar.
                with db.usando_llm("app", p["id_perfume"]), db.conexao() as conn:
                    cliente = criar_cliente(provedor, cfg)
                    forcar = bool(atual) and not coletar_antes
                    try:
                        analise = obter_ou_gerar(conn, p["id_perfume"], cliente, cfg, forcar=forcar,
                                                 modo=cfg.modo_analise_app)
                    except SemMencoes:
                        if cfg.modo_analise_app != "rapido":
                            raise
                        # Modelos pequenos às vezes não dão conta de ler tudo de uma vez: refaz em lotes.
                        st.info("A leitura rápida não encontrou menções com este modelo. Refazendo pela "
                                "leitura detalhada, em lotes (leva alguns minutos a mais)...")
                        analise = obter_ou_gerar(conn, p["id_perfume"], cliente, cfg, forcar=forcar,
                                                 modo="detalhado")
            except Exception as erro:
                st.error(f"Falha ao gerar a análise: {erro}")
                analise = None
            nota_analista.empty()
        if analise:
            aviso.empty()
            if coletar_antes:
                # As reviews e a ficha acabaram de chegar, mas o cabeçalho já foi desenhado com os
                # dados de antes da coleta ("Reviews no banco 0"). Recarrega a página: a análise
                # recém-salva vem do banco, e o cabeçalho, a aba Reviews e a busca, atualizados.
                st.rerun()

    if analise:
        r = db.para_json(analise["resultado"])
        st.subheader("Resumo")
        st.write(r["resumo_geral"])

        linhas = linhas_grafico(r["dimensoes"])
        if linhas:
            grafico = (alt.Chart(pd.DataFrame(linhas))
                       .mark_bar(cornerRadiusEnd=4)
                       .encode(x=alt.X("Nota:Q", scale=alt.Scale(domain=[0, 10]), title="Nota (0 a 10)"),
                               y=alt.Y("Dimensão:N", sort="-x", title=None),
                               color=alt.Color("Confiança:N", scale=alt.Scale(
                                   domain=list(CORES_CONFIANCA), range=list(CORES_CONFIANCA.values()))),
                               tooltip=["Dimensão", "Nota", "Confiança", "Menções"])
                       .properties(height=40 * len(linhas) + 20))
            st.altair_chart(grafico, use_container_width=True)

        st.subheader("Perfil de cliente ideal")
        perfil = r["perfil_cliente"]
        st.write(perfil["descricao"])
        k1, k2 = st.columns(2)
        k1.markdown(f"**Indicado para:** {lista_texto(perfil.get('indicado_para', []))}")
        k1.markdown(f"**Ocasiões:** {lista_texto(perfil.get('ocasioes', []))}")
        k2.markdown(f"**Não indicado para:** {lista_texto(perfil.get('nao_indicado_para', []))}")
        k2.markdown(f"**Estações:** {lista_texto(perfil.get('estacoes', []))} · "
                    f"**Período:** {perfil.get('periodo') or '—'}")
        f1, f2 = st.columns(2)
        f1.markdown("**Pontos fortes:** " + lista_texto(r.get("pontos_fortes", [])))
        f2.markdown("**Pontos fracos:** " + lista_texto(r.get("pontos_fracos", [])))

        st.subheader("Dimensões em detalhe")
        for d in r["dimensoes"]:
            titulo = f"{nome_dimensao(d['dimensao'])} — {nota_formatada(d['nota'])} · confiança {d['confianca']}"
            with st.expander(titulo, expanded=False):
                st.write(d["resumo"])
                st.caption(descricao_mencoes(d) + (f" · índice calculado {d['indice_calculado']}"
                                                   if d.get("indice_calculado") is not None else "")
                           + {"indice": " · o modelo não deu nota: vale o índice calculado",
                              "poucos_relatos": " · poucos relatos: a nota é o índice calculado",
                              "modelo_ajustada": " · nota do modelo ajustada para perto do índice",
                              }.get(d.get("origem_nota"), ""))
                for e in d["evidencias"][:5]:
                    marca = "" if e["verificada"] else " · ⚠️ não encontrado literalmente"  # análises antigas
                    st.markdown(f"> “{e['trecho']}”  \n<small>{e['polaridade']} · review {e['review_id']}"
                                f"{marca}</small>", unsafe_allow_html=True)

        taxa = analise.get("taxa_evidencias_ok")
        modo_txt = {"rapido": "análise rápida", "detalhado": "análise detalhada"}.get(
            db.para_json(analise["resultado"]).get("modo"), "")
        st.caption(
            (f"{modo_txt.capitalize()}. " if modo_txt else "") +
            f"Gerado por IA ({analise['provedor']} · {analise['modelo_sintese']} · prompt {analise['versao_prompt']}) "
            f"em {analise['criado_em']:%d/%m/%Y %H:%M} com {analise['n_reviews_usadas']} reviews"
            + (f" · {float(taxa):.0%} das menções apontam frases que existem nas reviews" if taxa is not None else "")
            + ". Pode conter imprecisões: confira os trechos.")

# ---------------------------------------------------------------------------
# Aba 2: reviews
# ---------------------------------------------------------------------------

with aba_reviews:
    filtro = st.text_input("Filtrar reviews por palavra", key="filtro_reviews")
    df = pd.DataFrame(regs)
    if df.empty:
        st.info("Sem reviews para este perfume.")
    else:
        if filtro:
            df = df[df["texto_limpo"].str.contains(filtro, case=False, na=False)]
        st.caption(f"{len(df)} reviews")
        st.dataframe(df[["review_id", "data_review", "autor", "n_palavras", "informativa", "texto_limpo"]],
                     use_container_width=True, hide_index=True,
                     column_config={"texto_limpo": st.column_config.TextColumn("texto", width="large"),
                                    "data_review": st.column_config.DatetimeColumn("data", format="DD/MM/YYYY")})

# ---------------------------------------------------------------------------
# Aba 3: saúde do pipeline
# ---------------------------------------------------------------------------

with aba_pipeline:
    with db.conexao() as conn:
        totais = db.consultar_um(conn, db.SQL_TOTAIS)
        diario = pd.DataFrame(db.consultar(conn, db.SQL_INGESTAO_DIARIA))
        analises_dia = pd.DataFrame(db.consultar(conn, db.SQL_ANALISES_DIARIAS))
        fila = db.consultar(conn, db.SQL_FILA_ANALISE, {
            "provedor": provedor, "modelo_sintese": modelos_do_modo(cfg, provedor, cfg.modo_analise_analista)[1],
            "versao_prompt": versao(cfg.modo_analise_analista), "min_reviews": cfg.min_reviews_analise, "limite": 100000})
    t1, t2, t3, t4 = st.columns(4)
    t1.metric("Produtos", totais["produtos"])
    t2.metric("Reviews", totais["reviews"])
    t3.metric("Análises em cache", totais["analises"])
    t4.metric("Última ingestão", totais["ultima_ingestao"].strftime("%d/%m %H:%M") if totais["ultima_ingestao"] else "—")
    if not diario.empty:
        st.caption("Reviews novas gravadas por dia (últimos 30 dias)")
        st.bar_chart(diario.set_index("dia")["reviews_novas"])
    st.caption(f"Fila do analista automático: {len(fila)} perfumes aguardando análise "
               f"({'trabalhando agora' if analista_ocupado() else 'parado ou aguardando'})")
    if not analises_dia.empty:
        st.caption("Análises geradas por dia")
        st.bar_chart(analises_dia.set_index("dia")["analises"])
