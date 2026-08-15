"""SQLite store.

The database is the app's memory of what it has already looked at. Positions
carry an `analyzed_at` and `captured_at` stamp so a rescan only does the work
that is genuinely new - that is the whole point of the "if new data (or old
history that hasn't been analyzed)" requirement.

Design notes:
  * WAL mode, because the render worker writes while the HTTP thread reads.
  * Positions are keyed by (account_id, ticket) - MT5 position ids are unique
    per account, not globally.
  * Computed metrics live in a JSON blob rather than 60 columns. They evolve
    as the metrics module grows; the columns that get filtered or sorted in
    SQL are promoted to real columns.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import paths

_LOCAL = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS terminals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    exe_path     TEXT    NOT NULL UNIQUE,
    data_dir     TEXT,
    instance_id  TEXT,
    broker       TEXT,
    build        INTEGER,
    is_portable  INTEGER DEFAULT 0,
    is_manual    INTEGER DEFAULT 0,
    enabled      INTEGER DEFAULT 1,
    known_logins TEXT,
    added_at     REAL,
    last_seen    REAL,
    last_error   TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id  INTEGER NOT NULL REFERENCES terminals(id) ON DELETE CASCADE,
    login        INTEGER NOT NULL,
    server       TEXT,
    holder       TEXT,
    company      TEXT,
    currency     TEXT,
    leverage     INTEGER,
    balance      REAL,
    equity       REAL,
    is_demo      INTEGER DEFAULT 0,
    first_deal   REAL,
    last_deal    REAL,
    start_balance REAL,
    last_sync    REAL,
    UNIQUE(terminal_id, login)
);

CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    ticket        INTEGER NOT NULL,
    symbol        TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    volume        REAL,
    open_time     REAL,
    close_time    REAL,
    open_price    REAL,
    close_price   REAL,
    sl_initial    REAL,
    tp_initial    REAL,
    sl_final      REAL,
    tp_final      REAL,
    gross_profit  REAL,
    commission    REAL,
    swap          REAL,
    fee           REAL,
    net_profit    REAL,
    magic         INTEGER,
    exit_reason   TEXT,
    entry_comment TEXT,
    exit_comment  TEXT,
    partials      INTEGER DEFAULT 0,
    duration_s    REAL,
    metrics_json  TEXT,
    analyzed_at   REAL,
    captured_at   REAL,
    note          TEXT,
    tags          TEXT,
    UNIQUE(account_id, ticket)
);

CREATE INDEX IF NOT EXISTS idx_pos_account_close
    ON positions(account_id, close_time);
CREATE INDEX IF NOT EXISTS idx_pos_pending
    ON positions(account_id, analyzed_at);
CREATE INDEX IF NOT EXISTS idx_pos_symbol
    ON positions(account_id, symbol);

CREATE TABLE IF NOT EXISTS deals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    ticket       INTEGER NOT NULL,
    position_id  INTEGER,
    order_ticket INTEGER,
    time_msc     INTEGER,
    type         INTEGER,
    entry        INTEGER,
    reason       INTEGER,
    symbol       TEXT,
    volume       REAL,
    price        REAL,
    profit       REAL,
    commission   REAL,
    swap         REAL,
    fee          REAL,
    comment      TEXT,
    UNIQUE(account_id, ticket)
);

CREATE INDEX IF NOT EXISTS idx_deals_position ON deals(account_id, position_id);

CREATE TABLE IF NOT EXISTS shots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id  INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    timeframe    TEXT    NOT NULL,
    event        TEXT    NOT NULL,
    rel_path     TEXT    NOT NULL,
    width        INTEGER,
    height       INTEGER,
    bars         INTEGER,
    source       TEXT DEFAULT 'studio',
    created_at   REAL,
    UNIQUE(position_id, timeframe, event, source)
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT,
    account_id   INTEGER,
    started_at   REAL,
    finished_at  REAL,
    status       TEXT,
    detail       TEXT,
    counts_json  TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect() -> sqlite3.Connection:
    """One connection per thread, created on first use."""
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        return conn
    conn = sqlite3.connect(paths.db_path(), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _LOCAL.conn = conn
    return conn


def init() -> None:
    conn = connect()
    conn.executescript(SCHEMA)


def close() -> None:
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        conn.close()
        _LOCAL.conn = None


# -- small helpers ---------------------------------------------------------
def query(sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, tuple(params)).fetchall()


def one(sql: str, params: Iterable = ()) -> sqlite3.Row | None:
    return connect().execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable = ()) -> sqlite3.Cursor:
    return connect().execute(sql, tuple(params))


def executemany(sql: str, seq: Iterable[Iterable]) -> sqlite3.Cursor:
    return connect().executemany(sql, [tuple(p) for p in seq])


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def set_meta(key: str, value: Any) -> None:
    execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def get_meta(key: str, default: Any = None) -> Any:
    row = one("SELECT value FROM meta WHERE key=?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


# -- terminals -------------------------------------------------------------
def upsert_terminal(info: dict) -> int:
    """Insert or refresh a discovered terminal, keyed on its exe path."""
    now = time.time()
    logins = json.dumps(info.get("known_logins") or [])
    execute(
        """
        INSERT INTO terminals
            (name, exe_path, data_dir, instance_id, broker, build, is_portable,
             is_manual, enabled, known_logins, added_at, last_seen)
        VALUES (?,?,?,?,?,?,?,?,1,?,?,?)
        ON CONFLICT(exe_path) DO UPDATE SET
            name=excluded.name,
            data_dir=COALESCE(excluded.data_dir, terminals.data_dir),
            instance_id=COALESCE(excluded.instance_id, terminals.instance_id),
            broker=COALESCE(excluded.broker, terminals.broker),
            build=COALESCE(excluded.build, terminals.build),
            is_portable=excluded.is_portable,
            known_logins=excluded.known_logins,
            last_seen=excluded.last_seen
        """,
        (
            info.get("name") or "MetaTrader 5",
            str(info["exe_path"]),
            str(info.get("data_dir") or "") or None,
            info.get("instance_id"),
            info.get("broker"),
            info.get("build"),
            1 if info.get("is_portable") else 0,
            1 if info.get("is_manual") else 0,
            logins,
            now,
            now,
        ),
    )
    row = one("SELECT id FROM terminals WHERE exe_path=?", (str(info["exe_path"]),))
    return int(row["id"]) if row else 0


def list_terminals() -> list[dict]:
    out = []
    for row in query("SELECT * FROM terminals ORDER BY name"):
        item = dict(row)
        try:
            item["known_logins"] = json.loads(item.get("known_logins") or "[]")
        except Exception:
            item["known_logins"] = []
        item["exists"] = Path(item["exe_path"]).exists()
        item["accounts"] = rows_to_dicts(
            query(
                "SELECT id, login, server, currency, balance, equity, last_sync,"
                " (SELECT COUNT(*) FROM positions p WHERE p.account_id=a.id) AS positions"
                " FROM accounts a WHERE terminal_id=? ORDER BY login",
                (item["id"],),
            )
        )
        out.append(item)
    return out


def delete_terminal(terminal_id: int) -> None:
    execute("DELETE FROM terminals WHERE id=?", (terminal_id,))


def set_terminal_enabled(terminal_id: int, enabled: bool) -> None:
    execute(
        "UPDATE terminals SET enabled=? WHERE id=?", (1 if enabled else 0, terminal_id)
    )


def set_terminal_error(terminal_id: int, message: str | None) -> None:
    execute("UPDATE terminals SET last_error=? WHERE id=?", (message, terminal_id))


# -- accounts --------------------------------------------------------------
def upsert_account(terminal_id: int, info: dict) -> int:
    execute(
        """
        INSERT INTO accounts
            (terminal_id, login, server, holder, company, currency, leverage,
             balance, equity, is_demo, last_sync)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(terminal_id, login) DO UPDATE SET
            server=excluded.server,
            holder=excluded.holder,
            company=excluded.company,
            currency=excluded.currency,
            leverage=excluded.leverage,
            balance=excluded.balance,
            equity=excluded.equity,
            is_demo=excluded.is_demo,
            last_sync=excluded.last_sync
        """,
        (
            terminal_id,
            int(info["login"]),
            info.get("server"),
            info.get("holder"),
            info.get("company"),
            info.get("currency"),
            info.get("leverage"),
            info.get("balance"),
            info.get("equity"),
            1 if info.get("is_demo") else 0,
            time.time(),
        ),
    )
    row = one(
        "SELECT id FROM accounts WHERE terminal_id=? AND login=?",
        (terminal_id, int(info["login"])),
    )
    return int(row["id"]) if row else 0


def get_account(account_id: int) -> dict | None:
    row = one(
        "SELECT a.*, t.name AS terminal_name, t.exe_path, t.broker"
        " FROM accounts a JOIN terminals t ON t.id=a.terminal_id WHERE a.id=?",
        (account_id,),
    )
    return dict(row) if row else None


def list_accounts() -> list[dict]:
    return rows_to_dicts(
        query(
            "SELECT a.*, t.name AS terminal_name, t.exe_path,"
            " (SELECT COUNT(*) FROM positions p WHERE p.account_id=a.id) AS positions,"
            " (SELECT COUNT(*) FROM positions p WHERE p.account_id=a.id"
            "   AND p.analyzed_at IS NULL) AS pending"
            " FROM accounts a JOIN terminals t ON t.id=a.terminal_id"
            " ORDER BY a.last_sync DESC"
        )
    )


def set_account_range(account_id: int, first: float | None, last: float | None,
                      start_balance: float | None) -> None:
    execute(
        "UPDATE accounts SET first_deal=COALESCE(?, first_deal),"
        " last_deal=COALESCE(?, last_deal),"
        " start_balance=COALESCE(?, start_balance) WHERE id=?",
        (first, last, start_balance, account_id),
    )


# -- positions -------------------------------------------------------------
POSITION_FIELDS = (
    "ticket symbol side volume open_time close_time open_price close_price "
    "sl_initial tp_initial sl_final tp_final gross_profit commission swap fee "
    "net_profit magic exit_reason entry_comment exit_comment partials duration_s"
).split()


def upsert_position(account_id: int, pos: dict) -> tuple[int, bool]:
    """Returns (row id, is_new). Existing rows are refreshed but keep their
    analysis stamps unless the underlying trade data actually changed."""
    existing = one(
        "SELECT id, close_time, net_profit, sl_final, partials FROM positions"
        " WHERE account_id=? AND ticket=?",
        (account_id, pos["ticket"]),
    )
    values = [pos.get(f) for f in POSITION_FIELDS]
    if existing is None:
        placeholders = ",".join("?" * (len(POSITION_FIELDS) + 1))
        execute(
            f"INSERT INTO positions (account_id,{','.join(POSITION_FIELDS)})"
            f" VALUES ({placeholders})",
            [account_id] + values,
        )
        row = one(
            "SELECT id FROM positions WHERE account_id=? AND ticket=?",
            (account_id, pos["ticket"]),
        )
        return (int(row["id"]) if row else 0), True

    changed = (
        _differs(existing["close_time"], pos.get("close_time"))
        or _differs(existing["net_profit"], pos.get("net_profit"))
        or _differs(existing["sl_final"], pos.get("sl_final"))
        or int(existing["partials"] or 0) != int(pos.get("partials") or 0)
    )
    assignments = ",".join(f"{f}=?" for f in POSITION_FIELDS)
    sql = f"UPDATE positions SET {assignments}"
    if changed:
        # The trade itself moved - any prior analysis is stale.
        sql += ", analyzed_at=NULL, captured_at=NULL, metrics_json=NULL"
    sql += " WHERE id=?"
    execute(sql, values + [existing["id"]])
    return int(existing["id"]), False


def _differs(a, b, tol: float = 1e-9) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    try:
        return abs(float(a) - float(b)) > tol
    except (TypeError, ValueError):
        return a != b


def save_metrics(position_row_id: int, metrics: dict) -> None:
    execute(
        "UPDATE positions SET metrics_json=?, analyzed_at=? WHERE id=?",
        (json.dumps(metrics, default=float), time.time(), position_row_id),
    )


def mark_captured(position_row_id: int) -> None:
    execute(
        "UPDATE positions SET captured_at=? WHERE id=?", (time.time(), position_row_id)
    )


def clear_analysis(account_id: int) -> int:
    cur = execute(
        "UPDATE positions SET analyzed_at=NULL, captured_at=NULL, metrics_json=NULL"
        " WHERE account_id=?",
        (account_id,),
    )
    return cur.rowcount or 0


def position_rows(account_id: int, only_pending: bool = False,
                  limit: int | None = None) -> list[dict]:
    sql = "SELECT * FROM positions WHERE account_id=?"
    params: list[Any] = [account_id]
    if only_pending:
        sql += " AND (analyzed_at IS NULL OR captured_at IS NULL)"
    sql += " ORDER BY close_time DESC, open_time DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    out = []
    for row in query(sql, params):
        item = dict(row)
        if item.get("metrics_json"):
            try:
                item["metrics"] = json.loads(item["metrics_json"])
            except Exception:
                item["metrics"] = {}
        else:
            item["metrics"] = {}
        item.pop("metrics_json", None)
        out.append(item)
    return out


def position_by_ticket(account_id: int, ticket: int) -> dict | None:
    rows = [p for p in position_rows(account_id) if int(p["ticket"]) == int(ticket)]
    return rows[0] if rows else None


def pending_count(account_id: int) -> int:
    row = one(
        "SELECT COUNT(*) AS n FROM positions WHERE account_id=?"
        " AND (analyzed_at IS NULL OR captured_at IS NULL)",
        (account_id,),
    )
    return int(row["n"]) if row else 0


def set_note(position_row_id: int, note: str, tags: str | None = None) -> None:
    execute(
        "UPDATE positions SET note=?, tags=COALESCE(?, tags) WHERE id=?",
        (note, tags, position_row_id),
    )


# -- deals -----------------------------------------------------------------
def store_deals(account_id: int, deals: Iterable[dict]) -> int:
    payload = [
        (
            account_id,
            d["ticket"],
            d.get("position_id"),
            d.get("order"),
            d.get("time_msc"),
            d.get("type"),
            d.get("entry"),
            d.get("reason"),
            d.get("symbol"),
            d.get("volume"),
            d.get("price"),
            d.get("profit"),
            d.get("commission"),
            d.get("swap"),
            d.get("fee"),
            d.get("comment"),
        )
        for d in deals
    ]
    if not payload:
        return 0
    executemany(
        "INSERT INTO deals (account_id,ticket,position_id,order_ticket,time_msc,"
        "type,entry,reason,symbol,volume,price,profit,commission,swap,fee,comment)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(account_id,ticket) DO NOTHING",
        payload,
    )
    return len(payload)


# -- shots -----------------------------------------------------------------
def record_shot(position_row_id: int, timeframe: str, event: str, rel_path: str,
                width: int, height: int, bars: int, source: str = "studio") -> None:
    execute(
        "INSERT INTO shots (position_id,timeframe,event,rel_path,width,height,"
        "bars,source,created_at) VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(position_id,timeframe,event,source) DO UPDATE SET"
        " rel_path=excluded.rel_path, width=excluded.width,"
        " height=excluded.height, bars=excluded.bars,"
        " created_at=excluded.created_at",
        (
            position_row_id,
            timeframe,
            event,
            rel_path,
            width,
            height,
            bars,
            source,
            time.time(),
        ),
    )


def shots_for(position_row_id: int) -> list[dict]:
    return rows_to_dicts(
        query(
            "SELECT * FROM shots WHERE position_id=? ORDER BY id", (position_row_id,)
        )
    )


def shot_counts(account_id: int) -> dict[int, int]:
    rows = query(
        "SELECT s.position_id AS pid, COUNT(*) AS n FROM shots s"
        " JOIN positions p ON p.id=s.position_id WHERE p.account_id=?"
        " GROUP BY s.position_id",
        (account_id,),
    )
    return {int(r["pid"]): int(r["n"]) for r in rows}


# -- runs ------------------------------------------------------------------
def start_run(kind: str, account_id: int | None, detail: str = "") -> int:
    cur = execute(
        "INSERT INTO runs (kind,account_id,started_at,status,detail)"
        " VALUES (?,?,?,'running',?)",
        (kind, account_id, time.time(), detail),
    )
    return int(cur.lastrowid or 0)


def finish_run(run_id: int, status: str, counts: dict | None = None,
               detail: str = "") -> None:
    execute(
        "UPDATE runs SET finished_at=?, status=?, counts_json=?,"
        " detail=COALESCE(NULLIF(?,''), detail) WHERE id=?",
        (time.time(), status, json.dumps(counts or {}), detail, run_id),
    )


def recent_runs(limit: int = 25) -> list[dict]:
    return rows_to_dicts(
        query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
    )
