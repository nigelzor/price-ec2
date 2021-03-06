all: .libs

PATH := $(PWD)/venv/bin:$(PATH)
SHELL := env PATH=$(PATH) /bin/bash

venv:
	python3 -m venv venv

requirements.txt: requirements-to-freeze.txt venv
	pip install -r requirements-to-freeze.txt --upgrade
	pip freeze -r requirements-to-freeze.txt > requirements.txt

.libs: requirements.txt venv
	pip install -r requirements.txt
	touch .libs

.PHONY: ci
ci: lint

.PHONY: lint
lint:
	flake8 --ignore=E501 price-ec2.py
