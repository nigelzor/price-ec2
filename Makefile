all: .libs

.libs: pyproject.toml uv.lock
	uv sync
	touch .libs

.PHONY: ci
ci: lint test

.PHONY: fix
fix: .libs
	uv run ruff format
	uv run ruff check --fix

.PHONY: lint
lint: .libs
	uv run ruff check
	uv run ruff format --check

.PHONY: test
test: .libs
	uv run python3 -m doctest price_ec2.py
