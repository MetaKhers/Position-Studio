"""Turn raw MT5 deals and orders into round-turn positions.

The grouping key is `position_id`, which every deal of a trade shares. What
makes this fiddly is that the useful information is spread across three places:

  * volumes, prices and money live on the deals;
  * the *initial* stop and target live on the opening order, and nowhere else -
    the statement export only ever shows the final ones;
  * the price a stop or target actually triggered at is quoted in the closing
    deal's comment ("[sl 52201.74]").

Reading all three is what lets the app tell initial risk apart from managed
risk, which is the difference between a real R-multiple and a guess.
"""

from __future__ import annotations

import datetime as dt

from . import db, model
from .model import Position

_TYPE_BUY = 0
_TYPE_SELL = 1
# Deal types above 1 are balance operations: deposits, credits, corrections.
_BALANCE_TYPES = {2, 3, 4, 5, 6, 7, 8}

_ENTRY_IN = 0
_ENTRY_OUT = 1
_ENTRY_INOUT = 2
_ENTRY_OUT_BY = 3

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


def _weighted(pairs: list[tuple[float, float]]) -> float | None:
    """Volume-weighted average price."""
    total_volume = sum(volume for volume, _ in pairs)
    if total_volume <= 0:
        return pairs[0][1] if pairs else None
    return sum(volume * price for volume, price in pairs) / total_volume


def start_balance(deals: list[dict]) -> float | None:
    """The account's opening balance, recoverable from the balance deals.

    MT5 records the initial funding as a balance-type deal, often commented
    "Start balance". Summing balance operations that precede the first trade
    gives the starting equity without asking the user for it.
    """
    trade_times = [
        d["time_msc"] for d in deals if d.get("type") in (_TYPE_BUY, _TYPE_SELL)
    ]
    first_trade = min(trade_times) if trade_times else None
    total = 0.0
    seen = False
    for deal in deals:
        if deal.get("type") not in _BALANCE_TYPES:
            continue
        if first_trade is not None and deal["time_msc"] > first_trade:
            continue
        total += float(deal.get("profit") or 0.0)
        seen = True
    return total if seen else None


