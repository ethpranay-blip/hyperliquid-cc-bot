# Corgi Calls Copy Trading Bot — Complete Game Plan

## The Goal

A fully automated copy trading bot that monitors the Corgi Portal for trade signals from whitelisted callers (voberoi, pranayyyy, corgil_) and mirrors them as leveraged isolated-margin futures positions on Hyperliquid — with a NiceGUI web dashboard at localhost:8080 for monitoring, manual control, and historical tracking.

---

## Architecture Overview

```
Portal (portal.corgicalls.com)
  │  polling every ~3s
  ▼
┌──────────────────────────┐
│  portal.py                │  ← Session cookie auth, activity feed parsing
│  (HTTP client + parser)   │
└──────────┬───────────────┘
           │ new trade / update / close events
           ▼
┌──────────────────────────┐
│  main.py                  │  ← Event router + auto/manual mode logic
│  (NiceGUI app + loops)    │
└──────┬───────────┬───────┘
       │           │
       ▼           ▼
┌────────────┐ ┌────────────┐
│  hl_client │ │  db.py     │
│  (SDK +WS) │ │  (SQLite)  │
└────────────┘ └────────────┘
       │
       ▼
  Hyperliquid DEX
```

---

## Phase Plan (Build Order)

### PHASE 1 — Foundation (do first, test before moving on)

| Step | File | What it does | Lines (est.) |
|------|------|-------------|-------------|
| 1.1 | `app/db.py` | SQLite WAL schema + all CRUD helpers | ~250 |
| 1.2 | `app/portal.py` | Session auth, cookie persistence, activity feed polling, trade parsing | ~200 |
| 1.3 | Test portal | Run portal.py standalone, confirm login + feed parsing works | — |

**Milestone: You can see live portal events printed to console.**

### PHASE 2 — Hyperliquid Integration

| Step | File | What it does | Lines (est.) |
|------|------|-------------|-------------|
| 2.1 | `app/hyperliquid_client.py` | SDK wrapper: open, close, partial TP, SL update, WS price feed, round_px, k-coins, HIP-3 | ~350 |
| 2.2 | Test on testnet | Set HL_TESTNET=true (or use testnet URL), verify order placement | — |

**Milestone: Bot can open and close a position on HL testnet from a portal signal.**

### PHASE 3 — Dashboard (NiceGUI)

| Step | File | What it does | Lines (est.) |
|------|------|-------------|-------------|
| 3.1 | `app/main.py` | NiceGUI app: trade cards, historic table, activity feed sidebar, auto-mode toggle, stats header | ~500 |
| 3.2 | `app/notifier.py` | Discord/Telegram webhook fire-and-forget notifications | ~60 |

**Milestone: Full working bot with dashboard on localhost:8080.**

### PHASE 4 — Harden & Ship

| Step | What |
|------|------|
| 4.1 | End-to-end test on testnet with real portal data |
| 4.2 | Edge case handling: restart state sync, partial exits, deduplication |
| 4.3 | DRY_RUN mode verified |
| 4.4 | Move to mainnet with small MARGIN_USD |
| 4.5 | Configs: requirements.txt, .env.example, .gitignore, README.md |

---

## Claude Code — How to Use It Efficiently

### Credit-saving rules

1. **One file at a time.** Don't ask Claude Code to "build the whole project" in one shot. Feed it one file's spec, get it right, move to the next.

2. **Use targeted edits.** Once a file exists, say "in hyperliquid_client.py, change the round_px function to also handle negative prices" — NOT "rewrite hyperliquid_client.py". Claude Code can do surgical str_replace edits.

3. **Paste the SPEC for each file, not the whole doc.** When building `portal.py`, paste only the Portal API section — not the entire game plan. Less input tokens = less cost.

4. **Test between phases.** Don't build all 5 files then debug. Build db.py + portal.py, run them, fix issues, THEN move to HL client. Bugs caught early cost 1 message to fix. Bugs caught late cost 10.

5. **Keep a CHANGELOG.** After each session, note what was built and what's next. Paste that as context in your next session instead of re-explaining everything.

6. **Don't repeat yourself.** If Claude Code already has the file open, don't re-describe what's in it. Say "add X to the existing function Y" and trust it can see the code.

### Prompt templates for Claude Code

**Starting a new file:**
```
Build app/db.py for my project. Here's the spec:
[paste only the relevant section]
Use async/await, SQLite WAL mode, thread-safe connections.
Write the complete file — no placeholders.
```

