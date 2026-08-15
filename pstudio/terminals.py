"""Find the MetaTrader 5 terminals installed on this PC.

MT5 keeps its per-installation state in %APPDATA%\\MetaQuotes\\Terminal\\<HASH>,
and each of those folders contains an `origin.txt` (UTF-16) pointing back at the
install directory. That mapping is the reliable way to pair an executable with
its data folder, so it is the primary discovery source. We then sweep the usual
install roots and the uninstall registry keys to catch terminals that have never
been launched, and support portable installs where the data folder *is* the
install folder.

Nothing here launches a terminal. Broker, last login and build are read straight
off disk (config/common.ini and the newest log file) so the terminal list is
informative before the user connects to anything.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

EXE_NAMES = ("terminal64.exe", "terminal.exe")

_BUILD_RE = re.compile(r"MetaTrader\s+5\s+(?:x64\s+)?build\s+(\d+)", re.I)
_LOGIN_RE = re.compile(r"\b(\d{4,12})\b")


def _decode(raw: bytes) -> str:
    """MT5 writes most of its text files as UTF-16LE with a BOM."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-16", errors="replace")


def _read_text(path: Path, limit: int = 2_000_000) -> str:
    try:
        with path.open("rb") as handle:
            return _decode(handle.read(limit))
    except Exception:
        return ""


def _ini_values(path: Path, section: str = "Common") -> dict[str, str]:
    """Read one section of an ini file.

    Section-scoping matters here: common.ini repeats `Server=` and `Login=`
    under an empty `[Ftp]` section, and a flat parse lets those blanks
    clobber the real broker and account.
    """
    out: dict[str, str] = {}
    wanted = section.strip().lower()
    current = ""
    for line in _read_text(path).splitlines():
        line = line.strip().lstrip("﻿")
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().lower()
            continue
        if current != wanted or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def appdata_instances() -> list[dict]:
    """Every %APPDATA%\\MetaQuotes\\Terminal\\<HASH> folder that has an origin."""
    base = Path(os.environ.get("APPDATA", "")) / "MetaQuotes" / "Terminal"
    if not base.is_dir():
        return []
    found: list[dict] = []
    for entry in base.iterdir():
        if not entry.is_dir() or entry.name in ("Common", "Community"):
            continue
        origin = _read_text(entry / "origin.txt").strip().strip("\x00")
        if not origin:
            continue
        found.append(
            {"instance_id": entry.name, "data_dir": entry, "install_dir": Path(origin)}
        )
    return found


def _install_roots() -> list[Path]:
    roots: list[Path] = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        value = os.environ.get(var)
        if value:
            roots.append(Path(value))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Programs")
    # Portable installs commonly sit at a drive root or in a Trading folder.
    for letter in "CDEFGH":
        drive = Path(f"{letter}:\\")
        if drive.exists():
            roots.append(drive)
            for name in ("Trading", "MT5", "MetaTrader", "Brokers", "Forex", "Tools"):
                candidate = drive / name
                if candidate.is_dir():
                    roots.append(candidate)
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root).lower()
        if key not in seen and root.is_dir():
            seen.add(key)
            unique.append(root)
    return unique


def _scan_root(root: Path, depth: int = 2) -> list[Path]:
    """Look for terminal executables up to `depth` levels below a root."""
    hits: list[Path] = []
    for name in EXE_NAMES:
        direct = root / name
        if direct.is_file():
            hits.append(direct)
    if depth <= 0:
        return hits
    try:
        children = [c for c in root.iterdir() if c.is_dir()]
    except (PermissionError, OSError):
        return hits
    for child in children:
        # Cheap filter at the top level: only descend into plausible folders.
        if depth == 2 and root.parent == root:
            lowered = child.name.lower()
            if not any(
                token in lowered
                for token in ("meta", "mt5", "mt4", "trad", "broker", "forex", "terminal")
            ):
                continue
        hits.extend(_scan_root(child, depth - 1))
    return hits


def registry_installs() -> list[Path]:
    """Uninstall entries usually record the install folder of each terminal."""
    try:
        import winreg
    except ImportError:
        return []
    out: list[Path] = []
    branches = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in branches:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, name) as key:
                        display = str(winreg.QueryValueEx(key, "DisplayName")[0])
                        if "metatrader" not in display.lower():
                            continue
                        location = ""
                        for value in ("InstallLocation", "DisplayIcon", "UninstallString"):
                            try:
                                location = str(winreg.QueryValueEx(key, value)[0])
                            except OSError:
                                continue
                            if location:
                                break
                        if not location:
                            continue
                        folder = Path(location.strip('" ').split(",")[0])
                        if folder.suffix.lower() == ".exe":
                            folder = folder.parent
                        if folder.is_dir():
                            out.append(folder)
                except OSError:
                    continue
    return out


