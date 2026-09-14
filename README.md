# Audio Downloader

A small self-hosted web app for saving Spotify tracks/albums/playlists and
YouTube videos/playlists as MP3 files.

Spotify links are handled by [spotDL](https://spotdl.readthedocs.io/), which
uses Spotify metadata and finds matching audio from available providers.
YouTube links are handled directly by
[yt-dlp](https://github.com/yt-dlp/yt-dlp).

## Start it

The only prerequisite is Docker Desktop or Docker Engine with Compose.

```bash
docker compose up --build
```

Open <http://localhost:8000>, paste a supported link, choose the MP3 quality,
and select **Add to queue**.

Downloaded files remain in `./downloads`. Job state remains in `./data`, so
completed items still appear after a restart.

## Run without Docker

Install Python 3.11+ and FFmpeg, then create a virtual environment and run:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
```

On Windows, use `.venv\Scripts\pip` and `.venv\Scripts\python` for the last
two commands.

The non-Docker server binds to `127.0.0.1:8000` by default.

## Configuration

Copy `.env.example` to `.env` if you want to change the defaults.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AUDIO_BIND` | `127.0.0.1` | Server bind address |
| `AUDIO_PORT` | `8000` | Server port |
| `AUDIO_DOWNLOAD_ROOT` | `./downloads` | Completed audio directory |
| `AUDIO_STATE_ROOT` | `./data` | Job metadata directory |
| `AUDIO_ARCHIVE_ROOT` | `./archives` | Generated ZIP directory |
| `AUDIO_MAX_LOG_LINES` | `160` | Log lines retained per job |
| `AUDIO_PASSWORD` | unset | Enables the password screen when set |
| `AUDIO_SECURE_COOKIES` | `1` | Set to `0` only for password-protected plain HTTP development |
| `SPOTIPY_CLIENT_ID` | unset | Optional Spotify application ID for spotDL |
| `SPOTIPY_CLIENT_SECRET` | unset | Optional Spotify application secret for spotDL |

Docker binds port 8000 to `127.0.0.1`, so the app is not exposed to the local
network. If you deliberately want LAN access, change the Compose port mapping
to `8000:8000` and put the app behind authentication before exposing it beyond
a trusted network.

For internet hosting, always set a strong `AUDIO_PASSWORD`. The sign-in cookie
is marked Secure by default and works behind a hosting provider's HTTPS URL.

## Notes

- YouTube may occasionally require fresh cookies or a newer yt-dlp release as
  its site changes. Rebuilding the image installs current downloader releases.
- A playlist may contain unavailable, private, or region-restricted items;
  yt-dlp skips unavailable entries and keeps the successful downloads.
- Only download media you own or are authorized to save, and follow the terms
  that apply to the source service.
