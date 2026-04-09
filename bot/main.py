from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import discord
from discord.ext import commands

from config.config import settings
from music_catcher.soundclound_catcher import SoundCloudCatcher, SoundCloudDownloadError
from music_catcher.spotify_catcher import SpotifyCatcher, SpotifyDownloadError
from music_catcher.youtube_catcher import DownloadedTrack, YouTubeCatcher, YouTubeDownloadError

intents = discord.Intents.default()
intents.message_content = settings.MESSAGE_CONTENT_INTENT

bot = commands.Bot(command_prefix=settings.PREFIX, intents=intents, help_command=None)
youtube_catcher = YouTubeCatcher(output_dir=Path("downloads"))
soundcloud_catcher = SoundCloudCatcher(output_dir=Path("downloads"))
spotify_catcher = SpotifyCatcher(output_dir=Path("downloads"))

IDLE_DISCONNECT_SECONDS = 300
SEEK_STEP_SECONDS = 10.0


@dataclass(slots=True)
class QueuedTrack:
    url: str
    text_channel_id: int
    requested_by_id: int
    search_query: str | None = None
    title_hint: str | None = None
    duration_seconds: float | None = None


@dataclass(slots=True)
class GuildPlayerState:
    queue: asyncio.Queue[QueuedTrack] = field(default_factory=asyncio.Queue)
    worker_task: asyncio.Task[None] | None = None
    worker_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    now_playing_title: str | None = None
    now_playing_url: str | None = None
    now_playing_requested_by_id: int | None = None
    last_text_channel_id: int | None = None
    current_file_path: Path | None = None
    current_duration_seconds: float | None = None
    current_started_at: float | None = None
    current_offset_seconds: float = 0.0
    pending_seek_seconds: float | None = None
    skip_requested: bool = False
    repeat_track: bool = False
    repeat_queue: bool = False


guild_players: dict[int, GuildPlayerState] = {}


class PlayerMenuView(discord.ui.View):
    def __init__(self, guild_id: int) -> None:
        super().__init__(timeout=300)
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await _send_interaction(interaction, "Это меню не для этого сервера.")
            return False

        voice_protocol = interaction.guild.voice_client
        if not isinstance(voice_protocol, discord.VoiceClient):
            await _send_interaction(interaction, "Бот не подключен к голосовому каналу.")
            return False

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None or member.voice is None or member.voice.channel != voice_protocol.channel:
            await _send_interaction(
                interaction,
                "Нужно находиться в том же голосовом канале, что и бот.",
            )
            return False

        return True

    @discord.ui.button(label="-10s", style=discord.ButtonStyle.secondary)
    async def seek_back(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[discord.ui.View],
    ) -> None:
        message = await _request_seek(self.guild_id, -SEEK_STEP_SECONDS)
        await _send_interaction(interaction, message)

    @discord.ui.button(label="+10s", style=discord.ButtonStyle.secondary)
    async def seek_forward(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[discord.ui.View],
    ) -> None:
        message = await _request_seek(self.guild_id, SEEK_STEP_SECONDS)
        await _send_interaction(interaction, message)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary)
    async def skip_button(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[discord.ui.View],
    ) -> None:
        message = await _request_skip(self.guild_id)
        await _send_interaction(interaction, message)

    @discord.ui.button(label="Repeat Track", style=discord.ButtonStyle.success)
    async def repeat_track_button(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[discord.ui.View],
    ) -> None:
        message = _toggle_repeat_track(self.guild_id)
        await _send_interaction(interaction, message)

    @discord.ui.button(label="Repeat Queue", style=discord.ButtonStyle.success)
    async def repeat_queue_button(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[discord.ui.View],
    ) -> None:
        message = _toggle_repeat_queue(self.guild_id)
        await _send_interaction(interaction, message)


