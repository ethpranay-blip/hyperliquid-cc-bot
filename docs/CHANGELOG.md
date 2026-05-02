# Changelog

All changes in chronological order from the build session.

Format: each item notes **what** + **why** (the bug it fixed or the gap it filled).

---

## Phase 1 — Foundation

### 1. `app/db.py` built
SQLite layer with WAL mode, foreign keys ON, thread-local connections (one sqlite3 connection per thread). Seven tables: `hl_live_trades`, `hl_opened_trades`, `hl_closed_trades`, `hl_sl_updates`, `hl_tp_updates`, `portal_events`, `portal_cookies`. ISO-8601 UTC timestamps on every row.

### 2. `app/portal.py` built (v0 — email/password)
Async httpx client, `POST /api/auth/login`, session cookies persisted to `portal_cookies`, 401 → re-login → retry-once. Event parser emitted 4 typed dicts: `new_trade`, `stop_update`, `tp_hit`, `full_close`. Caller whitelist filtered via `ALLOWED_CALLERS` env with once-per-trade-id "ignoring" log.

### 3. `app/hyperliquid_client.py` built
SDK wrapper with mandatory `round_px(px, sz_decimals)`, deterministic Cloids (`Cloid.from_str(f"0x{trade_id:032x}")`), atomic bracket via `bulk_orders(grouping="normalTpsl")`, reduce-only IOC closes, partial TP, k-coin prefix + stop scaling, HIP-3 dex support, allMids WebSocket auto-reconnect, DRY_RUN mode, exp-backoff retry (transient only, max 3), post-close fill reconciliation via `user_fills_by_time()`.

### 4. `app/notifier.py` built
Webhook dispatcher. Auto-detects Discord vs Telegram from URL shape; disabled silently if `NOTIFY_WEBHOOK_URL` unset. Every call wrapped in try/except — never crashes the bot.

### 5. `app/main.py` built
NiceGUI dashboard at port 8080 with stats header, auto-mode toggle, DRY_RUN banner, active trade cards, historic table, activity feed sidebar. Rotating log handler (5MB × 3). Event router wired to all 4 handler types.

### 6. PnL sign-format bug (build-time)
`_fmt_pnl(-12.3)` produced `"$-12.30"` instead of `"-$12.30"`. Fixed in both `main.py` and `notifier.py` by separating sign char from absolute value.

---

## Phase 2 — Configuration + Deployment

### 7. `.env` loaded via python-dotenv
Added `load_dotenv()` at top of `app/main.py` BEFORE any module reads env (notifier reads `NOTIFY_WEBHOOK_URL` at import time). `python-dotenv>=1.0.0` was already in `requirements.txt`.

---

## Phase 3 — Portal auth iteration

### 8. First-contact login kept returning 401
With `PORTAL_USER=pranayyyy`, `PORTAL_PASSWORD=Pranay6320`, POST `/api/auth/login` → 401 `{"error":"Invalid password"}`. The path and field name were wrong.

### 9. Brief detour: v1 API-key rewrite
Temporarily rewrote `portal.py` to use `/api/v1/` endpoints with `x-api-key` header. All endpoints returned 401 uniformly regardless of path, confirming the portal uses session auth not API keys. Rewrite reverted.

### 10. Correct endpoint discovered via reference repo
Cloned `eleazarpin/corgicalls-automation-app` for comparison. Its `portal.py` uses `POST /api/portal/login` (not `/api/auth/login`) with JSON field `username` (not `email`). Two-line fix applied. Login returned HTTP 200.

### 11. `PORTAL_USER` value corrected
User confirmed their handle is `pranay` not `pranayyyy`. `.env` updated by user; login succeeded.

---

## Phase 4 — HL SDK + network corrections

### 12. Adaptive `perp_dexs` probe
HL SDK init crashed on testnet with `KeyError: 'cash'`. Testnet only hosts `["", "xyz", "flx"]`; mainnet has all four. Added `_probe_available_dexs(base_url, candidates)` that POSTs `/info {"type":"meta"}` per candidate dex and keeps only the ones that return a valid `universe` array. Default dex `""` always included as fallback.

