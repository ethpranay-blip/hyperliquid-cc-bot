# Developer Handoff — Corgi Copy Trading Bot

**For:** Encroma (integrating this bot into an existing web portal).
**From:** the existing code + this session's build notes.
**Scope:** what the bot is, every integration surface, every pitfall already caught, and what changes if you want to serve more than one user.

If you're looking for background narrative, see:
- [ARCHITECTURE.md](ARCHITECTURE.md) — data flow diagram, startup sequence, state boundaries
- [FEATURES.md](FEATURES.md) — organized feature inventory
- [API.md](API.md) — exhaustive wire-level reference
- [CHANGELOG.md](CHANGELOG.md) — every fix in build order
- [DEPLOYMENT.md](DEPLOYMENT.md) — local + VPS + Docker

This handoff is a **single self-contained document** that covers what you need to build on top of this.

---

## 1. What the bot actually does

A single-user copy-trading bot that:

1. Polls the **Corgi Calls** portal (`portal.corgicalls.com`) every 3 s
2. Parses the activity feed for whitelisted signallers
3. Mirrors those signals as leveraged perps orders on **Hyperliquid** (mainnet or testnet)
4. Tracks every event in SQLite, serves a **NiceGUI dashboard** at `:8080`, and optionally pings a Discord/Telegram webhook

The whole thing runs as one Python process on one host, signed by one API sub-wallet, against one portal account.

---

## 2. Complete file list

