"""exporter: which playlists are offered, and what lands on disk per export."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import exporter
from conftest import ME, make_response, playlist_entry, playlist_meta, track_item
from exporter import LIKED_SONGS_ID


@pytest.fixture
def me(api):
    api.on_get("/me", make_response(json_body=ME))
    return ME


def liked_total(api, total: int) -> None:
    api.on_get("/me/tracks", make_response(json_body={"items": [], "next": None, "total": total}))


class Progress:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str | None]] = []

    def __call__(self, item_id: str, status: str, detail: str | None) -> None:
        self.events.append((item_id, status, detail))

    def final(self, item_id: str) -> tuple[str, str | None]:
        return [(s, d) for i, s, d in self.events if i == item_id][-1]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def scan_entry(pid: str, name: str) -> dict:
    return {"id": pid, "name": name, "kind": "playlist"}


LIKED_ENTRY = {"id": LIKED_SONGS_ID, "name": "Polubione utwory", "kind": "liked_songs"}


# --- scan --------------------------------------------------------------------------

class TestScan:
    def test_liked_songs_come_first_with_library_total(self, client, api, me):
        liked_total(api, 321)
        api.on_paged("/me/playlists", [])

        liked = exporter.scan(client)[0]

        assert liked["id"] == LIKED_SONGS_ID
        assert liked["kind"] == "liked_songs"
        assert liked["track_count"] == 321
        assert liked["owner"] == "Ja"
        assert api.calls_to("GET", "/me/tracks")[0]["params"] == {"limit": 1}

    def test_owner_falls_back_to_user_id_without_display_name(self, client, api):
        api.on_get("/me", make_response(json_body={"id": "me-user", "display_name": None}))
        liked_total(api, 0)
        api.on_paged("/me/playlists", [])

        assert exporter.scan(client)[0]["owner"] == "me-user"

    def test_offers_owned_and_collaborative_but_not_followed_playlists(self, client, api, me):
        liked_total(api, 0)
        api.on_paged("/me/playlists", [
            playlist_meta("own", "Mine"),
            playlist_meta("collab", "Shared", owner_id="friend", collaborative=True),
            playlist_meta("followed", "Today's Top Hits", owner_id="spotify"),
            None,  # Spotify returns null slots for unavailable playlists
        ], page_size=2)

        ids = [e["id"] for e in exporter.scan(client)]

        assert ids == [LIKED_SONGS_ID, "own", "collab"]

    def test_playlist_entry_fields(self, client, api, me):
        liked_total(api, 0)
        api.on_paged("/me/playlists", [
            playlist_meta("p1", "Road trip", collaborative=True, items={"total": 42}, images=[
                {"url": "https://img/small", "width": 60, "height": 60},
                {"url": "https://img/large", "width": 640, "height": 640},
                {"url": "https://img/mid", "width": 300, "height": 300},
            ]),
        ])

        entry = exporter.scan(client)[1]

        assert entry["name"] == "Road trip"
        assert entry["kind"] == "playlist"
        assert entry["track_count"] == 42
        assert entry["owner"] == "Me-User"
        assert entry["collaborative"] is True
        assert entry["image"] == "https://img/large"

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({"items": {"total": 7}}, 7),
            ({"items": None, "tracks": {"total": 9}}, 9),  # pre-2026 field name
            ({"items": None}, 0),
        ],
        ids=["items-total", "legacy-tracks-total", "missing"],
    )
    def test_track_count_reads_new_and_legacy_fields(self, client, api, me, fields, expected):
        liked_total(api, 0)
        api.on_paged("/me/playlists", [playlist_meta("p1", "X", **fields)])
        assert exporter.scan(client)[1]["track_count"] == expected

    def test_unnamed_playlist_and_no_images(self, client, api, me):
        liked_total(api, 0)
        api.on_paged("/me/playlists", [playlist_meta("p1", "", images=None)])

        entry = exporter.scan(client)[1]

        assert entry["name"] == "(bez nazwy)"
        assert entry["image"] is None


# --- export: playlist document --------------------------------------------------------

class TestExportPlaylist:
    @pytest.fixture
    def run(self, client, api, tmp_path):
        """Export one playlist with 3 entries across 2 pages, including a dead slot."""
        api.add_playlist(
            playlist_meta("p1", "Road trip", description="Rock &amp; Roll &#x27;24", collaborative=True),
            [
                playlist_entry(track_item(1)),
                playlist_entry(None),  # removed / region-blocked track
                playlist_entry(track_item(3), key="track"),  # legacy entry shape
            ],
            images=[{"url": "https://img/cover", "width": 640, "height": 640}],
        )
        progress = Progress()
        run_dir = exporter.export(client, [scan_entry("p1", "Road trip")], tmp_path / "out", progress)
        return run_dir, progress

    def test_writes_into_a_fresh_timestamped_dir(self, run, tmp_path):
        run_dir, _ = run
        assert run_dir.parent == tmp_path / "out"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{6}", run_dir.name)

    def test_playlist_metadata(self, run):
        doc = read_json(run[0] / "Road trip" / "playlist.json")

        assert doc["schema_version"] == 1
        assert doc["source"] == "spotify"
        pl = doc["playlist"]
        assert pl["kind"] == "playlist"
        assert pl["id"] == "p1"
        assert pl["description"] == "Rock & Roll '24"  # Spotify sends HTML-escaped text
        assert pl["url"] == "https://open.spotify.com/playlist/p1"
        assert pl["owner"] == {"id": "me-user", "display_name": "Me-User"}
        assert pl["collaborative"] is True
        assert pl["snapshot_id"] == "snap-1"
        assert pl["cover"] == "cover.png"

    def test_track_fields(self, run):
        first = read_json(run[0] / "Road trip" / "playlist.json")["tracks"][0]

        assert first["position"] == 0
        assert first["name"] == "Song 1"
        assert first["artists"] == ["Artist 1", "Feat"]
        assert first["album"] == "Album 1"
        assert first["release_date"] == "2020-01-01"
        assert first["isrc"] == "ISRC00001"
        assert first["uri"] == "spotify:track:t1"
        assert first["url"] == "https://open.spotify.com/track/t1"
        assert first["added_at"] == "2024-05-01T10:00:00Z"
        assert first["added_by"] == "adder"

    def test_every_entry_keeps_its_position_even_when_track_is_gone(self, run):
        doc = read_json(run[0] / "Road trip" / "playlist.json")

        assert doc["track_count"] == len(doc["tracks"]) == 3
        assert [t["position"] for t in doc["tracks"]] == [0, 1, 2]
        dead = doc["tracks"][1]
        assert dead["name"] is None and dead["isrc"] is None and dead["artists"] == []
        assert dead["added_at"] == "2024-05-01T10:00:00Z"

    def test_legacy_track_key_is_read(self, run):
        doc = read_json(run[0] / "Road trip" / "playlist.json")
        assert doc["tracks"][2]["name"] == "Song 3"

    def test_cover_bytes_saved_next_to_json(self, run):
        assert (run[0] / "Road trip" / "cover.png").read_bytes() == b"\x89PNG-fake"

    def test_index_lists_the_playlist(self, run):
        index = read_json(run[0] / "index.json")

        assert index["playlists"] == [{"id": "p1", "name": "Road trip", "folder": "Road trip", "track_count": 3}]
        assert datetime.fromisoformat(index["exported_at"]).tzinfo is not None

    def test_progress_reports_running_then_done_with_count(self, run):
        assert run[1].events == [("p1", "running", None), ("p1", "done", "3 utwory")]

    def test_episode_takes_album_from_show(self, client, api, tmp_path):
        episode = {"type": "episode", "name": "Ep", "show": {"name": "Podcast"}, "uri": "spotify:episode:e"}
        api.add_playlist(playlist_meta("p1", "Pods"), [playlist_entry(episode)])

        run_dir = exporter.export(client, [scan_entry("p1", "Pods")], tmp_path, Progress())

        track = read_json(run_dir / "Pods" / "playlist.json")["tracks"][0]
        assert (track["type"], track["album"]) == ("episode", "Podcast")

    def test_non_ascii_written_as_utf8_not_escaped(self, client, api, tmp_path):
        api.add_playlist(playlist_meta("p1", "Żółć"), [playlist_entry(track_item(1, name="Gęś"))])

        run_dir = exporter.export(client, [scan_entry("p1", "Żółć")], tmp_path, Progress())

        raw = (run_dir / "Żółć" / "playlist.json").read_text(encoding="utf-8")
        assert "Gęś" in raw


class TestExportLikedSongs:
    def test_liked_songs_document_and_no_cover_request(self, client, api, tmp_path):
        api.on_paged("/me/tracks", [
            {"track": track_item(1), "added_at": "2023-01-01T00:00:00Z"},
            {"track": track_item(2), "added_at": "2023-01-02T00:00:00Z"},
            {"track": track_item(3), "added_at": "2023-01-03T00:00:00Z"},
        ])
        progress = Progress()

        run_dir = exporter.export(client, [LIKED_ENTRY], tmp_path, progress)

        doc = read_json(run_dir / "Polubione utwory" / "playlist.json")
        assert doc["playlist"]["kind"] == "liked_songs"
        assert doc["playlist"]["cover"] is None
        assert [t["name"] for t in doc["tracks"]] == ["Song 1", "Song 2", "Song 3"]
        assert [t["added_at"][:10] for t in doc["tracks"]] == ["2023-01-01", "2023-01-02", "2023-01-03"]
        assert not any("/images" in c["url"] for c in api.calls)
        assert progress.final(LIKED_SONGS_ID) == ("done", "3 utwory")


# --- export: failure isolation ------------------------------------------------------------

class TestExportFailures:
    def test_403_playlist_gets_friendly_error_and_others_still_export(self, client, api, tmp_path):
        api.on_get("/playlists/locked", make_response(403, json_body={"error": {"status": 403, "message": "Forbidden"}}))
        api.add_playlist(playlist_meta("ok", "Fine"), [playlist_entry(track_item(1))])
        progress = Progress()

        run_dir = exporter.export(
            client, [scan_entry("locked", "Locked"), scan_entry("ok", "Fine")], tmp_path, progress)

        status, detail = progress.final("locked")
        assert status == "error"
        assert "współtwórcą" in detail
        assert progress.final("ok") == ("done", "1 utwór")
        assert not (run_dir / "Locked").exists()
        index = read_json(run_dir / "index.json")["playlists"]
        assert index[0] == {"id": "locked", "name": "Locked", "error": detail}
        assert index[1]["folder"] == "Fine"

    def test_other_api_errors_keep_spotify_message(self, client, api, tmp_path):
        api.on_get("/playlists/gone", make_response(404, json_body={"error": {"status": 404, "message": "Not found"}}))
        progress = Progress()

        exporter.export(client, [scan_entry("gone", "Gone")], tmp_path, progress)

        assert progress.final("gone") == ("error", "Spotify API 404: Not found")

    @pytest.mark.parametrize(
        "break_cover",
        [
            lambda api: api.on_get("/playlists/p1/images", make_response(404, json_body={"error": "x"})),
            lambda api: api.on_get("/playlists/p1/images", make_response(json_body=[])),
            lambda api: api.on_get("https://img/cover", make_response(500)),
        ],
        ids=["images-endpoint-error", "no-images", "download-fails"],
    )
    def test_cover_problems_never_fail_the_playlist(self, client, api, tmp_path, break_cover):
        api.add_playlist(playlist_meta("p1", "P"), [playlist_entry(track_item(1))],
                         images=[{"url": "https://img/cover", "width": 1, "height": 1}])
        break_cover(api)
        progress = Progress()

        run_dir = exporter.export(client, [scan_entry("p1", "P")], tmp_path, progress)

        assert progress.final("p1") == ("done", "1 utwór")
        assert read_json(run_dir / "P" / "playlist.json")["playlist"]["cover"] is None
        assert not list((run_dir / "P").glob("cover.*"))

    @pytest.mark.parametrize(
        ("content_type", "filename"),
        [("image/jpeg", "cover.jpg"), ("image/webp; charset=binary", "cover.webp"),
         ("application/octet-stream", "cover.jpg")],
        ids=["jpeg", "webp-with-params", "unknown-defaults-jpg"],
    )
    def test_cover_extension_follows_content_type(self, client, api, tmp_path, content_type, filename):
        api.add_playlist(playlist_meta("p1", "P"), [], images=[{"url": "https://img/c", "width": None, "height": None}],
                         image_type=content_type)

        run_dir = exporter.export(client, [scan_entry("p1", "P")], tmp_path, Progress())

        assert (run_dir / "P" / filename).is_file()

    def test_cover_url_is_fetched_fresh_not_taken_from_scan(self, client, api, tmp_path):
        api.add_playlist(playlist_meta("p1", "P"), [], images=[{"url": "https://img/fresh", "width": 1, "height": 1}])
        stale = {**scan_entry("p1", "P"), "image": "https://img/expired"}

        exporter.export(client, [stale], tmp_path, Progress())

        assert api.calls_to("GET", "https://img/fresh")
        assert not api.calls_to("GET", "https://img/expired")

    def test_two_exports_in_the_same_second_do_not_collide(self, client, api, tmp_path, monkeypatch):
        frozen = datetime(2026, 11, 25, 13, 30, 12, tzinfo=timezone.utc)

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen

        monkeypatch.setattr(exporter, "datetime", FrozenDatetime)
        api.add_playlist(playlist_meta("p1", "P"), [playlist_entry(track_item(1))])
        first, second = Progress(), Progress()

        dir1 = exporter.export(client, [scan_entry("p1", "P")], tmp_path, first)
        dir2 = exporter.export(client, [scan_entry("p1", "P")], tmp_path, second)

        assert second.final("p1")[0] == "done"
        assert dir1 != dir2


# --- folder naming and labels (pure helpers with their own rules) --------------------------

class TestFolderNames:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Rock/Pop: best?", "Rock_Pop_ best_"),
            ('a\\b*c"d<e>f|g', "a_b_c_d_e_f_g"),
            ("tab\there", "tab_here"),
            ("  .hidden. ", "hidden"),
            ("..", "playlista"),
            ("", "playlista"),
            ("x" * 200, "x" * 80),
        ],
        ids=["slash-colon-question", "windows-reserved", "control-char", "leading-dot-space",
             "dots-only", "empty", "truncated"],
    )
    def test_names_are_filesystem_safe(self, name, expected):
        assert exporter._unique_folder_name(name, set()) == expected

    def test_duplicates_get_numbered_case_insensitively(self):
        used: set[str] = set()
        names = [exporter._unique_folder_name(n, used) for n in ["Mix", "mix", "MIX", "Mix (2)"]]
        assert names == ["Mix", "mix (2)", "MIX (3)", "Mix (2) (2)"]

    def test_duplicate_names_export_to_separate_folders(self, client, api, tmp_path):
        api.add_playlist(playlist_meta("a", "Mix"), [playlist_entry(track_item(1))])
        api.add_playlist(playlist_meta("b", "Mix"), [playlist_entry(track_item(2)), playlist_entry(track_item(3))])

        run_dir = exporter.export(client, [scan_entry("a", "Mix"), scan_entry("b", "Mix")], tmp_path, Progress())

        assert read_json(run_dir / "Mix" / "playlist.json")["playlist"]["id"] == "a"
        assert read_json(run_dir / "Mix (2)" / "playlist.json")["playlist"]["id"] == "b"
        assert [p["folder"] for p in read_json(run_dir / "index.json")["playlists"]] == ["Mix", "Mix (2)"]


@pytest.mark.parametrize(
    ("n", "label"),
    [(0, "0 utworów"), (1, "1 utwór"), (2, "2 utwory"), (4, "4 utwory"), (5, "5 utworów"),
     (12, "12 utworów"), (14, "14 utworów"), (21, "21 utworów"), (22, "22 utwory"),
     (112, "112 utworów"), (1003, "1003 utwory")],
    ids=lambda v: str(v),
)
def test_polish_plural_of_track_count(n, label):
    assert exporter._tracks_label(n) == label
