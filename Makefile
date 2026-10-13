.PHONY: run test scan

run:
	python -m moviexfone

test:
	pytest

scan:
	ruff check src tests
	ruff format --check src tests
	mypy src
	bandit -r src -ll
	pip-audit
	pytest
