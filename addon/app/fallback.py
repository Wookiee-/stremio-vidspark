"""Fallback provider: the moviesapi/nextgencloudfabric backend behind
vidspark.to's iframe fallback (player.moviesapi.vip).

API rules (probed live):
- movies: ?imdb={tt}&type=movie (+ optional source=) — tmdb form 404s
- tv:     ?source={justhd,hdtoday}&tmdb={tmdb}&type=tv&season=&episode=
          (also imdb forms); plain tmdb without source 404s
- headers: Referer/Origin https://nextgencloudfabric.com (else app-404)
- response data.stream_urls[]: 3 packs, each a master m3u8 URL with
  360p + 720p variants; segments are sequential page-N.html files.
"""

import httpx

from .vidora import UA, fetch_variants

API = "https://streamdata.vaplayer.ru/api.php"
ORIGIN = "https://nextgencloudfabric.com"
SOURCES = ["justhd", "hdtoday"]
SOURCE_LABEL = {"justhd": "Source 1", "hdtoday": "Source 2"}


def headers() -> dict:
    return {"User-Agent": UA, "Referer": ORIGIN + "/", "Origin": ORIGIN}


async def _query(client, params: dict):
    r = await client.get(API, params=params, headers=headers())
    if r.status_code != 200:
        return None
    try:
        d = r.json()
    except Exception:
        return None
    if str(d.get("status_code")) != "200":
        return None
    data = d.get("data") or {}
    if not data.get("stream_urls"):
        return None
    return data


async def fetch_fallback(
    client,
    kind: str,
    tmdb: str | None = None,
    imdb: str | None = None,
    season: int | None = None,
    episode: int | None = None,
) -> list:
    """Return Vidora-shaped sources: directUrl=master, variants, mirrors."""
    queries: list[dict] = []
    if kind == "movie":
        if imdb:
            for s in SOURCES:
                queries.append({"source": s, "imdb": imdb, "type": "movie"})
    else:
        if tmdb:
            for s in SOURCES:
                queries.append(
                    {
                        "source": s,
                        "tmdb": tmdb,
                        "type": "tv",
                        "season": season,
                        "episode": episode,
                    }
                )
        if imdb:
            for s in SOURCES:
                queries.append(
                    {
                        "source": s,
                        "imdb": imdb,
                        "type": "tv",
                        "season": season,
                        "episode": episode,
                    }
                )
    out = []
    for q in queries:
        source = q["source"]
        try:
            data = await _query(client, q)
        except (httpx.ConnectError, httpx.TimeoutException, OSError):
            continue
        except Exception:
            continue
        if not data:
            continue
        packs = data["stream_urls"]
        variants = await fetch_variants(client, packs[0], ORIGIN + "/", headers())
        out.append(
            {
                "file_code": source,
                "source": f"fallback-{source}",
                "title": data.get("title"),
                "directUrl": packs[0],
                "headers": headers(),
                "variants": variants,
                "mirrors": packs[1:],
                "tracks": [],
            }
        )
    return out
