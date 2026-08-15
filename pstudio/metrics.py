"""Per-position excursion metrics: MAE, MFE, efficiency, R-multiples.

MAE and MFE are the two numbers that separate "I won" from "I was right".
They need intra-trade prices, and there are two sources with different
honesty profiles:

  * **Ticks** - exact. Every quote the broker published while the trade was
    open. For a buy the adverse side is the *bid* (what you could exit at), so
    that is what is measured, rather than pretending mid-price was available.
  * **M1 bars** - approximate. Highs and lows only, so an excursion that
    happened inside a minute is captured but its timing is rounded to that
    minute. Cheap enough to run over years of history.

Which one was used is recorded on every position as `excursion_source`, because
a metric whose precision is unknown is not a metric.

Money conversion goes through `order_calc_profit`, the terminal's own
calculator, so MAE in dollars matches what the account would actually have
shown. It reproduces recorded deal profits to the cent on this account.
"""

from __future__ import annotations

import datetime as dt
import math

from . import model
from .model import format_duration, duration_bucket, r_bucket, session_of

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    try:
        return float(numerator) / float(denominator)
    except (TypeError, ZeroDivisionError):
        return None


def _excursion_from_ticks(ticks: list[dict], side: str) -> dict | None:
    """Worst and best exit price achievable while the trade was open."""
    if not ticks:
        return None
    best_price = worst_price = None
    best_at = worst_at = None
    for tick in ticks:
        # Exit price for the direction held: sell at bid, buy back at ask.
        price = tick["bid"] if side == "buy" else tick["ask"]
        if price <= 0:
            price = tick["last"] or tick["bid"] or tick["ask"]
        if price <= 0:
            continue
        if best_price is None:
            best_price = worst_price = price
            best_at = worst_at = tick["time_msc"]
            continue
        if side == "buy":
            if price > best_price:
                best_price, best_at = price, tick["time_msc"]
            if price < worst_price:
                worst_price, worst_at = price, tick["time_msc"]
        else:
            if price < best_price:
                best_price, best_at = price, tick["time_msc"]
            if price > worst_price:
                worst_price, worst_at = price, tick["time_msc"]
    if best_price is None:
        return None
    return {
        "best_price": best_price,
        "worst_price": worst_price,
        "best_at": best_at / 1000.0 if best_at else None,
        "worst_at": worst_at / 1000.0 if worst_at else None,
        "high": max(best_price, worst_price),
        "low": min(best_price, worst_price),
        "source": "ticks",
        "samples": len(ticks),
    }


def _excursion_from_bars(bars: list[dict], side: str) -> dict | None:
    if not bars:
        return None
    high_bar = max(bars, key=lambda b: b["high"])
    low_bar = min(bars, key=lambda b: b["low"])
    if side == "buy":
        best_price, best_at = high_bar["high"], high_bar["time"]
        worst_price, worst_at = low_bar["low"], low_bar["time"]
    else:
        best_price, best_at = low_bar["low"], low_bar["time"]
        worst_price, worst_at = high_bar["high"], high_bar["time"]
    return {
        "best_price": best_price,
        "worst_price": worst_price,
        "best_at": float(best_at),
        "worst_at": float(worst_at),
        "high": high_bar["high"],
        "low": low_bar["low"],
        "source": "bars_m1",
        "samples": len(bars),
    }


def _efficiency(side: str, entry: float, exit_price: float, high: float,
                low: float) -> dict:
    """Sweeney efficiency ratios over the trade's own price range.

    Entry efficiency asks how close the entry was to the best available price,
    exit efficiency how close the exit was to the best available exit, and
    total efficiency how much of the range the trade actually banked.
    """
    span = high - low
    if span <= 0:
        return {"entry_efficiency": None, "exit_efficiency": None, "total_efficiency": None}
    if side == "buy":
        entry_eff = (high - entry) / span
        exit_eff = (exit_price - low) / span
        total_eff = (exit_price - entry) / span
    else:
        entry_eff = (entry - low) / span
        exit_eff = (high - exit_price) / span
        total_eff = (entry - exit_price) / span
    return {
        "entry_efficiency": round(entry_eff, 4),
        "exit_efficiency": round(exit_eff, 4),
        "total_efficiency": round(total_eff, 4),
    }


