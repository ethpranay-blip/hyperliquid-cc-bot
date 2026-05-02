# Systems Architecture — CC Portal Copy Trading Bot

## Overview
A NiceGUI dashboard + background event loop that **polls** the Corgi Calls portal
activity feed (~3s), parses each event into a typed dict, and routes it to a
handler that drives Hyperliquid orders via the SDK. Despite the original spec,
the portal exposes no webhook — the bot drives all I/O itself.

The dashboard (port 8080) shows live PnL, active trades, an Activity Feed
sidebar, and exposes an Auto-Mode toggle plus per-trade Enter / Cancel /
Dismiss buttons. The same process owns the portal poll loop, the HL WS feed,
periodic reconcilers, and a heartbeat ping. Currently deployed on Railway.

## Data Flow
```
CC Portal (portal.corgicalls.com)
  └── REST: /api/portal/me/activity-feed       (every ~3s, session-cookie auth)
        │  events: trade_opened | trade_updated | trade_closed | bet_* | …
        ▼
app/portal.py  (PortalClient)
  • Email/password login → cookies persisted in db.portal_cookies
  • Auto re-login on 401, retry once
  • _parse_event → {new_trade | stop_update | tp_hit | full_close} | None
  • Caller whitelist (ALLOWED_CALLERS), event-id dedup, oldest-first sort
  • Sets last_successful_poll_ms for liveness
        │ async generator yields parsed events
        ▼
app/main.py  (NiceGUI app + supervisor)
  • portal_poll_supervisor — respawns poll loop on ANY exit (silent-death guard)
  • route_event → handle_new_trade | handle_stop_update | handle_tp_hit | handle_full_close
  • Stale-trade guard (portal ts < startup cutoff → entry blocked)
  • Auto-mode + manual Enter button → enter_trade
  • Pre-flight margin check; insufficient margin → DROP signal (no backfill)
  • Periodic reconcile (60s) + HL userEvents-driven reconcile (~2s debounced)
  • heartbeat_loop → notifier (every HEARTBEAT_INTERVAL_SECONDS, default 600s)
        │
        ▼
app/hyperliquid_client.py  (HyperliquidClient)
  • SDK wrapper (hyperliquid-python-sdk): Exchange + Info
  • WS feed: allMids (default + each HIP-3 dex) + userEvents
  • HIP-3 dex resolution (probes "", "xyz", "cash", "flx") → asset_index
  • k-coin handling: hl_symbol_for(), scale_stop_for_k()
  • Mandatory round_px on every price
  • open_trade — atomic bracket (entry IOC + SL trigger) via bulk_orders
  • close_trade / partial_tp — reduce-only IOC
  • update_stop — cancel old SL by cloid + place fresh trigger
  • Fill reconciliation via user_fills_by_time → real fee/pnl/avg_fill
  • Deterministic Cloids per trade_id (entry + disjoint SL cloid)
  • get_available_margin (withdrawable), open_positions (None on failure)
        │
        ▼
Hyperliquid (mainnet or testnet)

app/db.py  (SQLite WAL, thread-local connections)
  Tables: hl_live_trades, hl_opened_trades, hl_closed_trades,
          hl_sl_updates, hl_tp_updates, portal_events, portal_cookies,
          hl_pending_trades

app/notifier.py  (fire-and-forget Discord/Telegram webhook)
  notify_opened / notify_closed / notify_sl_triggered /
  notify_tp_hit / notify_heartbeat
```

## Files

| File | Responsibility | Key Interfaces |
|------|---------------|----------------|
| `app/main.py` | NiceGUI dashboard (port 8080), startup/shutdown, event router, supervised portal poll, periodic + WS-driven reconcilers, heartbeat, manual button actions | `on_startup`, `route_event`, `enter_trade`, `cancel_trade`, `EVENT_HANDLERS` |
| `app/portal.py` | Async portal client, session-cookie auth, activity-feed polling, event parsing + dedup, follow-and-fetch trade enrichment | `PortalClient.start/login/poll/get_activity_feed/get_trade_detail`, `PortalAuthError` |
| `app/hyperliquid_client.py` | HL SDK wrapper, WS price + userEvents feed, HIP-3 + k-coin resolution, atomic bracket open, close / partial TP / SL update, fill reconciliation | `HyperliquidClient.open_trade/close_trade/partial_tp/update_stop/start_price_feed/open_positions/get_available_margin`, `hl_symbol_for`, `round_px`, `cloid_for`, `HyperliquidValidationError` |
| `app/db.py` | SQLite WAL, schema + lightweight migrations, all persistence | `init_db`, `add_live_trade`, `is_coin_live`, `insert_opened_trade`, `insert_closed_trade`, `insert_sl_update`, `insert_tp_update`, `insert_portal_event`, `get_stats`, `get_historic_trades`, `list_pending_trades`, `get/set/clear_portal_cookies` |
| `app/notifier.py` | Fire-and-forget Discord/Telegram webhook (auto-detected by URL) | `notify_opened`, `notify_closed`, `notify_sl_triggered`, `notify_tp_hit`, `notify_heartbeat`, `is_enabled` |

