all: .libs offers

.PHONY: offers
offers:
	wget -N -r -nH https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/index.json \
		https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/index.json \
		https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/index.json \
		https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonElastiCache/current/index.json

venv:
	virtualenv -p python3 venv

.libs: requirements.txt venv
	./venv/bin/pip install -r requirements.txt
	touch .libs
