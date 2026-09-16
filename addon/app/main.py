"""VidSpark Stremio addon — pure Python (FastAPI + httpx over HTTP/2).

Stremio -> :7002/stream/movie|series/{id}.json -> Vidora (direct, no sidecar)
Direct-stream mode: returns the upstream https HLS URL with
behaviorHints.proxyHeaders so Stremio sends Referer/Origin itself (2xx).
Set INCLUDE_PROXY_FALLBACK=1 to also append this addon's own /proxy URL
(single public port — playlists/segments proxied with Range support).
"""

import base64
import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import vidora
from . import fallback as fb_provider

ADDON_NAME = os.environ.get("ADDON_NAME", "VidSpark")
INCLUDE_PROXY_FALLBACK = os.environ.get("INCLUDE_PROXY_FALLBACK", "0") == "1"
# 1 = include subtitles in emitted streams. Default off.
INCLUDE_SUBS = os.environ.get("INCLUDE_SUBS", "0") == "1"
# 0 = /proxy returns 403 and no proxy URLs are emitted: video bytes can
# never flow through this server (only tiny JSON resolve calls do).
PROXY_ENABLED = os.environ.get("PROXY_ENABLED", "1") == "1"
# Gain (dB) applied to proxied TS segments via ffmpeg (video untouched).
# Fallback rips measure ~-28 LUFS; +10dB lands near streaming norms without
# clipping (true peak was -14dBTP). 0 = passthrough. Needs ffmpeg in PATH.
try:
    AUDIO_BOOST_DB = float(os.environ.get("AUDIO_BOOST_DB", "0"))
except ValueError:
    AUDIO_BOOST_DB = 0.0
# Normalize everything toward one integrated loudness (LUFS) so all titles
# play at the same level. Single-pass live mode, no pre-measure needed.
# 0 = off (falls back to AUDIO_BOOST_DB fixed gain above).
try:
    AUDIO_TARGET_LUFS = float(os.environ.get("AUDIO_TARGET_LUFS", "-24"))
except ValueError:
    AUDIO_TARGET_LUFS = 0.0
_ffmpeg: str | None = shutil.which("ffmpeg")
# Hosts whose segments failed boosting (e.g. encrypted) — passthrough.
_boost_skip: set = set()
# Host suffixes the /proxy endpoint will fetch (open-proxy guard).
# Fallback CDN hosts rotate per title (xyz.site), hence the bare "site".
PROXY_ALLOW = tuple(
    s.strip().lower()
    for s in os.environ.get(
        "PROXY_ALLOW", "netrocdn.site,workers.dev,vidspark.to,site"
    ).split(",")
    if s.strip()
)

# Fail fast while Vidora is known-unreachable (connectivity errors only).
_down_until = 0.0
_client: httpx.AsyncClient | None = None
_fallback: httpx.AsyncClient | None = None


def _shared_client() -> httpx.AsyncClient:
    # Lifespan client when running under Granian; lazy process-wide
    # singleton otherwise (e.g. lifespan unsupported) — never per-request.
    # HTTP/2 + keepalive: one socket reused instead of a TLS handshake call.
    global _fallback
    if _client is not None:
        return _client
    if _fallback is None:
        _fallback = httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=10.0),
            follow_redirects=True,
            http2=True,
        )
    return _fallback


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(25.0, connect=10.0),
        follow_redirects=True,
        http2=True,
        limits=httpx.Limits(max_keepalive_connections=4, max_connections=10),
    )
    yield
    await _client.aclose()
    _client = None


MANIFEST = {
    "id": "community.vidspark",
    "version": "0.2.0",
    "name": ADDON_NAME,
    "description": "VidSpark (Vidora backend) direct streams. Movies + series by IMDb/TMDB id.",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt", "tmdb:"],
    "catalogs": [],
    "behaviorHints": {"configurable": False},
}

LANG_MAP = {
    "english": "eng", "spanish": "spa", "french": "fre", "arabic": "ara",
    "czech": "cze", "danish": "dan", "german": "ger", "greek": "gre",
    "finnish": "fin", "fil": "fil", "hebrew": "heb", "croatian": "hrv",
    "hungarian": "hun", "ind": "ind", "italian": "ita", "japanese": "jpn",
    "korean": "kor", "may": "may", "nob": "nob", "dutch": "dut",
    "polish": "pol", "portuguese": "por", "romanian": "rum", "russian": "rus",
    "swedish": "swe", "thai": "tha", "turkish": "tur", "ukrainian": "ukr",
    "vietnamese": "vie", "chinese": "chi",
}

