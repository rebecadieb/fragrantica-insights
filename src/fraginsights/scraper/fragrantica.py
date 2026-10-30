"""Coletor do Fragrantica (Selenium + SeleniumBase em modo UC).

Mesma técnica validada no Colab: Chrome "undetected" + tela virtual (Xvfb) para
passar pela verificação do Cloudflare, e rolagem gradual para disparar o
carregamento preguiçoso das resenhas.

A coleta é INCREMENTAL: para cada perfume, o coletor recebe o maior review_id já
gravado no banco e para de clicar em "mais resenhas" assim que a página chega
nele (os ids crescem com o tempo e a lista vem das mais novas para as mais antigas).
"""
from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Optional

from ..models import ProdutoEvento, ReviewEvento
from . import js

log = logging.getLogger(__name__)

BASE = "https://www.fragrantica.com.br"
RECONNECT_S = 3
ESPERA_REVIEWS_S = 12


# ---------------------------------------------------------------------------
# Interpretação (funções puras, testáveis sem navegador)
# ---------------------------------------------------------------------------

def _numero(texto: Optional[str]) -> Optional[int]:
    if not texto:
        return None
    digitos = re.sub(r"\D", "", texto)
    return int(digitos) if digitos else None


def interpretar_detalhes(bruto: dict, titulo_pagina: str) -> dict:
    """Converte o retorno de JS_EXTRAIR nos campos do ProdutoEvento."""
    corpo = bruto.get("corpo", "")
    nota = re.search(r"(\d+[.,]\d+)\s*fora de\s*5", corpo)
    votos = re.search(r"([\d.,]+)\s*votos", corpo)
    resenhas = re.search(r"resenhas\s*\(\s*([\d.,]+)\s*\)", corpo, re.I)
    ano = (re.search(r"lan[çc]ad[oa]\s+em\s+(\d{4})", corpo, re.I)
           or re.search(r"(\d{4})\s*$", titulo_pagina or ""))

    texto_genero = f"{bruto.get('h1', '')} {titulo_pagina}".lower()
    if "para homens e mulheres" in texto_genero or "compartilh" in texto_genero or "unissex" in texto_genero:
        genero = "compartilhável"
    elif "para mulheres" in texto_genero or "feminino" in texto_genero:
        genero = "feminino"
    elif "para homens" in texto_genero or "masculino" in texto_genero:
        genero = "masculino"
    else:
        genero = None

    notas = bruto.get("notas", {})
    return {
        "genero": genero,
        "ano": int(ano.group(1)) if ano else None,
        "nota_media": float(nota.group(1).replace(",", ".")) if nota else None,
        "votos": _numero(votos.group(1)) if votos else None,
        "reviews_site": _numero(resenhas.group(1)) if resenhas else None,
        "perfumistas": bruto.get("perfumistas", []),
        "acordes": bruto.get("acordes", []),
        "notas_topo": notas.get("topo", []),
        "notas_coracao": notas.get("coracao", []),
        # pirâmide "plana" (sem níveis) cai em notas gerais -> tratamos como base
        "notas_base": notas.get("base", []) or notas.get("gerais", []),
    }


def filtrar_novas(reviews_brutas: list[dict], ultimo_review_id: Optional[int]) -> list[dict]:
    """Mantém só reviews com id numérico maior que o último já gravado."""
    novas = []
    for r in reviews_brutas:
        rid = str(r.get("review_id", ""))
        if not rid.isdigit():
            continue
        if ultimo_review_id is None or int(rid) > ultimo_review_id:
            novas.append(r)
    return novas


_LINK_MARCA = re.compile(rf"^{re.escape(BASE)}/desenhista/([^/]+)\.html$")


def filtrar_marcas(links: list[dict], limite: int = 60) -> list[dict]:
    """Links da seção 'Marcas Mais Populares' -> marcas únicas, na ordem do ranking."""
    marcas, vistos = [], set()
    for item in links:
        m = _LINK_MARCA.match(item.get("href") or "")
        if not m or m.group(1) in vistos:
            continue
        vistos.add(m.group(1))
        nome = re.sub(r"^\d+\.\s*", "", (item.get("texto") or "").strip())  # tira "1. " do ranking
        if not nome or nome.endswith("..."):  # nome cortado na tela
            nome = (item.get("titulo") or "").strip() or m.group(1).replace("-", " ")
        marcas.append({"ranking": len(marcas) + 1, "designer": nome, "slug": m.group(1),
                       "link": item["href"]})
        if len(marcas) >= limite:
            break
    return marcas


def filtrar_perfumes(links: list[dict], slug: str) -> list[dict]:
    """Links da página da marca -> perfumes DESSA marca (ignora barras laterais)."""
    padrao = re.compile(rf"^{re.escape(BASE)}/perfume/{re.escape(slug)}/[^/]+-(\d+)\.html$")
    perfumes, vistos = [], set()
    for item in links:
        m = padrao.match(item.get("href") or "")
        if not m or m.group(1) in vistos:
            continue
        vistos.add(m.group(1))
        nome = (item.get("texto") or "").strip().split("\n")[0].strip()
        perfumes.append({"id_perfume": int(m.group(1)), "nome": nome or None, "link": item["href"]})
    return perfumes


# ---------------------------------------------------------------------------
# Navegador
# ---------------------------------------------------------------------------

