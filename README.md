# Spotify → JSON

Eksportuje twoje playlisty Spotify (własne i współtworzone) oraz „Polubione utwory” do plików JSON, razem z okładkami. Playlisty wybierasz checkboxami w przeglądarce, a wynik zapisujesz do folderu albo pobierasz jako ZIP.

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

### Ikonka (macOS)

```sh
launcher/build_app.sh        # tworzy dist/Eksport playlist.app
open dist
```

Przeciągnij **Eksport playlist.app** do Aplikacji albo na Dock.
- Kliknięcie uruchamia serwer w tle (bez okna terminala) i otwiera stronę w przeglądarce. Jeśli aplikacja już działa, kliknięcie tylko otwiera stronę.
- Przycisk **Zakończ** na stronie zamyka aplikację.
- Logi trafiają do `~/Library/Logs/spotify-export.log`.

Aplikacja ma na sztywno wpisaną ścieżkę do tego folderu i do `python3`. Po przeniesieniu folderu albo zmianie Pythona zbuduj ją ponownie.

### Z terminala

```sh
python3 app.py
```

1. Otworzy się karta przeglądarki. Zaloguj się do Spotify.
2. Skrypt zeskanuje playlisty. Zaznacz te, które chcesz wyeksportować.
3. Wybierz, co zrobić z plikami:
   - **Zapisz w folderze**: domyślnie `output/`. Przycisk **Zmień…** otwiera systemowe okno wyboru folderu (macOS), a wybór jest zapamiętany.
   - **Pobierz przez przeglądarkę**: dostaniesz jeden plik ZIP. Na dysku nic nie zostaje poza pobranym plikiem.
4. Kliknij **Eksportuj**.
5. Pracę kończysz przyciskiem **Zakończ** albo `Ctrl+C` w terminalu.

Opcje:
- `--port 9000`: inny port. Wtedy zmień też Redirect URI w Dashboardzie.
- `--out ścieżka`: domyślny folder wyjściowy.
- `--no-browser`: nie otwiera przeglądarki automatycznie.

Dane logowania są zapamiętane w `.token.json`, więc przy kolejnym uruchomieniu nie trzeba się logować. Przycisk **Wyloguj** usuwa ten plik.

Ikonę rysuje `launcher/make_icon.py` (wymaga Pillow). Uruchamiaj go tylko wtedy, gdy chcesz zmienić jej wygląd.

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
