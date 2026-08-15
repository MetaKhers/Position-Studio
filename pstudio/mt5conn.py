"""Connection to a MetaTrader 5 terminal.

The MetaTrader5 package talks to exactly one terminal per process, and
`initialize()` on a second path silently keeps you on the first. So every
connection here is exclusive: a module-level lock serialises access, and
switching terminals means a full shutdown first.

`initialize(path=...)` will start the terminal if it is closed. That is
deliberate - it is the only way to reach history for a terminal the user is
not currently running - but it is slow, so callers hold a session open across
a whole sync rather than reconnecting per call.
"""

from __future__ import annotations

import datetime as dt
import threading
from contextlib import contextmanager
from typing import Any

_LOCK = threading.RLock()
_ACTIVE: str | None = None

TIMEFRAME_CODES = {
    "M1": "TIMEFRAME_M1",
    "M2": "TIMEFRAME_M2",
    "M3": "TIMEFRAME_M3",
    "M5": "TIMEFRAME_M5",
    "M10": "TIMEFRAME_M10",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H2": "TIMEFRAME_H2",
    "H4": "TIMEFRAME_H4",
    "H6": "TIMEFRAME_H6",
    "H8": "TIMEFRAME_H8",
    "H12": "TIMEFRAME_H12",
    "D1": "TIMEFRAME_D1",
    "W1": "TIMEFRAME_W1",
    "MN1": "TIMEFRAME_MN1",
}

# Bar length in seconds - used to size the window a shot needs.
TIMEFRAME_SECONDS = {
    "M1": 60, "M2": 120, "M3": 180, "M5": 300, "M10": 600, "M15": 900,
    "M30": 1800, "H1": 3600, "H2": 7200, "H4": 14400, "H6": 21600,
    "H8": 28800, "H12": 43200, "D1": 86400, "W1": 604800, "MN1": 2592000,
}

DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
DEAL_ENTRY_INOUT = 2
DEAL_ENTRY_OUT_BY = 3

# DEAL_REASON_* -> label. These are what actually closed the position.
DEAL_REASONS = {
    0: "Client",
    1: "Mobile",
    2: "Web",
    3: "Expert",
    4: "Stop Loss",
    5: "Take Profit",
    6: "Stop Out",
    7: "Rollover",
    8: "Variation Margin",
    9: "Split",
}


class MT5Error(RuntimeError):
    pass


def available() -> bool:
    try:
        import MetaTrader5  # noqa: F401
    except Exception:
        return False
    return True


def _mt5():
    try:
        import MetaTrader5 as mt5
    except Exception as exc:  # pragma: no cover - import guard
        raise MT5Error(
            "The MetaTrader5 package is not installed in this Python environment."
        ) from exc
    return mt5


def timeframe_const(name: str) -> int:
    mt5 = _mt5()
    code = TIMEFRAME_CODES.get(name.upper())
    if not code:
        raise MT5Error(f"unknown timeframe: {name}")
    return int(getattr(mt5, code))


