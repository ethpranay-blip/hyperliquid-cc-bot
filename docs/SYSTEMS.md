# Systems Architecture — CC Portal Copy Trading Bot

## Overview
A NiceGUI dashboard + background event loop that **polls** the Corgi Calls portal
activity feed (~3s), parses each event into a typed dict, and routes it to a
handler that drives Hyperliquid orders via the SDK. Despite the original spec,
the portal exposes no webhook — the bot drives all I/O itself.

The dashboard (port 8080) shows live PnL, active trades, an Activity Feed
sidebar, a Position-Sizing panel, a `/performance` bot-vs-portal analytics page,
and an Auto-Mode toggle plus per-trade Enter / Cancel / Dismiss buttons. The
same process owns the portal poll loop, the HL WS feed, periodic reconcilers,
and a heartbeat ping. Deployed on Railway (DB on a mounted volume at
`/data/corgi.db`).

## Data Flow
```
CC Portal (portal.corgicalls.com)
  └── REST: /api/portal/me/activity-feed       (every ~3s, session-cookie auth)
        │  events: trade_opened | trade_updated | trade_closed | bet_* | …
        ▼
app/portal.py  (PortalClient)
  • Email/password login → cookies persisted in db.portal_cookies; re-login on 401
  • _parse_event → {new_trade | stop_update | tp_hit | full_close} | None
  • Caller whitelist (ALLOWED_CALLERS), event-id dedup, oldest-first sort
        │ async generator yields parsed events
        ▼
app/main.py  (NiceGUI app + supervisor)
  • portal_poll_supervisor — respawns poll loop on ANY exit (silent-death guard)
  • route_event → handle_new_trade | handle_stop_update | handle_tp_hit | handle_full_close
  • Stale-trade guard (portal ts < startup cutoff → entry blocked)
  • Auto-mode + manual Enter → enter_trade (records skips to hl_skipped_trades)
  • Pre-flight margin check; insufficient margin → DROP (no backfill)
  • Auto-trailing stop after every partial TP (app/trailing.compute_trailed_stop):
    TP1/TP2 → breakeven (real fill), TP3 → TP1 price, TP4 → TP2 price; never
    loosens (ratchet); TP1/TP2 prices resolved from hl_tp_updates
  • Startup adoption (app/adoption) re-tracks live HL positions missing from DB
  • Periodic reconcile (60s) + HL userEvents-driven reconcile (~2s debounced)
  • heartbeat_loop → notifier (every HEARTBEAT_INTERVAL_SECONDS, default 600s)
        │
        ▼
app/hyperliquid_client.py  (HyperliquidClient)
  • SDK wrapper (hyperliquid-python-sdk): Exchange + Info
  • WS feed: allMids (default + each HIP-3 dex) + userEvents
  • resolve_asset: exact match across dexes ("", "xyz", "cash", "flx") FIRST,
    then app/hyperliquid_client.TICKER_ALIASES fallback (e.g. US500→SP500)
  • k-coin handling: hl_symbol_for(), scale_stop_for_k(); mandatory round_px
  • Sizing via app/sizing.compute_position_size (fixed-margin | fixed-risk)
  • Strict per-action slippage caps via app/execution (entry/SL 0.5%, TP 1%):
    IOC limit anchored to the caller's level; out-of-band → LevelSlippageExceeded
  • open_trade — atomic bracket (entry IOC + SL trigger) via bulk_orders
  • close_trade / partial_tp — reduce-only IOC, slippage-capped vs target level
  • update_stop — PLACE-BEFORE-CANCEL (new SL placed first, old cancelled after)
    + wrong-side-of-mid guard (sl_triggers_immediately)
  • Fill reconciliation via user_fills_by_time → real fee/pnl/avg_fill
  • Deterministic Cloids per trade_id; get_available_margin via spot_user_state
    (unified-account aware); open_positions (None on failure)
        │
        ▼
Hyperliquid (mainnet or testnet)

app/db.py  (SQLite WAL, thread-local connections)
  Tables: hl_live_trades, hl_opened_trades, hl_closed_trades, hl_sl_updates,
          hl_tp_updates, portal_events, portal_cookies, hl_pending_trades,
          bot_settings, hl_skipped_trades, portal_trades

app/notifier.py  (fire-and-forget Discord/Telegram webhook)
  notify_opened / notify_closed / notify_sl_triggered / notify_tp_hit /
  notify_sl_moved / notify_sl_failed / notify_skipped / notify_heartbeat
```

## Files

