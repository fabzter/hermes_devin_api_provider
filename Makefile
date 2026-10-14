PY ?= python3

.PHONY: test serve cutover install launchd

test:
	$(PY) -m tests.test_protocol

serve:
	$(PY) -m devin_api_provider serve

cutover:
	$(PY) scripts/remove_devin_acp.py --apply

install:
	bash install.sh

launchd:
	bash scripts/install_launchd.sh