| Path | Lines | What it does |
|---|---|---|
| `app/__init__.py` | 0 | Empty — marks `app/` as a package |
| `app/db.py` | 701 | SQLite layer: 7-table schema, thread-local connections (`threading.local`), lightweight migrations via `_apply_migrations()`, CRUD helpers for every table, WAL mode + `foreign_keys=ON`, ISO-8601 UTC timestamps everywhere |
| `app/portal.py` | 695 | Corgi Portal client: session-cookie auth via `POST /api/portal/login` with `{username, password}`, cookie persistence to SQLite, auto re-login on 401, activity-feed polling with chronological sort + dedup, trade-detail enrichment via POST-to-follow, event parser emitting 4 typed dicts |
| `app/hyperliquid_client.py` | 1,271 | HL SDK wrapper: `Exchange`/`Info`/`Cloid`, deterministic cloids, **mandatory `round_px` on every price**, atomic bracket orders via `bulk_orders(grouping="normalTpsl"\|"na")`, reduce-only IOC closes, **cancel+replace SL updates** (modify_order doesn't work on triggers), k-coin prefix + stop scaling, HIP-3 asset resolution across 4 dexes, adaptive dex probe, allMids WebSocket with auto-reconnect, DRY_RUN mode, fill reconciliation via `user_fills_by_time()`, retry with exp backoff on transient errors only |
| `app/notifier.py` | 307 | Webhook notifier: auto-detects Discord vs Telegram from URL, 4 trigger types (`notify_opened`/`notify_closed`/`notify_sl_triggered`/`notify_tp_hit`), fire-and-forget via `loop.create_task`, **never raises** |
| `app/main.py` | 1,208 | NiceGUI dashboard + orchestrator: startup sequence, event router, 4 handler functions, stale/dedup/BLOCKED guards, auto-mode, startup reconciliation, pre-seeding of closed trades from backlog, dashboard UI (stats header, trade cards, historic table, activity feed sidebar), `_safe_notify` for UI-context safety |
| `Makefile` | 136 | Task runner: `make start / stop / restart / logs / status / deploy` |
| `env.example` | 32 | Template — copy to `.env` and fill in real values |
| `requirements.txt` | 6 | `nicegui`, `httpx`, `python-dotenv`, `rich`, `hyperliquid-python-sdk`, `websockets` |
| `data/corgi.db` | (binary) | SQLite file, auto-created on first run |
| `app.log` | (rotates) | Structured log: 5 MB × 3 backups via `RotatingFileHandler` |
| `docs/*.md` | — | Reference documentation (you're reading one of them) |

**Not in `app/`:**
- `GAMEPLAN.md` — the original spec used to build it
- `README.md` — top-level project intro
- `corgicalls-automation-app/` — a reference repo cloned for comparison; **not part of this bot**, safe to delete
- `gitignore` (no leading dot) — ⚠️ doesn't actually work until renamed to `.gitignore`

---

## 3. Environment variables

All loaded via `python-dotenv` at the top of `app/main.py`. Required unless noted.

### Credentials

| Var | Description |
|---|---|
| `PORTAL_USER` | Corgi portal username (NOT an email — the handle you log in with, e.g. `pranay`). The login field is literally `username`. |
| `PORTAL_PASSWORD` | Corgi portal password. Only sent in the `/api/portal/login` body. Persisted result is a session cookie stored in SQLite. |
| `HL_WALLET_ADDRESS` | **Main wallet address** (0x-prefixed, 42 chars). This is the account that holds the USDC. |
| `HL_PRIVATE_KEY` | **API sub-wallet** private key (0x-prefixed, 66 chars). **NEVER your main wallet key.** Generate at `app.hyperliquid.xyz` → API → Create API Wallet. Testnet and mainnet sub-wallets are separate universes. |

### Trading config

| Var | Default | Description |
|---|---|---|
| `HL_LEVERAGE` | `10` | Default leverage when portal signal doesn't specify one. Capped per-asset at HL's `maxLeverage`. |
| `HL_MARGIN_USD` | `100` | USDC margin per trade. Notional = `margin × leverage`. $10 minimum notional enforced by the SDK wrapper. |
| `ALLOWED_CALLERS` | `voberoi,pranayyyy,corgil_` | Comma-separated userTags to copy. Trades from other callers are silently dropped (logged once per trade_id). |

### Safety / runtime

| Var | Default | Description |
|---|---|---|
| `HL_TESTNET` | `false` | `true` → swap in `api.hyperliquid-testnet.xyz` endpoints |
| `DRY_RUN` | `true` | `true` → log what would happen, place **no real orders**, return mock responses |
| `AUTO_MODE` | `false` | `true` → bot auto-executes every fresh whitelisted `new_trade`. Loud startup banner emitted when combined with `DRY_RUN=false`. |

### Observability

| Var | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `NOTIFY_WEBHOOK_URL` | *(empty)* | Discord webhook URL or Telegram bot URL. Empty = no notifications. Auto-detects payload shape. |

### Advanced overrides

| Var | Default |
|---|---|
| `PORTAL_BASE_URL` | `https://portal.corgicalls.com` |
| `PORTAL_POLL_INTERVAL` | `3.0` (seconds) |
| `HL_BASE_URL` | mainnet/testnet REST per `HL_TESTNET` |
| `HL_WS_URL` | mainnet/testnet WS per `HL_TESTNET` |
| `CORGI_DB_PATH` | `./data/corgi.db` |
| `PORT` | `8080` (dashboard) |
| `HOST` | `0.0.0.0` (dashboard bind) |

---

## 4. Install from scratch

### Prereqs
- Python 3.9+ (tested on 3.9.6 macOS CLI Tools and should work on 3.10/3.11/3.12)
- Outbound HTTPS to `portal.corgicalls.com`, `api.hyperliquid.xyz`, `api.hyperliquid-testnet.xyz`
- ~200 MB disk

### Steps

```bash
git clone <this-repo>
cd <repo>

# venv (recommended — keeps deps isolated)
python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt

cp env.example .env
chmod 600 .env
# edit .env — fill in PORTAL_USER, PORTAL_PASSWORD, HL_WALLET_ADDRESS, HL_PRIVATE_KEY

# First-run validation — keep DRY_RUN=true here
python -m app.main
# → dashboard at http://localhost:8080
# → watch app.log for portal auth + HL init messages
```

If `make` is available:
```bash
make start       # launches in background via Makefile
make status
make logs
make stop
```

(If using venv, update Makefile's `PYTHON` variable to `venv/bin/python`.)

### First-time verification checklist

From the log, confirm each step printed:

1. `Database initialized at data/corgi.db`
2. `startup cutoff: <ms>` — stale cutoff locked in
3. `HL wrapper ready (MAINNET|TESTNET, dry_run=..., dexs=[...])`
4. `HL price feed connected (subscribed default + [...])` — WS up
5. `portal login successful` (first run) OR `loaded N persisted cookie(s)` (subsequent runs)
6. `seed_closed_from_backlog: N pre-seeded, M already in DB`
7. `startup sync: HL and DB in sync (X positions)`
8. `ready — dry_run=... testnet=... auto_mode=...`

If any step emits an `ERROR` or fails to complete, see [DEPLOYMENT.md troubleshooting](DEPLOYMENT.md#troubleshooting).

### Verify HL sub-wallet is registered

```bash
python3 -c "
import httpx, os
for ln in open('.env'):
    if '=' in ln and not ln.strip().startswith('#'):
        k,_,v = ln.strip().partition('='); os.environ.setdefault(k, v)
from eth_account import Account
sub = Account.from_key(os.environ['HL_PRIVATE_KEY']).address
r = httpx.post('https://api.hyperliquid.xyz/info',
               json={'type':'extraAgents','user':os.environ['HL_WALLET_ADDRESS']},
               timeout=15).json()
addrs = [a['address'].lower() for a in r]
print(f'derived sub-wallet: {sub}')
print(f'registered? {sub.lower() in addrs}')
"
```

Registered-but-underfunded accounts will still pass this check — fund via `app.hyperliquid.xyz` deposit.

---

## 5. Portal API — exact request/response shapes

Base URL: `https://portal.corgicalls.com`. Auth: session cookie from login, persisted to SQLite `portal_cookies` table.

### 5.1 `POST /api/portal/login`

**Request:**
```json
{"username": "<PORTAL_USER>", "password": "<PORTAL_PASSWORD>"}
```

**Responses:**
- `200 OK` — sets session cookie (`Set-Cookie` header); bot persists the whole jar
- `401` — `{"error":"Invalid username or password."}` → `PortalAuthError`
- `403` — `PortalAuthError`

Call site: [`PortalClient.login()`](app/portal.py).

### 5.2 `GET /api/portal/me/activity-feed`

The firehose. Called every 3 s.

**Response** (real payload):
```json
{
  "events": [
    {
      "id": "trade_open_591",
      "type": "trade_opened",
      "timestamp": 1776826676022,
      "coin": "TRUMP",
      "side": "long",
      "caller": "pranayyyy",
      "callerDiscordId": "457581276350644246",
      "tradeId": 591,
      "entryRaw": "2.8885",
      "personalized": true
    },
    {
      "id": "trade_update_560_4",
      "type": "trade_updated",
      "timestamp": 1776783937578,
      "coin": "BTC",
      "side": "long",
      "caller": "voberoi",
      "tradeId": 560,
      "updateText": "Stop moved to $75,000",
      "updateType": "stop_moved"
    },
    {
      "id": "trade_close_583",
      "type": "trade_closed",
      "timestamp": 1776815200334,
      "coin": "SP500",
      "side": "short",
      "caller": "corgil_",
      "tradeId": 583,
      "closePrice": 7090,
      "pnlPct": -0.07057163020465773
    },
    {
      "id": "bet_open_243",
      "type": "bet_opened",
      "caller": "corgil_",
      "betId": 243,
      "marketTitle": "...",
      "position": "..."
    }
  ],
  "latestTimestamp": 1776826676022,
  "latestPersonalizedTimestamp": 1776826676022
}
```

**Event types the bot recognizes:**
- `trade_opened` — primary open signal. `entryRaw` is a **string** (not a number). `stopLoss`/`takeProfits`/`leverage` are **not** in this event; fetch them via `get_trade_detail()`.
- `trade_updated` + `updateType: "stop_moved"` — SL change. Price is embedded in `updateText`; parse via regex.
- `trade_updated` + `updateType: "tp_hit"` — partial take-profit. May carry `sizePct`, `tpPrice`, `tpNum`.
- `trade_closed` — final close. `closePrice` is the key field. `closeReason` may indicate SL trigger.
- `bet_opened` / `bet_closed` — betting events (different ID namespace, `betId` not `tradeId`). **Silently skipped.**

Call site: [`PortalClient.get_activity_feed()`](app/portal.py).

### 5.3 `POST /api/portal/me/trades` — follow + enrichment

**The enrichment trick:** there's no per-trade GET endpoint. POSTing to follow a trade returns the full trade detail inline.

**Request:**
```json
{"tradeId": 591}
```

**Responses:**

`201 Created` — wrapper with the full trade detail embedded:
```json
{
  "id": 1274,
  "tradeId": 591,
  "userId": "457581276350644246",
  "enteredAt": "2026-04-22T03:39:28.809Z",
  "entryPriceAtFollow": 2.881,
  "userExitPrice": null,
  "userExitedAt": null,
  "userExits": null,
  "autoExited": false,
  "userLeverage": 10,
  "trade": {
    "id": 591,
    "userTag": "pranayyyy",
    "side": "long",
    "coin": "TRUMP",
    "entryRaw": "2.8885",
    "entryUsed": 2.8885,
    "stop": 2.8125,
    "tp": null,
    "leverage": 10,
    "status": "open",
    "originalStop": 2.8125,
    "closePrice": null,
    "updates": "[]"
  }
}
```

`409 Conflict` — already following. Falls back to `GET /api/portal/me/trades` and searches by `trade.id`.

`400` / `404` — trade closed / invalid. Logged at **DEBUG** (not WARNING) so log stays clean on backlog fetches.

Call sites: [`PortalClient.follow_trade()`](app/portal.py), [`PortalClient.get_trade_detail()`](app/portal.py).

### 5.4 `GET /api/portal/me/trades`

Lists currently-followed trades. Shape: list of the wrapper objects above.

Primary use: fallback for `get_trade_detail()` when POST returns 409.

### 5.5 `PATCH /api/portal/me/trades/{tradeId}`

Marks a followed trade closed on the portal side.

**Request:**
```json
{"userExitPrice": 85.8, "sizePct": 100}
```

**Defined but NOT currently called** by any handler. The bot closes positions on HL directly and relies on the portal's own `trade_closed` events to update internal state. Kept in case you want explicit bidirectional sync.

---

## 6. Hyperliquid SDK operations

SDK: [`hyperliquid-python-sdk`](https://github.com/hyperliquid-dex/hyperliquid-python-sdk). All sync; wrapped in `asyncio.to_thread()` to not block NiceGUI's event loop.

### 6.1 Constructor

```python
Exchange(
    account=eth_account.Account.from_key(HL_PRIVATE_KEY),
    base_url=HL_BASE_URL,
    account_address=HL_WALLET_ADDRESS,   # main wallet, sub-wallet signs on its behalf
    perp_dexs=["", "xyz", "cash", "flx"],  # filtered by _probe_available_dexs at startup
)

Info(base_url=HL_BASE_URL, skip_ws=True)  # bot manages its own WS
```

### 6.2 Info queries

| Call | Purpose |
|---|---|
| `info.meta(dex="")` / `info.meta(dex="xyz")` | Asset universe for a given dex. Cached per-dex in `_meta_cache`. |
| `info.all_mids(dex="")` / `info.all_mids(dex="xyz")` | REST fallback when WS cache is cold. Per-dex allMids return prefixed keys (`"xyz:SILVER"`). |
| `info.user_state(address, dex="...")` | Perps state: `marginSummary`, `assetPositions[]`, `withdrawable`. Queried per-dex for HIP-3 positions. |
| `info.user_fills_by_time(address, start_ms, end_ms)` | Real avg fill price, real fee, real `closedPnl`. Used post-open and post-close. |
| `info.frontend_open_orders(address)` | All resting orders, used by `_cancel_sls_for_trade()` to find the SL cloid to cancel. |

### 6.3 Exchange operations

| Call | Purpose |
|---|---|
| `exchange.update_leverage(leverage, coin, is_cross=False)` | Set isolated leverage before open. Best-effort. |
| `exchange.bulk_orders(orders, grouping="normalTpsl"\|"na")` | Atomic multi-order submission. `"normalTpsl"` for entry+SL brackets; `"na"` for single-order submissions. |
| `exchange.order(coin, is_buy, sz, limit_px, order_type, reduce_only=..., cloid=...)` | Single order. Used for close + SL replacement. |
| `exchange.bulk_cancel([{coin, oid}])` | Cancel by oid. Used in the cancel+replace SL update flow. |

### 6.4 Order dict shape

```python
{
    "coin": "xyz:SILVER",            # dex-prefixed for HIP-3
    "is_buy": True,                  # bool
    "sz": 30.42,                     # size (float)
    "limit_px": 78.2,                # must go through round_px()
    "order_type": {"limit": {"tif": "Ioc"}},          # for entries/closes
    # OR:
    "order_type": {"trigger": {
        "triggerPx": 77.3,
        "isMarket": True,
        "tpsl": "sl"                 # "sl" or "tp"
    }},
    "reduce_only": False,            # True for SL + close
    "cloid": Cloid.from_str("0x..."),
}
```

### 6.5 Error parsing

HL's error shape — checked by `_check_hl_response()`:
```
resp["response"]["data"]["statuses"][i]["error"]
```

On any error in the list → `HyperliquidValidationError` is raised (not retried). Transient errors (timeout, connection, 5xx) are detected by substring match and retried with exp backoff (max 3).

### 6.6 Deterministic cloids

```python
entry_cloid = Cloid.from_str(f"0x{trade_id:032x}")
sl_cloid    = Cloid.from_str(f"0x{(trade_id | (1<<127)):032x}")  # high bit disambiguates
```

Same `trade_id` → same cloid across process restarts → HL's duplicate-cloid error becomes the backstop against double-opens even if in-memory state is lost.

### 6.7 WebSocket

`wss://api.hyperliquid.xyz/ws` (or testnet equivalent). Subscribes to `allMids` once per active dex:
```json
{"method":"subscribe","subscription":{"type":"allMids"}}
{"method":"subscribe","subscription":{"type":"allMids","dex":"xyz"}}
{"method":"subscribe","subscription":{"type":"allMids","dex":"cash"}}
{"method":"subscribe","subscription":{"type":"allMids","dex":"flx"}}
```

All keys merge into `HyperliquidClient.prices: dict[str, float]`. Auto-reconnect with exp backoff (1s → 30s cap).

---

## 7. Bugs already fixed (heads-up so you don't re-introduce them)

The build session caught and fixed these. If you refactor, **don't regress on them**:

| # | Bug | Why it broke | Guard |
|---|---|---|---|
| 1 | Portal login 401 on default `.env` | Path/field guess wrong — it's `POST /api/portal/login` with field `username`, not `/api/auth/login` with `email` | Correct path hardcoded in `PortalClient.login()` |
| 2 | Testnet `KeyError: 'cash'` on Exchange init | Testnet lacks the `cash` dex; SDK errors out | `_probe_available_dexs()` filters candidates at startup via `POST /info {"type":"meta","dex":X}` |
| 3 | `Unexpected number of trigger orders` when SL is None | `grouping="normalTpsl"` requires a TP/SL leg | `grouping = "normalTpsl" if len(orders) > 1 else "na"` |
| 4 | `Cannot modify canceled or filled order` on every SL update | `exchange.modify_order()` doesn't work on resting trigger orders | Cancel+replace pattern: `frontend_open_orders` → filter by cloid → `bulk_cancel` by oid → `exchange.order()` with same cloid |
| 5 | Close event for SOL #582 silently missed | Portal feed is newest-first; on restart, close event arrived before the open had been recorded in DB → `handle_full_close` no-op'd and marked the close as "seen" | `fetch_new_events()` sorts events ascending by `timestamp` before processing |
| 6 | On fresh DB, restart re-opens every closed trade in backlog | `handle_new_trade` only dedup'd against `hl_live_trades`, not `hl_closed_trades`; in-memory `_seen_event_ids` resets on restart | Dedup now checks `get_closed_trade(tid)` too + `seed_closed_from_backlog()` pre-seeds at startup |
| 7 | SILVER (HIP-3 asset) "not found on any dex" | HIP-3 assets named `"xyz:SILVER"` in universe, not `"SILVER"` — strict equality missed them | `resolve_asset()` now returns `order_name` (dex-prefixed); all order submissions / position lookups / WS keys use `order_name` |
| 8 | `ui.html(...)` crashed dashboard with `missing 'sanitize'` | Newer NiceGUI requires kwarg | `ui.html(..., sanitize=False)` with `TypeError` fallback for older versions |
| 9 | `ui.notify()` explodes when called from poll-loop (auto-mode) | No UI client bound outside a request handler | `_safe_notify(msg, type)` wrapper logs + swallows |
| 10 | Dashboard PnL off by the full slippage cushion (~$25/trade on HYPE example) | DB stored `entry_price = mid * 1.05` (the limit we sent), not the real fill | `hl_opened_trades.my_fill_price` column added; `open_trade()` now calls `_reconcile_fills()` post-submit; card uses `my_fill_price` with `entry_price` fallback |
| 11 | Fresh DB + auto-mode would open EVERY backlog trade on startup | No timestamp-based gate; every replayed `trade_opened` was treated as fresh | `state.startup_time_ms` anchor + `_event_is_stale()` check; stale trades appear as dashboard cards but can't be entered (STALE badge, disabled button) |
| 12 | `$0 perps balance` confusion on HyperCore unified accounts | `clearinghouseState` reads $0 because USDC sits in Spot and is drawn automatically; it's cosmetic not a real problem | Documented; no code change needed |

Full session narrative in [CHANGELOG.md](CHANGELOG.md).

---

## 8. Extending to multi-user

The bot is architected as a **single-user process**. To serve N users concurrently, here's what changes — biggest effort → smallest:

### 8.1 Schema changes (biggest)

Every table needs a `user_id` dimension. Currently:

- `hl_live_trades(trade_id PK)` → add `user_id` to the PK or as a composite key
- `hl_opened_trades`, `hl_closed_trades`, `hl_sl_updates`, `hl_tp_updates`, `portal_events` → add `user_id` column + index
- `portal_cookies(key PK, value, at)` → add `user_id` to the PK (same cookie name for different users must coexist)

**Migration is non-trivial** because `trade_id` isn't globally unique across users — two users can legitimately both be "following trade #591" via the portal. The natural PK becomes `(user_id, trade_id)`.

Consider **switching to Postgres**. SQLite + WAL handles one writer reasonably, but N concurrent users means N concurrent `UPDATE`s and SQLite locks the whole DB per write.

### 8.2 Config / credentials (per-user)

`.env` is process-wide. For multi-user:

- `PORTAL_USER`, `PORTAL_PASSWORD`, `HL_WALLET_ADDRESS`, `HL_PRIVATE_KEY` → per-user record
- Store encrypted at rest (private keys especially — see Security section)
- `HL_LEVERAGE`, `HL_MARGIN_USD`, `ALLOWED_CALLERS`, `AUTO_MODE` → per-user preferences
- `NOTIFY_WEBHOOK_URL` → per-user

Schema suggestion:
```sql
CREATE TABLE users (
    id              INTEGER PRIMARY KEY,
    external_id     TEXT UNIQUE,       -- your portal's user ID
    portal_user     TEXT NOT NULL,
    portal_password_enc BLOB NOT NULL,
    hl_wallet_address   TEXT NOT NULL,
    hl_private_key_enc  BLOB NOT NULL,
    margin_usd      REAL NOT NULL DEFAULT 100,
    leverage        REAL NOT NULL DEFAULT 10,
    allowed_callers TEXT NOT NULL,     -- CSV
    auto_mode       BOOL NOT NULL DEFAULT 0,
    dry_run         BOOL NOT NULL DEFAULT 1,
    notify_webhook  TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
```

### 8.3 Runtime structure (medium)

Today:
- One `PortalClient` instance (module-level state)
- One `HyperliquidClient` instance
- One `AppState` singleton

Multi-user:
- Map `user_id` → `PortalClient` (one per user — each has their own cookie jar, own session)
- Map `user_id` → `HyperliquidClient` (one per user — each has their own `Exchange` with their sub-wallet)
- Map `user_id` → per-user state dict (`pending_trades`, `startup_time_ms`, `auto_mode`)
- **OR** a `UserSession` class that bundles those three

Pros of one-per-user: simple, isolated, matches today's code closely.
Cons: memory scales linearly; at 1000 users you have 1000 WS connections to HL. HL imposes a WS connection limit per IP. **Consider a shared WS client** that broadcasts prices to all per-user handlers.

### 8.4 Shared vs per-user state

Best architecture:

```
┌─────────────────────────────────────────┐
│  SHARED (one per process):              │
│  - HL allMids WebSocket connection      │
│    → broadcasts prices to all sessions  │
│  - Portal client factory + HTTP client  │
│    → connection pooling                 │
│  - DB (Postgres)                        │
│  - Event processing queue               │
└──────────────┬──────────────────────────┘
               │
    ┌──────────┴──────────┬───────┬───────┐
    ▼                     ▼       ▼       ▼
  UserSession_1         U_2     U_3     U_N
  ├ PortalClient        ...
  ├ HyperliquidClient
  └ pending/state
```

### 8.5 Event routing

Currently: one poll loop fetches one feed.

Multi-user: two options:

1. **N poll loops (one per user)** — simplest, matches existing code. Each user's feed is personalized (`personalized: true` field is user-specific). Cost: N portal polls per cycle.

2. **Shared poll with per-user filter** — if the portal has a "global" feed endpoint (it doesn't by default, but you could negotiate one with Corgi), one poll → route each event to all users with matching caller whitelist. Cheaper at scale.

Recommend (1) for now; migrate to (2) if Corgi adds a suitable endpoint.

### 8.6 Auth / users table

Currently none. You need to:
- Build a signup/login UI (or integrate with your existing portal's auth)
- Store the per-user credentials securely (see Security)
- Scope every DB query and UI route to the authenticated user
- Add rate-limiting per user

### 8.7 Concurrency hot spots

- **Portal cookie refresh on 401** is racy across users. If 10 users' cookies expire in the same minute, 10 simultaneous re-logins. Each is fine alone but the portal may throttle. Add a per-user lock (bot already has `self._login_lock` inside PortalClient; this keeps working if PortalClient is per-user).
- **WS reconnect flaps** cause all per-user handlers to lose prices briefly. Add a stale-price guard on sizing calls (reject opens if price is > N seconds old).
- **SL cancel+replace is not idempotent across racing SL updates.** If two `stop_update` events for the same trade arrive within milliseconds, the cancel for the first can race the place for the second. Add a per-trade asyncio.Lock.

---

## 9. Embedding the dashboard vs running standalone

NiceGUI serves its own Vue frontend + WebSocket backend at `:8080`. Three integration patterns, in increasing order of engineering effort:

### 9.1 Standalone with SSH tunnel (zero change)

User runs bot on a VPS. Open a tunnel: `ssh -L 8080:localhost:8080 user@vps`. Browse `http://localhost:8080`. Works today. Not a multi-user solution.

### 9.2 Reverse proxy under your domain (small change)

Put Caddy or nginx in front:

```caddyfile
corgi.example.com {
    basicauth { admin <bcrypt> }
    reverse_proxy localhost:8080
}
```

Pros: TLS + basic auth gets you online in 10 minutes.
Cons: still single-user (whoever knows the basic-auth password is **the user** — they can click Cancel on all trades). NiceGUI is not multi-tenant; all visitors share the same `AppState`.

### 9.3 Iframe embed (medium)

```html
<iframe src="https://corgi.example.com" sandbox="allow-scripts allow-same-origin"></iframe>
```

Iframes work with NiceGUI; it supports normal HTTP/WebSocket. You'd need to:
- Configure CORS (NiceGUI/Starlette lets you customize via `app.add_middleware(...)`)
- Ensure the parent auth cookie / token is visible to the iframe origin
- Handle NiceGUI's reconnect-on-navigation

### 9.4 Extract the backend; build your own UI (biggest, cleanest long-term)

The clean path: treat `app/main.py`'s event loop + DB + HL client as a **headless service**, add a thin REST/WebSocket API in front of it, build the UI inside your existing portal's frontend stack.

**Proposed slice:**

Headless service exposes:

```
GET  /api/bot/state                 → AppState snapshot (auto_mode, dry_run, etc.)
POST /api/bot/auto_mode { on: bool } → toggle auto mode
GET  /api/bot/trades/live           → list active trades + live mid + pnl
GET  /api/bot/trades/pending        → pending cards (stale and actionable)
GET  /api/bot/trades/history        → historic table
POST /api/bot/trades/{id}/enter     → manual enter (if not stale)
POST /api/bot/trades/{id}/close     → manual cancel
GET  /api/bot/events/stream         → SSE/WS of activity feed
GET  /api/bot/stats                 → headline PnL/WR/Open counts
```

All the logic is already in `main.py` — you're essentially mapping handler functions to HTTP routes + serializing `db.list_live_trades()` etc. Less code than you'd think.

Benefits:
- Your portal's existing auth + RBAC applies
- Your portal's existing styling
- Multi-user natural (route is scoped to authenticated `user_id`)
- Easy to add API clients for mobile, CLI, etc.
- NiceGUI becomes optional — keep it as a dev-only diagnostic dashboard, or drop it

Recommend extracting to FastAPI. The rest of the stack (httpx, asyncio) is already compatible.

---

## 10. Security notes — MUST-NEVER-EXPOSE

### 10.1 Private keys
- **`HL_PRIVATE_KEY`** = full trading authority over the main wallet's perps balance. A leaked sub-wallet key cannot directly withdraw (that requires the main wallet), but it **can**:
  - Open maximally-leveraged positions and trade to zero
  - Place counter-trades against the user (drain via fee loops)
- **Never log it.** Code should not write it to stdout, app.log, or any notifier payload.
- **Never include it in responses.** Multi-user REST endpoints must not echo it back in a user profile.
- **Encrypt at rest.** If DB leaks, encrypted private keys with an app-managed KMS key are still recoverable only with the key. Options:
  - AWS KMS / GCP KMS / Vault for key encryption
  - `cryptography.fernet.Fernet` with a master key kept outside the DB
  - Hardware-backed keys (YubiHSM, Nitro Enclaves) for paranoid setups

### 10.2 Portal credentials
- **`PORTAL_PASSWORD`** sent once in the login body, then replaced by session cookies. Same rules as above: never log, never return via API, encrypt at rest.
- Session cookies in `portal_cookies` are effectively bearer tokens — grant full portal access. If leaked, the attacker IS that user on the portal (can follow trades, close trades, etc.). Treat them like passwords.

### 10.3 `.env` file
- Mode `chmod 600` — owner-only read. systemd unit in DEPLOYMENT.md does this.
- **NOT checked into git.** The current `gitignore` file has a missing dot prefix — fix `mv gitignore .gitignore` before pushing anywhere.
- If committed by accident: rotate everything. Passwords, sub-wallet keys, webhook URLs.

### 10.4 Dashboard
- **Zero authentication built in.** `0.0.0.0:8080` + no password = anyone who can reach port 8080 can click "Cancel" on live trades and manually enter any pending card.
- **Always front it with something.** SSH tunnel, basic auth proxy, VPN, your portal's auth middleware. The bot assumes a trusted network.
- NiceGUI sessions are not isolated by client — all visitors share `state.auto_mode`, `state.pending_trades`, etc. Toggling Auto Mode in one tab toggles it for everyone.

### 10.5 Logs
- **`app.log`** should not contain secrets but may contain:
  - Portal user (the whoami line: `logging in to portal as pranay`)
  - Trade sizes, prices, PnL — not private but arguably PII about strategy
  - Wallet addresses — public on-chain, but still data
- Don't ship raw logs to third-party log aggregators without scrubbing
- Consider `LOG_LEVEL=WARNING` in production to minimize surface area

### 10.6 Webhook URLs
- Discord webhook URL + secret = anyone with it can post as the webhook to that channel. Not catastrophic but annoying.
- Telegram bot token in the URL same deal — full bot control.
- Scope webhooks to low-sensitivity channels. Don't reuse a webhook from a production ops channel.

### 10.7 HL `extraAgents` lifetime
- API sub-wallets have an expiry (`validUntil` field; ~50 weeks when freshly created).
- Add monitoring: if `validUntil - now < 30 days`, surface a "renew your sub-wallet" warning to the user.
- On expiry, every order will fail with `does not exist` — same symptom as a wrong key.

### 10.8 Rate limits you'll eventually hit
- **Portal**: unknown limits but 3s poll cadence works. `429` handled with exp backoff. If you multi-user, share the poll or stagger wake-up times per user.
- **HL REST**: generous, but `user_fills_by_time()` is fast — we call it after every open/close. At high trade volume this can add up.
- **HL WS**: connection limit per IP is the gotcha for multi-user. Shared WS recommended at scale.
- **Discord/Telegram**: rate-limited per-channel. High-volume notifications → aggregate or queue.

---

## 11. Gotchas worth knowing

- **`entryRaw` is a string.** Cast to float before use.
- **`trade_opened` has no stop/TP/leverage.** You MUST call `get_trade_detail()` to get them.
- **Portal feed is newest-first.** Sort ascending or you'll re-hit bug #5.
- **HL silently rejects over-precise prices.** `round_px(px, sz_decimals)` on **every** price — entry, SL, TP, close.
- **`modify_order` doesn't work on trigger orders.** Use cancel+replace.
- **`normalTpsl` grouping requires SL leg.** Use `"na"` for single-order submissions.
- **HIP-3 coin names are dex-prefixed.** `"xyz:SILVER"`, not `"SILVER"`.
- **Testnet and mainnet API sub-wallets are separate.** A key generated on one won't work on the other.
- **Unified (HyperCore) accounts show $0 in `clearinghouseState`.** Don't add a pre-flight balance check — HL draws from Spot automatically on unified setups.
- **Log-line PnL basis uses `my_fill_price`.** If you add another PnL display surface, use the same column not `entry_price`.
- **The stale cutoff is 5 min.** Any event with timestamp older than `startup_time_ms - 5min` is blocked from entry. Tune `STALE_SLACK_MS` in `main.py` if this bites legitimate trades.
- **Pre-seeded closed trades use `close_type='pre-seeded'`.** They're excluded from `get_stats()` and `get_historic_trades()`. If you add new aggregate queries, filter them out.
- **Deterministic cloids are your backstop.** `Cloid.from_str(f"0x{trade_id:032x}")` means the same trade_id can't double-open even with lost state. Don't "improve" this to random cloids.

---

## 12. Where to start

If I were you, I'd approach this in this order:

1. **Run it single-user in DRY_RUN on a staging host for a week.** Watch [`app.log`](../app.log) and [`data/corgi.db`](../data/corgi.db). Understand the event flow end-to-end.
2. **Extract the REST slice from §9.4** — keep `app/` intact, build `app/api.py` with FastAPI routes that call into the existing handlers. One commit.
3. **Add `users` table + encrypted credential columns.** Replace `os.environ[...]` reads with `user_repo.get_credentials(user_id)`. One commit per subsystem (portal, hl_client, main).
4. **Swap SQLite for Postgres.** Schema is already vanilla-SQL enough that `sqlite3` → `asyncpg` is mostly a driver swap + replacing `AUTOINCREMENT` with `SERIAL`. Keep the migration helpers.
5. **Shared WS client.** One connection to HL, broadcast prices to all user sessions.
6. **Build the UI in your portal's stack.** Deprecate `app/main.py`'s NiceGUI bits; keep the event router, handlers, reconcile.
7. **Harden credentials** (KMS, per-user key rotation, sub-wallet expiry monitoring).

Ping me (the existing author, via commit log or issue tracker) if anything in `app/` looks intentional-but-weird — session had many footguns and we fixed them deliberately. CHANGELOG entries cite the exact failure mode.

Good luck.
