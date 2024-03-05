all: .libs

PATH := $(PWD)/venv/bin:$(PATH)
SHELL := env PATH=$(PATH) /bin/bash

venv:
	python3 -m venv venv
	pip install --upgrade pip setuptools wheel
	pip install -r requirements.txt -r requirements-dev.txt

requirements.txt: setup.py
	pip-compile

requirements-dev.txt: requirements-dev.in requirements.txt
	pip-compile requirements-dev.in

.libs: setup.py requirements.txt requirements-dev.txt venv
	pip-sync requirements.txt requirements-dev.txt
	pip install -e .
	touch .libs

.PHONY: ci
ci: lint test

.PHONY: lint
lint: .libs
	flake8 --ignore=E501 --exclude venv .

.PHONY: test
test: .libs
	python3 -m doctest price_ec2.py
