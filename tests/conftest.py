"""Shared test doubles: an in-memory Spotify API and a controllable clock.

The fake replaces only the transport (`SpotifyClient._session`), so the real
client code — auth headers, retries, refresh, pagination, error parsing — runs
in every test. Responses are real `requests.Response` objects, so `.json()`,
`.text` and `.raise_for_status()` behave exactly as they do against Spotify.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest
import requests
from requests.structures import CaseInsensitiveDict

import spotify_client
from spotify_client import API_URL, TOKEN_URL, SpotifyClient

REDIRECT_URI = "http://127.0.0.1:8888/callback"
CLIENT_ID = "test-client-id"
ME = {"id": "me-user", "display_name": "Ja"}


def make_response(
    status: int = 200,
    json_body: Any = None,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    reason: str = "",
) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp.reason = reason or {200: "OK", 404: "Not Found", 500: "Internal Server Error"}.get(status, "")
    resp.headers = CaseInsensitiveDict(headers or {})
    if json_body is not None:
        resp._content = json.dumps(json_body).encode()
        resp.headers.setdefault("Content-Type", "application/json")
    else:
        resp._content = body or b""
    resp.encoding = "utf-8"
    return resp


class FakeSpotifyAPI:
    """Stands in for `requests.Session`; routes are scripted per (method, url).

    Each route holds a queue of responses; the last one repeats once the queue
    is down to it. A queued Exception is raised (network failure), a callable
    is invoked at request time (lets a test block or count). An unrouted URL
    raises AssertionError so a test can never pass by silently hitting nothing.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[Any]] = {}
        self.calls: list[dict[str, Any]] = []

    # --- scripting --------------------------------------------------------

    def route(self, method: str, url: str, *responses: Any) -> None:
        full = url if url.startswith("http") else f"{API_URL}{url}"
        self.routes[(method, full)] = list(responses)

    def on_get(self, url: str, *responses: Any) -> None:
        self.route("GET", url, *responses)

    def on_token(self, *responses: Any) -> None:
        self.route("POST", TOKEN_URL, *responses)

    def on_paged(self, path: str, items: list[Any], page_size: int = 2) -> None:
        """Serve `items` the way Spotify pages them: first page at `path`
        (requested with ?limit), later pages via absolute `next` URLs."""
        chunks = [items[i:i + page_size] for i in range(0, len(items), page_size)] or [[]]
        for n, chunk in enumerate(chunks):
            url = f"{API_URL}{path}" if n == 0 else f"{API_URL}{path}?offset={n * page_size}&limit=50"
            nxt = f"{API_URL}{path}?offset={(n + 1) * page_size}&limit=50" if n + 1 < len(chunks) else None
            self.on_get(url, make_response(json_body={"items": chunk, "next": nxt, "total": len(items)}))

    def add_playlist(
        self,
        playlist: dict[str, Any],
        items: list[Any],
        images: list[dict[str, Any]] | None = None,
        image_bytes: bytes = b"\x89PNG-fake",
        image_type: str = "image/png",
    ) -> None:
        pid = playlist["id"]
        self.on_get(f"/playlists/{pid}", make_response(json_body=playlist))
        self.on_paged(f"/playlists/{pid}/items", items)
        self.on_get(f"/playlists/{pid}/images", make_response(json_body=images or []))
        for img in images or []:
            self.on_get(img["url"], make_response(body=image_bytes, headers={"Content-Type": image_type}))

    # --- inspection -------------------------------------------------------

    def calls_to(self, method: str, url: str) -> list[dict[str, Any]]:
        full = url if url.startswith("http") else f"{API_URL}{url}"
        return [c for c in self.calls if c["method"] == method and c["url"] == full]

    # --- requests.Session interface --------------------------------------

    def get(self, url: str, params=None, headers=None, timeout=None) -> requests.Response:
        return self._dispatch("GET", url, params=params, headers=headers or {}, timeout=timeout)

    def post(self, url: str, data=None, timeout=None) -> requests.Response:
        return self._dispatch("POST", url, data=data, timeout=timeout)

    def _dispatch(self, method: str, url: str, **kw: Any) -> requests.Response:
        self.calls.append({"method": method, "url": url, **kw})
        queue = self.routes.get((method, url))
        if not queue:
            raise AssertionError(f"unrouted request: {method} {url}")
        nxt = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(nxt, Exception):
            raise nxt
        if callable(nxt):
            return nxt()
        return nxt


class FakeClock:
    """Replaces the `time` module inside spotify_client: no real sleeping,
    token expiry controlled by the test, sleeps recorded for backoff asserts."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backstop: if any code path bypasses the fake transport, fail instead of
    reaching api.spotify.com."""
    def refuse(self, method, url, *args, **kwargs):
        raise AssertionError(f"real HTTP attempted: {method} {url}")
    monkeypatch.setattr(requests.Session, "request", refuse)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(spotify_client, "time", fake)
    return fake


@pytest.fixture
def api() -> FakeSpotifyAPI:
    return FakeSpotifyAPI()


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    return tmp_path / ".token.json"


def write_token(path: Path, clock: FakeClock, **overrides: Any) -> dict[str, Any]:
    token = {"access_token": "AT-1", "refresh_token": "RT-1", "token_type": "Bearer",
             "expires_at": clock.now + 3600, **overrides}
    path.write_text(json.dumps(token))
    return token


@pytest.fixture
def make_client(api: FakeSpotifyAPI, clock: FakeClock, token_path: Path) -> Callable[..., SpotifyClient]:
    def factory(logged_in: bool = True, **token_overrides: Any) -> SpotifyClient:
        if logged_in:
            write_token(token_path, clock, **token_overrides)
        client = SpotifyClient(CLIENT_ID, REDIRECT_URI, token_path)
        client._session = api  # the transport seam; everything above it is real
        return client
    return factory


@pytest.fixture
def client(make_client) -> SpotifyClient:
    return make_client()


# --- Spotify payload builders ---------------------------------------------

def track_item(n: int, **overrides: Any) -> dict[str, Any]:
    item = {
        "type": "track",
        "name": f"Song {n}",
        "artists": [{"name": f"Artist {n}"}, {"name": "Feat"}],
        "album": {"name": f"Album {n}", "release_date": "2020-01-01"},
        "duration_ms": 200_000 + n,
        "explicit": False,
        "external_ids": {"isrc": f"ISRC{n:05d}"},
        "uri": f"spotify:track:t{n}",
        "external_urls": {"spotify": f"https://open.spotify.com/track/t{n}"},
        "is_local": False,
    }
    item.update(overrides)
    return item


def playlist_entry(item: dict[str, Any] | None, key: str = "item", **overrides: Any) -> dict[str, Any]:
    entry = {key: item, "added_at": "2024-05-01T10:00:00Z", "added_by": {"id": "adder"}, "is_local": False}
    entry.update(overrides)
    return entry


def playlist_meta(pid: str, name: str, owner_id: str = ME["id"], **overrides: Any) -> dict[str, Any]:
    meta = {
        "id": pid,
        "name": name,
        "description": "",
        "external_urls": {"spotify": f"https://open.spotify.com/playlist/{pid}"},
        "uri": f"spotify:playlist:{pid}",
        "owner": {"id": owner_id, "display_name": owner_id.title()},
        "collaborative": False,
        "public": True,
        "snapshot_id": "snap-1",
        "items": {"total": 0},
        "images": [],
    }
    meta.update(overrides)
    return meta
