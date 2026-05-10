"""
Database layer for the Corgi Calls copy trading bot.

SQLite with WAL mode + foreign keys ON.
Thread-safe via threading.local (one sqlite3.Connection per thread).
All timestamps are stored as ISO-8601 UTC strings.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

# ============================================================
# SECTION: Paths / configuration
# ============================================================

_DEFAULT_DB_PATH = Path(
    os.environ.get(
        "CORGI_DB_PATH",
        str(Path(__file__).resolve().parent.parent / "data" / "corgi.db"),
    )
)

# ============================================================
# SECTION: Schema
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS hl_live_trades (
    trade_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS hl_opened_trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id     INTEGER NOT NULL,
    coin         TEXT    NOT NULL,
    side         TEXT    NOT NULL,
    -- entry_price is the LIMIT price the bot sent to HL (slippage-adjusted:
    -- mid*1.05 for longs, mid*0.95 for shorts). Kept as a fallback for rows
    -- opened before fill-reconciliation was added.
    entry_price  REAL    NOT NULL,
    -- my_fill_price is the ACTUAL average fill price from HL's user_fills_by_time.
    -- Populated post-open. NULL if the fill reconcile call failed or the bot
    -- row predates this column. Dashboard PnL should prefer this over entry_price.
    my_fill_price REAL,
    entry_sl     REAL,
    size         REAL    NOT NULL,
    margin       REAL    NOT NULL,
    leverage     REAL    NOT NULL,
    caller       TEXT,
    at           TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS hl_closed_trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id     INTEGER NOT NULL,
    coin         TEXT    NOT NULL,
    side         TEXT    NOT NULL,
    entry_price  REAL    NOT NULL,
    exit_price   REAL    NOT NULL,
    size         REAL    NOT NULL,
    trade_value  REAL,
    margin       REAL,
    fee          REAL,
    pnl          REAL,
    close_type   TEXT    NOT NULL
        CHECK (close_type IN ('automatic', 'manual', 'stop_triggered', 'pre-seeded')),
    at           TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS hl_sl_updates (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id             INTEGER NOT NULL,
    old_stop             REAL,
    new_stop             REAL    NOT NULL,
    size                 REAL,
    original_size        REAL,
    trigger_conditions   TEXT,
    at                   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS hl_tp_updates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id     INTEGER NOT NULL,
    tp_price     REAL    NOT NULL,
    tp_pct       REAL,
    tp_num       INTEGER,
    size         REAL,
    fee          REAL,
    at           TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS portal_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id    INTEGER,
    coin        TEXT,
    side        TEXT,
    caller      TEXT,
    event_type  TEXT    NOT NULL
        CHECK (event_type IN (
            'enter', 'cancel', 'tp_hit', 'auto_close',
            'stale_close', 'sl_triggered'
        )),
    details     TEXT,
    at          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS portal_cookies (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL,
    at     TEXT NOT NULL
);

-- Trades that arrived while the bot couldn't open them (e.g. insufficient
-- margin) — kept here until the drain task either succeeds or the user
-- removes them. Persists across restarts.
CREATE TABLE IF NOT EXISTS hl_pending_trades (
    trade_id      INTEGER PRIMARY KEY,
    coin          TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    caller        TEXT,
    event_json    TEXT    NOT NULL,
    reason        TEXT    NOT NULL,
    queued_at     TEXT    NOT NULL,
    last_retry_at TEXT,
    retry_count   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_opened_trade_id ON hl_opened_trades(trade_id);
CREATE INDEX IF NOT EXISTS idx_closed_trade_id ON hl_closed_trades(trade_id);
CREATE INDEX IF NOT EXISTS idx_closed_at       ON hl_closed_trades(at);
CREATE INDEX IF NOT EXISTS idx_sl_trade_id     ON hl_sl_updates(trade_id);
CREATE INDEX IF NOT EXISTS idx_tp_trade_id     ON hl_tp_updates(trade_id);
CREATE INDEX IF NOT EXISTS idx_portal_events_trade ON portal_events(trade_id);
CREATE INDEX IF NOT EXISTS idx_portal_events_at    ON portal_events(at);
"""

