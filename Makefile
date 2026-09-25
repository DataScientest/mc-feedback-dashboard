.PHONY: up down logs visits clean

# Classement des pages vues. Filtres optionnels : make visits DAYS=30 TOP=20
DAYS ?=
TOP ?= 15

up:
	docker compose up -d --build

down:
	docker compose down

# Journaux applicatifs (tracebacks des erreurs 500, actualisations, scheduler).
logs:
	docker compose logs -f --tail=400

visits:
	docker compose exec slack-bot python -m backend.visit_stats --top $(TOP) $(if $(DAYS),--days $(DAYS))

clean:
	docker system prune -a --volumes -f