class Session:
    """An open connection to one terminal. Use via `connect()`."""

    def __init__(self, exe_path: str, login: int | None = None):
        self.exe_path = exe_path
        self.login = login
        self.mt5 = _mt5()

    # -- context -----------------------------------------------------------
    def terminal_info(self) -> dict:
        info = self.mt5.terminal_info()
        if info is None:
            return {}
        data = info._asdict()
        return {
            "name": data.get("name"),
            "company": data.get("company"),
            "build": data.get("build"),
            "data_path": data.get("data_path"),
            "connected": bool(data.get("connected")),
            "trade_allowed": bool(data.get("trade_allowed")),
        }

    def account_info(self) -> dict:
        info = self.mt5.account_info()
        if info is None:
            raise MT5Error(
                "Terminal is running but no account is logged in. "
                "Log in inside MetaTrader 5, then rescan."
            )
        data = info._asdict()
        return {
            "login": int(data["login"]),
            "server": data.get("server"),
            "holder": data.get("name"),
            "company": data.get("company"),
            "currency": data.get("currency"),
            "leverage": data.get("leverage"),
            "balance": data.get("balance"),
            "equity": data.get("equity"),
            "is_demo": data.get("trade_mode") == 0,
        }

    # -- history -----------------------------------------------------------
    def deals(self, since: dt.datetime, until: dt.datetime) -> list[dict]:
        raw = self.mt5.history_deals_get(to_mt5_time(since), to_mt5_time(until))
        if raw is None:
            code, message = self.mt5.last_error()
            if code in (1, 0):
                return []
            raise MT5Error(f"history_deals_get failed: {message} ({code})")
        return [d._asdict() for d in raw]

    def orders(self, since: dt.datetime, until: dt.datetime) -> list[dict]:
        raw = self.mt5.history_orders_get(to_mt5_time(since), to_mt5_time(until))
        if raw is None:
            return []
        return [o._asdict() for o in raw]

    def open_positions(self) -> list[dict]:
        raw = self.mt5.positions_get()
        return [p._asdict() for p in raw] if raw else []

    # -- market data -------------------------------------------------------
    def symbol_info(self, symbol: str) -> dict | None:
        info = self.mt5.symbol_info(symbol)
        if info is None:
            # Symbols hidden from Market Watch need selecting before use.
            if not self.mt5.symbol_select(symbol, True):
                return None
            info = self.mt5.symbol_info(symbol)
        if info is None:
            return None
        data = info._asdict()
        return {
            "name": data.get("name"),
            "digits": data.get("digits"),
            "point": data.get("point"),
            "spread": data.get("spread"),
            "contract_size": data.get("trade_contract_size"),
            "tick_value": data.get("trade_tick_value"),
            "tick_size": data.get("trade_tick_size"),
            "currency_profit": data.get("currency_profit"),
            "description": data.get("description"),
            "path": data.get("path"),
        }

    def ensure_symbol(self, symbol: str) -> bool:
        if self.mt5.symbol_info(symbol) is not None:
            return True
        return bool(self.mt5.symbol_select(symbol, True))

    def rates_range(self, symbol: str, timeframe: str, since: dt.datetime,
                    until: dt.datetime) -> list[dict]:
        self.ensure_symbol(symbol)
        raw = self.mt5.copy_rates_range(
            symbol, timeframe_const(timeframe), to_mt5_time(since), to_mt5_time(until)
        )
        return _rates_to_dicts(raw)

    def rates_from(self, symbol: str, timeframe: str, anchor: dt.datetime,
                   count: int) -> list[dict]:
        """`count` bars ending at (or just before) `anchor`."""
        self.ensure_symbol(symbol)
        raw = self.mt5.copy_rates_from(
            symbol, timeframe_const(timeframe), to_mt5_time(anchor), int(count)
        )
        return _rates_to_dicts(raw)

    def rates_window(self, symbol: str, timeframe: str, center: dt.datetime,
                     before: int, after: int) -> list[dict]:
        """Bars around a moment: `before` leading, `after` trailing.

        copy_rates_from only looks backwards, so the forward half is fetched
        as a range and the two are stitched and de-duplicated on bar time.
        """
        self.ensure_symbol(symbol)
        code = timeframe_const(timeframe)
        step = TIMEFRAME_SECONDS.get(timeframe.upper(), 60)
        left = _rates_to_dicts(
            self.mt5.copy_rates_from(symbol, code, to_mt5_time(center), int(before) + 1)
        )
        right: list[dict] = []
        if after > 0:
            # Ask for a generous span; markets close and bars go missing.
            end = center + dt.timedelta(seconds=step * (after + 2))
            right = _rates_to_dicts(
                self.mt5.copy_rates_range(
                    symbol, code, to_mt5_time(center), to_mt5_time(end)
                )
            )
        merged: dict[int, dict] = {}
        for bar in left + right:
            merged[int(bar["time"])] = bar
        bars = [merged[key] for key in sorted(merged)]
        # Trim the forward side to what was asked for, keeping the anchor bar.
        # Compare epochs directly: both sides are broker time already.
        anchor_ts = int((center - _EPOCH).total_seconds())
        pivot = 0
        for index, bar in enumerate(bars):
            if bar["time"] <= anchor_ts:
                pivot = index
            else:
                break
        start = max(0, pivot - before)
        return bars[start : pivot + after + 1]

    def ticks(self, symbol: str, since: dt.datetime, until: dt.datetime) -> list[dict]:
        self.ensure_symbol(symbol)
        mt5 = self.mt5
        raw = mt5.copy_ticks_range(
            symbol, to_mt5_time(since), to_mt5_time(until), mt5.COPY_TICKS_ALL
        )
        if raw is None or len(raw) == 0:
            return []
        return [
            {
                "time_msc": int(t["time_msc"]),
                "bid": float(t["bid"]),
                "ask": float(t["ask"]),
                "last": float(t["last"]),
            }
            for t in raw
        ]

    def calc_profit(self, side: str, symbol: str, volume: float,
                    open_price: float, close_price: float) -> float | None:
        mt5 = self.mt5
        order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
        value = mt5.order_calc_profit(
            order_type, symbol, float(volume), float(open_price), float(close_price)
        )
        return float(value) if value is not None else None


