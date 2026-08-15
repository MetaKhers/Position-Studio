"""Position shape, duration formatting and time bucketing.

One subtlety worth recording: MT5 reports deal and bar times as epoch seconds
in *broker server time*, not local time. Both sides use the same convention so
they line up with each other, but converting either to a local timezone would
break that alignment and shift charts against trades. Everything here treats
timestamps as naive broker time - `dt.datetime.fromtimestamp` on the same
machine is only used for display, never for matching.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

# Prices quoted inside deal comments when a stop or target fires:
# "[sl 52201.74]", "[tp 25336.19]".
_BRACKET_PRICE = re.compile(r"\[(sl|tp)\s+([\d.]+)\]", re.I)


def broker_dt(epoch: float | int | None) -> dt.datetime | None:
    """Epoch seconds -> naive datetime in broker time."""
    if epoch is None:
        return None
    return dt.datetime(1970, 1, 1) + dt.timedelta(seconds=float(epoch))


def epoch(moment: dt.datetime) -> float:
    return (moment - dt.datetime(1970, 1, 1)).total_seconds()


def parse_bracket_price(comment: str | None, kind: str) -> float | None:
    for match in _BRACKET_PRICE.finditer(comment or ""):
        if match.group(1).lower() == kind.lower():
            try:
                return float(match.group(2))
            except ValueError:
                return None
    return None


def format_duration(seconds: float | None) -> str:
    """Duration in the units a trader actually thinks in.

    Under 61 seconds it is reported in seconds - a scalper's sub-minute trade
    is not "0m". Under an hour, minutes and seconds. Beyond that, hours and
    minutes, and days once it runs over one.
    """
    if seconds is None:
        return ""
    total = max(0.0, float(seconds))
    if total < 61:
        return f"{total:.0f}s" if total >= 10 else f"{total:.1f}s"
    if total < 3600:
        minutes, secs = divmod(int(round(total)), 60)
        return f"{minutes}m {secs:02d}s"
    if total < 86400:
        hours, rest = divmod(int(round(total)), 3600)
        minutes = rest // 60
        return f"{hours}h {minutes:02d}m"
    days, rest = divmod(int(round(total)), 86400)
    hours = rest // 3600
    minutes = (rest % 3600) // 60
    return f"{days}d {hours}h {minutes:02d}m"


def duration_bucket(seconds: float | None) -> str:
    """Buckets scaled for an M1-M15 trader, not a swing trader."""
    if seconds is None:
        return "Unknown"
    total = float(seconds)
    if total < 61:
        return "A. < 1 min"
    if total < 300:
        return "B. 1-5 min"
    if total < 900:
        return "C. 5-15 min"
    if total < 1800:
        return "D. 15-30 min"
    if total < 3600:
        return "E. 30-60 min"
    if total < 14400:
        return "F. 1-4 h"
    if total < 86400:
        return "G. 4-24 h"
    return "H. > 1 day"


def session_of(moment: dt.datetime | None, sessions: dict[str, list[int]]) -> str:
    if moment is None:
        return "Unknown"
    hour = moment.hour
    for name, span in sessions.items():
        start, end = int(span[0]), int(span[1])
        if start <= hour < end:
            return name
    return "Unknown"


def r_bucket(r_multiple: float | None) -> str:
    if r_multiple is None:
        return "Unknown"
    value = float(r_multiple)
    if value <= -1.5:
        return "A. <= -1.5R"
    if value <= -1.0:
        return "B. -1.5R..-1R"
    if value < -0.5:
        return "C. -1R..-0.5R"
    if value < 0:
        return "D. -0.5R..0"
    if value == 0:
        return "E. Breakeven"
    if value < 0.5:
        return "F. 0..0.5R"
    if value < 1.0:
        return "G. 0.5R..1R"
    if value < 2.0:
        return "H. 1R..2R"
    if value < 3.0:
        return "I. 2R..3R"
    return "J. >= 3R"


@dataclass
class Position:
    """A round-turn trade, assembled from its deals."""

    ticket: int
    symbol: str
    side: str  # "buy" | "sell"
    volume: float = 0.0
    open_time: float | None = None
    close_time: float | None = None
    open_price: float | None = None
    close_price: float | None = None
    sl_initial: float | None = None
    tp_initial: float | None = None
    sl_final: float | None = None
    tp_final: float | None = None
    gross_profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    fee: float = 0.0
    net_profit: float = 0.0
    magic: int = 0
    exit_reason: str = ""
    entry_comment: str = ""
    exit_comment: str = ""
    partials: int = 0
    duration_s: float | None = None
    deals: list[dict] = field(default_factory=list)

    @property
    def is_closed(self) -> bool:
        return self.close_time is not None

    @property
    def direction(self) -> int:
        return 1 if self.side == "buy" else -1

    def as_row(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "side": self.side,
            "volume": self.volume,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "open_price": self.open_price,
            "close_price": self.close_price,
            "sl_initial": self.sl_initial,
            "tp_initial": self.tp_initial,
            "sl_final": self.sl_final,
            "tp_final": self.tp_final,
            "gross_profit": self.gross_profit,
            "commission": self.commission,
            "swap": self.swap,
            "fee": self.fee,
            "net_profit": self.net_profit,
            "magic": self.magic,
            "exit_reason": self.exit_reason,
            "entry_comment": self.entry_comment,
            "exit_comment": self.exit_comment,
            "partials": self.partials,
            "duration_s": self.duration_s,
        }