# ============================================================
# SECTION: Connection management (thread-local)
# ============================================================

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False
_db_path: Path = _DEFAULT_DB_PATH


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply column-addition migrations for DBs that predate newer columns."""
    def _has_col(table: str, col: str) -> bool:
        rows = conn.execute(f"PRAGMA table_info({table});").fetchall()
        return any(r[1] == col for r in rows)

    if not _has_col("hl_opened_trades", "my_fill_price"):
        conn.execute(
            "ALTER TABLE hl_opened_trades ADD COLUMN my_fill_price REAL;"
        )
        log.info("migration: added hl_opened_trades.my_fill_price")


def init_db(db_path: Optional[str | Path] = None) -> None:
    """Initialize the database file and apply the schema.

    Safe to call multiple times. Idempotent.
    """
    global _initialized, _db_path
    with _init_lock:
        if db_path is not None:
            _db_path = Path(db_path)
        _ensure_parent(_db_path)
        # Use a temporary connection solely for bootstrap so we don't cache
        # it against any particular thread-local before pragmas are set.
        conn = sqlite3.connect(str(_db_path), timeout=30.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.executescript(SCHEMA)
            # Lightweight migrations — adding columns to existing tables.
            # CREATE TABLE IF NOT EXISTS leaves pre-existing tables untouched,
            # so new columns must be added separately.
            _apply_migrations(conn)
        finally:
            conn.close()
        _initialized = True
        log.info("Database initialized at %s", _db_path)


def _get_conn() -> sqlite3.Connection:
    """Return a sqlite3.Connection scoped to the current thread."""
    global _initialized
    if not _initialized:
        init_db()
    conn: Optional[sqlite3.Connection] = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(
            str(_db_path),
            timeout=30.0,
            isolation_level=None,  # autocommit; we wrap transactions explicitly
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        _local.conn = conn
    return conn


def close_thread_conn() -> None:
    """Close the thread-local connection if one exists."""
    conn: Optional[sqlite3.Connection] = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _local.conn = None


def _execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    return _get_conn().execute(sql, tuple(params))


def _executemany(sql: str, seq: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
    return _get_conn().executemany(sql, [tuple(p) for p in seq])


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row is not None else None


# ============================================================
# SECTION: hl_live_trades — simple set of currently-live trade_ids
# ============================================================

def add_live_trade(trade_id: int) -> None:
    _execute(
        "INSERT OR IGNORE INTO hl_live_trades (trade_id) VALUES (?);",
        (int(trade_id),),
    )


def remove_live_trade(trade_id: int) -> None:
    _execute(
        "DELETE FROM hl_live_trades WHERE trade_id = ?;",
        (int(trade_id),),
    )


def get_live_trade(trade_id: int) -> Optional[dict]:
    row = _execute(
        "SELECT trade_id FROM hl_live_trades WHERE trade_id = ?;",
        (int(trade_id),),
    ).fetchone()
    return _row_to_dict(row)


def list_live_trades() -> list[dict]:
    """Return live trades joined with their opened-trade details."""
    rows = _execute(
        """
        SELECT lt.trade_id AS trade_id,
               ot.coin     AS coin,
               ot.side     AS side,
               ot.entry_price,
               ot.my_fill_price,
               ot.entry_sl,
               ot.size,
               ot.margin,
               ot.leverage,
               ot.caller,
               ot.at       AS opened_at
        FROM hl_live_trades lt
        LEFT JOIN hl_opened_trades ot
               ON ot.trade_id = lt.trade_id
              AND ot.id = (
                  SELECT MAX(id) FROM hl_opened_trades
                  WHERE trade_id = lt.trade_id
              )
        ORDER BY ot.id DESC;
        """
    ).fetchall()
    return [dict(r) for r in rows]


def is_coin_live(coin: str) -> bool:
    """True if any live trade currently holds the given coin.

    Used by the BLOCKED-card logic in the dashboard.
    """
    row = _execute(
        """
        SELECT 1
        FROM hl_live_trades lt
        JOIN hl_opened_trades ot
          ON ot.trade_id = lt.trade_id
         AND ot.id = (
             SELECT MAX(id) FROM hl_opened_trades
             WHERE trade_id = lt.trade_id
         )
        WHERE UPPER(ot.coin) = UPPER(?)
        LIMIT 1;
        """,
        (coin,),
    ).fetchone()
    return row is not None


# ============================================================
# SECTION: hl_opened_trades
# ============================================================

def insert_opened_trade(
    *,
    trade_id: int,
    coin: str,
    side: str,
    entry_price: float,
    entry_sl: Optional[float],
    size: float,
    margin: float,
    leverage: float,
    caller: Optional[str],
    my_fill_price: Optional[float] = None,
    at: Optional[str] = None,
) -> int:
    cur = _execute(
        """
        INSERT INTO hl_opened_trades
            (trade_id, coin, side, entry_price, my_fill_price, entry_sl,
             size, margin, leverage, caller, at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            int(trade_id),
            coin,
            side,
            float(entry_price),
            None if my_fill_price is None else float(my_fill_price),
            None if entry_sl is None else float(entry_sl),
            float(size),
            float(margin),
            float(leverage),
            caller,
            at or _utcnow_iso(),
        ),
    )
    return int(cur.lastrowid)


