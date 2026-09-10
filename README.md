# stremio-vidspark

Pure-Python Stremio addon for `vidspark.to` (Vidora backend, single auto server — no multi-server picker). No sidecar: FastAPI + httpx (HTTP/2 keepalive) does resolve, variant parsing, and proxying.

## Architecture

```
Stremio -> :8002 (FastAPI + Granian: resolve + direct streams + /proxy)
         -> https://vidspark.to/api/vidora/... (key scraped from frontend JS)
         -> bx.netrocdn.site / cdn-proxy.sparkvid.workers.dev (HLS)
```

The rotating `x-player-key` is scraped from `/assets/index-*.js` (hourly
cache, baked-in fallback). Incoming IMDb ids are mapped to Vidora's native
TMDB keys via Cinemeta (cached; falls back to the raw id), plus a small
`TMDB_OVERRIDES` table in `addon/app/vidora.py` for titles Vidora catalogs
under the wrong TMDB id (e.g. Reacher → 108978). Playback is **direct**
upstream https plus `behaviorHints.proxyHeaders` (Referer/Origin/UA) so
Stremio gets 2xx itself.

## Run (one window)

Double-click **`start.bat`** — or `.\start.bat` from a console. Granian runs
in the foreground on `:8002`; `Ctrl+C` stops it. `stop.ps1` kills `:8002`.

Install in Stremio:
```
http://127.0.0.1:8002/manifest.json
```

## Run (docker — start/stop the server)

```powershell
.\docker-start.bat   # docker compose up --build -d  (pure-python image)
.\docker-stop.bat    # docker compose down
```

`Dockerfile.python` is the whole server (python:3.12-slim, no Node).
Note: the root `Dockerfile` is a leftover from another project (cinesrc,
ports 7001/8001) — ours is `Dockerfile.python`, referenced explicitly by
`docker-compose.yml`.

## Bandwidth

Default posture uses **no server bandwidth for video**: all emitted streams
are direct upstream https, so bytes flow device ↔ CDN only. What still hits
this server per lookup is tiny JSON (Vidora/Cinemeta/streamdata APIs plus
master playlists fetched to label qualities — a few KB).

To lock it down fully: `INCLUDE_PROXY_FALLBACK=0` (default — no `/proxy`
stream entries are emitted) and `PROXY_ENABLED=0` (`/proxy` returns 403, so
not even a hand-crafted URL can pull bytes through). Verified: 15 + 12
streams with 0 local URLs, `/proxy` → `{"error":"proxy disabled"}`.

Enabling the proxy (`INCLUDE_PROXY_FALLBACK=1`) routes that entry's
playlists/segments through this server, plus audio normalization CPU.

## Test

```powershell
curl.exe http://127.0.0.1:8002/manifest.json
curl.exe http://127.0.0.1:8002/stream/movie/tt6604188.json
curl.exe http://127.0.0.1:8002/stream/series/tt4574334:5:3.json
```

Known IDs: movie `tt6604188` (= TMDB 533533, TRON: Ares), series `tt4574334:5:3` (= TMDB 66732 S05E03).

## Streams

Two backends, merged per title (all direct https with `proxyHeaders`):

1. **Vidora** (primary): one `Auto` adaptive master plus one entry per
   quality from the master playlist. Subtitles are direct `.vtt` URLs
   (no headers needed).
2. **Fallback** (`streamdata.vaplayer.ru`, the backend behind vidspark.to's
   own iframe fallback — reversed from its `player.min.js`): two servers,
   `justhd` (Source 1) and `hdtoday` (Source 2). Movies resolve by IMDb,
   TV by TMDB + season/episode. Each server gives `Auto` + per-quality
   entries + 2 mirror packs. This is what plays titles Vidora lacks
   (e.g. Reacher S1E1 → 12 fallback streams, up to 1080p).

Server names are the backends' own (`justhd`/`hdtoday`, `Vidora`);
qualities match the site's player menu (`Auto`/4K/1440p/1080p/720p/480p/360p/240p
by nearest playlist width).

## Audio

Fallback rips measure ~-28 LUFS (very quiet). `/proxy` normalizes all
proxied audio toward `AUDIO_TARGET_LUFS` (default -24) via ffmpeg single-pass
`loudnorm`, video copied untouched — every title plays at the same level.
`0` disables it (falls back to fixed `AUDIO_BOOST_DB` gain, default 0 =
passthrough). Hosts that fail processing (e.g. encrypted) are detected once
and passed through afterwards. Only full (200) responses are processed;
ranged (206) requests pass through. With
`INCLUDE_PROXY_FALLBACK=1` an extra `Vidora Proxy` entry is served through
this addon's own `/proxy` (Range-capable, host-allowlisted via `PROXY_ALLOW`).
