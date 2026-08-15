"""Account-level trading statistics.

Everything here is derived from the closed positions and their per-trade
metrics. The rules that matter:

  * A statistic that needs a sample says so. `sample` and `sufficient` travel
    with every block, so the UI can grey out a number computed from 6 trades
    instead of presenting it with the same confidence as one from 600.
  * Ratios that can divide by zero return None, never 0 or infinity - a profit
    factor of "infinity" reads as a perfect system when it just means no losses
    yet.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict

MIN_SAMPLE = 20  # below this, distribution stats are noise


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] * (high - position) + ordered[high] * (position - low)


def _round(value, digits=2):
    return round(value, digits) if isinstance(value, (int, float)) else value


def summarize(positions: list[dict], start_balance: float | None = None) -> dict:
    """Headline performance figures for a set of closed positions."""
    closed = [p for p in positions if p.get("close_time")]
    closed.sort(key=lambda p: p["close_time"])
    count = len(closed)
    if not count:
        return {"trades": 0, "sufficient": False}

    nets = [float(p.get("net_profit") or 0.0) for p in closed]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    scratches = [n for n in nets if n == 0]

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    net_total = sum(nets)
    win_rate = len(wins) / count if count else None
    avg_win = _mean(wins)
    avg_loss = abs(_mean(losses)) if losses else None

    expectancy = _mean(nets)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None
    payoff = (avg_win / avg_loss) if (avg_win and avg_loss) else None

    # System Quality Number - Van Tharp. Meaningless on a tiny sample, so it is
    # gated rather than shown with a confident-looking value.
    sqn = None
    net_stdev = _stdev(nets)
    if count >= MIN_SAMPLE and net_stdev:
        sqn = math.sqrt(count) * (expectancy / net_stdev)

    r_values = [
        p["metrics"].get("r_multiple")
        for p in closed
        if p.get("metrics", {}).get("r_multiple") is not None
    ]
    expectancy_r = _mean(r_values)

    balance = float(start_balance or 0.0)
    peak = balance
    max_dd = 0.0
    max_dd_pct = 0.0
    equity_curve = []
    dd_start = dd_trough = None
    longest_dd_trades = 0
    current_dd_len = 0
    for position in closed:
        balance += float(position.get("net_profit") or 0.0)
        if balance > peak:
            peak = balance
            current_dd_len = 0
        else:
            current_dd_len += 1
            longest_dd_trades = max(longest_dd_trades, current_dd_len)
        drawdown = peak - balance
        if drawdown > max_dd:
            max_dd = drawdown
            dd_trough = position.get("close_time")
        if peak > 0:
            max_dd_pct = max(max_dd_pct, drawdown / peak * 100.0)
        equity_curve.append(round(balance, 2))

    streaks = _streaks(nets)
    durations = [
        float(p.get("duration_s") or 0) for p in closed if p.get("duration_s")
    ]

    return {
        "trades": count,
        "sufficient": count >= MIN_SAMPLE,
        "wins": len(wins),
        "losses": len(losses),
        "scratches": len(scratches),
        "win_rate": _round(win_rate * 100.0, 2) if win_rate is not None else None,
        "net_profit": _round(net_total),
        "gross_profit": _round(gross_win),
        "gross_loss": _round(-gross_loss),
        "profit_factor": _round(profit_factor, 3),
        "expectancy": _round(expectancy, 3),
        "expectancy_r": _round(expectancy_r, 3),
        "payoff_ratio": _round(payoff, 3),
        "avg_win": _round(avg_win),
        "avg_loss": _round(-avg_loss) if avg_loss else None,
        "largest_win": _round(max(wins)) if wins else None,
        "largest_loss": _round(min(losses)) if losses else None,
        "median_trade": _round(_median(nets), 3),
        "stdev_trade": _round(net_stdev, 3),
        "sqn": _round(sqn, 2),
        "sqn_grade": _sqn_grade(sqn),
        "start_balance": _round(start_balance) if start_balance else None,
        "end_balance": _round(balance),
        "return_pct": _round(net_total / start_balance * 100.0, 2)
        if start_balance
        else None,
        "max_drawdown": _round(max_dd),
        "max_drawdown_pct": _round(max_dd_pct, 2),
        "recovery_factor": _round(net_total / max_dd, 3) if max_dd > 0 else None,
        "longest_dd_trades": longest_dd_trades,
        "equity_curve": equity_curve,
        "avg_duration_s": _round(_mean(durations), 1) if durations else None,
        "median_duration_s": _round(_median(durations), 1) if durations else None,
        **streaks,
        **_excursion_summary(closed),
        **_risk_summary(closed),
    }


def _sqn_grade(sqn: float | None) -> str | None:
    if sqn is None:
        return None
    if sqn < 1.6:
        return "Below average"
    if sqn < 2.0:
        return "Average"
    if sqn < 2.5:
        return "Good"
    if sqn < 3.0:
        return "Excellent"
    if sqn < 5.0:
        return "Superb"
    return "Holy grail"


def _streaks(nets: list[float]) -> dict:
    best = worst = 0
    current = 0
    best_money = worst_money = 0.0
    run_money = 0.0
    for net in nets:
        if net > 0:
            current = current + 1 if current > 0 else 1
            run_money = run_money + net if run_money > 0 else net
        elif net < 0:
            current = current - 1 if current < 0 else -1
            run_money = run_money + net if run_money < 0 else net
        else:
            current = 0
            run_money = 0.0
        best = max(best, current)
        worst = min(worst, current)
        best_money = max(best_money, run_money)
        worst_money = min(worst_money, run_money)
    return {
        "max_win_streak": best,
        "max_loss_streak": abs(worst),
        "best_run_money": _round(best_money),
        "worst_run_money": _round(worst_money),
        "current_streak": current,
    }


def _excursion_summary(closed: list[dict]) -> dict:
    def collect(key):
        return [
            p["metrics"][key]
            for p in closed
            if p.get("metrics", {}).get(key) is not None
        ]

    mae = collect("mae_money")
    mfe = collect("mfe_money")
    heat = collect("heat_pct")
    giveback = collect("giveback_money")
    winners = [p for p in closed if float(p.get("net_profit") or 0) > 0]
    losers = [p for p in closed if float(p.get("net_profit") or 0) < 0]
    winners_mae = [
        p["metrics"]["mae_money"]
        for p in winners
        if p.get("metrics", {}).get("mae_money") is not None
    ]
    losers_mfe = [
        p["metrics"]["mfe_money"]
        for p in losers
        if p.get("metrics", {}).get("mfe_money") is not None
    ]

    # Capture ratio only means something on a trade that had a gain to capture.
    # Averaging net/MFE across losers divides small favourable excursions into
    # full losses and produces a large negative number that describes nothing.
    # The aggregate below - money kept over money offered - is bounded and
    # answers the actual question.
    win_mfe_total = sum(
        p["metrics"]["mfe_money"]
        for p in winners
        if p.get("metrics", {}).get("mfe_money")
    )
    win_net_total = sum(float(p.get("net_profit") or 0) for p in winners)
    winner_captures = [
        p["metrics"]["capture_ratio"]
        for p in winners
        if p.get("metrics", {}).get("capture_ratio") is not None
    ]

    # "Was in profit before it lost" needs a floor. Measured on ticks, nearly
    # every trade shows a fractionally positive MFE, so counting anything above
    # zero turns noise into a finding. A loser only counts as given back if it
    # reached a meaningful fraction of its own planned risk.
    give_back_threshold_r = 0.5
    reversed_winners = [
        p
        for p in losers
        if (p.get("metrics", {}).get("mfe_r") or 0) >= give_back_threshold_r
    ]

    return {
        "avg_mae": _round(_mean(mae)),
        "avg_mfe": _round(_mean(mfe)),
        "median_heat_pct": _round(_median(heat), 1),
        "p90_heat_pct": _round(_percentile(heat, 0.9), 1),
        "capture_of_winners": _round(win_net_total / win_mfe_total, 3)
        if win_mfe_total > 0
        else None,
        "median_winner_capture": _round(_median(winner_captures), 3),
        "total_giveback": _round(sum(giveback)) if giveback else None,
        # How much heat a winning trade typically takes - the number that says
        # whether stops are wider than they need to be.
        "avg_winner_mae": _round(_mean(winners_mae)),
        # Money that was on the table in trades that ended red.
        "avg_loser_mfe": _round(_mean(losers_mfe)),
        "reversed_winners": len(reversed_winners),
        "reversed_winners_pct": _round(len(reversed_winners) / len(losers) * 100.0, 1)
        if losers
        else None,
        "reversed_winners_threshold_r": give_back_threshold_r,
        "excursion_sample": len(mae),
    }


def _risk_summary(closed: list[dict]) -> dict:
    risks = [
        p["metrics"]["risk_money"]
        for p in closed
        if p.get("metrics", {}).get("risk_money")
    ]
    risk_pcts = [
        p["metrics"]["risk_pct_of_balance"]
        for p in closed
        if p.get("metrics", {}).get("risk_pct_of_balance")
    ]
    planned = [
        p["metrics"]["planned_rr"]
        for p in closed
        if p.get("metrics", {}).get("planned_rr")
    ]
    return {
        "avg_risk_money": _round(_mean(risks)),
        "median_risk_money": _round(_median(risks)),
        "risk_consistency": _round(_stdev(risks) / _mean(risks), 3)
        if risks and _mean(risks)
        else None,
        "avg_risk_pct": _round(_mean(risk_pcts), 3),
        "max_risk_pct": _round(max(risk_pcts), 3) if risk_pcts else None,
        "avg_planned_rr": _round(_mean(planned), 3),
    }


def group_by(positions: list[dict], key_fn, min_sample: int = 1) -> list[dict]:
    """Aggregate closed trades under an arbitrary key."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for position in positions:
        if not position.get("close_time"):
            continue
        key = key_fn(position)
        if key is None:
            continue
        buckets[str(key)].append(position)

    rows = []
    total_net = sum(float(p.get("net_profit") or 0) for p in positions
                    if p.get("close_time"))
    for key, group in buckets.items():
        if len(group) < min_sample:
            continue
        nets = [float(p.get("net_profit") or 0) for p in group]
        wins = [n for n in nets if n > 0]
        losses = [n for n in nets if n < 0]
        gross_loss = abs(sum(losses))
        r_values = [
            p["metrics"].get("r_multiple")
            for p in group
            if p.get("metrics", {}).get("r_multiple") is not None
        ]
        durations = [float(p.get("duration_s") or 0) for p in group if p.get("duration_s")]
        rows.append(
            {
                "key": key,
                "trades": len(group),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": _round(len(wins) / len(group) * 100.0, 1),
                "net": _round(sum(nets)),
                "gross_win": _round(sum(wins)),
                "gross_loss": _round(-gross_loss),
                "profit_factor": _round(sum(wins) / gross_loss, 3)
                if gross_loss > 0
                else None,
                "expectancy": _round(_mean(nets), 3),
                "expectancy_r": _round(_mean(r_values), 3),
                "avg_duration_s": _round(_mean(durations), 1),
                "share_of_net": _round(sum(nets) / total_net * 100.0, 1)
                if total_net
                else None,
                "best": _round(max(nets)),
                "worst": _round(min(nets)),
            }
        )
    rows.sort(key=lambda r: r["net"], reverse=True)
    return rows


