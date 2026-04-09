from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from music_catcher.youtube_catcher import DownloadedTrack, YouTubeCatcher, YouTubeDownloadError


class SpotifyDownloadError(Exception):
    """Raised when a Spotify URL cannot be resolved for playback."""


@dataclass(slots=True)
class SpotifyQueueTrack:
    title: str
    search_query: str
    duration_seconds: float | None = None


class SpotifyCatcher:
    _NEXT_DATA_PATTERN = re.compile(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
    )

    def __init__(self, output_dir: Path | str = "downloads") -> None:
        self._downloader = YouTubeCatcher(output_dir=output_dir)

    def download_audio(self, url: str) -> DownloadedTrack:
        tracks = self.expand_to_queries(url)
        if not tracks:
            raise SpotifyDownloadError("Не удалось прочитать данные трека Spotify.")
        query = tracks[0].search_query
        try:
            return self._downloader.download_audio(query)
        except YouTubeDownloadError as exc:
            raise SpotifyDownloadError(
                "Не удалось найти и скачать этот Spotify трек через YouTube."
            ) from exc

    def expand_to_queries(self, url: str) -> list[SpotifyQueueTrack]:
        if "spotify.com" not in url:
            raise SpotifyDownloadError("Ссылка не похожа на Spotify URL.")

        embed_url = self._build_embed_url(url)
        if embed_url:
            tracks = self._extract_tracks_from_embed(embed_url)
            if tracks:
                return tracks

        title = self._fetch_oembed_title(url)
        if not title:
            raise SpotifyDownloadError("Не удалось прочитать данные Spotify ссылки.")
        return [SpotifyQueueTrack(title=title, search_query=f"ytsearch1:{title} audio")]

    @staticmethod
    def _build_embed_url(url: str) -> str | None:
        parsed = urlparse(url)
        path_parts = [part for part in parsed.path.split("/") if part]
        if not path_parts:
            return None

        if path_parts[0] == "embed":
            if len(path_parts) >= 3:
                kind, entity_id = path_parts[1], path_parts[2]
            else:
                return None
        else:
            if len(path_parts) < 2:
                return None
            kind, entity_id = path_parts[0], path_parts[1]

        if kind not in {"track", "album", "playlist"}:
            return None
        return f"https://open.spotify.com/embed/{kind}/{entity_id}"

    def _extract_tracks_from_embed(self, embed_url: str) -> list[SpotifyQueueTrack]:
        request = Request(embed_url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urlopen(request, timeout=15) as response:  # noqa: S310
                html = response.read().decode("utf-8", errors="ignore")
        except Exception:
            return []

        match = self._NEXT_DATA_PATTERN.search(html)
        if not match:
            return []

        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []

        page_props = payload.get("props", {}).get("pageProps", {})
        if not isinstance(page_props, dict):
            return []

        ordered_tracks: list[SpotifyQueueTrack] = []
        seen_uris: set[str] = set()

        def walk(node: object) -> None:
            if isinstance(node, dict):
                uri = node.get("uri")
                if isinstance(uri, str) and uri.startswith("spotify:track:") and uri not in seen_uris:
                    title = str(node.get("title", "")).strip()
                    subtitle = str(node.get("subtitle", "")).replace("\xa0", " ").strip()
                    if title:
                        query_name = f"{subtitle} - {title}" if subtitle else title
                        duration_raw = node.get("duration")
                        duration_seconds = None
                        if isinstance(duration_raw, (int, float)) and duration_raw > 0:
                            duration_seconds = float(duration_raw) / 1000.0
                        ordered_tracks.append(
                            SpotifyQueueTrack(
                                title=query_name,
                                search_query=f"ytsearch1:{query_name} audio",
                                duration_seconds=duration_seconds,
                            )
                        )
                        seen_uris.add(uri)

                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(page_props)
        return ordered_tracks

    @staticmethod
    def _fetch_oembed_title(url: str) -> str | None:
        endpoint = f"https://open.spotify.com/oembed?url={quote(url, safe='')}"
        request = Request(
            endpoint,
            headers={"User-Agent": "darkmusic-bot/1.0"},
        )
        try:
            with urlopen(request, timeout=10) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return None

        title = str(payload.get("title", "")).strip()
        return title or None
