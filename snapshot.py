"""Publish a bounded, token-free ListenBrainz snapshot using only the stdlib."""
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

USER = "therealnyg8"
API = f"https://api.listenbrainz.org/1/user/{USER}/listens"
DAYS = 90
PAGE_SIZE = 1000
MAX_PAGES = 10


def utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def request_json(params: dict) -> dict:
    repository = os.environ.get("GITHUB_REPOSITORY")
    contact = f"https://github.com/{repository}" if repository else f"https://listenbrainz.org/user/{USER}/"
    request = Request(
        f"{API}?{urlencode(params)}",
        headers={"User-Agent": f"ListenBrainzSnapshot/1.0 ({contact})", "Accept": "application/json"},
    )
    for attempt in range(5):
        try:
            with urlopen(request, timeout=45) as response:
                data = json.load(response)
                if response.headers.get("X-RateLimit-Remaining") == "0":
                    time.sleep(float(response.headers.get("X-RateLimit-Reset-In", "5")) + 1)
                return data
        except HTTPError as error:
            if error.code != 429 and not 500 <= error.code < 600:
                raise
            if attempt == 4:
                raise
            delay = error.headers.get("Retry-After") or error.headers.get("X-RateLimit-Reset-In")
            time.sleep(float(delay) + 1 if delay and delay.isdigit() else 2 ** (attempt + 1))
        except (URLError, TimeoutError, HTTPException):
            if attempt == 4:
                raise
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError("Retry limit exhausted")


def fetch_recent(now: int, fetch=request_json) -> tuple[list[dict], bool, int]:
    cutoff, cursor = now - DAYS * 86400, now + 1
    records = {}
    for page_number in range(1, MAX_PAGES + 1):
        payload = fetch({"count": PAGE_SIZE, "max_ts": cursor})["payload"]
        if payload.get("user_id", USER) != USER:
            raise ValueError("ListenBrainz returned a different user")
        page = payload["listens"]
        if not isinstance(page, list):
            raise ValueError("Invalid listens response")
        if not page:
            return list(records.values()), True, page_number
        timestamps = [item["listened_at"] for item in page]
        if any(type(ts) is not int or ts >= cursor for ts in timestamps):
            raise ValueError("Invalid timestamp or pagination response")
        oldest = min(timestamps)
        for item in page:
            if cutoff <= item["listened_at"] <= now:
                records[json.dumps(item, sort_keys=True)] = item
        if oldest < cutoff or len(page) < PAGE_SIZE:
            return list(records.values()), True, page_number
        # Re-fetch the boundary second so equal timestamps are not skipped.
        next_cursor = oldest + 1
        if next_cursor >= cursor:
            raise RuntimeError("Pagination cannot advance past a full timestamp bucket")
        cursor = next_cursor
        time.sleep(1)
    return list(records.values()), False, MAX_PAGES


def normalize(records: list[dict]) -> list[dict]:
    listens = {}
    for record in sorted(records, key=lambda item: json.dumps(item, sort_keys=True)):
        metadata = record["track_metadata"]
        artist, track = metadata["artist_name"].strip(), metadata["track_name"].strip()
        album = (metadata.get("release_name") or "").strip()
        if not artist or not track:
            raise ValueError("Listen missing artist or track")
        key = (record["listened_at"], artist.casefold(), track.casefold(), album.casefold())
        info = metadata.get("additional_info") or {}
        mapping = metadata.get("mbid_mapping") or {}
        listen = listens.setdefault(key, {
            "listened_at": record["listened_at"], "listened_at_utc": utc(record["listened_at"]),
            "artist": artist, "track": track, "album": album or None,
            "musicbrainz": {}, "submission_clients": [],
        })
        for field in ("artist_mbids", "recording_mbid", "release_mbid", "release_group_mbid"):
            value = mapping.get(field) or info.get(field)
            if value and field not in listen["musicbrainz"]:
                listen["musicbrainz"][field] = value
        client = info.get("submission_client")
        if client and client not in listen["submission_clients"]:
            listen["submission_clients"].append(client)
    return sorted(listens.values(), key=lambda item: (-item["listened_at"], item["artist"], item["track"], item["album"] or ""))


def aggregate(listens: list[dict]) -> dict:
    artists = Counter(item["artist"] for item in listens)
    albums = Counter((item["artist"], item["album"]) for item in listens if item["album"])
    album_tracks = {}
    album_latest = {}
    for item in listens:
        key = (item["artist"], item["album"])
        album_tracks.setdefault(key, set()).add(item["track"])
        album_latest[key] = max(album_latest.get(key, 0), item["listened_at"])
    return {
        "listen_count": len(listens),
        "unique_artists": len(artists),
        "unique_albums": len(albums),
        "listens_without_album": sum(item["album"] is None for item in listens),
        "artists": [{"artist": artist, "listen_count": count}
                    for artist, count in sorted(artists.items(), key=lambda pair: (-pair[1], pair[0]))],
        "albums": [{"artist": artist, "album": album, "listen_count": count,
                    "unique_tracks": len(album_tracks[(artist, album)]),
                    "last_listened_at_utc": utc(album_latest[(artist, album)])}
                   for (artist, album), count in sorted(albums.items(), key=lambda pair: (-pair[1], pair[0]))],
    }


def build_snapshot(records: list[dict], complete: bool, pages: int, now: int) -> dict:
    listens = normalize(records)
    return {
        "schema_version": 1, "user": USER, "generated_at_utc": utc(now),
        "source": {"profile_url": f"https://listenbrainz.org/user/{USER}/", "api_url": API},
        "coverage": {
            "requested_days": DAYS, "from_utc": utc(now - DAYS * 86400), "through_utc": utc(now),
            "complete": complete, "pages_fetched": pages, "max_pages": MAX_PAGES,
            "raw_listen_count": len(records), "deduplicated_listen_count": len(listens),
            "duplicates_removed": len(records) - len(listens),
            "oldest_listen_utc": listens[-1]["listened_at_utc"] if listens else None,
            "newest_listen_utc": listens[0]["listened_at_utc"] if listens else None,
        },
        "counting_notes": [
            "Counts represent track listens, not completed album plays.",
            "Exact timestamp, artist, track and album matches are merged after trimming and case-folding names.",
            "Different timestamps are retained; near-duplicate imports may remain.",
            "Album rankings group by submitted artist and album names; editions may appear separately.",
            "All aggregates use fetched, deduplicated data. Check coverage.complete before interpreting totals.",
        ],
        "aggregates": {
            f"last_{days}_days": aggregate([item for item in listens if item["listened_at"] >= now - days * 86400])
            for days in (7, 30, 90)
        },
        "recent_listens": listens,
    }


def main() -> None:
    now = int(time.time())
    records, complete, pages = fetch_recent(now)
    snapshot = build_snapshot(records, complete, pages, now)
    output = Path(f"{USER}.json")
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(json.dumps(snapshot["coverage"], indent=2))


if __name__ == "__main__":
    main()
