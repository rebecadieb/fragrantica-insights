"""Divide as reviews em frases numeradas ([3.2] = review 3, frase 2) e filtra as que não
tocam em nenhuma dimensão.

O modelo aponta a evidência pelo NÚMERO da frase em vez de copiar o trecho: a resposta fica
muito menor (a análise fica mais rápida) e a evidência mostrada é sempre a frase real,
recortada pelo código, então não tem como ser inventada.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

# Pistas largas de cada dimensão: servem só para descartar frases que claramente não falam
# de nenhuma (história de quem deu o presente, saudações...). Na dúvida, a frase fica.
PISTAS = {
    "fixacao": r"fix|dur|hora|\bh\b|\d+ ?h\b|longev|evapor|some|sumi|perman|pele|roupa|dia (todo|inteiro)|"
               r"last|long|wear",
    "projecao": r"proje|rastro|sillage|espalh|colad|perto|discret|forte|fraco|intens|suave|ambiente|sente|"
                r"sentir|percebe|elevador|trail|beast",
    "cheiro": r"cheir|aroma|perfum|fragr|nota|doce|flor|fresc|amadeir|sint[eé]t|enjoa|lind|maravilh|delic|"
              r"horr|gost|amo|adoro|amei|smell|scent|baunilha|rosa|jasmim|c[ií]tric|almisc|[aâ]mbar",
    "versatilidade": r"vers[aá]t|ocasi|dia a dia|trabalho|escrit|noite|balada|festa|encontro|ver[aã]o|inverno|"
                     r"calor|frio|outono|primavera|esta[cç]|qualquer|uso|usar|casual|formal",
    "originalidade": r"[uú]nic|origin|diferent|clone|parecid|lembr|igual|gen[eé]ric|comum|inova|semelhan|"
                     r"dupe|similar|vers[aã]o",
    "custo_beneficio": r"pre[cç]|caro|barat|cust|vale|valor|pag|rend|investi|dinheiro|reais|r\$|centavo|"
                       r"promo|desconto|ml\b|frasco|price|worth",
    "elogios": r"elogi|coment|pergunt|notar|notaram|repar|ningu[eé]m|todos|gente|pessoas|marido|esposa|namor|"
               r"amig|m[aã]e|colega|compliment",
}
_PISTA = re.compile("|".join(f"(?:{p})" for p in PISTAS.values()), re.I)

_DIVISOR = re.compile(r"(?<=[.!?…])\s+|\n+")


def dividir(texto: str, min_palavras: int = 3) -> list[str]:
    """Frases da review. Pedaços muito curtos ("Amei!") são juntados à frase anterior."""
    frases: list[str] = []
    for parte in _DIVISOR.split(texto or ""):
        parte = parte.strip()
        if not parte:
            continue
        if frases and len(parte.split()) < min_palavras:
            frases[-1] = f"{frases[-1]} {parte}"
        else:
            frases.append(parte)
    if len(frases) > 1 and len(frases[0].split()) < min_palavras:  # primeira curta: junta à seguinte
        frases[1] = f"{frases[0]} {frases[1]}"
        frases.pop(0)
    return frases


def relevante(frase: str) -> bool:
    return bool(_PISTA.search(frase))


@dataclass
class ReviewPreparada:
    numero: int                       # 1..N na chamada ao modelo
    review_id: int
    frases: list[str]                 # todas as frases (índice 0 = frase 1)
    enviadas: list[int] = field(default_factory=list)   # números (1..) das frases mostradas ao modelo

    def frase(self, n: int) -> str | None:
        return self.frases[n - 1] if 1 <= n <= len(self.frases) and n in self.enviadas else None


def preparar(reviews: list[dict], filtrar: bool = True, orcamento_caracteres: int | None = None,
             inicio: int = 1) -> list[ReviewPreparada]:
    """Numera reviews e frases. Com `filtrar`, só vão ao modelo as frases com alguma pista
    (reviews sem nenhuma ficam fora da chamada, mas continuam contando como lidas).
    `orcamento_caracteres` corta a lista quando o texto enviado passaria do contexto."""
    saida, total = [], 0
    for i, r in enumerate(reviews):
        frases = dividir(r["texto_limpo"])
        enviadas = [n for n, f in enumerate(frases, start=1) if not filtrar or relevante(f)]
        tamanho = sum(len(frases[n - 1]) for n in enviadas)
        if orcamento_caracteres is not None and saida and total + tamanho > orcamento_caracteres:
            break
        total += tamanho
        saida.append(ReviewPreparada(numero=inicio + i, review_id=r["review_id"], frases=frases, enviadas=enviadas))
    return saida


def formatar(preparadas: list[ReviewPreparada]) -> str:
    blocos = []
    for p in preparadas:
        if not p.enviadas:
            continue
        linhas = "\n".join(f"[{p.numero}.{n}] {escape(p.frases[n - 1])}" for n in p.enviadas)
        blocos.append(f'<review numero="{p.numero}">\n{linhas}\n</review>')
    return "\n".join(blocos)


def ler_id_frase(texto: str) -> tuple[int, int] | None:
    """'3.2' -> (3, 2)."""
    m = re.fullmatch(r"\s*\[?(\d+)\.(\d+)\]?\s*", str(texto))
    return (int(m.group(1)), int(m.group(2))) if m else None
