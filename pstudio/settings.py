"""User-tunable settings, persisted as JSON next to the database.

Every threshold the app uses lives here rather than being buried in code -
Armin tunes his own numbers. Unknown keys in the file are preserved, missing
keys fall back to DEFAULTS, so upgrading the app never wipes a customisation.
"""

from __future__ import annotations

import copy
import json
import threading
from typing import Any

from . import paths

# Timeframes we can capture. Order matters: this is the order shots render in.
TIMEFRAMES = ["H4", "H1", "M15", "M5", "M1"]

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "capture": {
        # Candles visible in a shot. The renderer aims for `target` and will
        # not go outside [min, max] - the brief asked for roughly 80-120.
        "candles_min": 80,
        "candles_target": 100,
        "candles_max": 120,
        # Timeframes captured at position open. H4 and H1 are omitted here
        # because one shot at those frames covers the whole trade - the brief
        # asked for a single closing shot rather than a near-identical pair.
        "open_timeframes": ["M15", "M5", "M1"],
        "close_timeframes": ["H4", "H1", "M15", "M5", "M1"],
        # Where the entry candle sits in the frame, 0.0 = left edge, 1.0 = right.
        # Slightly left of centre leaves room for what happened next.
        "entry_anchor": 0.62,
        "exit_anchor": 0.68,
        # A "journey" shot spans entry->exit in one frame when they fit.
        "render_journey": True,
        "width": 1920,
        "height": 1080,
        "supersample": 2,
        "image_format": "png",
        "jpeg_quality": 92,
        # Skip re-rendering shots that already exist on disk.
        "skip_existing": True,
        "theme": "midnight",
    },
    "analysis": {
        # MAE/MFE source: "ticks" is exact but slow, "bars" uses M1 highs/lows,
        # "auto" uses ticks for short trades and bars for long ones.
        "excursion_source": "auto",
        "tick_trade_max_minutes": 240,
        # Sub-minute durations are reported in seconds, per the brief.
        "seconds_threshold": 61,
        "risk_free_rate": 0.0,
        # Trading-day boundaries for session tagging, in broker time.
        "sessions": {
            "Asia": [0, 8],
            "London": [8, 13],
            "NY Overlap": [13, 17],
            "NY Late": [17, 24],
        },
        "monte_carlo_runs": 5000,
        "monte_carlo_horizon": 100,
    },
    "profile": {
        # Derived defaults for a scalper; the UI lets these be overridden.
        "account_risk_pct": 1.0,
        "target_r": 2.0,
        "trades_per_day_budget": 0,  # 0 = derive from history
        "revenge_window_minutes": 0,  # 0 = derive from history
    },
    "naming": {
        # Tokens: {seq} {ticket} {symbol} {side} {tf} {event} {date} {time}
        # {outcome} {rr} {duration}
        "folder": "{symbol}/{date}_{ticket}_{symbol}_{side}_{outcome}",
        "file": "{seq}_{ticket}_{symbol}_{side}_{tf}_{event}_{date}-{time}",
        "date_format": "%Y-%m-%d",
        "time_format": "%H%M%S",
    },
    "ui": {
        "language": "en",
        "theme": "dark",
        "accent": "cyan",
        "auto_scan_on_start": True,
    },
    "export": {
        "include_charts_sheet": True,
        "embed_thumbnails": True,
        "max_embedded_rows": 400,
    },
}

_LOCK = threading.Lock()
_CACHE: dict[str, Any] | None = None

# What a value is allowed to be, by dotted path. Anything not listed here is
# stored as given - the point is not to police every field, it is to stop the
# handful that would break something downstream if they were wrong.
#
# Enums: only these strings. A stray value here means a stylesheet with no
# matching rule, or a theme lookup that raises mid-render.
_CHOICES: dict[str, tuple[str, ...]] = {
    "capture.image_format": ("png", "jpg", "webp"),
    "capture.theme": ("midnight", "daylight"),
    "analysis.excursion_source": ("auto", "ticks", "bars"),
    "ui.language": ("en", "fa"),
    "ui.theme": ("dark", "light"),
    "ui.accent": ("cyan", "violet", "amber", "rose"),
}