def update_opened_fill_price(trade_id: int, my_fill_price: float) -> None:
    """Backfill my_fill_price on the most recent opened row for a trade.

    Used when the fill-reconciliation call happens AFTER insert (e.g. when
    fills take a moment to settle on HL).
    """
    _execute(
        """
        UPDATE hl_opened_trades
        SET my_fill_price = ?
        WHERE id = (
            SELECT MAX(id) FROM hl_opened_trades WHERE trade_id = ?
        );
        """,
        (float(my_fill_price), int(trade_id)),
    )


def get_opened_trade(trade_id: int) -> Optional[dict]:
    """Return the most recent opened-trade row for a trade_id."""
    row = _execute(
        """
        SELECT * FROM hl_opened_trades
        WHERE trade_id = ?
        ORDER BY id DESC
        LIMIT 1;
        """,
        (int(trade_id),),
    ).fetchone()
    return _row_to_dict(row)


def get_closed_trade(trade_id: int) -> Optional[dict]:
    """Return the most recent closed-trade row for a trade_id, or None."""
    row = _execute(
        """
        SELECT * FROM hl_closed_trades
        WHERE trade_id = ?
        ORDER BY id DESC
        LIMIT 1;
        """,
        (int(trade_id),),
    ).fetchone()
    return _row_to_dict(row)


# ============================================================
# SECTION: hl_closed_trades
# ============================================================

def insert_closed_trade(
    *,
    trade_id: int,
    coin: str,
    side: str,
    entry_price: float,
    exit_price: float,
    size: float,
    trade_value: Optional[float],
    margin: Optional[float],
    fee: Optional[float],
    pnl: Optional[float],
    close_type: str,
    at: Optional[str] = None,
) -> int:
    if close_type not in ("automatic", "manual", "stop_triggered", "pre-seeded"):
        raise ValueError(f"invalid close_type: {close_type!r}")
    cur = _execute(
        """
        INSERT INTO hl_closed_trades
            (trade_id, coin, side, entry_price, exit_price,
             size, trade_value, margin, fee, pnl, close_type, at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            int(trade_id),
            coin,
            side,
            float(entry_price),
            float(exit_price),
            float(size),
            None if trade_value is None else float(trade_value),
            None if margin is None else float(margin),
            None if fee is None else float(fee),
            None if pnl is None else float(pnl),
            close_type,
            at or _utcnow_iso(),
        ),
    )
    return int(cur.lastrowid)


# ============================================================
# SECTION: hl_sl_updates / hl_tp_updates
# ============================================================

def insert_sl_update(
    *,
    trade_id: int,
    old_stop: Optional[float],
    new_stop: float,
    size: Optional[float] = None,
    original_size: Optional[float] = None,
    trigger_conditions: Optional[str] = None,
    at: Optional[str] = None,
) -> int:
    cur = _execute(
        """
        INSERT INTO hl_sl_updates
            (trade_id, old_stop, new_stop, size, original_size,
             trigger_conditions, at)
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        (
            int(trade_id),
            None if old_stop is None else float(old_stop),
            float(new_stop),
            None if size is None else float(size),
            None if original_size is None else float(original_size),
            trigger_conditions,
            at or _utcnow_iso(),
        ),
    )
    return int(cur.lastrowid)