app = FastAPI(title="stremio-vidspark", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def lang_for(label: str) -> str:
    key = (label or "").strip().lower()
    if key in LANG_MAP:
        return LANG_MAP[key]
    short = re.sub(r"[^a-z]", "", key)[:3]
    return short or "und"


def strip_json(v: str) -> str:
    return v[:-5] if v.endswith(".json") else v


def strip_tmdb(v: str) -> str:
    # Accept tmdb:82684 / tmdb:82684:1:1 alongside raw tt../numeric ids.
    return v[len("tmdb:"):] if v.startswith("tmdb:") else v


def b64e(u: str) -> str:
    return base64.urlsafe_b64encode(u.encode()).decode()


def b64d(s: str) -> str:
    return base64.urlsafe_b64decode(s.encode()).decode()


@app.get("/", response_class=HTMLResponse)
def index():
    return (
        "<h1>VidSpark Stremio addon</h1>"
        "<p>Manifest: <a href='/manifest.json'>/manifest.json</a></p>"
        "<p>Example: <code>/stream/movie/tt6604188.json</code> "
        "<code>/stream/series/tt4574334:5:3.json</code></p>"
    )


@app.get("/manifest.json")
def manifest():
    return JSONResponse(MANIFEST)


@app.get("/health")
def health():
    return {"ok": True, "mode": "pure-python", "proxy_fallback": INCLUDE_PROXY_FALLBACK,
            "allow": list(PROXY_ALLOW), "loudnorm": AUDIO_TARGET_LUFS, "boost": AUDIO_BOOST_DB,
            "proxy": PROXY_ENABLED}


async def resolve_title(ctype: str, sid: str) -> dict:
    global _down_until
    if time.time() < _down_until:
        raise ConnectionError("backends backing off")
    sid = strip_tmdb(strip_json(sid))
    client = _shared_client()
    vidora_payload: dict | None = None
    fb_sources: list = []
    try:
        if ctype == "movie":
            imdb = sid if sid.startswith("tt") else None
            tid = sid if not imdb else await vidora.tmdb_id(client, sid, "movie")
            try:
                vidora_payload = await vidora.fetch_resolve(client, "movie", tid)
            except (httpx.ConnectError, httpx.TimeoutException, OSError) as e:
                raise ConnectionError(f"vidora unreachable: {e}") from e
            except Exception:
                pass  # content error — fallback may still have it
            if not imdb:
                imdb = await vidora.imdb_id(client, tid)
            try:
                fb_sources = await fb_provider.fetch_fallback(client, "movie", imdb=imdb)
            except Exception:
                pass
        elif ctype == "series":
            # Stremio: tt...:season:episode (also allow tmdb:season:episode)
            parts = sid.split(":")
            if len(parts) != 3:
                raise ValueError("series id must look like tt4574334:5:3")
            sid_id, season, episode = parts
            imdb = sid_id if sid_id.startswith("tt") else None
            tid = sid_id if not imdb else await vidora.tmdb_id(client, sid_id, "tv")
            try:
                vidora_payload = await vidora.fetch_resolve(
                    client, "tv", tid, int(season), int(episode)
                )
            except (httpx.ConnectError, httpx.TimeoutException, OSError) as e:
                raise ConnectionError(f"vidora unreachable: {e}") from e
            except Exception:
                pass  # content error — fallback may still have it
            try:
                fb_sources = await fb_provider.fetch_fallback(
                    client, "tv", tmdb=tid, imdb=imdb,
                    season=int(season), episode=int(episode),
                )
            except Exception:
                pass
        else:
            raise ValueError("unsupported type")
    except (httpx.ConnectError, httpx.TimeoutException, OSError, ConnectionError) as e:
        # Unreachable for everyone — back off globally, fail fast.
        _down_until = time.time() + 30
        raise ConnectionError(f"backends unreachable: {e}") from e
    # Anything else (HTTP errors, bad playlist for one title): single-title
    # failure, must NOT penalize other titles — no global cooldown.
    base = vidora_payload or {}
    return {
        "type": base.get("type", ctype),
        "title": base.get("title") or (fb_sources[0].get("title") if fb_sources else ""),
        "year": base.get("year", ""),
        "ref": base.get("ref", ""),
        "sources": base.get("sources", []),
        "fallback": fb_sources,
    }


def res_label(v: dict) -> str:
    """Quality label ported from the site's own player (getQualityLabel):
    nearest standard WIDTH -> 4K/1440p/1080p/720p/480p/360p/240p."""
    standards = [
        (3840, "4K"), (2560, "1440p"), (1920, "1080p"), (1280, "720p"),
        (854, "480p"), (640, "360p"), (426, "240p"),
    ]
    width = v.get("width") or 0
    if not width:
        return "Auto"
    return min(standards, key=lambda s: abs(width - s[0]))[1]


FB_SERVER_LABEL = {"justhd": "justhd", "hdtoday": "hdtoday"}


def _proxy_url(base: str, target: str, ref: str) -> str:
    return f"{base}/proxy?u={b64e(target)}&ref={ref}"


def to_stremio_streams(payload: dict, ctype: str, proxy_base: str = "") -> list:
    title = payload.get("title", "")
    year = payload.get("year", "")
    label = f"{title} ({year})" if year else title
    ref = payload.get("ref", "")
    streams = []
    for s in payload.get("sources", []) or []:
        if not s.get("directUrl"):
            continue
        # Subtitles play direct (verified 200 with no special headers).
        subs = [
            {"url": t["directUrl"], "lang": lang_for(t.get("label", ""))}
            for t in (s.get("tracks") or [])
            if t.get("directUrl")
        ][:12] if INCLUDE_SUBS else []
        proxy_headers = {"request": s.get("headers") or {}}
        base_hints = {
            "bingeGroup": "vidspark-vidora",
            "notWebReady": False,
            "proxyHeaders": proxy_headers,
        }
        variants = sorted(
            (s.get("variants") or []), key=lambda v: v.get("bandwidth", 0), reverse=True
        )
        if variants:
            # Auto (adaptive master) first, then one direct entry per quality.
            streams.append(
                {
                    "name": "VidSpark",
                    "title": f"{label}\nVidora • Auto",
                    "url": s["directUrl"],
                    "behaviorHints": dict(base_hints),
                    "subtitles": subs,
                }
            )
            for v in variants:
                streams.append(
                    {
                        "name": "VidSpark",
                        "title": f"{label}\nVidora • {res_label(v)}",
                        "url": v["directUrl"],
                        "behaviorHints": dict(base_hints),
                        "subtitles": subs,
                    }
                )
        else:
            # Master parse failed — single adaptive entry.
            streams.append(
                {
                    "name": "VidSpark",
                    "title": f"{label}\nVidora • Auto",
                    "url": s["directUrl"],
                    "behaviorHints": dict(base_hints),
                    "subtitles": subs,
                }
            )
        if INCLUDE_PROXY_FALLBACK:
            streams.append(
                {
                    "name": "VidSpark",
                    "title": f"{label}\nVidora • Proxy",
                    "url": _proxy_url(proxy_base, s["directUrl"], ref),
                    "behaviorHints": {
                        "bingeGroup": "vidspark-vidora",
                        "notWebReady": True,
                    },
                    "subtitles": subs,
                }
            )
    for s in payload.get("fallback", []) or []:
        if not s.get("directUrl"):
            continue
        server = FB_SERVER_LABEL.get(s.get("file_code", ""), s.get("file_code", ""))
        fb_hints = {
            "bingeGroup": "vidspark-fallback",
            "notWebReady": False,
            "proxyHeaders": {"request": s.get("headers") or {}},
        }
        variants = sorted(
            (s.get("variants") or []), key=lambda v: v.get("bandwidth", 0), reverse=True
        )
        streams.append(
            {
                "name": "VidSpark",
                "title": f"{label}\n{server} • Auto",
                "url": s["directUrl"],
                "behaviorHints": dict(fb_hints),
                "subtitles": [],
            }
        )
        for v in variants:
            streams.append(
                {
                    "name": "VidSpark",
                    "title": f"{label}\n{server} • {res_label(v)}",
                    "url": v["directUrl"],
                    "behaviorHints": dict(fb_hints),
                    "subtitles": [],
                }
            )
        for i, m in enumerate(s.get("mirrors") or [], 1):
            streams.append(
                {
                    "name": "VidSpark",
                    "title": f"{label}\n{server} • Mirror {i}",
                    "url": m,
                    "behaviorHints": dict(fb_hints),
                    "subtitles": [],
                }
            )
    return streams


def _proxy_allowed(url: str) -> bool:
    try:
        host = httpx.URL(url).host.lower()
    except Exception:
        return False
    return any(host == s or host.endswith("." + s) for s in PROXY_ALLOW)


def _rewrite_m3u8(text: str, playlist_url: str, base: str, ref: str) -> str:
    from urllib.parse import urljoin

    def abs_proxy(uri: str) -> str:
        return f"{base}/proxy?u={b64e(urljoin(playlist_url, uri))}&ref={ref}"

    out = []
    for line in text.split("\n"):
        t = line.strip()
        if not t or t.startswith("#"):
            line = re.sub(
                r'URI="([^"]+)"',
                lambda m: f'URI="{abs_proxy(m.group(1))}"',
                line,
            )
        else:
            line = abs_proxy(t)
        out.append(line)
    return "\n".join(out)


_PROXY_PASS_HEADERS = {"content-type", "content-range", "accept-ranges", "cache-control"}


def _audio_filter() -> str | None:
    if AUDIO_TARGET_LUFS:
        return f"loudnorm=I={AUDIO_TARGET_LUFS}:TP=-1.5:LRA=11,aresample=48000"
    if AUDIO_BOOST_DB:
        return f"volume={AUDIO_BOOST_DB}dB,alimiter=limit=0.95,aresample=48000"
    return None


def _boost_audio(buf: bytes, host: str) -> bytes | None:
    """Normalize segment audio toward one LUFS target (or fixed gain);
    None = passthrough (off/unavailable/failed).

    Small bodies (keys, VTTs) are never touched and never mark the host —
    only segment-sized undecodable bodies (e.g. encrypted) disable future
    attempts for that host.
    """
    af = _audio_filter()
    if not af or not _ffmpeg or host in _boost_skip:
        return None
    if len(buf) < 100_000:
        return None
    try:
        p = subprocess.run(
            [
                _ffmpeg, "-hide_banner", "-loglevel", "error",
                "-i", "pipe:0",
                "-c:v", "copy",
                "-af", af,
                "-c:a", "aac", "-b:a", "128k",
                "-f", "mpegts", "pipe:1",
            ],
            input=buf,
            capture_output=True,
            timeout=60,
        )
    except Exception:
        return None
    if p.returncode != 0 or p.stdout[:1] != b"\x47":  # TS sync byte
        _boost_skip.add(host)
        return None
    return p.stdout


@app.get("/proxy")
async def proxy_fetch(request: Request):
    if not PROXY_ENABLED:
        return JSONResponse({"error": "proxy disabled"}, status_code=403)
    enc = request.query_params.get("u", "")
    ref = request.query_params.get("ref", "") or "https://vidspark.to/movie/533533"
    try:
        target = b64d(enc)
        assert target.startswith(("http://", "https://"))
    except Exception:
        return JSONResponse({"error": "bad url"}, status_code=400)
    if not _proxy_allowed(target):
        return JSONResponse({"error": "host not allowed"}, status_code=403)
    # Origin must match the backend the URL came from (Vidora <-> vidspark.to,
    # fallback CDN <-> nextgencloudfabric.com) — derive it from ref.
    from urllib.parse import urlsplit

    _rp = urlsplit(ref)
    _origin = f"{_rp.scheme}://{_rp.netloc}" if _rp.netloc else vidora.BASE
    fwd = {"User-Agent": vidora.UA, "Origin": _origin, "Referer": ref}
    if "range" in request.headers:
        fwd["range"] = request.headers["range"]
    try:
        r = await _shared_client().get(target, headers=fwd)
    except (httpx.ConnectError, httpx.TimeoutException, OSError) as e:
        return JSONResponse({"error": f"upstream unreachable: {e}"}, status_code=502)
    if r.status_code not in (200, 206):
        return JSONResponse({"error": f"upstream {r.status_code}"}, status_code=r.status_code)
    headers = {k: v for k, v in r.headers.items() if k.lower() in _PROXY_PASS_HEADERS}
    headers["access-control-allow-origin"] = "*"
    ct = r.headers.get("content-type", "")
    if target.endswith(".m3u8") or "mpegurl" in ct or "x-mpegurl" in ct:
        base = str(request.base_url).rstrip("/")
        return Response(
            content=_rewrite_m3u8(r.text, target, base, ref),
            status_code=r.status_code,
            headers={**headers, "content-type": "application/vnd.apple.mpegurl"},
        )
    body = r.content
    if r.status_code == 200:
        try:
            host = httpx.URL(target).host.lower()
        except Exception:
            host = ""
        boosted = await asyncio.to_thread(_boost_audio, body, host)
        if boosted is not None:
            headers.pop("content-range", None)
            headers["content-length"] = str(len(boosted))
            return Response(content=boosted, status_code=200, headers=headers)
    return Response(content=body, status_code=r.status_code, headers=headers)


@app.get("/stream/{ctype}/{sid}")
async def stream(ctype: str, sid: str, request: Request):
    try:
        payload = await resolve_title(ctype, sid)
    except Exception as e:
        # Stremio clients expect {streams: []} on failure, not 500
        return JSONResponse({"streams": [], "error": str(e)[:300]})
    base = str(request.base_url).rstrip("/")
    return JSONResponse({"streams": to_stremio_streams(payload, ctype, base)})
