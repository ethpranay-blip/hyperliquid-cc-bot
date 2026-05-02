# Feature list

## Portal integration
- Session-cookie auth via `POST /api/portal/login` with `{username, password}` from `.env`
- Cookies persisted to SQLite (`portal_cookies` table, JSON payload with domain/path/expires)
- Automatic re-login + single retry on HTTP 401
- Activity-feed polling every ~3 s with exponential backoff on transport errors / 429
- **Chronological event ordering** — feed sorted ascending by timestamp before processing so open → update → close arrive in causal order
- Per-event deduplication via in-memory `_seen_event_ids` set (capped at 5000, auto-pruned)
- Trade-detail enrichment via `POST /api/portal/me/trades` (returns full trade object inline including `stop`, `tp`, `entryUsed`, `leverage`)
- Graceful handling of already-followed trades (409) with `GET /api/portal/me/trades` fallback
- 404/400 on closed trades demoted from WARNING → DEBUG to avoid log noise

## Event parsing
Typed dicts emitted to main.py:
- `new_trade` — from `type: "trade_opened"` events (reads `entryRaw` as the entry price)
- `stop_update` — from `type: "trade_updated"` + `updateType: "stop_moved"` (parses price from `updateText` via regex)
- `tp_hit` — from `type: "trade_updated"` + `updateType: "tp_hit"` OR from any event with `0 < sizePct < 100`
- `full_close` — from `type: "trade_closed"` (with `stop_triggered: true` inferred from `closeReason`)
- `bet_opened` / `bet_closed` — silently skipped (different ID namespace, not trading-actionable)

## Caller whitelist
- `ALLOWED_CALLERS` env var (comma-separated, default `voberoi,pranayyyy,corgil_`)
- Non-whitelisted callers ignored + logged once per `trade_id`

## Hyperliquid execution
- **Atomic bracket orders**: `bulk_orders([entry, SL], grouping="normalTpsl")` when SL present, `grouping="na"` for single-order submissions
- **Deterministic Cloids**:
  - Entry: `Cloid.from_str(f"0x{trade_id:032x}")`
  - SL: high-bit set: `Cloid.from_str(f"0x{trade_id | (1<<127):032x}")`
- **Mandatory price rounding** — `round_px(px, sz_decimals)` on every price (entry, SL trigger, SL limit, close limit, partial-TP limit)
- **Reduce-only IOC closes** with 5% aggressive slippage cushion
- **Partial TP** — closes `(sizePct / 100) * current_position_size`
- **SL updates via cancel+replace**:
  1. `info.frontend_open_orders(address)`
  2. Filter to `coin + reduceOnly + cloid match + isTrigger`
  3. `exchange.bulk_cancel([{coin, oid}])`
  4. `exchange.order(order_name, !is_buy, size, sl_limit_px, trigger_body, reduce_only=True, cloid=same_cloid)`
