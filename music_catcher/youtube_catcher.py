from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp


class YouTubeDownloadError(Exception):
    """Raised when yt-dlp cannot download audio from a URL."""


@dataclass(slots=True)
class DownloadedTrack:
    file_path: Path
    title: str
    duration_seconds: float | None = None


class YouTubeCatcher:
    def __init__(self, output_dir: Path | str = "downloads") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

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

    @staticmethod
    def _normalize_info(info: dict[str, Any]) -> dict[str, Any]:
        entries = info.get("entries")
        if not entries:
            return info

        first_entry = next((entry for entry in entries if entry), None)
        if first_entry is None:
            raise YouTubeDownloadError("Не удалось получить данные трека.")
        return first_entry
