"""app: the local HTTP UI, driven over a real loopback socket.

The server runs in-process on an ephemeral port; only the Spotify transport
is faked, so request routing, the Host / Content-Type guards, the login
flow and the background export thread all run for real.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

import app as app_module
from app import FOLDER_PICKER_TIMEOUT, App, ExportJob, choose_folder, load_dotenv, make_handler
from conftest import ME, make_response, playlist_entry, playlist_meta, track_item
from spotify_client import TOKEN_URL
from exporter import LIKED_SONGS_ID


class Server:
    def __init__(self, application: App) -> None:
        self.app = application
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(application))
        self.port = self.httpd.server_address[1]
        application.host = f"127.0.0.1:{self.port}"
        # Short poll interval: shutdown() otherwise waits up to 0.5s per test.
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01},
                                       daemon=True)
        self.thread.start()

    def request(self, method: str, path: str, body: bytes | str | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post_json(self, path: str, data, **kw):
        body = data if isinstance(data, (bytes, str)) else json.dumps(data)
        return self.request("POST", path, body=body, headers={"Content-Type": "application/json"}, **kw)

    def status(self) -> dict:
        return json.loads(self.get("/api/status")[2])

    def wait_until_idle(self, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = self.status()
            if not snap["running"]:
                return snap
            time.sleep(0.01)
        raise AssertionError(f"export still running after {timeout}s: {snap}")

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def out_root(tmp_path: Path) -> Path:
    return tmp_path / "output"


@pytest.fixture
def make_server(make_client, out_root):
    servers: list[Server] = []

    def factory(logged_in: bool = True, out: Path | None = None) -> Server:
        server = Server(App(make_client(logged_in=logged_in), out or out_root, host="unset"))
        servers.append(server)
        return server

    yield factory
    for s in servers:
        s.close()


@pytest.fixture
def server(make_server) -> Server:
    return make_server()


@pytest.fixture
def library(api):
    """A user with Liked Songs, one owned playlist and one followed playlist."""
    api.on_get("/me", make_response(json_body=ME))
    api.on_get("/me/tracks", make_response(json_body={"items": [], "next": None, "total": 1}))
    api.on_paged("/me/playlists", [
        playlist_meta("own", "Moja", items={"total": 2}),
        playlist_meta("followed", "Hits", owner_id="spotify"),
    ])
    api.add_playlist(playlist_meta("own", "Moja"), [playlist_entry(track_item(1)), playlist_entry(track_item(2))])
    return api


def scan(server: Server) -> list[dict]:
    status, _, body = server.get("/api/playlists")
    assert status == 200
    return json.loads(body)


class FakeOsascript:
    """Stands in for `subprocess.run`: records argv/kwargs, returns a scripted
    CompletedProcess or raises, so no dialog ever opens."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "",
                 raises: BaseException | None = None) -> None:
        self.returncode, self.stdout, self.stderr, self.raises = returncode, stdout, stderr, raises
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)

    @property
    def start_dir(self) -> str:
        return self.calls[-1][0][3]


