"""The operations the UI can ask for, and nothing about HTTP.

Everything here either returns a plain dict immediately (reads, cheap writes) or
submits a job and returns its descriptor (anything that touches MT5, the disk,
or hundreds of positions). Keeping the split here rather than in the request
handler means the same operations can be driven from a script or a test without
a server running.

The headline operation is `run_pipeline`: sync -> analyze -> capture -> export in
one job. That is the brief's actual workflow - "if there is new data, or old
history that hasn't been analyzed, read it fully, shoot the charts, build the
workbook" - and making it one button rather than four is the difference between
a tool someone uses daily and one they poke at once.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

from . import (
    capture,
    db,
    excel,
    ingest,
    jobs,
    metrics,
    model,
    mt5conn,
    paths,
    settings,
    stats,
    terminals,
)


# -- reads -----------------------------------------------------------------
def bootstrap() -> dict:
    """Everything the UI needs to draw itself on load."""
    conf = settings.load()
    account_rows = db.list_accounts()
    active = jobs.runner().active()
    return {
        "app": {
            "name": paths.APP_NAME,
            "title": paths.APP_TITLE,
            "portable": paths.is_portable(),
            "root": str(paths.user_root()),
            "charts_dir": str(paths.charts_dir()),
            "exports_dir": str(paths.exports_dir()),
            "python": sys.version.split()[0],
            "mt5_available": mt5conn.available(),
            "min_sample": stats.MIN_SAMPLE,
            "timeframes": settings.TIMEFRAMES,
        },
        "terminals": db.list_terminals(),
        "accounts": account_rows,
        "settings": conf,
        "runs": db.recent_runs(8),
        "job": active.as_dict() if active else None,
        "jobs": jobs.runner().recent(6),
        "exports": _recent_exports(),
    }


def _recent_exports(limit: int = 8) -> list[dict]:
    folder = paths.exports_dir()
    files = [
        p for p in folder.glob("*.xlsx")
        # Excel's lock files share the extension and are not exports.
        if not p.name.startswith("~$")
    ]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "name": p.name,
            "path": str(p),
            "size": p.stat().st_size,
            "modified": p.stat().st_mtime,
        }
        for p in files[:limit]
    ]


def account_overview(account_id: int) -> dict:
    """Headline figures for one account, computed the same way the workbook does."""
    account = db.get_account(account_id)
    if not account:
        raise ValueError(f"no such account: {account_id}")
    conf = settings.load()
    positions = db.position_rows(account_id)
    closed = [p for p in positions if p.get("close_time")]
    closed.sort(key=lambda p: p["close_time"])
    metrics.enrich_series(closed, account.get("start_balance"), conf)
    summary = stats.summarize(closed, account.get("start_balance"))
    analysis = conf.get("analysis", {})
    return {
        "account": account,
        "summary": summary,
        "monte_carlo": stats.monte_carlo(
            closed,
            runs=int(analysis.get("monte_carlo_runs", 5000)),
            horizon=int(analysis.get("monte_carlo_horizon", 100)),
            start_balance=account.get("start_balance"),
        ),
        "by_symbol": stats.group_by(closed, lambda p: p.get("symbol"))[:12],
        "by_session": stats.group_by(
            closed, lambda p: (p.get("metrics") or {}).get("session")
        ),
        "by_weekday": stats.group_by(
            closed, lambda p: (p.get("metrics") or {}).get("weekday")
        ),
        "by_r_bucket": stats.group_by(
            closed, lambda p: (p.get("metrics") or {}).get("r_bucket")
        ),
        "counts": {
            "positions": len(positions),
            "closed": len(closed),
            "open": len(positions) - len(closed),
            "pending": db.pending_count(account_id),
            "shots": sum(db.shot_counts(account_id).values()),
        },
    }


def position_list(account_id: int, limit: int = 200, offset: int = 0,
                  search: str = "", outcome: str = "",
                  symbol: str = "") -> dict:
    """Positions for the table, newest first, with the shot count per row."""
    conf = settings.load()
    positions = db.position_rows(account_id)
    account = db.get_account(account_id) or {}
    # Sorts the closed trades itself, so the display order here is irrelevant.
    metrics.enrich_series(positions, account.get("start_balance"), conf)
    counts = db.shot_counts(account_id)

    needle = (search or "").strip().lower()
    filtered = []
    for position in positions:
        if symbol and position.get("symbol") != symbol:
            continue
        net = float(position.get("net_profit") or 0.0)
        if outcome == "win" and net <= 0:
            continue
        if outcome == "loss" and net >= 0:
            continue
        if outcome == "open" and position.get("close_time"):
            continue
        if needle:
            haystack = " ".join(
                str(position.get(field) or "")
                for field in ("ticket", "symbol", "side", "exit_reason", "note")
            ).lower()
            if needle not in haystack:
                continue
        filtered.append(position)

    total = len(filtered)
    window = filtered[offset: offset + limit]
    rows = [_position_row(p, counts.get(p["id"], 0)) for p in window]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "rows": rows,
        "symbols": sorted({p["symbol"] for p in positions if p.get("symbol")}),
    }


def _position_row(position: dict, shot_count: int) -> dict:
    """One table row. Formatting that the UI would otherwise duplicate is done
    here so the table and the workbook can never disagree."""
    computed = position.get("metrics") or {}
    opened = model.broker_dt(position.get("open_time"))
    closed = model.broker_dt(position.get("close_time"))
    return {
        "id": position["id"],
        "ticket": position.get("ticket"),
        "symbol": position.get("symbol"),
        "side": position.get("side"),
        "volume": position.get("volume"),
        "open_time": opened.isoformat(sep=" ") if opened else None,
        "close_time": closed.isoformat(sep=" ") if closed else None,
        "open_price": position.get("open_price"),
        "close_price": position.get("close_price"),
        "sl_initial": position.get("sl_initial"),
        "tp_initial": position.get("tp_initial"),
        "net_profit": position.get("net_profit"),
        "duration_s": position.get("duration_s"),
        "duration_label": computed.get("duration_label")
        or model.format_duration(position.get("duration_s")),
        "r_multiple": computed.get("r_multiple"),
        "planned_rr": computed.get("planned_rr"),
        "mae_money": computed.get("mae_money"),
        "mfe_money": computed.get("mfe_money"),
        "heat_pct": computed.get("heat_pct"),
        "capture_ratio": computed.get("capture_ratio"),
        "session": computed.get("session"),
        "weekday": computed.get("weekday"),
        "exit_reason": position.get("exit_reason"),
        "excursion_source": computed.get("excursion_source"),
        "balance_after": computed.get("balance_after"),
        "note": position.get("note") or "",
        "analyzed": bool(position.get("analyzed_at")),
        "captured": bool(position.get("captured_at")),
        "shots": shot_count,
        "partials": position.get("partials") or 0,
    }


def position_detail(position_id: int) -> dict:
    """One position with its metrics and its shots, for the review pane."""
    row = db.one("SELECT * FROM positions WHERE id=?", (position_id,))
    if row is None:
        raise ValueError(f"no such position: {position_id}")
    position = dict(row)
    import json

    try:
        position["metrics"] = (
            json.loads(position.pop("metrics_json") or "{}") or {}
        )
    except Exception:
        position["metrics"] = {}
        position.pop("metrics_json", None)

    shots = db.shots_for(position_id)
    root = paths.charts_dir()
    for shot in shots:
        shot["exists"] = (root / shot["rel_path"]).exists()
        # Served through the local HTTP server, not as a file:// path - the
        # webview will not load file:// images from an http:// document.
        shot["url"] = "/charts/" + shot["rel_path"]
    folder = shots[0]["rel_path"].rsplit("/", 1)[0] if shots else ""
    return {
        "position": _position_row(position, len(shots)),
        "raw": position,
        "shots": shots,
        "folder": str(root / folder) if folder else "",
    }


def save_note(position_id: int, note: str, tags: str | None = None) -> dict:
    db.set_note(position_id, note, tags)
    return {"ok": True}


# -- terminals -------------------------------------------------------------
def add_terminal(raw_path: str) -> dict:
    record = terminals.describe_path(raw_path)
    if not record:
        raise ValueError(
            "That is not a MetaTrader 5 terminal. Point at terminal64.exe or "
            "the folder containing it."
        )
    terminal_id = db.upsert_terminal(record)
    return {"terminal_id": terminal_id, "terminals": db.list_terminals()}


def set_terminal_enabled(terminal_id: int, enabled: bool) -> dict:
    db.set_terminal_enabled(terminal_id, enabled)
    return {"terminals": db.list_terminals()}


def remove_terminal(terminal_id: int) -> dict:
    db.delete_terminal(terminal_id)
    return {"terminals": db.list_terminals()}


def update_settings(patch: dict) -> dict:
    merged, rejected = settings.save(patch)
    return {"settings": merged, "rejected": rejected}


def reset_settings() -> dict:
    return {"settings": settings.reset()}


def reveal(target: str) -> dict:
    """Open a folder, or a file's folder with the file selected, in Explorer."""
    path = Path(target)
    if not path.exists():
        raise ValueError(f"path no longer exists: {target}")
    if sys.platform.startswith("win"):
        if path.is_dir():
            os.startfile(str(path))  # noqa: S606 - user-initiated, user's own path
        else:
            subprocess.Popen(["explorer", "/select,", str(path)])
    else:  # pragma: no cover - the app targets Windows
        subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])
    return {"ok": True}


