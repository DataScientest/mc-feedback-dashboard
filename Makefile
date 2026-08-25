.PHONY: up down logs clean

up:
	docker compose up -d --build

down:
	docker compose down

# Journaux applicatifs (tracebacks des erreurs 500, actualisations, scheduler).
logs:
	docker compose logs -f --tail=400

clean:
	docker system prune -a --volumes -f