| File | Responsibility | Key Interfaces |
|------|---------------|----------------|
| `app/main.py` | NiceGUI dashboard (port 8080) + `/performance` page, startup/shutdown, event router, supervised portal poll, reconcilers, heartbeat, manual actions, auto-trailing stops, startup adoption, sizing panel | `on_startup`, `route_event`, `enter_trade`, `cancel_trade`, `_auto_trail_stop_after_tp`, `_adopt_untracked_positions`, `performance_page`, `EVENT_HANDLERS` |
| `app/portal.py` | Async portal client, session-cookie auth, activity-feed polling, event parsing + dedup, follow-and-fetch enrichment | `PortalClient.start/login/poll/get_activity_feed/get_trade_detail`, `_parse_event`, `PortalAuthError` |
| `app/hyperliquid_client.py` | HL SDK wrapper, WS feed, HIP-3 + k-coin + alias resolution, atomic bracket open, close / partial TP, place-before-cancel SL update, fill reconciliation | `HyperliquidClient.open_trade/close_trade/partial_tp/update_stop/open_positions/get_available_margin/get_resting_stop_price`, `hl_symbol_for`, `aliased_symbol`, `round_px`, `scale_stop_for_k`, `TICKER_ALIASES` |
| `app/sizing.py` | Pure position sizing | `compute_position_size` (fixed_margin / fixed_risk, available-margin cap, unknown-margin fallback) |
| `app/execution.py` | Pure slippage-cap + SL-validity logic | `get_entry/tp/sl_slip_pct`, `slippage_capped_limit`, `enforce_slip`, `check_within_slip`, `sl_triggers_immediately`, `LevelSlippageExceeded` |
| `app/trailing.py` | Pure trailing-stop computation | `compute_trailed_stop` (rule map + never-loosen ratchet) |
| `app/adoption.py` | Pure feed→open-trade matcher for startup adoption | `build_open_trade_index` |
| `app/performance.py` | Pure bot-vs-portal reconciliation | `aggregate_portal_trades`, `reconcile`, `summarize`, `cumulative_series` |
| `app/db.py` | SQLite WAL, schema + migrations, all persistence | `init_db`, `add_live_trade`, `is_coin_live`, `insert_opened_trade`, `insert_closed_trade`, `insert_sl_update`, `insert_tp_update`, `get_tp_price_from_history`, `get_current_stop`, `get_stats`, `get_historic_trades`, `get/set_setting`, `get_sizing_settings`, `insert_skipped_trade`, `upsert_portal_trade`, `list_portal_trades` |
| `app/notifier.py` | Fire-and-forget Discord/Telegram webhook (auto-detected by URL) | `notify_opened/closed/sl_triggered/tp_hit/sl_moved/sl_failed/skipped/heartbeat`, `is_enabled` |

There is **no** `config.py`, `webhook_listener.py`, or `signal_engine.py` —
config is read from env directly in each module, and the "signal engine" lives
in `main.py` as the event router + handlers. The pure modules (`sizing`,
`execution`, `trailing`, `adoption`, `performance`) contain no I/O so they're
fully unit-tested without the SDK or DB.

## Key Data Structures

### Parsed event (dict, produced by `PortalClient._parse_event`)
Every event carries `event_id`, `trade_id`, `coin`, `side`, `caller`, `at`, `raw`
plus a `type`-specific payload:

- `new_trade` → `entry_price`, `stop_loss`, `take_profits`, `leverage`, `status`
- `stop_update` → `new_stop`, `old_stop` (price extracted from `updateText` when
  Corgi sends the `trade_updated`/`stop_moved` shape)
- `tp_hit` → `size_pct` (defaults 25%), `tp_price`, `tp_num`
- `full_close` → `exit_price`, `stop_triggered`, `close_reason`, `pnl_pct`

`bet_*` events are dropped (different ID namespace). Non-whitelisted callers
return None. `main.handle_new_trade` enriches via `PortalClient.get_trade_detail`.

### Result dataclasses (`hyperliquid_client.py`)
`OpenResult` (trade_id, coin=HL order_name, side, size, entry_price=slippage-
padded limit, stop_price, entry_cloid, sl_cloid, dry_run, my_fill_price=real avg
fill, fee, raw) and `CloseResult` (trade_id, coin, size, avg_exit_price, fee,
pnl, dry_run, raw).

## Database schema (`/data/corgi.db` on Railway volume, WAL)

