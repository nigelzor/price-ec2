all: .libs offers

PATH := $(PWD)/venv/bin:$(PATH)
SHELL := env PATH=$(PATH) /bin/bash

.PHONY: offers
offers:
	wget --compression=auto -N -r -nH https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/index.json \
		https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/index.json \
		https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/index.json \
		https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonElastiCache/current/index.json

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
