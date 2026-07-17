# Single surface for running the geopolitics trading system.
# `make help` lists everything. Every target uses configs/geopolitics/
# discovery.yaml (override: make status CONFIG=path/to/other.yaml) and
# sources .env for secrets via bin/geo.

CONFIG ?= configs/geopolitics/discovery.yaml
GEO := bin/geo
PY := .venv/bin/python

.DEFAULT_GOAL := help

help: ## show this list
	@grep -E '^[a-z][a-zA-Z_-]*:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-16s %s\n", $$1, $$2}'
	@echo ""
	@echo "  Any other CLI command: bin/geo <command> --config $(CONFIG) ..."

# ---- setup ----

setup: ## create venv, install deps, seed .env
	test -d .venv || python3 -m venv .venv
	.venv/bin/pip install --quiet -r requirements.txt
	@test -f .env || (cp .env.example .env && chmod 600 .env && echo ">>> created .env -- FILL IN YOUR KEYS before running")
	@echo "setup complete. next: edit .env, then 'make paper'"

test: ## run the full test suite
	$(PY) -m pytest -q tests/

# ---- running (paper is the default posture) ----

paper: ## run the fleet: discover/grade/scan/alert + paper bots, cannot trade
	$(GEO) run-fleet --config $(CONFIG)

paper-once: ## one paper fleet cycle, then exit (smoke test)
	$(GEO) run-fleet --config $(CONFIG) --once

live: ## run the fleet LIVE (requires config armed live + I_UNDERSTAND_LIVE_TRADING=yes)
	@test "$(I_UNDERSTAND_LIVE_TRADING)" = "yes" || \
		(echo "refusing: run as 'make live I_UNDERSTAND_LIVE_TRADING=yes'"; \
		 echo "and first set fleet.position_mode: live + auto_ack: true in $(CONFIG)"; exit 1)
	$(GEO) run-fleet --config $(CONFIG) --live

# ---- controls ----

halt: ## MASTER KILL: stop all execution mid-cycle, everywhere
	$(GEO) set-fleet-mode --mode off

watch-only: ## fleet keeps watching + alerting but never trades
	$(GEO) set-fleet-mode --mode alert_only

arm: ## clear the master switch back to live (per-market gates still apply)
	$(GEO) set-fleet-mode --mode live

# ---- observability ----

status: ## the 3am view: positions, heartbeats, ledger, drawdown headroom, scan
	$(GEO) fleet-status --config $(CONFIG)

funnel: ## where edge died across the whole universe
	$(GEO) funnel-report --config $(CONFIG)

calibration: ## are the probability sources beating the market? (Brier report)
	$(GEO) calibration-report --config $(CONFIG)

reconcile: ## ledger hygiene: free dead position slots, roll stale buckets
	$(GEO) reconcile-ledger --config $(CONFIG)

# ---- change validation (run before arming any prompt/config change) ----

# usage: make replay BOT=configs/geopolitics/generated/x.yaml ARTICLES=logs/binary_articles.jsonl
replay: ## rerun archived articles through the full pipeline (isolated, dry-run)
	@test -n "$(BOT)" -a -n "$(ARTICLES)" || (echo "usage: make replay BOT=<bot.yaml> ARTICLES=<articles.jsonl>"; exit 1)
	$(GEO) replay --config $(BOT) --articles $(ARTICLES)

# usage: make eval BOT=configs/geopolitics/generated/x.yaml
eval: ## adversarial regression cases; nonzero exit = the change regressed
	@test -n "$(BOT)" || (echo "usage: make eval BOT=<bot.yaml> [CASES=<cases.jsonl>]"; exit 1)
	$(GEO) eval-classifier --config $(BOT) --cases $(or $(CASES),configs/geopolitics/eval-cases/binary-adversarial.jsonl)

# ---- data safety ----

backup: ## snapshot data/ (ledger, journals, calibration, acks)
	deploy/backup.sh

.PHONY: help setup test paper paper-once live halt watch-only arm status funnel calibration reconcile replay eval backup
