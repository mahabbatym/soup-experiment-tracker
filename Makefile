.PHONY: setup test load up down
setup:
	python -m pip install -r requirements-dev.txt
	python -m playwright install chromium
test:
	python -m pytest -vv -ra
load:
	python scripts/load_probe.py
up:
	docker compose up --build
down:
	docker compose down
