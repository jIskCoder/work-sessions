.PHONY: test coverage

PYTHON ?= python3

test:
	$(PYTHON) -m unittest discover -s tests

coverage:
	$(PYTHON) -m coverage erase
	$(PYTHON) -m coverage run --source=app,import_monday -m unittest discover -s tests
	$(PYTHON) -m coverage report --show-missing
	$(PYTHON) -m coverage html -d coverage-report
