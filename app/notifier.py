"""
Webhook notifications for the Corgi Calls Copy Trading Bot.

Fire-and-forget notifications to Discord or Telegram.

- Set NOTIFY_WEBHOOK_URL in the env to enable.
- If the env var is empty/unset, every notify_* call is a silent no-op.
- All send paths swallow exceptions — this module MUST NOT crash the bot.
- Async httpx POST, scheduled via asyncio.create_task() so callers don't block.

URL shape auto-detection:
- Discord:  contains 'discord.com/api/webhooks' or 'discordapp.com'
            → POST body:  {"content": "..."}
- Telegram: contains 'api.telegram.org/bot'
            → POST body:  {"text": "...", "parse_mode": "HTML"}
- Anything else is treated as Discord-compatible by default.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger(__name__)


# ============================================================
# SECTION: Configuration
# ============================================================

_WEBHOOK_ENV = "NOTIFY_WEBHOOK_URL"
_TIMEOUT_SECONDS = 5.0


def _detect_provider(url: str) -> str:
    u = url.lower()
    if "discord.com/api/webhooks" in u or "discordapp.com" in u:
        return "discord"
    if "api.telegram.org/bot" in u:
        return "telegram"
    return "discord"  # safe default — same shape as most webhooks


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_price(p) -> str:
    if p is None:
        return "—"
    try:
        p = float(p)
    except (ValueError, TypeError):
        return str(p)
    if abs(p) >= 1000:
        return f"{p:,.2f}"
    if abs(p) >= 1:
        return f"{p:.4f}"
    return f"{p:.8f}".rstrip("0").rstrip(".")


def _fmt_pnl(pnl) -> str:
    if pnl is None:
        return "—"
    try:
        pnl = float(pnl)
    except (ValueError, TypeError):
        return str(pnl)
    sign = "+" if pnl >= 0 else "-"
    return f"{sign}${abs(pnl):,.2f}"


def _side_arrow(side: Optional[str]) -> str:
    s = (side or "").lower()
    if s in ("long", "buy"):
        return "📈 LONG"
    if s in ("short", "sell"):
        return "📉 SHORT"
    return (side or "").upper() or "—"


# ============================================================
# SECTION: Notifier
# ============================================================

class Notifier:
    """Webhook notifier. Safe to instantiate even when disabled."""

    def __init__(self, webhook_url: Optional[str] = None, timeout: float = _TIMEOUT_SECONDS):
        self.webhook_url = (webhook_url if webhook_url is not None
                            else os.environ.get(_WEBHOOK_ENV, "")).strip()
        self.provider = _detect_provider(self.webhook_url) if self.webhook_url else None
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    # --- low-level send ------------------------------------------------

    async def _send_raw(self, message: str) -> None:
        if not self.enabled:
            return
        try:
            if self.provider == "telegram":
                payload = {"text": message, "parse_mode": "HTML"}
            else:  # discord (default)
                payload = {"content": message}

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.webhook_url, json=payload)
                if resp.status_code >= 400:
                    log.warning(
                        "notifier: webhook returned %s: %s",
                        resp.status_code,
                        (resp.text or "")[:200],
                    )
        except Exception as exc:
            # Fire-and-forget: never crash the bot over a broken webhook.
            log.warning("notifier: send failed: %s", exc)

    def _dispatch(self, message: str) -> None:
        """Schedule a send on the running loop; swallow everything."""
        if not self.enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop — run to completion synchronously but still swallow errors.
            try:
                asyncio.run(self._send_raw(message))
            except Exception as exc:
                log.warning("notifier: sync dispatch failed: %s", exc)
            return
        try:
            loop.create_task(self._send_raw(message))
        except Exception as exc:
            log.warning("notifier: schedule failed: %s", exc)

    # --- public fire-and-forget triggers -------------------------------

    def notify_opened(
        self,
        *,
        coin: str,
        side: str,
        entry: Optional[float],
        leverage: Optional[float] = None,
        size: Optional[float] = None,
        caller: Optional[str] = None,
        trade_id: Optional[int] = None,
        dry_run: bool = False,
    ) -> None:
        """Position opened on HL."""
        if not self.enabled:
            return
        header = "🟢 POSITION OPENED" + ("  [DRY RUN]" if dry_run else "")
        lines = [
            header,
            f"• {coin}  {_side_arrow(side)}",
            f"• Entry: {_fmt_price(entry)}",
        ]
        if leverage is not None:
            lines.append(f"• Leverage: {leverage:g}x")
        if size is not None:
            lines.append(f"• Size: {size:g}")
        if caller:
            lines.append(f"• Caller: {caller}")
        if trade_id is not None:
            lines.append(f"• Trade: #{trade_id}")
        lines.append(f"• {_utcnow()}")
        self._dispatch("\n".join(lines))

    def notify_closed(
        self,
        *,
        coin: str,
        side: str,
        entry: Optional[float],
        exit: Optional[float],
        pnl: Optional[float],
        fee: Optional[float] = None,
        close_type: Optional[str] = None,
        trade_id: Optional[int] = None,
        dry_run: bool = False,
    ) -> None:
        """Position closed on HL, with realized PnL."""
        if not self.enabled:
            return
        try:
            pnl_num = float(pnl) if pnl is not None else None
        except (ValueError, TypeError):
            pnl_num = None
        icon = "⚪"
        if pnl_num is not None:
            icon = "🟢" if pnl_num > 0 else ("🔴" if pnl_num < 0 else "⚪")
        header = f"{icon} POSITION CLOSED" + ("  [DRY RUN]" if dry_run else "")
        lines = [
            header,
            f"• {coin}  {_side_arrow(side)}",
            f"• Entry: {_fmt_price(entry)}",
            f"• Exit:  {_fmt_price(exit)}",
            f"• PnL:   {_fmt_pnl(pnl)}",
        ]
        if fee is not None:
            lines.append(f"• Fee:   ${float(fee):.2f}")
        if close_type:
            lines.append(f"• Type:  {close_type}")
        if trade_id is not None:
            lines.append(f"• Trade: #{trade_id}")
        lines.append(f"• {_utcnow()}")
        self._dispatch("\n".join(lines))

    def notify_sl_triggered(
        self,
        *,
        coin: str,
        side: str,
        entry: Optional[float],
        stop: Optional[float],
        pnl: Optional[float] = None,
        trade_id: Optional[int] = None,
        dry_run: bool = False,
    ) -> None:
        """Stop-loss triggered on HL."""
        if not self.enabled:
            return
        header = "🛑 STOP LOSS TRIGGERED" + ("  [DRY RUN]" if dry_run else "")
        lines = [
            header,
            f"• {coin}  {_side_arrow(side)}",
            f"• Entry: {_fmt_price(entry)}",
            f"• Stop:  {_fmt_price(stop)}",
        ]
        if pnl is not None:
            lines.append(f"• PnL:   {_fmt_pnl(pnl)}")
        if trade_id is not None:
            lines.append(f"• Trade: #{trade_id}")
        lines.append(f"• {_utcnow()}")
        self._dispatch("\n".join(lines))

    def notify_tp_hit(
        self,
        *,
        coin: str,
        side: str,
        tp_price: Optional[float],
        size_pct: Optional[float],
        tp_num: Optional[int] = None,
        trade_id: Optional[int] = None,
        dry_run: bool = False,
    ) -> None:
        """Take-profit level hit (partial close)."""
        if not self.enabled:
            return
        header = "🎯 TAKE PROFIT HIT" + ("  [DRY RUN]" if dry_run else "")
        lines = [
            header,
            f"• {coin}  {_side_arrow(side)}",
        ]
        if tp_num is not None:
            lines.append(f"• Level: TP{tp_num}")
        lines.append(f"• Price: {_fmt_price(tp_price)}")
        if size_pct is not None:
            lines.append(f"• Closed: {float(size_pct):g}%")
        if trade_id is not None:
            lines.append(f"• Trade: #{trade_id}")
        lines.append(f"• {_utcnow()}")
        self._dispatch("\n".join(lines))

    def notify_sl_moved(
        self,
        *,
        coin: str,
        side: str,
        old_stop: Optional[float],
        new_stop: float,
        reason: Optional[str] = None,
        trade_id: Optional[int] = None,
        dry_run: bool = False,
    ) -> None:
        """Stop-loss PRICE was changed on HL (not triggered).

        Fires for both portal-driven `stop_update` events and the automatic
        post-TP trailing logic. `reason` distinguishes them in the message
        (e.g. "TP2 hit → BE", "portal update").
        """
        if not self.enabled:
            return
        header = "🔒 STOP LOSS MOVED" + ("  [DRY RUN]" if dry_run else "")
        lines = [
            header,
            f"• {coin}  {_side_arrow(side)}",
        ]
        if old_stop is not None:
            lines.append(f"• From: {_fmt_price(old_stop)}")
        lines.append(f"• To:   {_fmt_price(new_stop)}")
        if reason:
            lines.append(f"• Why:  {reason}")
        if trade_id is not None:
            lines.append(f"• Trade: #{trade_id}")
        lines.append(f"• {_utcnow()}")
        self._dispatch("\n".join(lines))

    def notify_skipped(
        self,
        *,
        coin: str,
        reason: str,
        side: Optional[str] = None,
        detail: Optional[str] = None,
        trade_id: Optional[int] = None,
        caller: Optional[str] = None,
    ) -> None:
        """A new-trade signal was NOT entered.

        `reason` is one of:
          - "stale"               — posted before bot startup cutoff
          - "blocked_coin_live"   — same coin already has an open position
          - "insufficient_margin" — pre-flight withdrawable < required
        Other strings are accepted as-is for future cases.
        """
        if not self.enabled:
            return
        icon = {
            "stale": "🕰️",
            "blocked_coin_live": "🚫",
            "insufficient_margin": "💸",
        }.get(reason, "⚠️")
        header = f"{icon} TRADE SKIPPED"
        lines = [header]
        if side:
            lines.append(f"• {coin}  {_side_arrow(side)}")
        else:
            lines.append(f"• {coin}")
        lines.append(f"• Reason: {reason.replace('_', ' ')}")
        if detail:
            lines.append(f"• Detail: {detail}")
        if caller:
            lines.append(f"• Caller: {caller}")
        if trade_id is not None:
            lines.append(f"• Trade: #{trade_id}")
        lines.append(f"• {_utcnow()}")
        self._dispatch("\n".join(lines))

    def notify_heartbeat(
        self,
        *,
        uptime_seconds: float = 0,
        seconds_since_last_poll: Optional[float] = None,
        open_positions: Optional[int] = None,
        dry_run: bool = False,
        auto_mode: bool = False,
    ) -> None:
        """Periodic 'still alive' ping.

        Sent every HEARTBEAT_INTERVAL_SECONDS (default 600s = 10 min) by
        main.heartbeat_loop. If you stop seeing these in your channel, the
        bot is dead.

        The poll-age field also surfaces "alive but stuck" failures:
        a green heartbeat with `poll=11h ago` is the Apr-28 silent-death
        signature.
        """
        if not self.enabled:
            return
        # Compose a compact one-line ping. Don't be chatty — these arrive
        # every 10 minutes; long messages get filtered out by users.
        def _fmt_age(s):
            if s is None:
                return "n/a"
            if s < 60:
                return f"{s:.0f}s"
            if s < 3600:
                return f"{s/60:.0f}m"
            return f"{s/3600:.1f}h"

        flags = []
        if dry_run:
            flags.append("DRY_RUN")
        if auto_mode:
            flags.append("AUTO")
        flag_str = f" [{','.join(flags)}]" if flags else ""

        msg = (
            f"✅ Bot alive{flag_str}  "
            f"uptime={_fmt_age(uptime_seconds)}  "
            f"poll={_fmt_age(seconds_since_last_poll)} ago  "
            f"open={open_positions if open_positions is not None else '?'}  "
            f"{_utcnow()}"
        )
        self._dispatch(msg)


# ============================================================
# SECTION: Module-level default instance + convenience wrappers
# ============================================================

_default = Notifier()


def is_enabled() -> bool:
    return _default.enabled


def notify_opened(**kwargs) -> None:
    _default.notify_opened(**kwargs)


def notify_closed(**kwargs) -> None:
    _default.notify_closed(**kwargs)


def notify_sl_triggered(**kwargs) -> None:
    _default.notify_sl_triggered(**kwargs)


def notify_tp_hit(**kwargs) -> None:
    _default.notify_tp_hit(**kwargs)


def notify_heartbeat(**kwargs) -> None:
    _default.notify_heartbeat(**kwargs)


def notify_sl_moved(**kwargs) -> None:
    _default.notify_sl_moved(**kwargs)


def notify_skipped(**kwargs) -> None:
    _default.notify_skipped(**kwargs)


def reload_from_env() -> None:
    """Re-read NOTIFY_WEBHOOK_URL from env (e.g. after .env reload)."""
    global _default
    _default = Notifier()
