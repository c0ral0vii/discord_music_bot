from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yt_dlp


class YouTubeDownloadError(Exception):
    """Raised when yt-dlp cannot download audio from a URL."""


@dataclass(slots=True)
class DownloadedTrack:
    file_path: Path
    title: str
    duration_seconds: float | None = None


@dataclass(slots=True)
class YouTubeQueueTrack:
    url: str
    title: str
    duration_seconds: float | None = None


class YouTubeCatcher:
    def __init__(self, output_dir: Path | str = "downloads") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def is_playlist_url(url: str) -> bool:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        if "youtube.com" not in netloc:
            return False

        return parsed.path.rstrip("/") == "/playlist" and "list" in parse_qs(parsed.query)

    def download_audio(self, url: str) -> DownloadedTrack:
        ydl_opts: dict[str, Any] = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "outtmpl": str(self.output_dir / "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                info = self._normalize_info(info)
        except Exception as exc:  # pragma: no cover
            raise YouTubeDownloadError("Не удалось скачать аудио с YouTube.") from exc

        requested_downloads = info.get("requested_downloads") or []
        if requested_downloads and requested_downloads[0].get("filepath"):
            filepath = requested_downloads[0]["filepath"]
        else:
            filepath = ydl.prepare_filename(info)

        track_path = Path(filepath)
        if not track_path.exists():
            raise YouTubeDownloadError("Файл после скачивания не найден.")

        title = info.get("title") or "Unknown title"
        duration_raw = info.get("duration")
        duration_seconds = float(duration_raw) if isinstance(duration_raw, (int, float)) else None
        return DownloadedTrack(
            file_path=track_path,
            title=title,
            duration_seconds=duration_seconds,
        )

    def expand_playlist(self, url: str) -> list[YouTubeQueueTrack]:
        if not self.is_playlist_url(url):
            raise YouTubeDownloadError("Ссылка не похожа на YouTube playlist URL.")

        ydl_opts: dict[str, Any] = {
            "extract_flat": "in_playlist",
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # pragma: no cover
            raise YouTubeDownloadError("Не удалось получить треки из YouTube плейлиста.") from exc

        entries = info.get("entries") or []
        if not entries:
            raise YouTubeDownloadError("YouTube плейлист пуст или недоступен.")

        tracks: list[YouTubeQueueTrack] = []
        seen_urls: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue

            entry_url = self._extract_entry_url(entry)
            if not entry_url or entry_url in seen_urls:
                continue

            title = str(entry.get("title") or "").strip() or entry_url
            duration_raw = entry.get("duration")
            duration_seconds = float(duration_raw) if isinstance(duration_raw, (int, float)) else None
            tracks.append(
                YouTubeQueueTrack(
                    url=entry_url,
                    title=title,
                    duration_seconds=duration_seconds,
                )
            )
            seen_urls.add(entry_url)

        if not tracks:
            raise YouTubeDownloadError("Не удалось извлечь видео из YouTube плейлиста.")

        return tracks

    @staticmethod
    def _normalize_info(info: dict[str, Any]) -> dict[str, Any]:
        entries = info.get("entries")
        if not entries:
            return info

        first_entry = next((entry for entry in entries if entry), None)
        if first_entry is None:
            raise YouTubeDownloadError("Не удалось получить данные трека.")
        return first_entry

    @staticmethod
    def _extract_entry_url(entry: dict[str, Any]) -> str | None:
        direct_url = entry.get("url")
        if isinstance(direct_url, str) and direct_url.startswith(("http://", "https://")):
            return direct_url

        webpage_url = entry.get("webpage_url")
        if isinstance(webpage_url, str) and webpage_url.startswith(("http://", "https://")):
            return webpage_url

        video_id = entry.get("id")
        if isinstance(video_id, str) and video_id:
            return f"https://www.youtube.com/watch?v={video_id}"

        return None
