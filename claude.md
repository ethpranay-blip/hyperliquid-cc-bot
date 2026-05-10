# CC Portal Copy Trading Bot

## What This Is
A Python bot (NiceGUI dashboard + background async loop) that **polls** the
Corgi Calls (CC) member portal activity feed at portal.corgicalls.com and
mirrors structured trade signals (entry, SL, TP, direction, sizing) onto
Hyperliquid via the SDK. Portal auth is email + password session cookies
(persisted in SQLite). Dashboard at port 8080 with live PnL, active-trade
cards, activity feed, and an Auto-Mode toggle.

**Status: deployed on Railway and running** (mainnet, real funds when
DRY_RUN=false). Webhook-listener / VPS / Discord-OAuth language from earlier
plans is obsolete — there is no inbound webhook.

## Quick Start (local dev)
```bash
pip install -r requirements.txt --break-system-packages
cp env.example .env       # fill in PORTAL_USER/PASSWORD + HL_WALLET_ADDRESS/PRIVATE_KEY
python -m app.main        # starts dashboard at http://localhost:8080
```

For Railway deployment specifics see [RAILWAY_DEPLOYMENT.md](RAILWAY_DEPLOYMENT.md)
and [RAILWAY_SETUP_SUMMARY.md](RAILWAY_SETUP_SUMMARY.md).

## Architecture
See [docs/SYSTEMS.md](docs/SYSTEMS.md) for the full architecture map.

## Key Decisions
- **Polling, not webhooks** — the portal exposes no push channel; bot polls
  `/api/portal/me/activity-feed` every ~3s with session cookies.
- **Portal auth** — email/password POST to `/api/portal/login`; cookies stored
  in `db.portal_cookies` and reloaded on startup; auto re-login on 401.
- **Atomic bracket entry** — entry IOC + SL trigger sent in one `bulk_orders`
  call (`grouping="normalTpsl"`); SL updates are cancel-by-cloid + replace.
- **Auto-trailing stops** — after every partial TP, the bot moves the SL on HL
  itself (TP1/TP2 → BE, TP3 → TP1 price, TP4 → TP2 price) instead of waiting
  for a portal `stop_update` event that may never come.
- **Unified-account margin** — `get_available_margin` queries `spot_user_state`
  (USDC total − hold), not `user_state`, since unified accounts report zero
  balance on per-perp-dex user states.
- **Fixed-size scaling** — portal signals carry a $100 tracking size; bot uses
  `HL_MARGIN_USD × HL_LEVERAGE` for real notional.
- **STALE guard** — events older than bot startup (minus 5 min slack) appear
  on the dashboard but cannot be entered. Override with `FORCE_ENTER_TIDS`.
- **Fail-loud supervisor** — Apr 28 outage was a silent poll-loop death; the
  supervisor in `main.py` now respawns on ANY exit and `portal.poll` raises
  rather than returning silently.
- **Reconcile safety** — May 1 wipe was caused by `open_positions()` returning
  `[]` on a transient API failure; it now returns `None` on failure and the
  reconciler refuses to clean DB state until 2 consecutive empties on a
  populated DB.

## Active Work
- [ ] Continue documenting newer scripts (`diagnostics_check.py`, `cleanup_stale_trades.py`, dedup checks) under docs/
- [ ] Persist TP1/TP2 prices on `hl_opened_trades` (or read from `hl_tp_updates`) — TP3/TP4 auto-trail currently no-ops because `opened.get("tp1"/"tp2")` is always None
- [ ] Decide on Phase-3 enhancements (TP-trigger pre-placement on HL vs portal-driven partial closes)

> Earlier "build webhook listener / map portal fields → HL params /
> document payload" tasks are **complete and superseded** by the running
> implementation in `app/portal.py` + `app/hyperliquid_client.py`.

## Session Ritual
START: `Read claude.md and docs/SYSTEMS.md to get full context. Confirm architecture before we begin.`

END:
```
Before we finish:
1. Update docs/SYSTEMS.md — reflect any changes from this session
2. Update the relevant docs/<component>.md with what changed and why
3. Update Active Work above — check done items, add next tasks
4. Remind me to git push and archive this session
Documentation only — no code changes.
```