def clear_analysis(account_id: int) -> dict:
    """Forget every metric and capture stamp so the next run redoes the lot."""
    count = db.clear_analysis(account_id)
    return {"reset": count, "pending": db.pending_count(account_id)}


# Set by `main` when a native window is hosting the UI: pywebview can open a
# real folder dialog, and a page in a plain browser cannot.
FOLDER_PICKER = None


def pick_folder(initial: str | None = None) -> dict:
    """Ask the user for a MetaTrader folder using the best dialog available.

    Typing a path is the fallback in the UI, not the expectation - most people
    have no idea where their terminal lives, which is exactly why the scanner
    exists and why this dialog matters for the ones the scanner misses.
    """
    if FOLDER_PICKER is not None:
        return {"path": FOLDER_PICKER(initial) or None}
    try:
        # Only reached when the UI is open in a browser rather than the app
        # window. Tk is in the standard library and adequate for one dialog.
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        chosen = filedialog.askdirectory(
            initialdir=initial or os.environ.get("PROGRAMFILES", "C:/"),
            title="Select the MetaTrader 5 folder",
        )
        root.destroy()
        return {"path": chosen or None}
    except Exception as exc:
        return {"path": None, "unavailable": str(exc)}


# -- jobs ------------------------------------------------------------------
def scan_terminals(deep: bool = True) -> dict:
    def work(job: jobs.Job) -> dict:
        job.update("Looking for MetaTrader 5 installations", fraction=0.1)
        found = terminals.scan(deep=deep)
        job.update(f"Found {len(found)}, saving", fraction=0.7, total=len(found))
        added = 0
        for index, record in enumerate(found, start=1):
            before = {t["exe_path"] for t in db.list_terminals()}
            db.upsert_terminal(record)
            if record["exe_path"] not in before:
                added += 1
            job.update(done=index, total=len(found))
        job.detail["summary"] = (
            f"{len(found)} terminal(s) found, {added} new"
            if found
            else "No terminals found"
        )
        return {"found": len(found), "added": added,
                "terminals": db.list_terminals()}

    return _submit("scan", "Scanning for MT5 terminals", work)


