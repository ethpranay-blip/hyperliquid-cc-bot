# Architecture

## Files at a glance

| File | Role |
|---|---|
| `app/db.py` | SQLite layer: schema, thread-safe connections, CRUD helpers, lightweight migrations |
| `app/portal.py` | Corgi Portal HTTP client: session-cookie auth, activity-feed polling, event parsing, trade-detail enrichment |
| `app/hyperliquid_client.py` | Hyperliquid SDK wrapper: asset resolution (incl. HIP-3 + k-coins), price-rounded bracket orders, cancel/replace SL, WebSocket price feed, fill reconciliation |
| `app/notifier.py` | Discord/Telegram webhook dispatcher (fire-and-forget, never raises) |
| `app/main.py` | NiceGUI dashboard + event router + background tasks: stale/dedup guards, auto-mode, startup sync, per-trade card UI |

Supporting files: `app/__init__.py` (empty package marker), `requirements.txt`, `.env` (gitignored).

---

## High-level data flow

```
     ┌──────────────────────┐
     │  portal.corgicalls   │
     │  (remote HTTPS)      │
     └──────────┬───────────┘
                │  GET /api/portal/me/activity-feed  (every 3s)
                ▼
     ┌──────────────────────┐
     │   PortalClient       │  app/portal.py
     │   - login()          │  session cookies persisted to DB
     │   - poll()           │  sorts events chronologically
     │   - _parse_event()   │  emits: new_trade / stop_update
     │   - _mark_seen()     │          tp_hit / full_close
     └──────────┬───────────┘
                │  typed event dicts
                ▼
     ┌──────────────────────┐
     │   route_event()      │  app/main.py
     │   (dispatcher)       │
     └──┬────┬────┬────┬────┘
        │    │    │    │
        ▼    ▼    ▼    ▼
   handle_* four handlers, each touches ≤3 subsystems:

     handle_new_trade      ──► portal.get_trade_detail() (enrich)
       dedup/stale guards      hl.open_trade() (if auto-mode)
                               db.insert_portal_event
                               db.insert_opened_trade
                               db.add_live_trade
                               notifier.notify_opened

     handle_stop_update    ──► hl.update_stop()
                               db.insert_sl_update

     handle_tp_hit         ──► hl.partial_tp()
                               db.insert_tp_update
                               notifier.notify_tp_hit

     handle_full_close     ──► hl.close_trade()
                               db.insert_closed_trade
                               db.remove_live_trade
                               notifier.notify_closed | notify_sl_triggered

                ▲
                │
     ┌──────────┴───────────┐
     │ HyperliquidClient    │  app/hyperliquid_client.py
     │ - resolve_asset()    │
     │ - open_trade()       │  bulk_orders(grouping=normalTpsl|na)
     │ - close_trade()      │  reduce-only IOC
     │ - partial_tp()       │
     │ - update_stop()      │  cancel + replace pattern
     │ - _reconcile_fills() │  real fee + fill price from user_fills_by_time
     │ - _ws_loop()         │  allMids per default + HIP-3 dex
     └──────────┬───────────┘
                │
                ▼
     ┌──────────────────────┐
     │  api.hyperliquid.xyz │
     │  + /ws allMids feed  │
     └──────────────────────┘
```

---

## Startup sequence (app.on_startup)

1. `load_dotenv()` — loaded at import time, before any module reads env
2. `_setup_logging()` — rotating file handler (`app.log`, 5MB × 3) + stdout
3. `db.init_db()` — creates schema + applies migrations
4. **`state.startup_time_ms = now()`** — stale-trade cutoff anchor
5. `HyperliquidClient.__init__()` — probes available dexs (`_probe_available_dexs`), initialises `Exchange` with `perp_dexs` filtered to what's actually live on the network, spawns WS price feed
6. `PortalClient.start()` — loads persisted cookies from `portal_cookies` table; if none, first request triggers `login()`
7. **`seed_closed_from_backlog()`** — fetches current activity feed, inserts every `trade_closed` ID into `hl_closed_trades` with `close_type='pre-seeded'` so dedup blocks replays
8. `reconcile_on_startup()` — compares HL `open_positions` (across all dexs) vs DB `hl_live_trades`, cleans DB-only entries, logs HL-only ones
9. Spawn background task: `portal_poll_loop()`
10. Log `ready — dry_run=... testnet=... auto_mode=...` + AUTO MODE warning if applicable

---

## Event lifecycle for a single whitelisted trade

