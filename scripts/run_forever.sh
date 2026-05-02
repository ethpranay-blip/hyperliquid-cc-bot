#!/usr/bin/env bash
# ============================================================
# scripts/run_forever.sh
# ============================================================
# Auto-restart wrapper for the Corgi copy trading bot.
#
# Runs `python -m app.main` in a while-loop. If the bot exits for ANY
# reason (clean exit, crash, OOM, asyncio meltdown), this script logs the
# restart, waits RESTART_DELAY seconds, and relaunches.
#
# This is the outer layer of defense:
#   - Inside the bot:  portal_poll_supervisor (respawns the poll task)
#                      heartbeat_loop          (alerts via webhook)
#   - Outside the bot: THIS SCRIPT             (relaunches the process)
#
# Usage:
#   ./scripts/run_forever.sh                  # foreground
#   nohup ./scripts/run_forever.sh &          # background (Makefile does this)
#
# Environment overrides:
#   PYTHON           interpreter (default: CLI-tools python3)
#   STDOUT_LOG       where bot stdout/stderr goes (default: /tmp/corgi-bot.stdout.log)
#   RESTART_LOG      where this script's restart events go (default: /tmp/corgi-bot.restarts.log)
#   RESTART_DELAY    seconds to wait between exit and respawn (default: 5)
#
# Stop: send SIGTERM to this script's PID. The trap below propagates to
# the bot child via SIGTERM, waits up to 10s, then SIGKILL.
# ============================================================

set -u

# Anchor to project root regardless of where this is invoked from
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-/Library/Developer/CommandLineTools/usr/bin/python3}"
STDOUT_LOG="${STDOUT_LOG:-/tmp/corgi-bot.stdout.log}"
RESTART_LOG="${RESTART_LOG:-/tmp/corgi-bot.restarts.log}"
RESTART_DELAY="${RESTART_DELAY:-5}"

# Track the python child so we can forward signals
bot_pid=

cleanup() {
    local sig="${1:-TERM}"
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] wrapper received $sig — stopping bot" \
      | tee -a "$RESTART_LOG"
    if [ -n "$bot_pid" ] && kill -0 "$bot_pid" 2>/dev/null; then
        kill -TERM "$bot_pid" 2>/dev/null || true
        # Wait up to 10s for graceful shutdown
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$bot_pid" 2>/dev/null || break
            sleep 1
        done
        kill -KILL "$bot_pid" 2>/dev/null || true
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] wrapper exiting" \
      | tee -a "$RESTART_LOG"
    exit 0
}

trap 'cleanup TERM' TERM
trap 'cleanup INT'  INT
trap 'cleanup HUP'  HUP

# ── main loop ──────────────────────────────────────────────
restart_count=0
while true; do
    restart_count=$((restart_count + 1))
    ts="$(date '+%Y-%m-%d %H:%M:%S %Z')"
    if [ "$restart_count" -eq 1 ]; then
        echo "[$ts] starting bot (start #1, wrapper-PID=$$)" \
          | tee -a "$RESTART_LOG"
    else
        echo "[$ts] RESTART #$restart_count — relaunching bot" \
          | tee -a "$RESTART_LOG"
    fi

    # Launch python in the background so we can capture its PID and trap signals
    "$PYTHON" -m app.main >> "$STDOUT_LOG" 2>&1 &
    bot_pid=$!
    echo "[$ts] bot PID=$bot_pid (wrapper PID=$$)" \
      | tee -a "$RESTART_LOG"

    # Wait for the bot to exit. `wait` returns the child's exit code; if a
    # signal interrupts wait, the trap fires cleanup() above which exits.
    wait "$bot_pid"
    exit_code=$?
    bot_pid=  # cleared so cleanup() doesn't try to kill an already-dead PID

    ts="$(date '+%Y-%m-%d %H:%M:%S %Z')"
    if [ "$exit_code" -eq 0 ]; then
        echo "[$ts] bot exited cleanly (code 0); respawn in ${RESTART_DELAY}s" \
          | tee -a "$RESTART_LOG"
    else
        echo "[$ts] bot CRASHED (code $exit_code); respawn in ${RESTART_DELAY}s" \
          | tee -a "$RESTART_LOG"
    fi
    sleep "$RESTART_DELAY"
done
