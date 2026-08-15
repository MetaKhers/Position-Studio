"""A local HTTP server that fronts `api` and serves the web UI.

The app is a desktop window running a local page. That needs a server rather
than `file://` for two reasons: the chart images live outside the web folder and
under paths with spaces, and pywebview's JS bridge is awkward for anything that
returns a large payload. A loopback HTTP server sidesteps both and has the
pleasant side effect that the same UI opens in a normal browser if the webview
runtime is missing.

Security posture: bound to 127.0.0.1 on an ephemeral port, and every request
must carry a token generated at startup. Without the token any page in any
browser on this machine could otherwise reach an API that reads the trader's
account history and writes files.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import socket
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import api, db, paths

TOKEN = secrets.token_urlsafe(24)

# Guessed types are unreliable on Windows, where the registry can map .js to
# something no browser will execute as a module.
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")


class ApiError(Exception):
    """An error with an HTTP status attached."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _int(value, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(f"{field} must be a number", 400) from None


class Handler(BaseHTTPRequestHandler):
    server_version = "PositionStudio"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing --------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: A003 - base class name
        """Silence the default stderr access log; failures are logged properly."""
        return

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The UI is generated here and never cached; images are content-addressed
        # by name and safe to cache for a session.
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _fail(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status)

    def _body(self) -> dict:
        length = _int(self.headers.get("Content-Length") or 0, "Content-Length")
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError("request body was not valid JSON", 400) from None
        if not isinstance(data, dict):
            raise ApiError("request body must be a JSON object", 400)
        return data

    def _authorized(self, query: dict) -> bool:
        header = (self.headers.get("X-Studio-Token") or "").strip()
        if secrets.compare_digest(header, TOKEN):
            return True
        # Images are loaded by <img src>, which cannot carry a header, so the
        # token may also arrive in the query string.
        supplied = (query.get("token") or [""])[0]
        return secrets.compare_digest(supplied, TOKEN)

    # -- routing ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - base class name
        self._route("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802
        self._route("PATCH")

    def _route(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path == "/" or path == "/index.html":
                self._serve_index()
                return
            if path.startswith("/api/") or path.startswith("/charts/"):
                if not self._authorized(query):
                    self._fail("not authorized", 401)
                    return
            if path.startswith("/api/"):
                self._api(method, path[len("/api/"):].strip("/"), query)
                return
            if path.startswith("/charts/"):
                self._serve_chart(path[len("/charts/"):])
                return
            self._serve_static(path.lstrip("/"))
        except ApiError as exc:
            self._fail(str(exc), exc.status)
        except ValueError as exc:
            # The api layer raises ValueError for "you asked for something that
            # does not exist", which is a client error, not a crash.
            self._fail(str(exc), 400)
        except BrokenPipeError:
            # The window closed mid-response. Nothing to report.
            return
        except Exception as exc:  # pragma: no cover - last-resort guard
            self._log_crash(traceback.format_exc())
            self._fail(f"{exc.__class__.__name__}: {exc}", 500)

    def _api(self, method: str, route: str, query: dict) -> None:
        parts = route.split("/") if route else []
        body = self._body() if method in ("POST", "PATCH") else {}

        def arg(name: str, default=None):
            if name in body:
                return body[name]
            if name in query:
                return query[name][0]
            return default

        # -- state and settings
        if parts == ["state"] and method == "GET":
            return self._json(api.bootstrap())
        if parts == ["settings"] and method == "GET":
            from . import settings

            return self._json({"settings": settings.load()})
        if parts == ["settings"] and method == "PATCH":
            return self._json(api.update_settings(body))
        if parts == ["settings", "reset"] and method == "POST":
            return self._json(api.reset_settings())

        # -- terminals
        if parts == ["terminals", "scan"] and method == "POST":
            return self._json(api.scan_terminals(bool(arg("deep", True))))
        if parts == ["terminals", "add"] and method == "POST":
            path = str(arg("path") or "").strip()
            if not path:
                raise ApiError("no path given", 400)
            return self._json(api.add_terminal(path))
        if len(parts) == 3 and parts[0] == "terminals" and method == "POST":
            terminal_id = _int(parts[1], "terminal id")
            if parts[2] == "probe":
                return self._json(api.probe_terminal(terminal_id))
            if parts[2] == "enabled":
                return self._json(
                    api.set_terminal_enabled(terminal_id, bool(arg("enabled", True)))
                )
            if parts[2] == "sync":
                return self._json(
                    api.sync_terminal(terminal_id, arg("since_days"))
                )
        if len(parts) == 2 and parts[0] == "terminals" and method == "DELETE":
            return self._json(api.remove_terminal(_int(parts[1], "terminal id")))

        # -- work
        if parts == ["pipeline"] and method == "POST":
            return self._json(
                api.run_pipeline(
                    _int(arg("terminal_id"), "terminal_id"),
                    account_id=(
                        _int(arg("account_id"), "account_id")
                        if arg("account_id") not in (None, "")
                        else None
                    ),
                    stages=arg("stages"),
                    only_pending=bool(arg("only_pending", True)),
                    limit=(
                        _int(arg("limit"), "limit")
                        if arg("limit") not in (None, "")
                        else None
                    ),
                    open_when_done=bool(arg("open_when_done", False)),
                )
            )
        if parts == ["analyze"] and method == "POST":
            return self._json(
                api.analyze_account(
                    _int(arg("account_id"), "account_id"),
                    only_pending=bool(arg("only_pending", True)),
                    limit=(
                        _int(arg("limit"), "limit")
                        if arg("limit") not in (None, "")
                        else None
                    ),
                )
            )
        if parts == ["capture"] and method == "POST":
            tickets = arg("tickets")
            return self._json(
                api.capture_account(
                    _int(arg("account_id"), "account_id"),
                    only_pending=bool(arg("only_pending", True)),
                    limit=(
                        _int(arg("limit"), "limit")
                        if arg("limit") not in (None, "")
                        else None
                    ),
                    tickets=[int(t) for t in tickets] if tickets else None,
                )
            )
        if parts == ["export"] and method == "POST":
            return self._json(
                api.export_workbook(_int(arg("account_id"), "account_id"))
            )

        # -- jobs
        if parts == ["jobs"] and method == "GET":
            return self._json(api.job_state())
        if parts == ["jobs", "cancel"] and method == "POST":
            job_id = arg("id")
            return self._json(
                api.cancel_job(_int(job_id, "id") if job_id not in (None, "") else None)
            )

        # -- accounts and positions
        if len(parts) == 3 and parts[0] == "accounts" and method == "GET":
            account_id = _int(parts[1], "account id")
            if parts[2] == "overview":
                return self._json(api.account_overview(account_id))
            if parts[2] == "positions":
                return self._json(
                    api.position_list(
                        account_id,
                        limit=_int(arg("limit", 200), "limit"),
                        offset=_int(arg("offset", 0), "offset"),
                        search=str(arg("search", "") or ""),
                        outcome=str(arg("outcome", "") or ""),
                        symbol=str(arg("symbol", "") or ""),
                    )
                )
        if len(parts) == 3 and parts[0] == "accounts" and parts[2] == "reset" \
                and method == "POST":
            return self._json(api.clear_analysis(_int(parts[1], "account id")))
        if len(parts) == 2 and parts[0] == "positions" and method == "GET":
            return self._json(api.position_detail(_int(parts[1], "position id")))
        if len(parts) == 3 and parts[0] == "positions" and parts[2] == "note" \
                and method == "POST":
            return self._json(
                api.save_note(
                    _int(parts[1], "position id"),
                    str(arg("note", "") or ""),
                    arg("tags"),
                )
            )

        # -- shell
        if parts == ["open"] and method == "POST":
            return self._json(api.reveal(str(arg("path") or "")))
        if parts == ["pick-folder"] and method == "POST":
            return self._json(api.pick_folder(arg("initial")))

        raise ApiError(f"no such endpoint: {method} /api/{route}", 404)

    # -- static ----------------------------------------------------------
    def _serve_index(self) -> None:
        """Serve the shell with the session token injected.

        The token has to reach the page somehow, and a placeholder swapped at
        serve time keeps it out of the source file and off the disk entirely.
        """
        index = paths.webapp_dir() / "index.html"
        if not index.exists():
            self._send(500, b"webapp/index.html is missing", "text/plain")
            return
        html = index.read_text(encoding="utf-8").replace("__STUDIO_TOKEN__", TOKEN)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _resolve(self, root: Path, relative: str) -> Path | None:
        """Join a request path onto a root, refusing anything that escapes it."""
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def _serve_static(self, relative: str) -> None:
        target = self._resolve(paths.webapp_dir(), relative)
        if target is None:
            self._send(404, b"not found", "text/plain")
            return
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        cache = "no-store" if target.suffix in (".html", ".js", ".css") else \
            "public, max-age=86400"
        self._send(200, target.read_bytes(), kind, {"Cache-Control": cache})

    def _serve_chart(self, relative: str) -> None:
        target = self._resolve(paths.charts_dir(), relative)
        if target is None:
            self._send(404, b"no such image", "text/plain")
            return
        kind = mimetypes.guess_type(target.name)[0] or "image/png"
        self._send(200, target.read_bytes(), kind,
                   {"Cache-Control": "private, max-age=3600"})

    def _log_crash(self, trace: str) -> None:
        try:
            log = paths.logs_dir() / "server.log"
            with log.open("a", encoding="utf-8") as handle:
                handle.write(f"\n{self.command} {self.path}\n{trace}")
        except Exception:
            pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Server:
    """The running HTTP server plus the URL the window should open."""

    def __init__(self, port: int | None = None):
        db.init()
        self.port = port or _free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, name="pstudio-http", daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> "Server":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def serve_forever(port: int | None = None) -> None:
    """Run the server in the foreground - used when there is no webview."""
    server = Server(port).start()
    print(f"{paths.APP_TITLE} listening on {server.url}")
    try:
        server.thread.join()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":  # pragma: no cover
    serve_forever()
