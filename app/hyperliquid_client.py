"""
Hyperliquid SDK wrapper for the Corgi Calls Copy Trading Bot.

Responsibilities
----------------
- Open / close / partial-TP / SL-update via hyperliquid-python-sdk
- Deterministic Cloid per trade_id
- MANDATORY price rounding (round_px) applied on every price sent to HL
- k-coin symbol remap and proportional stop scaling
- HIP-3 asset resolution across dexes ("", "xyz", "cash", "flx")
- WebSocket allMids feed with auto-reconnect and a shared price dict
- DRY_RUN mode (no real orders; log + mock responses)
- Exponential-backoff retry on transient errors only (max 3)
- Post-close fill reconciliation via user_fills_by_time()

All synchronous SDK calls run through asyncio.to_thread so they never block
the NiceGUI event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# ============================================================================
# ⚠️  CREDENTIALS / SAFETY NOTICE  ⚠️
# ----------------------------------------------------------------------------
# HL_PRIVATE_KEY **MUST** be an API sub-wallet key — NEVER your main wallet's
# private key. Generate one here before running with real funds:
#
#     1. https://app.hyperliquid.xyz
#     2. Connect your main wallet
#     3. API → Generate (creates a separate signing key)
#     4. Put that sub-wallet key in HL_PRIVATE_KEY
#     5. Put your MAIN wallet address in HL_WALLET_ADDRESS
#
# Using a main-wallet private key gives this bot full custody of your funds.
# ============================================================================

import websockets
from eth_account import Account

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

try:
    from hyperliquid.utils.signing import Cloid  # type: ignore
except ImportError:  # pragma: no cover
    from hyperliquid.utils.types import Cloid  # type: ignore  # older SDKs

from app import db

log = logging.getLogger(__name__)


# ============================================================
# SECTION: Constants
# ============================================================

HL_MAINNET_URL = "https://api.hyperliquid.xyz"
HL_MAINNET_WS  = "wss://api.hyperliquid.xyz/ws"
HL_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
HL_TESTNET_WS  = "wss://api.hyperliquid-testnet.xyz/ws"

K_COINS: frozenset[str] = frozenset(
    {"PEPE", "BONK", "SHIB", "FLOKI", "DOGS", "LUNC", "NEIRO"}
)
HIP3_DEXS: tuple[str, ...] = ("", "xyz", "cash", "flx")

MAX_RETRIES = 3
BASE_BACKOFF = 1.0
MAX_BACKOFF = 15.0
MIN_NOTIONAL_USD = 10.0
DEFAULT_SLIPPAGE = 0.05  # 5%

# Transient-error heuristics (substring match on exception/str)
_TRANSIENT_KEYWORDS = (
    "timeout", "timed out", "connection", "connection reset",
    "temporarily unavailable", "bad gateway", "service unavailable",
    "too many requests", "429", "502", "503", "504",
)


# ============================================================
# SECTION: Exceptions
# ============================================================

class HyperliquidError(RuntimeError):
    """Base class for HL wrapper errors."""


class HyperliquidValidationError(HyperliquidError):
    """Non-transient order rejection — do NOT retry."""


class HyperliquidTransientError(HyperliquidError):
    """Transient error — safe to retry with backoff."""


# ============================================================
# SECTION: Typed results
# ============================================================

@dataclass
class OpenResult:
    trade_id: int
    coin: str        # HL symbol (may be k-prefixed)
    side: str        # "long" | "short"
    size: float
    entry_price: float          # LIMIT price we sent (mid * slippage)
    stop_price: Optional[float]
    entry_cloid: str
    sl_cloid: Optional[str]
    dry_run: bool
    # Actual avg fill price reported by HL's user_fills_by_time after submit.
    # None if fill-reconcile failed or we're in dry_run. Dashboard/PnL code
    # should prefer this over entry_price when non-null.
    my_fill_price: Optional[float] = None
    fee: Optional[float] = None
    raw: dict = field(default_factory=dict)


@dataclass
class CloseResult:
    trade_id: int
    coin: str
    size: float
    avg_exit_price: Optional[float]
    fee: Optional[float]
    pnl: Optional[float]
    dry_run: bool
    raw: dict = field(default_factory=dict)


# ============================================================
# SECTION: Helpers
# ============================================================

def _is_transient(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, TimeoutError)):
        return True
    return any(k in msg for k in _TRANSIENT_KEYWORDS)


def _check_hl_response(resp: Any) -> dict:
    """
    Raise on HL error responses. Per API, errors appear at:
        resp["response"]["data"]["statuses"][i]["error"]
    Also accepts the alternate top-level {"status":"err","response":"..."} shape.
    """
    if not isinstance(resp, dict):
        return {"raw": resp}

    if resp.get("status") == "err":
        raise HyperliquidValidationError(str(resp.get("response") or resp))

    try:
        statuses = resp["response"]["data"]["statuses"]
    except (KeyError, TypeError):
        statuses = None

    if isinstance(statuses, list):
        errs = [s["error"] for s in statuses if isinstance(s, dict) and "error" in s]
        if errs:
            raise HyperliquidValidationError("; ".join(str(e) for e in errs))

    return resp


# ============================================================
# SECTION: Price rounding — MANDATORY on every price sent to HL
# ============================================================

def round_px(px: float, sz_decimals: int) -> float:
    """Round a price to HL's accepted precision.

    HL silently rejects orders with too many decimal places — this is the #1
    cause of "order not filling" bugs. Apply to EVERY price before send.
    """
    if px is None:
        return px
    if px <= 0:
        return px
    max_dp = 6 - int(sz_decimals)
    sig_dp = max(0, 5 - math.floor(math.log10(abs(px))) - 1)
    return round(px, min(max_dp, sig_dp))


# ============================================================
# SECTION: Symbol / k-coin helpers
# ============================================================

def hl_symbol_for(portal_coin: str) -> str:
    """Return HL symbol (with k-prefix for qualifying memecoins)."""
    s = portal_coin.upper().strip()
    if s in K_COINS:
        return "k" + s
    return s


def is_k_coin(portal_coin: str) -> bool:
    return portal_coin.upper().strip() in K_COINS


def scale_stop_for_k(
    portal_coin: str,
    portal_stop: Optional[float],
    portal_entry: Optional[float],
    hl_mid: Optional[float],
) -> Optional[float]:
    """For k-coins, stop must be scaled: hl_stop = hl_mid * (portal_stop/portal_entry)."""
    if portal_stop is None:
        return None
    if not is_k_coin(portal_coin):
        return portal_stop
    if not portal_entry or not hl_mid or portal_entry <= 0 or hl_mid <= 0:
        return portal_stop
    return hl_mid * (portal_stop / portal_entry)


def _probe_available_dexs(
    base_url: str, candidates: tuple[str, ...], timeout: float = 5.0,
) -> tuple[str, ...]:
    """
    Probe each candidate perp dex via POST /info {"type":"meta", "dex":...}.

    Only dexs that respond with a valid meta payload are returned. The empty
    string "" (the default dex) is always included first; if every probe fails
    we fall back to ("",) so the SDK still initializes.

    Used during Exchange() init to avoid KeyError from hardcoded dex names
    that don't exist on the current network (e.g. testnet lacks "cash"/"flx").
    """
    import httpx

    info_url = base_url.rstrip("/") + "/info"
    available: list[str] = []

    for dex in candidates:
        try:
            body = {"type": "meta"}
            if dex:
                body["dex"] = dex
            resp = httpx.post(info_url, json=body, timeout=timeout)
            if resp.status_code != 200:
                log.debug("dex probe %r → HTTP %s", dex, resp.status_code)
                continue
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("universe"), list):
                available.append(dex)
                log.debug("dex probe %r → ok (%d assets)", dex, len(data["universe"]))
            else:
                log.debug("dex probe %r → unexpected shape", dex)
        except Exception as exc:
            log.debug("dex probe %r failed: %s", dex, exc)

    if not available:
        log.warning("no perp dexs probed successfully — falling back to default")
        return ("",)

    # Ensure "" is always first so the default dex is the primary
    if "" in available:
        available = [""] + [d for d in available if d != ""]
    else:
        available = [""] + available
    return tuple(available)


def cloid_for(trade_id: int) -> Cloid:
    """Deterministic Cloid from trade_id (prevents double-open collisions)."""
    return Cloid.from_str(f"0x{int(trade_id):032x}")


def sl_cloid_for(trade_id: int) -> Cloid:
    """Deterministic SL-order Cloid (disjoint from entry cloid)."""
    return Cloid.from_str(f"0x{(int(trade_id) | (1 << 127)):032x}")


# ============================================================
# SECTION: HyperliquidClient
# ============================================================

class HyperliquidClient:
    """High-level async wrapper around hyperliquid-python-sdk + WS feed."""

    def __init__(
        self,
        private_key: Optional[str] = None,
        main_address: Optional[str] = None,
        base_url: Optional[str] = None,
        ws_url: Optional[str] = None,
        testnet: Optional[bool] = None,
        dry_run: Optional[bool] = None,
        leverage: Optional[float] = None,
        margin_usd: Optional[float] = None,
    ) -> None:
        # Config resolution: args → env → defaults
        private_key = private_key or os.environ.get("HL_PRIVATE_KEY") or ""
        main_address = main_address or os.environ.get("HL_WALLET_ADDRESS") or ""

        if testnet is None:
            testnet = os.environ.get("HL_TESTNET", "").lower() in ("1", "true", "yes")
        self.testnet = bool(testnet)

        self.base_url = (
            base_url
            or os.environ.get("HL_BASE_URL")
            or (HL_TESTNET_URL if self.testnet else HL_MAINNET_URL)
        )
        self.ws_url = (
            ws_url
            or os.environ.get("HL_WS_URL")
            or (HL_TESTNET_WS if self.testnet else HL_MAINNET_WS)
        )

        if dry_run is None:
            dry_run = os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes")
        self.dry_run = bool(dry_run)

        self.default_leverage = float(
            leverage if leverage is not None else os.environ.get("HL_LEVERAGE", "10")
        )
        self.margin_usd = float(
            margin_usd if margin_usd is not None else os.environ.get("HL_MARGIN_USD", "100")
        )

        # Margin mode: "isolated" (default) or "cross". Read every open from
        # this attribute so HL UI changes the user makes manually don't
        # collide with the bot's pre-trade update_leverage call. Configurable
        # via HL_MARGIN_MODE env var.
        margin_mode = os.environ.get("HL_MARGIN_MODE", "isolated").strip().lower()
        if margin_mode not in ("isolated", "cross"):
            log.warning(
                "HL_MARGIN_MODE=%r is not 'isolated' or 'cross' — defaulting to isolated",
                margin_mode,
            )
            margin_mode = "isolated"
        self.margin_mode = margin_mode
        self.use_cross_margin = (margin_mode == "cross")

        # Dex resolution priority. The first dex in this list (after the
        # default "") that lists a coin wins in resolve_asset(). Configurable
        # via HL_DEX_PRIORITY env var (comma-separated). Default order
        # ("", "xyz", "cash", "flx") matches HIP3_DEXS.
        prio_env = os.environ.get("HL_DEX_PRIORITY", "").strip()
        if prio_env:
            user_priority = tuple(d.strip() for d in prio_env.split(",") if d.strip())
        else:
            user_priority = tuple(d for d in HIP3_DEXS if d)  # ("xyz","cash","flx")
        # Default dex "" is ALWAYS probed first; user's priority controls
        # the ORDER of HIP-3 dexes after it.
        candidates = ("",) + tuple(d for d in user_priority if d)
        self._dex_priority_request: tuple[str, ...] = candidates

        self.main_address = main_address

        # Lazy SDK init — real Exchange init requires a key
        self._exchange: Optional[Exchange] = None
        self._info: Optional[Info] = None
        self._active_dexs: tuple[str, ...] = ("",)
        if private_key and main_address:
            try:
                account = Account.from_key(private_key)
                # Probe with the user-configured priority order, not a static tuple.
                # _probe_available_dexs preserves input order and always keeps "" first.
                self._active_dexs = _probe_available_dexs(
                    self.base_url, self._dex_priority_request,
                )
                self._exchange = Exchange(
                    account,
                    self.base_url,
                    account_address=main_address,
                    perp_dexs=list(self._active_dexs),
                )
                self._info = Info(self.base_url, skip_ws=True)
                log.info(
                    "HL wrapper ready (%s, dry_run=%s, margin_mode=%s, dexs=%s)",
                    "TESTNET" if self.testnet else "MAINNET",
                    self.dry_run,
                    self.margin_mode,
                    list(self._active_dexs),
                )
            except Exception:
                log.exception("failed to initialize HL SDK; operating in degraded mode")
                self._exchange = None
                self._info = None
        else:
            if not self.dry_run:
                log.warning(
                    "HL_PRIVATE_KEY / HL_WALLET_ADDRESS not set — forcing DRY_RUN=True"
                )
                self.dry_run = True

        # Meta cache per dex (universe info)
        self._meta_cache: dict[str, dict] = {}
        self._asset_index: dict[str, dict] = {}  # hl_symbol -> asset info
        self._meta_lock = asyncio.Lock()

        # Shared live-price dict (written by WS task, read by everyone)
        self.prices: dict[str, float] = {}
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_stop: asyncio.Event = asyncio.Event()

        # Track current SL cloid per trade_id for modify_order()
        self._sl_cloids: dict[int, Cloid] = {}

        # Phase-2: callback fired whenever HL userEvents WS reports a
        # change to user state (fills, manual closes, manual SL moves).
        # main.py registers a function that flips an asyncio.Event so the
        # debounced reconciler picks it up.
        # Signature: callback(channel: str, data: dict) -> None  (sync, fast).
        self._user_change_callback: Optional[Any] = None
        # Track liveness of userEvents subscription for diagnostics
        self.last_user_event_ms: int = 0

    # ------------------------------------------------------------------
    # Public: WS lifecycle
    # ------------------------------------------------------------------

    def set_user_change_callback(self, fn) -> None:
        """Register a callback fired on every HL userEvents WS message.

        Called from the WS task's event loop (so the callback should be
        cheap and non-blocking — typically just sets an asyncio.Event).
        Used by main.hl_change_reconciler to trigger fast reconciles.
        """
        self._user_change_callback = fn

    async def start_price_feed(self) -> None:
        """Start the allMids + userEvents WebSocket task."""
        if self._ws_task is not None and not self._ws_task.done():
            return
        self._ws_stop.clear()
        self._ws_task = asyncio.create_task(self._ws_loop(), name="hl-ws-feed")

    async def stop_price_feed(self) -> None:
        self._ws_stop.set()
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ws_task = None

    async def _ws_loop(self) -> None:
        backoff = 1.0
        while not self._ws_stop.is_set():
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=30,
                    ping_timeout=20,
                    max_size=10 * 1024 * 1024,
                ) as ws:
                    # Default (main) dex allMids — emits plain "BTC", "kPEPE", etc.
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "allMids"},
                    }))
                    # Per-dex allMids for each active HIP-3 dex — emits
                    # "xyz:SILVER", "cash:...", etc. All keys land in the
                    # same self.prices dict; key format matches `order_name`
                    # from resolve_asset so lookups work uniformly.
                    for dex in self._active_dexs:
                        if not dex:
                            continue
                        try:
                            await ws.send(json.dumps({
                                "method": "subscribe",
                                "subscription": {"type": "allMids", "dex": dex},
                            }))
                        except Exception as exc:
                            log.debug("allMids subscribe dex=%r failed: %s", dex, exc)

                    # Phase-2: subscribe to userEvents for the main wallet so
                    # any HL-side state change (fills, manual closes, manual
                    # SL moves, liquidations) gets pushed to us in real time.
                    # We only subscribe when we have an address — DRY_RUN /
                    # missing-key cases skip this safely.
                    if self.main_address:
                        try:
                            await ws.send(json.dumps({
                                "method": "subscribe",
                                "subscription": {
                                    "type": "userEvents",
                                    "user": self.main_address,
                                },
                            }))
                            log.info(
                                "HL userEvents subscribed (user=0x%s…%s)",
                                self.main_address[2:6], self.main_address[-4:],
                            )
                        except Exception as exc:
                            log.warning("HL userEvents subscribe failed: %s", exc)

                    backoff = 1.0
                    log.info(
                        "HL price feed connected (subscribed default + %s)",
                        [d for d in self._active_dexs if d],
                    )
                    async for raw in ws:
                        if self._ws_stop.is_set():
                            break
                        self._handle_ws_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("HL WS disconnected: %s (retry in %.1fs)", exc, backoff)
                try:
                    await asyncio.wait_for(self._ws_stop.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)

    def _handle_ws_message(self, raw: Any) -> None:
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return

        channel = msg.get("channel")

        if channel == "allMids":
            data = msg.get("data") or {}
            mids = data.get("mids") if isinstance(data, dict) and "mids" in data else data
            if not isinstance(mids, dict):
                return
            updated = 0
            for coin, px in mids.items():
                try:
                    self.prices[coin] = float(px)
                    updated += 1
                except (ValueError, TypeError):
                    continue
            if updated and log.isEnabledFor(logging.DEBUG):
                log.debug("allMids tick: %d symbols", updated)
            return

        if channel == "userEvents":
            self._handle_user_events(msg)
            return

        # Subscription ack and other channels — ignore quietly
        return

    def _handle_user_events(self, msg: dict) -> None:
        """Phase-2: HL pushed a user-state change. Trigger downstream
        debounced reconcile via the registered callback.

        Payload shapes (varies by event type):
          {"channel": "userEvents", "data": {"fills": [...]}}
          {"channel": "userEvents", "data": {"funding": {...}}}
          {"channel": "userEvents", "data": {"liquidation": {...}}}
          {"channel": "userEvents", "data": {"nonUserCancel": [...]}}
        We don't try to mutate state from here — the reconcile pass owns
        all DB writes. We just bump liveness + invoke the callback.
        """
        import time as _time
        self.last_user_event_ms = int(_time.time() * 1000)

        data = msg.get("data") or {}
        # Brief log so the user sees these in app.log
        kinds = []
        for key in ("fills", "funding", "liquidation", "nonUserCancel", "twapHistory"):
            v = data.get(key)
            if v is not None:
                if isinstance(v, list):
                    kinds.append(f"{key}={len(v)}")
                else:
                    kinds.append(key)
        if kinds:
            log.info("HL userEvents: %s", ", ".join(kinds))

        cb = self._user_change_callback
        if cb is not None:
            try:
                cb(channel="userEvents", data=data)
            except Exception:
                log.exception("user_change_callback raised")

    def get_mid(self, coin: str) -> Optional[float]:
        """Return cached mid price for an HL coin (None if not yet seen)."""
        return self.prices.get(coin)

    # ------------------------------------------------------------------
    # Meta / asset resolution (HIP-3 aware)
    # ------------------------------------------------------------------

    async def _load_meta(self, dex: str) -> dict:
        assert self._info is not None
        def _call():
            try:
                return self._info.meta(dex=dex) if dex else self._info.meta()
            except TypeError:
                return self._info.meta()
        return await asyncio.to_thread(_call)

    async def resolve_asset(self, portal_coin: str) -> dict:
        """Locate an asset across HIP-3 dexes.

        Returns dict: {hl_coin, order_name, dex, sz_decimals, max_leverage, asset_id}.

        - `hl_coin` is the logical symbol for k-coin handling + logging ("SILVER",
          "kPEPE", "BTC").
        - `order_name` is the HL-addressable name used in order submissions,
          user_state, and allMids lookups. For HIP-3 assets it's prefixed:
          "xyz:SILVER", "cash:...". For the default dex it's the same as hl_coin.
        """
        hl_coin = hl_symbol_for(portal_coin)
        cached = self._asset_index.get(hl_coin)
        if cached is not None:
            return cached

        async with self._meta_lock:
            cached = self._asset_index.get(hl_coin)
            if cached is not None:
                return cached

            if self._info is None:
                # Degraded mode (no SDK): return sensible defaults.
                info = {
                    "hl_coin": hl_coin, "order_name": hl_coin,
                    "dex": "", "sz_decimals": 4,
                    "max_leverage": 50, "asset_id": None,
                }
                self._asset_index[hl_coin] = info
                return info

            for dex in self._active_dexs:
                try:
                    meta = self._meta_cache.get(dex) or await self._load_meta(dex)
                    self._meta_cache[dex] = meta
                except Exception as exc:
                    log.debug("meta load failed for dex=%r: %s", dex, exc)
                    continue
                universe = meta.get("universe") or []
                # Names can appear either as "SILVER" (default dex) or
                # "xyz:SILVER" (HIP-3 dex). Match either.
                dex_prefixed = f"{dex}:{hl_coin}" if dex else hl_coin
                for idx, asset in enumerate(universe):
                    name = asset.get("name")
                    if name == hl_coin or name == dex_prefixed:
                        info = {
                            "hl_coin": hl_coin,
                            "order_name": name,
                            "dex": dex,
                            "sz_decimals": int(asset.get("szDecimals", 4)),
                            "max_leverage": int(asset.get("maxLeverage", 50)),
                            "asset_id": idx,
                        }
                        self._asset_index[hl_coin] = info
                        log.info(
                            "resolved %s → order_name=%r dex=%r sz_dec=%d max_lev=%d",
                            hl_coin, name, dex, info["sz_decimals"], info["max_leverage"],
                        )
                        return info

            raise HyperliquidValidationError(
                f"asset {portal_coin!r} (hl={hl_coin!r}) not found on any dex"
            )

    async def get_price_for_pricing(self, portal_coin: str) -> Optional[float]:
        """Best-available price for sizing (WS dict → REST fallback).

        Keys prices by the HL order_name (prefixed for HIP-3 assets) since that
        matches both the WS allMids broadcast keys and per-dex REST all_mids keys.
        """
        hl_coin = hl_symbol_for(portal_coin)
        # Before resolve: speculative lookup by hl_coin (default dex case)
        px = self.get_mid(hl_coin)
        if px:
            return px
        if self._info is None:
            return None
        try:
            asset = await self.resolve_asset(portal_coin)
            dex = asset["dex"]
            order_name = asset["order_name"]
            # Try WS cache under order_name (covers HIP-3)
            px = self.get_mid(order_name)
            if px:
                return px
            # REST fallback for the specific dex
            def _call():
                try:
                    return self._info.all_mids(dex=dex) if dex else self._info.all_mids()
                except TypeError:
                    return self._info.all_mids()
            mids = await asyncio.to_thread(_call)
            if not isinstance(mids, dict):
                return None
            # Accept both prefixed and bare forms just in case
            val = mids.get(order_name) or mids.get(hl_coin)
            return float(val) if val is not None else None
        except Exception:
            log.exception("failed to fetch REST mid for %s", portal_coin)
            return None

    # ------------------------------------------------------------------
    # Size / leverage
    # ------------------------------------------------------------------

    def _compute_size(
        self, mid_price: float, leverage: float, sz_decimals: int
    ) -> float:
        raw_size = (self.margin_usd * leverage) / mid_price
        size = round(raw_size, int(sz_decimals))
        notional = size * mid_price
        if notional < MIN_NOTIONAL_USD:
            raise HyperliquidValidationError(
                f"computed notional ${notional:.2f} below min ${MIN_NOTIONAL_USD}"
            )
        return size

    def _capped_leverage(self, desired: float, max_leverage: float) -> float:
        return float(min(max(desired, 1.0), max_leverage))

    # ------------------------------------------------------------------
    # Retry wrapper (transient-only, max 3)
    # ------------------------------------------------------------------

    async def _with_retry(self, fn, *, desc: str):
        last_exc: Optional[BaseException] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = await asyncio.to_thread(fn)
                return _check_hl_response(result) if isinstance(result, dict) else result
            except HyperliquidValidationError:
                raise  # never retry validation errors
            except Exception as exc:
                last_exc = exc
                if not _is_transient(exc) or attempt == MAX_RETRIES:
                    log.error("%s failed (attempt %d): %s", desc, attempt, exc)
                    raise
                backoff = min(MAX_BACKOFF, BASE_BACKOFF * (2 ** (attempt - 1)))
                backoff += random.uniform(0, backoff * 0.2)
                log.warning(
                    "%s transient error (attempt %d/%d): %s — retrying in %.1fs",
                    desc, attempt, MAX_RETRIES, exc, backoff,
                )
                await asyncio.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------
    # Open — atomic bracket (entry + SL) via bulk_orders("normalTpsl")
    # ------------------------------------------------------------------

    async def open_trade(
        self,
        *,
        trade_id: int,
        portal_coin: str,
        side: str,
        portal_entry: Optional[float] = None,
        portal_stop: Optional[float] = None,
        leverage: Optional[float] = None,
        slippage: float = DEFAULT_SLIPPAGE,
    ) -> OpenResult:
        side_l = side.lower()
        if side_l not in ("long", "short", "buy", "sell"):
            raise HyperliquidValidationError(f"invalid side: {side!r}")
        is_buy = side_l in ("long", "buy")

        asset = await self.resolve_asset(portal_coin)
        hl_coin = asset["hl_coin"]
        order_name = asset["order_name"]
        sz_dec = asset["sz_decimals"]
        max_lev = asset["max_leverage"]
        dex = asset["dex"]

        lev = self._capped_leverage(
            float(leverage if leverage is not None else self.default_leverage),
            max_lev,
        )

        mid = await self.get_price_for_pricing(portal_coin)
        if mid is None or mid <= 0:
            raise HyperliquidValidationError(
                f"no live mid price for {order_name}; cannot size order"
            )

        size = self._compute_size(mid, lev, sz_dec)

        # Scale stop for k-coins, then ALWAYS round_px every price
        hl_stop = scale_stop_for_k(portal_coin, portal_stop, portal_entry, mid)

        # Entry limit with ±slippage, IOC
        slip_mul = (1 + slippage) if is_buy else (1 - slippage)
        entry_px = round_px(mid * slip_mul, sz_dec)

        sl_px = round_px(hl_stop, sz_dec) if hl_stop is not None else None

        entry_cloid = cloid_for(trade_id)
        sl_cloid = sl_cloid_for(trade_id) if sl_px is not None else None

        log.info(
            "OPEN %s #%s %s %s sz=%s entry=%s sl=%s lev=%sx dex=%r dry_run=%s",
            order_name, trade_id, side_l, "BUY" if is_buy else "SELL",
            size, entry_px, sl_px, lev, dex, self.dry_run,
        )

        if self.dry_run:
            self._sl_cloids[trade_id] = sl_cloid if sl_cloid else cloid_for(trade_id)
            return OpenResult(
                trade_id=trade_id, coin=order_name, side=side_l,
                size=size, entry_price=entry_px, stop_price=sl_px,
                entry_cloid=entry_cloid.to_raw() if hasattr(entry_cloid, "to_raw") else str(entry_cloid),
                sl_cloid=(sl_cloid.to_raw() if sl_cloid and hasattr(sl_cloid, "to_raw") else (str(sl_cloid) if sl_cloid else None)),
                dry_run=True,
                raw={"mock": True, "status": "ok"},
            )

        if self._exchange is None:
            raise HyperliquidError("SDK not initialized (missing credentials)")

        # Set per-asset leverage — best-effort; not atomic w/ order.
        # Margin mode comes from HL_MARGIN_MODE env (default isolated). This
        # used to be hardcoded cross=False, which silently overrode any UI
        # margin-mode change the user made.
        await self._set_leverage(
            order_name, int(lev), cross=self.use_cross_margin, dex=dex,
        )

        # Build the atomic bracket
        entry_order = {
            "coin": order_name,
            "is_buy": is_buy,
            "sz": size,
            "limit_px": entry_px,
            "order_type": {"limit": {"tif": "Ioc"}},
            "reduce_only": False,
            "cloid": entry_cloid,
        }

        orders: list[dict] = [entry_order]

        if sl_px is not None and sl_cloid is not None:
            sl_order = {
                "coin": order_name,
                "is_buy": not is_buy,
                "sz": size,
                "limit_px": sl_px,
                "order_type": {"trigger": {
                    "triggerPx": sl_px,
                    "isMarket": True,
                    "tpsl": "sl",
                }},
                "reduce_only": True,
                "cloid": sl_cloid,
            }
            orders.append(sl_order)

        exchange = self._exchange
        # "normalTpsl" grouping requires the SL (and/or TP) legs to be present.
        # When portal_stop is missing, we only send the entry order, in which
        # case HL rejects the bracket with "Unexpected number of trigger orders".
        # Use "na" grouping for single-order submissions.
        grouping = "normalTpsl" if len(orders) > 1 else "na"

        def _submit():
            try:
                return exchange.bulk_orders(orders, grouping=grouping)
            except TypeError:
                # Some SDK versions take a different signature
                return exchange.bulk_orders(orders)

        open_started_at = int(time.time() * 1000)
        resp = await self._with_retry(_submit, desc=f"bulk_orders #{trade_id}")

        # Track SL cloid for future modify_order() calls
        if sl_cloid is not None:
            self._sl_cloids[trade_id] = sl_cloid

        # Reconcile the REAL average fill price for the entry.
        # entry_px (above) is only the slippage-adjusted limit (mid*1.05 for longs)
        # — using it as the PnL basis on dashboard cards would overstate losses
        # by the full slippage cushion. Query user_fills_by_time for the actual
        # fill(s) that just happened, average by size.
        fee, _pnl_unused, avg_fill_px = await self._reconcile_fills(
            order_name, open_started_at,
        )
        if avg_fill_px is not None:
            log.info(
                "fill reconciled #%s: limit=%s avg_fill=%s fee=%s",
                trade_id, entry_px, avg_fill_px, fee,
            )

        return OpenResult(
            trade_id=trade_id, coin=order_name, side=side_l,
            size=size, entry_price=entry_px, stop_price=sl_px,
            entry_cloid=entry_cloid.to_raw() if hasattr(entry_cloid, "to_raw") else str(entry_cloid),
            sl_cloid=(sl_cloid.to_raw() if sl_cloid and hasattr(sl_cloid, "to_raw") else (str(sl_cloid) if sl_cloid else None)),
            dry_run=False,
            my_fill_price=avg_fill_px,
            fee=fee,
            raw=resp if isinstance(resp, dict) else {"raw": resp},
        )

    async def _set_leverage(
        self, hl_coin: str, leverage: int, *, cross: bool = False, dex: str = ""
    ) -> None:
        if self._exchange is None or self.dry_run:
            return
        exchange = self._exchange

        def _call():
            try:
                return exchange.update_leverage(leverage, hl_coin, is_cross=cross)
            except TypeError:
                try:
                    return exchange.update_leverage(leverage, hl_coin, cross)
                except Exception:
                    return None
            except Exception:
                return None

        try:
            await asyncio.to_thread(_call)
        except Exception:
            log.debug("set_leverage failed (non-fatal)", exc_info=True)

    # ------------------------------------------------------------------
    # Close — reduce-only market IOC (full)
    # ------------------------------------------------------------------

    async def close_trade(
        self,
        *,
        trade_id: int,
        portal_coin: str,
        side: str,
        size: Optional[float] = None,
    ) -> CloseResult:
        """Full-size market close, reduce-only."""
        return await self._close_common(
            trade_id=trade_id, portal_coin=portal_coin, side=side,
            size=size, size_pct=100.0, is_partial=False,
        )

    async def partial_tp(
        self,
        *,
        trade_id: int,
        portal_coin: str,
        side: str,
        size_pct: float,
        current_size: Optional[float] = None,
    ) -> CloseResult:
        """Close (size_pct/100) * current_size of the position."""
        if size_pct <= 0 or size_pct >= 100:
            raise HyperliquidValidationError(
                f"partial_tp size_pct must be in (0,100), got {size_pct}"
            )
        return await self._close_common(
            trade_id=trade_id, portal_coin=portal_coin, side=side,
            size=current_size, size_pct=size_pct, is_partial=True,
        )

    async def _close_common(
        self, *, trade_id: int, portal_coin: str, side: str,
        size: Optional[float], size_pct: float, is_partial: bool,
    ) -> CloseResult:
        side_l = side.lower()
        is_buy_original = side_l in ("long", "buy")
        # reduce-only: opposite side
        exit_is_buy = not is_buy_original

        asset = await self.resolve_asset(portal_coin)
        hl_coin = asset["hl_coin"]
        order_name = asset["order_name"]
        sz_dec = asset["sz_decimals"]

        # Determine size to close (query HL by order_name for HIP-3 correctness)
        if size is None:
            size = await self._current_position_size(order_name)
        if size is None or size <= 0:
            raise HyperliquidValidationError(
                f"no open size for {order_name} to close (trade_id={trade_id})"
            )
        close_size = round(size * (size_pct / 100.0), sz_dec)
        if close_size <= 0:
            raise HyperliquidValidationError("close_size rounded to 0")

        mid = await self.get_price_for_pricing(portal_coin)
        if mid is None or mid <= 0:
            raise HyperliquidValidationError(f"no mid for {order_name}")

        # Market-IOC is a limit with aggressive price past the mid
        slip_mul = 1.05 if exit_is_buy else 0.95
        limit_px = round_px(mid * slip_mul, sz_dec)

        log.info(
            "%s %s #%s sz=%s mid=%s limit=%s dry_run=%s",
            "PARTIAL_TP" if is_partial else "CLOSE",
            order_name, trade_id, close_size, mid, limit_px, self.dry_run,
        )

        if self.dry_run:
            return CloseResult(
                trade_id=trade_id, coin=order_name, size=close_size,
                avg_exit_price=limit_px, fee=0.0, pnl=0.0, dry_run=True,
                raw={"mock": True, "status": "ok"},
            )

        if self._exchange is None:
            raise HyperliquidError("SDK not initialized")

        exchange = self._exchange
        close_cloid_num = int(trade_id) + (2 << 120) + int(time.time()) % (1 << 100)
        close_cloid = Cloid.from_str(f"0x{close_cloid_num & ((1 << 128) - 1):032x}")

        order = {
            "coin": order_name,
            "is_buy": exit_is_buy,
            "sz": close_size,
            "limit_px": limit_px,
            "order_type": {"limit": {"tif": "Ioc"}},
            "reduce_only": True,
            "cloid": close_cloid,
        }

        def _submit():
            try:
                return exchange.order(
                    order["coin"], order["is_buy"], order["sz"], order["limit_px"],
                    order["order_type"], reduce_only=True, cloid=close_cloid,
                )
            except TypeError:
                return exchange.bulk_orders([order])

        close_started_at = int(time.time() * 1000)
        resp = await self._with_retry(_submit, desc=f"close #{trade_id}")

        # Reconcile real fills/fees/pnl — HL user_fills_by_time reports coin
        # as order_name for HIP-3 too
        fee, pnl, avg_px = await self._reconcile_fills(order_name, close_started_at)

        # If fully closed, drop the tracked SL cloid
        if not is_partial:
            self._sl_cloids.pop(trade_id, None)

        return CloseResult(
            trade_id=trade_id, coin=order_name, size=close_size,
            avg_exit_price=avg_px if avg_px is not None else limit_px,
            fee=fee, pnl=pnl, dry_run=False,
            raw=resp if isinstance(resp, dict) else {"raw": resp},
        )

    # ------------------------------------------------------------------
    # SL update — cancel + replace
    # ------------------------------------------------------------------
    # NOTE: HL's exchange.modify_order() does NOT work on already-resting
    # trigger orders — they come back as "canceled or filled" because HL
    # treats triggers as independent conditional orders, not modifiable
    # limit orders. The correct pattern (matching the reference repo) is:
    #   1. list frontend_open_orders
    #   2. filter to this trade's trigger SL (by coin + cloid + trigger flag)
    #   3. bulk_cancel by oid
    #   4. place a fresh SL trigger with the SAME cloid and new price
    # ------------------------------------------------------------------

    def _cancel_sls_for_trade(
        self, order_name: str, trade_id: int, dex: str = "",
    ) -> int:
        """Cancel every open SL trigger for this trade on HL. Returns count.

        `order_name` must match the HL-side coin string — which is the
        dex-prefixed name for HIP-3 assets (e.g. "xyz:SILVER").
        """
        if self._exchange is None or self._info is None or not self.main_address:
            return 0
        sl_cloid = self._sl_cloids.get(trade_id) or sl_cloid_for(trade_id)
        cloid_raw = sl_cloid.to_raw().lower() if hasattr(sl_cloid, "to_raw") else str(sl_cloid).lower()

        try:
            orders = self._info.frontend_open_orders(self.main_address)
        except Exception as exc:
            log.warning("frontend_open_orders failed for #%s: %s", trade_id, exc)
            return 0

        to_cancel: list[dict] = []
        for o in orders or []:
            if not isinstance(o, dict):
                continue
            if o.get("coin") != order_name:
                continue
            if not o.get("reduceOnly"):
                continue
            ocloid = (o.get("cloid") or "").lower()
            if ocloid != cloid_raw:
                continue
            otype = str(o.get("orderType") or "").lower()
            is_trigger = (
                o.get("isTrigger")
                or "stop" in otype
                or "trigger" in otype
            )
            if not is_trigger:
                continue
            to_cancel.append({"coin": order_name, "oid": o["oid"]})

        if not to_cancel:
            return 0

        try:
            resp = self._exchange.bulk_cancel(to_cancel)
            log.info("canceled %d stale SL order(s) for #%s", len(to_cancel), trade_id)
            _check_hl_response(resp) if isinstance(resp, dict) else None
        except Exception as exc:
            log.warning("bulk_cancel failed for #%s: %s", trade_id, exc)
            return 0
        return len(to_cancel)

    async def update_stop(
        self,
        *,
        trade_id: int,
        portal_coin: str,
        side: str,
        new_portal_stop: float,
        portal_entry: Optional[float] = None,
        size: Optional[float] = None,
    ) -> dict:
        side_l = side.lower()
        is_buy_original = side_l in ("long", "buy")
        asset = await self.resolve_asset(portal_coin)
        hl_coin = asset["hl_coin"]
        order_name = asset["order_name"]
        sz_dec = asset["sz_decimals"]
        dex = asset.get("dex", "")

        mid = await self.get_price_for_pricing(portal_coin)
        scaled = scale_stop_for_k(portal_coin, new_portal_stop, portal_entry, mid)
        new_stop_px = round_px(scaled, sz_dec)

        log.info(
            "UPDATE_SL %s #%s new_stop=%s dry_run=%s",
            order_name, trade_id, new_stop_px, self.dry_run,
        )

        if self.dry_run:
            return {"mock": True, "status": "ok", "new_stop": new_stop_px}

        if self._exchange is None:
            raise HyperliquidError("SDK not initialized")

        if size is None:
            size = await self._current_position_size(order_name)
        if size is None or size <= 0:
            raise HyperliquidValidationError(f"no open size for SL update ({order_name})")

        sl_cloid = self._sl_cloids.get(trade_id) or sl_cloid_for(trade_id)

        # Step 1: cancel existing SL(s) for this trade by cloid match
        canceled = await asyncio.to_thread(
            self._cancel_sls_for_trade, order_name, trade_id, dex,
        )

        # Step 2: place a fresh SL trigger with the same cloid and new price.
        # Note: for triggers, limit_px is a worst-case protective price.
        # For a long: SL sells below the market, so limit < trigger;
        # for a short: SL buys above, so limit > trigger.
        protective_mul = 0.9 if is_buy_original else 1.1
        sl_limit_px = round_px(new_stop_px * protective_mul, sz_dec)

        exchange = self._exchange

        def _place():
            try:
                return exchange.order(
                    order_name,
                    not is_buy_original,   # SL sells a long / buys a short
                    size,
                    sl_limit_px,
                    {"trigger": {
                        "triggerPx": new_stop_px,
                        "isMarket": True,
                        "tpsl": "sl",
                    }},
                    reduce_only=True,
                    cloid=sl_cloid,
                )
            except TypeError:
                # Older SDK — fall back to bulk_orders([...], grouping="na")
                return exchange.bulk_orders(
                    [{
                        "coin": order_name,
                        "is_buy": not is_buy_original,
                        "sz": size,
                        "limit_px": sl_limit_px,
                        "order_type": {"trigger": {
                            "triggerPx": new_stop_px,
                            "isMarket": True,
                            "tpsl": "sl",
                        }},
                        "reduce_only": True,
                        "cloid": sl_cloid,
                    }],
                    grouping="na",
                )

        resp = await self._with_retry(_place, desc=f"replace SL #{trade_id}")
        self._sl_cloids[trade_id] = sl_cloid
        return {
            "canceled_count": canceled,
            "new_stop_px": new_stop_px,
            "sl_limit_px": sl_limit_px,
            "raw": resp if isinstance(resp, dict) else {"raw": resp},
        }

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    async def _current_position_size(self, order_name: str) -> Optional[float]:
        """Return abs size of user's open position for `order_name`.

        HL's user_state reports `coin` using the same namespace as order_name:
        "BTC" on the default dex, "xyz:SILVER" on HIP-3. So callers should
        pass the asset's resolved order_name, not the bare hl_coin symbol.
        For HIP-3 dexs, user_state is also scoped per dex — we iterate.
        """
        if self._info is None or not self.main_address:
            return None

        # Determine which dex this order lives on, from cached index
        dex_for = ""
        for entry in self._asset_index.values():
            if entry.get("order_name") == order_name:
                dex_for = entry.get("dex", "")
                break

        def _call():
            try:
                if dex_for:
                    return self._info.user_state(self.main_address, dex=dex_for)
                return self._info.user_state(self.main_address)
            except TypeError:
                return self._info.user_state(self.main_address)
            except Exception:
                return None

        state = await asyncio.to_thread(_call)
        if not isinstance(state, dict):
            return None
        positions = state.get("assetPositions") or []
        for p in positions:
            pos = p.get("position") or {}
            if pos.get("coin") == order_name:
                sz = pos.get("szi") or pos.get("sz")
                try:
                    return abs(float(sz)) if sz is not None else None
                except (ValueError, TypeError):
                    return None
        return None

    async def get_available_margin(self) -> Optional[float]:
        """Return USDC available as initial margin for new perp positions.

        On HyperCore unified accounts (cross or isolated), `withdrawable`
        from clearinghouseState is the right field — it's the amount you
        can deploy as initial margin without disrupting existing positions
        or exceeding maintenance buffer.

        Returns None on query failure so callers can distinguish "really 0"
        from "couldn't tell" — same defensive contract as open_positions().
        """
        if self.dry_run or self._info is None or not self.main_address:
            return None
        def _call():
            return self._info.user_state(self.main_address)
        try:
            state = await asyncio.to_thread(_call)
        except Exception as exc:
            log.warning("get_available_margin: user_state failed: %s", exc)
            return None
        if not isinstance(state, dict):
            return None
        try:
            return float(state.get("withdrawable") or 0)
        except (ValueError, TypeError):
            return None

    async def open_positions(self) -> Optional[list[dict]]:
        """Return the user's current open positions (for startup sync).

        Aggregates across default perps dex and all active HIP-3 dexes so
        HIP-3 positions (xyz:SILVER, cash:*, flx:*) are visible to the
        startup reconciliation.

        Returns:
          list[dict]  — successful query, may be empty if user has no positions
          None        — query FAILED (network blip, HL API error). Reconcile
                        callers MUST distinguish this from an empty list and
                        skip cleanup, otherwise transient failures will wipe
                        the bot's DB state for live positions.

        This was the May 1 outage root cause: the previous version swallowed
        exceptions and returned [], indistinguishable from "no positions".
        Reconcile then "cleaned" 4 live positions out of the DB on a single
        flaky API call.
        """
        if self.dry_run or self._info is None or not self.main_address:
            return []

        def _call_state(dex: str):
            try:
                if dex:
                    return self._info.user_state(self.main_address, dex=dex)
                return self._info.user_state(self.main_address)
            except TypeError:
                # SDK signature mismatch — fall back to no-dex form
                return self._info.user_state(self.main_address)
            # Other exceptions intentionally propagate so the caller can detect
            # a failed query rather than treat it as "no positions".

        out: list[dict] = []
        seen_coins: set[str] = set()
        any_dex_succeeded = False
        for dex in self._active_dexs or ("",):
            try:
                state = await asyncio.to_thread(_call_state, dex)
            except Exception as exc:
                log.warning(
                    "open_positions: user_state(dex=%r) FAILED: %s — "
                    "treating overall query as failed for safety",
                    dex, exc,
                )
                # If ANY dex query fails, fall through to None so the caller
                # doesn't wipe DB based on a partial view. Conservative.
                return None
            any_dex_succeeded = True
            if not isinstance(state, dict):
                continue
            for p in (state.get("assetPositions") or []):
                pos = p.get("position") or {}
                try:
                    sz = float(pos.get("szi") or 0)
                except (ValueError, TypeError):
                    sz = 0.0
                if sz == 0:
                    continue
                coin = pos.get("coin")
                if coin in seen_coins:
                    continue
                seen_coins.add(coin)
                out.append({
                    "coin": coin,
                    "size": abs(sz),
                    "side": "long" if sz > 0 else "short",
                    "entry_price": float(pos.get("entryPx") or 0) or None,
                    "unrealized_pnl": float(pos.get("unrealizedPnl") or 0) or None,
                    "leverage": (pos.get("leverage") or {}).get("value"),
                    "raw": pos,
                })
        if not any_dex_succeeded:
            return None
        return out

    # ------------------------------------------------------------------
    # Fill reconciliation (real fee + pnl from HL)
    # ------------------------------------------------------------------

    async def _reconcile_fills(
        self, hl_coin: str, since_ms: int, window_ms: int = 15_000,
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        if self._info is None or not self.main_address:
            return None, None, None

        # Let fills settle on HL, then query
        await asyncio.sleep(1.0)
        end_ms = int(time.time() * 1000) + window_ms

        def _call():
            try:
                return self._info.user_fills_by_time(
                    self.main_address, since_ms - 2_000, end_ms,
                )
            except TypeError:
                try:
                    return self._info.user_fills_by_time(
                        self.main_address, since_ms - 2_000,
                    )
                except Exception:
                    return []
            except Exception:
                return []

        fills = await asyncio.to_thread(_call)
        if not isinstance(fills, list):
            return None, None, None

        total_fee = 0.0
        total_pnl = 0.0
        total_sz = 0.0
        px_sum = 0.0
        n = 0

        for f in fills:
            if not isinstance(f, dict):
                continue
            if f.get("coin") != hl_coin:
                continue
            try:
                sz = abs(float(f.get("sz") or 0))
                px = float(f.get("px") or 0)
                fee = float(f.get("fee") or 0)
                cpnl = float(f.get("closedPnl") or 0)
            except (ValueError, TypeError):
                continue
            total_fee += fee
            total_pnl += cpnl
            total_sz += sz
            px_sum += px * sz
            n += 1

        if n == 0:
            return None, None, None

        avg_px = (px_sum / total_sz) if total_sz > 0 else None
        return total_fee, total_pnl, avg_px


# ============================================================
# SECTION: Module-level convenience singleton (optional use)
# ============================================================

_default_client: Optional[HyperliquidClient] = None


def get_default_client() -> HyperliquidClient:
    global _default_client
    if _default_client is None:
        _default_client = HyperliquidClient()
    return _default_client
