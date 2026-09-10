.PHONY: help demo test lint bench export clean

help:
	@echo "make demo    - reconcile a generated book and write docs/report.html"
	@echo "make test    - run the test suite"
	@echo "make lint    - ruff"
	@echo "make bench   - 25 independent books, report the metric spread"
	@echo "make export  - write the synthetic book to data/*.csv"

demo:
	PYTHONPATH=src python3 -m recon demo --out docs/report.html --json docs/metrics.json

test:
	PYTHONPATH=src python3 -m pytest

lint:
	ruff check src tests

bench:
	PYTHONPATH=src python3 -m recon bench --seeds 25 --json docs/bench.json

export:
	PYTHONPATH=src python3 -m recon export --out data

clean:
	rm -rf data .pytest_cache **/__pycache__ docs/report.html docs/metrics.json docs/bench.json