def _rates_to_dicts(raw) -> list[dict]:
    if raw is None or len(raw) == 0:
        return []
    return [
        {
            "time": int(r["time"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": int(r["tick_volume"]),
            "spread": int(r["spread"]),
        }
        for r in raw
    ]


_EPOCH = dt.datetime(1970, 1, 1)


def to_mt5_time(moment: dt.datetime) -> dt.datetime:
    """Convert a broker-time datetime into what the package expects as input.

    This is the sharpest edge in the whole MT5 Python bridge. Timestamps come
    *out* as epoch seconds in broker server time, but datetimes passed *in* are
    interpreted against the machine's local timezone. On a PC at UTC+3:30 that
    is a silent 3.5-hour shift: tick queries return the wrong slice of the day
    and MAE/MFE get computed on prices the trade never saw.

    `datetime.fromtimestamp` applies exactly the local offset the package is
    about to undo, so the round trip is exact whatever the machine's timezone
    or the broker's server offset. Every call below goes through here.
    """
    if moment.tzinfo is not None:
        moment = moment.replace(tzinfo=None)
    return dt.datetime.fromtimestamp((moment - _EPOCH).total_seconds())


@contextmanager
def connect(exe_path: str, login: int | None = None, password: str | None = None,
            server: str | None = None, timeout_ms: int = 60000):
    """Open an exclusive session against one terminal."""
    global _ACTIVE
    mt5 = _mt5()
    with _LOCK:
        if _ACTIVE is not None and _ACTIVE != exe_path:
            mt5.shutdown()
            _ACTIVE = None
        kwargs: dict[str, Any] = {"path": exe_path, "timeout": timeout_ms}
        if login and password:
            kwargs.update(login=int(login), password=password)
            if server:
                kwargs["server"] = server
        ok = mt5.initialize(**kwargs)
        if not ok:
            code, message = mt5.last_error()
            mt5.shutdown()
            _ACTIVE = None
            raise MT5Error(_explain(code, message, exe_path))
        _ACTIVE = exe_path
        try:
            yield Session(exe_path, login)
        finally:
            mt5.shutdown()
            _ACTIVE = None


def _explain(code: int, message: str, exe_path: str) -> str:
    hints = {
        -10005: "Timed out starting the terminal. Open it manually once, then retry.",
        -10003: "That path is not a MetaTrader 5 terminal executable.",
        -10004: "The terminal refused the connection - check it is not mid-update.",
        -10001: "Could not start the terminal process.",
        -10002: "The terminal is too old for the Python bridge; update MT5.",
    }
    hint = hints.get(code, "")
    return f"Could not connect to {exe_path}: {message} ({code}). {hint}".strip()


def probe(exe_path: str) -> dict:
    """Connect briefly to read terminal and account identity."""
    with connect(exe_path) as session:
        terminal = session.terminal_info()
        try:
            account = session.account_info()
            error = None
        except MT5Error as exc:
            account = None
            error = str(exc)
        return {"terminal": terminal, "account": account, "error": error}
