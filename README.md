# Spotify → JSON

Eksportuje twoje playlisty Spotify (własne i współtworzone) oraz „Polubione utwory” do plików JSON, razem z okładkami. Playlisty wybierasz checkboxami w przeglądarce.

## Ograniczenia API (stan na 2026)

- Aplikacja musi należeć do konta **Spotify Premium**.
- Spotify udostępnia listę utworów tylko z playlist, które sam utworzyłeś albo współtworzysz. Dlatego obserwowane cudze playlisty, w tym te od samego Spotify, nie pojawiają się na liście.
  - Obejście: w aplikacji Spotify zaznacz wszystkie utwory takiej playlisty, wybierz „Dodaj do playlisty”, potem „Nowa playlista”. Kopia pojawi się na liście jako twoja.

## Jednorazowa konfiguracja

1. Wejdź na https://developer.spotify.com/dashboard i kliknij **Create app**.
   - **Redirect URI:** `http://127.0.0.1:8888/callback`. Musi być dokładnie taki; `localhost` nie zadziała.
   - **APIs used:** Web API.
2. Skopiuj **Client ID** z ustawień aplikacji. Client Secret nie jest potrzebny, bo logowanie używa PKCE.
3. Wykonaj w tym folderze:
   ```sh
   cp .env.example .env      # wpisz Client ID w .env
   pip3 install -r requirements.txt
   ```

## Użycie

```sh
python3 app.py
```

1. Otworzy się karta przeglądarki. Zaloguj się do Spotify.
2. Skrypt zeskanuje playlisty. Zaznacz te, które chcesz wyeksportować, i kliknij **Eksportuj**.
3. Pliki zapiszą się w `output/<data_godzina>/`.
4. `Ctrl+C` w terminalu kończy pracę.

Opcje:
- `--port 9000`: inny port. Wtedy zmień też Redirect URI w Dashboardzie.
- `--out ścieżka`: inny folder wyjściowy.
- `--no-browser`: nie otwiera przeglądarki automatycznie.

Dane logowania są zapamiętane w `.token.json`, więc przy kolejnym uruchomieniu nie trzeba się logować. Przycisk **Wyloguj** usuwa ten plik.

## Format wyjścia

```
output/2026-11-25_143012/
├── index.json                # lista wyeksportowanych playlist i ewentualne błędy
├── Polubione utwory/
│   └── playlist.json
└── Moja playlista/
    ├── playlist.json
    └── cover.jpg
```

`playlist.json`:

```json
{
  "schema_version": 1,
  "source": "spotify",
  "exported_at": "2026-11-25T13:30:12+00:00",
  "playlist": {
    "kind": "playlist",
    "id": "37i9dQ…",
    "name": "Moja playlista",
    "description": "…",
    "url": "https://open.spotify.com/playlist/…",
    "owner": { "id": "…", "display_name": "…" },
    "collaborative": false,
    "cover": "cover.jpg"
  },
  "track_count": 1,
  "tracks": [
    {
      "position": 0,
      "type": "track",
      "name": "Tytuł",
      "artists": ["Wykonawca"],
      "album": "Album",
      "release_date": "2020-01-01",
      "duration_ms": 215000,
      "explicit": false,
      "isrc": "USUM72000001",
      "uri": "spotify:track:…",
      "url": "https://open.spotify.com/track/…",
      "is_local": false,
      "added_at": "2024-05-01T10:00:00Z",
      "added_by": "…"
    }
  ]
}
```

Pole `isrc` to międzynarodowy identyfikator nagrania. Narzędzia do importu w innych serwisach (TIDAL, Apple Music, YouTube Music) używają go do najdokładniejszego dopasowania utworów.

Usunięty lub niedostępny w twoim regionie utwór zostaje zapisany z pustymi polami (`null`). Dzięki temu numeracja pozycji zgadza się z oryginałem.
