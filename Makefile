# Atalhos. Sem make? Os comandos equivalentes estão no README.
.PHONY: setup up down demo carga catalogo coletar logs teste ui psql analista lote-ver lote-coletar lote-analisar remover-exemplo exportar restaurar

setup:            ## cria o .env a partir do exemplo
	@test -f .env || cp .env.example .env
	@echo "Edite o .env (ANTHROPIC_API_KEY) e rode: make up"

up: setup         ## sobe a pilha completa
	docker compose up -d --build

down:             ## para tudo (os dados do PostgreSQL ficam no volume)
	docker compose down

demo:             ## carrega os dados FICTÍCIOS de exemplo
	docker compose --profile demo run --rm demo

carga:            ## carrega os CSVs reais de data/input
	docker compose --profile carga run --rm carga

catalogo:         ## descobre no site os perfumes das marcas populares (MARCAS=60 por padrão)
	docker compose exec coletor python -m fraginsights.producer catalogo --marcas $${MARCAS:-60}

coletar:          ## força uma rodada de coleta agora (LIMITE=20 por padrão)
	docker compose exec coletor python -m fraginsights.producer coletar --limite $${LIMITE:-20}

logs:             ## acompanha os logs do pipeline
	docker compose logs -f consumer coletor analista

teste:            ## roda os testes (precisa de Python + requirements instalados)
	python -m pytest

ui:               ## painel do Kafka em http://localhost:8080
	docker compose --profile observabilidade up -d kafka-ui

analista:         ## acompanha o analista automático de IA
	docker compose logs -f analista

# Carga em lote: coloque o CSV em data/input (padrão: data/input/populares.csv).
CSV ?= populares.csv
TOTAL ?= 80
POR_MARCA ?= 2

lote-ver:         ## mostra os perfumes que o lote vai pegar (CSV=populares.csv TOTAL=80 POR_MARCA=2)
	docker compose exec app python -m fraginsights.lote ver /dados/$(CSV) --total $(TOTAL) --por-marca $(POR_MARCA)

lote-coletar:     ## coleta ficha e reviews dos perfumes do lote (leva ~1 min por perfume)
	docker compose exec coletor python -m fraginsights.lote coletar /dados/$(CSV) --total $(TOTAL) --por-marca $(POR_MARCA)

lote-analisar:    ## mostra a estimativa de custo; com SIM=1, gera as análises do lote
	docker compose exec app python -m fraginsights.lote analisar /dados/$(CSV) --total $(TOTAL) --por-marca $(POR_MARCA) $(if $(SIM),--sim,)

remover-exemplo:  ## apaga do banco os perfumes fictícios de demonstração
	docker compose exec app python -m fraginsights.lote remover-exemplo

# Cópia dos dados para o repositório: quem baixar o projeto vê as análises sem chave da API.
SNAPSHOT = data/snapshot/fragrantica.sql.gz

exportar:         ## salva perfumes, reviews e análises em data/snapshot (rode antes: make remover-exemplo)
	@mkdir -p data/snapshot
	docker compose exec -T postgres pg_dump -U fragrantica -d fragrantica --data-only --inserts \
		--on-conflict-do-nothing -t produtos -t reviews -t analises | gzip > $(SNAPSHOT)
	@docker compose exec -T postgres psql -U fragrantica -d fragrantica -Atc \
		"SELECT count(*) || ' perfumes, ' || (SELECT count(*) FROM reviews) || ' reviews, ' || (SELECT count(*) FROM analises) || ' análises exportados' FROM produtos"

restaurar:        ## carrega data/snapshot no banco (pode rodar mais de uma vez; não duplica)
	gunzip -c $(SNAPSHOT) | docker compose exec -T postgres psql -q -U fragrantica -d fragrantica > /dev/null
	@docker compose exec -T postgres psql -q -U fragrantica -d fragrantica -c \
		"SELECT setval(pg_get_serial_sequence('analises', 'id'), COALESCE((SELECT max(id) FROM analises), 1))" > /dev/null
	@docker compose exec -T postgres psql -U fragrantica -d fragrantica -Atc \
		"SELECT 'No banco: ' || count(*) || ' perfumes, ' || (SELECT count(*) FROM reviews) || ' reviews, ' || (SELECT count(*) FROM analises) || ' análises' FROM produtos"

psql:             ## abre o psql no banco
	docker compose exec postgres psql -U fragrantica -d fragrantica
