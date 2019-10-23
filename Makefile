all: .libs

PATH := $(PWD)/venv/bin:$(PATH)
SHELL := env PATH=$(PATH) /bin/bash

venv:
	virtualenv -p python3 venv

.libs: requirements.txt venv
	pip install -r requirements.txt
	touch .libs

.PHONY: ci
ci: lint

.PHONY: lint
lint:
	flake8 --ignore=E501 price-ec2.py