class MetricsEngine:
    """Computes position metrics against a live MT5 session."""

    def __init__(self, session, config: dict):
        self.session = session
        self.analysis = config.get("analysis", {})
        self.sessions = self.analysis.get("sessions", {})
        self._symbol_cache: dict[str, dict | None] = {}

    def symbol(self, name: str) -> dict | None:
        if name not in self._symbol_cache:
            self._symbol_cache[name] = self.session.symbol_info(name)
        return self._symbol_cache[name]

    def _money(self, side: str, symbol: str, volume: float, entry: float,
               price: float, value_per_price: float | None) -> float | None:
        """Convert a price level into account currency at this trade's size."""
        if price is None or entry is None:
            return None
        value = None
        if self.session is not None:
            try:
                value = self.session.calc_profit(side, symbol, volume, entry, price)
            except Exception:
                value = None
        if value is None and value_per_price is not None:
            direction = 1 if side == "buy" else -1
            value = (price - entry) * direction * value_per_price
        return round(value, 2) if value is not None else None

    def _value_per_price(self, position: dict) -> float | None:
        """Account currency per 1.0 of price movement, derived from the trade.

        Taken from the trade's own realized result, so it stays correct for
        cross-currency instruments without needing conversion rates.
        """
        entry = position.get("open_price")
        exit_price = position.get("close_price")
        gross = position.get("gross_profit")
        if None in (entry, exit_price, gross):
            return None
        direction = 1 if position["side"] == "buy" else -1
        move = (exit_price - entry) * direction
        if abs(move) < 1e-12:
            return None
        return abs(gross / move) * (1 if gross * move >= 0 else -1)

    def _intrabar(self, position: dict) -> dict | None:
        """Fetch the intra-trade price path with the configured precision."""
        symbol = position["symbol"]
        side = position["side"]
        open_time = position.get("open_time")
        close_time = position.get("close_time")
        if not open_time or not close_time:
            return None
        start = model.broker_dt(open_time)
        end = model.broker_dt(close_time)
        if start is None or end is None:
            return None

        mode = str(self.analysis.get("excursion_source", "auto")).lower()
        minutes = (close_time - open_time) / 60.0
        limit = float(self.analysis.get("tick_trade_max_minutes", 240))
        use_ticks = mode == "ticks" or (mode == "auto" and minutes <= limit)

        if use_ticks:
            # Pad by a second either side so the opening and closing quotes are
            # inside the window.
            ticks = self.session.ticks(
                symbol, start - dt.timedelta(seconds=1), end + dt.timedelta(seconds=1)
            )
            result = _excursion_from_ticks(ticks, side)
            if result:
                return result

        bars = self.session.rates_range(
            symbol,
            "M1",
            start - dt.timedelta(minutes=1),
            end + dt.timedelta(minutes=1),
        )
        return _excursion_from_bars(bars, side)

    def compute(self, position: dict) -> dict:
        """Full metric set for one position."""
        side = position["side"]
        symbol = position["symbol"]
        volume = float(position.get("volume") or 0)
        entry = position.get("open_price")
        exit_price = position.get("close_price")
        net = float(position.get("net_profit") or 0.0)
        direction = 1 if side == "buy" else -1
        info = self.symbol(symbol) or {}
        point = float(info.get("point") or 0) or None
        digits = int(info.get("digits") or 2)
        value_per_price = self._value_per_price(position)

        out: dict = {
            "digits": digits,
            "point": point,
            "value_per_price": round(value_per_price, 6) if value_per_price else None,
            "contract_size": info.get("contract_size"),
            "symbol_description": info.get("description"),
        }

        open_dt = model.broker_dt(position.get("open_time"))
        close_dt = model.broker_dt(position.get("close_time"))
        duration = position.get("duration_s")
        out.update(
            {
                "duration_s": duration,
                "duration_label": format_duration(duration),
                "duration_minutes": round(duration / 60.0, 3) if duration else None,
                "duration_bucket": duration_bucket(duration),
                "open_date": open_dt.strftime("%Y-%m-%d") if open_dt else None,
                "open_hhmm": open_dt.strftime("%H:%M:%S") if open_dt else None,
                "close_date": close_dt.strftime("%Y-%m-%d") if close_dt else None,
                "close_hhmm": close_dt.strftime("%H:%M:%S") if close_dt else None,
                "weekday": _WEEKDAYS[open_dt.weekday()] if open_dt else None,
                "weekday_index": open_dt.weekday() + 1 if open_dt else None,
                "hour": open_dt.hour if open_dt else None,
                "session": session_of(open_dt, self.sessions),
                "week": open_dt.strftime("%G-W%V") if open_dt else None,
                "month": open_dt.strftime("%Y-%m") if open_dt else None,
            }
        )

        # -- planned risk and reward -----------------------------------------
        sl = position.get("sl_initial")
        tp = position.get("tp_initial")
        sl_distance = abs(entry - sl) if (entry and sl) else None
        tp_distance = abs(tp - entry) if (entry and tp) else None
        risk_money = None
        if sl_distance:
            value = self._money(side, symbol, volume, entry, sl, value_per_price)
            risk_money = abs(value) if value is not None else None
        reward_money = None
        if tp_distance:
            value = self._money(side, symbol, volume, entry, tp, value_per_price)
            reward_money = abs(value) if value is not None else None

        out.update(
            {
                "sl_distance_price": round(sl_distance, digits) if sl_distance else None,
                "tp_distance_price": round(tp_distance, digits) if tp_distance else None,
                "sl_distance_points": round(sl_distance / point, 1)
                if (sl_distance and point)
                else None,
                "tp_distance_points": round(tp_distance / point, 1)
                if (tp_distance and point)
                else None,
                "risk_money": risk_money,
                "reward_money": reward_money,
                "planned_rr": round(_safe_div(reward_money, risk_money), 3)
                if risk_money and reward_money
                else None,
                "r_multiple": round(net / risk_money, 3) if risk_money else None,
            }
        )
        out["r_bucket"] = r_bucket(out["r_multiple"])

        # -- realized move ---------------------------------------------------
        if entry and exit_price:
            move = (exit_price - entry) * direction
            out["result_price"] = round(move, digits)
            out["result_points"] = round(move / point, 1) if point else None
            out["realized_rr"] = (
                round(abs(move) / sl_distance, 3) if sl_distance else None
            )
        out["net_profit"] = round(net, 2)
        out["gross_profit"] = round(float(position.get("gross_profit") or 0), 2)
        out["costs"] = round(
            float(position.get("commission") or 0)
            + float(position.get("swap") or 0)
            + float(position.get("fee") or 0),
            2,
        )
        out["outcome"] = "Win" if net > 0 else ("Loss" if net < 0 else "Breakeven")
        out["is_win"] = 1 if net > 0 else 0

        # -- stop management --------------------------------------------------
        sl_final = position.get("sl_final")
        if sl and sl_final:
            moved = abs(sl_final - sl) > (point or 1e-9)
            out["sl_moved"] = 1 if moved else 0
            if moved:
                # Toward entry = risk reduced; away = risk increased.
                out["sl_move_points"] = (
                    round((abs(entry - sl) - abs(entry - sl_final)) / point, 1)
                    if point and entry
                    else None
                )
                out["sl_direction"] = (
                    "Tightened" if abs(entry - sl_final) < abs(entry - sl) else "Widened"
                )
                if entry and abs(sl_final - entry) <= (point or 0) * 2:
                    out["sl_direction"] = "Breakeven"
            else:
                out["sl_move_points"] = 0.0
                out["sl_direction"] = "Unchanged"

        # -- excursions -------------------------------------------------------
        path = self._intrabar(position)
        if path:
            mfe_money = self._money(
                side, symbol, volume, entry, path["best_price"], value_per_price
            )
            mae_money = self._money(
                side, symbol, volume, entry, path["worst_price"], value_per_price
            )
            # An excursion cannot help you in the wrong direction: clamp so a
            # trade that never went green reports MFE 0, not a positive number.
            if mfe_money is not None:
                mfe_money = max(0.0, mfe_money)
            if mae_money is not None:
                mae_money = min(0.0, mae_money)
            mfe_price_move = (path["best_price"] - entry) * direction if entry else None
            mae_price_move = (path["worst_price"] - entry) * direction if entry else None

            out.update(
                {
                    "excursion_source": path["source"],
                    "excursion_samples": path["samples"],
                    "mfe_price": round(path["best_price"], digits),
                    "mae_price": round(path["worst_price"], digits),
                    "range_high": round(path["high"], digits),
                    "range_low": round(path["low"], digits),
                    "mfe_money": mfe_money,
                    "mae_money": mae_money,
                    "mfe_points": round(mfe_price_move / point, 1)
                    if (mfe_price_move is not None and point)
                    else None,
                    "mae_points": round(mae_price_move / point, 1)
                    if (mae_price_move is not None and point)
                    else None,
                    "mfe_r": round(_safe_div(mfe_money, risk_money), 3)
                    if risk_money and mfe_money is not None
                    else None,
                    "mae_r": round(_safe_div(mae_money, risk_money), 3)
                    if risk_money and mae_money is not None
                    else None,
                }
            )
            open_time = position.get("open_time") or 0
            if path.get("best_at"):
                out["time_to_mfe_s"] = round(max(0.0, path["best_at"] - open_time), 1)
                out["time_to_mfe_label"] = format_duration(out["time_to_mfe_s"])
            if path.get("worst_at"):
                out["time_to_mae_s"] = round(max(0.0, path["worst_at"] - open_time), 1)
                out["time_to_mae_label"] = format_duration(out["time_to_mae_s"])
            if path.get("best_at") and path.get("worst_at"):
                out["mfe_before_mae"] = 1 if path["best_at"] <= path["worst_at"] else 0

            if entry and exit_price:
                out.update(_efficiency(side, entry, exit_price, path["high"], path["low"]))

            # How much of the best unrealized gain was kept.
            if mfe_money and mfe_money > 0:
                out["capture_ratio"] = round(net / mfe_money, 3)
                out["giveback_money"] = round(mfe_money - net, 2)
            # How much of the planned stop the trade actually consumed.
            if risk_money and mae_money is not None:
                out["heat_pct"] = round(abs(mae_money) / risk_money * 100.0, 1)
            # Did price come within a whisker of the stop without hitting it?
            if sl and path["worst_price"] and point:
                gap = abs(path["worst_price"] - sl) / point
                out["stop_proximity_points"] = round(gap, 1)
                out["near_miss"] = (
                    1
                    if (out.get("exit_reason") != "Stop Loss" and gap <= 5)
                    else 0
                )
            if mae_money is not None and mfe_money is not None and mae_money != 0:
                out["mfe_mae_ratio"] = round(abs(mfe_money / mae_money), 3)
        else:
            out["excursion_source"] = "unavailable"

        return out