def build_positions(deals: list[dict], orders: list[dict]) -> list[Position]:
    """Group deals into positions and enrich them from the order history."""
    opening_order: dict[int, dict] = {}
    closing_orders: dict[int, list[dict]] = {}
    for order in orders:
        pid = int(order.get("position_id") or 0)
        if not pid:
            continue
        # An opening order's price_open is 0 for a market fill; the order that
        # closes on a stop or target carries the trigger price instead.
        if order.get("reason") in (4, 5) or (order.get("comment") or "").startswith("["):
            closing_orders.setdefault(pid, []).append(order)
        elif pid not in opening_order or int(order["ticket"]) == pid:
            opening_order[pid] = order

    grouped: dict[int, list[dict]] = {}
    for deal in deals:
        if deal.get("type") not in (_TYPE_BUY, _TYPE_SELL):
            continue
        pid = int(deal.get("position_id") or 0)
        if not pid:
            continue
        grouped.setdefault(pid, []).append(deal)

    positions: list[Position] = []
    for pid, deal_list in grouped.items():
        deal_list.sort(key=lambda d: (d["time_msc"], d["ticket"]))
        entries = [d for d in deal_list if d.get("entry") in (_ENTRY_IN, _ENTRY_INOUT)]
        exits = [d for d in deal_list if d.get("entry") in (_ENTRY_OUT, _ENTRY_OUT_BY)]
        if not entries:
            # A position opened before the requested window - skip rather than
            # invent an entry price.
            continue

        first = entries[0]
        side = "buy" if first["type"] == _TYPE_BUY else "sell"
        position = Position(
            ticket=pid,
            symbol=first.get("symbol") or "",
            side=side,
            volume=round(sum(float(d.get("volume") or 0) for d in entries), 4),
            open_time=first["time_msc"] / 1000.0,
            open_price=_weighted(
                [(float(d.get("volume") or 0), float(d.get("price") or 0)) for d in entries]
            ),
            magic=int(first.get("magic") or 0),
            entry_comment=(first.get("comment") or "").strip(),
            deals=deal_list,
        )

        for deal in deal_list:
            position.gross_profit += float(deal.get("profit") or 0.0)
            position.commission += float(deal.get("commission") or 0.0)
            position.swap += float(deal.get("swap") or 0.0)
            position.fee += float(deal.get("fee") or 0.0)
        position.net_profit = round(
            position.gross_profit + position.commission + position.swap + position.fee, 6
        )

        if exits:
            last = exits[-1]
            position.close_time = last["time_msc"] / 1000.0
            position.close_price = _weighted(
                [(float(d.get("volume") or 0), float(d.get("price") or 0)) for d in exits]
            )
            position.exit_reason = DEAL_REASONS.get(
                int(last.get("reason") or 0), "Unknown"
            )
            position.exit_comment = (last.get("comment") or "").strip()
            position.partials = max(0, len(exits) - 1)
            position.duration_s = position.close_time - position.open_time
            # A stop or target quotes its trigger price in the comment, which
            # is the position's stop as managed at the moment it closed.
            for kind, attr in (("sl", "sl_final"), ("tp", "tp_final")):
                price = model.parse_bracket_price(position.exit_comment, kind)
                if price:
                    setattr(position, attr, price)
            for order in closing_orders.get(pid, []):
                trigger = float(order.get("price_open") or 0)
                if not trigger:
                    continue
                if order.get("reason") == 4 and position.sl_final is None:
                    position.sl_final = trigger
                elif order.get("reason") == 5 and position.tp_final is None:
                    position.tp_final = trigger

        opener = opening_order.get(pid)
        if opener:
            sl = float(opener.get("sl") or 0)
            tp = float(opener.get("tp") or 0)
            position.sl_initial = sl or None
            position.tp_initial = tp or None
        # With no recorded modification, the stop it closed on was the stop it
        # opened with.
        if position.sl_final is None:
            position.sl_final = position.sl_initial
        if position.tp_final is None:
            position.tp_final = position.tp_initial

        positions.append(position)

    positions.sort(key=lambda p: (p.open_time or 0, p.ticket))
    return positions


def sync_account(session, terminal_id: int, since: dt.datetime | None = None,
                 until: dt.datetime | None = None, progress=None) -> dict:
    """Pull history from a live session into the database.

    Returns counts so the UI can say what actually changed rather than just
    "done". Positions whose numbers moved have their analysis stamps cleared
    by the store layer, so they come back round as pending work.
    """
    account = session.account_info()
    account_id = db.upsert_account(terminal_id, account)

    if since is None:
        # MT5 accepts a wide range cheaply; going back to 2000 covers any
        # account and lets the app find "old history that hasn't been analyzed".
        since = dt.datetime(2000, 1, 1)
    if until is None:
        until = dt.datetime.now() + dt.timedelta(days=2)

    if progress:
        progress("Reading deal history", 0.05)
    deals = session.deals(since, until)
    if progress:
        progress("Reading order history", 0.25)
    orders = session.orders(since, until)

    db.store_deals(account_id, deals)
    opening_balance = start_balance(deals)

    if progress:
        progress("Grouping deals into positions", 0.45)
    positions = build_positions(deals, orders)

    new_count = 0
    for index, position in enumerate(positions):
        _, is_new = db.upsert_position(account_id, position.as_row())
        if is_new:
            new_count += 1
        if progress and index % 25 == 0 and positions:
            progress(
                f"Storing positions ({index + 1}/{len(positions)})",
                0.45 + 0.5 * (index + 1) / len(positions),
            )

    times = [p.open_time for p in positions if p.open_time]
    closes = [p.close_time for p in positions if p.close_time]
    db.set_account_range(
        account_id,
        min(times) if times else None,
        max(closes) if closes else None,
        opening_balance,
    )

    if progress:
        progress("History synced", 1.0)
    return {
        "account_id": account_id,
        "login": account["login"],
        "deals": len(deals),
        "orders": len(orders),
        "positions": len(positions),
        "new_positions": new_count,
        "open_positions": len([p for p in positions if not p.is_closed]),
        "pending": db.pending_count(account_id),
        "start_balance": opening_balance,
    }
