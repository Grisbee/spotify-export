"""SpotifyClient: PKCE login, token persistence/refresh, retrying GET, pagination."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import threading
import urllib.parse

import pytest
import requests

from conftest import CLIENT_ID, REDIRECT_URI, make_response, write_token
import spotify_client
from spotify_client import (API_URL, MAX_RETRIES, MAX_RETRY_AFTER, PAGE_LIMIT, SCOPES, TOKEN_URL,
                            SpotifyClient, SpotifyError)

OK = make_response(json_body={"ok": True})


def query_of(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def bearer(call: dict) -> str:
    return call["headers"]["Authorization"]


# --- PKCE login ---------------------------------------------------------------

class TestAuthorizeAndLogin:
    def test_authorize_url_carries_pkce_s256_challenge_and_scopes(self, make_client):
        client = make_client(logged_in=False)
        q = query_of(client.authorize_url())

        assert q["client_id"] == CLIENT_ID
        assert q["redirect_uri"] == REDIRECT_URI
        assert q["response_type"] == "code"
        assert q["scope"] == SCOPES
        assert q["code_challenge_method"] == "S256"
        assert "=" not in q["code_challenge"], "PKCE challenge must be unpadded base64url"

    def test_verifier_sent_on_login_hashes_to_challenge_from_url(self, make_client, api):
        client = make_client(logged_in=False)
        api.on_token(make_response(json_body={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}))
        q = query_of(client.authorize_url())

        client.complete_login("the-code", q["state"])

        sent = api.calls_to("POST", TOKEN_URL)[0]["data"]
        digest = hashlib.sha256(sent["code_verifier"].encode()).digest()
        assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == q["code_challenge"]
        assert sent["grant_type"] == "authorization_code"
        assert sent["code"] == "the-code"
        assert sent["redirect_uri"] == REDIRECT_URI
        assert sent["client_id"] == CLIENT_ID

    def test_each_authorize_url_gets_its_own_state(self, make_client):
        client = make_client(logged_in=False)
        assert query_of(client.authorize_url())["state"] != query_of(client.authorize_url())["state"]

    def test_successful_login_persists_token_and_marks_logged_in(self, make_client, api, clock, token_path):
        client = make_client(logged_in=False)
        api.on_token(make_response(json_body={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}))
        state = query_of(client.authorize_url())["state"]

        client.complete_login("code", state)

        assert client.logged_in
        saved = json.loads(token_path.read_text())
        assert saved["access_token"] == "AT"
        assert saved["refresh_token"] == "RT"
        # expires a minute early so a request never races the real expiry
        assert saved["expires_at"] == clock.now + 3600 - 60

    def test_token_file_is_owner_only(self, make_client, api, token_path):
        client = make_client(logged_in=False)
        api.on_token(make_response(json_body={"access_token": "AT", "refresh_token": "RT"}))
        client.complete_login("code", query_of(client.authorize_url())["state"])

        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600

    def test_unknown_state_is_rejected_without_contacting_spotify(self, make_client, api):
        client = make_client(logged_in=False)
        client.authorize_url()

        with pytest.raises(SpotifyError, match="state") as exc:
            client.complete_login("code", "forged-state")

        assert exc.value.status == 400
        assert api.calls == []
        assert not client.logged_in

    def test_state_is_single_use(self, make_client, api):
        client = make_client(logged_in=False)
        api.on_token(make_response(json_body={"access_token": "AT", "refresh_token": "RT"}))
        state = query_of(client.authorize_url())["state"]
        client.complete_login("code", state)

        with pytest.raises(SpotifyError) as exc:
            client.complete_login("code", state)
        assert exc.value.status == 400
        assert len(api.calls_to("POST", TOKEN_URL)) == 1

    def test_rejected_code_exchange_raises_with_oauth_description(self, make_client, api, token_path):
        client = make_client(logged_in=False)
        api.on_token(make_response(400, json_body={"error": "invalid_grant",
                                                    "error_description": "Invalid authorization code"}))
        state = query_of(client.authorize_url())["state"]

        with pytest.raises(SpotifyError, match="Invalid authorization code") as exc:
            client.complete_login("bad", state)

        assert exc.value.status == 400
        assert not client.logged_in
        assert not token_path.exists()

    def test_network_error_on_code_exchange_becomes_spotify_error_0(self, make_client, api, token_path):
        client = make_client(logged_in=False)
        api.on_token(requests.ConnectionError("dns failure"))
        state = query_of(client.authorize_url())["state"]

        with pytest.raises(SpotifyError, match="dns failure") as exc:
            client.complete_login("code", state)

        assert exc.value.status == 0
        assert isinstance(exc.value.__cause__, requests.ConnectionError)
        assert not client.logged_in
        assert not token_path.exists()


# --- token storage ----------------------------------------------------------------

class TestTokenStorage:
    def test_saved_token_is_picked_up_by_a_new_client(self, make_client, token_path):
        make_client()
        assert SpotifyClient(CLIENT_ID, REDIRECT_URI, token_path).logged_in

    @pytest.mark.parametrize("content", ["", "{not json", "null"], ids=["empty", "corrupt", "json-null"])
    def test_unreadable_token_file_means_logged_out(self, token_path, content):
        token_path.write_text(content)
        assert not SpotifyClient(CLIENT_ID, REDIRECT_URI, token_path).logged_in

    def test_logout_removes_token_file(self, client, token_path):
        client.logout()
        assert not client.logged_in
        assert not token_path.exists()

    def test_logout_without_token_file_is_harmless(self, make_client, token_path):
        client = make_client(logged_in=False)
        client.logout()
        assert not token_path.exists()

    def test_saving_over_a_world_readable_file_leaves_it_owner_only(self, make_client, api, clock, token_path):
        client = make_client(expires_at=clock.now - 1)
        token_path.chmod(0o644)
        api.on_token(make_response(json_body={"access_token": "AT-2", "expires_in": 3600}))
        api.on_get("/me", OK)

        client.get("/me")

        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
        assert json.loads(token_path.read_text())["access_token"] == "AT-2"

    def test_save_leaves_no_temp_file_behind(self, make_client, api, clock, token_path):
        client = make_client(expires_at=clock.now - 1)
        api.on_token(make_response(json_body={"access_token": "AT-2", "expires_in": 3600}))
        api.on_get("/me", OK)

        client.get("/me")

        assert sorted(p.name for p in token_path.parent.iterdir()) == [token_path.name]

    def test_stale_world_readable_temp_file_is_tightened_before_writing(self, make_client, api, clock, token_path):
        # O_CREAT's 0o600 is ignored for a file that already exists, so only
        # the explicit fchmod keeps a leftover temp file from leaking the token.
        tmp = token_path.with_name(token_path.name + ".tmp")
        tmp.write_text("leftover")
        tmp.chmod(0o644)
        client = make_client(expires_at=clock.now - 1)
        api.on_token(make_response(json_body={"access_token": "AT-2", "expires_in": 3600}))
        api.on_get("/me", OK)

        client.get("/me")

        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
        assert json.loads(token_path.read_text())["access_token"] == "AT-2"

    def test_interrupted_write_keeps_the_previous_token_file(self, make_client, api, clock, token_path, monkeypatch):
        client = make_client(expires_at=clock.now - 1)
        api.on_token(make_response(json_body={"access_token": "AT-2", "refresh_token": "RT-2", "expires_in": 3600}))

        def disk_full(obj, f):
            f.write('{"access_token": "AT-')
            raise OSError("No space left on device")

        monkeypatch.setattr(spotify_client.json, "dump", disk_full)

        # The error type is not this test's concern, only what is left on disk.
        with pytest.raises((OSError, SpotifyError)):
            client.get("/me")

        assert json.loads(token_path.read_text())["refresh_token"] == "RT-1"

    def test_token_is_saved_where_os_has_no_fchmod(self, make_client, api, token_path, monkeypatch):
        # Windows before Python 3.13 has no os.fchmod.
        monkeypatch.delattr(os, "fchmod")
        client = make_client(logged_in=False)
        api.on_token(make_response(json_body={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}))

        client.complete_login("code", query_of(client.authorize_url())["state"])

        assert json.loads(token_path.read_text())["refresh_token"] == "RT"
        assert SpotifyClient(CLIENT_ID, REDIRECT_URI, token_path).logged_in

    def test_logout_waits_for_an_in_flight_refresh_instead_of_being_undone(self, make_client, api, clock, token_path):
        client = make_client(expires_at=clock.now - 1)
        logout = threading.Thread(target=client.logout)

        def refresh_response():
            # Runs while the refreshing thread holds the client lock.
            logout.start()
            logout.join(0.2)
            assert logout.is_alive(), "logout must not complete while a refresh is mid-flight"
            return make_response(json_body={"access_token": "AT-2", "refresh_token": "RT-2", "expires_in": 3600})

        api.on_token(refresh_response)
        api.on_get("/me", OK)

        client.get("/me")
        logout.join(5)

        assert not logout.is_alive()
        assert not client.logged_in
        assert not token_path.exists(), "refresh re-saved the token after logout"

    def test_get_while_logged_out_raises_401_without_request(self, make_client, api):
        client = make_client(logged_in=False)
        with pytest.raises(SpotifyError) as exc:
            client.get("/me")
        assert exc.value.status == 401
        assert api.calls == []


# --- refresh ----------------------------------------------------------------------

class TestRefresh:
    def test_expired_token_is_refreshed_before_the_request(self, make_client, api, clock):
        client = make_client(expires_at=clock.now - 1)
        api.on_token(make_response(json_body={"access_token": "AT-2", "refresh_token": "RT-2", "expires_in": 3600}))
        api.on_get("/me", OK)

        assert client.get("/me") == {"ok": True}

        refresh = api.calls_to("POST", TOKEN_URL)[0]["data"]
        assert refresh == {"grant_type": "refresh_token", "refresh_token": "RT-1", "client_id": CLIENT_ID}
        assert bearer(api.calls_to("GET", "/me")[0]) == "Bearer AT-2"

    def test_refresh_without_new_refresh_token_keeps_the_old_one(self, make_client, api, clock, token_path):
        client = make_client(expires_at=clock.now - 1)
        api.on_token(make_response(json_body={"access_token": "AT-2", "expires_in": 3600}))
        api.on_get("/me", OK)

        client.get("/me")

        assert json.loads(token_path.read_text())["refresh_token"] == "RT-1"

    def test_token_still_valid_is_not_refreshed(self, client, api):
        api.on_get("/me", OK)
        client.get("/me")
        assert api.calls_to("POST", TOKEN_URL) == []

    @pytest.mark.parametrize("status", [400, 401], ids=["invalid-grant", "invalid-client"])
    def test_rejected_refresh_logs_out(self, make_client, api, clock, token_path, status):
        client = make_client(expires_at=clock.now - 1)
        api.on_token(make_response(status, json_body={"error": "invalid_grant",
                                                       "error_description": "Refresh token revoked"}))

        with pytest.raises(SpotifyError, match="Refresh token revoked") as exc:
            client.get("/me")

        assert exc.value.status == status
        assert not client.logged_in
        assert not token_path.exists()

    @pytest.mark.parametrize(
        ("failure", "status"),
        [
            (make_response(500, json_body={"error": "server_error"}), 500),
            (make_response(503, body=b"", reason="Service Unavailable"), 503),
            (make_response(429, headers={"Retry-After": "1"}), 429),
            (requests.ConnectionError("connection reset"), 0),
            (requests.Timeout("read timed out"), 0),
        ],
        ids=["500", "503", "429", "connection-error", "timeout"],
    )
    def test_transient_refresh_failure_keeps_the_session(self, make_client, api, clock, token_path,
                                                         failure, status):
        client = make_client(expires_at=clock.now - 1)
        api.on_token(failure)

        with pytest.raises(SpotifyError) as exc:
            client.get("/me")

        assert exc.value.status == status
        assert client.logged_in
        assert json.loads(token_path.read_text())["refresh_token"] == "RT-1"

    def test_session_kept_after_transient_failure_refreshes_on_next_request(self, make_client, api, clock):
        client = make_client(expires_at=clock.now - 1)
        api.on_token(make_response(503, body=b""),
                     make_response(json_body={"access_token": "AT-2", "expires_in": 3600}))
        api.on_get("/me", OK)

        with pytest.raises(SpotifyError):
            client.get("/me")
        assert client.get("/me") == {"ok": True}

        assert [c["data"]["refresh_token"] for c in api.calls_to("POST", TOKEN_URL)] == ["RT-1", "RT-1"]
        assert bearer(api.calls_to("GET", "/me")[-1]) == "Bearer AT-2"

    def test_network_error_during_refresh_is_a_spotify_error_not_a_requests_one(self, make_client, api, clock):
        client = make_client(expires_at=clock.now - 1)
        api.on_token(requests.ConnectionError("unreachable"))

        with pytest.raises(SpotifyError, match="unreachable") as exc:
            client.get("/me")

        assert isinstance(exc.value.__cause__, requests.ConnectionError)
        assert api.calls_to("GET", "/me") == []

    def test_server_error_on_refresh_after_api_401_keeps_the_session(self, client, api, token_path):
        api.on_get("/me", make_response(401, json_body={"error": {"status": 401, "message": "expired"}}))
        api.on_token(make_response(502, body=b"", reason="Bad Gateway"))

        with pytest.raises(SpotifyError) as exc:
            client.get("/me")

        assert exc.value.status == 502
        assert client.logged_in
        assert token_path.exists()

    def test_expired_token_without_refresh_token_logs_out_with_401(self, make_client, api, clock, token_path):
        client = make_client(expires_at=clock.now - 1, refresh_token=None)

        with pytest.raises(SpotifyError) as exc:
            client.get("/me")

        assert exc.value.status == 401
        assert not client.logged_in
        assert not token_path.exists()
        assert api.calls == []


# --- retrying GET -------------------------------------------------------------------

class TestGetRetries:
    def test_relative_path_is_prefixed_and_absolute_url_kept(self, client, api):
        api.on_get("/me", OK)
        api.on_get(f"{API_URL}/me/playlists?offset=50", OK)

        client.get("/me", {"limit": 1})
        client.get(f"{API_URL}/me/playlists?offset=50")

        first, second = api.calls
        assert (first["url"], first["params"]) == (f"{API_URL}/me", {"limit": 1})
        assert second["url"] == f"{API_URL}/me/playlists?offset=50"
        assert bearer(first) == "Bearer AT-1"
        assert first["timeout"] is not None, "every outbound call needs a timeout"

    def test_401_triggers_one_refresh_and_retry_with_new_token(self, client, api):
        api.on_get("/me", make_response(401, json_body={"error": {"status": 401, "message": "expired"}}), OK)
        api.on_token(make_response(json_body={"access_token": "AT-2", "expires_in": 3600}))

        assert client.get("/me") == {"ok": True}

        assert [bearer(c) for c in api.calls_to("GET", "/me")] == ["Bearer AT-1", "Bearer AT-2"]
        assert len(api.calls_to("POST", TOKEN_URL)) == 1

    def test_second_401_after_refresh_is_raised_not_looped(self, client, api):
        api.on_get("/me", make_response(401, json_body={"error": {"status": 401, "message": "nope"}}))
        api.on_token(make_response(json_body={"access_token": "AT-2", "expires_in": 3600}))

        with pytest.raises(SpotifyError, match="nope") as exc:
            client.get("/me")

        assert exc.value.status == 401
        assert len(api.calls_to("POST", TOKEN_URL)) == 1
        assert len(api.calls_to("GET", "/me")) == 2

    def test_429_waits_retry_after_plus_one_second(self, client, api, clock):
        api.on_get("/me", make_response(429, headers={"Retry-After": "3"}), OK)

        assert client.get("/me") == {"ok": True}
        assert clock.sleeps == [4]

    def test_429_without_retry_after_waits_default(self, client, api, clock):
        api.on_get("/me", make_response(429), OK)
        client.get("/me")
        assert clock.sleeps == [6]

    def test_retry_after_at_the_cap_is_still_waited_out(self, client, api, clock):
        api.on_get("/me", make_response(429, headers={"Retry-After": str(MAX_RETRY_AFTER)}), OK)

        assert client.get("/me") == {"ok": True}
        assert clock.sleeps == [MAX_RETRY_AFTER + 1]

    @pytest.mark.parametrize(("retry_after", "minutes"), [(MAX_RETRY_AFTER + 1, 2), (3600, 61)],
                             ids=["just-over-cap", "one-hour"])
    def test_retry_after_over_the_cap_fails_fast_without_sleeping(self, client, api, clock, retry_after, minutes):
        api.on_get("/me", make_response(429, headers={"Retry-After": str(retry_after)}), OK)

        with pytest.raises(SpotifyError, match=f"za ok. {minutes} min") as exc:
            client.get("/me")

        assert exc.value.status == 429
        assert clock.sleeps == []
        assert len(api.calls) == 1

    def test_negative_retry_after_is_treated_as_zero(self, client, api, clock):
        api.on_get("/me", make_response(429, headers={"Retry-After": "-30"}), OK)
        client.get("/me")
        assert clock.sleeps == [1]

    def test_persistent_429_gives_up_after_max_retries(self, client, api):
        api.on_get("/me", make_response(429, headers={"Retry-After": "0"}))

        with pytest.raises(SpotifyError) as exc:
            client.get("/me")

        assert exc.value.status == 429
        assert len(api.calls) == MAX_RETRIES

    def test_5xx_is_retried_with_exponential_backoff(self, client, api, clock):
        api.on_get("/me", make_response(500), make_response(502), OK)

        assert client.get("/me") == {"ok": True}
        assert clock.sleeps == [1, 2]

    def test_persistent_5xx_raises_its_status_after_max_retries(self, client, api, clock):
        api.on_get("/me", make_response(503, json_body={"error": {"status": 503, "message": "down"}}))

        with pytest.raises(SpotifyError, match="down") as exc:
            client.get("/me")

        assert exc.value.status == 503
        assert len(api.calls) == MAX_RETRIES
        assert clock.sleeps == [1, 2, 4, 8]  # no pointless sleep after the last attempt

    def test_network_error_is_retried(self, client, api, clock):
        api.on_get("/me", requests.ConnectionError("reset"), requests.Timeout("slow"), OK)

        assert client.get("/me") == {"ok": True}
        assert clock.sleeps == [1, 2]

    def test_persistent_network_error_becomes_spotify_error_0(self, client, api):
        api.on_get("/me", requests.ConnectionError("unreachable"))

        with pytest.raises(SpotifyError, match="unreachable") as exc:
            client.get("/me")

        assert exc.value.status == 0
        assert isinstance(exc.value.__cause__, requests.ConnectionError)
        assert len(api.calls) == MAX_RETRIES

    @pytest.mark.parametrize("status", [400, 403, 404])
    def test_client_errors_are_not_retried(self, client, api, clock, status):
        api.on_get("/me", make_response(status, json_body={"error": {"status": status, "message": "bad"}}))

        with pytest.raises(SpotifyError) as exc:
            client.get("/me")

        assert exc.value.status == status
        assert len(api.calls) == 1
        assert clock.sleeps == []

    @pytest.mark.parametrize(
        ("resp", "expected"),
        [
            (make_response(404, json_body={"error": {"status": 404, "message": "Resource not found"}}),
             "Resource not found"),
            (make_response(400, json_body={"error": "invalid_client", "error_description": "Invalid client"}),
             "Invalid client"),
            (make_response(400, json_body={"error": "invalid_request"}), "invalid_request"),
            (make_response(404, body=b"<html>gateway page</html>"), "<html>gateway page</html>"),
            (make_response(404, body=b"", reason="Not Found"), "Not Found"),
        ],
        ids=["api-error-object", "oauth-description", "oauth-error-only", "non-json-body", "empty-body"],
    )
    def test_error_message_is_extracted_from_body(self, client, api, resp, expected):
        api.on_get("/me", resp)
        with pytest.raises(SpotifyError) as exc:
            client.get("/me")
        assert str(exc.value) == f"Spotify API {resp.status_code}: {expected}"


# --- pagination & download ------------------------------------------------------------

class TestPaginate:
    def test_follows_next_links_across_pages_in_order(self, client, api):
        api.on_paged("/me/playlists", [{"n": i} for i in range(5)], page_size=2)

        assert [p["n"] for p in client.paginate("/me/playlists")] == [0, 1, 2, 3, 4]
        assert len(api.calls) == 3
        assert api.calls[0]["params"] == {"limit": PAGE_LIMIT}

    def test_extra_params_go_on_the_first_page_only(self, client, api):
        api.on_paged("/me/playlists", [{"n": i} for i in range(3)], page_size=2)

        assert len(list(client.paginate("/me/playlists", {"additional_types": "track,episode"}))) == 3

        first, second = api.calls
        assert first["params"] == {"limit": PAGE_LIMIT, "additional_types": "track,episode"}
        # the `next` URL already encodes every query parameter
        assert second["params"] is None

    def test_empty_or_null_items_yield_nothing(self, client, api):
        api.on_get("/me/playlists", make_response(json_body={"items": None, "next": None}))
        assert list(client.paginate("/me/playlists")) == []

    def test_is_lazy(self, client, api):
        api.on_paged("/me/playlists", [{"n": i} for i in range(4)], page_size=2)
        it = client.paginate("/me/playlists")
        next(it)
        assert len(api.calls) == 1


class TestDownload:
    def test_returns_body_without_sending_the_bearer_token(self, client, api):
        url = "https://i.scdn.co/image/abc"
        api.on_get(url, make_response(body=b"IMG", headers={"Content-Type": "image/jpeg"}))

        assert client.download(url).content == b"IMG"
        assert "Authorization" not in api.calls[0]["headers"]

    def test_http_error_is_raised(self, client, api):
        url = "https://i.scdn.co/image/gone"
        api.on_get(url, make_response(404))
        with pytest.raises(requests.HTTPError):
            client.download(url)