There is **no** `config.py`, `webhook_listener.py`, or `signal_engine.py` —
config is read from env directly inside each module's `__init__`, and what the
original spec called the "signal engine" lives in `main.py` as the event router
+ handler functions.

## Key Data Structures

### Parsed event (dict, produced by `PortalClient._parse_event`)
Every event carries `event_id`, `trade_id`, `coin`, `side`, `caller`, `at`, `raw`
plus a `type`-specific payload:

- `new_trade` → `entry_price`, `stop_loss`, `take_profits`, `leverage`, `status`
- `stop_update` → `new_stop`, `old_stop`
- `tp_hit` → `size_pct`, `tp_price`, `tp_num`
- `full_close` → `exit_price`, `stop_triggered`, `close_reason`, `pnl_pct`

`main.handle_new_trade` later enriches the event with the portal trade detail
(stop, tp, leverage, entryUsed) via `PortalClient.get_trade_detail`.

### Result dataclasses (`hyperliquid_client.py`)
```python
@dataclass
class OpenResult:
    trade_id: int
    coin: str                          # HL order_name (HIP-3 prefixed if applicable)
    side: str                          # "long" | "short" | "buy" | "sell"
    size: float
    entry_price: float                 # slippage-padded LIMIT we sent
    stop_price: Optional[float]
    entry_cloid: str
    sl_cloid: Optional[str]
    dry_run: bool
    my_fill_price: Optional[float]     # real avg fill from user_fills_by_time
    fee: Optional[float]
    raw: dict

@dataclass
class CloseResult:
    trade_id: int
    coin: str
    size: float
    avg_exit_price: Optional[float]
    fee: Optional[float]
    pnl: Optional[float]
    dry_run: bool
    raw: dict
```

## Database schema (`app/data/corgi.db`, WAL)

| Table | Purpose |
|-------|---------|
| `hl_live_trades` | Set of currently-live `trade_id`s |
| `hl_opened_trades` | Per-open row: coin/side/entry_price/my_fill_price/entry_sl/size/margin/leverage/caller/at |
| `hl_closed_trades` | Per-close row + `close_type` ∈ {automatic, manual, stop_triggered, pre-seeded} |
| `hl_sl_updates` | Each SL move (old/new/size/conditions) |
| `hl_tp_updates` | Each TP partial (price/pct/num/size/fee) |
| `portal_events` | Raw portal event log: enter / cancel / tp_hit / auto_close / stale_close / sl_triggered |
| `portal_cookies` | Persisted httpx cookie jar for portal session |
| `hl_pending_trades` | Trades the bot couldn't open immediately (margin / etc.); FIFO retry queue |

Notable: `pre-seeded` close_type is used by `seed_closed_from_backlog()` to
mark historical trade_ids as already-closed so a fresh DB doesn't replay the
backlog. `get_stats()` and `get_historic_trades()` filter pre-seeded rows out.
A lightweight migration in `_apply_migrations` adds `my_fill_price` to old DBs.

## External dependencies

