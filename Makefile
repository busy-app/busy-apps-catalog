.PHONY: e2e
ENV ?= dev
e2e:
	python -m pytest e2e/scenarios --env=$(ENV) --report=e2e-report.md $(E2E_ARGS)