@pytest.fixture(autouse=True)
def no_real_subprocess(monkeypatch):
    """Backstop: a code path that reaches the real osascript fails the test
    instead of popping a Finder dialog."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"real subprocess attempted: {args}")
    monkeypatch.setattr(subprocess, "run", refuse)


@pytest.fixture
def picker(monkeypatch):
    def install(**kw) -> FakeOsascript:
        fake = FakeOsascript(**kw)
        monkeypatch.setattr(subprocess, "run", fake)
        return fake
    return install


@pytest.fixture
def on_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")


@pytest.fixture
def scratch_dirs(monkeypatch, tmp_path) -> list[Path]:
    """Routes the download mode's mkdtemp into tmp_path and records each dir,
    so a test can check it is gone afterwards."""
    root = tmp_path / "scratch"
    root.mkdir()
    created: list[Path] = []
    real = tempfile.mkdtemp

    def tracking(*args, **kwargs):
        path = real(*args, **{**kwargs, "dir": root})
        created.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", tracking)
    return created


def unzip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


# --- request guards ---------------------------------------------------------------------

class TestHostCheck:
    @pytest.mark.parametrize("host", ["evil.example:8888", "localhost", "127.0.0.1"],
                             ids=["rebinding-domain", "localhost-alias", "missing-port"])
    def test_foreign_host_header_is_forbidden_on_get(self, server, host):
        status, _, body = server.get("/api/status", headers={"Host": host})
        assert (status, body) == (403, b"Forbidden")

    def test_foreign_host_cannot_log_out(self, server, token_path):
        status, _, _ = server.request("POST", "/api/logout", body="{}",
                                      headers={"Host": "evil.example", "Content-Type": "application/json"})
        assert status == 403
        assert token_path.exists()
        assert server.app.client.logged_in


class TestJsonOnlyPosts:
    @pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded",
                                              "multipart/form-data; boundary=x", None],
                             ids=["text-plain", "form", "multipart", "none"])
    def test_cors_simple_request_types_cannot_start_export(self, server, library, content_type):
        scan(server)
        headers = {"Content-Type": content_type} if content_type else {}

        status, _, body = server.request("POST", "/api/export", body=json.dumps({"ids": ["own"]}), headers=headers)

        assert status == 415
        assert "error" in json.loads(body)
        assert server.status()["items"] == []
        assert library.calls_to("GET", "/playlists/own") == []

    def test_cors_simple_request_cannot_log_out(self, server, token_path):
        status, _, _ = server.request("POST", "/api/logout", body="{}", headers={"Content-Type": "text/plain"})
        assert status == 415
        assert token_path.exists()

    def test_json_with_charset_parameter_is_accepted(self, server):
        status, _, _ = server.request("POST", "/api/logout", body="{}",
                                      headers={"Content-Type": "application/json; charset=utf-8"})
        assert status == 200


class TestAntiFramingHeaders:
    @pytest.mark.parametrize(
        "send",
        [
            lambda s: s.get("/"),
            lambda s: s.get("/api/status"),
            lambda s: s.get("/nope"),
            lambda s: s.get("/api/status", headers={"Host": "evil.example"}),
            lambda s: s.get("/callback?error=access_denied"),
            lambda s: s.request("POST", "/api/logout", body="{}", headers={"Content-Type": "text/plain"}),
        ],
        ids=["ui-page", "json-api", "not-found", "forbidden-host", "error-page", "unsupported-media"],
    )
    def test_every_rendered_response_refuses_framing_and_sniffing(self, server, send):
        _, headers, _ = send(server)

        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Content-Security-Policy"] == "frame-ancestors 'none'"
        assert headers["X-Content-Type-Options"] == "nosniff"


# --- pages and login flow -------------------------------------------------------------------

class TestPagesAndLogin:
    def test_root_redirects_to_login_when_logged_out(self, make_server):
        status, headers, _ = make_server(logged_in=False).get("/")
        assert (status, headers["Location"]) == (302, "/login")

    def test_root_serves_ui_when_logged_in(self, server):
        status, headers, body = server.get("/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert headers["Cache-Control"] == "no-store"
        assert body == app_module.INDEX_HTML.read_bytes()

    def test_full_login_round_trip(self, make_server, api, token_path):
        server = make_server(logged_in=False)
        api.on_token(make_response(json_body={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}))

        status, headers, _ = server.get("/login")
        assert status == 302
        authorize = urllib.parse.urlsplit(headers["Location"])
        assert authorize.netloc == "accounts.spotify.com"
        state = urllib.parse.parse_qs(authorize.query)["state"][0]

        status, headers, _ = server.get(f"/callback?code=abc&state={urllib.parse.quote(state)}")

        assert (status, headers["Location"]) == (302, "/")
        assert api.calls_to("POST", TOKEN_URL)[0]["data"]["code"] == "abc"
        assert token_path.exists()
        assert server.get("/")[0] == 200

    def test_denied_login_shows_escaped_error_page(self, make_server, api):
        server = make_server(logged_in=False)

        status, headers, body = server.get("/callback?error=%3Cscript%3Ealert(1)%3C%2Fscript%3E")

        assert status == 400
        assert headers["Content-Type"].startswith("text/html")
        assert b"<script>" not in body
        assert "&lt;script&gt;".encode() in body
        assert api.calls == []

    def test_callback_with_unknown_state_does_not_exchange_code(self, make_server, api):
        server = make_server(logged_in=False)
        server.get("/login")

        status, _, body = server.get("/callback?code=abc&state=forged")

        assert status == 400
        assert "state".encode() in body
        assert api.calls == []

    def test_callback_shows_error_when_code_exchange_fails(self, make_server, api):
        server = make_server(logged_in=False)
        api.on_token(make_response(400, json_body={"error": "invalid_grant", "error_description": "Code expired"}))
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(server.get("/login")[1]["Location"]).query)["state"][0]

        status, _, body = server.get(f"/callback?code=abc&state={state}")

        assert status == 400
        assert b"Code expired" in body
        assert not server.app.client.logged_in

    def test_callback_shows_error_page_when_token_endpoint_is_unreachable(self, make_server, api, token_path):
        server = make_server(logged_in=False)
        api.on_token(requests.ConnectionError("dns failure"))
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(server.get("/login")[1]["Location"]).query)["state"][0]

        status, headers, body = server.get(f"/callback?code=abc&state={state}")

        assert status == 400
        assert headers["Content-Type"].startswith("text/html")
        assert "Błąd sieci".encode() in body
        assert not token_path.exists()

    def test_callback_shows_error_page_when_token_endpoint_returns_non_json(self, make_server, api):
        server = make_server(logged_in=False)
        api.on_token(make_response(body=b"<html>captive portal</html>", headers={"Content-Type": "text/html"}))
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(server.get("/login")[1]["Location"]).query)["state"][0]

        status, _, _ = server.get(f"/callback?code=abc&state={state}")

        assert status == 400

    def test_logout_ends_session(self, server, token_path):
        status, _, body = server.post_json("/api/logout", {})

        assert (status, json.loads(body)) == (200, {"ok": True})
        assert not token_path.exists()
        assert server.get("/")[1]["Location"] == "/login"

    def test_unknown_paths_are_404(self, server):
        assert server.get("/nope")[0] == 404
        status, _, body = server.post_json("/api/nope", {})
        assert status == 404 and "error" in json.loads(body)


# --- /api/playlists -----------------------------------------------------------------------------

class TestPlaylistsApi:
    def test_lists_exportable_playlists(self, server, library):
        assert [e["id"] for e in scan(server)] == [LIKED_SONGS_ID, "own"]

    def test_not_logged_in_maps_to_401(self, make_server):
        status, _, body = make_server(logged_in=False).get("/api/playlists")
        assert status == 401
        assert "error" in json.loads(body)

    def test_spotify_failure_maps_to_502_with_message(self, server, api):
        api.on_get("/me", make_response(404, json_body={"error": {"status": 404, "message": "nope"}}))

        status, _, body = server.get("/api/playlists")

        assert status == 502
        assert json.loads(body)["error"] == "Spotify API 404: nope"

    def test_unreachable_token_endpoint_during_refresh_is_502_and_keeps_session(self, make_client, out_root,
                                                                              api, clock, token_path):
        server = Server(App(make_client(expires_at=clock.now - 1), out_root, host="unset"))
        api.on_token(requests.ConnectionError("connection reset"))
        try:
            status, _, body = server.get("/api/playlists")

            assert status == 502
            assert "Błąd sieci" in json.loads(body)["error"]
            assert token_path.exists()
            assert server.get("/")[0] == 200, "a network blip must not log the user out"
        finally:
            server.close()

    def test_revoked_refresh_token_during_scan_logs_out(self, make_client, out_root, api, clock, token_path):
        server = Server(App(make_client(expires_at=clock.now - 1), out_root, host="unset"))
        api.on_token(make_response(400, json_body={"error": "invalid_grant", "error_description": "revoked"}))
        try:
            assert server.get("/api/playlists")[0] == 502
            assert not token_path.exists()
            assert server.get("/")[1]["Location"] == "/login"
        finally:
            server.close()

    def test_long_rate_limit_answers_quickly_with_a_wait_hint(self, server, api, clock):
        api.on_get("/me", make_response(429, headers={"Retry-After": "600"}))

        status, _, body = server.get("/api/playlists")

        assert status == 502
        assert "11 min" in json.loads(body)["error"]
        assert clock.sleeps == []

    def test_non_json_api_body_maps_to_502(self, server, api):
        api.on_get("/me", make_response(body=b"<html>captive portal</html>", headers={"Content-Type": "text/html"}))

        assert server.get("/api/playlists")[0] == 502


# --- /api/export + /api/status --------------------------------------------------------------------

class TestExportApi:
    def test_export_runs_in_background_and_reports_status(self, server, library, out_root):
        scan(server)

        status, _, body = server.post_json("/api/export", {"ids": ["own", LIKED_SONGS_ID]})
        assert (status, json.loads(body)) == (202, {"ok": True})

        snap = server.wait_until_idle()

        assert snap["error"] is None
        items = {i["id"]: i for i in snap["items"]}
        assert items["own"] == {"id": "own", "name": "Moja", "status": "done", "detail": "2 utwory"}
        assert items[LIKED_SONGS_ID]["status"] == "done"
        out = Path(snap["output_dir"])
        assert out.parent == out_root
        assert (out / "Moja" / "playlist.json").is_file()
        assert (out / "index.json").is_file()

    @pytest.mark.parametrize(
        "body",
        [b"{not json", b"[]", {}, {"ids": []}, {"ids": "own"}, {"ids": None}],
        ids=["invalid-json", "array-body", "no-ids", "empty-ids", "ids-not-list", "ids-null"],
    )
    def test_malformed_request_is_400(self, server, library, body):
        scan(server)
        status, _, resp = server.post_json("/api/export", body)
        assert status == 400
        assert "error" in json.loads(resp)
        assert server.status()["items"] == []

    def test_ids_not_from_last_scan_are_rejected(self, server, library):
        scan(server)

        status, _, _ = server.post_json("/api/export", {"ids": ["followed", "someone-elses"]})

        assert status == 409
        assert server.status() == {"running": False, "mode": "folder", "output_dir": None,
                                   "download_ready": False, "error": None, "items": []}
        assert library.calls_to("GET", "/playlists/followed") == []

    def test_unknown_ids_are_dropped_from_a_valid_selection(self, server, library):
        scan(server)

        assert server.post_json("/api/export", {"ids": ["own", "followed"]})[0] == 202

        assert [i["id"] for i in server.wait_until_idle()["items"]] == ["own"]

    def test_export_without_prior_scan_is_rejected(self, server):
        assert server.post_json("/api/export", {"ids": ["own"]})[0] == 409

    def test_second_export_while_running_is_409(self, server, library):
        scan(server)
        release, entered = threading.Event(), threading.Event()
        meta = make_response(json_body=playlist_meta("own", "Moja"))

        def slow_meta():
            entered.set()
            release.wait(5)
            return meta

        library.on_get("/playlists/own", slow_meta)

        try:
            assert server.post_json("/api/export", {"ids": ["own"]})[0] == 202
            assert entered.wait(5)
            assert server.status()["running"] is True

            assert server.post_json("/api/export", {"ids": ["own"]})[0] == 409
        finally:
            release.set()
        assert server.wait_until_idle()["items"][0]["status"] == "done"
        assert len(library.calls_to("GET", "/playlists/own")) == 1

    def test_unwritable_output_dir_finishes_job_with_error(self, make_server, library, tmp_path):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        server = make_server(out=blocker)
        scan(server)

        assert server.post_json("/api/export", {"ids": ["own"]})[0] == 202
        snap = server.wait_until_idle()

        assert snap["output_dir"] is None
        assert snap["error"]

    def test_a_new_export_can_start_after_one_finishes(self, server, library):
        scan(server)
        server.post_json("/api/export", {"ids": ["own"]})
        server.wait_until_idle()

        assert server.post_json("/api/export", {"ids": [LIKED_SONGS_ID]})[0] == 202

        snap = server.wait_until_idle()
        assert [i["id"] for i in snap["items"]] == [LIKED_SONGS_ID]


# --- export destinations: folder vs ZIP download ------------------------------------------------

RUN_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}")
NO_SUCH_USER = "no-such-user-spotify-export"  # `~NO_SUCH_USER` makes expanduser raise RuntimeError


class TestExportDestination:
    def test_custom_absolute_folder_receives_the_export(self, server, library, out_root, tmp_path):
        target = tmp_path / "picked" / "not-yet-created"
        scan(server)

        assert server.post_json("/api/export", {"ids": ["own"], "mode": "folder", "folder": str(target)})[0] == 202
        snap = server.wait_until_idle()

        out = Path(snap["output_dir"])
        assert out.parent == target
        assert (out / "Moja" / "playlist.json").is_file()
        assert not out_root.exists(), "the default folder must stay untouched"

    def test_tilde_folder_is_expanded_to_home(self, server, library, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        scan(server)

        assert server.post_json("/api/export", {"ids": ["own"], "folder": "~/Eksporty"})[0] == 202

        assert Path(server.wait_until_idle()["output_dir"]).parent == home / "Eksporty"

    @pytest.mark.parametrize("body", [{"ids": ["own"]}, {"ids": ["own"], "folder": None},
                                      {"ids": ["own"], "folder": ""}, {"ids": ["own"], "mode": "folder"}],
                             ids=["no-mode-no-folder", "folder-null", "folder-empty", "folder-mode-no-folder"])
    def test_missing_folder_falls_back_to_default(self, server, library, out_root, body):
        scan(server)

        assert server.post_json("/api/export", body)[0] == 202
        snap = server.wait_until_idle()

        assert snap["mode"] == "folder"
        assert Path(snap["output_dir"]).parent == out_root

    def test_custom_folder_that_is_a_file_finishes_with_error(self, server, library, tmp_path):
        blocker = tmp_path / "a-file"
        blocker.write_text("")
        scan(server)

        assert server.post_json("/api/export", {"ids": ["own"], "folder": str(blocker)})[0] == 202
        snap = server.wait_until_idle()

        assert snap["output_dir"] is None
        assert snap["error"]

    @pytest.mark.parametrize(
        "extra",
        [{"mode": "zip"}, {"mode": None}, {"mode": ["folder"]}, {"mode": "FOLDER"},
         {"folder": "output"}, {"folder": "./elsewhere"}, {"mode": "folder", "folder": 123}],
        ids=["unknown-mode", "mode-null", "mode-list", "mode-wrong-case",
             "relative-folder", "dot-relative-folder", "folder-not-a-string"],
    )
    def test_invalid_mode_or_folder_is_400_and_starts_nothing(self, server, library, out_root, tmp_path, extra):
        scan(server)

        status, _, body = server.post_json("/api/export", {"ids": ["own"], **extra})

        assert status == 400
        assert "error" in json.loads(body)
        assert server.status()["items"] == []
        assert library.calls_to("GET", "/playlists/own") == []
        assert not out_root.exists()
        assert not (tmp_path / "output").exists() and not (tmp_path / "elsewhere").exists()

    # expanduser raises on these (RuntimeError / ValueError); unhandled, the
    # handler thread died and the browser saw a dropped connection, not a 400.
    @pytest.mark.parametrize("folder", [f"~{NO_SUCH_USER}/Eksporty", "~\x00/Eksporty", "~ja\x00n/Eksporty"],
                             ids=["unknown-user", "nul-as-user", "nul-inside-user"])
    def test_unexpandable_folder_is_400_and_starts_nothing(self, server, library, folder):
        scan(server)

        status, _, body = server.post_json("/api/export", {"ids": ["own"], "mode": "folder", "folder": folder})

        assert (status, json.loads(body)) == (400, {"error": "Podaj pełną ścieżkę folderu."})
        assert server.status()["items"] == []
        assert library.calls_to("GET", "/playlists/own") == []

    @pytest.mark.xfail(strict=True, reason="BUG: a NUL byte outside a ~user prefix passes validation; "
                                           "the export thread then fails on mkdir instead of a 400")
    def test_nul_byte_in_absolute_folder_is_400(self, server, library, tmp_path, monkeypatch):
        monkeypatch.setattr(app_module.traceback, "print_exc", lambda: None)
        scan(server)

        status, _, _ = server.post_json("/api/export", {"ids": ["own"], "folder": f"{tmp_path}/a\x00b"})
        server.wait_until_idle()

        assert status == 400


class TestDownloadMode:
    def test_zip_is_built_offered_and_nothing_is_left_on_disk(self, server, library, out_root, scratch_dirs):
        scan(server)

        assert server.post_json("/api/export", {"ids": ["own", LIKED_SONGS_ID], "mode": "download",
                                                "folder": None})[0] == 202
        snap = server.wait_until_idle()

        assert snap["error"] is None
        assert (snap["mode"], snap["output_dir"], snap["download_ready"]) == ("download", None, True)
        assert {i["status"] for i in snap["items"]} == {"done"}
        assert not out_root.exists()
        assert len(scratch_dirs) == 1 and not scratch_dirs[0].exists(), "temp export dir must be removed"

        status, headers, data = server.get("/api/download")

        assert status == 200
        assert headers["Content-Type"] == "application/zip"
        filename = re.fullmatch(r'attachment; filename="(spotify-export_(.+)\.zip)"; '
                                r"filename\*=UTF-8''(.+)", headers["Content-Disposition"])
        assert filename and filename.group(1) == filename.group(3)
        stamp = filename.group(2)
        assert RUN_STAMP.fullmatch(stamp)
        files = unzip(data)
        assert set(files) == {f"{stamp}/index.json", f"{stamp}/Moja/playlist.json",
                              f"{stamp}/Polubione utwory/playlist.json"}
        assert len(json.loads(files[f"{stamp}/Moja/playlist.json"])["tracks"]) == 2

    def test_download_mode_ignores_a_folder_in_the_request(self, server, library, tmp_path, scratch_dirs):
        elsewhere = tmp_path / "should-not-appear"
        scan(server)

        assert server.post_json("/api/export", {"ids": ["own"], "mode": "download",
                                                "folder": str(elsewhere)})[0] == 202

        assert server.wait_until_idle()["download_ready"] is True
        assert not elsewhere.exists()

    def test_zip_can_be_fetched_again(self, server, library, scratch_dirs):
        scan(server)
        server.post_json("/api/export", {"ids": ["own"], "mode": "download"})
        server.wait_until_idle()

        first, second = server.get("/api/download"), server.get("/api/download")

        assert first[0] == second[0] == 200
        assert first[2] == second[2]

    def test_zip_failure_reports_error_and_still_removes_temp_dir(self, server, library, scratch_dirs,
                                                                  monkeypatch):
        def disk_full(run_dir):
            assert run_dir.is_dir()
            raise OSError("No space left on device")

        monkeypatch.setattr(app_module.exporter, "zip_dir", disk_full)
        scan(server)

        server.post_json("/api/export", {"ids": ["own"], "mode": "download"})
        snap = server.wait_until_idle()

        assert snap["error"] == "No space left on device"
        assert snap["download_ready"] is False
        assert server.get("/api/download")[0] == 404
        assert len(scratch_dirs) == 1 and not scratch_dirs[0].exists()

    def test_unexpected_error_still_removes_temp_dir_and_ends_job(self, client, out_root, scratch_dirs,
                                                                  monkeypatch):
        def explode(client, entries, root, progress):
            (root / "partial.json").write_text("{}")
            raise ValueError("unexpected payload")

        monkeypatch.setattr(app_module.exporter, "export", explode)
        monkeypatch.setattr(app_module.traceback, "print_exc", lambda: None)
        application = App(client, out_root, host="unused")
        entries = [{"id": "a", "name": "A", "kind": "playlist"}]
        application.job.start(entries, "download")

        application._run_download(entries)

        snap = application.job.snapshot()
        assert (snap["running"], snap["download_ready"]) == (False, False)
        assert "unexpected payload" in snap["error"]
        assert len(scratch_dirs) == 1 and not scratch_dirs[0].exists()

    def test_temp_dir_creation_failure_does_not_leave_job_running(self, client, out_root, monkeypatch):
        def no_tmp(*args, **kwargs):
            raise OSError("Read-only file system")

        monkeypatch.setattr(tempfile, "mkdtemp", no_tmp)
        application = App(client, out_root, host="unused")
        entries = [{"id": "a", "name": "A", "kind": "playlist"}]
        application.job.start(entries, "download")

        with contextlib.suppress(OSError):
            application._run_download(entries)

        snap = application.job.snapshot()
        assert snap["running"] is False
        assert snap["error"]


class TestDownloadApi:
    def test_nothing_to_download_before_any_export(self, server):
        status, headers, body = server.get("/api/download")
        assert status == 404
        assert headers["Content-Type"] == "application/json"
        assert "error" in json.loads(body)

    def test_folder_export_offers_no_download(self, server, library):
        scan(server)
        server.post_json("/api/export", {"ids": ["own"]})
        assert server.wait_until_idle()["download_ready"] is False

        assert server.get("/api/download")[0] == 404

    def test_starting_a_new_export_withdraws_the_previous_zip(self, server, library, scratch_dirs):
        scan(server)
        server.post_json("/api/export", {"ids": ["own"], "mode": "download"})
        server.wait_until_idle()
        assert server.get("/api/download")[0] == 200

        server.post_json("/api/export", {"ids": ["own"], "mode": "folder"})
        snap = server.wait_until_idle()

        assert (snap["mode"], snap["download_ready"]) == ("folder", False)
        assert server.get("/api/download")[0] == 404

    def test_foreign_host_cannot_fetch_the_zip(self, server, library, scratch_dirs):
        scan(server)
        server.post_json("/api/export", {"ids": ["own"], "mode": "download"})
        server.wait_until_idle()

        status, _, body = server.get("/api/download", headers={"Host": "evil.example:8888"})

        assert (status, body) == (403, b"Forbidden")


# --- /api/config --------------------------------------------------------------------------------

class TestConfigApi:
    @pytest.mark.parametrize(("platform", "can_choose"), [("darwin", True), ("linux", False), ("win32", False)])
    def test_reports_default_folder_and_picker_availability(self, server, out_root, monkeypatch,
                                                            platform, can_choose):
        monkeypatch.setattr(sys, "platform", platform)

        status, _, body = server.get("/api/config")

        assert status == 200
        assert json.loads(body) == {"default_folder": str(out_root), "can_choose_folder": can_choose}


# --- /api/choose-folder (macOS picker, faked) ---------------------------------------------------

class TestChooseFolderApi:
    def test_returns_the_picked_folder_without_trailing_slash(self, server, picker, on_macos, tmp_path):
        current = tmp_path / "current"
        current.mkdir()
        fake = picker(stdout="/Users/me/Music/Eksport/\n")

        status, _, body = server.post_json("/api/choose-folder", {"current": str(current)})

        assert (status, json.loads(body)) == (200, {"path": "/Users/me/Music/Eksport"})
        argv, kwargs = fake.calls[0]
        assert argv[:2] == ["osascript", "-e"] and argv[3] == str(current)
        assert kwargs == {"capture_output": True, "text": True, "timeout": FOLDER_PICKER_TIMEOUT}

    def test_cancel_returns_null_path(self, server, picker, on_macos):
        picker(returncode=1, stderr="0:180: execution error: User canceled. (-128)\n")

        status, _, body = server.post_json("/api/choose-folder", {})

        assert (status, json.loads(body)) == (200, {"path": None})

    @pytest.mark.parametrize(
        ("fake_kw", "message"),
        [({"returncode": 1, "stderr": "execution error: Not authorized to send Apple events (-1743)\n"},
          "Not authorized to send Apple events (-1743)"),
         ({"returncode": 1, "stderr": ""}, "osascript failed"),
         ({"raises": subprocess.TimeoutExpired("osascript", FOLDER_PICKER_TIMEOUT)}, "timed out"),
         ({"raises": FileNotFoundError(2, "No such file or directory", "osascript")}, "No such file")],
        ids=["osascript-error", "error-without-stderr", "dialog-timeout", "osascript-missing"],
    )
    def test_picker_failures_are_500_with_reason(self, server, picker, on_macos, fake_kw, message):
        picker(**fake_kw)

        status, _, body = server.post_json("/api/choose-folder", {})

        assert status == 500
        error = json.loads(body)["error"]
        assert error.startswith("Nie udało się otworzyć okna: ")
        assert message in error

    def test_not_on_macos_is_501_and_runs_nothing(self, server, picker, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        fake = picker()

        status, _, body = server.post_json("/api/choose-folder", {})

        assert status == 501
        assert "error" in json.loads(body)
        assert fake.calls == []

    @pytest.mark.parametrize("body", [{}, {"current": None}, {"current": ""}, b"{not json", b"[]"],
                             ids=["no-current", "current-null", "current-empty", "invalid-json", "array-body"])
    def test_without_current_folder_starts_from_nearest_existing_default(self, server, picker, on_macos,
                                                                         out_root, tmp_path, body):
        assert not out_root.exists()
        fake = picker(stdout="/x\n")

        assert server.post_json("/api/choose-folder", body)[0] == 200

        assert fake.start_dir == str(tmp_path)

    # The saved folder is only a start hint: one expanduser rejects falls back
    # to the default instead of dropping the connection.
    @pytest.mark.parametrize("current", [f"~{NO_SUCH_USER}/Muzyka", "~\x00/Muzyka"],
                             ids=["unknown-user", "nul-as-user"])
    def test_unexpandable_current_folder_starts_from_default(self, server, picker, on_macos,
                                                             out_root, tmp_path, current):
        out_root.mkdir()
        fake = picker(stdout="/x\n")

        status, _, body = server.post_json("/api/choose-folder", {"current": current})

        assert (status, json.loads(body)) == (200, {"path": "/x"})
        assert fake.start_dir == str(out_root)

    def test_tilde_current_folder_starts_from_home(self, server, picker, on_macos, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / "Muzyka").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        fake = picker(stdout="/x\n")

        assert server.post_json("/api/choose-folder", {"current": "~/Muzyka"})[0] == 200

        assert fake.start_dir == str(home / "Muzyka")

    def test_deleted_saved_folder_starts_from_its_nearest_existing_parent(self, server, picker, on_macos,
                                                                          tmp_path):
        fake = picker(stdout="/x\n")

        server.post_json("/api/choose-folder", {"current": str(tmp_path / "gone" / "deeper")})

        assert fake.start_dir == str(tmp_path)

    @pytest.mark.parametrize(
        "send",
        [lambda s: s.request("POST", "/api/choose-folder", body="{}", headers={"Content-Type": "text/plain"}),
         lambda s: s.request("POST", "/api/choose-folder", body="{}",
                             headers={"Content-Type": "application/json", "Host": "evil.example"}),
         lambda s: s.get("/api/choose-folder")],
        ids=["cors-simple-post", "foreign-host", "get"],
    )
    def test_other_sites_cannot_pop_the_dialog(self, server, picker, on_macos, send):
        fake = picker()

        assert send(server)[0] in (403, 404, 415)

        assert fake.calls == []


class TestChooseFolder:
    def test_path_is_passed_as_argument_never_spliced_into_script(self, picker, tmp_path):
        benign = tmp_path / "plain"
        hostile = tmp_path / 'x" & (do shell script "touch pwned") & "'
        benign.mkdir()
        hostile.mkdir()
        fake = picker(stdout="/x\n")

        choose_folder(benign)
        choose_folder(hostile)

        (argv_benign, _), (argv_hostile, _) = fake.calls
        assert argv_hostile[3] == str(hostile)
        assert argv_benign[2] == argv_hostile[2], "script source must not depend on the path"
        assert "do shell script" not in argv_hostile[2]

    def test_unreachable_start_walks_up_to_filesystem_root(self, picker, monkeypatch):
        fake = picker(stdout="/picked\n")
        monkeypatch.setattr(Path, "is_dir", lambda self: str(self) == "/")

        assert choose_folder(Path("/a/b/c")) == Path("/picked")
        assert fake.start_dir == "/"

    def test_cancel_is_none(self, picker, tmp_path):
        picker(returncode=1, stderr="execution error: User canceled. (-128)")
        assert choose_folder(tmp_path) is None

    def test_other_failure_raises_oserror_with_stderr(self, picker, tmp_path):
        picker(returncode=1, stderr="  boom (-1700)  \n")
        with pytest.raises(OSError, match=r"^boom \(-1700\)$"):
            choose_folder(tmp_path)


# --- /api/shutdown --------------------------------------------------------------------------------

class TestShutdownApi:
    def test_answers_then_requests_shutdown_once(self, server):
        # The handler replies first and calls the hook afterwards, so the hook
        # may still be pending when the response arrives.
        calls, called = [], threading.Event()
        server.app.request_shutdown = lambda: (calls.append("stop"), called.set())

        status, _, body = server.post_json("/api/shutdown", {})

        assert (status, json.loads(body)) == (200, {"ok": True})
        assert called.wait(5)
        assert calls == ["stop"]

    def test_confirmation_reaches_the_browser_while_shutdown_is_still_running(self, server):
        # The hook blocks longer than the client's 5 s timeout: had the server
        # stopped before replying, this request would time out.
        entered, release = threading.Event(), threading.Event()

        def slow_stop():
            entered.set()
            release.wait(10)

        server.app.request_shutdown = slow_stop
        try:
            status, _, body = server.post_json("/api/shutdown", {})
            assert entered.wait(5)
        finally:
            release.set()
        assert (status, json.loads(body)) == (200, {"ok": True})

    @pytest.mark.parametrize("mode", ["folder", "download"])
    def test_refused_while_an_export_runs_and_allowed_once_it_ends(self, server, library, scratch_dirs, mode):
        # Stopping mid-run would leave a partial folder or an orphaned temp dir.
        calls = []
        server.app.request_shutdown = lambda: calls.append("stop")
        release, entered = threading.Event(), threading.Event()
        meta = make_response(json_body=playlist_meta("own", "Moja"))

        def slow_meta():
            entered.set()
            release.wait(5)
            return meta

        library.on_get("/playlists/own", slow_meta)
        scan(server)
        try:
            assert server.post_json("/api/export", {"ids": ["own"], "mode": mode})[0] == 202
            assert entered.wait(5)

            status, _, body = server.post_json("/api/shutdown", {})

            assert status == 409
            assert "Trwa eksport" in json.loads(body)["error"]
            assert calls == []
        finally:
            release.set()

        snap = server.wait_until_idle()
        assert (snap["error"], snap["items"][0]["status"]) == (None, "done"), "the refused stop must not abort the run"
        assert server.post_json("/api/shutdown", {})[0] == 200

    @pytest.mark.parametrize(
        "send",
        [lambda s: s.request("POST", "/api/shutdown", body="{}", headers={"Content-Type": "text/plain"}),
         lambda s: s.request("POST", "/api/shutdown", body="{}",
                             headers={"Content-Type": "application/json", "Host": "evil.example"}),
         lambda s: s.get("/api/shutdown")],
        ids=["cors-simple-post", "foreign-host", "get"],
    )
    def test_other_sites_cannot_stop_the_server(self, server, send):
        calls = []
        server.app.request_shutdown = lambda: calls.append("stop")

        assert send(server)[0] in (403, 404, 415)

        assert calls == []


# --- job state (unit) -----------------------------------------------------------------------------

class TestExportJob:
    def test_only_one_run_at_a_time(self):
        job = ExportJob()
        assert job.start([{"id": "a", "name": "A"}])
        assert not job.start([{"id": "b", "name": "B"}])
        assert [i["id"] for i in job.snapshot()["items"]] == ["a"]

    def test_restart_clears_previous_result(self, tmp_path):
        job = ExportJob()
        job.start([{"id": "a", "name": "A"}])
        job.progress("a", "error", "boom")
        job.finish(None, "failed")

        assert job.start([{"id": "b", "name": "B"}])

        assert job.snapshot() == {"running": True, "mode": "folder", "output_dir": None,
                                  "download_ready": False, "error": None,
                                  "items": [{"id": "b", "name": "B", "status": "pending", "detail": None}]}

    def test_download_run_exposes_mode_and_readiness(self):
        job = ExportJob()
        job.start([{"id": "a", "name": "A"}], "download")
        assert job.snapshot()["mode"] == "download"
        assert job.snapshot()["download_ready"] is False

        job.finish(None, download=("spotify-export_x.zip", b"PK\x05\x06"))

        snap = job.snapshot()
        assert (snap["running"], snap["mode"], snap["output_dir"], snap["download_ready"], snap["error"]) == \
            (False, "download", None, True, None)
        assert job.download == ("spotify-export_x.zip", b"PK\x05\x06")

    def test_restart_drops_previous_download_and_resets_mode(self):
        job = ExportJob()
        job.start([{"id": "a", "name": "A"}], "download")
        job.finish(None, download=("x.zip", b"PK"))

        assert job.start([{"id": "b", "name": "B"}])

        assert job.download is None
        assert (job.snapshot()["mode"], job.snapshot()["download_ready"]) == ("folder", False)

    def test_refused_start_keeps_running_job_mode(self):
        job = ExportJob()
        job.start([{"id": "a", "name": "A"}], "download")

        assert not job.start([{"id": "b", "name": "B"}], "folder")

        assert job.snapshot()["mode"] == "download"

    def test_unexpected_error_in_export_does_not_leave_job_running(self, client, out_root, monkeypatch):
        def explode(*args, **kwargs):
            raise ValueError("unexpected payload")

        monkeypatch.setattr(app_module.exporter, "export", explode)
        application = App(client, out_root, host="unused")
        entries = [{"id": "a", "name": "A", "kind": "playlist"}]
        application.job.start(entries)

        # Run the thread target inline so the outcome is deterministic and no
        # unhandled-thread-exception noise reaches the test output.
        with contextlib.suppress(Exception):
            application._run_export(entries)

        snap = application.job.snapshot()
        assert snap["running"] is False
        assert snap["error"]


# --- .env loading -----------------------------------------------------------------------------------

class TestLoadDotenv:
    KEYS = ("SPOTIFY_CLIENT_ID", "QUOTED", "SINGLE", "no_equals_line")

    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        # setenv-then-delenv registers each key with monkeypatch, so whatever
        # load_dotenv writes is undone and a real SPOTIFY_CLIENT_ID is restored.
        for key in self.KEYS:
            monkeypatch.setenv(key, "placeholder")
            monkeypatch.delenv(key)

    def loaded(self) -> dict[str, str]:
        return {k: os.environ[k] for k in self.KEYS if k in os.environ}

    def test_parses_values_comments_and_quotes(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text(
            "# comment\n\nSPOTIFY_CLIENT_ID = abc123 \nQUOTED=\"with space\"\nSINGLE='x=y'\nno_equals_line\n",
            encoding="utf-8")

        load_dotenv(path)

        assert self.loaded() == {"SPOTIFY_CLIENT_ID": "abc123", "QUOTED": "with space", "SINGLE": "x=y"}

    def test_real_environment_wins_over_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "from-shell")
        path = tmp_path / ".env"
        path.write_text("SPOTIFY_CLIENT_ID=from-file\n", encoding="utf-8")

        load_dotenv(path)

        assert os.environ["SPOTIFY_CLIENT_ID"] == "from-shell"

    def test_missing_file_is_ignored(self, tmp_path):
        load_dotenv(tmp_path / "absent.env")
        assert self.loaded() == {}