def probe_terminal(terminal_id: int) -> dict:
    row = db.one("SELECT * FROM terminals WHERE id=?", (terminal_id,))
    if row is None:
        raise ValueError(f"no such terminal: {terminal_id}")
    exe_path = dict(row)["exe_path"]

    def work(job: jobs.Job) -> dict:
        job.update("Connecting to the terminal", fraction=0.3)
        try:
            info = mt5conn.probe(exe_path)
        except mt5conn.MT5Error as exc:
            db.set_terminal_error(terminal_id, str(exc))
            raise
        db.set_terminal_error(terminal_id, info.get("error"))
        account_id = None
        if info.get("account"):
            account_id = db.upsert_account(terminal_id, info["account"])
            job.detail["summary"] = (
                f"Connected · account {info['account'].get('login')}"
            )
        else:
            job.detail["summary"] = info.get("error") or "Connected, no account"
        return {
            **info,
            "account_id": account_id,
            "terminals": db.list_terminals(),
            "accounts": db.list_accounts(),
        }

    return _submit("probe", f"Connecting to {Path(exe_path).parent.name}", work,
                   {"terminal_id": terminal_id})


def sync_terminal(terminal_id: int, since_days: int | None = None) -> dict:
    exe_path = _exe_for_terminal(terminal_id)

    def work(job: jobs.Job) -> dict:
        result = _do_sync(job, terminal_id, exe_path, since_days)
        job.detail["summary"] = (
            f"{result['new_positions']} new of {result['positions']} positions"
        )
        return result

    return _submit("sync", "Reading account history", work,
                   {"terminal_id": terminal_id})


