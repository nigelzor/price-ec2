all: .libs

.libs: pyproject.toml poetry.lock
	poetry install
	touch .libs

.PHONY: ci
ci: lint test

.PHONY: fix
fix: .libs
	poetry run ruff format
	poetry run ruff check --fix

.PHONY: lint
lint: .libs
	poetry run ruff check
	poetry run ruff format --check

.PHONY: test
test: .libs
	poetry run python3 -m doctest price_ec2.py