- **Fill reconciliation** after every open and close — `info.user_fills_by_time()` yields real avg fill price + real fee + real `closedPnl`
- **Per-asset leverage** — `exchange.update_leverage(leverage, hl_coin, is_cross=False)` best-effort before each open (capped at asset's `maxLeverage`)
- **Minimum notional** enforced ($10) + `HL_MARGIN_USD × leverage` sizing

## Asset resolution
- **k-coin prefix**: `PEPE`, `BONK`, `SHIB`, `FLOKI`, `DOGS`, `LUNC`, `NEIRO` → `k`-prefixed HL symbol
- **k-coin stop scaling**: `hl_stop = hl_mid × (portal_stop / portal_entry)` to match HL's 1000x-scaled feed
- **HIP-3 dex resolution** — `resolve_asset()` matches against both `hl_coin` ("SILVER") and `{dex}:{hl_coin}` ("xyz:SILVER")
- **Adaptive `perp_dexs`** — `_probe_available_dexs()` POSTs `/info {"type":"meta","dex":X}` per candidate dex at startup and only passes the ones that return a valid universe (default + any of xyz/cash/flx that respond)
- **Per-dex price subscriptions** — WS subscribes to `allMids` once for default + once per HIP-3 dex; all keys merge into shared `self.prices` dict (matches `order_name` format)
- **Per-dex user_state queries** — HIP-3 positions live in per-dex `user_state` calls; `_current_position_size(order_name)` routes to the right dex
- **Cross-dex position aggregation** — `open_positions()` iterates all active dexs with coin-level deduplication

## WebSocket price feed
- `wss://api.hyperliquid.xyz/ws` (or testnet equivalent)
- Subscribes to `{"type":"allMids"}` per active dex
- Auto-reconnect with exponential backoff (1s → 30s)
- Handles both `{channel: "allMids", data: {mids: {...}}}` and flat `{...}` payload shapes
- Ping interval 30s, ping timeout 20s

## Safety guards
All four fire BEFORE any HL call is made:

1. **Live dedup**: `db.get_live_trade(tid)` != None → skip
2. **Closed dedup**: `db.get_closed_trade(tid)` != None → skip (fresh-DB replay blocker)
3. **Stale guard**: event timestamp < `startup_time_ms - 5min` → mark stale, no entry
4. **Pending dedup**: already in `state.pending_trades` → skip

Plus:

5. **BLOCKED guard**: `db.is_coin_live(coin)` → same coin already has a live position → skip auto-entry
6. **Startup reconcile**: compares HL positions (across all dexs, bare-name normalized) against DB; cleans DB-only entries, logs HL-only ones
7. **Pre-seed closes from backlog**: on startup, fetches activity feed once and pre-seeds `hl_closed_trades` with every already-closed `trade_id` (using `close_type='pre-seeded'`) so Guard #2 catches replays
8. **DRY_RUN mode**: every HL path short-circuits with a mock response when enabled
9. **Credentials check**: if `HL_PRIVATE_KEY`/`HL_WALLET_ADDRESS` missing and not dry-run, forces `DRY_RUN=true` with warning

## Auto mode
- `AUTO_MODE=true|false` env var seeds the runtime flag
- Runtime toggle on dashboard header (per-session)
- When ON + fresh + not BLOCKED + not stale: auto-enters every whitelisted `new_trade` event
- Loud startup banner: `⚠️ AUTO MODE ON + DRY_RUN=false — real orders will be placed automatically`

## Dashboard (NiceGUI at :8080)
- **Header**: app title, Auto Mode switch, DRY RUN banner (when enabled)
- **Stats row**: Total PnL (excludes pre-seeded rows), Win Rate, Open Count, Total Fees
- **Active trade cards**:
  - LIVE cards: green `●ᴸᴵⱽᴱ` badge, real-time Mid + PnL (basis = `my_fill_price ?? entry_price`), red Cancel button
  - PENDING cards: Enter button (disabled if BLOCKED or STALE), Dismiss button
  - BLOCKED badge (yellow) when coin already live
  - STALE badge (orange) when event posted before bot start — entry disabled
- **Historic trades table**: ID, Date, Coin, Side, Caller, Entry, Exit, PnL, Type (excludes pre-seeded)
- **Activity feed sidebar** (right, scrollable): color-coded recent events (new_trade, opened, tp_hit, stop_update, close, closed_manual, sl_triggered, blocked, stale)
- **Dark theme** by default, 1s tick for card PnL updates, 3s tick for stats + history refresh

## Persistence
SQLite WAL mode, foreign keys ON, ISO-8601 UTC timestamps:

| Table | Holds |
|---|---|
| `hl_live_trades` | Set of currently-live `trade_id`s |
| `hl_opened_trades` | Every open attempt: entry_price (limit), **my_fill_price** (real fill), entry_sl, size, margin, leverage, caller |
| `hl_closed_trades` | Every close (plus pre-seeded backlog markers): entry_price, exit_price, size, trade_value, margin, fee, pnl, close_type |
| `hl_sl_updates` | SL change log with old/new stops |
| `hl_tp_updates` | Partial-TP fills with price, pct, size, fee |
| `portal_events` | Audit log of every routed portal event with JSON `details` |
| `portal_cookies` | JSON-serialized httpx cookies for session persistence |

Thread-safe: one `sqlite3.Connection` per thread via `threading.local`. Lightweight migrations via `_apply_migrations()` on init.

## Observability
- **Rotating file log**: `app.log`, 5 MB × 3 backups, formatter `%(asctime)s %(levelname)-7s %(name)s: %(message)s`
- **LOG_LEVEL** env var (default INFO)
- Every handler logs the action taken; every rejection logs the HL error verbatim
- In-dashboard activity feed for real-time observability without tailing files
- Diagnostic ad-hoc scripts documented in session (extraAgents probe, spot/perps balance, fill reconciliation)

## Notifications (optional)
- `NOTIFY_WEBHOOK_URL` env var — empty = silent no-op
- Auto-detect Discord (`discord.com/api/webhooks/*`) vs Telegram (`api.telegram.org/bot*`)
- Discord payload: `{"content": "message"}`
- Telegram payload: `{"text": "message", "parse_mode": "HTML"}`
- 4 trigger types: `notify_opened`, `notify_closed`, `notify_sl_triggered`, `notify_tp_hit`
- Fire-and-forget via `loop.create_task` — never raises, never blocks, always logs
- Opened messages use real fill price when available (preferred over limit)

## Error handling policy
- Transient errors (timeout, connection reset, 5xx, 429): exponential backoff, max 3 retries
- Validation errors (HL order rejections): never retry, log + surface to user via `_safe_notify`
- `_safe_notify` — UI-context-safe notify wrapper that logs + silently skips UI when no client bound (e.g., auto-mode from poll task)
- HL error shape: `response.data.statuses[*].error` checked on every response