def analyze_account(account_id: int, only_pending: bool = True,
                    limit: int | None = None) -> dict:
    def work(job: jobs.Job) -> dict:
        result = _do_analyze(job, account_id, only_pending, limit)
        job.detail["summary"] = f"{result['analyzed']} position(s) analyzed"
        return result

    return _submit("analyze", "Computing MAE / MFE and R-multiples", work,
                   {"account_id": account_id})


def capture_account(account_id: int, only_pending: bool = True,
                    limit: int | None = None,
                    tickets: list[int] | None = None) -> dict:
    def work(job: jobs.Job) -> dict:
        result = _do_capture(job, account_id, only_pending, limit, tickets)
        job.detail["summary"] = (
            f"{result['shots']} shot(s) across {result['positions']} position(s)"
        )
        return result

    return _submit("capture", "Rendering charts", work,
                   {"account_id": account_id})


def export_workbook(account_id: int) -> dict:
    def work(job: jobs.Job) -> dict:
        result = _do_export(job, account_id)
        job.detail["summary"] = Path(result["path"]).name
        return result

    return _submit("export", "Building the mentorship workbook", work,
                   {"account_id": account_id})


def run_pipeline(terminal_id: int, account_id: int | None = None,
                 stages: list[str] | None = None,
                 only_pending: bool = True, limit: int | None = None,
                 open_when_done: bool = False) -> dict:
    """Sync, analyze, capture and export in one job.

    Stages are weighted by how long they actually take on this machine - capture
    dominates at roughly two thirds of the wall clock - so the progress bar
    tracks reality instead of jumping.
    """
    exe_path = _exe_for_terminal(terminal_id)
    wanted = stages or ["sync", "analyze", "capture", "export"]
    weights = {"sync": 0.08, "analyze": 0.22, "capture": 0.62, "export": 0.08}
    active = [s for s in ("sync", "analyze", "capture", "export") if s in wanted]
    total_weight = sum(weights[s] for s in active) or 1.0

    def work(job: jobs.Job) -> dict:
        done_weight = 0.0
        out: dict = {"stages": {}}
        resolved = account_id

        for stage in active:
            span = weights[stage] / total_weight
            base = done_weight

            def scaled(message: str, inner: float) -> None:
                job.update(message, fraction=base + span * max(0.0, min(1.0, inner)))

            if stage == "sync":
                out["stages"]["sync"] = _do_sync(
                    job, terminal_id, exe_path, None, scaled
                )
                resolved = out["stages"]["sync"]["account_id"]
            elif resolved is None:
                raise ValueError(
                    "No account known for this terminal yet. Run a sync first."
                )
            elif stage == "analyze":
                out["stages"]["analyze"] = _do_analyze(
                    job, resolved, only_pending, limit, scaled
                )
            elif stage == "capture":
                out["stages"]["capture"] = _do_capture(
                    job, resolved, only_pending, limit, None, scaled
                )
            elif stage == "export":
                out["stages"]["export"] = _do_export(job, resolved, scaled)
            done_weight += span

        out["account_id"] = resolved
        parts = []
        if "sync" in out["stages"]:
            parts.append(f"{out['stages']['sync']['new_positions']} new")
        if "analyze" in out["stages"]:
            parts.append(f"{out['stages']['analyze']['analyzed']} analyzed")
        if "capture" in out["stages"]:
            parts.append(f"{out['stages']['capture']['shots']} shots")
        if "export" in out["stages"]:
            parts.append(Path(out["stages"]["export"]["path"]).name)
        job.detail["summary"] = " · ".join(parts) or "Nothing to do"
        if open_when_done and "export" in out["stages"]:
            try:
                reveal(out["stages"]["export"]["path"])
            except Exception:
                # Failing to pop open Explorer must not fail the run.
                pass
        return out

    return _submit("pipeline", "Full run", work,
                   {"terminal_id": terminal_id, "stages": active})