def insert_tp_update(
    *,
    trade_id: int,
    tp_price: float,
    tp_pct: Optional[float] = None,
    tp_num: Optional[int] = None,
    size: Optional[float] = None,
    fee: Optional[float] = None,
    at: Optional[str] = None,
) -> int:
    cur = _execute(
        """
        INSERT INTO hl_tp_updates
            (trade_id, tp_price, tp_pct, tp_num, size, fee, at)
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        (
            int(trade_id),
            float(tp_price),
            None if tp_pct is None else float(tp_pct),
            None if tp_num is None else int(tp_num),
            None if size is None else float(size),
            None if fee is None else float(fee),
            at or _utcnow_iso(),
        ),
    )
    return int(cur.lastrowid)


def get_tp_price_from_history(trade_id: int, tp_num: int) -> Optional[float]:
    """Return the most recent recorded tp_price for (trade_id, tp_num).

    Used by main._auto_trail_stop_after_tp to look up TP1/TP2 prices when a
    later TP hits, because hl_opened_trades does not persist TP levels.
    Rows with tp_price <= 0 are filtered out — `insert_tp_update` falls back
    to 0.0 when both the portal `tp_price` and HL `avg_exit_price` are null,
    and trailing the SL to 0 would be catastrophic for a long position.

    Returns None if no qualifying row exists.
    """
    row = _execute(
        """
        SELECT tp_price FROM hl_tp_updates
        WHERE trade_id = ? AND tp_num = ? AND tp_price > 0
        ORDER BY id DESC
        LIMIT 1;
        """,
        (int(trade_id), int(tp_num)),
    ).fetchone()
    return float(row["tp_price"]) if row is not None else None


# ============================================================
# SECTION: portal_events
# ============================================================

def insert_portal_event(
    *,
    event_type: str,
    trade_id: Optional[int] = None,
    coin: Optional[str] = None,
    side: Optional[str] = None,
    caller: Optional[str] = None,
    details: Optional[dict | list | str] = None,
    at: Optional[str] = None,
) -> int:
    allowed = {"enter", "cancel", "tp_hit", "auto_close", "stale_close", "sl_triggered"}
    if event_type not in allowed:
        raise ValueError(f"invalid event_type: {event_type!r}")

    if details is None:
        details_str: Optional[str] = None
    elif isinstance(details, str):
        details_str = details
    else:
        details_str = json.dumps(details, separators=(",", ":"), default=str)

    cur = _execute(
        """
        INSERT INTO portal_events
            (trade_id, coin, side, caller, event_type, details, at)
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        (
            None if trade_id is None else int(trade_id),
            coin,
            side,
            caller,
            event_type,
            details_str,
            at or _utcnow_iso(),
        ),
    )
    return int(cur.lastrowid)


# ============================================================
# SECTION: portal_cookies
# ============================================================

def set_portal_cookie(key: str, value: str) -> None:
    _execute(
        """
        INSERT INTO portal_cookies (key, value, at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, at = excluded.at;
        """,
        (key, value, _utcnow_iso()),
    )


def set_portal_cookies(cookies: dict[str, str]) -> None:
    now = _utcnow_iso()
    _executemany(
        """
        INSERT INTO portal_cookies (key, value, at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, at = excluded.at;
        """,
        [(k, v, now) for k, v in cookies.items()],
    )


def get_portal_cookie(key: str) -> Optional[str]:
    row = _execute(
        "SELECT value FROM portal_cookies WHERE key = ?;",
        (key,),
    ).fetchone()
    return row["value"] if row else None


def get_portal_cookies() -> dict[str, str]:
    rows = _execute("SELECT key, value FROM portal_cookies;").fetchall()
    return {r["key"]: r["value"] for r in rows}


def clear_portal_cookies() -> None:
    _execute("DELETE FROM portal_cookies;")


# ============================================================
# SECTION: Stats & dashboard queries
# ============================================================

def get_stats() -> dict:
    """Return aggregate stats for the dashboard stats header."""
    row = _execute(
        """
        SELECT
            COALESCE(SUM(pnl), 0.0)                            AS total_pnl,
            COALESCE(SUM(fee), 0.0)                            AS total_fees,
            COUNT(*)                                           AS total_closed,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)           AS wins,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END)           AS losses
        FROM hl_closed_trades
        WHERE close_type != 'pre-seeded';
        """
    ).fetchone()

    open_row = _execute(
        "SELECT COUNT(*) AS n FROM hl_live_trades;"
    ).fetchone()

    total_closed = row["total_closed"] or 0
    wins = row["wins"] or 0
    win_rate = (wins / total_closed) if total_closed > 0 else 0.0

    return {
        "total_pnl":     float(row["total_pnl"] or 0.0),
        "total_fees":    float(row["total_fees"] or 0.0),
        "total_closed":  int(total_closed),
        "wins":          int(wins),
        "losses":        int(row["losses"] or 0),
        "win_rate":      float(win_rate),
        "open_count":    int(open_row["n"] or 0),
    }