### 13. First testnet API sub-wallet key rejected
HL returned `User or API Wallet 0xc284985d... does not exist`. That key derived to an address not registered on testnet. User generated a testnet-specific API sub-wallet and swapped the key in `.env`. Orders started submitting correctly.

### 14. `ui.html(...)` crashed on newer NiceGUI
Dashboard returned HTTP 500 with `__init__() missing 1 required keyword-only argument: 'sanitize'`. Fixed by passing `sanitize=False` with a `TypeError` fallback for older NiceGUI versions.

### 15. `ui.notify()` exploded in auto-mode
Notify calls from the poll-loop task have no UI client context bound. Introduced `_safe_notify(msg, type)` wrapper that always logs and swallows NiceGUI context errors.

### 16. `bulk_orders` rejected single-order submissions
When `portal_stop` was `None`, the bot submitted just `[entry_order]` with `grouping="normalTpsl"`. HL rejected: `Unexpected number of trigger orders`. Fixed by computing `grouping = "normalTpsl" if len(orders) > 1 else "na"`.

### 17. SL updates failed with "Cannot modify canceled or filled order"
`exchange.modify_order()` doesn't work on resting trigger orders. Reference repo pattern: list `frontend_open_orders`, filter to the trade's SL by cloid + coin + `isTrigger`, `bulk_cancel` by oid, then place a fresh SL with the same cloid. Implemented via new `_cancel_sls_for_trade()` helper.

### 18. Portal missed SOL #582 close event (race bug)
Portal's activity feed returns events newest-first. On restart with both open+close for same `trade_id` in backlog, close was processed first (no-op'd because trade wasn't yet in `hl_live_trades`) and marked as seen. Then open executed — position opened on HL and never closed. Fixed by sorting events **chronologically** (ascending timestamp) in `portal.fetch_new_events()`. SOL #582 manually reconciled on HL + DB.

### 19. Restart replayed closed trades ("opened SOL again after closing")
On fresh-process restart, `_seen_event_ids` resets. Portal backlog still contains old opens. `handle_new_trade` only dedup'd against `hl_live_trades` (empty after close). Added `db.get_closed_trade(trade_id)` check — any trade_id already in `hl_closed_trades` is skipped on replay.

---

## Phase 5 — Portal enrichment

### 20. `trade_opened` events lack stop/TP/leverage
Activity-feed entries for new trades only carry `coin`, `side`, `tradeId`, `caller`, `entryRaw`. No stop_loss. No takeProfits. No leverage.

**Fix:** new `PortalClient.get_trade_detail(trade_id)` method. `POST /api/portal/me/trades {"tradeId": N}` returns the full trade object inline (including `stop`, `tp`, `entryUsed`, `leverage`, `originalStop`). Handles 409 "already following" with a `GET /api/portal/me/trades` fallback. 404/400 on already-closed trades logged at DEBUG (not WARNING) to avoid log noise.

### 21. Enrichment merged into event dict
New `_enrich_event_from_detail()` in `main.py` — merges portal detail fields into the event without clobbering non-null event values. Called in `handle_new_trade` before auto-mode fires.

