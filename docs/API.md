# API reference

Every remote call the bot makes. Grouped by service.

---

## Corgi Portal API

Base URL: `https://portal.corgicalls.com` (overridable via `PORTAL_BASE_URL`).
Auth: **session cookie** obtained from `POST /api/portal/login`. Cookies persisted to SQLite (`portal_cookies` table).
All other endpoints expect the session cookie; on 401 the bot re-logins and retries once.

### `POST /api/portal/login`
Authenticate and receive a session cookie.

**Call site:** `PortalClient.login()` ([app/portal.py:263](app/portal.py))

**Request body:**
```json
{
  "username": "<PORTAL_USER from .env>",
  "password": "<PORTAL_PASSWORD from .env>"
}
```

**Responses:**
- `200 OK` → body + `Set-Cookie` session; cookies saved via `_save_cookies()`
- `401 Unauthorized` → `{"error":"Invalid username or password."}` → raises `PortalAuthError`
- `403 Forbidden` → raises `PortalAuthError`

---

### `GET /api/portal/me/activity-feed`
The firehose — up to ~50 recent events across every caller the user can see.

**Call site:** `PortalClient.get_activity_feed()` ([app/portal.py:319](app/portal.py))
Called every `PORTAL_POLL_INTERVAL` (default 3s) by `PortalClient.poll()`.

**Query params:** none

**Response shape** (real payload):
```json
{
  "events": [
    {
      "id": "trade_open_601",
      "type": "trade_opened",
      "timestamp": 1776865571374,
      "coin": "SILVER",
      "side": "long",
      "caller": "pranayyyy",
      "callerDiscordId": "457581276350644246",
      "tradeId": 601,
      "entryRaw": "78.2",
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
    }
  ],
  "latestTimestamp": 1776826676022,
  "latestPersonalizedTimestamp": 1776826676022
}
```

**Observed event types:**
- `trade_opened` — new trade published by a caller. Carries `tradeId`, `coin`, `side`, `caller`, `entryRaw` (string). NOT included: `stopLoss`, `takeProfits`, `leverage` — those require `get_trade_detail()`.
- `trade_updated` — dispatches on `updateType`:
  - `stop_moved` — SL change. Price embedded in `updateText` (regex-parsed: `"Stop moved to $75,000"`).
  - `tp_hit` — take-profit level hit. May carry `sizePct`, `tpPrice`, `tpNum`.
- `trade_closed` — final close. Carries `closePrice`, `pnlPct`, optionally `closeReason`.
- `bet_opened` / `bet_closed` — betting events (different namespace, `betId` not `tradeId`). **Silently skipped** by parser.

**Processing:**
- Events sorted ascending by `timestamp` before parsing (fix for open/close race).
- Each event's `id` checked against in-memory `_seen_event_ids` (capped 5000, pruned at limit).
- Non-whitelisted callers filtered via `ALLOWED_CALLERS`; log-once-per-trade_id for noise suppression.

---

### `POST /api/portal/me/trades`
Follow a trade. This is also the **enrichment endpoint** — the POST response body contains the full trade detail inline.

**Call site:**
- `PortalClient.follow_trade(trade_id)` ([app/portal.py:351](app/portal.py))
- `PortalClient.get_trade_detail(trade_id)` wraps it ([app/portal.py:363](app/portal.py))

**Request body:**
```json
{"tradeId": 601}
```

**Responses:**
- `201 Created` → full wrapper object with the trade detail embedded:

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

- `409 Conflict` → already following; bot handles gracefully by calling `GET /api/portal/me/trades` and finding the row
- `400` / `404` → trade closed / invalid → logged at DEBUG, returns `None`