def get_historic_trades(limit: int = 200, offset: int = 0) -> list[dict]:
    """Return closed trades joined with their open-side data for the table."""
    rows = _execute(
        """
        SELECT
            ct.id           AS id,
            ct.trade_id     AS trade_id,
            ct.coin         AS coin,
            ct.side         AS side,
            ct.entry_price  AS entry_price,
            ct.exit_price   AS exit_price,
            ct.size         AS size,
            ct.trade_value  AS trade_value,
            ct.margin       AS margin,
            ct.fee          AS fee,
            ct.pnl          AS pnl,
            ct.close_type   AS close_type,
            ct.at           AS closed_at,
            ot.caller       AS caller,
            ot.leverage     AS leverage,
            ot.at           AS opened_at
        FROM hl_closed_trades ct
        LEFT JOIN hl_opened_trades ot
               ON ot.trade_id = ct.trade_id
              AND ot.id = (
                  SELECT MAX(id) FROM hl_opened_trades
                  WHERE trade_id = ct.trade_id
              )
        WHERE ct.close_type != 'pre-seeded'
        ORDER BY ct.id DESC
        LIMIT ? OFFSET ?;
        """,
        (int(limit), int(offset)),
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_portal_events(limit: int = 100) -> list[dict]:
    """Return the most recent portal events for the sidebar activity feed."""
    rows = _execute(
        """
        SELECT id, trade_id, coin, side, caller, event_type, details, at
        FROM portal_events
        ORDER BY id DESC
        LIMIT ?;
        """,
        (int(limit),),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        raw = d.get("details")
        if raw:
            try:
                d["details"] = json.loads(raw)
            except (ValueError, TypeError):
                # leave as raw string if it isn't valid JSON
                pass
        out.append(d)
    return out


# ============================================================
# SECTION: hl_pending_trades — trades queued because the bot couldn't open them
# ============================================================

def insert_pending_trade(
    *,
    trade_id: int,
    coin: str,
    side: str,
    caller: Optional[str],
    event: dict,
    reason: str,
    at: Optional[str] = None,
) -> None:
    """Add or update a trade in the pending queue (idempotent on trade_id).

    `event` is the full new_trade event dict (will be JSON-serialized so the
    drain task can re-construct it without re-fetching from the portal).
    """
    _execute(
        """
        INSERT INTO hl_pending_trades
            (trade_id, coin, side, caller, event_json, reason, queued_at, retry_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(trade_id) DO UPDATE SET
            reason     = excluded.reason,
            event_json = excluded.event_json;
        """,
        (
            int(trade_id),
            coin,
            side,
            caller,
            json.dumps(event, default=str),
            reason,
            at or _utcnow_iso(),
        ),
    )


def remove_pending_trade(trade_id: int) -> None:
    _execute(
        "DELETE FROM hl_pending_trades WHERE trade_id = ?;",
        (int(trade_id),),
    )


def get_pending_trade(trade_id: int) -> Optional[dict]:
    row = _execute(
        "SELECT * FROM hl_pending_trades WHERE trade_id = ?;",
        (int(trade_id),),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["event"] = json.loads(d["event_json"])
    except (ValueError, TypeError):
        d["event"] = {}
    return d


def list_pending_trades() -> list[dict]:
    """Return queued trades ordered oldest-first (FIFO drain order)."""
    rows = _execute(
        "SELECT * FROM hl_pending_trades ORDER BY queued_at ASC, trade_id ASC;"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["event"] = json.loads(d["event_json"])
        except (ValueError, TypeError):
            d["event"] = {}
        out.append(d)
    return out


def bump_pending_retry(trade_id: int) -> None:
    _execute(
        """
        UPDATE hl_pending_trades
        SET retry_count   = retry_count + 1,
            last_retry_at = ?
        WHERE trade_id = ?;
        """,
        (_utcnow_iso(), int(trade_id)),
    )


# ============================================================
# SECTION: Module load — ensure the DB is ready for any caller.
# ============================================================

init_db()
