"""Pure-Python Vidora resolver (ex-sidecar). All HTTP via the shared
httpx client (HTTP/2 + keepalive) passed in by the caller."""

import asyncio
import os
import re
import time

BASE = os.environ.get("VIDSPARK_BASE", "https://vidspark.to").rstrip("/")
FALLBACK_KEY = os.environ.get(
    "VIDSPARK_PLAYER_KEY",
    "3a67e8866ae1d2bb9e81fe7f73315a56eb3bdf5e3e755c7554c8be6910aa6b13",
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
KEY_TTL = 3600

_key: str | None = None
_key_at = 0.0
_key_lock = asyncio.Lock()

# IMDb -> TMDB cache (never expires, like upstream ids themselves).
_tmdb_ids: dict = {}
# TMDB -> IMDb cache for the fallback movie API (imdb-keyed).
_imdb_ids: dict = {}
# Vidora catalogs some titles under the wrong TMDB id. Map real -> theirs.
# e.g. Reacher: real TMDB 82684, Vidora lists it as 108978.
TMDB_OVERRIDES = {"82684": "108978", "tt13603966": "108978"}


def headers_for(ref: str) -> dict:
    return {"User-Agent": UA, "Origin": BASE, "Referer": ref}


def ref_for(kind: str, id: str, season=None, episode=None) -> str:
    if kind == "tv":
        return f"{BASE}/tv/{id}/{season}/{episode}"
    return f"{BASE}/movie/{id}"


async def tmdb_id(client, media_id: str, kind: str) -> str:
    """Map an IMDb id to Vidora's native TMDB key via Cinemeta (cached)."""
    if media_id in _tmdb_ids:
        return _tmdb_ids[media_id]
    if media_id in TMDB_OVERRIDES:
        _tmdb_ids[media_id] = TMDB_OVERRIDES[media_id]
        return _tmdb_ids[media_id]
    if not media_id.startswith("tt"):
        return media_id
    cinemeta_kind = "series" if kind == "tv" else "movie"
    try:
        m = (
            await client.get(
                f"https://v3-cinemeta.strem.io/meta/{cinemeta_kind}/{media_id}.json",
                timeout=15,
            )
        ).json()["meta"]
        tmdb = str(m.get("moviedb_id") or media_id)
    except Exception:
        tmdb = media_id
    tmdb = TMDB_OVERRIDES.get(tmdb, tmdb)
    _tmdb_ids[media_id] = tmdb
    return tmdb


async def imdb_id(client, tmdb: str) -> str | None:
    """Map a TMDB movie id back to IMDb (fallback movie API is imdb-keyed)."""
    if tmdb in _imdb_ids:
        return _imdb_ids[tmdb]
    if tmdb.startswith("tt"):
        return tmdb
    try:
        m = (
            await client.get(
                f"https://v3-cinemeta.strem.io/meta/movie/{tmdb}.json",
                timeout=15,
            )
        ).json()["meta"]
        imdb = m.get("imdb_id") or None
    except Exception:
        imdb = None
    _imdb_ids[tmdb] = imdb
    return imdb


async def get_player_key(client) -> str:
    """Scrape the rotating x-player-key from the frontend JS bundle."""
    global _key, _key_at
    async with _key_lock:
        if _key and time.time() - _key_at < KEY_TTL:
            return _key
        try:
            html = (await client.get(f"{BASE}/movie/533533", headers={"User-Agent": UA})).text
            m = re.search(r'/assets/index-[^"\']+\.js', html)
            if not m:
                raise RuntimeError("js chunk not found")
            js_url = m.group(0) if m.group(0).startswith("http") else BASE + m.group(0)
            js = (await client.get(js_url, headers={"User-Agent": UA})).text
            keys = re.findall(r'[`"\'"]([0-9a-f]{64})[`"\'"]', js)
            if not keys:
                raise RuntimeError("key not found in js")
            _key, _key_at = keys[0], time.time()
        except Exception:
            _key, _key_at = _key or FALLBACK_KEY, time.time()
        return _key


async def fetch_resolve(client, kind: str, id: str, season=None, episode=None) -> dict:
    key = await get_player_key(client)
    path = (
        f"/api/vidora/v1/tv/{id}/{season}/{episode}"
        if kind == "tv"
        else f"/api/vidora/v1/movie/{id}"
    )
    r = await client.get(
        BASE + path,
        headers={**headers_for(ref_for(kind, id, season, episode)), "x-player-key": key},
    )
    if r.status_code != 200:
        raise RuntimeError(f"vidora {r.status_code}: {r.text[:200]}")
    data = r.json()
    ref = ref_for(kind, id, season, episode)
    headers = headers_for(ref)
    sources = []
    for s in data.get("sources") or []:
        if not s.get("url"):
            continue
        variants = await fetch_variants(client, s["url"], ref)
        sources.append(
            {
                "file_code": s.get("file_code", ""),
                "source": s.get("source") or "vidora",
                "directUrl": s["url"],
                "headers": headers,
                "variants": variants,
                "tracks": [
                    {"label": t.get("label") or "sub", "directUrl": t["file"]}
                    for t in (s.get("tracks") or [])
                    if t.get("file")
                ],
            }
        )
    return {
        "type": data.get("type"),
        "title": data.get("title"),
        "tmdb_id": data.get("tmdb_id"),
        "imdb_id": data.get("imdb_id"),
        "year": data.get("year"),
        "season": data.get("season"),
        "episode": data.get("episode"),
        "ref": ref,
        "sources": sources,
    }


def parse_master(text: str, base_url: str) -> list:
    if not text.startswith("#EXTM3U"):
        return []
    lines = text.split("\n")
    out = []

    def attr(attrs: str, k: str):
        m = re.search(rf'{k}=("[^"]+"|[^,]+)', attrs)
        return m.group(1).strip('"') if m else None

    for i, line in enumerate(lines):
        line = line.strip()
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = line[len("#EXT-X-STREAM-INF:"):]
        raw = (lines[i + 1] if i + 1 < len(lines) else "").strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            from urllib.parse import urljoin

            abs_url = urljoin(base_url, raw)
        except Exception:
            continue
        res = attr(attrs, "RESOLUTION") or ""
        m = re.match(r"(\d+)x(\d+)", res)
        try:
            bw = int(attr(attrs, "BANDWIDTH") or 0)
        except ValueError:
            bw = 0
        out.append(
            {
                "directUrl": abs_url,
                "bandwidth": bw,
                "resolution": res or None,
                "width": int(m.group(1)) if m else None,
                "height": int(m.group(2)) if m else None,
                "frameRate": attr(attrs, "FRAME-RATE"),
                "codecs": attr(attrs, "CODECS"),
            }
        )
    return out


async def fetch_variants(client, master_url: str, ref: str, headers: dict | None = None) -> list:
    """Best-effort variant list with resolutions; [] on any failure."""
    try:
        r = await client.get(master_url, headers=headers or headers_for(ref))
        if r.status_code != 200:
            return []
        return parse_master(r.text, master_url)
    except Exception:
        return []
