"""Per-position chart capture.

One position produces a set of shots across timeframes, all of them landing in
a single folder for that position - which is what the brief asked for, and also
what makes a trade reviewable: you open one folder and see the same moment at
five zoom levels.

Decisions worth knowing about:

  * Bar windows are sized so the moment being captured sits at a fixed fraction
    across the frame, not dead centre. An entry pinned mid-frame wastes half the
    image on history the trader has already seen; anchoring at ~0.62 leaves room
    for what happened next.
  * The count of bars is held inside the configured 80-120 band. When the market
    was closed and the terminal simply has no bars, the shot is still rendered
    from whatever came back rather than skipped - a thin chart is information.
  * A "journey" shot is added when entry and exit both fit in one frame at the
    lowest captured timeframe. It is the single most useful image for review.
  * Nothing here mutates the terminal. No template switching, no objects, no
    chart windows. The terminal supplies OHLC and nothing else.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from pathlib import Path

from . import db, model, mt5conn, paths, render, settings

_SEQ = {"H4": 10, "H1": 20, "M15": 30, "M5": 40, "M1": 50}
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _safe(text: str) -> str:
    """Windows-safe path fragment, trimmed of the characters that break paths."""
    cleaned = _UNSAFE.sub("-", str(text or "")).strip(" .")
    return cleaned or "unknown"


def _outcome(position: dict) -> str:
    net = float(position.get("net_profit") or 0.0)
    if net > 0:
        return "win"
    if net < 0:
        return "loss"
    return "scratch"


def _tokens(position: dict, account: dict, timeframe: str, event: str,
            conf: dict) -> dict:
    """Values available to the folder and file name templates."""
    opened = model.broker_dt(position.get("open_time"))
    closed = model.broker_dt(position.get("close_time"))
    stamp = closed if (event == "close" and closed) else opened
    naming = conf.get("naming", settings.DEFAULTS["naming"])
    date_fmt = naming.get("date_format", "%Y-%m-%d")
    time_fmt = naming.get("time_format", "%H%M%S")
    return {
        "login": account.get("login", ""),
        "ticket": position.get("ticket", ""),
        "symbol": _safe(position.get("symbol", "")),
        "side": str(position.get("side", "")).lower(),
        "outcome": _outcome(position),
        "tf": timeframe.upper(),
        "event": event,
        "seq": f"{_SEQ.get(timeframe.upper(), 90):02d}",
        "date": stamp.strftime(date_fmt) if stamp else "undated",
        "time": stamp.strftime(time_fmt) if stamp else "000000",
        "open_date": opened.strftime(date_fmt) if opened else "undated",
        "open_time": opened.strftime(time_fmt) if opened else "000000",
        "year": stamp.strftime("%Y") if stamp else "0000",
        "month": stamp.strftime("%m") if stamp else "00",
    }


def position_folder(position: dict, account: dict, conf: dict | None = None) -> Path:
    """Folder holding every shot of one position."""
    conf = conf or settings.load()
    naming = conf.get("naming", settings.DEFAULTS["naming"])
    tokens = _tokens(position, account, "M1", "open", conf)
    template = naming.get("folder") or settings.DEFAULTS["naming"]["folder"]
    try:
        relative = template.format(**tokens)
    except KeyError:
        relative = settings.DEFAULTS["naming"]["folder"].format(**tokens)
    parts = [_safe(part) for part in Path(relative).parts]
    account_dir = _safe(f"{account.get('login', 'account')}")
    return paths.charts_dir().joinpath(account_dir, *parts)


def shot_name(position: dict, account: dict, timeframe: str, event: str,
              conf: dict) -> str:
    naming = conf.get("naming", settings.DEFAULTS["naming"])
    tokens = _tokens(position, account, timeframe, event, conf)
    template = naming.get("file") or settings.DEFAULTS["naming"]["file"]
    try:
        stem = template.format(**tokens)
    except KeyError:
        stem = settings.DEFAULTS["naming"]["file"].format(**tokens)
    suffix = str(conf.get("capture", {}).get("image_format", "png")).lower()
    if suffix not in ("png", "jpg", "jpeg", "webp"):
        suffix = "png"
    return f"{_safe(stem)}.{suffix}"


def _window(timeframe: str, position: dict, event: str, conf: dict) -> tuple[int, int]:
    """(bars before, bars after) for a shot, inside the configured band.

    The trade's own span is honoured where it fits: on a close shot there is no
    point showing 70 bars of pre-entry history if the entry then falls off the
    left edge.
    """
    capture = conf.get("capture", settings.DEFAULTS["capture"])
    target = int(capture.get("candles_target", 100))
    low = int(capture.get("candles_min", 80))
    high = int(capture.get("candles_max", 120))
    target = max(low, min(high, target))
    anchor = float(
        capture.get("entry_anchor", 0.62) if event == "open"
        else capture.get("exit_anchor", 0.68)
    )
    anchor = min(0.92, max(0.08, anchor))

    before = int(round((target - 1) * anchor))
    after = target - 1 - before

    step = render._TF_SECONDS.get(timeframe.upper(), 60)
    open_time = position.get("open_time")
    close_time = position.get("close_time")
    if event == "close" and open_time and close_time:
        # Reach back far enough to keep the entry in frame when it fits.
        span_bars = int(math.ceil((close_time - open_time) / step)) + 2
        if span_bars <= high - 1:
            before = max(before, min(span_bars + 6, high - 1 - after))
            total = before + after + 1
            if total > high:
                after = max(4, high - 1 - before)
    return max(1, before), max(0, after)


def _journey_window(timeframe: str, position: dict, conf: dict) -> tuple[int, int] | None:
    """Window covering entry to exit with padding, or None if it will not fit.

    Returned relative to the *entry* bar, which is what `rates_window` is
    anchored on, so the forward half has to carry the whole trade plus its
    trailing margin.
    """
    capture = conf.get("capture", settings.DEFAULTS["capture"])
    high = int(capture.get("candles_max", 120))
    low = int(capture.get("candles_min", 80))
    open_time = position.get("open_time")
    close_time = position.get("close_time")
    if not open_time or not close_time:
        return None
    step = render._TF_SECONDS.get(timeframe.upper(), 60)
    span_bars = int(math.ceil((close_time - open_time) / step)) + 1
    if span_bars > high - 8:
        return None
    # Pad the remainder evenly, biased to the left: the approach into the entry
    # is what a review is usually looking at. The -1 accounts for the anchor bar
    # itself, which `rates_window` returns on top of before+after.
    total = max(low, min(high, span_bars + 24))
    spare = max(0, total - span_bars - 1)
    lead = int(spare * 0.6)
    trail = spare - lead
    return lead, span_bars + trail


def _overlay(position: dict, digits: int) -> render.TradeOverlay:
    metrics = position.get("metrics") or {}
    return render.TradeOverlay(
        side=str(position.get("side", "buy")).lower(),
        entry_price=position.get("open_price"),
        exit_price=position.get("close_price"),
        entry_time=position.get("open_time"),
        exit_time=position.get("close_time"),
        sl_initial=position.get("sl_initial"),
        tp_initial=position.get("tp_initial"),
        sl_final=position.get("sl_final"),
        tp_final=position.get("tp_final"),
        mae_price=metrics.get("mae_price"),
        mfe_price=metrics.get("mfe_price"),
        digits=digits,
    )


def _meta(position: dict, account: dict, timeframe: str, event: str,
          digits: int) -> dict:
    metrics = position.get("metrics") or {}
    opened = model.broker_dt(position.get("open_time"))
    closed = model.broker_dt(position.get("close_time"))
    phase = {
        "open": "open shot",
        "close": "close shot",
        "journey": "entry to exit",
    }.get(event, event)

    bits = [f"#{position.get('ticket')}"]
    if opened:
        bits.append(f"opened {opened:%d %b %Y %H:%M:%S}")
    bits.append(f"closed {closed:%d %b %Y %H:%M:%S}" if closed else "still open")
    if metrics.get("session"):
        bits.append(str(metrics["session"]))

    return {
        "symbol": position.get("symbol", ""),
        "timeframe": timeframe,
        "side": position.get("side", ""),
        "volume": position.get("volume"),
        "digits": digits,
        "phase": phase,
        "subtitle": "   ·   ".join(bits),
        "net_profit": position.get("net_profit") if closed else None,
        "currency": account.get("currency") or "",
        "r_multiple": metrics.get("r_multiple"),
        "duration_label": metrics.get("duration_label"),
        "exit_reason": position.get("exit_reason"),
        "mfe_money": metrics.get("mfe_money"),
        "mae_money": metrics.get("mae_money"),
        "heat_pct": metrics.get("heat_pct"),
        "planned_rr": metrics.get("planned_rr"),
        "stamp": f"{account.get('login', '')} · {paths.APP_TITLE}",
    }


def _plan(position: dict, conf: dict) -> list[tuple[str, str]]:
    """(timeframe, event) pairs to render for one position, in draw order."""
    capture = conf.get("capture", settings.DEFAULTS["capture"])
    closed = bool(position.get("close_time"))
    jobs: list[tuple[str, str]] = []
    for timeframe in settings.TIMEFRAMES:
        if timeframe in capture.get("open_timeframes", []):
            jobs.append((timeframe, "open"))
        if closed and timeframe in capture.get("close_timeframes", []):
            jobs.append((timeframe, "close"))
    if closed and capture.get("render_journey", True):
        # Lowest captured timeframe gives the journey shot the most detail.
        candidates = [
            tf for tf in reversed(settings.TIMEFRAMES)
            if tf in capture.get("open_timeframes", [])
            or tf in capture.get("close_timeframes", [])
        ]
        for timeframe in candidates:
            if _journey_window(timeframe, position, conf):
                jobs.append((timeframe, "journey"))
                break
    return jobs


def capture_position(session, position: dict, account: dict,
                     conf: dict | None = None,
                     symbol_info: dict | None = None) -> dict:
    """Render every planned shot for one position. Returns a per-shot report."""
    conf = conf or settings.load()
    capture = conf.get("capture", settings.DEFAULTS["capture"])
    symbol = position.get("symbol") or ""
    info = symbol_info if symbol_info is not None else (session.symbol_info(symbol) or {})
    digits = int(info.get("digits") or 2)

    folder = position_folder(position, account, conf)
    overlay = _overlay(position, digits)
    theme = capture.get("theme", "midnight")
    width = int(capture.get("width", 1920))
    height = int(capture.get("height", 1080))
    supersample = int(capture.get("supersample", 2))
    quality = int(capture.get("jpeg_quality", 92))
    skip_existing = bool(capture.get("skip_existing", True))

    written: list[dict] = []
    skipped: list[str] = []
    failures: list[str] = []
    recorded: set[tuple[str, str]] = set()

    for timeframe, event in _plan(position, conf):
        name = shot_name(position, account, timeframe, event, conf)
        destination = folder / name
        if skip_existing and destination.exists():
            skipped.append(destination.name)
            recorded.add((timeframe, event))
            continue

        if event == "journey":
            window = _journey_window(timeframe, position, conf)
            if not window:
                continue
            before, after = window
            centre_epoch = position.get("open_time")
            moment = None
            moment_label = ""
        else:
            before, after = _window(timeframe, position, event, conf)
            centre_epoch = (
                position.get("open_time") if event == "open"
                else position.get("close_time")
            )
            moment = centre_epoch
            moment_label = "ENTRY" if event == "open" else "EXIT"
        if not centre_epoch:
            continue

        centre = model.broker_dt(centre_epoch)
        try:
            bars = session.rates_window(symbol, timeframe, centre, before, after)
        except mt5conn.MT5Error as exc:
            failures.append(f"{timeframe}/{event}: {exc}")
            continue
        if not bars:
            failures.append(f"{timeframe}/{event}: no bars returned")
            continue

        meta = _meta(position, account, timeframe, event, digits)
        try:
            render.render_chart(
                bars, meta, trade=overlay, destination=destination,
                theme=theme, width=width, height=height,
                supersample=supersample, moment=moment,
                moment_label=moment_label,
                watermark=paths.APP_NAME.upper(),
            )
        except Exception as exc:  # pragma: no cover - drawing guard
            failures.append(f"{timeframe}/{event}: {exc}")
            continue

        relative = destination.relative_to(paths.charts_dir()).as_posix()
        recorded.add((timeframe, event))
        db.record_shot(
            position["id"], timeframe, event, relative,
            width, height, len(bars), source="studio",
        )
        written.append(
            {"timeframe": timeframe, "event": event, "path": relative,
             "bars": len(bars)}
        )

    if written or skipped:
        db.mark_captured(position["id"])

    # Drop rows for shots this position no longer produces. Changing the
    # timeframe list otherwise leaves the workbook pointing at files that were
    # deleted from disk, which is worse than having no row at all.
    stale = [
        shot for shot in db.shots_for(position["id"])
        if shot.get("source") == "studio"
        and (shot["timeframe"], shot["event"]) not in recorded
    ]
    for shot in stale:
        if not (paths.charts_dir() / shot["rel_path"]).exists():
            db.execute("DELETE FROM shots WHERE id=?", (shot["id"],))

    return {
        "ticket": position.get("ticket"),
        "folder": str(folder),
        "written": written,
        "skipped": skipped,
        "failures": failures,
    }


def capture_account(account_id: int, tickets: list[int] | None = None,
                    only_pending: bool = True, limit: int | None = None,
                    progress=None, conf: dict | None = None) -> dict:
    """Capture shots for an account, holding one terminal session throughout.

    Reconnecting per position would dominate the runtime - starting a terminal
    takes seconds, rendering a chart takes milliseconds - so the session is held
    open across the whole batch and symbols are resolved once each.
    """
    conf = conf or settings.load()
    account = db.get_account(account_id)
    if not account:
        raise ValueError(f"no such account: {account_id}")
    terminal = db.one("SELECT * FROM terminals WHERE id=?", (account["terminal_id"],))
    if terminal is None:
        raise ValueError("account has no terminal on record")
    exe_path = dict(terminal)["exe_path"]

    positions = db.position_rows(account_id, only_pending=only_pending)
    if tickets:
        wanted = {int(t) for t in tickets}
        positions = [p for p in positions if int(p["ticket"]) in wanted]
    # Oldest first so a part-finished run reads chronologically on disk.
    positions.sort(key=lambda p: p.get("open_time") or 0)
    if limit:
        positions = positions[: int(limit)]

    run_id = db.start_run(
        "capture", account_id, f"{len(positions)} position(s)"
    )
    reports: list[dict] = []
    shots = failures = 0
    try:
        if not positions:
            db.finish_run(run_id, "ok", {"positions": 0, "shots": 0})
            return {"positions": 0, "shots": 0, "failures": 0, "reports": []}

        with mt5conn.connect(exe_path) as session:
            symbol_cache: dict[str, dict] = {}
            for index, position in enumerate(positions, start=1):
                symbol = position.get("symbol") or ""
                if symbol not in symbol_cache:
                    symbol_cache[symbol] = session.symbol_info(symbol) or {}
                report = capture_position(
                    session, position, account, conf,
                    symbol_info=symbol_cache[symbol],
                )
                shots += len(report["written"])
                failures += len(report["failures"])
                reports.append(report)
                if progress:
                    progress(
                        {
                            "done": index,
                            "total": len(positions),
                            "ticket": position.get("ticket"),
                            "symbol": symbol,
                            "shots": len(report["written"]),
                        }
                    )
    except Exception as exc:
        db.finish_run(run_id, "error", {"shots": shots}, str(exc))
        raise
    db.finish_run(
        run_id, "ok",
        {"positions": len(positions), "shots": shots, "failures": failures},
    )
    return {
        "positions": len(positions),
        "shots": shots,
        "failures": failures,
        "reports": reports,
    }
