"""Portable path resolution.

Everything the user owns - database, rendered charts, exported workbooks -
lives next to the executable so the whole folder can be copied to another
machine and run there. If that spot is not writable (folder dropped into
Program Files), we fall back to %LOCALAPPDATA%\\PositionStudio so the app
still starts instead of dying on first write.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "PositionStudio"
APP_TITLE = "MT5 Position Studio"

_USER_ROOT: Path | None = None


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Where read-only bundled assets (webapp/, fonts/) live."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


def portable_root() -> Path:
    """The folder the user sees - holds the exe once frozen."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def user_root() -> Path:
    """Parent of data/, charts/ and exports/. Probed once, then cached."""
    global _USER_ROOT
    if _USER_ROOT is None:
        candidate = portable_root()
        if _writable(candidate / "data"):
            _USER_ROOT = candidate
        else:
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
            _USER_ROOT = base / APP_NAME
            (_USER_ROOT / "data").mkdir(parents=True, exist_ok=True)
    return _USER_ROOT


def _ensure(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def data_dir() -> Path:
    return _ensure(user_root() / "data")


def charts_dir() -> Path:
    return _ensure(user_root() / "Charts")


def exports_dir() -> Path:
    return _ensure(user_root() / "Exports")


def logs_dir() -> Path:
    return _ensure(user_root() / "data" / "logs")


def db_path() -> Path:
    return data_dir() / "positionstudio.db"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def webapp_dir() -> Path:
    return bundle_dir() / "webapp"


def font_path(name: str) -> Path | None:
    """Resolve a bundled font file, then fall back to the Windows font dir."""
    local = bundle_dir() / "assets" / "fonts" / name
    if local.exists():
        return local
    system = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name
    return system if system.exists() else None


def is_portable() -> bool:
    """False when we had to fall back off the exe folder - the UI says so."""
    return user_root() == portable_root()
