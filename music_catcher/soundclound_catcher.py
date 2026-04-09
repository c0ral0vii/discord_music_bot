from __future__ import annotations

from pathlib import Path

from music_catcher.youtube_catcher import DownloadedTrack, YouTubeCatcher, YouTubeDownloadError


class SoundCloudDownloadError(Exception):
    """Raised when audio cannot be downloaded from SoundCloud."""


class SoundCloudCatcher:
    def __init__(self, output_dir: Path | str = "downloads") -> None:
        self._downloader = YouTubeCatcher(output_dir=output_dir)

    def download_audio(self, url: str) -> DownloadedTrack:
        try:
            return self._downloader.download_audio(url)
        except YouTubeDownloadError as exc:
            raise SoundCloudDownloadError("Не удалось скачать аудио с SoundCloud.") from exc
