.PHONY: up down test logs

up:
	docker compose up --build -d

down:
	docker compose down

test:
	cd backend && ../.venv/bin/python -m pytest -q || python -m pytest -q

logs:
	docker compose logs -f
