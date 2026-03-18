.PHONY: setup launch test reset-cache relaunch

setup:
	python3 -m venv .venv
	. .venv/bin/activate && python -m pip install -U pip && python -m pip install -e .

launch:
	bash scripts/launch_local.sh

test:
	. .venv/bin/activate && python -m pytest -q

reset-cache:
	. .venv/bin/activate && PYTHONPATH=src python scripts/reset_runtime.py

relaunch:
	bash scripts/launch_local.sh
