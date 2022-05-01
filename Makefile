all: .libs

PATH := $(PWD)/venv/bin:$(PATH)
SHELL := env PATH=$(PATH) /bin/bash

venv:
	python3 -m venv venv
	pip install --upgrade pip setuptools wheel
	pip install -r requirements.txt -r requirements-dev.txt

requirements.txt: requirements.in
	pip-compile requirements.in

requirements-dev.txt: requirements-dev.in
	pip-compile requirements-dev.in

.libs: requirements.txt requirements-dev.txt venv
	pip-sync requirements.txt requirements-dev.txt
	touch .libs

.PHONY: ci
ci: lint

.PHONY: lint
lint: .libs
	flake8 --ignore=E501 --exclude venv .