| Table | Purpose |
|-------|---------|
| `hl_live_trades` | Set of currently-live `trade_id`s |
| `hl_opened_trades` | Per-open row: coin/side/entry_price/my_fill_price/entry_sl/size/margin/leverage/caller/at |
| `hl_closed_trades` | Per-close row + `close_type` ∈ {automatic, manual, stop_triggered, pre-seeded} |
| `hl_sl_updates` | Each SL move (old/new/size/conditions) |
| `hl_tp_updates` | Each TP partial (price/pct/num/size/fee) — source for TP1/TP2 trail prices |
| `portal_events` | Bot-relevant portal event log: enter / cancel / tp_hit / auto_close / stale_close / sl_triggered |
| `portal_cookies` | Persisted httpx cookie jar for portal session |
| `hl_pending_trades` | Trades the bot couldn't open immediately; FIFO retry queue |
| `bot_settings` | Live dashboard-editable key/value (sizing_mode, margin_usd, risk_usd) |
| `hl_skipped_trades` | Why a signal wasn't entered (stale / blocked_coin_live / insufficient_margin / entry_slipped / ticker_not_found); `UNIQUE(trade_id, reason)` |
| `portal_trades` | Persisted portal outcomes (pnl_pct, close_price, timestamps) so the performance comparison survives the rolling feed |

Notable: `pre-seeded` close_type marks historical trade_ids as already-closed so
a fresh DB doesn't replay the backlog; `get_stats()`/`get_historic_trades()`
filter them out. `_apply_migrations` adds `my_fill_price` to old DBs.

## External dependencies

| Service | Purpose | Auth |
|---------|---------|------|
| Corgi Portal REST | Activity-feed polling, follow-trade detail, manual close | Session cookies via `POST /api/portal/login` |
| Hyperliquid REST + WS | Orders, user_state, fills, mids, userEvents, meta | API sub-wallet key (HL_PRIVATE_KEY) signing for HL_WALLET_ADDRESS |
| Discord/Telegram webhook (optional) | All lifecycle notifications | URL secret in `NOTIFY_WEBHOOK_URL` |

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PORTAL_USER` / `PORTAL_EMAIL` | — | Portal login email |
| `PORTAL_PASSWORD` | — | Portal login password |
| `PORTAL_BASE_URL` | `https://portal.corgicalls.com` | Override portal host |
| `PORTAL_POLL_INTERVAL` | `3.0` | Seconds between activity-feed polls |
| `ALLOWED_CALLERS` | `voberoi,pranayyyy,corgil_` | Comma-separated whitelist |
| `HL_WALLET_ADDRESS` | — | Main wallet address (positions/fills queried for this) |
| `HL_PRIVATE_KEY` | — | **API sub-wallet** signing key (never main-wallet key) |
| `HL_TESTNET` | `false` | Route to testnet REST + WS endpoints |
| `HL_BASE_URL` / `HL_WS_URL` | mainnet/testnet defaults | Override HL endpoints |
| `HL_LEVERAGE` | `10` | Default leverage when portal doesn't specify (capped at asset max) |
| `HL_MARGIN_USD` | `100` | Per-trade margin in fixed-margin mode (default for both sizing $ inputs) |
| `HL_MARGIN_MODE` | `isolated` | `isolated` or `cross` |
| `HL_DEX_PRIORITY` | `xyz,cash,flx` | HIP-3 dex resolution order (default dex `""` always first) |
| `MAX_ENTRY_SLIPPAGE_PCT` | `0.005` | Entry slippage cap vs caller's entry (0.5%) |
| `MAX_TP_SLIPPAGE_PCT` | `0.010` | TP / manual-close cap vs caller's level (1%) |
| `MAX_SL_SLIPPAGE_PCT` | `0.005` | SL-triggered close cap (0.5%) |
| `DRY_RUN` | `true` | If true, log orders but don't submit to HL |
| `AUTO_MODE` | `false` | Auto-enter every fresh whitelisted new_trade event |
| `FORCE_ENTER_TIDS` | — | Comma-separated trade_ids that bypass the STALE check |
| `RECONCILE_INTERVAL_SECONDS` | `60` | Periodic HL/DB sync interval |
| `PENDING_DRAIN_INTERVAL_SECONDS` | `60` | Pending-queue retry cadence |
| `HL_CHANGE_DEBOUNCE_SECONDS` | `2.0` | Debounce for userEvents-driven reconcile |
| `HEARTBEAT_INTERVAL_SECONDS` | `600` | Webhook "still alive" cadence |
| `NOTIFY_WEBHOOK_URL` | — | Discord/Telegram webhook URL (auto-detected; Telegram needs `?chat_id=`) |
| `LOG_LEVEL` | `INFO` | Root logger level |
| `PORT` / `HOST` | `8080` / `0.0.0.0` | NiceGUI bind |
| `CORGI_DB_PATH` | `data/corgi.db` | SQLite path (set to `/data/corgi.db` on Railway for the volume) |

