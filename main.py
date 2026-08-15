"""Launch MT5 Position Studio.

    python main.py             open the app window
    python main.py --browser   serve only, open in the default browser
    python main.py --serve     serve only, print the URL and wait
    python main.py --port 8765 pin the port (handy while developing)

Also carries the command line for the parts that are useful without a UI, so a
scheduled task can refresh a workbook overnight:

    python main.py --cli scan
    python main.py --cli run          sync, analyze, capture, export
    python main.py --cli export
"""

from __future__ import annotations

import argparse
import sys
import time
import webbrowser

from pstudio import api, db, jobs, paths, server


def _window(url: str, width: int, height: int) -> bool:
    """Open the app window. Returns False if pywebview is not usable here."""
    try:
        import webview
    except ImportError:
        return False

    window = webview.create_window(
        paths.APP_TITLE,
        url,
        width=width,
        height=height,
        min_size=(1120, 720),
        # The page paints its own dark background before anything renders, so a
        # dark frame colour avoids the white flash on open.
        background_color="#0B0F17",
        text_select=False,
        confirm_close=False,
    )

    def picker(initial: str | None) -> str | None:
        result = window.create_file_dialog(
            webview.FOLDER_DIALOG, directory=initial or ""
        )
        # pywebview returns a tuple, or None when the dialog is dismissed.
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else str(result)

    api.FOLDER_PICKER = picker
    try:
        # Edge WebView2 on Windows; the other backends are there for good
        # measure rather than because this app is expected off Windows.
        webview.start(gui="edgechromium" if sys.platform.startswith("win") else None)
    except Exception as exc:
        print(f"Could not start the app window: {exc}")
        return False
    return True


def _cli(command: str, args) -> int:
    """Run one operation to completion, printing progress as it goes."""
    db.init()
    runner = jobs.runner()

    if command == "scan":
        api.scan_terminals(deep=True)
    elif command in ("run", "sync", "analyze", "capture", "export"):
        terminals = [t for t in db.list_terminals() if t.get("enabled")]
        if not terminals:
            print("No terminals on record. Run: python main.py --cli scan")
            return 2
        terminal = terminals[0]
        accounts = [
            a for a in db.list_accounts() if a["terminal_id"] == terminal["id"]
        ]
        account_id = accounts[0]["id"] if accounts else None
        if command == "run":
            api.run_pipeline(terminal["id"], account_id, only_pending=True)
        elif command == "sync":
            api.sync_terminal(terminal["id"])
        elif account_id is None:
            print("No account known yet. Run: python main.py --cli sync")
            return 2
        elif command == "analyze":
            api.analyze_account(account_id)
        elif command == "capture":
            api.capture_account(account_id)
        else:
            api.export_workbook(account_id)
    else:
        print(f"Unknown command: {command}")
        return 2

    last = ""
    while True:
        job = runner.active() or runner.current()
        if job is None:
            break
        state = job.as_dict()
        line = f"{state['message']} ({int((state['fraction'] or 0) * 100)}%)"
        if line != last:
            print(line)
            last = line
        if state["finished"]:
            break
        time.sleep(0.4)

    recent = runner.recent(1)
    if recent:
        final = recent[0]
        print(f"{final['status']}: {final['message']}")
        if final.get("result"):
            print(final["result"])
        return 0 if final["status"] == "done" else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="PositionStudio", description=paths.APP_TITLE
    )
    parser.add_argument("--port", type=int, default=None,
                        help="port to serve on (default: a free one)")
    parser.add_argument("--browser", action="store_true",
                        help="open in the default browser instead of a window")
    parser.add_argument("--serve", action="store_true",
                        help="serve only; do not open anything")
    parser.add_argument("--width", type=int, default=1480)
    parser.add_argument("--height", type=int, default=940)
    parser.add_argument("--cli", metavar="COMMAND",
                        help="scan | sync | analyze | capture | export | run")
    args = parser.parse_args(argv)

    if args.cli:
        return _cli(args.cli.strip().lower(), args)

    instance = server.Server(args.port).start()
    url = instance.url
    print(f"{paths.APP_TITLE}")
    print(f"  data       {paths.user_root()}")
    print(f"  listening  {url}")

    if args.serve:
        try:
            instance.thread.join()
        except KeyboardInterrupt:
            pass
        return 0

    if args.browser or not _window(url, args.width, args.height):
        if not args.browser:
            print("  pywebview unavailable - opening in your browser instead.")
        webbrowser.open(url)
        print("  Press Ctrl+C to stop.")
        try:
            instance.thread.join()
        except KeyboardInterrupt:
            pass

    instance.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
