"""
Dashboard authentication helpers. Pure (no nicegui) so they're unit-testable.

The dashboard exposes live-money controls (Auto Mode toggle, Cancel buttons,
position sizing) on a public Railway URL — without a password anyone who
finds the link can drive the bot. Set DASHBOARD_PASSWORD to require login.

Behavior:
- DASHBOARD_PASSWORD set   → /login gate on every page (session via
  app.storage.user, signed with DASHBOARD_STORAGE_SECRET).
- DASHBOARD_PASSWORD unset → dashboard stays open (no deploy lockout) but the
  bot logs a loud warning and the header shows a red "public" banner.

Sessions: if DASHBOARD_STORAGE_SECRET is unset, a random per-boot secret is
used — every redeploy then invalidates sessions (you just log in again).
Set the env var to keep sessions across deploys.
"""
from __future__ import annotations

import hmac
import os


def configured_password() -> str:
    """The dashboard password from env ('' = auth disabled)."""
    return os.environ.get("DASHBOARD_PASSWORD", "").strip()


def auth_enabled() -> bool:
    return bool(configured_password())


def password_ok(supplied: object) -> bool:
    """Constant-time check of a supplied password against the configured one.

    With auth disabled (no configured password) everything passes — the
    caller-side gate is also off in that case, so this is just consistent.
    """
    cfg = configured_password()
    if not cfg:
        return True
    return hmac.compare_digest(str(supplied or ""), cfg)