| Service | Purpose | Auth |
|---------|---------|------|
| Corgi Portal REST (`portal.corgicalls.com`) | Activity-feed polling, follow-trade detail, manual close PATCH | Session cookies via `POST /api/portal/login` (username + password) |
| Hyperliquid REST + WS | Order placement, user_state, fills, mids, userEvents | API sub-wallet private key (HL_PRIVATE_KEY) signing for HL_WALLET_ADDRESS |
| Discord/Telegram webhook (optional) | Open/close/SL/TP/heartbeat notifications | URL secret in `NOTIFY_WEBHOOK_URL` |

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PORTAL_USER` / `PORTAL_EMAIL` | — | Portal login email |
| `PORTAL_PASSWORD` | — | Portal login password |
| `PORTAL_BASE_URL` | `https://portal.corgicalls.com` | Override portal host |
| `PORTAL_POLL_INTERVAL` | `3.0` | Seconds between activity-feed polls |
| `ALLOWED_CALLERS` | `voberoi,pranayyyy,corgil_` | Comma-separated whitelist; non-matching trades are dropped |
| `HL_WALLET_ADDRESS` | — | Main wallet address (positions/fills queried for this) |
| `HL_PRIVATE_KEY` | — | **API sub-wallet** signing key (never main-wallet key) |
| `HL_TESTNET` | `false` | Route to testnet REST + WS endpoints |
| `HL_BASE_URL` / `HL_WS_URL` | mainnet/testnet defaults | Override HL endpoints |
| `HL_LEVERAGE` | `10` | Default leverage when portal doesn't specify |
| `HL_MARGIN_USD` | `100` | Real per-trade margin (notional = margin × leverage) |
| `HL_MARGIN_MODE` | `isolated` | `isolated` or `cross` (passed to `update_leverage`) |
| `HL_DEX_PRIORITY` | `xyz,cash,flx` | HIP-3 dex resolution order (default dex `""` is always first) |
| `DRY_RUN` | `true` | If true, log orders but don't submit to HL |
| `AUTO_MODE` | `false` | Auto-enter every fresh whitelisted new_trade event |
| `FORCE_ENTER_TIDS` | — | Comma-separated trade_ids that bypass the STALE check |
| `RECONCILE_INTERVAL_SECONDS` | `60` | Periodic HL/DB sync interval |
| `PENDING_DRAIN_INTERVAL_SECONDS` | `60` | Pending-queue retry cadence |
| `HL_CHANGE_DEBOUNCE_SECONDS` | `2.0` | Debounce window for userEvents-driven reconcile |
| `HEARTBEAT_INTERVAL_SECONDS` | `600` | Webhook "still alive" cadence |
| `NOTIFY_WEBHOOK_URL` | — | Discord/Telegram webhook URL (auto-detected) |
| `LOG_LEVEL` | `INFO` | Root logger level |
| `PORT` / `HOST` | `8080` / `0.0.0.0` | NiceGUI bind |
| `CORGI_DB_PATH` | `data/corgi.db` | SQLite path |

## Background tasks (spawned in `on_startup`)

| Task | Purpose | Failure mode |
|------|---------|--------------|
| `portal-poll-supervisor` | Wraps `portal_poll_loop` and respawns on ANY exit; exponential backoff (5s → 60s, reset after 5 min healthy) | Apr 28 silent-death guard — supervisor + finally-block in `portal.poll` make a future regression loud |
| `hl-ws-feed` | allMids (default + per-dex) + userEvents subscription with auto-reconnect | 1s → 30s backoff |
| `periodic-reconcile` | Every `RECONCILE_INTERVAL_S`, diff HL positions vs `hl_live_trades` and clean DB-only drift | Skips cleanup when `open_positions()` returns `None` (May 1 wipe guard); requires 2 consecutive empty results before cleaning a populated DB |
| `hl-change-reconciler` | Awaits `state.hl_change_event` set by userEvents callback, debounces, runs same reconcile within ~2s | Falls back to periodic reconcile if WS subscription drops |
| `heartbeat` | Pings notifier with uptime + poll-age + open count | Errors swallowed; never crashes the bot |

## Known invariants & guards

- **STALE guard**: every event with portal timestamp < `startup_time_ms − STALE_SLACK_MS` (5 min) is dashboard-only — neither auto-mode nor manual Enter can take it. Override with `FORCE_ENTER_TIDS`.
- **Backlog dedup**: `seed_closed_from_backlog()` pre-seeds every `trade_closed_*` event id from the current activity feed into `hl_closed_trades` with `close_type='pre-seeded'`, so a fresh DB never replays historical opens as live trades.
- **Pre-flight margin**: `enter_trade` checks `withdrawable` before submitting. Insufficient margin → DROP (no backfill, by design — May 1 decision).
- **`open_positions()` returns `None` on failure** (vs `[]` for "really empty"). Reconcile callers MUST distinguish, otherwise a transient HL API blip wipes live positions from DB (May 1 incident).
- **`round_px`** is mandatory on every price sent to HL — wrong precision is silently rejected.
- **Atomic bracket**: `open_trade` sends entry IOC + SL trigger in one `bulk_orders` call with `grouping="normalTpsl"` (or `"na"` if no SL). SL update is cancel-by-cloid + place-fresh, because HL `modify_order` doesn't work on resting triggers.
- **HIP-3 namespace**: HL reports positions / fills using `dex:COIN` (e.g. `xyz:SILVER`); DB stores the bare portal coin (`SILVER`). `_bare()` and `hl_symbol_for()` translate.
