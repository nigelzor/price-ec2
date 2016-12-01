all: .libs offers/ec2.json

offers/index.json:
	wget https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/index.json -O $@

offers/ec2.json:
	wget https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/index.json -O $@

venv:
	virtualenv -p python3 venv

.libs: requirements.txt venv
	./venv/bin/pip install -r requirements.txt
	touch .libs