def running_terminals() -> list[Path]:
    """Executable paths of terminals that are open right now."""
    try:
        proc = subprocess.run(
            [
                "wmic",
                "process",
                "where",
                "name like '%terminal%.exe'",
                "get",
                "ExecutablePath",
                "/value",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []
    out: list[Path] = []
    for line in (proc.stdout or "").splitlines():
        if "=" in line:
            value = line.split("=", 1)[1].strip()
            if value.lower().endswith(EXE_NAMES):
                out.append(Path(value))
    return out


def _describe(exe: Path, data_dir: Path | None, instance_id: str | None) -> dict:
    install_dir = exe.parent
    # A portable install keeps MQL5/ and config/ beside the executable.
    portable = (install_dir / "MQL5").is_dir() and (install_dir / "config").is_dir()
    if data_dir is None and portable:
        data_dir = install_dir

    broker = None
    login = None
    logins: list[int] = []
    build = None

    if data_dir:
        common = _ini_values(data_dir / "config" / "common.ini")
        broker = common.get("Server") or None
        raw_login = common.get("Login")
        if raw_login and raw_login.isdigit():
            login = int(raw_login)
            logins.append(login)
        logs = data_dir / "logs"
        if logs.is_dir():
            files = sorted(logs.glob("*.log"), key=lambda p: p.name, reverse=True)
            for log in files[:3]:
                text = _read_text(log, 400_000)
                match = _BUILD_RE.search(text)
                if match and build is None:
                    build = int(match.group(1))
                for hit in re.finditer(r"'(\d{4,12})':", text):
                    value = int(hit.group(1))
                    if value not in logins:
                        logins.append(value)
                if build and len(logins) > 1:
                    break

    # The install folder name is what the user actually recognises
    # ("MT5 - SGB"), so prefer it over the generic product name.
    label = install_dir.name
    if label.lower() in ("metatrader 5", "metatrader5", "mt5"):
        label = f"{label}{' - ' + broker if broker else ''}"

    return {
        "name": label,
        "exe_path": exe,
        "data_dir": data_dir,
        "instance_id": instance_id,
        "broker": broker,
        "build": build,
        "is_portable": portable,
        "last_login": login,
        "known_logins": logins,
    }


def scan(deep: bool = True) -> list[dict]:
    """Discover terminals. Returns one record per executable found."""
    by_exe: dict[str, dict] = {}

    def add(exe: Path, data_dir: Path | None = None, instance_id: str | None = None):
        try:
            exe = exe.resolve()
        except OSError:
            return
        if not exe.is_file():
            return
        key = str(exe).lower()
        current = by_exe.get(key)
        if current and (current.get("data_dir") or data_dir is None):
            return
        by_exe[key] = _describe(exe, data_dir, instance_id)

    # 1. AppData instances - authoritative exe <-> data-dir pairing.
    for instance in appdata_instances():
        install = instance["install_dir"]
        for name in EXE_NAMES:
            candidate = install / name
            if candidate.is_file():
                add(candidate, instance["data_dir"], instance["instance_id"])
                break

    # 2. Registry-declared install folders.
    for folder in registry_installs():
        for name in EXE_NAMES:
            if (folder / name).is_file():
                add(folder / name)
                break

    # 3. Anything already running.
    for exe in running_terminals():
        add(exe)

    # 4. Filesystem sweep of the usual roots.
    if deep:
        for root in _install_roots():
            for exe in _scan_root(root):
                add(exe)

    # Pair up any exe still missing a data dir with a matching AppData hash.
    instances = appdata_instances()
    for record in by_exe.values():
        if record.get("data_dir"):
            continue
        install = Path(record["exe_path"]).parent
        for instance in instances:
            if str(instance["install_dir"]).lower() == str(install).lower():
                enriched = _describe(
                    Path(record["exe_path"]), instance["data_dir"], instance["instance_id"]
                )
                record.update(enriched)
                break

    return sorted(by_exe.values(), key=lambda r: r["name"].lower())


def describe_path(raw: str) -> dict | None:
    """Validate a manually entered path (exe or install folder)."""
    candidate = Path(raw.strip().strip('"'))
    if candidate.is_dir():
        for name in EXE_NAMES:
            if (candidate / name).is_file():
                candidate = candidate / name
                break
        else:
            return None
    if not candidate.is_file() or candidate.name.lower() not in EXE_NAMES:
        return None
    record = _describe(candidate, None, None)
    if not record.get("data_dir"):
        for instance in appdata_instances():
            if str(instance["install_dir"]).lower() == str(candidate.parent).lower():
                record = _describe(
                    candidate, instance["data_dir"], instance["instance_id"]
                )
                break
    record["is_manual"] = True
    return record