def monte_carlo(positions: list[dict], runs: int = 5000, horizon: int = 100,
                start_balance: float | None = None, seed: int = 12345) -> dict:
    """Resample the trade distribution to bound future drawdown.

    This shuffles the trader's *own* results rather than assuming a normal
    distribution, so fat tails in the real record stay in the simulation.
    Requires a meaningful sample; below that it declines to answer.
    """
    nets = [
        float(p.get("net_profit") or 0.0) for p in positions if p.get("close_time")
    ]
    if len(nets) < MIN_SAMPLE:
        return {
            "available": False,
            "reason": f"Needs at least {MIN_SAMPLE} closed trades, have {len(nets)}.",
            "sample": len(nets),
        }

    rng = random.Random(seed)
    balance = float(start_balance or 0.0)
    finals: list[float] = []
    drawdowns: list[float] = []
    ruin_count = 0
    ruin_threshold = balance * 0.5 if balance else None

    for _ in range(runs):
        equity = balance
        peak = equity
        worst = 0.0
        busted = False
        for _ in range(horizon):
            equity += rng.choice(nets)
            peak = max(peak, equity)
            worst = max(worst, peak - equity)
            if ruin_threshold is not None and equity <= ruin_threshold:
                busted = True
        finals.append(equity)
        drawdowns.append(worst)
        if busted:
            ruin_count += 1

    return {
        "available": True,
        "runs": runs,
        "horizon": horizon,
        "sample": len(nets),
        "median_final": _round(_median(finals)),
        "p05_final": _round(_percentile(finals, 0.05)),
        "p95_final": _round(_percentile(finals, 0.95)),
        "median_drawdown": _round(_median(drawdowns)),
        "p95_drawdown": _round(_percentile(drawdowns, 0.95)),
        "worst_drawdown": _round(max(drawdowns)),
        "risk_of_50pct_loss": _round(ruin_count / runs * 100.0, 2)
        if ruin_threshold
        else None,
        "profitable_pct": _round(
            len([f for f in finals if f > balance]) / runs * 100.0, 1
        ),
    }