**Making a targeted edit:**
```
In app/portal.py, the _parse_activity_event method doesn't handle
the case where userExits is null. Add a null check that defaults
to an empty list.
```

**Adding a feature later:**
```
In app/main.py, add a "Total Fees Paid" stat to the dashboard
stats header, next to the existing PnL and Win Rate stats.
Pull the data from hl_closed_trades.fee column.
```

---

## File-by-File Spec (paste the relevant section into Claude Code)

### 1. app/db.py — Database Layer

```
SQLite with WAL mode, foreign keys ON.
Thread-safe (one connection per thread via threading.local).
All timestamps as ISO-8601 UTC strings.

Tables:
- hl_live_trades(trade_id INTEGER PK)
- hl_opened_trades(id AUTOINCREMENT, trade_id, coin, side, entry_price, entry_sl,
    size, margin, leverage, caller, at TEXT)
- hl_closed_trades(id AUTOINCREMENT, trade_id, coin, side, entry_price, exit_price,
    size, trade_value, margin, fee, pnl, close_type TEXT, at TEXT)
    close_type: 'automatic' | 'manual' | 'stop_triggered'
- hl_sl_updates(id AUTOINCREMENT, trade_id, old_stop, new_stop, size,
    original_size, trigger_conditions, at TEXT)
- hl_tp_updates(id AUTOINCREMENT, trade_id, tp_price, tp_pct, tp_num, size, fee, at TEXT)
- portal_events(id AUTOINCREMENT, trade_id, coin, side, caller, event_type TEXT,
    details TEXT JSON, at TEXT)
    event_type: 'enter' | 'cancel' | 'tp_hit' | 'auto_close' | 'stale_close' | 'sl_triggered'
- portal_cookies(key TEXT PK, value TEXT, at TEXT)

Helpers needed:
- add/remove/get/list live trades
- insert opened trade, insert closed trade
- insert SL update, insert TP update
- insert portal event
- get/set/clear portal cookies
- get stats (total PnL, win rate, open count)
- get historic trades (for dashboard table)
- check if coin is already live (for BLOCKED logic)
```

### 2. app/portal.py — Portal API Client

```
Async HTTP client using httpx.AsyncClient with cookie persistence.

Auth:
- POST /api/auth/login { email, password } → session cookie
- Persist cookies to SQLite portal_cookies table
- On startup: load cookies from DB, skip login if still valid
- On 401: re-login automatically, retry the failed request once

Endpoints:
- GET /api/portal/me/activity-feed → returns array of events
- GET /api/portal/me/trades → full trade list
- POST /api/portal/me/trades { tradeId } → follow a trade
- PATCH /api/portal/me/trades/{id} { userExitPrice, sizePct: 100 } → close

Event parsing — activity feed events map to:
- New trade opened (status: open, side, coin, entryPrice, stopLoss, takeProfits[])
- TP hit (partial close with sizePct < 100)
- Full close / SL trigger
- Stop loss update (new stopLoss value)

Caller filtering:
- WATCHED_CALLERS = {"voberoi", "pranayyyy", "corgil_"}
- Configurable via ALLOWED_CALLERS env var
- Ignore all trades where userTag not in whitelist
- Log ignored trades once per trade ID

Polling:
- Poll activity-feed every ~3 seconds
- Return parsed events as typed dataclasses/dicts for main.py to consume
```

### 3. app/hyperliquid_client.py — Hyperliquid SDK Wrapper

