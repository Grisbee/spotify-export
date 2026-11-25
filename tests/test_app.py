"""app: the local HTTP UI, driven over a real loopback socket.

The server runs in-process on an ephemeral port; only the Spotify transport
is faked, so request routing, the Host / Content-Type guards, the login
flow and the background export thread all run for real.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import app as app_module
from app import App, ExportJob, load_dotenv, make_handler
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
        assert server.status() == {"running": False, "output_dir": None, "error": None, "items": []}
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

        assert job.snapshot() == {"running": True, "output_dir": None, "error": None,
                                  "items": [{"id": "b", "name": "B", "status": "pending", "detail": None}]}

    @pytest.mark.xfail(strict=True, reason=(
        "BUG: App._run_export only catches SpotifyError/OSError; any other exception "
        "(e.g. ValueError from a non-JSON 200 body, KeyError on a malformed payload) kills "
        "the thread before job.finish(), so the job stays running and every later export "
        "gets 409 until the app is restarted"))
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