**Used by:** `handle_new_trade` calls `get_trade_detail()` right before auto-entry to merge `stop`, `tp`, `leverage` into the event (they're not carried in the activity-feed `trade_opened` event).

---

### `GET /api/portal/me/trades`
List the trades you're currently following.

**Call site:** `PortalClient.get_trades()` ([app/portal.py:341](app/portal.py))
Primarily used as the fallback path in `get_trade_detail` when POST returns 409.

**Query params:** none

**Response shape:** list of wrapper objects (same shape as `POST /api/portal/me/trades` 201 response, one per followed trade).

---

### `PATCH /api/portal/me/trades/{tradeId}`
Mark a followed trade closed on the portal side (updates your portal-tracked PnL).

**Call site:** `PortalClient.close_trade(user_trade_id, user_exit_price, size_pct=100.0)` ([app/portal.py:416](app/portal.py))

**Request body:**
```json
{"userExitPrice": 85.8, "sizePct": 100}
```

> **Note:** currently defined but **not wired into any handler in the current build**. The bot closes positions on HL directly and relies on the portal's own close events to update internal state. Kept for future explicit-sync workflows.

---

### `GET /api/portal/me`
Account info / whoami.

**Call site:** only used in ad-hoc diagnostics during the session, not called by the running bot.

**Response shape:**
```json
{
  "accessState": "...", "accessStartAt": "...", "accessEndAt": "...",
  "accessSource": "...", "exemptRole": "...", "role": "...",
  "refCode": "...", "discordId": "...", "username": "pranay"
}
```

---

## Hyperliquid SDK calls

SDK: `hyperliquid-python-sdk` (imported as `hyperliquid.exchange.Exchange`, `hyperliquid.info.Info`, `hyperliquid.utils.signing.Cloid`).
Base URL: `https://api.hyperliquid.xyz` mainnet / `https://api.hyperliquid-testnet.xyz` testnet.
All SDK calls are sync — bot wraps them in `asyncio.to_thread()` to not block NiceGUI.

### `Exchange(account, base_url, account_address=main_wallet, perp_dexs=[...])`
Constructed in `HyperliquidClient.__init__` ([app/hyperliquid_client.py:295](app/hyperliquid_client.py)).

**Params:**
- `account`: `eth_account.Account.from_key(HL_PRIVATE_KEY)` — the API sub-wallet for signing
- `base_url`: mainnet/testnet REST base
- `account_address`: **main wallet address** (where the USDC is) — the sub-wallet signs on its behalf
- `perp_dexs`: list of dex names — filtered by `_probe_available_dexs()` at startup to only include dexes that return a valid meta universe (default dex `""` always first; HIP-3 dexes `xyz`, `cash`, `flx` added if live)

**Failure modes:**
- `KeyError: 'cash'` on testnet if we pass a dex the SDK doesn't recognize → mitigated by adaptive probe
- `User or API Wallet 0x… does not exist` on any order → sub-wallet not registered as an `extraAgents` entry on the main wallet

---

### `Info(base_url, skip_ws=True)`
Constructed alongside `Exchange`. WebSocket is managed separately by our own loop (`_ws_loop`), so `skip_ws=True`.

---

### `info.meta(dex="")` / `info.meta(dex="xyz")`
Fetches the universe of tradable perps on a given dex.

**Call sites:**
- `_probe_available_dexs()` at startup ([app/hyperliquid_client.py:145](app/hyperliquid_client.py))
- `_load_meta(dex)` lazily on first `resolve_asset` for a given dex ([app/hyperliquid_client.py:481](app/hyperliquid_client.py))

**Response shape:**
```json
{
  "universe": [
    {"name": "BTC", "szDecimals": 5, "maxLeverage": 40, ...},
    {"name": "ETH", "szDecimals": 4, "maxLeverage": 25, ...},
    ...
  ]
}
```

For HIP-3 dexes, names are prefixed: `"xyz:SILVER"`, `"xyz:AAPL"`, `"cash:..."`.

Result cached per dex in `HyperliquidClient._meta_cache`.

---

### `info.all_mids()` / `info.all_mids(dex="xyz")`
REST fallback for mid prices when the WS cache is cold.

**Call site:** `get_price_for_pricing()` ([app/hyperliquid_client.py:581](app/hyperliquid_client.py))

**Response shape:** `{"BTC": "82000.5", "ETH": "2353.1", ...}` (strings). Per-dex calls return HIP-3 prefixed keys: `{"xyz:SILVER": "82.19", ...}`.

---

### `info.user_state(address)` / `info.user_state(address, dex="xyz")`
Fetch a user's perps state (assetPositions, marginSummary, withdrawable).

**Call sites:**
- `_current_position_size(order_name)` ([app/hyperliquid_client.py:1121](app/hyperliquid_client.py)) — dex-routed by looking up `order_name` in `_asset_index`
- `open_positions()` ([app/hyperliquid_client.py:1155](app/hyperliquid_client.py)) — iterates every active dex, deduplicates by coin

**Response shape:**
```json
{
  "marginSummary": {
    "accountValue": "321.57", "totalNtlPos": "0.0",
    "totalRawUsd": "321.57", "totalMarginUsed": "0.0"
  },
  "withdrawable": "321.57",
  "assetPositions": [
    {"position": {
      "coin": "HYPE", "szi": "12.17", "entryPx": "41.086",
      "unrealizedPnl": "-1.42389",
      "leverage": {"type": "isolated", "value": 10}, ...
    }},
    ...
  ],
  "time": 1776878461625
}
```

> **Unified-account note:** on HyperCore unified accounts, Spot USDC serves as Perps margin automatically; `accountValue` on `clearinghouseState` reads $0 but trades still succeed. Use `spotClearinghouseState` (not in current bot, used only in diagnostics) to see the actual USDC balance.

---

### `info.user_fills_by_time(address, start_time_ms, end_time_ms)`
Real fill data: avg fill price, fee, realized `closedPnl`.

**Call site:** `_reconcile_fills(order_name, since_ms)` ([app/hyperliquid_client.py:1208](app/hyperliquid_client.py))
Called after every `open_trade` (populates `my_fill_price`) and every `close_trade` / `partial_tp` (populates `fee`, `pnl`, `avg_exit_price`).

**Response shape:** list of fill dicts:
```json
[
  {"coin": "HYPE", "side": "B", "px": "41.086", "sz": "12.17",
   "fee": "0.15", "closedPnl": "0.0", "time": 1776870000000, ...}
]
```

Bot aggregates by `coin`, sums fees + closedPnl, weights price by size to get avg.

---

### `info.frontend_open_orders(address)`
Lists all currently-resting orders for a user, including reduce-only triggers.

**Call site:** `_cancel_sls_for_trade(order_name, trade_id, dex)` ([app/hyperliquid_client.py:979](app/hyperliquid_client.py))
Used in the cancel+replace SL update pattern.

**Response shape:**
```json
[
  {"coin": "xyz:SILVER", "oid": 12345, "cloid": "0x...",
   "reduceOnly": true, "isTrigger": true,
   "orderType": "Stop Market", ...},
  ...
]
```

Bot filters by `coin == order_name` + `cloid` matches our deterministic SL cloid + `reduceOnly=True` + `isTrigger` — then bulk-cancels matches by `oid`.

---

### `exchange.update_leverage(leverage, coin, is_cross=False)`
Set per-asset leverage (isolated) before opening.

**Call site:** `_set_leverage(order_name, int(lev), cross=False, dex=dex)` ([app/hyperliquid_client.py:789](app/hyperliquid_client.py))
Best-effort — failures logged at DEBUG and don't block the order.

---

### `exchange.bulk_orders(orders, grouping="normalTpsl"|"na")`
Atomic multi-order submission.

**Call sites:**
- `open_trade` ([app/hyperliquid_client.py:752](app/hyperliquid_client.py)) — entry + SL bracket
- `_close_common` fallback ([app/hyperliquid_client.py:919](app/hyperliquid_client.py)) — for SDK versions where `exchange.order()` signature differs
- `update_stop` fallback ([app/hyperliquid_client.py:1075](app/hyperliquid_client.py))

**Order shape:**
```python
{
    "coin": order_name,          # dex-prefixed for HIP-3
    "is_buy": True,              # bool
    "sz": 12.17,                 # size (float)
    "limit_px": 41.09,           # round_px-rounded
    "order_type": {"limit": {"tif": "Ioc"}},          # entry / close
    # OR:
    "order_type": {"trigger": {
        "triggerPx": 39.5,
        "isMarket": True,
        "tpsl": "sl"             # or "tp"
    }},
    "reduce_only": False,         # True for SL + close
    "cloid": Cloid.from_str("0x..."),
}
```

**Grouping rules:**
- `"normalTpsl"` — required when the list contains a TP or SL leg alongside the entry. Fails with "Unexpected number of trigger orders" if submitted with just one order.
- `"na"` — for single-order submissions (when `portal_stop` is missing).

**HL error shape:** `response["response"]["data"]["statuses"][*]["error"]` — checked by `_check_hl_response`. Raises `HyperliquidValidationError` (never retried) on any error string in the list.

---

### `exchange.order(coin, is_buy, sz, limit_px, order_type, reduce_only=True, cloid=Cloid)`
Single-order submission (SDK convenience).

**Call sites:**
- `_close_common` ([app/hyperliquid_client.py:914](app/hyperliquid_client.py)) — primary close path
- `update_stop` ([app/hyperliquid_client.py:1060](app/hyperliquid_client.py)) — primary SL-replacement path

Falls back to `bulk_orders([order])` on `TypeError` (older SDK versions).

---

### `exchange.bulk_cancel(cancels)`
Cancel by `oid` (order ID).

**Call site:** `_cancel_sls_for_trade()` ([app/hyperliquid_client.py:996](app/hyperliquid_client.py))

**Cancel shape:**
```python
[{"coin": "xyz:SILVER", "oid": 12345}, ...]
```

Returns the standard HL status shape; checked by `_check_hl_response`.

---

### `Cloid.from_str(hex_128bit_str)`
Deterministic client order ID derived from `trade_id`:

- Entry: `Cloid.from_str(f"0x{trade_id:032x}")`
- SL: `Cloid.from_str(f"0x{(trade_id | (1<<127)):032x}")` (high bit set for disambiguation)

Means the same portal trade can never double-open on HL even after process restart (the `duplicate cloid` error is the backstop).

---

## Hyperliquid WebSocket

URL: `wss://api.hyperliquid.xyz/ws` mainnet / `wss://api.hyperliquid-testnet.xyz/ws` testnet.
Managed by `HyperliquidClient._ws_loop()` ([app/hyperliquid_client.py:398](app/hyperliquid_client.py)).

### Subscribe — `allMids` (default dex)
```json
{"method": "subscribe", "subscription": {"type": "allMids"}}
```

Emits plain keys: `"BTC"`, `"ETH"`, `"kPEPE"`.

### Subscribe — `allMids` per HIP-3 dex
After the default subscription, one additional subscribe per active HIP-3 dex:
```json
{"method": "subscribe", "subscription": {"type": "allMids", "dex": "xyz"}}
{"method": "subscribe", "subscription": {"type": "allMids", "dex": "cash"}}
{"method": "subscribe", "subscription": {"type": "allMids", "dex": "flx"}}
```

Emits prefixed keys: `"xyz:SILVER"`, `"xyz:AAPL"`, `"cash:..."`.

All keys merge into `HyperliquidClient.prices: dict[str, float]` keyed by the HL `order_name` format.

### Incoming message shape
```json
{"channel": "allMids", "data": {"mids": {"BTC": "82000.5", ...}}}
```
(Bot also handles the flat `{"channel":"allMids","data":{"BTC":"..."}}` variant defensively.)

### Reconnect
- `ping_interval=30`, `ping_timeout=20`, `max_size=10MB`
- On disconnect: exponential backoff 1s → 2s → 4s → … → 30s max
- `_ws_stop` asyncio.Event for clean shutdown from `on_shutdown`

---

## Hyperliquid REST info queries (outside the SDK)

Used only in ad-hoc diagnostics during the session; not called by the running bot.

### `POST /info {"type":"extraAgents","user":"0x…"}`
Returns the list of registered API sub-wallets for a main wallet:
```json
[
  {"name": "Copytrading", "address": "0x84fe…e52a",
   "validUntil": 1792422011829},
  ...
]
```

Used to confirm `HL_PRIVATE_KEY` derives to a wallet registered on the main wallet.

### `POST /info {"type":"spotClearinghouseState","user":"0x…"}`
Returns spot balances (USDC and all tokens). Needed on HyperCore/unified accounts where Spot USDC is the actual margin source.

```json
{
  "balances": [
    {"coin": "USDC", "total": "321.57", "hold": "0.0", "entryNtl": "0.0"},
    ...
  ]
}
```

### `POST /info {"type":"userNonFundingLedgerUpdates","user":"0x…","startTime":...,"endTime":...}`
Recent deposit/transfer history. Used to verify a deposit arrived.

### `POST /info {"type":"clearinghouseState","user":"0x…"}`
Equivalent to `info.user_state()` — used in curl one-liners where we don't want to spin up the full SDK.

---

## Discord / Telegram webhooks (optional)

Triggered by `NOTIFY_WEBHOOK_URL` env. Auto-detected from URL shape ([app/notifier.py:37](app/notifier.py)).

### Discord
URL contains `discord.com/api/webhooks` or `discordapp.com`.
POST body: `{"content": "<message>"}`.

### Telegram
URL contains `api.telegram.org/bot`.
POST body: `{"text": "<message>", "parse_mode": "HTML"}`.

### Trigger points
- `notify_opened` — after successful `open_trade` (uses real fill price when available)
- `notify_closed` — after full close (with PnL + fee)
- `notify_sl_triggered` — full-close with `stop_triggered=True`
- `notify_tp_hit` — after successful `partial_tp`

All calls are fire-and-forget via `loop.create_task(self._send_raw(msg))`; every error is logged and swallowed. The bot never crashes because of webhook failures.
