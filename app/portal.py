"""
Portal API client for the Corgi Calls Copy Trading Bot.

Session-cookie auth via POST /api/auth/login with email + password.
Cookies are persisted to SQLite (db.portal_cookies) and reloaded on start.
On 401 the client re-logins and retries the failed request once.

Event parsing produces typed dicts matching main.py's router:
    new_trade | stop_update | tp_hit | full_close

Caller whitelist is enforced; non-whitelisted trades are silently dropped
(logged once per trade_id).

Public interface (unchanged — main.py keeps working):
    PortalClient(email=..., password=..., base_url=..., ...)
    async start() / close()
    async login()
    has_session() -> bool
    async poll(stop_event=None) -> AsyncIterator[dict]
    async fetch_new_events() -> list[dict]
    PortalAuthError
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, AsyncIterator, Iterable, Optional
from urllib.parse import urlparse

import httpx

from app import db

log = logging.getLogger(__name__)


# ============================================================
# SECTION: Configuration
# ============================================================

DEFAULT_BASE_URL = "https://portal.corgicalls.com"
DEFAULT_POLL_INTERVAL = 3.0
DEFAULT_CALLERS = "voberoi,pranayyyy,corgil_"


def _env_callers() -> set[str]:
    raw = os.environ.get("ALLOWED_CALLERS", DEFAULT_CALLERS)
    return {c.strip() for c in raw.split(",") if c.strip()}


# ============================================================
# SECTION: Exceptions
# ============================================================

class PortalError(RuntimeError):
    """Base class for portal-client errors."""


class PortalAuthError(PortalError):
    """Raised when login cannot proceed or is rejected."""


# ============================================================
# SECTION: Helpers
# ============================================================

def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None


def _unwrap_list(data: Any, keys: Iterable[str]) -> list[Any]:
    """Accept list, or {key: [...]} for any of `keys`, else []."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


# ============================================================
# SECTION: PortalClient
# ============================================================