```
Uses: hyperliquid-python-sdk (Exchange, Info, Cloid)
Base URL: https://api.hyperliquid.xyz
WebSocket: wss://api.hyperliquid.xyz/ws

# ⚠️ IMPORTANT: HL_PRIVATE_KEY must be an API sub-wallet key.
# Generate at app.hyperliquid.xyz → API before first run with real funds.

Exchange init:
  Exchange(account, base_url, account_address=main_wallet, perp_dexs=["","xyz","cash","flx"])

Price rounding (MANDATORY — silent rejection if skipped):
  def round_px(px: float, sz_decimals: int) -> float:
      if px <= 0: return px
      max_dp = 6 - sz_decimals
      sig_dp = max(0, 5 - math.floor(math.log10(abs(px))) - 1)
      return round(px, min(max_dp, sig_dp))
  Apply to ALL prices: entry limit, SL trigger, TP.

Cloid (deterministic per trade):
  Cloid.from_str(f"0x{trade_id:032x}")

Size calculation:
  size = round(HL_MARGIN_USD * leverage / mid_price, sz_decimals)
  Cap leverage at asset's maxLeverage from info.meta()
  Enforce minimum $10 notional

Open trade — atomic bracket (entry + SL):
  exchange.bulk_orders([entry_order, sl_order], grouping="normalTpsl")
  Entry: limit IOC with 5% slippage
  SL: trigger market, reduce_only=True

Close trade — reduce-only market IOC

Partial TP — same as close but for (sizePct/100) * total_size

Update SL — exchange.modify_order() with original SL cloid

k-prefixed memecoins (PEPE, BONK, SHIB, FLOKI, DOGS, LUNC, NEIRO):
  Prefix "k" on HL symbol
  Scale stop: hl_stop = hl_mid * (portal_stop / portal_entry)

HIP-3 coins (BRENTOIL, XYZ100 etc.):
  Resolve via dex="xyz" / "cash" / "flx"
  Use per-dex allMids for pricing

WebSocket price feed:
  Connect to wss://api.hyperliquid.xyz/ws
  Subscribe: {"method":"subscribe","subscription":{"type":"allMids"}}
  Maintain shared dict of latest prices
  Auto-reconnect on disconnect

After close:
  Query user_fills_by_time() for real fee + closedPnl from HL fills

DRY_RUN mode:
  Log what would happen, return mock responses, place no real orders

Retry:
  Exponential backoff, max 3 retries on transient errors (connection, 5xx)
  Do NOT retry on order validation errors

Error handling:
  HL errors are in response.data.statuses[*].error — check this shape
```

### 4. app/notifier.py — Webhook Notifications

```
Fire-and-forget notifications via Discord or Telegram webhook.

Env var: NOTIFY_WEBHOOK_URL (optional, skip if empty)

Events to notify on:
- Position opened (coin, side, entry, leverage, size)
- Position closed (coin, side, entry, exit, PnL)
- SL triggered (coin, side, entry, stop price)
- TP hit (coin, side, TP level, sizePct)

Implementation:
- Async httpx POST to webhook URL
- Discord format: { "content": "message" }
- Best-effort: catch all exceptions, log warning, never crash the bot
- Include timestamp in all messages
```

### 5. app/main.py — NiceGUI Dashboard + Event Loop

```
NiceGUI app running at localhost:8080.

Layout:
┌─────────────────────────────────────────────┬──────────────────┐
│ [Auto Mode Toggle] [DRY RUN banner]         │                  │
│ [Stats: Total PnL | Win Rate | Open Count]  │  Activity Feed   │
├─────────────────────────────────────────────┤  (scrollable,    │
│                                             │   real-time)     │
│  Active Trade Cards                         │                  │
│  ┌─────────────────────────────────┐        │                  │
│  │ BTC LONG — voberoi — #560      │        │                  │
│  │ Entry: 75204  SL: 74000        │        │                  │
│  │ Mid: 75500  PnL: +$3.92       │        │                  │
│  │ ● LIVE          [Cancel]       │        │                  │
│  └─────────────────────────────────┘        │                  │
│  ┌─────────────────────────────────┐        │                  │
│  │ ETH SHORT — pranayyyy — #558   │        │                  │
│  │ BLOCKED (ETH already live)      │        │                  │
│  │            [Enter] (disabled)   │        │                  │
│  └─────────────────────────────────┘        │                  │
├─────────────────────────────────────────────┤                  │
│                                             │                  │
│  Historic Trades Table                      │                  │
│  ID | Date | Coin | Side | Caller |         │                  │
│  Entry | Close | PnL                        │                  │
│                                             │                  │
└─────────────────────────────────────────────┴──────────────────┘

Behavior:
- Auto mode toggle: per-session (resets on reload), default OFF
  When ON: auto-enters every new trade from watched callers
  Respects BLOCKED guard (coin already live) and coin availability
- Manual Enter button: opens position on HL for that trade
- Manual Cancel button: market-closes position on HL (red, LIVE only)
- ● LIVE badge: green dot, persisted in DB, survives page reload
- BLOCKED badge: yellow, shown when coin already has a live position
- DRY RUN banner: prominent warning when DRY_RUN=true
- Stats header: Total Realized PnL, Win Rate, Open Positions count
  Updated on every close event

Background loops (async, never block NiceGUI):
- Portal poll loop (~3s): fetch activity feed, parse events, route to handlers
- Price update: WS feed updates cards in real-time
- All HL calls run in executor/thread to not block UI

Event routing:
  new_trade → if auto_mode: open position, else: show card with Enter button
  stop_update → update SL on HL + DB
  tp_hit → partial close on HL + DB, check if fully closed
  full_close → close position on HL + DB, move card to historic
  sl_triggered → same as full_close with close_type='stop_triggered'

Startup sync:
  On boot, check HL open positions against DB live trades
  Reconcile any mismatches (positions opened manually, bot crashed mid-trade)

Logging:
  Rich console handler + rotating file (app.log, 5MB × 3 backups)
  Configurable via LOG_LEVEL env var
```