async def _send_interaction(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def _get_state(guild_id: int) -> GuildPlayerState:
    state = guild_players.get(guild_id)
    if state is None:
        state = GuildPlayerState()
        guild_players[guild_id] = state
    return state


def _clear_queue(state: GuildPlayerState) -> int:
    removed = 0
    while True:
        try:
            state.queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        else:
            state.queue.task_done()
            removed += 1
    return removed


def _reset_now_playing(state: GuildPlayerState) -> None:
    state.now_playing_title = None
    state.now_playing_url = None
    state.now_playing_requested_by_id = None


def _reset_runtime_track_state(state: GuildPlayerState) -> None:
    state.current_file_path = None
    state.current_duration_seconds = None
    state.current_started_at = None
    state.current_offset_seconds = 0.0
    state.pending_seek_seconds = None
    state.skip_requested = False


def _voice_has_humans(voice_client: discord.VoiceClient) -> bool:
    return any(not member.bot for member in voice_client.channel.members)


async def _send_text_message(channel_id: int, message: str) -> None:
    channel = bot.get_channel(channel_id)
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        with suppress(discord.HTTPException):
            await channel.send(message)


def _track_label(item: QueuedTrack) -> str:
    return item.title_hint or item.url


def _toggle_repeat_track(guild_id: int) -> str:
    state = _get_state(guild_id)
    state.repeat_track = not state.repeat_track
    if state.repeat_track:
        state.repeat_queue = False
        return "Повтор трека: **ON**. Повтор очереди: **OFF**."
    return "Повтор трека: **OFF**."


def _toggle_repeat_queue(guild_id: int) -> str:
    state = _get_state(guild_id)
    state.repeat_queue = not state.repeat_queue
    if state.repeat_queue:
        state.repeat_track = False
        return "Повтор очереди: **ON**. Повтор трека: **OFF**."
    return "Повтор очереди: **OFF**."


def _build_queue_items(url: str, text_channel_id: int, requested_by_id: int) -> list[QueuedTrack]:
    netloc = urlparse(url).netloc.lower()
    if "spotify.com" in netloc:
        spotify_tracks = spotify_catcher.expand_to_queries(url)
        if not spotify_tracks:
            raise SpotifyDownloadError("Не удалось получить треки из Spotify ссылки.")

        return [
            QueuedTrack(
                url=url,
                text_channel_id=text_channel_id,
                requested_by_id=requested_by_id,
                search_query=track.search_query,
                title_hint=track.title,
                duration_seconds=track.duration_seconds,
            )
            for track in spotify_tracks
        ]

    return [
        QueuedTrack(
            url=url,
            text_channel_id=text_channel_id,
            requested_by_id=requested_by_id,
        )
    ]


def _download_track(item: QueuedTrack) -> DownloadedTrack:
    if item.search_query:
        return youtube_catcher.download_audio(item.search_query)

    netloc = urlparse(item.url).netloc.lower()
    if "spotify.com" in netloc:
        return spotify_catcher.download_audio(item.url)
    if "soundcloud.com" in netloc or "snd.sc" in netloc:
        return soundcloud_catcher.download_audio(item.url)
    return youtube_catcher.download_audio(item.url)


async def _cleanup_worker(guild_id: int) -> None:
    state = _get_state(guild_id)
    async with state.worker_lock:
        current = asyncio.current_task()
        if state.worker_task is current:
            state.worker_task = None


async def _play_downloaded_track(
    state: GuildPlayerState,
    voice_client: discord.VoiceClient,
    track: DownloadedTrack,
    fallback_duration_seconds: float | None,
) -> tuple[Exception | None, bool]:
    loop = asyncio.get_running_loop()
    start_position = 0.0
    state.current_file_path = track.file_path
    state.current_duration_seconds = track.duration_seconds or fallback_duration_seconds
    state.pending_seek_seconds = None
    state.skip_requested = False

    try:
        while True:
            done_event = asyncio.Event()
            playback_error: Exception | None = None

            state.current_started_at = loop.time()
            state.current_offset_seconds = start_position

            before_options = "-nostdin"
            if start_position > 0:
                before_options = f"-nostdin -ss {start_position:.3f}"

            source = discord.FFmpegPCMAudio(
                str(track.file_path),
                before_options=before_options,
            )

            def _after_playback(error: Exception | None) -> None:
                nonlocal playback_error
                playback_error = error
                loop.call_soon_threadsafe(done_event.set)

            voice_client.play(source, after=_after_playback)
            await done_event.wait()

            if state.skip_requested:
                state.skip_requested = False
                return None, True

            seek_to = state.pending_seek_seconds
            if seek_to is not None:
                state.pending_seek_seconds = None
                start_position = max(0.0, seek_to)
                max_duration = state.current_duration_seconds
                if max_duration is not None and start_position >= max_duration:
                    return None, False
                continue

            if playback_error:
                return playback_error, False

            if state.repeat_track:
                start_position = 0.0
                continue

            return None, False
    finally:
        with suppress(OSError):
            track.file_path.unlink(missing_ok=True)
        _reset_runtime_track_state(state)


async def _guild_player_worker(guild_id: int) -> None:
    state = _get_state(guild_id)
    try:
        while True:
            try:
                item = await asyncio.wait_for(
                    state.queue.get(), timeout=IDLE_DISCONNECT_SECONDS
                )
            except asyncio.TimeoutError:
                guild = bot.get_guild(guild_id)
                if guild and guild.voice_client:
                    with suppress(discord.DiscordException):
                        await guild.voice_client.disconnect(force=False)
                if state.last_text_channel_id:
                    await _send_text_message(
                        state.last_text_channel_id,
                        "5 минут без новых треков. Отключаюсь от голосового канала.",
                    )
                break

            try:
                guild = bot.get_guild(guild_id)
                if guild is None:
                    continue

                voice_protocol = guild.voice_client
                if not isinstance(voice_protocol, discord.VoiceClient):
                    await _send_text_message(
                        item.text_channel_id,
                        "Бот не подключен к голосовому каналу. Добавь трек заново.",
                    )
                    continue

                try:
                    track = await asyncio.to_thread(_download_track, item)
                except (
                    YouTubeDownloadError,
                    SoundCloudDownloadError,
                    SpotifyDownloadError,
                ) as exc:
                    await _send_text_message(item.text_channel_id, str(exc))
                    continue

                state.now_playing_title = item.title_hint or track.title
                state.now_playing_url = item.url
                state.now_playing_requested_by_id = item.requested_by_id

                await _send_text_message(
                    item.text_channel_id,
                    f"Сейчас играет: **{state.now_playing_title}**\n"
                    f"Запросил: <@{item.requested_by_id}>",
                )

                try:
                    playback_error, was_skipped = await _play_downloaded_track(
                        state=state,
                        voice_client=voice_protocol,
                        track=track,
                        fallback_duration_seconds=item.duration_seconds,
                    )
                except Exception:
                    await _send_text_message(
                        item.text_channel_id,
                        "Не удалось запустить FFmpeg. Убедись, что ffmpeg установлен.",
                    )
                    continue

                if playback_error:
                    await _send_text_message(
                        item.text_channel_id,
                        "Во время воспроизведения возникла ошибка.",
                    )
                elif state.repeat_queue and not was_skipped:
                    await state.queue.put(replace(item))
            finally:
                _reset_now_playing(state)
                state.queue.task_done()
    except asyncio.CancelledError:
        raise
    finally:
        await _cleanup_worker(guild_id)


async def _ensure_worker_started(guild_id: int) -> None:
    state = _get_state(guild_id)
    async with state.worker_lock:
        if state.worker_task is None or state.worker_task.done():
            state.worker_task = asyncio.create_task(_guild_player_worker(guild_id))


async def _stop_worker(guild_id: int) -> None:
    state = _get_state(guild_id)
    async with state.worker_lock:
        task = state.worker_task
        state.worker_task = None
    if task and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _current_position_seconds(state: GuildPlayerState) -> float | None:
    if state.current_started_at is None:
        return None
    loop = asyncio.get_running_loop()
    elapsed = max(0.0, loop.time() - state.current_started_at)
    return max(0.0, state.current_offset_seconds + elapsed)


async def _request_skip(guild_id: int) -> str:
    guild = bot.get_guild(guild_id)
    voice_protocol = guild.voice_client if guild else None
    if not isinstance(voice_protocol, discord.VoiceClient):
        return "Бот не подключен к голосовому каналу."

    state = _get_state(guild_id)
    if state.current_file_path is None:
        return "Сейчас нечего пропускать."

    state.pending_seek_seconds = None
    state.skip_requested = True

    if voice_protocol.is_playing() or voice_protocol.is_paused():
        voice_protocol.stop()
        return "Пропускаю текущий трек."

    return "Текущий трек уже заканчивается."


async def _request_seek(guild_id: int, delta_seconds: float) -> str:
    guild = bot.get_guild(guild_id)
    voice_protocol = guild.voice_client if guild else None
    if not isinstance(voice_protocol, discord.VoiceClient):
        return "Бот не подключен к голосовому каналу."

    state = _get_state(guild_id)
    if state.current_file_path is None:
        return "Сейчас ничего не играет."

    if not (voice_protocol.is_playing() or voice_protocol.is_paused()):
        return "Сейчас ничего не играет."

    position = _current_position_seconds(state)
    if position is None:
        return "Не удалось определить позицию трека."

    target = max(0.0, position + delta_seconds)
    duration = state.current_duration_seconds
    if duration is not None:
        target = min(target, max(duration - 0.25, 0.0))

    if target == position:
        return "Дальше перематывать нельзя."

    state.skip_requested = False
    state.pending_seek_seconds = target
    voice_protocol.stop()

    if delta_seconds > 0:
        return "Перемотал на 10 секунд вперед."
    return "Перемотал на 10 секунд назад."


async def _ensure_voice_client(
    ctx: commands.Context[commands.Bot],
) -> discord.VoiceClient:
    if not ctx.author.voice or not ctx.author.voice.channel:
        raise commands.CommandError("Сначала зайди в голосовой канал.")

    author_channel = ctx.author.voice.channel
    voice_protocol = ctx.voice_client

    if voice_protocol and not isinstance(voice_protocol, discord.VoiceClient):
        raise commands.CommandError(
            "Текущий voice-клиент не поддерживается этим музыкальным модулем."
        )

    voice_client = cast(discord.VoiceClient | None, voice_protocol)
    if voice_client and voice_client.channel != author_channel:
        raise commands.CommandError(
            f"Бот уже в канале **{voice_client.channel.name}**. "
            "Зайди в этот канал или сначала выполни `!stop`."
        )

    if voice_client is None:
        try:
            connected = await author_channel.connect()
        except RuntimeError as exc:
            error_text = str(exc).lower()
            if "davey library needed" in error_text:
                raise commands.CommandError(
                    "Для голоса не хватает зависимости `davey`. "
                    "Установи зависимости (`uv sync`) и перезапусти бота."
                ) from exc
            raise
        if not isinstance(connected, discord.VoiceClient):
            raise commands.CommandError(
                "Не удалось получить стандартный VoiceClient после подключения."
            )
        voice_client = connected

    return voice_client


@bot.command(name="play")
async def play(ctx: commands.Context[commands.Bot], url: str | None = None) -> None:
    if ctx.guild is None:
        await ctx.send("Эта команда работает только на сервере.")
        return

    if not url:
        await ctx.send("Использование: `!play <url>`")
        return

    try:
        voice_client = await _ensure_voice_client(ctx)
    except commands.CommandError as exc:
        await ctx.send(str(exc))
        return

    try:
        async with ctx.typing():
            items = await asyncio.to_thread(
                _build_queue_items,
                url,
                ctx.channel.id,
                ctx.author.id,
            )
    except SpotifyDownloadError as exc:
        await ctx.send(str(exc))
        return

    state = _get_state(ctx.guild.id)
    state.last_text_channel_id = ctx.channel.id

    start_position = state.queue.qsize() + 1
    if voice_client.is_playing() or voice_client.is_paused():
        start_position += 1

    for item in items:
        await state.queue.put(item)

    await _ensure_worker_started(ctx.guild.id)

    if len(items) == 1:
        if start_position == 1:
            await ctx.send("Трек добавлен. Начинаю загрузку...")
        else:
            await ctx.send(f"Трек добавлен в очередь. Позиция: **{start_position}**.")
        return

    end_position = start_position + len(items) - 1
    await ctx.send(
        f"Добавил **{len(items)}** треков в очередь. "
        f"Позиции: **{start_position}-{end_position}**."
    )


@bot.command(name="skip")
async def skip(ctx: commands.Context[commands.Bot]) -> None:
    if ctx.guild is None:
        await ctx.send("Эта команда работает только на сервере.")
        return
    await ctx.send(await _request_skip(ctx.guild.id))


@bot.command(name="stop")
async def stop(ctx: commands.Context[commands.Bot]) -> None:
    if ctx.guild is None:
        await ctx.send("Эта команда работает только на сервере.")
        return

    state = _get_state(ctx.guild.id)
    removed = _clear_queue(state)

    voice_protocol = ctx.voice_client
    if voice_protocol:
        if isinstance(voice_protocol, discord.VoiceClient):
            if voice_protocol.is_playing() or voice_protocol.is_paused():
                voice_protocol.stop()
        await voice_protocol.disconnect(force=False)

    await _stop_worker(ctx.guild.id)
    _reset_now_playing(state)
    _reset_runtime_track_state(state)

    if removed:
        await ctx.send(f"Остановил воспроизведение и очистил очередь ({removed} треков).")
    else:
        await ctx.send("Остановил воспроизведение и вышел из канала.")


@bot.command(name="queue")
async def queue_command(ctx: commands.Context[commands.Bot]) -> None:
    if ctx.guild is None:
        await ctx.send("Эта команда работает только на сервере.")
        return

    state = _get_state(ctx.guild.id)
    lines: list[str] = []
    modes: list[str] = []
    if state.repeat_track:
        modes.append("repeat track")
    if state.repeat_queue:
        modes.append("repeat queue")
    lines.append(f"Режимы: {', '.join(modes) if modes else 'off'}")
    lines.append("")

    if state.now_playing_title:
        requester = (
            f"<@{state.now_playing_requested_by_id}>"
            if state.now_playing_requested_by_id
            else "unknown"
        )
        lines.append(f"Сейчас играет: **{state.now_playing_title}** (запросил {requester})")
    else:
        lines.append("Сейчас ничего не играет.")

    pending = list(getattr(state.queue, "_queue", []))
    if not pending:
        lines.append("Очередь пуста.")
        await ctx.send("\n".join(lines))
        return

    lines.append("")
    lines.append(f"В очереди: {len(pending)} трек(ов)")
    for idx, item in enumerate(pending[:10], start=1):
        lines.append(f"{idx}. {_track_label(item)} (от <@{item.requested_by_id}>)")
    if len(pending) > 10:
        lines.append(f"... и еще {len(pending) - 10}")

    await ctx.send("\n".join(lines))


@bot.command(name="menu")
async def menu_command(ctx: commands.Context[commands.Bot]) -> None:
    if ctx.guild is None:
        await ctx.send("Эта команда работает только на сервере.")
        return

    voice_protocol = ctx.guild.voice_client
    if not isinstance(voice_protocol, discord.VoiceClient):
        await ctx.send("Бот не подключен к голосовому каналу.")
        return

    if not ctx.author.voice or ctx.author.voice.channel != voice_protocol.channel:
        await ctx.send("Нужно находиться в том же голосовом канале, что и бот.")
        return

    await ctx.send(
        "Меню управления воспроизведением:",
        view=PlayerMenuView(ctx.guild.id),
    )


@bot.command(name="help")
async def help_command(ctx: commands.Context[commands.Bot]) -> None:
    prefix = settings.PREFIX
    help_text = "\n".join(
        [
            "**Доступные команды:**",
            f"`{prefix}play <url>` - добавить трек в очередь (YouTube/SoundCloud/Spotify).",
            f"`{prefix}queue` - показать текущий трек и очередь.",
            f"`{prefix}skip` - перейти к следующему треку.",
            f"`{prefix}menu` - кнопки: -10s / +10s / skip / repeat track / repeat queue.",
            f"`{prefix}stop` - остановить и очистить очередь.",
            f"`{prefix}help` - показать это сообщение.",
        ]
    )
    await ctx.send(help_text)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    guild = member.guild
    voice_protocol = guild.voice_client
    if not isinstance(voice_protocol, discord.VoiceClient):
        return

    bot_channel = voice_protocol.channel
    if before.channel != bot_channel and after.channel != bot_channel:
        return

    if _voice_has_humans(voice_protocol):
        return

    state = _get_state(guild.id)
    removed = _clear_queue(state)
    if voice_protocol.is_playing() or voice_protocol.is_paused():
        voice_protocol.stop()

    with suppress(discord.DiscordException):
        await voice_protocol.disconnect(force=False)

    await _stop_worker(guild.id)
    _reset_now_playing(state)
    _reset_runtime_track_state(state)

    if state.last_text_channel_id:
        if removed:
            await _send_text_message(
                state.last_text_channel_id,
                f"В канале не осталось людей. Отключаюсь и очищаю очередь ({removed} треков).",
            )
        else:
            await _send_text_message(
                state.last_text_channel_id,
                "В канале не осталось людей. Отключаюсь.",
            )


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (id={bot.user.id if bot.user else 'n/a'})")


def run() -> None:
    if not settings.MESSAGE_CONTENT_INTENT:
        print(
            "WARNING: MESSAGE_CONTENT_INTENT=false. "
            "Префиксные команды (!play, !stop) могут не работать."
        )
    bot.run(settings.BOT_TOKEN)


if __name__ == "__main__":
    run()