class PortalClient:
    """Async Corgi Portal client with email/password session auth."""

    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        base_url: Optional[str] = None,
        poll_interval: Optional[float] = None,
        allowed_callers: Optional[set[str]] = None,
        timeout: float = 30.0,
        # accepted for forward-compat but not used here:
        api_key: Optional[str] = None,
        heartbeat_interval: Optional[float] = None,
        full_refresh_interval: Optional[float] = None,
    ) -> None:
        self.email = (
            email
            or os.environ.get("PORTAL_USER")
            or os.environ.get("PORTAL_EMAIL")
        )
        self.password = password or os.environ.get("PORTAL_PASSWORD")

        self.base_url = (
            base_url
            or os.environ.get("PORTAL_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")

        env_interval = os.environ.get("PORTAL_POLL_INTERVAL")
        if poll_interval is not None:
            self.poll_interval = float(poll_interval)
        elif env_interval is not None:
            try:
                self.poll_interval = float(env_interval)
            except ValueError:
                self.poll_interval = DEFAULT_POLL_INTERVAL
        else:
            self.poll_interval = DEFAULT_POLL_INTERVAL

        self.allowed_callers = (
            allowed_callers if allowed_callers is not None else _env_callers()
        )
        self.timeout = timeout

        self._client: Optional[httpx.AsyncClient] = None
        self._login_lock = asyncio.Lock()

        # Deduplication & once-per-id logging
        self._ignored_loggged: set[Any] = set()
        self._seen_event_ids: set[Any] = set()
        self._seen_cap = 5000

        # Liveness tracking: bumped to wall-clock ms on every successful
        # activity-feed fetch. Used by main.heartbeat_loop to detect a poll
        # task that's "alive but stuck" (the Apr 28 silent-death pattern).
        self.last_successful_poll_ms: int = 0

        parsed = urlparse(self.base_url)
        self._cookie_domain = parsed.netloc.split(":")[0] or ""

    # ------------------------------------------------------------------
    # Context-manager lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "PortalClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def start(self) -> None:
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout, connect=10.0),
            follow_redirects=True,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "corgi-copy-bot/1.0",
            },
        )
        self._load_cookies()
        log.info(
            "portal client started (base=%s, poll=%.1fs, callers=%s)",
            self.base_url, self.poll_interval, sorted(self.allowed_callers),
        )

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            self._save_cookies()
        except Exception:
            log.exception("failed to persist cookies on close")
        try:
            await self._client.aclose()
        finally:
            self._client = None

    # ------------------------------------------------------------------
    # Cookie persistence (db.portal_cookies; value holds JSON payload)
    # ------------------------------------------------------------------

    def _save_cookies(self) -> None:
        if self._client is None:
            return
        payload: dict[str, str] = {}
        for cookie in self._client.cookies.jar:
            payload[cookie.name] = json.dumps(
                {
                    "value":   cookie.value,
                    "domain":  cookie.domain or self._cookie_domain,
                    "path":    cookie.path or "/",
                    "secure":  bool(cookie.secure),
                    "expires": cookie.expires,
                },
                separators=(",", ":"),
            )
        db.clear_portal_cookies()
        if payload:
            db.set_portal_cookies(payload)

    def _load_cookies(self) -> None:
        if self._client is None:
            return
        stored = db.get_portal_cookies()
        loaded = 0
        for name, raw in stored.items():
            try:
                data = json.loads(raw)
                value = data["value"]
                domain = data.get("domain") or self._cookie_domain
                path = data.get("path") or "/"
            except (ValueError, TypeError, KeyError):
                value = raw
                domain = self._cookie_domain
                path = "/"
            try:
                self._client.cookies.set(name=name, value=value, domain=domain, path=path)
                loaded += 1
            except Exception:
                log.warning("failed to load cookie %r", name, exc_info=True)
        if loaded:
            log.info("loaded %d persisted cookie(s)", loaded)

    def has_session(self) -> bool:
        if self._client is None:
            return False
        return any(True for _ in self._client.cookies.jar)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def login(self) -> None:
        if self._client is None:
            await self.start()
        if not self.email or not self.password:
            raise PortalAuthError(
                "PORTAL_USER and PORTAL_PASSWORD must be set (env or constructor)"
            )
        async with self._login_lock:
            assert self._client is not None
            log.info("logging in to portal as %s", self.email)
            try:
                resp = await self._client.post(
                    "/api/portal/login",
                    json={"username": self.email, "password": self.password},
                )
            except httpx.HTTPError as exc:
                raise PortalAuthError(f"login request failed: {exc}") from exc

            if resp.status_code in (401, 403):
                raise PortalAuthError(
                    f"login rejected ({resp.status_code}): "
                    f"{(resp.text or '')[:200]}"
                )
            if resp.status_code >= 400:
                raise PortalAuthError(
                    f"login failed ({resp.status_code}): "
                    f"{(resp.text or '')[:200]}"
                )
            self._save_cookies()
            log.info("portal login successful")

    # ------------------------------------------------------------------
    # Request layer — 401 → re-login → retry once
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        auth_retry: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        if self._client is None:
            await self.start()
        assert self._client is not None

        resp = await self._client.request(method, url, **kwargs)
        if resp.status_code == 401 and auth_retry:
            log.info("got 401 on %s %s — re-logging in and retrying", method, url)
            await self.login()
            resp = await self._client.request(method, url, **kwargs)
        return resp

    # ------------------------------------------------------------------
    # Endpoint wrappers
    # ------------------------------------------------------------------

    async def get_activity_feed(self) -> list[dict]:
        resp = await self._request("GET", "/api/portal/me/activity-feed")
        resp.raise_for_status()
        try:
            data = resp.json()
        except json.JSONDecodeError:
            log.warning("activity-feed response was not JSON")
            return []
        # DEBUG: dump first successful payload so we can inspect real field names
        try:
            dump_path = "/tmp/portal_raw.json"
            if not os.path.exists(dump_path):
                with open(dump_path, "w") as f:
                    json.dump(data, f, indent=2, default=str)
                log.info("dumped raw activity-feed payload → %s", dump_path)
        except Exception:
            log.debug("failed to dump raw payload", exc_info=True)
        return [
            e for e in _unwrap_list(data, ("events", "data", "items", "feed"))
            if isinstance(e, dict)
        ]

    async def get_trades(self) -> list[dict]:
        resp = await self._request("GET", "/api/portal/me/trades")
        resp.raise_for_status()
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return []
        return [
            t for t in _unwrap_list(data, ("trades", "data", "items"))
            if isinstance(t, dict)
        ]

    async def follow_trade(self, trade_id: int) -> dict:
        resp = await self._request(
            "POST", "/api/portal/me/trades", json={"tradeId": int(trade_id)},
        )
        # 201 Created = newly followed; 409 Conflict = already following.
        if resp.status_code == 409:
            return {}
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def get_trade_detail(self, trade_id: int) -> Optional[dict]:
        """Fetch the full trade object (with stop/tp/leverage) for a given trade_id.

        The Corgi portal has no per-trade GET endpoint. Instead, the POST-to-follow
        endpoint returns the full detail inline, AND follow-then-list returns the
        same detail in GET /me/trades. This method:

          1. POSTs /api/portal/me/trades {"tradeId": N} — returns {id, tradeId, ...,
             trade: {stop, tp, leverage, entryUsed, ...}} for new follows.
          2. If already followed (409) or body lacks `trade`, falls back to
             GET /api/portal/me/trades and picks the matching entry.

        Returns the inner `trade` dict on success, None on any failure.
        Distinguishes "this trade no longer exists / is closed" (404/400)
        from transport errors: the former is logged at DEBUG, the latter
        at WARNING, so logs aren't polluted by already-closed trades.
        """
        try:
            wrapper = await self.follow_trade(int(trade_id))
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (400, 404, 410, 422):
                # Trade is closed / invalid / no longer followable.
                log.debug("follow_trade(#%s): %s — treating as closed", trade_id, status)
                return None
            log.warning("follow_trade(#%s) failed: %s", trade_id, exc)
            wrapper = {}
        except Exception as exc:
            log.warning("follow_trade(#%s) failed: %s", trade_id, exc)
            wrapper = {}

        # Path 1: POST returned the inline detail
        if isinstance(wrapper, dict):
            inner = wrapper.get("trade")
            if isinstance(inner, dict):
                return inner

        # Path 2: fall back to /me/trades list
        try:
            trades = await self.get_trades()
        except Exception as exc:
            log.warning("get_trades() fallback failed for #%s: %s", trade_id, exc)
            return None

        for t in trades:
            if not isinstance(t, dict):
                continue
            inner = t.get("trade") if isinstance(t.get("trade"), dict) else t
            if _int(inner.get("id")) == int(trade_id) or _int(t.get("tradeId")) == int(trade_id):
                if isinstance(t.get("trade"), dict):
                    return t["trade"]
                return t

        return None

    async def close_trade(
        self,
        user_trade_id: int,
        user_exit_price: float,
        size_pct: float = 100.0,
    ) -> dict:
        resp = await self._request(
            "PATCH",
            f"/api/portal/me/trades/{int(user_trade_id)}",
            json={
                "userExitPrice": float(user_exit_price),
                "sizePct": float(size_pct),
            },
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    # ------------------------------------------------------------------
    # Event parsing
    # ------------------------------------------------------------------

    def _parse_event(self, raw: dict) -> Optional[dict]:
        """Normalize one activity-feed entry into a typed event dict.

        Returns one of {new_trade, stop_update, tp_hit, full_close} or None.
        """
        trade = raw.get("trade") if isinstance(raw.get("trade"), dict) else {}

        kind_raw = raw.get("type") or raw.get("eventType") or raw.get("action") or ""
        kind = str(kind_raw).lower()

        # Silently drop bet events — they use a different ID namespace (betId)
        # and have no trading-actionable shape.
        if kind in {"bet_opened", "bet_closed", "bet_updated"}:
            return None

        trade_id = _int(raw.get("tradeId") or raw.get("trade_id") or trade.get("id"))
        coin = raw.get("coin") or trade.get("coin") or trade.get("symbol")
        side = (raw.get("side") or trade.get("side") or "").lower() or None
        caller = (
            raw.get("userTag")
            or raw.get("caller")
            or trade.get("userTag")
            or trade.get("caller")
        )
        event_id = raw.get("id") or raw.get("eventId")
        at = raw.get("createdAt") or raw.get("at") or raw.get("timestamp")

        # Caller whitelist — log once per trade_id
        if caller and caller not in self.allowed_callers:
            dedup_key = trade_id if trade_id is not None else ("caller", caller, event_id)
            if dedup_key not in self._ignored_loggged:
                self._ignored_loggged.add(dedup_key)
                log.info(
                    "ignoring trade %s from non-whitelisted caller %r",
                    trade_id, caller,
                )
            return None

        common = {
            "event_id": event_id,
            "trade_id": trade_id,
            "coin": coin,
            "side": side,
            "caller": caller,
            "at": at,
            "raw": raw,
        }

        size_pct = _num(raw.get("sizePct") or raw.get("size_pct"))

        # --- new trade ---
        if kind in {
            "trade_opened", "new_trade", "open", "trade.open",
            "trade_open", "position_opened", "enter",
        } or (
            kind == "" and (
                raw.get("status") == "open" or trade.get("status") == "open"
            )
        ):
            # entryRaw is the real field name in the Corgi feed (string).
            entry_price = _num(
                raw.get("entryPrice")
                or raw.get("entryRaw")
                or trade.get("entryPrice")
                or trade.get("entryRaw")
            )
            return {
                **common,
                "type": "new_trade",
                "entry_price": entry_price,
                "stop_loss":   _num(raw.get("stopLoss") or trade.get("stopLoss")),
                "take_profits": (
                    raw.get("takeProfits") or trade.get("takeProfits") or []
                ),
                "leverage":    _num(raw.get("leverage") or trade.get("leverage")),
                "status":      raw.get("status") or trade.get("status"),
            }

        # --- trade_updated (Corgi's real update event) ---
        # Shape: {type: "trade_updated", updateType: "stop_moved"|..., updateText: "Stop moved to $75,000", ...}
        if kind == "trade_updated":
            update_type = str(raw.get("updateType") or "").lower()
            update_text = raw.get("updateText") or ""

            if update_type in {"stop_moved", "sl_moved", "stop_update", "sl_update"}:
                # Prefer explicit fields if present, else extract from text.
                new_stop = _num(
                    raw.get("newStopLoss")
                    or raw.get("newStop")
                    or raw.get("stopLoss")
                )
                if new_stop is None and isinstance(update_text, str):
                    import re
                    m = re.search(r"([-+]?\d[\d,]*\.?\d*)", update_text.replace(",", ""))
                    if m:
                        new_stop = _num(m.group(1))
                return {
                    **common,
                    "type": "stop_update",
                    "new_stop": new_stop,
                    "old_stop": _num(raw.get("oldStopLoss") or raw.get("oldStop")),
                }

            if update_type in {"tp_hit", "take_profit_hit", "partial_exit", "partial_close"}:
                return {
                    **common,
                    "type": "tp_hit",
                    "size_pct": size_pct if size_pct is not None else 25.0,
                    "tp_price": _num(
                        raw.get("tpPrice")
                        or raw.get("exitPrice")
                        or raw.get("fillPrice")
                    ),
                    "tp_num": _int(raw.get("tpNum") or raw.get("tpIndex")),
                }

            # Unknown updateType — drop silently
            return None

        # --- stop-loss update (legacy shape) ---
        if kind in {"stop_update", "sl_update", "stop_loss_update", "stoplossupdated"}:
            return {
                **common,
                "type": "stop_update",
                "new_stop": _num(
                    raw.get("stopLoss") or raw.get("newStop") or raw.get("newStopLoss")
                ),
                "old_stop": _num(raw.get("oldStop") or raw.get("oldStopLoss")),
            }

        # --- tp hit / partial close (legacy shape) ---
        if kind in {
            "tp_hit", "take_profit_hit", "partial_close", "partial_exit", "tp",
        } or (size_pct is not None and 0 < size_pct < 100):
            return {
                **common,
                "type": "tp_hit",
                "size_pct": size_pct,
                "tp_price": _num(
                    raw.get("exitPrice") or raw.get("tpPrice") or raw.get("fillPrice")
                ),
                "tp_num": _int(raw.get("tpNum") or raw.get("tpIndex")),
            }

        # --- full close / SL trigger / auto close ---
        full_close_kinds = {
            "full_close", "close", "trade_closed", "position_closed",
            "stop_triggered", "sl_triggered", "auto_close", "stale_close",
            "cancel", "cancelled", "canceled",
        }
        if kind in full_close_kinds or (size_pct is not None and size_pct >= 100):
            close_reason = raw.get("closeReason") or raw.get("reason") or kind or None
            reason_str = str(close_reason or "").lower()
            stop_triggered = (
                kind in {"stop_triggered", "sl_triggered"}
                or reason_str in {"stop", "sl", "stop_loss", "stopped_out"}
            )
            return {
                **common,
                "type": "full_close",
                "exit_price": _num(
                    raw.get("closePrice") or raw.get("exitPrice") or raw.get("fillPrice")
                ),
                "stop_triggered": stop_triggered,
                "close_reason": close_reason,
                "pnl_pct": _num(raw.get("pnlPct")),
            }

        return None

    # ------------------------------------------------------------------
    # Dedup
    # ------------------------------------------------------------------

    def _normalize_event_id(self, event_id: Any) -> Any:
        """Normalize event_id to int or str for set membership."""
        if event_id is None:
            return None
        try:
            return int(event_id)
        except (TypeError, ValueError):
            return str(event_id)

    def _is_seen(self, event_id: Any) -> bool:
        """Check if we've already processed this event_id (read-only check)."""
        if event_id is None:
            return False
        key = self._normalize_event_id(event_id)
        return key in self._seen_event_ids

    def _mark_as_seen(self, event_id: Any) -> None:
        """Mark this event_id as seen (called AFTER successful processing)."""
        if event_id is None:
            return
        key = self._normalize_event_id(event_id)
        self._seen_event_ids.add(key)
        if len(self._seen_event_ids) > self._seen_cap:
            keep_n = self._seen_cap // 2
            self._seen_event_ids = set(list(self._seen_event_ids)[-keep_n:])

    def _mark_seen(self, event_id: Any) -> bool:
        """DEPRECATED: use _is_seen + _mark_as_seen instead.

        True if fresh, False if we've already processed this event_id.
        This version both checks AND marks in one call, which causes silent
        data loss if processing fails after the mark. Kept for compatibility.
        """
        if event_id is None:
            return True
        key = self._normalize_event_id(event_id)
        if key in self._seen_event_ids:
            return False
        self._seen_event_ids.add(key)
        if len(self._seen_event_ids) > self._seen_cap:
            keep_n = self._seen_cap // 2
            self._seen_event_ids = set(list(self._seen_event_ids)[-keep_n:])
        return True

    # ------------------------------------------------------------------
    # Fetch + parse + dedup
    # ------------------------------------------------------------------

    async def fetch_new_events(self) -> list[dict]:
        raw_events = await self.get_activity_feed()
        # The portal returns events newest-first. That's a footgun on restart:
        # if both open+close for the same trade_id are in the backlog, the
        # close fires first, handle_full_close sees nothing in hl_live_trades,
        # silently no-ops, the event is marked seen, and the open then opens
        # a real position that will never be auto-closed.
        # Process in chronological order (oldest first) so open → *_update →
        # close events arrive in causal order.
        def _ts(e):
            return _int(e.get("timestamp") or e.get("createdAt")) or 0
        raw_events = sorted(raw_events, key=_ts)

        # Liveness signal — the heartbeat task reads this to detect "alive
        # but stuck" failures. Bumped after a SUCCESSFUL feed fetch only,
        # before any per-event processing.
        self.last_successful_poll_ms = int(time.time() * 1000)

        out: list[dict] = []
        for raw in raw_events:
            event_id = raw.get("id") or raw.get("eventId")
            # Check if already seen (read-only check)
            if self._is_seen(event_id):
                continue
            # Parse the event
            parsed = self._parse_event(raw)
            if parsed is not None:
                out.append(parsed)
                # Mark as seen AFTER successful parse and append
                self._mark_as_seen(event_id)
        return out

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def poll(self, stop_event: Optional[asyncio.Event] = None) -> AsyncIterator[dict]:
        """Yield parsed events forever; ~3s poll, exp backoff on errors.

        SILENT-EXIT GUARD (Apr 28 outage):
        On Apr 28 this generator exited without raising and without stop_event
        being set — bot silently stopped polling for 11 hours. To make sure a
        repeat of that pattern is loud:

          - All known control paths now go through explicit `return` (when
            stop_event is set) or `raise` (PortalAuthError, or unhandled
            exceptions which propagate).
          - A `finally:` block flags ANY non-stop_event exit at ERROR level,
            so the supervisor (in main.py) sees evidence in logs.
          - After the while loop, an unconditional RuntimeError raises if
            execution somehow falls through. Statically unreachable, but
            this is a defensive last-line guard.

        The supervisor in main.py treats any exit as a failure and respawns,
        so even if a future bug reintroduces a silent-return path, the bot
        won't stay dead.
        """
        backoff = self.poll_interval
        max_backoff = max(self.poll_interval * 10, 30.0)
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    log.info("portal.poll: stop_event set — clean exit")
                    return
                try:
                    events = await self.fetch_new_events()
                    backoff = self.poll_interval
                    for evt in events:
                        yield evt
                except PortalAuthError:
                    log.exception("portal auth error — stopping poll")
                    raise
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code if exc.response is not None else "?"
                    if status == 429:
                        log.warning("portal 429 — backing off %.1fs", backoff)
                    else:
                        log.warning("portal HTTP %s on poll: %s", status, exc)
                    backoff = min(backoff * 2, max_backoff)
                except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                    log.warning("portal transport error: %s", exc)
                    backoff = min(backoff * 2, max_backoff)
                except Exception:
                    log.exception("unexpected error in portal poll loop")
                    backoff = min(backoff * 2, max_backoff)

                if stop_event is not None:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                        log.info("portal.poll: stop_event awaited — clean exit")
                        return
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(backoff)
        finally:
            # Distinguish three paths through this finally:
            # 1. CancelledError / GeneratorExit in flight → clean shutdown
            #    (e.g. SIGTERM via wrapper, NiceGUI on_shutdown). Don't alarm.
            # 2. Other exception in flight → already logged where it happened;
            #    let supervisor handle.
            # 3. No exception, stop_event NOT set → the silent-death bug.
            #    Log loudly so it shows up in monitoring.
            import sys as _sys
            in_flight = _sys.exc_info()[1]
            if isinstance(in_flight, (asyncio.CancelledError, GeneratorExit)):
                log.info("portal.poll: cancelled — clean shutdown")
            elif in_flight is None and (stop_event is None or not stop_event.is_set()):
                log.error(
                    "portal.poll: generator exiting WITHOUT stop_event set "
                    "and WITHOUT exception — silent-death pattern. "
                    "Supervisor should respawn."
                )

        # Unreachable in normal control flow (the while True has no break).
        # If we ever do reach here, raise so the supervisor sees a real failure.
        raise RuntimeError(
            "portal.poll exited the main loop unexpectedly — silent-death guard"
        )