```
t0  Caller posts trade on Corgi Discord → portal publishes trade_opened event
    (timestamp ~ t0, in activity feed)

t1  Bot's next 3s poll fetches feed
    - sorted chronologically (fix for open/close race)
    - _mark_seen() by event.id → dedup on in-memory set
    - _parse_event() → {type:"new_trade", trade_id, coin, side, caller, entry_price(from entryRaw), at:timestamp}

t2  route_event("new_trade") → handle_new_trade
    - insert_portal_event (type='enter')
    - guard #1: get_live_trade(tid) != None → skip
    - guard #2: get_closed_trade(tid) != None → skip (replay blocker)
    - guard #3: event timestamp < startup_time_ms - 5min → mark stale
    - guard #4: trade_id in pending_trades → skip
    - enrich via portal.get_trade_detail(tid) (POST-follow returns inline trade)
      merges stop, leverage, entryUsed, tp list into event
    - pending_trades[tid] = event; fire UI refresh
    - IF auto_mode AND NOT stale AND NOT is_coin_live(coin):
        → await enter_trade(tid)

t3  enter_trade(tid) →
    - STALE guard (belt-and-suspenders for manual button clicks)
    - BLOCKED guard (is_coin_live)
    - hl.open_trade(...) → bulk_orders([entry IOC, SL trigger], grouping="normalTpsl")
                          OR just [entry] with grouping="na" if no SL
    - OpenResult carries { coin (order_name), entry_price (limit), my_fill_price (real avg), fee, ... }
    - db.insert_opened_trade(coin=<portal-bare>, entry_price=..., my_fill_price=...)
    - db.add_live_trade(tid)
    - notifier.notify_opened
    - UI refresh → card switches from pending to LIVE

t4+ Subsequent events (same trade_id):
    trade_updated/stop_moved → handle_stop_update → hl.update_stop
                                                    (cancel old SL by cloid → place new)
                                                    → db.insert_sl_update
    trade_updated/tp_hit     → handle_tp_hit      → hl.partial_tp(size_pct)
                                                    → db.insert_tp_update
    trade_closed             → handle_full_close  → hl.close_trade
                                                    → db.insert_closed_trade (+ real pnl/fee from
                                                       user_fills_by_time)
                                                    → db.remove_live_trade
                                                    → notifier.notify_closed/notify_sl_triggered
```

---

## State boundaries

| State | Where | Persists restart? |
|---|---|---|
| Portal cookies | `portal_cookies` table (SQLite) | Yes |
| Opened/closed trades | `hl_opened_trades`, `hl_closed_trades` | Yes |
| Live trade set | `hl_live_trades` | Yes |
| SL / TP history | `hl_sl_updates`, `hl_tp_updates` | Yes |
| Event audit log | `portal_events` | Yes |
| `pending_trades` dict | in-memory (AppState) | No (rebuilt from next poll) |
| `_seen_event_ids` set | in-memory (PortalClient) | No |
| `_asset_index` | in-memory (HyperliquidClient) | No (lazy rebuild) |
| `prices` dict (WS) | in-memory (HyperliquidClient) | No |
| `_sl_cloids[tid]` | in-memory (HyperliquidClient) | No (regenerated deterministically from trade_id) |
| Startup time cutoff | `state.startup_time_ms` | No (resets per launch) |
| Auto mode flag | `state.auto_mode` (env-seeded) | No |

---

## Key invariants

1. **Deterministic Cloids.** `entry_cloid = 0x{trade_id:032x}`. `sl_cloid = 0x{trade_id | (1<<127):032x}`. Means the same portal trade_id never double-opens on HL even if in-memory state is lost.
2. **DB stores the PORTAL coin.** `hl_opened_trades.coin = "SILVER"`, not `"xyz:SILVER"`. HL-side lookups use the asset's `order_name` (prefixed for HIP-3); `is_coin_live()` and portal event handlers use the bare name.
3. **round_px on EVERY price sent to HL.** Entry limit, SL trigger, SL limit, close limit, partial-TP limit — all go through `round_px(px, sz_decimals)`. HL silently rejects overly-precise prices.
4. **`normalTpsl` grouping requires SL leg.** Single-order submissions use `grouping="na"` to avoid HL's "Unexpected number of trigger orders" rejection.
5. **Trigger orders cannot be modified.** `update_stop` does cancel+replace, never `modify_order`.
6. **Events process in chronological order.** `fetch_new_events` sorts by `timestamp` ascending so open → update → close arrive causally.
7. **Stale cutoff is pre-enrichment.** Stale trades are never POST-followed (saves portal API cost + avoids 40x errors on closed trades).

---

## Security-sensitive surfaces

- `HL_PRIVATE_KEY` — loaded once at `HyperliquidClient.__init__`, never logged or transmitted beyond the HL SDK signing path
- `PORTAL_PASSWORD` — only sent in `POST /api/portal/login` body, persisted only as the resulting session cookie in SQLite
- Session cookies — stored in `portal_cookies` table as JSON, including `domain`, `path`, `secure`, `expires`
- Dashboard — served on `0.0.0.0:8080` by default. If deployed publicly, put behind auth (see DEPLOYMENT.md)
