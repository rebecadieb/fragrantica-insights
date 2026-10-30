# Fragrantica Insights

Pipeline de dados de ponta a ponta que **coleta reviews de perfumes todos os dias**, ingere de forma **incremental via Kafka** em um **PostgreSQL** e oferece uma aplicação **Streamlit** em que o usuário escolhe **qualquer perfume do Fragrantica** (pelo catálogo ou colando o link). Se as reviews daquele perfume ainda não estão no banco, ou estão desatualizadas, o app pede a coleta na hora pelo Kafka. Em seguida, um modelo de **GenAI** lê as reviews e entrega:

- uma **nota de 0 a 10** por dimensão de uso real: fixação/durabilidade, projeção, qualidade do cheiro, versatilidade, originalidade, custo-benefício e elogios;
- um **resumo** de cada dimensão, com **trechos reais das reviews** como evidência (conferidos automaticamente);
- o **perfil de cliente ideal**: para quem é indicado, para quem não é, ocasiões, estações e período do dia.

> Projeto final da Especialização em Engenharia de Dados. O documento completo de desenvolvimento (arquitetura, decisões e validação) acompanha este repositório.

---

## Sumário

1. [Para avaliar o projeto (sem chave de API)](#para-avaliar-o-projeto-sem-chave-de-api)
1. [Arquitetura](#arquitetura)
2. [Estrutura do repositório](#estrutura-do-repositório)
3. [Pré-requisitos](#pré-requisitos)
4. [Testar de ponta a ponta (com chave da API)](#testar-de-ponta-a-ponta-com-chave-da-api)
5. [Dados reais: de onde vêm](#dados-reais-de-onde-vêm)
6. [Coleta diária incremental](#coleta-diária-incremental)
7. [Kafka: tópicos e garantias](#kafka-tópicos-e-garantias)
8. [Banco de dados](#banco-de-dados)
9. [A análise com GenAI](#a-análise-com-genai)
    - [IA open source com Ollama](#ia-open-source-com-ollama)
    - [Analista automático (análises em lote)](#analista-automático-análises-em-lote)
10. [A aplicação Streamlit](#a-aplicação-streamlit)
11. [Configuração (.env)](#configuração-env)
12. [Testes e validação](#testes-e-validação)
13. [Rodando sem Docker](#rodando-sem-docker)
14. [Solução de problemas](#solução-de-problemas)
15. [Limitações e uso responsável](#limitações-e-uso-responsável)

---

## Arquitetura

```mermaid
flowchart LR
    subgraph Fontes
        A[Notebook Colab<br/>CSVs opcionais]
        B[Site Fragrantica]
    end
    subgraph Ingestão
        C[producer carga-csv<br/>opcional]
        D[coletor agendado<br/>catálogo + coleta diária 03:00]
    end
    subgraph Kafka
        T1[(fragrantica.produtos)]
        T2[(fragrantica.reviews)]
        T4[(fragrantica.pedidos)]
        T5[(fragrantica.pedidos.status)]
        T3[(fragrantica.dlq)]
    end
    E[consumer<br/>validação + ETL + upsert]
    F[(PostgreSQL<br/>produtos · reviews · analises)]
    G[Streamlit<br/>filtros + análise]
    H[LLM: Ollama open source ou Claude<br/>extração + síntese]
    I[analista<br/>análises em lote, cede a vez ao app]

    A --> C
    B --> D
    C --> T1 & T2
    D --> T1 & T2
    G -- pedido de coleta --> T4
    T4 --> D
    D --> T5
    T1 & T2 & T4 & T5 --> E
    E -- inválidas --> T3
    E --> F
    F --> D
    F <--> G
    G <--> H
    F <--> I
    I --> H
```

| Serviço | O que faz | Tecnologia |
|---|---|---|
| `kafka` | Barramento de eventos entre a coleta e o banco (modo KRaft, sem ZooKeeper) | Apache Kafka 3.8 |
| `postgres` | Banco de registro: produtos, reviews, análises e log de ingestão | PostgreSQL 16 |
| `coletor` | Atende **na hora** os pedidos de coleta feitos pelo app e, todo dia, monta o catálogo e visita os perfumes em rodízio, publicando a ficha e **só as reviews novas** | SeleniumBase (modo UC) + Chrome + Xvfb |
| `carga` / `demo` | Opcionais: publicam no Kafka os CSVs que você já tem do Colab, ou os dados de exemplo | Python + pandas |
| `consumer` | Valida as mensagens, limpa o texto, deduplica e grava em lote; registra o andamento dos pedidos | confluent-kafka + psycopg |
| `analista` | Gera as análises de IA sozinho, um perfume por vez (os mais populares primeiro), e **pausa quando alguém pede uma análise no app** | Python + Ollama |
| `app` | Escolha do perfume, pedido de coleta sob demanda e análise com IA | Streamlit + Ollama (open source) ou Anthropic SDK |

**Fluxo híbrido.** A coleta diária mantém atualizados os perfumes mais populares, que são os mais procurados, e eles respondem na hora. Qualquer outro perfume, ou um com reviews de mais de 7 dias, é coletado sob demanda quando o usuário pede a análise. A coleta sob demanda busca só as reviews mais recentes que a análise usa (`LLM_MAX_REVIEWS`, 100 por padrão), que são as que a análise usa, para a espera ficar em 1 a 2 minutos.

**Por que Kafka aqui?** A coleta (lenta, sujeita a bloqueios) fica desacoplada da gravação e do app: o coletor só publica eventos; se o banco cair, as mensagens esperam no tópico (retenção de 7 dias) e o consumer retoma do último offset confirmado. Qualquer nova fonte (outro site, outro notebook) entra publicando no mesmo contrato, sem tocar no consumer.

---

## Estrutura do repositório

```
fragrantica-insights/
├── docker-compose.yml          # pilha completa + perfis de carga/demonstração/observabilidade
├── Dockerfile                  # imagem do consumer, da carga e do app
├── Dockerfile.coleta           # imagem do coletor (Chrome + Xvfb)
├── Makefile                    # atalhos (make up, make demo, make carga...)
├── .env.example                # todas as configurações, comentadas
├── requirements.txt / requirements-coleta.txt
├── sql/                        # 001 tabelas e view, 002 pedidos de coleta, 003 uso do LLM
├── src/fraginsights/
│   ├── config.py               # leitura das variáveis de ambiente
│   ├── models.py               # contratos das mensagens do Kafka (Pydantic)
│   ├── links.py                # leitura de links de perfume do Fragrantica
│   ├── bus.py                  # producer Kafka idempotente + barramento em memória (testes)
│   ├── producer.py             # catalogo | coletar | agendado | carga-csv
│   ├── consumer.py             # Kafka -> validação -> ETL -> PostgreSQL
│   ├── analista.py             # análises de IA em lote, cedendo a vez ao app
│   ├── etl.py                  # limpeza de texto, datas, hash de deduplicação, pseudonimização
│   ├── db.py                   # todo o SQL do projeto
│   ├── scraper/
│   │   ├── fragrantica.py      # coletor incremental (Selenium)
│   │   └── js.py               # seletores (JavaScript executado na página)
│   ├── llm/
│   │   ├── taxonomia.py        # as 7 dimensões analisadas
│   │   ├── schemas.py          # saída estruturada exigida do LLM
│   │   ├── prompts.py          # prompts dos 2 estágios (versionados)
│   │   ├── clientes.py         # Claude (tool use) e modo demonstração
│   │   ├── frases.py           # frases numeradas, filtro por pistas
│   │   └── analise.py          # modos rápido e detalhado, agregação, cache
│   └── app/
│       ├── streamlit_app.py    # interface
│       ├── fluxo.py            # quando coletar antes de analisar; acompanhamento do pedido
│       └── formatacao.py
├── scripts/validacao.py        # amostra para rotulagem humana + precisão/recall/kappa
├── notebooks/                  # notebook do Colab usado na coleta inicial
├── data/
│   ├── exemplo/                # dados FICTÍCIOS para demonstração e testes
│   └── input/                  # coloque aqui os CSVs reais (ignorado pelo git)
└── tests/                      # 41 testes (pytest)
```

---

## Pré-requisitos

- **Docker Desktop** (ou Docker Engine + Compose v2) com ~4 GB de RAM livres.
- Para **gerar** análises, um modelo de IA, à escolha:
    - **Claude (padrão, pago):** uma chave da API da Anthropic, com créditos, criada em platform.claude.com → *API Keys*.
    - **Open source, grátis:** o [Ollama](https://ollama.com) instalado na sua máquina, com o Qwen 2.5 3B. Mais lento e com texto mais simples; veja [IA open source com Ollama](#ia-open-source-com-ollama).
    - Sem chave, o app abre em **modo leitura**: mostra as análises já salvas e coleta reviews, mas não gera análises novas.
- Opcional: Python 3.11+ para rodar os testes fora do Docker.

> Em Mac com Apple Silicon, o serviço `coletor` roda emulado (`platform: linux/amd64`), porque o Google Chrome para Linux só existe em amd64. Funciona, mas é mais lento.

---

## Para avaliar o projeto (sem chave de API)

O repositório traz uma cópia dos perfumes coletados, das reviews e das análises geradas pelo Claude (`data/snapshot/fragrantica.sql.gz`). Para ver tudo funcionando, não é preciso chave nem coleta:

```bash
git clone <url-do-repositorio> fragrantica-insights
cd fragrantica-insights
cp .env.example .env          # deixe ANTHROPIC_API_KEY vazia
docker compose up -d --build
make restaurar                # carrega a cópia no banco; mostra quantos perfumes e análises entraram
```

Abra **http://localhost:8501**. Sem chave, o app abre em **modo leitura**:
- os perfumes analisados aparecem em *Buscar no catálogo*, com as análises completas (notas, confiança, perfil de cliente ideal e os trechos de evidência de cada dimensão);
- colar o link de outro perfume e clicar em **Coletar reviews agora** mostra a coleta sob demanda funcionando (app → Kafka → coletor → Kafka → consumer → PostgreSQL), sem custo;
- só o botão de gerar análises novas fica desligado. Com uma chave no `.env` (`ANTHROPIC_API_KEY=...`) e `docker compose up -d app`, ele volta.

Sem `make`: `gunzip -c data/snapshot/fragrantica.sql.gz | docker compose exec -T postgres psql -q -U fragrantica -d fragrantica`.

**Para atualizar a cópia** (quem desenvolve): `make remover-exemplo` (tira os dados fictícios) e depois `make exportar`. A cópia leva o texto das reviews coletadas, com os autores pseudonimizados; por isso, o repositório deve ser privado.

---

## Testar de ponta a ponta (com chave da API)

```bash
git clone <url-do-repositorio> fragrantica-insights
cd fragrantica-insights
cp .env.example .env
open -e .env                  # Mac; em outros sistemas, abra o .env no editor
```

No `.env`, preencha **uma** linha com a chave (sem aspas e sem espaços; se a chave aparecer em duas linhas, vale a última):

```
ANTHROPIC_API_KEY=sk-ant-api03-...
```

```bash
docker compose up -d --build  # sobe kafka, postgres, consumer, coletor, analista e app
docker compose ps             # todos "Up" (o consumer pode reiniciar nos primeiros segundos)
make restaurar                # opcional: carrega as análises que vêm no repositório
```

1. Abra **http://localhost:8501**. A barra lateral deve mostrar `Provedor de IA: anthropic · claude-sonnet-5`, sem o aviso de modo leitura.
2. Escolha **Colar link do Fragrantica** e cole o link de um perfume, por exemplo `https://www.fragrantica.com.br/perfume/Carolina-Herrera/Good-Girl-39681.html`.
3. Clique em **Gerar análise**. O app pede a coleta pelo Kafka, mostra o andamento ("na fila", "coletando...", "coleta concluída") e gera a análise em seguida. Leva de 1 a 3 minutos no total.
4. Confira a análise: notas por dimensão com a confiança, perfil de cliente ideal e, abrindo cada dimensão, as frases das reviews que sustentam a nota. As abas **Reviews** e **Pipeline** mostram os dados coletados e o que entrou no banco.
5. Abra o mesmo perfume de novo: a análise aparece na hora, do cache, sem custo.

Para acompanhar por dentro: `docker compose logs -f app coletor consumer`.

### Demonstração com dados fictícios (opcional)

`make demo` carrega 3 perfumes **inventados** (marca "Casa Fictícia", 36 reviews), só para ver o fluxo sem coletar nada no site. Para tirá-los do banco depois: `make remover-exemplo`.

---

## Dados reais: de onde vêm

**Você não precisa de nenhum arquivo.** O próprio repositório coleta tudo no site, em três etapas encadeadas:

| Etapa | Comando | O que faz | Quando roda |
|---|---|---|---|
| 1. Catálogo | `producer catalogo` | Lê a seção "Marcas Mais Populares" (60 marcas) e lista os perfumes de cada uma (nome + link), cerca de 15 mil | Automaticamente na 1ª rodada se o banco estiver vazio; depois a cada `ATUALIZAR_CATALOGO_DIAS` (7) para achar lançamentos |
| 2. Ficha + reviews | `producer coletar` | Visita cada perfume: ficha completa e reviews novas | Todo dia no `HORARIO_COLETA`, 200 perfumes por rodada |
| 3. Gravação | `consumer` | Lê do Kafka e grava no PostgreSQL | Sempre ligado |
| Sob demanda | app → `fragrantica.pedidos` → coletor | Coleta o perfume escolhido no app que não está no banco ou está desatualizado | Na hora, quando alguém clica em *Gerar análise* |

Para começar do zero com dados reais:

```bash
make up                         # sobe tudo
make catalogo                   # monta o catálogo agora (~30 min para 60 marcas); MARCAS=5 make catalogo para testar
make coletar                    # coleta ficha + reviews de 20 perfumes agora (LIMITE=50 make coletar)
```

A partir daí, a coleta diária segue sozinha. Para não precisar dos dois últimos comandos, use `COLETAR_AO_INICIAR=true` no `.env`: o coletor monta o catálogo e faz a primeira rodada assim que sobe.

### Atalho opcional: aproveitar o que você já coletou no Colab

Se você já rodou os notebooks do Colab (pasta `notebooks/`), dá para carregar esse resultado e não repetir horas de coleta. Copie do Drive para `data/input/`, com estes nomes:

| Arquivo | Uma linha por | Efeito |
|---|---|---|
| `perfumes.csv` | perfume (catálogo) | Substitui a etapa 1 |
| `detalhes_perfumes.csv` | perfume (ficha) | Adianta parte da etapa 2 |
| `reviews.csv` | review | Já entra com todo o histórico de reviews |

Use os arquivos gerados com `MODO_TESTE = False`, sem `_teste` no nome. Pode ser qualquer subconjunto dos três. Depois rode:

```bash
make carga                      # = docker compose --profile carga run --rm carga
```

A carga pode rodar mais de uma vez sem duplicar nada. Linhas inválidas são registradas no log e puladas. As datas relativas ("a day ago") são interpretadas a partir da data de modificação do `reviews.csv`; baixar pelo Drive já preserva essa data.

---

## Carga em lote dos perfumes mais populares

Para popular o catálogo de uma vez (coleta + análise), use uma lista de links em ordem de prioridade, por exemplo os perfumes das marcas mais populares, na ordem do ranking das marcas. A seleção pega **2 perfumes de cada marca, na ordem do arquivo, até completar 80** (`POR_MARCA` e `TOTAL` mudam isso).

1. Salve o CSV em `data/input/populares.csv`. Ele só precisa ter uma coluna com os links do Fragrantica (de qualquer idioma do site); as outras colunas são ignoradas, e vírgula, ponto e vírgula ou tabulação funcionam.
2. Confira a seleção (não coleta nem gasta nada):
   ```bash
   make lote-ver
   ```
3. Colete (cerca de 1 minuto por perfume; o coletor atende os pedidos do app normalmente nesse meio-tempo):
   ```bash
   make lote-coletar
   ```
   Pode rodar de novo à vontade: quem foi coletado nos últimos `VALIDADE_COLETA_DIAS` é pulado, então uma segunda rodada só tenta os que falharam.
4. Gere as análises. Sem `SIM=1`, o comando só mostra quantas faltam e a estimativa de custo na API; com `SIM=1`, gera:
   ```bash
   make lote-analisar          # estimativa
   make lote-analisar SIM=1    # gera
   ```
   Perfumes que já têm análise em cache são pulados e não custam nada.

O número de reviews por análise segue `LLM_MAX_REVIEWS`. Com o Claude, 100 reviews custam cerca de US$ 0,05 a 0,10 por análise (estimativa; o valor real fica no painel da Anthropic) e ainda levam a maioria das dimensões à confiança alta.

### Remover os dados fictícios

Os três perfumes de demonstração (`make demo`, fonte `exemplo`) saem do banco, com reviews e análises, com:

```bash
make remover-exemplo
```

Os arquivos em `data/exemplo` continuam no repositório, porque os testes usam esses dados.

---

## Coleta diária incremental

O serviço `coletor` fica no ar e executa uma rodada **todo dia no `HORARIO_COLETA`** (padrão 03:00, fuso `TZ`).

**Quais perfumes entram na rodada:** até `MAX_PRODUTOS_POR_COLETA` (padrão 200), em rodízio: primeiro os que nunca foram visitados, depois os visitados há mais tempo e, entre eles, os mais populares primeiro. Com 200 por dia, cerca de 6 mil perfumes são revisitados a cada mês. Ajuste conforme o tempo de coleta que você aceita (cerca de 15 a 30 s por perfume).

**Como a coleta é incremental:** os `review_id` do site crescem com o tempo e a página lista as reviews das mais novas para as mais antigas. Para cada perfume, o coletor consulta no banco o maior `review_id` já gravado e **para de clicar em "mais resenhas" assim que alcança esse id**. Só as reviews novas são publicadas no Kafka. Na primeira visita de um perfume, ele carrega até `MAX_CLIQUES_REVIEWS` páginas (cerca de 7 reviews por clique).

Comandos úteis:

```bash
make coletar                       # força uma rodada agora com 20 perfumes (LIMITE=50 make coletar)
docker compose logs -f coletor     # acompanha a coleta
```

Para testar a coleta logo ao subir a pilha, use `COLETAR_AO_INICIAR=true` no `.env`.

---

## Kafka: tópicos e garantias

| Tópico | Chave | Valor | Produzido por |
|---|---|---|---|
| `fragrantica.produtos` | `id_perfume` | `ProdutoEvento` (catálogo ou ficha completa) | carga, coletor |
| `fragrantica.reviews` | `id_perfume` | `ReviewEvento` (texto bruto) | carga, coletor |
| `fragrantica.pedidos` | `id_perfume` | `PedidoColeta`: o app pede a coleta de um perfume | app |
| `fragrantica.pedidos.status` | `id_perfume` | `StatusPedido`: coletando → concluído (nº de reviews, maior id) ou erro | coletor |
| `fragrantica.dlq` | chave original | mensagem rejeitada + motivo | consumer |

- **Chave = id do perfume:** todas as mensagens de um perfume caem na mesma partição, o que preserva a ordem por produto.
- **Producer idempotente** (`enable.idempotence=true`, `acks=all`): reenvios por falha de rede não duplicam mensagens no broker.
- **Consumer *at-least-once*:** o offset só é confirmado depois do `COMMIT` no banco. Se o processo cair no meio de um lote, o lote é relido.
- **Banco idempotente:** `ON CONFLICT` no `review_id` e no `hash_conteudo` (produto + texto normalizado). Releituras viram "duplicadas" e não geram linhas novas. Na prática, o resultado final é o de *exatamente uma vez*.
- **Contrato validado** com Pydantic nas duas pontas. Uma mensagem fora do contrato vai para a DLQ e não trava o pipeline.
- **Retenção de 7 dias:** se o PostgreSQL ficar fora do ar, nada se perde nesse prazo.
- **Pedidos com dois consumidores:** o tópico de pedidos é lido pelo coletor (que coleta) e pelo consumer (que registra o pedido como "na fila"). Como os status podem chegar fora de ordem, a tabela só deixa o status avançar (na fila → coletando → concluído/erro). O app considera a coleta pronta quando a ficha **e** a review de maior id informada pelo coletor já estão no banco.

Painel do Kafka (tópicos, mensagens, *lag* do consumer): `make ui` e abra http://localhost:8080.

---

## Banco de dados

| Tabela | Papel |
|---|---|
| `produtos` | 1 linha por perfume. A ficha completa sobrescreve o catálogo, sem apagar campos já preenchidos. |
| `reviews` | 1 linha por review: **texto original preservado** + texto limpo, data interpretada, nº de palavras, flag `informativa` (≥ 5 palavras), hash de deduplicação e autor pseudonimizado. |
| `analises` | Cache das análises do LLM, com chave em produto + provedor + modelo + versão do prompt + **assinatura do conjunto de reviews**. |
| `ingestao_log` | Uma linha por lote do consumer: mensagens, reviews novas, duplicadas e rejeitadas. |
| `pedidos_coleta` | Andamento de cada pedido sob demanda: status, nº de reviews coletadas, mensagem de erro. |
| `uso_llm` | Quem está usando o modelo agora (`app` ou `analista`). É assim que o analista sabe que deve ceder a vez. |
| `vw_produtos` | View usada pelo app: produto + nº de reviews + data da review mais recente. |

Consultas rápidas (`make psql`):

```sql
SELECT nome, designer, n_reviews FROM vw_produtos ORDER BY n_reviews DESC LIMIT 10;
SELECT date_trunc('day', ingerido_em) AS dia, count(*) FROM reviews GROUP BY 1 ORDER BY 1 DESC;
SELECT * FROM ingestao_log ORDER BY executado_em DESC LIMIT 5;
```

---

## A análise com GenAI

### Dimensões

| Chave | Dimensão | O que mede |
|---|---|---|
| `fixacao` | Fixação / durabilidade | Quanto tempo o perfume fica perceptível na pele ou na roupa |
| `projecao` | Projeção e rastro | O quanto o cheiro se espalha e é percebido pelos outros |
| `cheiro` | Qualidade do cheiro | O quanto o cheiro em si agrada |
| `versatilidade` | Versatilidade | Clima, horário e ocasiões em que funciona |
| `originalidade` | Originalidade | Único versus genérico ou "clone" |
| `custo_beneficio` | Custo-benefício | Preço, rendimento e o que entrega |
| `elogios` | Elogios recebidos | Reação de outras pessoas |

"Durabilidade" e "fixação" ficaram na mesma dimensão porque os consumidores usam as duas palavras como sinônimos. Para mudar a taxonomia, edite `llm/taxonomia.py` e suba `VERSAO_PROMPT` em `llm/prompts.py`.

### Como a análise é gerada (dois modos)

| | Modo **detalhado** (padrão do app e do analista) | Modo **rápido** (opcional) |
|---|---|---|
| Chamadas ao modelo | 1 por lote de 10 reviews + 1 de síntese | 1 |
| Tempo medido com `qwen2.5:3b` num Mac (40 reviews) | 2 min 07 s a 3 min 32 s | 1 min 18 s a 2 min |
| Contagens | o modelo aponta cada menção, review por review | o modelo lista quais reviews elogiam e criticam cada dimensão |
| Resultado no teste | 98% das menções válidas, notas coerentes | 1ª tentativa sem nenhuma menção; 2ª com quase tudo em confiança baixa e notas genéricas (7 e 5) |

Com o `qwen2.5:3b`, o modo rápido economiza 1 a 2 minutos e perde muita qualidade: lendo 36 reviews de uma vez, o modelo pula a maioria. Por isso o padrão é o detalhado; o rápido fica como opção para modelos maiores (`MODO_ANALISE_APP=rapido`). Se o modo rápido não encontrar nenhuma menção, o app refaz sozinho no detalhado.

O que vale para os dois:

1. **Seleção:** até `LLM_MAX_REVIEWS` (100; 40 com o Ollama) reviews, priorizando as informativas e as mais recentes. No modo rápido com modelo local, a lista é cortada para caber no contexto (`OLLAMA_NUM_CTX`).
2. **Frases numeradas:** o código divide cada review em frases (`[3.2]` = review 3, frase 2). Com `FILTRAR_FRASES=true` (padrão), só vão ao modelo as frases com alguma pista de dimensão (horas, rastro, preço, elogio, clima...): "comprei na loja do shopping" nem é enviada. Menos texto, mais rápido.
3. **Evidência pelo número da frase:** o modelo não copia trechos; ele aponta o número da frase. A evidência mostrada é a frase real, recortada pelo código, então **não tem como ser inventada**. Isso resolveu o problema do segundo teste, em que o modelo de 3B copiava "dura o dia todo" das instruções. A resposta também fica muito menor (≈ 20 tokens por menção, contra ≈ 185 por review no formato anterior), e é isso que deixa a análise rápida: o tempo de um modelo local acompanha o número de tokens que ele escreve. Número de review ou de frase que não existe na entrada é descartado e contado ("N menções descartadas").
4. **Agregação feita pelo código:** contagem por dimensão (cada review conta uma vez), índice de 0 a 10 (positiva = 1, neutra = 0,5, negativa = 0) **suavizado** com 2 relatos neutros imaginários, para que poucos relatos não deem notas extremas (3 críticas dão 2,0, não 0; 40 elogios dão 9,8), e **confiança** (alta com ≥ 15 reviews, média com ≥ 5, baixa com ≥ 1). Frases repetidas aparecem uma vez só, alternando críticas e elogios.
5. **Texto final:** nota de cada dimensão (1 frase de resumo), pontos fortes e fracos, perfil de cliente ideal e resumo geral, em textos curtos. **O índice é a âncora da nota:** com menos de 5 relatos (confiança baixa) vale o índice; com mais, vale a nota do modelo, limitada a 2 pontos do índice, porque modelos pequenos exageram (dão 10 ou 0 com facilidade). A tela diz quando a nota veio do índice ou foi ajustada. Listas vazias são preenchidas pelas contagens. Dimensões sem menções ficam sem nota, sempre.
6. **Trava contra resumo inventado:** se nenhuma menção for encontrada, a análise **não é salva** e o app explica o motivo.
7. **Cache:** o resultado vai para a tabela `analises`, separado por provedor, modelo, versão do prompt **e modo**. O app mostra primeiro a análise do analista (com mais reviews), depois a detalhada do app e, por último, a rápida.

A saída é forçada para um JSON Schema (*tool use* no Claude, `format` no Ollama) e validada com Pydantic; uma saída inválida volta ao modelo uma vez, com o erro.

**Custo aproximado:** com 100 reviews, uma análise faz 3 chamadas de extração (lotes de 40) e 1 de síntese: pela estimativa, alguns centavos de dólar (o valor real aparece no painel da Anthropic, em Usage). Como a análise só roda quando alguém clica em **Gerar análise**, o custo acompanha o uso, e não o tamanho do catálogo.

### Provedores

| `LLM_PROVEDOR` | Modelo | Custo | Reviews por análise | Como a saída estruturada é garantida |
|---|---|---|---|---|
| `ollama` | Qwen 2.5 3B (open source), na sua máquina | grátis | até 40, lotes de 10, um por vez | parâmetro `format` com o JSON Schema + validação Pydantic |
| `anthropic` (padrão) | Claude Haiku 4.5 (extração) + Sonnet 5 (síntese) | ~US$ 0,05 a 0,10 por análise com 100 reviews (o dobro com 200) | até 100 (`LLM_MAX_REVIEWS`), lotes de 40, 4 em paralelo | *tool use* forçado + validação Pydantic |
| `demo` | heurística por palavras-chave (não é IA) | grátis | todas | — |

Os dois LLMs recebem os mesmos prompts, taxonomia e checagem de evidências, e as análises ficam separadas no cache por provedor e modelo. Para outro provedor (OpenAI, por exemplo), implemente uma classe com `gerar(modelo, sistema, usuario, schema)` em `llm/clientes.py` e registre em `criar_cliente`.

### IA open source com Ollama

O [Ollama](https://ollama.com) roda modelos open source na sua máquina, sem custo e sem chave. No Mac, ele usa a GPU do chip Apple, por isso é instalado **direto no Mac**, e não dentro do Docker.

1. Baixe e instale o Ollama em [ollama.com/download](https://ollama.com/download) e abra o app (o ícone da lhama aparece na barra de menu).
2. No terminal, baixe o modelo (cerca de 4,7 GB, uma vez só):
   ```bash
   ollama pull qwen2.5:3b
   ```
3. No `.env`, confira `LLM_PROVEDOR=ollama` (já é o padrão) e recarregue o app:
   ```bash
   docker compose up -d app
   ```
4. No app, a barra lateral mostra `Provedor de IA: ollama · qwen2.5:3b`. Se o Ollama estiver fechado ou o modelo não tiver sido baixado, aparece um aviso dizendo o que fazer.

**Máquina:** o Qwen 2.5 3B ocupa cerca de 2,3 GB e, num Mac com Apple Silicon, roda inteiro na GPU. O 7B (cerca de 4,7 GB) escreve melhor, mas num Mac de 8 a 16 GB parte dele vai para a CPU e fica bem mais lento. Ele pode valer a pena só na síntese (`OLLAMA_MODELO_SINTESE=qwen2.5:7b`), que é uma chamada por perfume.

**Tempo:** um modelo local é bem mais lento que a API. Por isso, com o Ollama a análise lê até 40 reviews, o app usa o modo rápido (uma chamada) e o modo detalhado roda em lotes de 10, uma chamada por vez, com um teto de saída por chamada (um modelo local às vezes "empaca" gerando espaços em branco; o teto impede que isso trave a análise). O andamento de cada chamada aparece em `docker compose logs -f app`. A primeira análise de um perfume leva alguns minutos, conforme a máquina. Depois, ela fica em cache e abre na hora.

**Qualidade:** um modelo de 3B a 7B segue bem o formato, mas tende a errar mais na polaridade e a copiar trechos menos fielmente do que o Claude. A interface mostra isso: cada dimensão diz quantas menções foram descartadas por apontarem uma review ou frase que não existe, e o rodapé mostra a % de menções válidas e o modo da análise. Modelos pequenos às vezes deixam a nota ou as listas da síntese vazias: nesse caso o código usa o índice calculado como nota (e avisa) e preenche pontos fortes, fracos e ocasiões a partir das contagens. Erros de português ("afixação") vêm do modelo de 3B; para melhorar só o texto final sem deixar a extração lenta, use `OLLAMA_MODELO_SINTESE=qwen2.5:7b`. Esse é um bom ponto de comparação para a validação do TCC (`scripts/validacao.py` funciona com qualquer provedor e nos dois modos).

**Linux ou Windows sem instalar o Ollama:** há um serviço opcional no compose (`docker compose --profile ollama up -d ollama`, depois `docker compose exec ollama ollama pull qwen2.5:3b` e `OLLAMA_URL_DOCKER=http://ollama:11434` no `.env`). No Mac, evite essa opção, porque dentro do Docker o modelo roda sem GPU.

### Analista automático (análises em lote)

Com um modelo local, cada análise leva minutos (≈ 8 min com 20 reviews e ≈ 16 min com 40, num Mac com `qwen2.5:3b`). Para o usuário não esperar, o serviço `analista` deixa as análises prontas antes:

1. Busca a **fila**: perfumes com pelo menos `MIN_REVIEWS_ANALISE` reviews informativas e **sem análise do modelo atual que cubra a review mais recente**. Os mais populares (mais reviews) primeiro. Dados fictícios ficam fora.
2. Analisa **um perfume por vez**, no modo detalhado (`MODO_ANALISE_ANALISTA`), lendo **até 100 reviews** (`ANALISTA_MAX_REVIEWS`; o app lê 40), e grava na mesma tabela `analises` que o app lê. Quando o usuário abrir esse perfume, a análise aparece na hora.
3. Quando chegam reviews novas pela coleta diária, o perfume volta para a fila sozinho. Fila vazia: dorme `ANALISTA_ESPERA_S` (300 s) e confere de novo.

**Cede a vez ao app.** Antes de *cada* chamada ao modelo (cada lote de 10 reviews e a síntese), o analista confere a tabela `uso_llm`. Se o app estiver gerando uma análise pedida por alguém, ele espera até ela terminar e só então continua. O pedido do usuário espera no máximo o trecho que já estava em andamento (um lote, cerca de 1 a 4 minutos), nunca a análise inteira.

| Variável | Padrão | Para quê |
|---|---|---|
| `ANALISTA_INICIO` / `ANALISTA_FIM` | vazio (o dia todo) | Janela de funcionamento, ex.: `22:00` e `07:00` (pode virar a noite) |
| `ANALISTA_MAX_REVIEWS` | 100 | Reviews lidas por perfume. Mais que o app (40), porque o analista não tem pressa: mais relatos, mais dimensões com confiança alta. O tempo cresce na mesma proporção (≈ 3 a 5 s por review) |
| `ANALISTA_MAX_POR_DIA` | 50 | Teto de análises por dia (conta também as feitas pelo app); `0` desliga |
| `ANALISTA_ESPERA_S` | 300 | Intervalo entre as conferências quando não há o que fazer |
| `ANALISTA_PERMITIR_PAGO` | `false` | Com `LLM_PROVEDOR=anthropic` o analista **fica desligado** para não gastar crédito, a não ser que você ligue |

```bash
make analista                        # acompanha: "Analisando X: 210 reviews; 37 na fila" / "Pronto X em 480 s"
docker compose stop analista         # pausa (a GPU fica livre)
docker compose start analista        # retoma de onde parou (a fila é recalculada no banco)
```

Perfumes analisados só no modo rápido (pelo app) também entram na fila, para ganhar a versão detalhada.

A aba **Pipeline** do app mostra quantos perfumes estão na fila e as análises geradas por dia.

Um perfume que falha (ex.: o modelo não identificou nenhuma menção) sai da fila por 24 h, para não travar os outros.

---

## A aplicação Streamlit

http://localhost:8501

- **Barra lateral, duas formas de escolher o perfume:**
    - *Buscar no catálogo:* filtros por marca, gênero e nome em todos os perfumes conhecidos, inclusive os que ainda não têm reviews ("sem reviews ainda").
    - *Colar link do Fragrantica:* qualquer perfume do site, de qualquer idioma (`fragrantica.com`, `.com.br`, `.es`...).
- **Gerar análise:**
    - Com reviews no banco e coletadas há menos de `VALIDADE_COLETA_DIAS` (7), a análise sai direto (ou na hora, se já existe em cache).
    - Sem reviews, fora do banco ou desatualizado: o app publica um pedido de coleta, mostra o andamento ("na fila", "coletando...", "coleta concluída: 84 reviews novas") e gera a análise em seguida. Se a coleta falhar (Cloudflare, link inválido), a mensagem do coletor aparece na tela.
    - O botão **Atualizar reviews** força uma coleta mesmo com reviews recentes.
- **Modo leitura (sem `ANTHROPIC_API_KEY`):** a barra lateral avisa; cada perfume mostra a análise salva mais recente, e o botão **Coletar reviews agora** coleta sem gerar análise. Para gerar, preencha a chave no `.env` e rode `docker compose up -d --force-recreate app`.
- **Cabeçalho:** nota e votos do site, reviews no banco, review mais recente, acordes e pirâmide olfativa.
- **Aba "Análise de uso real (IA)":** resumo, gráfico das notas por dimensão (cor = confiança), perfil de cliente ideal, pontos fortes e fracos e, em cada dimensão, o resumo, as contagens e os trechos de evidência.
- **Aba "Reviews":** tabela pesquisável com as reviews do perfume.
- **Aba "Pipeline":** totais do banco, reviews novas por dia, a fila do analista automático e as análises geradas por dia.

Todo conteúdo gerado traz o aviso de que foi produzido por IA, com o modelo, a data, o nº de reviews e a % de evidências conferidas.

---

## Configuração (.env)

| Variável | Padrão | Para que serve |
|---|---|---|
| `LLM_PROVEDOR` | `ollama` | `ollama`, `anthropic` ou `demo` |
| `OLLAMA_MODELO` | `qwen2.5:3b` | Modelo open source usado pelo Ollama |
| `OLLAMA_MODELO_SINTESE` | vazio (= `OLLAMA_MODELO`) | Modelo só para a síntese do modo detalhado (o texto que o usuário lê, 1 chamada por perfume); ex.: extração com `qwen2.5:3b` e síntese com `qwen2.5:7b`. O modo rápido usa sempre `OLLAMA_MODELO` |
| `OLLAMA_NUM_CTX` | `8192` | Tamanho do contexto: o lote de reviews e a resposta precisam caber; valores altos pesam na memória |
| `OLLAMA_MAX_REVIEWS` / `OLLAMA_REVIEWS_POR_LOTE` / `OLLAMA_PARALELO` | `40` / `10` / `1` | Limites mais leves para o modelo local |
| `OLLAMA_TIMEOUT_S` | `600` | Tempo máximo de cada chamada ao modelo local |
| `OLLAMA_URL_DOCKER` | `http://host.docker.internal:11434` | Onde o app (no Docker) encontra o Ollama |
| `ANTHROPIC_API_KEY` | — | Chave da API (obrigatória com `anthropic`) |
| `LLM_MODELO_EXTRACAO` | `claude-haiku-4-5-20251001` | Modelo do estágio 1 |
| `LLM_MODELO_SINTESE` | `claude-sonnet-5` | Modelo do estágio 2 |
| `LLM_REVIEWS_POR_LOTE` | `40` | Reviews por chamada de extração |
| `LLM_MAX_REVIEWS` | `100` | Máximo de reviews lidas por análise com a API (e coletadas sob demanda). 200 aumenta a confiança e dobra o custo |
| `MIN_REVIEWS_ANALISE` | `5` | Mínimo de reviews informativas para liberar a análise |
| `MODO_ANALISE_APP` | `detalhado` | Modo da análise pedida no app (`detalhado` ou `rapido`) |
| `MODO_ANALISE_ANALISTA` | `detalhado` | Modo do analista em lote |
| `FILTRAR_FRASES` | `true` | Envia ao modelo só as frases com pista de alguma dimensão |
| `TZ` / `HORARIO_COLETA` | `America/Fortaleza` / `03:00` | Quando roda a coleta diária |
| `COLETAR_AO_INICIAR` | `false` | Faz uma rodada (catálogo, se preciso, + coleta) assim que o coletor sobe |
| `MAX_MARCAS` | `60` | Marcas do ranking "Marcas Mais Populares" usadas no catálogo |
| `ATUALIZAR_CATALOGO_DIAS` | `7` | De quanto em quanto tempo o catálogo procura perfumes novos |
| `MAX_PRODUTOS_POR_COLETA` | `200` | Perfumes visitados por dia |
| `VALIDADE_COLETA_DIAS` | `7` | Reviews mais antigas que isso são atualizadas antes da análise |
| `ESPERA_COLETA_S` | `600` | Quanto o app espera uma coleta sob demanda; pedidos mais velhos que isso são descartados pelo coletor |
| `MAX_CLIQUES_REVIEWS` | `100` | Limite de "mais resenhas" por perfume |
| `SCRAPER_HEADLESS` | `false` | `false` = Chrome em tela virtual (passa melhor no Cloudflare) |
| `ANONIMIZAR_AUTORES` | `true` | Troca o nome do avaliador por um apelido estável |
| `MIN_PALAVRAS_INFORMATIVA` | `5` | Abaixo disso, a review não entra na análise |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:29092` | Endereço do Kafka fora do compose |
| `DATABASE_URL` | `postgresql://...@localhost:5432/...` | Banco fora do compose |

---

## Testes e validação

```bash
pip install -r requirements.txt
python -m pytest                     # 40 testes unitários + 1 de integração (pulado sem banco)

# integração contra o PostgreSQL do compose (APAGA as tabelas desse banco!)
docker compose up -d postgres
DATABASE_URL_TESTE=postgresql://fragrantica:fragrantica@localhost:5432/fragrantica python -m pytest -k integracao
```

Os testes cobrem a limpeza e as datas, os contratos das mensagens, o fluxo producer → barramento → consumer (inclusive reentrega e mensagens fora de ordem), a DLQ, o agendamento, a agregação e a checagem de evidências, a chamada ao Claude (com um SDK falso: *tool use* forçado e nova tentativa), a chamada ao Ollama (com um servidor HTTP falso: JSON Schema, contexto, nova tentativa, modelo não baixado), a interpretação das páginas (catálogo, ficha e reviews) e a coleta sob demanda (leitura de links, decisão de coletar, pedido, atendimento no coletor e detecção de que as reviews chegaram).

**Validação da IA contra rótulos humanos** (precisão, recall e kappa de Cohen por dimensão):

```bash
docker compose exec app python scripts/validacao.py amostra --id-perfume 31861 --n 100 --saida /tmp/validacao.csv
docker compose cp app:/tmp/validacao.csv .
# preencha as colunas humano_* sem olhar as colunas llm_*
python scripts/validacao.py avaliar validacao.csv
```

Para comparar os modos com os mesmos rótulos, refaça só as colunas `llm_*` no outro modo e avalie de novo:

```bash
docker compose cp validacao.csv app:/tmp/validacao.csv
docker compose exec app python scripts/validacao.py reclassificar /tmp/validacao.csv --id-perfume 31861 \
    --modo rapido --saida /tmp/validacao_rapido.csv
docker compose cp app:/tmp/validacao_rapido.csv .
python scripts/validacao.py avaliar validacao_rapido.csv
```

A diferença de recall entre os dois mostra quanto o modo rápido perde ao ler tudo de uma vez.

---

## Rodando sem Docker

Útil para desenvolver. Kafka e PostgreSQL continuam no compose:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-coleta.txt
docker compose up -d kafka postgres
export $(grep -v '^#' .env | xargs)       # usa localhost:29092 e localhost:5432
export PYTHONPATH=src

python -m fraginsights.consumer &                                  # terminal 1
python -m fraginsights.producer carga-csv --pasta data/exemplo --fonte exemplo
streamlit run src/fraginsights/app/streamlit_app.py
python -m fraginsights.producer catalogo --marcas 3                # usa o Chrome da sua máquina
python -m fraginsights.producer coletar --limite 5
```

Rodando na sua máquina, o coletor usa o seu IP residencial, que o Cloudflare bloqueia bem menos do que IPs de nuvem.

---

## Solução de problemas

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| App mostra "Não foi possível conectar ao banco" | PostgreSQL ainda subindo | `docker compose ps`; espere o `postgres` ficar *healthy* |
| Nenhum perfume no filtro | Banco vazio ou filtro alto | `make restaurar`, ou cole o link de um perfume; reduza *Mínimo de reviews* |
| `Nenhum produto com link no banco` no coletor | Catálogo ainda vazio | `make catalogo`, ou espere a rodada diária, que monta o catálogo sozinha |
| "O coletor não respondeu a tempo" no app | Coletor parado ou ocupado com o catálogo | `docker compose logs coletor`; pedidos são atendidos entre uma marca e outra, então pode haver espera |
| Consumer reinicia em loop | Kafka ainda não estava pronto | Normal nos primeiros segundos; confira `docker compose logs kafka` |
| `Cloudflare em ... (tentativa N)` no coletor | O site desafiou o IP | Aguarde; reduza `MAX_PRODUTOS_POR_COLETA`; rode o coletor na sua máquina |
| Coletor publica 0 reviews para perfumes populares | O seletor do site mudou | Ajuste `scraper/js.py`; o notebook do Colab tem uma célula de diagnóstico |
| Barra lateral diz "modo leitura" mesmo com a chave | Linha `ANTHROPIC_API_KEY=` vazia **depois** da linha com a chave (vale a última), `#` na frente, ou container não recriado | `grep -n ANTHROPIC_API_KEY .env \| cut -c1-35` deve mostrar uma linha só; depois `docker compose up -d --force-recreate app analista` |
| Erro de autenticação ao gerar | Credencial que não é chave de API | Crie em platform.claude.com → *API Keys* → *Create Key* (começa com `sk-ant-api03-`) |
| App quebra com `No module named ...` | Arquivos de versões diferentes misturados na pasta | Baixe o projeto completo de novo numa pasta limpa e copie só o `.env` |
| "O Ollama não respondeu" | App do Ollama fechado | Abra o Ollama no Mac; confira com `curl http://localhost:11434/api/tags` |
| "O modelo ... ainda não foi baixado" | Falta o `ollama pull` | `ollama pull qwen2.5:3b` (ou o modelo do seu `.env`) |
| Análise com Ollama muito lenta ou "levou mais de 10 min" | Máquina com pouca memória (o modelo vai para o swap) | Feche outros programas; `ollama ps` mostra o uso; use `OLLAMA_MODELO=qwen2.5:3b` (e `ollama pull qwen2.5:3b`) ou reduza `OLLAMA_MAX_REVIEWS` para 20 |
| Análise falha com erro de schema | Saída do modelo fora do contrato mesmo após a nova tentativa | Gere de novo; se persistir, reduza `LLM_REVIEWS_POR_LOTE` |
| Mudou o SQL e nada aconteceu | Os scripts só são reaplicados quando o consumer reinicia | `docker compose restart consumer`. Os arquivos são idempotentes (só criam o que falta); para recriar do zero, `docker compose down -v` apaga os dados. |

---

## Limitações e uso responsável

- **Termos de uso do site:** respeite o `robots.txt` e os termos do Fragrantica. Use a coleta para fins acadêmicos, com ritmo baixo (pausas entre páginas e um limite diário de perfumes).
- **Privacidade:** os nomes dos avaliadores são pseudonimizados por padrão, e o texto das reviews é usado apenas para análise agregada.
- **Conteúdo gerado por IA:** notas e resumos podem conter erros. Por isso a interface mostra as contagens, a confiança e os trechos originais, e marca evidências que não foram encontradas literalmente.
- **Representatividade:** quem escreve reviews não representa todos os consumidores, e perfumes com poucas reviews recebem confiança baixa.
- **Dependência do HTML do site:** mudanças no layout podem quebrar o coletor. Os seletores estão isolados em `scraper/js.py`.