---

## Credentials Setup (do this AFTER code is built)

### Step 1: Portal credentials
1. Get your portal login email and password
2. Add to `.env`:
   ```
   PORTAL_USER=your@email.com
   PORTAL_PASSWORD=yourpassword
   ```

### Step 2: Hyperliquid API sub-wallet
1. Go to https://app.hyperliquid.xyz
2. Connect your main wallet
3. Navigate to API section
4. Generate an API sub-wallet (this creates a separate signing key)
5. Copy the private key and your main wallet address
6. Add to `.env`:
   ```
   HL_WALLET_ADDRESS=0x<your_main_wallet_address>
   HL_PRIVATE_KEY=0x<api_sub_wallet_private_key>
   ```
7. **NEVER** use your main wallet private key — always the API sub-wallet

### Step 3: Optional — Notifications
1. Create a Discord webhook (Server Settings → Integrations → Webhooks)
   OR use a Telegram bot webhook URL
2. Add to `.env`:
   ```
   NOTIFY_WEBHOOK_URL=https://discord.com/api/webhooks/...
   ```

### Step 4: Configure trading params
```
HL_LEVERAGE=10
HL_MARGIN_USD=100
DRY_RUN=true          # START with dry run!
ALLOWED_CALLERS=voberoi,pranayyyy,corgil_
LOG_LEVEL=INFO
```

### Step 5: First run
1. `DRY_RUN=true` — verify portal connection, event parsing, dashboard
2. Switch to HL testnet if available — verify order placement
3. `DRY_RUN=false` with small `HL_MARGIN_USD` on mainnet — monitor closely
4. Scale up once confident

---

## Extending the Dashboard Later

The NiceGUI architecture makes it straightforward to add features:

**Adding a new stat to the header:**
→ Add a query to db.py, call it in the stats refresh function in main.py

**Adding a new card field (e.g., time since entry):**
→ Edit the card builder function in main.py, compute from opened_at

**Adding a new page/tab (e.g., caller leaderboard):**
→ Add a new @ui.page route in main.py, query DB for caller-level stats

**Adding new trade filters (e.g., by caller, by coin):**
→ Add filter dropdowns to the UI, pass params to the DB query

**Adding charts (e.g., equity curve):**
→ NiceGUI supports plotly/matplotlib — query hl_closed_trades, plot cumulative PnL

**Adding new notification channels:**
→ Extend notifier.py with new transport methods

---

## Gotchas & Edge Cases

1. **Portal session expiry**: cookies expire — the 401 → re-login → retry flow is critical
2. **Race condition on partial exits**: TP hit events can arrive while a previous TP is still executing on HL — queue them
3. **Bot restart mid-trade**: startup sync MUST reconcile HL positions vs DB state
4. **k-coin price scaling**: PEPE on portal is raw price, on HL it's k-prefixed (1000x) — stop prices must be scaled proportionally
5. **round_px on EVERY price**: HL silently rejects orders with too many decimal places — this is the #1 cause of "order not filling" bugs
6. **Cloid uniqueness**: if you open the same trade_id twice (shouldn't happen with dedup, but), the cloid collision will fail — dedup is your first line of defense
7. **HIP-3 coins**: not all coins are on the default perp dex — the perp_dexs init param handles this but pricing must come from the correct dex's allMids
8. **NiceGUI event loop**: all HL/portal calls MUST be async or run in an executor — blocking the UI thread kills the dashboard
9. **429 from portal**: respect rate limits, implement exponential backoff
10. **Partial exit math**: close sizePct% of YOUR remaining position, not the original size — track cumulative exits
