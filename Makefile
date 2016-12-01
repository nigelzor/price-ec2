all: .libs

venv:
	virtualenv -p python3 venv

.libs: requirements.txt venv
	./venv/bin/pip install -r requirements.txt