def enrich_series(positions: list[dict], start_balance: float | None = None,
                  config: dict | None = None) -> list[dict]:
    """Add running balance, drawdown and streak context in trade order.

    These are sequence-dependent, so they are computed once over the closed
    trades sorted by close time rather than per position.
    """
    config = config or {}
    closed = sorted(
        [p for p in positions if p.get("close_time")], key=lambda p: p["close_time"]
    )
    balance = float(start_balance or 0.0)
    peak = balance
    max_dd = 0.0
    max_dd_pct = 0.0
    streak = 0
    for index, position in enumerate(closed, start=1):
        metrics = position.setdefault("metrics", {})
        net = float(position.get("net_profit") or 0.0)
        before = balance
        balance += net
        peak = max(peak, balance)
        drawdown = peak - balance
        max_dd = max(max_dd, drawdown)
        dd_pct = (drawdown / peak * 100.0) if peak > 0 else 0.0
        max_dd_pct = max(max_dd_pct, dd_pct)

        won = net > 0
        if net == 0:
            streak = 0
        elif streak == 0 or (streak > 0) != won:
            streak = 1 if won else -1
        else:
            streak += 1 if won else -1

        metrics.update(
            {
                "trade_no": index,
                "balance_before": round(before, 2),
                "balance_after": round(balance, 2),
                "equity_peak": round(peak, 2),
                "drawdown_money": round(drawdown, 2),
                "drawdown_pct": round(dd_pct, 3),
                "streak": streak,
                "risk_pct_of_balance": round(
                    metrics.get("risk_money", 0) / before * 100.0, 3
                )
                if metrics.get("risk_money") and before > 0
                else None,
                "return_pct_of_balance": round(net / before * 100.0, 3)
                if before > 0
                else None,
            }
        )
    for position in positions:
        metrics = position.setdefault("metrics", {})
        metrics.setdefault("max_drawdown_money", round(max_dd, 2))
        metrics.setdefault("max_drawdown_pct", round(max_dd_pct, 3))
    return positions


