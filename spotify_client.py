"""Spotify Web API client: PKCE login, token storage and paginated reads.

Targets the post-February-2026 Development Mode API: playlist contents come
from /playlists/{id}/items and each entry carries the track under `item`
(the older `track` key is still read as a fallback).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

import requests

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"
PAGE_LIMIT = 50  # maximum allowed by every paginated endpoint used here
TIMEOUT = (5, 30)  # (connect, read) seconds
MAX_RETRY_AFTER = 60  # longer waits fail fast instead of freezing the UI
MAX_RETRIES = 5


class SpotifyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Spotify API {status}: {message}")
        self.status = status


class SpotifyClient:
    def __init__(self, client_id: str, redirect_uri: str, token_path: Path):
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.token_path = token_path
        self._token: dict[str, Any] | None = self._load_token()
        self._pending_auth: dict[str, str] = {}  # state -> code_verifier
        self._lock = threading.RLock()  # _refresh() calls logout() while holding it
        self._session = requests.Session()

    # --- auth -------------------------------------------------------------

    @property
    def logged_in(self) -> bool:
        return self._token is not None

    def authorize_url(self) -> str:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        self._pending_auth[state] = verifier
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    def complete_login(self, code: str, state: str) -> None:
        verifier = self._pending_auth.pop(state, None)
        if verifier is None:
            raise SpotifyError(400, "Nieznany parametr state — zacznij logowanie od nowa.")
        self._request_token({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": verifier,
        })

    def _refresh(self) -> None:
        refresh_token = (self._token or {}).get("refresh_token")
        if not refresh_token:
            self.logout()
            raise SpotifyError(401, "Sesja wygasła — zaloguj się ponownie.")
        try:
            self._request_token({
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
            }, previous_refresh=refresh_token)
        except SpotifyError as exc:
            # Only a definitive rejection ends the session; a 5xx, 429 or
            # network hiccup must not throw away a still-valid refresh token.
            if exc.status in (400, 401):
                self.logout()
            raise

    def _request_token(self, data: dict[str, str], previous_refresh: str | None = None) -> None:
        try:
            resp = self._session.post(TOKEN_URL, data=data, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise SpotifyError(0, f"Błąd sieci: {exc}") from exc
        if resp.status_code != 200:
            raise SpotifyError(resp.status_code, _error_message(resp))
        token = _json_body(resp)
        token["expires_at"] = time.time() + token.get("expires_in", 3600) - 60
        # Spotify may omit the refresh token on refresh; keep the old one then.
        token.setdefault("refresh_token", previous_refresh)
        self._token = token
        try:
            self._save_token()
        except OSError as exc:
            # The session still works from memory; it just won't survive a restart.
            raise SpotifyError(0, f"Nie udało się zapisać tokenu: {exc}") from exc

    def logout(self) -> None:
        # Under the lock so an in-flight refresh can't re-save the token afterwards.
        with self._lock:
            self._token = None
            self.token_path.unlink(missing_ok=True)

    def _load_token(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.token_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _save_token(self) -> None:
        # The file holds a long-lived refresh token, so keep it owner-only.
        # Written to a temp file and swapped in, so an interrupted write can't
        # truncate it — a rotated refresh token can't be recovered.
        tmp = self.token_path.with_name(self.token_path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        if hasattr(os, "fchmod"):  # absent on Windows before Python 3.13
            os.fchmod(fd, 0o600)  # O_CREAT's mode doesn't apply to an existing file
        with os.fdopen(fd, "w") as f:
            json.dump(self._token, f)
        os.replace(tmp, self.token_path)

    def _access_token(self) -> str:
        with self._lock:
            if self._token is None:
                raise SpotifyError(401, "Nie zalogowano.")
            if time.time() >= self._token.get("expires_at", 0):
                self._refresh()
            return self._token["access_token"]

    # --- API --------------------------------------------------------------

    def get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = path_or_url if path_or_url.startswith("http") else f"{API_URL}{path_or_url}"
        refreshed = False
        for attempt in range(MAX_RETRIES):
            headers = {"Authorization": f"Bearer {self._access_token()}"}
            try:
                resp = self._session.get(url, params=params, headers=headers, timeout=TIMEOUT)
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES - 1:
                    raise SpotifyError(0, f"Błąd sieci: {exc}") from exc
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 200:
                return _json_body(resp)
            if resp.status_code == 401 and not refreshed:
                with self._lock:
                    self._refresh()
                refreshed = True
                continue
            if resp.status_code == 429:
                wait = _retry_after(resp)
                if wait > MAX_RETRY_AFTER:
                    raise SpotifyError(429, f"Limit zapytań Spotify — spróbuj za ok. {wait // 60 + 1} min.")
                time.sleep(wait + 1)
                continue
            if resp.status_code >= 500 and attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            raise SpotifyError(resp.status_code, _error_message(resp))
        raise SpotifyError(429, "Przekroczono limit zapytań — spróbuj później.")

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        # `next` URLs already carry every query parameter, so params apply to page one only.
        page: dict[str, Any] | None = self.get(path, {"limit": PAGE_LIMIT, **(params or {})})
        while page:
            yield from page.get("items") or []
            next_url = page.get("next")
            page = self.get(next_url) if next_url else None

    def me(self) -> dict[str, Any]:
        return self.get("/me")

    def download(self, url: str) -> requests.Response:
        resp = self._session.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp


def _json_body(resp: requests.Response) -> Any:
    # A proxy or captive portal can answer 200 with an HTML page.
    try:
        return resp.json()
    except ValueError as exc:
        raise SpotifyError(resp.status_code, "Spotify zwrócił odpowiedź, która nie jest JSON-em.") from exc


def _retry_after(resp: requests.Response) -> int:
    try:
        return max(0, int(resp.headers.get("Retry-After", "5")))
    except ValueError:
        return 5


def _error_message(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200] or resp.reason
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("message") or json.dumps(err)
    return body.get("error_description") or str(err or body)