# Numbers: (low, high, integer?). Bounds are generous - this is a guard rail,
# not a second opinion about how Armin wants to work. A 4-candle window or a
# 60,000-pixel image is a mistake rather than a preference.
_RANGES: dict[str, tuple[float, float, bool]] = {
    "capture.candles_min": (10, 600, True),
    "capture.candles_target": (10, 600, True),
    "capture.candles_max": (10, 600, True),
    "capture.entry_anchor": (0.02, 0.98, False),
    "capture.exit_anchor": (0.02, 0.98, False),
    "capture.width": (640, 7680, True),
    "capture.height": (400, 4320, True),
    "capture.supersample": (1, 4, True),
    "capture.jpeg_quality": (40, 100, True),
    "analysis.tick_trade_max_minutes": (0, 100_000, True),
    "analysis.seconds_threshold": (1, 3600, True),
    "analysis.monte_carlo_runs": (100, 200_000, True),
    "analysis.monte_carlo_horizon": (1, 5000, True),
    "profile.account_risk_pct": (0.01, 100, False),
    "profile.target_r": (0.1, 100, False),
    "export.max_embedded_rows": (0, 5000, True),
}


def _clean(patch: dict, prefix: str = "", rejected: list[str] | None = None) -> dict:
    """Drop or clamp values that would break something, keeping the rest.

    Dropping beats raising. A patch arrives as one object holding every field on
    the settings page, so refusing the whole thing over a single bad number
    would lose a dozen good edits the user made in the same visit.
    """
    out: dict[str, Any] = {}
    for key, value in (patch or {}).items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out[key] = _clean(value, f"{path}.", rejected)
            continue
        if path in _CHOICES:
            if not isinstance(value, str) or value not in _CHOICES[path]:
                if rejected is not None:
                    rejected.append(path)
                continue
            out[key] = value
            continue
        if path in _RANGES:
            low, high, whole = _RANGES[path]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                if rejected is not None:
                    rejected.append(path)
                continue
            clamped = min(high, max(low, float(value)))
            out[key] = int(round(clamped)) if whole else clamped
            continue
        out[key] = value
    return out


def _reconcile(merged: dict[str, Any]) -> None:
    """Fix relationships between fields that are each individually valid.

    candles_min <= target <= max is the one that matters: the renderer trusts
    the window it is handed, and a min above a max produces an empty range
    rather than an error, which shows up as a chart with three candles on it.
    """
    cap = merged.get("capture")
    if not isinstance(cap, dict):
        return
    try:
        lo = int(cap["candles_min"])
        hi = int(cap["candles_max"])
        target = int(cap["candles_target"])
    except (KeyError, TypeError, ValueError):
        return
    if lo > hi:
        lo, hi = hi, lo
    cap["candles_min"] = lo
    cap["candles_max"] = hi
    cap["candles_target"] = min(hi, max(lo, target))


def _merge(base: dict, override: dict) -> dict:
    """Deep-merge override onto a copy of base, keeping unknown keys."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(force: bool = False) -> dict[str, Any]:
    global _CACHE
    with _LOCK:
        if _CACHE is not None and not force:
            return copy.deepcopy(_CACHE)
        stored: dict[str, Any] = {}
        path = paths.settings_path()
        if path.exists():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                stored = {}
        _CACHE = _merge(DEFAULTS, stored)
        return copy.deepcopy(_CACHE)


def save(patch: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Deep-merge a patch into the stored settings and write them back.

    Returns the merged settings and the dotted paths that were refused. The
    refusals are a second return value rather than a key inside the settings
    because the caller hands that dict straight to the UI as its new state, and
    a bookkeeping key living in there would eventually get saved back.
    """
    global _CACHE
    rejected: list[str] = []
    current = load()
    merged = _merge(current, _clean(patch or {}, rejected=rejected))
    _reconcile(merged)
    with _LOCK:
        _CACHE = merged
        tmp = paths.settings_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        tmp.replace(paths.settings_path())
    return copy.deepcopy(merged), rejected


def reset() -> dict[str, Any]:
    global _CACHE
    with _LOCK:
        _CACHE = copy.deepcopy(DEFAULTS)
        paths.settings_path().write_text(json.dumps(_CACHE, indent=2), encoding="utf-8")
    return copy.deepcopy(_CACHE)


def get(dotted: str, default: Any = None) -> Any:
    """settings.get('capture.candles_target')"""
    node: Any = load()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node
