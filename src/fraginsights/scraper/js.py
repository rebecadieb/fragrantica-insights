"""Trechos de JavaScript executados dentro da página do Fragrantica.

Rodar a extração no navegador (um único execute_script) é bem mais rápido do que
dezenas de find_elements. Os seletores ficam isolados aqui: se o site mudar o
HTML, este é o único arquivo a ajustar.
"""

JS_EXTRAIR = r'''
const txt = el => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
const h1 = txt(document.querySelector('h1'));

// Acordes principais: links para /accords-search/
const acordes = [...new Set(
  [...document.querySelectorAll('a[href*="/accords-search/"]')].map(txt).filter(Boolean)
)];

// Perfumistas: links para /narizes/
const perfumistas = [...new Set(
  [...document.querySelectorAll('a[href*="/narizes/"]')].map(txt).filter(Boolean)
)];

// Pirâmide: percorre a página em ordem; cada título "Notas de ..." define o nível
// e os links /notas/ seguintes entram nesse nível.
const niveis = {topo: [], coracao: [], base: [], gerais: []};
let nivel = null;
const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
while (walker.nextNode()) {
  const el = walker.currentNode;
  if (el.children.length === 0 || /^H[1-6]$/.test(el.tagName)) {
    const t = txt(el).toLowerCase();
    if (/^notas de topo$/.test(t)) nivel = 'topo';
    else if (/^notas de cora[cç][aã]o$/.test(t)) nivel = 'coracao';
    else if (/^notas de (base|fundo)$/.test(t)) nivel = 'base';
  }
  if (el.tagName === 'A' && (el.getAttribute('href') || '').includes('/notas/')) {
    const nome = txt(el);
    if (!nome) continue;
    const alvo = niveis[nivel || 'gerais'];
    if (!alvo.includes(nome)) alvo.push(nome);
  }
}

return {
  h1: h1,
  corpo: document.body.innerText,
  acordes: acordes,
  perfumistas: perfumistas,
  notas: niveis,
};
'''

JS_REVIEWS = r'''
const limpa = s => (s || '').replace(/\s+/g, ' ').trim();
const reData = /(\b(\d+|an?)\s+\w+\s+ago|h[áa]\s+(\d+|um|uma)\s+\w+|\d+\s+\w+\s+atr[áa]s|\d{1,2}\/\d{1,2}\/\d{2,4})/i;
const ignorar = /^(compartilhar|share|leia mais|ler mais|read more|mostrar mais|mostrar menos|responder|denunciar)$/i;
const out = [];
for (const corpo of document.querySelectorAll('div[id^="review_"]')) {
  // sobe até o maior bloco que ainda contém só esta resenha (o "card")
  let card = corpo;
  while (card.parentElement && card.parentElement !== document.body &&
         card.parentElement.querySelectorAll('div[id^="review_"]').length === 1) {
    card = card.parentElement;
  }
  const paragrafos = [...corpo.querySelectorAll('p')].map(p => limpa(p.textContent)).filter(Boolean);
  const texto = paragrafos.length ? paragrafos.join('\n') : limpa(corpo.textContent);
  // folhas de texto do card que estão fora do corpo da resenha
  const folhas = [...card.querySelectorAll('*')]
    .filter(e => !corpo.contains(e) &&
                 [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()))
    .map(e => limpa([...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ')))
    .filter(Boolean);
  const links = [...card.querySelectorAll('a')].filter(a => !corpo.contains(a))
    .map(a => limpa(a.textContent)).filter(t => t && !ignorar.test(t));
  const autor = links[0] || folhas.find(t => !reData.test(t) && !ignorar.test(t)) || '';
  const data = folhas.find(t => reData.test(t)) || '';
  const tags = folhas.filter(t => t !== autor && t !== data && t.length <= 30 && !ignorar.test(t));
  out.push({
    review_id: corpo.id.replace('review_', ''),
    autor: autor,
    data_texto: data,
    tags: [...new Set(tags)].join('; '),
    texto: texto,
  });
}
return out;
'''

JS_BOTAO_MAIS = r'''
const alvos = [...document.querySelectorAll('button, a, span, div')].filter(el => {
  if (el.children.length > 2 || el.offsetParent === null) return false;
  const t = (el.textContent || '').trim().toLowerCase();
  if (t.length > 40) return false;
  const href = el.getAttribute('href');
  if (href && !href.startsWith('#') && !href.startsWith('javascript')) return false;
  return /(mais|todas as|ver) (resenhas|reviews)|^(mostrar|carregar|ver) mais$|^(load|show) more/.test(t);
});
if (!alvos.length) return false;
alvos[alvos.length - 1].scrollIntoView({block: 'center'});
alvos[alvos.length - 1].click();
return true;
'''

JS_CONTAR_REVIEWS = "return document.querySelectorAll('div[id^=review_]').length"

# Menor review_id carregado na página (as reviews vêm das mais novas para as mais antigas).
JS_MENOR_REVIEW_ID = r'''
const ids = [...document.querySelectorAll('div[id^="review_"]')]
  .map(e => parseInt(e.id.replace('review_', ''), 10)).filter(n => !isNaN(n));
return ids.length ? Math.min(...ids) : null;
'''

JS_ROLAR_ATE_REVIEWS = r'''
const a = document.querySelector('#all-reviews, [id*=review], [name=all-reviews]');
if (a) a.scrollIntoView({block: 'start'});
'''

# Links que vêm DEPOIS do título "Marcas Mais Populares", em ordem de documento.
JS_LINKS_MARCAS_POPULARES = r'''
const titulo = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,div,span,p')]
  .find(e => e.children.length === 0 && /Marcas Mais Populares/i.test(e.textContent || ''));
const links = [...document.querySelectorAll('a[href*="/desenhista/"]')]
  .filter(a => !titulo || (titulo.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING));
return links.map(a => ({href: a.href, texto: (a.textContent || '').trim(), titulo: a.getAttribute('title') || ''}));
'''

JS_LINKS_PERFUMES = r'''
return [...document.querySelectorAll('a[href*="/perfume/"]')]
  .map(a => ({href: a.href, texto: (a.innerText || a.textContent || '').trim()}));
'''
