"""Local web UI for exporting Spotify playlists to JSON.

Run `python3 app.py`, log in to Spotify in the browser tab that opens, tick the
playlists to export and confirm. Files land in ./output/<timestamp>/.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import threading
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import exporter
from spotify_client import SpotifyClient, SpotifyError

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "static" / "index.html"


class ExportJob:
    """State of the single export run the UI polls; one run at a time."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.items: dict[str, dict[str, Any]] = {}
        self.output_dir: str | None = None
        self.error: str | None = None

    def start(self, entries: list[dict[str, Any]]) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.output_dir = None
            self.error = None
            self.items = {e["id"]: {"name": e["name"], "status": "pending", "detail": None}
                          for e in entries}
            return True

    def progress(self, item_id: str, status: str, detail: str | None) -> None:
        with self.lock:
            self.items[item_id].update(status=status, detail=detail)

    def finish(self, output_dir: Path | None, error: str | None = None) -> None:
        with self.lock:
            self.running = False
            self.output_dir = str(output_dir) if output_dir else None
            self.error = error

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "output_dir": self.output_dir,
                "error": self.error,
                "items": [{"id": k, **v} for k, v in self.items.items()],
            }


class App:
    def __init__(self, client: SpotifyClient, out_root: Path, host: str):
        self.client = client
        self.host = host  # expected Host header, e.g. "127.0.0.1:8888"
        self.out_root = out_root
        self.job = ExportJob()
        self.scanned: dict[str, dict[str, Any]] = {}

    def scan(self) -> list[dict[str, Any]]:
        entries = exporter.scan(self.client)
        self.scanned = {e["id"]: e for e in entries}
        return entries

    def start_export(self, ids: list[str]) -> bool:
        # Only ids from the last scan are accepted, so the UI can't request
        # playlists the user isn't allowed to read.
        entries = [self.scanned[i] for i in ids if i in self.scanned]
        if not entries or not self.job.start(entries):
            return False
        threading.Thread(target=self._run_export, args=(entries,), daemon=True).start()
        return True

    def _run_export(self, entries: list[dict[str, Any]]) -> None:
        try:
            out = exporter.export(self.client, entries, self.out_root, self.job.progress)
        except (SpotifyError, OSError) as exc:
            self.job.finish(None, str(exc))
        else:
            self.job.finish(out)


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _host_ok(self) -> bool:
            # Rejects DNS-rebinding: a foreign site resolving its name to
            # 127.0.0.1 would arrive with its own Host header.
            if self.headers.get("Host") == app.host:
                return True
            self._send(HTTPStatus.FORBIDDEN, b"Forbidden", "text/plain")
            return False

        def do_GET(self) -> None:
            if not self._host_ok():
                return
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            if url.path == "/":
                if not app.client.logged_in:
                    return self._redirect("/login")
                return self._send(HTTPStatus.OK, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/login":
                return self._redirect(app.client.authorize_url())
            if url.path == "/callback":
                return self._callback(query)
            if url.path == "/api/playlists":
                return self._api(app.scan)
            if url.path == "/api/status":
                return self._json(HTTPStatus.OK, app.job.snapshot())
            self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")

        def do_POST(self) -> None:
            if not self._host_ok():
                return
            # Requiring a JSON body forces a CORS preflight for cross-origin
            # requests, which this server never answers — so other websites
            # open in the browser can't trigger an export.
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Wymagany JSON."})
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/export":
                body = self._read_json()
                ids = body.get("ids") if isinstance(body, dict) else None
                if not isinstance(ids, list) or not ids:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "Nie wybrano playlist."})
                if not app.start_export([str(i) for i in ids]):
                    return self._json(HTTPStatus.CONFLICT,
                                      {"error": "Eksport już trwa albo wybór jest nieaktualny — odśwież listę."})
                return self._json(HTTPStatus.ACCEPTED, {"ok": True})
            if path == "/api/logout":
                app.client.logout()
                return self._json(HTTPStatus.OK, {"ok": True})
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def _callback(self, query: dict[str, list[str]]) -> None:
            if "error" in query:
                return self._error_page(f"Spotify odrzucił logowanie: {query['error'][0]}")
            try:
                app.client.complete_login(query.get("code", [""])[0], query.get("state", [""])[0])
            except SpotifyError as exc:
                return self._error_page(str(exc))
            self._redirect("/")

        def _api(self, fn) -> None:
            try:
                self._json(HTTPStatus.OK, fn())
            except SpotifyError as exc:
                status = HTTPStatus.UNAUTHORIZED if exc.status == 401 else HTTPStatus.BAD_GATEWAY
                self._json(status, {"error": str(exc)})

        def _read_json(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(length) or b"null")
            except json.JSONDecodeError:
                return None

        def _error_page(self, message: str) -> None:
            body = (f"<!doctype html><meta charset=utf-8><p>{html.escape(message)}</p>"
                    f'<p><a href="/login">Spróbuj ponownie</a></p>').encode()
            self._send(HTTPStatus.BAD_REQUEST, body, "text/html; charset=utf-8")

        def _redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", location)
            self.end_headers()

        def _json(self, status: HTTPStatus, data: Any) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json")

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # keep the terminal for our own messages

    return Handler


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Eksport playlist Spotify do JSON")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--out", type=Path, default=BASE_DIR / "output")
    parser.add_argument("--no-browser", action="store_true", help="nie otwieraj przeglądarki")
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    if not client_id:
        sys.exit("Brak SPOTIFY_CLIENT_ID — skopiuj .env.example do .env i wpisz Client ID (patrz README.md).")

    redirect_uri = f"http://127.0.0.1:{args.port}/callback"
    client = SpotifyClient(client_id, redirect_uri, BASE_DIR / ".token.json")
    app = App(client, args.out.resolve(), f"127.0.0.1:{args.port}")

    # Bind to loopback only: the UI exposes your Spotify session.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"UI: {url}   (Redirect URI w Spotify Dashboard: {redirect_uri})")
    print("Ctrl+C kończy pracę.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