def analyze_account(account_id: int, only_pending: bool = True,
                    limit: int | None = None, progress=None,
                    config: dict | None = None) -> dict:
    """Compute and store metrics for an account's positions.

    Mirrors `capture.capture_account`: one terminal session is held across the
    whole batch, because opening one costs seconds and computing a position
    costs milliseconds. Ticks for a single trade are the expensive part, so the
    default is to touch only positions that have never been analyzed.
    """
    from . import db, mt5conn, settings

    config = config or settings.load()
    account = db.get_account(account_id)
    if not account:
        raise ValueError(f"no such account: {account_id}")
    terminal = db.one(
        "SELECT * FROM terminals WHERE id=?", (account["terminal_id"],)
    )
    if terminal is None:
        raise ValueError("account has no terminal on record")
    exe_path = dict(terminal)["exe_path"]

    positions = db.position_rows(account_id)
    if only_pending:
        positions = [p for p in positions if not p.get("analyzed_at")]
    # Oldest first, so a part-finished run leaves a contiguous analyzed history
    # rather than holes.
    positions.sort(key=lambda p: p.get("open_time") or 0)
    if limit:
        positions = positions[: int(limit)]

    run_id = db.start_run("analyze", account_id, f"{len(positions)} position(s)")
    done = failed = 0
    try:
        if not positions:
            db.finish_run(run_id, "ok", {"positions": 0})
            return {"positions": 0, "analyzed": 0, "failures": 0}

        with mt5conn.connect(exe_path) as session:
            engine = MetricsEngine(session, config)
            for index, position in enumerate(positions, start=1):
                try:
                    computed = engine.compute(position)
                except Exception as exc:  # pragma: no cover - per-trade guard
                    # One unreadable trade must not abandon the other 500.
                    failed += 1
                    computed = {"error": str(exc)}
                else:
                    done += 1
                db.save_metrics(position["id"], computed)
                if progress:
                    progress(
                        {
                            "done": index,
                            "total": len(positions),
                            "ticket": position.get("ticket"),
                            "symbol": position.get("symbol"),
                        }
                    )
    except Exception as exc:
        db.finish_run(run_id, "error", {"analyzed": done}, str(exc))
        raise
    db.finish_run(
        run_id, "ok", {"positions": len(positions), "analyzed": done,
                       "failures": failed}
    )
    return {"positions": len(positions), "analyzed": done, "failures": failed}