def cancel_job(job_id: int | None = None) -> dict:
    return {"cancelled": jobs.runner().cancel(job_id)}


def job_state() -> dict:
    watched = jobs.runner().watch()
    return {
        "job": watched.as_dict() if watched else None,
        "recent": jobs.runner().recent(6),
        "depth": jobs.runner().queue_depth(),
    }


# -- stage implementations -------------------------------------------------
# Each takes the job so it can report progress, and an optional `scaled`
# reporter so the same code serves both the single-stage jobs and the pipeline,
# where its progress is one slice of the bar.
def _submit(kind: str, title: str, work, payload: dict | None = None) -> dict:
    return jobs.runner().submit(kind, title, work, payload).as_dict()


def _exe_for_terminal(terminal_id: int) -> str:
    row = db.one("SELECT * FROM terminals WHERE id=?", (terminal_id,))
    if row is None:
        raise ValueError(f"no such terminal: {terminal_id}")
    return dict(row)["exe_path"]


def _do_sync(job: jobs.Job, terminal_id: int, exe_path: str,
             since_days: int | None, scaled=None) -> dict:
    report = scaled or (lambda message, inner: job.update(message, fraction=inner))
    report("Connecting to the terminal", 0.02)
    since = None
    if since_days:
        since = dt.datetime.now() - dt.timedelta(days=int(since_days))
    with mt5conn.connect(exe_path) as session:
        result = ingest.sync_account(
            session, terminal_id, since=since,
            progress=lambda message, fraction: report(message, fraction),
        )
    db.set_terminal_error(terminal_id, None)
    return result


def _do_analyze(job: jobs.Job, account_id: int, only_pending: bool,
                limit: int | None, scaled=None) -> dict:
    report = scaled or (lambda message, inner: job.update(message, fraction=inner))

    def on_progress(info: dict) -> None:
        total = info.get("total") or 1
        job.update(done=info.get("done"), total=total)
        report(
            f"Analyzing {info.get('symbol', '')} #{info.get('ticket', '')}"
            f" ({info.get('done')}/{total})",
            (info.get("done") or 0) / total,
        )

    report("Preparing analysis", 0.01)
    return metrics.analyze_account(
        account_id, only_pending=only_pending, limit=limit, progress=on_progress
    )


def _do_capture(job: jobs.Job, account_id: int, only_pending: bool,
                limit: int | None, tickets: list[int] | None,
                scaled=None) -> dict:
    report = scaled or (lambda message, inner: job.update(message, fraction=inner))

    def on_progress(info: dict) -> None:
        total = info.get("total") or 1
        job.update(done=info.get("done"), total=total,
                   shots=info.get("shots"))
        report(
            f"Charting {info.get('symbol', '')} #{info.get('ticket', '')}"
            f" ({info.get('done')}/{total})",
            (info.get("done") or 0) / total,
        )

    report("Preparing capture", 0.01)
    return capture.capture_account(
        account_id, tickets=tickets, only_pending=only_pending, limit=limit,
        progress=on_progress,
    )


def _do_export(job: jobs.Job, account_id: int, scaled=None) -> dict:
    report = scaled or (lambda message, inner: job.update(message, fraction=inner))
    steps = ["Reading positions", "Computing statistics", "Writing trades",
             "Building dashboard", "Building pivots", "Building excursions",
             "Writing findings", "Saving workbook"]
    seen: list[str] = []

    def on_progress(info: dict) -> None:
        message = info.get("message", "")
        if message and message not in seen:
            seen.append(message)
        # The workbook builder names its steps; matching them against the known
        # order gives a real fraction rather than a guess.
        try:
            position = steps.index(message) + 1
        except ValueError:
            position = len(seen)
        report(message or "Building workbook", position / (len(steps) + 1))

    report("Preparing workbook", 0.02)
    return excel.build_workbook(account_id, progress=on_progress)
