# ============================================================
# Corgi Copy Trading Bot — task runner
# ============================================================
# Usage:
#   make start     — launch the bot in the background
#   make stop      — gracefully stop the running bot
#   make restart   — stop then start
#   make logs      — tail -f app.log
#   make status    — show whether bot is running + port info
#   make deploy    — git pull + restart (for VPS use)
#
# Environment overrides (optional):
#   make start PYTHON=/path/to/python3
#   make start PID_FILE=/tmp/mybot.pid
# ============================================================

PYTHON      ?= /Library/Developer/CommandLineTools/usr/bin/python3
MODULE      ?= app.main
PID_FILE    ?= .bot.pid
STDOUT_LOG  ?= /tmp/corgi-bot.stdout.log
RESTART_LOG ?= /tmp/corgi-bot.restarts.log
APP_LOG     ?= app.log
STOP_WAIT   ?= 10
WRAPPER     ?= ./scripts/run_forever.sh

.PHONY: help start stop restart logs status deploy

help:
	@echo "Corgi Copy Trading Bot — available targets:"
	@echo ""
	@echo "  make start    launch bot in background (PID → $(PID_FILE))"
	@echo "  make stop     graceful SIGTERM, then SIGKILL after $(STOP_WAIT)s"
	@echo "  make restart  stop + start"
	@echo "  make logs     tail -f $(APP_LOG)"
	@echo "  make status   show running state, PID, uptime, port"
	@echo "  make deploy   git pull --ff-only + restart"
	@echo ""
	@echo "Override defaults: make start PYTHON=/usr/local/bin/python3"

# ------------------------------------------------------------
# start — launch bot in background via run_forever wrapper
# ------------------------------------------------------------
# The PID we save is the WRAPPER's, not the python's. Killing the wrapper
# (via `make stop`) sends SIGTERM, which the wrapper's trap catches and
# propagates to the python child for graceful shutdown.
start:
	@if [ -f "$(PID_FILE)" ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "✗ already running (wrapper PID $$(cat $(PID_FILE)))"; exit 1; \
	fi
	@rm -f "$(PID_FILE)"
	@if [ ! -x "$(WRAPPER)" ]; then \
		echo "✗ wrapper not executable: $(WRAPPER)"; exit 1; \
	fi
	@echo "Starting bot via $(WRAPPER) (auto-restart on crash)..."
	@nohup env PYTHON="$(PYTHON)" STDOUT_LOG="$(STDOUT_LOG)" \
	    RESTART_LOG="$(RESTART_LOG)" \
	    "$(WRAPPER)" > /dev/null 2>&1 & \
	  echo $$! > $(PID_FILE)
	@sleep 2
	@if kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "✓ started — wrapper PID $$(cat $(PID_FILE))"; \
		echo "  stdout : $(STDOUT_LOG)"; \
		echo "  restart log: $(RESTART_LOG)"; \
		echo "  app log: $(APP_LOG)"; \
		echo "  dashboard: http://localhost:8080"; \
	else \
		echo "✗ wrapper failed to start — inspect $(RESTART_LOG)"; \
		rm -f "$(PID_FILE)"; \
		tail -n 10 $(RESTART_LOG) 2>/dev/null; \
		exit 1; \
	fi

# ------------------------------------------------------------
# stop — SIGTERM, wait up to $(STOP_WAIT)s, then SIGKILL
# ------------------------------------------------------------
stop:
	@if [ ! -f "$(PID_FILE)" ]; then \
		echo "○ not running (no $(PID_FILE))"; exit 0; \
	fi
	@pid=$$(cat $(PID_FILE)); \
	if ! kill -0 $$pid 2>/dev/null; then \
		echo "○ PID $$pid already dead — cleaning PID file"; \
		rm -f "$(PID_FILE)"; exit 0; \
	fi; \
	echo "Stopping PID $$pid ..."; \
	kill -TERM $$pid; \
	i=0; \
	while [ $$i -lt $(STOP_WAIT) ]; do \
		if ! kill -0 $$pid 2>/dev/null; then \
			echo "✓ stopped gracefully"; rm -f "$(PID_FILE)"; exit 0; \
		fi; \
		sleep 1; \
		i=$$((i + 1)); \
	done; \
	echo "⚠ did not exit after $(STOP_WAIT)s — sending SIGKILL"; \
	kill -KILL $$pid 2>/dev/null || true; \
	rm -f "$(PID_FILE)"

# ------------------------------------------------------------
# restart — stop then start
# ------------------------------------------------------------
restart:
	@$(MAKE) --no-print-directory stop
	@sleep 1
	@$(MAKE) --no-print-directory start

# ------------------------------------------------------------
# logs — tail the structured app log
# ------------------------------------------------------------
logs:
	@touch $(APP_LOG)
	@echo "tailing $(APP_LOG) — Ctrl-C to exit"
	@tail -n 50 -f $(APP_LOG)

# ------------------------------------------------------------
# status — show running state + port + uptime
# ------------------------------------------------------------
status:
	@if [ -f "$(PID_FILE)" ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		pid=$$(cat $(PID_FILE)); \
		uptime=$$(ps -p $$pid -o etime= 2>/dev/null | tr -d ' '); \
		echo "● running  PID $$pid  uptime $$uptime"; \
		port=$$(lsof -iTCP:8080 -sTCP:LISTEN -n -P 2>/dev/null | awk 'NR==2 {print "listening (" $$1 " " $$2 ")"}'); \
		if [ -n "$$port" ]; then \
			echo "  port 8080: $$port"; \
		else \
			echo "  port 8080: NOT listening (bot may still be starting)"; \
		fi; \
		echo "  stdout : $(STDOUT_LOG)"; \
		echo "  app log: $(APP_LOG)"; \
	else \
		echo "○ not running"; \
		rm -f "$(PID_FILE)" 2>/dev/null || true; \
	fi

# ------------------------------------------------------------
# deploy — git pull + restart (VPS helper)
# ------------------------------------------------------------
deploy:
	@echo "▶ git pull --ff-only"
	@git pull --ff-only
	@echo ""
	@echo "▶ restarting bot"
	@$(MAKE) --no-print-directory restart
	@echo ""
	@echo "✓ deploy complete"
	@$(MAKE) --no-print-directory status