class ColetorFragrantica:
    """Uso:
        with ColetorFragrantica() as coletor:
            produto, reviews = coletor.coletar(produto_dict)
    """

    def __init__(self, headless: bool = False, max_cliques: int = 100,
                 binario_chrome: Optional[str] = None, reiniciar_a_cada: int = 150):
        self.headless = headless
        self.max_cliques = max_cliques
        self.binario_chrome = binario_chrome
        self.reiniciar_a_cada = reiniciar_a_cada
        self._driver = None
        self._display = None
        self._paginas = 0

    # -- ciclo de vida -------------------------------------------------------
    def __enter__(self):
        self._iniciar()
        return self

    def __exit__(self, *exc):
        self._encerrar()

    def _iniciar(self):
        from seleniumbase import Driver

        if not self.headless:
            from pyvirtualdisplay import Display

            self._display = Display(visible=0, size=(1920, 1080))
            self._display.start()
        opcoes = {"uc": True, "page_load_strategy": "eager", "block_images": True,
                  "headless": self.headless}
        if self.binario_chrome:
            opcoes["binary_location"] = self.binario_chrome
        self._driver = Driver(**opcoes)
        self._driver.set_page_load_timeout(30)

    def _encerrar(self):
        try:
            if self._driver:
                self._driver.quit()
        except Exception:  # navegador já morto
            pass
        if self._display:
            self._display.stop()
        self._driver = self._display = None

    # -- navegação -----------------------------------------------------------
    def _abrir(self, url: str, tentativas: int = 3, esperar: str = "h1") -> None:
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        d = self._driver
        for t in range(1, tentativas + 1):
            try:
                d.uc_open_with_reconnect(url, RECONNECT_S)
            except TimeoutException:
                d.execute_script("window.stop();")
            try:
                WebDriverWait(d, 15).until(EC.presence_of_element_located((By.TAG_NAME, esperar)))
            except TimeoutException:
                pass
            titulo = d.title.lower()
            if "just a moment" in titulo or "um momento" in titulo:
                log.warning("Cloudflare em %s (tentativa %d)", url, t)
                time.sleep(15 * t)
                continue
            if d.find_elements(By.TAG_NAME, esperar):
                return
            time.sleep(5 * t)
        raise RuntimeError(f"Página não carregou: {url} (título {d.title!r})")

    def _esperar_reviews(self) -> bool:
        d = self._driver
        d.execute_script(js.JS_ROLAR_ATE_REVIEWS)
        fim = time.time() + ESPERA_REVIEWS_S
        while time.time() < fim:
            if d.execute_script(js.JS_CONTAR_REVIEWS):
                return True
            d.execute_script("window.scrollBy(0, 700);")
            time.sleep(0.6)
        return d.execute_script(js.JS_CONTAR_REVIEWS) > 0

    def _carregar_mais(self, ultimo_review_id: Optional[int], max_reviews: Optional[int] = None) -> None:
        d = self._driver
        anteriores = -1
        for _ in range(self.max_cliques):
            if max_reviews and d.execute_script(js.JS_CONTAR_REVIEWS) >= max_reviews:
                return  # coleta sob demanda: a análise só usa as mais recentes
            if ultimo_review_id is not None:
                menor = d.execute_script(js.JS_MENOR_REVIEW_ID)
                if menor is not None and menor <= ultimo_review_id:
                    return  # já chegamos ao que está no banco
            qtd = d.execute_script(js.JS_CONTAR_REVIEWS)
            if qtd == anteriores or not d.execute_script(js.JS_BOTAO_MAIS):
                return
            anteriores = qtd
            time.sleep(random.uniform(1.2, 2.0))

    # -- API pública ---------------------------------------------------------
    def coletar(self, produto: dict, max_reviews: Optional[int] = None) -> tuple[ProdutoEvento, list[ReviewEvento]]:
        """`produto` precisa de id_perfume, designer, nome, link e (opcional) max_review_id.
        `max_reviews` limita quantas resenhas carregar (coleta sob demanda)."""
        if self._paginas and self._paginas % self.reiniciar_a_cada == 0:
            self._encerrar()  # recria o Chrome de tempos em tempos (memória)
            self._iniciar()
        self._paginas += 1

        agora = datetime.now(timezone.utc)
        self._abrir(produto["link"])
        detalhes = interpretar_detalhes(self._driver.execute_script(js.JS_EXTRAIR), self._driver.title)
        evento_produto = ProdutoEvento(
            tipo="detalhes", id_perfume=produto["id_perfume"], designer=produto["designer"],
            nome=produto["nome"], link=produto["link"], fonte="scraper", coletado_em=agora, **detalhes,
        )

        ultimo = produto.get("max_review_id")
        reviews: list[ReviewEvento] = []
        if self._esperar_reviews():
            self._carregar_mais(ultimo, max_reviews)
            brutas = filtrar_novas(self._driver.execute_script(js.JS_REVIEWS), ultimo)
            for r in brutas:
                if not r.get("texto"):
                    continue
                reviews.append(ReviewEvento(
                    review_id=int(r["review_id"]), id_perfume=produto["id_perfume"],
                    designer=produto["designer"], perfume=produto["nome"],
                    autor=r.get("autor"), data_texto=r.get("data_texto"), tags=r.get("tags"),
                    texto=r["texto"], fonte="scraper", coletado_em=agora,
                ))
        elif detalhes["reviews_site"]:
            log.warning("%s: site informa %s resenhas, nenhuma carregou", produto["nome"], detalhes["reviews_site"])
        return evento_produto, reviews

    def listar_marcas_populares(self, limite: int = 60) -> list[dict]:
        """Seção 'Marcas Mais Populares' da página de designers, na ordem do ranking."""
        self._abrir(f"{BASE}/desenhista/", esperar="a")
        return filtrar_marcas(self._driver.execute_script(js.JS_LINKS_MARCAS_POPULARES), limite)

    def listar_perfumes_da_marca(self, marca: dict) -> list[dict]:
        self._abrir(marca["link"], esperar="a")
        for _ in range(5):  # parte da lista só carrega rolando
            self._driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1)
        return filtrar_perfumes(self._driver.execute_script(js.JS_LINKS_PERFUMES), marca["slug"])
