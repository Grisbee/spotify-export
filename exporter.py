"""Scans exportable playlists and writes each selected one to JSON + cover image."""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from spotify_client import SpotifyClient, SpotifyError

LIKED_SONGS_ID = "liked-songs"
SCHEMA_VERSION = 1
IMAGE_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def scan(client: SpotifyClient) -> list[dict[str, Any]]:
    """List Liked Songs plus playlists whose tracks the API will return.

    Since February 2026 Development Mode only returns contents of playlists the
    user owns or collaborates on, so followed playlists are left out entirely.
    """
    me = client.me()
    liked = client.get("/me/tracks", {"limit": 1})
    result = [{
        "id": LIKED_SONGS_ID,
        "name": "Polubione utwory",
        "kind": "liked_songs",
        "track_count": liked.get("total", 0),
        "owner": me.get("display_name") or me["id"],
        "image": None,
    }]
    for p in client.paginate("/me/playlists"):
        if not p:
            continue
        owned = (p.get("owner") or {}).get("id") == me["id"]
        if not (owned or p.get("collaborative")):
            continue
        result.append({
            "id": p["id"],
            "name": p.get("name") or "(bez nazwy)",
            "kind": "playlist",
            "track_count": _item_total(p),
            "owner": (p.get("owner") or {}).get("display_name"),
            "collaborative": bool(p.get("collaborative")),
            "image": _best_image(p.get("images")),
        })
    return result


def export(
    client: SpotifyClient,
    selected: list[dict[str, Any]],
    out_root: Path,
    on_progress: Callable[[str, str, str | None], None],
) -> Path:
    """Export the selected scan entries into a fresh timestamped directory.

    `on_progress(id, status, detail)` is called with status in
    {"running", "done", "error"}; a failure of one playlist doesn't stop the rest.
    """
    exported_at = datetime.now(timezone.utc)
    run_dir = _new_run_dir(out_root, exported_at.astimezone().strftime("%Y-%m-%d_%H%M%S"))
    used_names: set[str] = set()
    index = []

    for entry in selected:
        on_progress(entry["id"], "running", None)
        folder = run_dir / _unique_folder_name(entry["name"], used_names)
        try:
            if entry["kind"] == "liked_songs":
                doc = _export_liked(client, exported_at)
            else:
                doc = _export_playlist(client, entry["id"], exported_at)
            folder.mkdir()
            doc["playlist"]["cover"] = _save_cover(client, entry, folder)
            (folder / "playlist.json").write_text(
                json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except (SpotifyError, OSError, ValueError, KeyError) as exc:
            message = str(exc)
            if isinstance(exc, SpotifyError) and exc.status == 403:
                message = "Spotify nie udostępnia utworów tej playlisty (nie jesteś jej współtwórcą)."
            on_progress(entry["id"], "error", message)
            index.append({"id": entry["id"], "name": entry["name"], "error": message})
            continue
        on_progress(entry["id"], "done", _tracks_label(len(doc["tracks"])))
        index.append({
            "id": entry["id"],
            "name": entry["name"],
            "folder": folder.name,
            "track_count": len(doc["tracks"]),
        })

    (run_dir / "index.json").write_text(
        json.dumps({"exported_at": exported_at.isoformat(), "playlists": index},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return run_dir


def _export_playlist(client: SpotifyClient, playlist_id: str, exported_at: datetime) -> dict[str, Any]:
    p = client.get(f"/playlists/{playlist_id}")
    tracks = [
        _track(entry.get("item") or entry.get("track"), i, entry)
        for i, entry in enumerate(client.paginate(f"/playlists/{playlist_id}/items"))
    ]
    owner = p.get("owner") or {}
    return _document(exported_at, {
        "kind": "playlist",
        "id": p["id"],
        "name": p.get("name"),
        "description": html.unescape(p.get("description") or ""),
        "url": (p.get("external_urls") or {}).get("spotify"),
        "uri": p.get("uri"),
        "owner": {"id": owner.get("id"), "display_name": owner.get("display_name")},
        "collaborative": p.get("collaborative"),
        "public": p.get("public"),
        "snapshot_id": p.get("snapshot_id"),
    }, tracks)


def _export_liked(client: SpotifyClient, exported_at: datetime) -> dict[str, Any]:
    tracks = [
        _track(entry.get("track") or entry.get("item"), i, entry)
        for i, entry in enumerate(client.paginate("/me/tracks"))
    ]
    return _document(exported_at, {
        "kind": "liked_songs",
        "id": None,
        "name": "Polubione utwory",
        "description": "",
        "url": None,
        "uri": None,
    }, tracks)


def _document(exported_at: datetime, playlist: dict[str, Any], tracks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "spotify",
        "exported_at": exported_at.isoformat(),
        "playlist": playlist,
        "track_count": len(tracks),
        "tracks": tracks,
    }


def _track(item: dict[str, Any] | None, position: int, entry: dict[str, Any]) -> dict[str, Any]:
    # A removed or region-blocked track comes back as a null item; keep the
    # slot so positions still match the original playlist.
    item = item or {}
    album = item.get("album") or {}
    added_by = entry.get("added_by") or {}
    return {
        "position": position,
        "type": item.get("type"),
        "name": item.get("name"),
        "artists": [a.get("name") for a in item.get("artists") or []],
        "album": album.get("name") or (item.get("show") or {}).get("name"),
        "release_date": album.get("release_date"),
        "duration_ms": item.get("duration_ms"),
        "explicit": item.get("explicit"),
        "isrc": (item.get("external_ids") or {}).get("isrc"),
        "uri": item.get("uri"),
        "url": (item.get("external_urls") or {}).get("spotify"),
        "is_local": entry.get("is_local", item.get("is_local", False)),
        "added_at": entry.get("added_at"),
        "added_by": added_by.get("id"),
    }


def _save_cover(client: SpotifyClient, entry: dict[str, Any], folder: Path) -> str | None:
    if entry["kind"] != "playlist":
        return None
    # Image URLs expire within a day, so fetch fresh ones right before download.
    try:
        url = _best_image(client.get(f"/playlists/{entry['id']}/images"))
        if not url:
            return None
        resp = client.download(url)
    except (SpotifyError, requests.RequestException):
        return None  # a missing cover must not fail the whole playlist
    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
    name = "cover" + IMAGE_EXTENSIONS.get(content_type, ".jpg")
    (folder / name).write_bytes(resp.content)
    return name


def _best_image(images: list[dict[str, Any]] | None) -> str | None:
    if not images:
        return None
    best = max(images, key=lambda img: (img.get("width") or 0) * (img.get("height") or 0))
    return best.get("url")


def _item_total(playlist: dict[str, Any]) -> int:
    for key in ("items", "tracks"):
        value = playlist.get(key)
        if isinstance(value, dict) and "total" in value:
            return value["total"]
    return 0


def _new_run_dir(out_root: Path, stamp: str) -> Path:
    out_root.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        run_dir = out_root / (stamp if n == 1 else f"{stamp}-{n}")
        try:
            run_dir.mkdir()
            return run_dir
        except FileExistsError:
            n += 1


def _tracks_label(n: int) -> str:
    if n == 1:
        return "1 utwór"
    few = n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14)
    return f"{n} {'utwory' if few else 'utworów'}"


def _unique_folder_name(name: str, used: set[str]) -> str:
    base = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", name).strip(" .") or "playlista"
    base = base[:80]
    candidate, n = base, 2
    while candidate.lower() in used:
        candidate = f"{base} ({n})"
        n += 1
    used.add(candidate.lower())
    return candidate