Sizing mode + amounts are normally set live from the dashboard (persisted in
`bot_settings`), not env; `HL_MARGIN_USD` is the fallback default.

## Background tasks (spawned in `on_startup`)

| Task | Purpose |
|------|---------|
| `portal-poll-supervisor` | Respawns `portal_poll_loop` on ANY exit; backoff 5s→60s (Apr 28 silent-death guard) |
| `hl-ws-feed` | allMids (default + per-dex) + userEvents, auto-reconnect |
| `periodic-reconcile` | Diff HL positions vs `hl_live_trades`, clean drift, adopt orphans; `open_positions()→None` skip-cleanup guard (May 1) |
| `hl-change-reconciler` | userEvents-triggered debounced reconcile (~2s) |
| `heartbeat` | Notifier ping with uptime + poll-age + open count |

The `/performance` page itself persists portal outcomes (read path) on each load.

## Known invariants & guards

- **STALE guard**: events older than `startup_time_ms − STALE_SLACK_MS` (5 min) are dashboard-only. Override with `FORCE_ENTER_TIDS`.
- **Backlog dedup**: `seed_closed_from_backlog()` pre-seeds close events as `pre-seeded` so a fresh DB never replays historical opens.
- **Startup adoption**: live HL positions missing from the DB are re-linked to their portal trade (id recovered from the feed) so the bot resumes managing them after a redeploy — combined with the persistent volume, trades survive deploys.
- **Pre-flight margin**: `enter_trade` checks `get_available_margin()` (via `spot_user_state`, unified-account aware). Insufficient → DROP, recorded in `hl_skipped_trades` (no backfill, May 1 decision).
- **Sizing**: `compute_position_size` supports fixed-margin (`margin×leverage`) and fixed-risk (`risk_usd / |entry−SL|`). Fixed-risk caps at available margin, and **falls back to fixed-margin when available margin is unknown** (API blip) so a tight SL can't produce an unbounded size.
- **Strict slippage caps**: entries/SL 0.5%, TPs/manual-close 1% (env-tunable). The IOC limit is anchored to the caller's level; if the live mid is past the cap the order can't fill → skipped + recorded (`entry_slipped`) / `notify_skipped`. Doubles as wrong-proxy protection. k-coins use the legacy `DEFAULT_SLIPPAGE` (5%) fallback.
- **Auto-trailing stops** (`_auto_trail_stop_after_tp` + `app/trailing.compute_trailed_stop`): after each partial TP — TP1/TP2 → breakeven (the **real fill** `my_fill_price`, not the limit), TP3 → TP1 price, TP4 → TP2 price. **TP1/TP2 prices are read from `hl_tp_updates`** (`get_tp_price_from_history`), and `tp_num` is inferred from the recorded TP count when the portal omits it. Never-loosen ratchet: keeps the tighter of {target, current stop}. (The earlier "TP3/TP4 silently no-op" limitation is **fixed**.)
- **SL update is place-before-cancel**: `update_stop` places the new SL (fresh rotating cloid) FIRST, then cancels the prior one(s) via `_cancel_other_sls`. A failed place raises with the old stop still resting → the position is never stopless. Callers fire `notify_sl_failed`. A wrong-side-of-mid stop is refused (`sl_triggers_immediately`) before any order is touched — guard inherited by both the portal and auto-trail paths.
- **`open_positions()` returns `None` on failure** (vs `[]` for empty) so reconcile won't wipe live positions on an API blip (May 1).
- **`round_px`** mandatory on every price sent to HL.
- **Atomic bracket**: `open_trade` sends entry IOC + SL trigger in one `bulk_orders` (`grouping="normalTpsl"`, or `"na"` with no SL).
- **HIP-3 namespace**: HL uses `dex:COIN`; DB stores bare portal coin. `_bare()` / `hl_symbol_for()` translate.
- **Performance comparison accuracy**: the activity feed is a rolling window, so portal outcomes are persisted to `portal_trades` and matched against history — the bot-vs-portal comparison stays accurate instead of decaying to "0 matched" as old trades scroll off.