### 22. Parser upgrades for real Corgi payload shape
- Dumped one real activity-feed response → inspected field names
- Added `entryRaw` as fallback for `entryPrice` in `new_trade` event
- Added `trade_updated` event type with `updateType="stop_moved"` (parses price from `updateText` via regex)
- Added `trade_updated` + `updateType="tp_hit"` handling
- `bet_opened` / `bet_closed` now silently skipped (they use `betId` not `tradeId` and aren't trading-actionable)

---

## Phase 6 — HIP-3 (SILVER) asset resolution

### 23. SILVER trade #601 rejected: "not found on any dex"
HIP-3 assets on HL appear in the per-dex `universe` as `"xyz:SILVER"`, not plain `"SILVER"`. The old `resolve_asset` compared strictly against `hl_coin`. Fix touched 7 surfaces:

- `resolve_asset()` now returns both `hl_coin` (logical) and `order_name` (HL-addressable, prefixed for HIP-3)
- `open_trade`, `_close_common`, `update_stop`, `_cancel_sls_for_trade` all submit orders with `coin=order_name`
- `_current_position_size` queries `user_state` per-dex and matches by `order_name`
- `open_positions()` aggregates across default + all active HIP-3 dexes
- WS `_ws_loop` subscribes to `allMids` per HIP-3 dex so `xyz:SILVER` price ticks land in the shared prices dict
- `get_price_for_pricing()` looks up by `hl_coin` first, then `order_name`, then per-dex REST fallback
- `main.py` DB writes still store the bare portal coin; `reconcile_on_startup` strips `"dex:"` prefix when diffing HL vs DB

### 24. After fix: SILVER order submitted correctly, HL returned "Trading is halted"
Market-state rejection (not a bot bug). Asset resolution now works end-to-end.

---

## Phase 7 — Mainnet safety

### 25. Mainnet API sub-wallet verified via `extraAgents`
Diagnostic script derived the address from `HL_PRIVATE_KEY` (never printed the key). `POST /info {"type":"extraAgents","user":main}` returned 3 agents, including one named `"Copytrading"` at the derived address — confirmed the key is registered.

### 26. Startup-time cutoff for stale backlog trades
On fresh DB, backlog replay would auto-enter every whitelisted trade in the feed (~11 trades at $500 notional each). Added `state.startup_time_ms` anchor + `STALE_SLACK_MS = 5min` + `_event_is_stale(event)` check in `handle_new_trade`. Stale events still appear as dashboard cards (so SL/TP/close routing still works) but cannot be entered by auto-mode or manual click. Added STALE badge + disabled Enter button on pending card.

### 27. `seed_closed_from_backlog()` on startup
Complement to the cutoff. Fetches the current activity feed once at startup, inserts every `trade_closed_*` trade_id into `hl_closed_trades` with `close_type='pre-seeded'` so the existing closed-trades dedup path blocks them. Idempotent. Added `'pre-seeded'` to the `close_type` CHECK constraint; `get_stats()` and `get_historic_trades()` filter it out.

### 28. Auto-mode seed from env
`AUTO_MODE=true|false` env var — bot boots with the flag pre-set. Previously always started OFF with in-memory toggle only.

### 29. Spot vs Perps balance discovery
User reported $0 balance. Diagnostic showed $321.57 in Spot USDC but $0 in Perps clearinghouse. User confirmed unified account (HyperCore) — Spot USDC acts as Perps margin automatically, so the $0 perps reading is cosmetic, not a blocker.

---

## Phase 8 — PnL accuracy

### 30. Dashboard PnL used limit price, not fill price
`open_trade` stored `entry_price = mid * 1.05` (for longs) as the DB entry_price. For a BTC long with mid=100k and real fill ≈100.05k, the DB stored 105k. Dashboard PnL = `(mid - 105000) * size` — overstates losses by the full 5% slippage cushion. Observed live: HYPE #602 displayed -$26.37 when actual HL upnl was -$1.42 (a $25 display error).

**Fix:**
- Schema: added `hl_opened_trades.my_fill_price REAL` (nullable)
- `_apply_migrations()` ALTER TABLE for pre-existing DBs
- `open_trade` calls `_reconcile_fills()` post-submit, populates `OpenResult.my_fill_price`
- `main.py` `enter_trade` forwards fill price to `db.insert_opened_trade`
- Card `_build_live_card` uses `my_fill_price` when set, falls back to `entry_price` otherwise; card label changes from `Entry:` to `Fill:` when real fill is known
- `db.update_opened_fill_price(tid, px)` helper for backfilling existing rows
- HYPE #602 retroactively backfilled from HL's reported `entryPx` (41.086) — dashboard now reads -$1.36, matching HL exactly

---

## Summary — bug counts

- **7 latent bugs caught + fixed** before any mainnet money was at risk (race, replay, SL update, grouping, HIP-3 naming, stale backlog, PnL basis)
- **3 config/auth corrections** (endpoint path, field name, sub-wallet key)
- **1 real-money event prevented**: the fresh-DB replay would have opened ~11 stale positions in seconds the moment a valid key was pasted; caught by the wallet-auth rejection, then properly guarded before the real key went live
